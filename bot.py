# Telegram expenses bot (long polling) using pyTelegramBotAPI + Supabase
# Tested with:
#   pyTelegramBotAPI==4.14.0  supabase==2.4.0  httpx==0.25.2  gotrue==2.4.2  python-dotenv==1.0.1

import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

# --- Load env ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
ALLOWED_TG_IDS = {s.strip() for s in (os.getenv("ALLOWED_TG_IDS", "")).split(",") if s.strip()}

print("=== Startup ===")
print("BOT_TOKEN starts with:", BOT_TOKEN[:12])
print("SUPABASE_URL:", SUPABASE_URL)
print("SR key prefix:", SUPABASE_SERVICE_ROLE_KEY[:10], "…")
print("ALLOWED_TG_IDS:", ALLOWED_TG_IDS)
print("================")

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise SystemExit("Missing env: BOT_TOKEN / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

# --- Supabase client ---
sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def supa_exec(q, label: str):
    """Run a Supabase query and return (data, error). Never throws; logs problems."""
    try:
        res = q.execute()
        err = getattr(res, "error", None)
        data = getattr(res, "data", None)
        if err:
            print(f"[DB][{label}] ERROR:", err)
        return data, err
    except Exception as e:
        print(f"[DB][{label}] EXCEPTION:", repr(e))
        return None, e

# --- Telegram bot ---
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# Keyboard
kb = ReplyKeyboardMarkup(resize_keyboard=True)
kb.add(KeyboardButton("Add expense"))
kb.add(KeyboardButton("See expenses"))
kb.add(KeyboardButton("See last month"))

def is_allowed(tg_user_id: int) -> bool:
    return str(tg_user_id) in ALLOWED_TG_IDS

def ensure_user(tg_user_id: int, chat_id: int):
    """Ensure exp_users row exists for whitelisted user. Returns exp_users.id or None."""
    if not is_allowed(tg_user_id):
        print("[AUTH] Not allowed:", tg_user_id)
        return None

    # 1) read existing
    data, err = supa_exec(
        sb.table("exp_users").select("id").eq("tg_user_id", tg_user_id).maybe_single(),
        "exp_users.select"
    )
    if err:
        return None

    if data:
        user_id = data["id"]
        supa_exec(sb.table("exp_users").update({"tg_chat_id": chat_id}).eq("id", user_id), "exp_users.update")
        return user_id

    # 2) insert new (FK to exp_allowed_tg must allow it)
    ins_data, ins_err = supa_exec(
        sb.table("exp_users").insert({"tg_user_id": tg_user_id, "tg_chat_id": chat_id}),
        "exp_users.insert"
    )
    if ins_err or not ins_data:
        print("[AUTH] Insert failed (not whitelisted in exp_allowed_tg or table missing).")
        return None
    return ins_data[0]["id"]

def set_state(user_id: int, action: str | None, temp_amount: float | None = None):
    supa_exec(
        sb.table("exp_bot_state").upsert({
            "user_id": user_id,
            "pending_action": action,
            "temp_amount": temp_amount,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }, on_conflict="user_id"),
        "exp_bot_state.upsert"
    )

def get_state(user_id: int):
    data, _ = supa_exec(
        sb.table("exp_bot_state").select("*").eq("user_id", user_id).maybe_single(),
        "exp_bot_state.select"
    )
    return data or None

def clear_state(user_id: int):
    supa_exec(sb.table("exp_bot_state").delete().eq("user_id", user_id), "exp_bot_state.delete")

def parse_amount(text: str):
    try:
        n = float(text.replace(",", "."))
        return round(n, 2) if n > 0 else None
    except:
        return None

@bot.message_handler(commands=['start','help'])
def cmd_start(message):
    tg_user_id = message.from_user.id
    if not is_allowed(tg_user_id):
        bot.reply_to(message, "This bot is private. Access denied.")
        return
    uid = ensure_user(tg_user_id, message.chat.id)
    if not uid:
        bot.reply_to(message, "Access error (not whitelisted in DB).")
        return
    bot.send_message(message.chat.id, "Welcome! Use the buttons below 👇", reply_markup=kb)

@bot.message_handler(func=lambda m: True, content_types=['text'])
def handle_text(message):
    tg_user_id = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()

    if not is_allowed(tg_user_id):
        bot.reply_to(message, "This bot is private. Access denied.")
        return

    user_id = ensure_user(tg_user_id, chat_id)
    if not user_id:
        bot.reply_to(message, "Access error (not whitelisted in DB).")
        return

    # Buttons / commands
    if text == "Add expense":
        set_state(user_id, "await_amount")
        bot.send_message(chat_id, "Send the amount (e.g., 23.50)")
        return

    if text == "See expenses":
        now = datetime.now(timezone.utc)
        first_iso = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()

        # Get last 10 items to display
        items, _ = supa_exec(
            sb.table("exp_expenses")
              .select("amount,title,created_at")
              .eq("user_id", user_id)
              .gte("created_at", first_iso)
              .order("created_at", desc=True)
              .limit(10),
            "exp_expenses.list_this_month"
        )

        # Get all amounts for this month and sum in Python (more reliable than SQL aggregate here)
        all_rows, _ = supa_exec(
            sb.table("exp_expenses")
              .select("amount")
              .eq("user_id", user_id)
              .gte("created_at", first_iso),
            "exp_expenses.amounts_this_month"
        )
        total = sum(float(r["amount"]) for r in (all_rows or []))

        lines = [f"This month total: {total:.2f}"]
        if items:
            for r in items:
                d = r["created_at"][5:10]  # MM-DD
                lines.append(f"• {float(r['amount']):.2f} — {r['title']} ({d})")
        else:
            lines.append("(no items yet)")
        bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)
        return

    if text == "See last month":
        now = datetime.now(timezone.utc)
        m = now.month - 1 or 12
        y = now.year if now.month != 1 else now.year - 1
        last_start = datetime(y, m, 1, tzinfo=timezone.utc).date().isoformat()

        mt, _ = supa_exec(
            sb.table("exp_monthly_totals")
              .select("total")
              .eq("user_id", user_id)
              .eq("month_start", last_start)
              .maybe_single(),
            "exp_monthly_totals.get_last"
        )
        total = float(mt["total"]) if mt and mt.get("total") is not None else 0.0
        bot.send_message(chat_id, f"Your expenses for {last_start[:7]}: {total:.2f}", reply_markup=kb)
        return

    # State machine (amount -> title)
    state = get_state(user_id)
    if state and state.get("pending_action") == "await_amount":
        amt = parse_amount(text)
        if not amt:
            bot.send_message(chat_id, "Invalid amount. Try again, e.g. 12.30")
            return
        set_state(user_id, "await_title", amt)
        bot.send_message(chat_id, f"Amount: {amt:.2f} ✅\nNow send the title (e.g., Groceries)")
        return

    if state and state.get("pending_action") == "await_title":
        title = " ".join(text.split()).strip()
        if not title:
            bot.send_message(chat_id, "Title can't be empty. Send a short title.")
            return
        _, err = supa_exec(
            sb.table("exp_expenses").insert({
                "user_id": user_id,
                "amount": state["temp_amount"],
                "title": title,
                "created_at": datetime.now(timezone.utc).isoformat()
            }),
            "exp_expenses.insert"
        )
        if err:
            bot.send_message(chat_id, "Save failed. Check logs.")
            return
        clear_state(user_id)
        bot.send_message(chat_id, f'Saved ✅ {state["temp_amount"]:.2f} — "{title}"', reply_markup=kb)
        return

    # Fallback
    bot.send_message(chat_id, "Use the buttons below to add or view expenses.", reply_markup=kb)

if __name__ == "__main__":
    print("Bot is running... Press Ctrl+C to stop.")
    # If polling doesn't receive messages, ensure the webhook is deleted:
    #   https://api.telegram.org/bot<TOKEN>/deleteWebhook?drop_pending_updates=true
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
