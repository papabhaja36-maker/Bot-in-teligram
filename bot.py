import os
import logging
import asyncio
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiohttp import web
from supabase import create_client, Client

# ================== CONFIG (Environment Variables se aayega) ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))          # Aapka Telegram user ID (number)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")            # service_role key use karein
PORT = int(os.getenv("PORT", "8080"))               # Render ke liye web server port

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== INIT ==================
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Reply-mapping: jab admin kisi forwarded message ko reply kare,
# to hume pata hona chahiye ki woh kis user ID ko ja raha hai.
# Hum ye mapping Supabase table 'admin_forward_map' me store karenge
# (forwarded_message_id -> original_user_id), taake restart ke baad bhi kaam kare.


# ================== DATABASE HELPERS ==================

def db_get_user(user_id: int):
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None


def db_save_user(user_id: int, username: str, full_name: str, ref_id: int = None):
    existing = db_get_user(user_id)
    if existing:
        return existing  # already saved, don't overwrite ref_id

    data = {
        "user_id": user_id,
        "username": username,
        "full_name": full_name,
        "referred_by": ref_id,          # None agar bina referral link ke aaya
        "joined_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("users").insert(data).execute()

    # Agar valid referral id hai to referrer ka count +1 karo
    if ref_id:
        try:
            ref_user = db_get_user(ref_id)
            if ref_user:
                new_count = (ref_user.get("referral_count") or 0) + 1
                supabase.table("users").update(
                    {"referral_count": new_count}
                ).eq("user_id", ref_id).execute()
        except Exception as e:
            logger.error(f"Referral count update failed: {e}")

    return data


def db_save_forward_map(admin_msg_id: int, user_id: int):
    supabase.table("admin_forward_map").insert(
        {"admin_message_id": admin_msg_id, "user_id": user_id}
    ).execute()


def db_get_forward_map(admin_msg_id: int):
    res = (
        supabase.table("admin_forward_map")
        .select("*")
        .eq("admin_message_id", admin_msg_id)
        .execute()
    )
    return res.data[0]["user_id"] if res.data else None


def db_all_user_ids():
    res = supabase.table("users").select("user_id").execute()
    return [row["user_id"] for row in res.data]


# ================== USER HANDLERS ==================

@dp.message(CommandStart())
async def start_handler(message: Message):
    args = message.text.split(maxsplit=1)
    ref_id = None
    if len(args) > 1 and args[1].isdigit():
        parsed = int(args[1])
        if parsed != message.from_user.id:   # khud ko refer na kar sake
            ref_id = parsed

    user = db_get_user(message.from_user.id)
    is_new = user is None

    db_save_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or "",
        ref_id=ref_id,
    )

    if is_new:
        if ref_id:
            logger.info(f"New user {message.from_user.id} joined via referral {ref_id}")
        else:
            logger.info(f"New user {message.from_user.id} came WITHOUT referral id")

    bot_username = (await bot.get_me()).username
    my_link = f"https://t.me/{bot_username}?start={message.from_user.id}"

    await message.answer(
        f"👋 Welcome, {message.from_user.full_name}!\n\n"
        f"Apna referral link share karein:\n<code>{my_link}</code>\n\n"
        f"Kuch bhi message likhein, hum admin tak pahunchayenge."
    )


@dp.message(Command("myrefs"))
async def my_refs_handler(message: Message):
    user = db_get_user(message.from_user.id)
    count = (user.get("referral_count") if user else 0) or 0
    await message.answer(f"📊 Aapke total referrals: <b>{count}</b>")


# Normal user -> bot ko message bheje -> Admin ko forward ho
@dp.message(F.chat.type == "private")
async def user_message_handler(message: Message):
    # Agar admin khud type kar raha hai, ye alag handler me handle hoga (neeche)
    if message.from_user.id == ADMIN_ID:
        return await admin_panel_handler(message)

    # Ensure user db me hai (safety, agar /start skip hua ho)
    db_save_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or "",
    )

    caption = (
        f"📩 <b>New message</b>\n"
        f"From: {message.from_user.full_name} (@{message.from_user.username or 'no_username'})\n"
        f"ID: <code>{message.from_user.id}</code>\n"
        f"────────────────"
    )
    await bot.send_message(ADMIN_ID, caption)
    forwarded = await bot.forward_message(
        chat_id=ADMIN_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )

    # Map save karo taaki admin isi message ko reply kare to hume pata chale kise bhejna hai
    db_save_forward_map(forwarded.message_id, message.from_user.id)

    await message.answer("✅ Aapka message admin ko bhej diya gaya hai.")


# ================== ADMIN HANDLERS ==================
# Commands (sirf admin ke liye):
#   /send <user_id> <message>   -> kisi specific ID par direct message
#   /broadcast <message>        -> sabhi users ko ek sath message
#   Ya seedha forwarded message ko reply karke bhi user ko reply ja sakta hai


@dp.message(Command("send"))
async def admin_send_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return await message.answer("Usage: /send <user_id> <message>")

    target_id, text = parts[1], parts[2]
    if not target_id.isdigit():
        return await message.answer("❌ User ID number hona chahiye.")

    try:
        await bot.send_message(int(target_id), text)
        await message.answer(f"✅ Message bhej diya gaya ID {target_id} ko.")
    except Exception as e:
        await message.answer(f"❌ Fail: {e}")


@dp.message(Command("broadcast"))
async def admin_broadcast_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("Usage: /broadcast <message>")

    text = parts[1]
    user_ids = db_all_user_ids()
    sent, failed = 0, 0
    status_msg = await message.answer(f"📤 Broadcasting to {len(user_ids)} users...")

    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # rate limit se bachne ke liye

    await status_msg.edit_text(f"✅ Broadcast complete.\nSent: {sent}\nFailed: {failed}")


async def admin_panel_handler(message: Message):
    """Admin agar forwarded message ko REPLY karta hai -> woh reply original user ko chala jaye."""
    if message.reply_to_message:
        target_user_id = db_get_forward_map(message.reply_to_message.message_id)
        if target_user_id:
            try:
                await bot.copy_message(
                    chat_id=target_user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                await message.answer(f"✅ Reply bhej diya gaya user {target_user_id} ko.")
            except Exception as e:
                await message.answer(f"❌ Fail: {e}")
            return

    # Agar reply nahi hai to sirf normal info dikhado
    await message.answer(
        "ℹ️ Kisi user ko reply karne ke liye uska forwarded message reply karein.\n"
        "Ya /send <user_id> <message> use karein.\n"
        "Sab users ko: /broadcast <message>"
    )


# ================== WEB SERVER (Render ke liye - keeps service alive) ==================

async def health_check(request):
    return web.Response(text="Bot is running ✅")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")


async def main():
    await start_web_server()
    logger.info("Bot polling started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
