import logging
import os
import shutil

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as KB

import config
import database as db
from ui import fmt_size, queue_lines, target_label

log = logging.getLogger("handlers")
MiB = 1024 * 1024
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".3gp", ".mpg", ".mpeg"}

HELP = (
    "🎬 Video Compressor Bot\n\n"
    "1. Send me a video (or forward one from any chat).\n"
    "2. Pick a quality, then an FPS option.\n"
    "3. The job joins the queue and is compressed on the server, one at a time.\n"
    "   You can close Telegram - I will send the result when it is ready.\n\n"
    "Commands:\n"
    "/queue - your jobs\n/status - current job details\n/cancel - cancel a job\n/help - this message\n\n"
    "Note: target sizes are approximate (usually within about ±15%)."
)


def quality_kb(mid):
    rows = [[Btn(f"🎬 {r}p — {q['label']}", callback_data=f"q:{mid}:{r}")] for r, q in config.QUALITIES.items()]
    return KB(rows)


def fps_kb(mid, res):
    rows = [[Btn(f"{fps} FPS {target_label(mb)}", callback_data=f"f:{mid}:{res}:{fps}")]
            for fps, mb in config.QUALITIES[res]["fps"].items()]
    rows.append([Btn("⬅️ Back", callback_data=f"b:{mid}")])
    return KB(rows)


