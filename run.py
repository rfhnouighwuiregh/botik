import asyncio
import os
import sys

from aiogram import Bot
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в Environment Variables Render")


async def clear_webhook():
    bot = Bot(TOKEN)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        info = await bot.get_webhook_info()
        print(f"Webhook очищен. URL: {info.url!r}", flush=True)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(clear_webhook())
    os.execv(sys.executable, [sys.executable, "bot.py"])
