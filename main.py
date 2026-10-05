import asyncio
import logging
import sys

from aiohttp import web
from pyrogram import Client
from pyrogram.enums import ParseMode

import config
import database as db
import handlers
from worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def start_web():
    async def health(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("health server on port %s", config.PORT)


async def main():
    missing = [n for n, v in (("BOT_TOKEN", config.BOT_TOKEN), ("API_ID", config.API_ID),
                              ("API_HASH", config.API_HASH)) if not v]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)
    if not config.ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS is empty: nobody can use the bot yet (the bot will show each user's ID).")
    if not config.DATABASE_URL:
        log.warning("DATABASE_URL not set: using SQLite in /tmp. The queue is LOST when the server restarts.")

    await start_web()
    await db.acall(db.init)
    recovered = await db.acall(db.recover, config.MAX_RETRIES)

    client = Client("compressor", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN,
                    in_memory=True, parse_mode=ParseMode.DISABLED, workers=4)
    worker = Worker(client)
    handlers.register(client, worker)
    await client.start()
    log.info("bot started")

    for j in recovered:
        try:
            if j["status"] == "queued":
                text = f"♻️ The server restarted. Job #{j['id']} was put back in the queue (attempt {j['retry_count'] + 1})."
            else:
                text = f"❌ Job #{j['id']} failed: interrupted by server restarts too many times."
            await client.send_message(j["chat_id"], text)
        except Exception:
            pass

    asyncio.create_task(worker.run())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