def register(app, worker):
    def ok(uid):
        return uid in config.ALLOWED_USER_IDS

    async def deny(msg):
        await msg.reply_text(
            "🔒 This is a private bot.\n"
            f"Your Telegram user ID: {msg.from_user.id}\n"
            "If you are the owner, add this ID to ALLOWED_USER_IDS on the server."
        )

    def authed(msg):
        return bool(msg.from_user) and ok(msg.from_user.id)

    @app.on_message(filters.private & filters.command(["start", "help"]))
    async def start(client, msg):
        if not authed(msg):
            return await deny(msg)
        await msg.reply_text(HELP)

    @app.on_message(filters.private & filters.command("queue"))
    async def queue(client, msg):
        if not authed(msg):
            return await deny(msg)
        jobs = await db.acall(db.user_jobs, msg.from_user.id)
        if not jobs:
            return await msg.reply_text("📭 Your queue is empty.")
        rows = [[Btn(f"🔄 Retry #{j['id']}", callback_data=f"retry:{j['id']}")] for j in jobs if j["status"] == "failed"]
        await msg.reply_text("📋 Your Queue\n\n" + queue_lines(jobs), reply_markup=KB(rows) if rows else None)

    @app.on_message(filters.private & filters.command("status"))
    async def status(client, msg):
        if not authed(msg):
            return await deny(msg)
        jobs = await db.acall(db.user_jobs, msg.from_user.id)
        active = [j for j in jobs if j["status"] in ("queued", "downloading", "processing", "uploading")]
        if not active:
            return await msg.reply_text("✅ You have no active jobs.")
        for j in active:
            if worker.current == j["id"] and worker.state.get("jid") == j["id"]:
                await msg.reply_text(await worker.render(j))
            else:
                ahead = await db.acall(db.jobs_ahead, j["id"])
                await msg.reply_text(f"⏳ Job #{j['id']} ({j['target_resolution']}p {j['target_fps']}fps) is waiting. Jobs ahead: {ahead}")

    @app.on_message(filters.private & filters.command("cancel"))
    async def cancel_cmd(client, msg):
        if not authed(msg):
            return await deny(msg)
        jobs = await db.acall(db.user_jobs, msg.from_user.id)
        active = [j for j in jobs if j["status"] in ("queued", "downloading", "processing", "uploading")]
        if not active:
            return await msg.reply_text("Nothing to cancel.")
        rows = [[Btn(f"❌ Cancel #{j['id']} ({j['status']})", callback_data=f"cancel:{j['id']}")] for j in active]
        await msg.reply_text("Which job do you want to cancel?", reply_markup=KB(rows))

    @app.on_message(filters.private & (filters.video | filters.document))
    async def on_video(client, msg):
        if not authed(msg):
            return await deny(msg)
        media = msg.video or msg.document
        if msg.document:
            ext = os.path.splitext(media.file_name or "")[1].lower()
            if not ((media.mime_type or "").startswith("video/") or ext in VIDEO_EXT):
                return await msg.reply_text("❌ This does not look like a video file. Please send a video (mp4, mkv, mov...).")
        size = media.file_size or 0
        if size > config.MAX_INPUT_MB * MiB:
            return await msg.reply_text(
                f"❌ This file is {fmt_size(size)}. Telegram bots can only download files up to about "
                f"{config.MAX_INPUT_MB} MB, so I cannot process it.")
        name = media.file_name or f"video_{media.file_unique_id}.mp4"
        lines = ["📥 Video received", f"📁 {name}", f"📦 Size: {fmt_size(size)}"]
        if getattr(media, "duration", None):
            m, s = divmod(int(media.duration), 60)
            h, m = divmod(m, 60)
            lines.append(f"⏱ Duration: {h}:{m:02d}:{s:02d}")
        if getattr(media, "width", None):
            lines.append(f"📐 Resolution: {media.width}×{media.height}")
        lines += ["", "Choose Quality:"]
        await msg.reply_text("\n".join(lines), reply_markup=quality_kb(msg.id), quote=True)

    @app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "queue", "status", "cancel"]))
    async def other(client, msg):
        if authed(msg):
            await msg.reply_text("Send me a video to compress. /help for instructions.")
        else:
            await deny(msg)

    @app.on_callback_query()
    async def on_cb(client, cq):
        uid = cq.from_user.id
        if not ok(uid):
            return await cq.answer("Not authorized.", show_alert=True)
        p = (cq.data or "").split(":")
        act = p[0]
        try:
            if act == "q":
                mid, res = int(p[1]), int(p[2])
                await cq.message.edit_text(f"🎬 {res}p — Select FPS:", reply_markup=fps_kb(mid, res))
                return await cq.answer()
            if act == "b":
                await cq.message.edit_text("Choose Quality:", reply_markup=quality_kb(int(p[1])))
                return await cq.answer()
            if act == "f":
                return await create_job(client, cq, int(p[1]), int(p[2]), int(p[3]))
            if act == "cancel":
                return await do_cancel(cq, int(p[1]))
            if act == "retry":
                return await do_retry(cq, int(p[1]))
        except Exception:
            log.exception("callback error")
            await cq.answer("Something went wrong. Please try again.", show_alert=True)

    async def create_job(client, cq, mid, res, fps):
        uid, chat = cq.from_user.id, cq.message.chat.id
        mb = config.QUALITIES.get(res, {}).get("fps", {}).get(fps)
        if mb is None:
            return await cq.answer("Invalid option.", show_alert=True)
        src = None
        try:
            src = await client.get_messages(chat, mid)
        except Exception:
            pass
        if not src or src.empty or not (src.video or src.document):
            src = cq.message.reply_to_message
        media = src and (src.video or src.document)
        if not media:
            return await cq.answer("Original video message not found. Please send it again.", show_alert=True)
        dup = await db.acall(db.find_duplicate, uid, media.file_unique_id, res, fps)
        if dup:
            return await cq.answer(f"This exact job is already in the queue (#{dup['id']}).", show_alert=True)
        size = media.file_size or 0
        os.makedirs(config.JOBS_DIR, exist_ok=True)
        free = shutil.disk_usage(config.JOBS_DIR).free
        need = size + mb * MiB + config.DISK_MARGIN_MB * MiB
        if free < need:
            return await cq.answer(
                "❌ Not enough server storage available. Please wait for current jobs to finish.", show_alert=True)
        name = media.file_name or f"video_{media.file_unique_id}.mp4"
        jid = await db.acall(
            db.add_job, user_id=uid, chat_id=chat, src_msg_id=mid, status_msg_id=cq.message.id,
            telegram_file_id=media.file_id, file_unique_id=media.file_unique_id, original_filename=name,
            input_size=size, duration=getattr(media, "duration", None),
            target_resolution=res, target_fps=fps, target_size=mb)
        ahead = await db.acall(db.jobs_ahead, jid)
        await cq.message.edit_text(
            "🎬 Compression started!\n\n"
            f"📁 File: {name}\n🎯 Quality: {res}p\n🎞 FPS: {fps}\n📦 Target: {target_label(mb)}\n\n"
            f"⏳ Added to queue as #{jid} (jobs ahead: {ahead})...",
            reply_markup=KB([[Btn(f"❌ Cancel #{jid}", callback_data=f"cancel:{jid}")]]))
        await cq.answer("Added to queue ✅")

    async def do_cancel(cq, jid):
        job = await db.acall(db.get_job, jid)
        if not job or job["user_id"] != cq.from_user.id:
            return await cq.answer("Job not found.", show_alert=True)
        if await db.acall(db.cancel_if_queued, jid):
            try:
                await client_edit(job, f"🚫 Job #{jid} cancelled.")
            except Exception:
                pass
            return await cq.answer("Cancelled.")
        if worker.cancel_running(jid):
            return await cq.answer("Cancelling... (temporary files will be deleted)")
        await cq.answer("This job is not active.", show_alert=True)

    async def client_edit(job, text):
        await app.edit_message_text(job["chat_id"], job["status_msg_id"], text)

    async def do_retry(cq, jid):
        if await db.acall(db.requeue, jid, cq.from_user.id):
            await cq.answer("Re-queued ✅")
            await app.send_message(cq.message.chat.id, f"🔄 Job #{jid} was added back to the queue.")
        else:
            await cq.answer("Only failed or cancelled jobs can be retried.", show_alert=True)
