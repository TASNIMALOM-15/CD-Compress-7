import asyncio
import logging
import os
import re
import shutil
import time

from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as KB

import config
import database as db
import ffmpeg_manager as fm
from ui import bar, fmt_long, fmt_size, fmt_time, queue_lines, target_label

log = logging.getLogger("worker")
MiB = 1024 * 1024


class PermanentError(Exception):
    """Errors where retrying cannot help (bad file, no disk space...)."""


def short(msg, n=300):
    return (" ".join(str(msg).split()))[:n] or "Unknown error"


def out_name(job):
    stem = os.path.splitext(job["original_filename"] or "video")[0]
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(" .") or "video"
    return f"{stem[:80]}_{job['target_resolution']}p_{job['target_fps']}fps.mp4"


class Worker:
    def __init__(self, app):
        self.app = app
        self.current = None
        self.task = None
        self.state = {}
        self.cancel_req = set()
        self.lock = asyncio.Lock()

    # ---------- main loop: strictly one job at a time ----------
    async def run(self):
        os.makedirs(config.JOBS_DIR, exist_ok=True)
        while True:
            try:
                job = await db.acall(db.next_queued)
                if job and await db.acall(db.claim, job["id"]):
                    job = await db.acall(db.get_job, job["id"])
                    self.current = job["id"]
                    self.task = asyncio.create_task(self.process(job))
                    await asyncio.wait({self.task})
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker loop error")
                await asyncio.sleep(10)

    def cancel_running(self, jid):
        if self.current == jid and self.task and not self.task.done():
            self.cancel_req.add(jid)
            self.task.cancel()
            return True
        return False

    # ---------- one job ----------
    async def process(self, job):
        jid = job["id"]
        d = os.path.join(config.JOBS_DIR, str(jid))
        self.state = {"jid": jid, "stage": "downloading", "t0": time.time(), "done": 0,
                      "total": job["input_size"] or 0, "finished": False}
        ticker = asyncio.create_task(self.ticker(job))
        try:
            await self._run_job(job, d)
        except asyncio.CancelledError:
            if jid in self.cancel_req:
                await db.acall(db.update_job, jid, status="cancelled", completed_at=time.time())
                await self.finish(job, f"🚫 Job #{jid} cancelled.\nTemporary files were deleted.")
            else:
                raise  # server shutting down; recovery will re-queue the job
        except PermanentError as e:
            await self.fail(job, str(e))
        except Exception as e:
            log.exception("job %s failed", jid)
            await self.transient(job, e)
        finally:
            ticker.cancel()
            shutil.rmtree(d, ignore_errors=True)
            self.cancel_req.discard(jid)
            self.current = None

    async def _run_job(self, job, d):
        jid, res, fps = job["id"], job["target_resolution"], job["target_fps"]
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)

        need = (job["input_size"] or 0) + job["target_size"] * MiB + config.DISK_MARGIN_MB * MiB
        free = shutil.disk_usage(d).free
        if free < need:
            raise PermanentError(
                f"Not enough server storage available (needs ~{fmt_size(need)}, free {fmt_size(free)})."
            )

        ext = os.path.splitext(job["original_filename"] or "")[1].lower()
        if not ext or len(ext) > 6:
            ext = ".mp4"
        inp, out, errlog = os.path.join(d, "input" + ext), os.path.join(d, "output.mp4"), os.path.join(d, "ffmpeg.log")

        # ---- download ----
        await self.refresh(job)

        def dl_progress(cur, total):
            self.state["done"] = cur
            if total:
                self.state["total"] = total

        try:
            path = await self.app.download_media(job["telegram_file_id"], file_name=inp, progress=dl_progress)
        except OSError as e:
            if e.errno == 28:
                raise PermanentError("Not enough server storage available.")
            raise
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError("Download from Telegram returned no data.")

        # ---- validate + plan ----
        info = await fm.probe(path)
        if not info or not info["has_video"] or info["duration"] <= 0:
            raise PermanentError("This file is not a valid video (no readable video stream).")
        plan = fm.build_plan(info, res, fps, job["target_size"], config.QUALITIES[res]["abr"], path, out)
        await db.acall(db.update_job, jid, status="processing", progress=0, duration=info["duration"])
        self.state.update(stage="processing", t0=time.time(), duration=info["duration"],
                          out_sec=0, speed=0, fps=0, size=0)
        await self.refresh(job)

        # ---- encode ----
        try:
            await fm.run_ffmpeg(plan["cmd"], lambda p: self.state.update(p), errlog)
        except fm.FFmpegError as e:
            if e.killed:
                raise RuntimeError("FFmpeg was killed by the system (probably out of memory).")
            raise PermanentError("FFmpeg error: " + short(e.reason, 200))
        os.remove(path)  # input no longer needed
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError("FFmpeg produced no output.")
        osize = os.path.getsize(out)
        if osize > config.MAX_OUTPUT_MB * MiB:
            raise PermanentError(f"Result ({fmt_size(osize)}) is larger than Telegram's 2 GB upload limit. Choose a lower quality.")
        oinfo = await fm.probe(out) or {}

        # ---- upload ----
        elapsed = time.time() - (job["started_at"] or time.time())
        name = out_name(job)
        caption = (
            "✅ Compression completed!\n\n"
            f"📁 {name}\n\n"
            f"Original: {fmt_size(job['input_size'])}\n"
            f"Compressed: {fmt_size(osize)}\n"
            f"Resolution: {oinfo.get('width', '?')}×{oinfo.get('height', '?')}\n"
            f"FPS: {oinfo.get('fps', fps):.0f}\n"
            f"Time: {fmt_long(elapsed)}"
        )
        if plan["notes"]:
            caption += "\n\nℹ️ " + " ".join(plan["notes"])
        await db.acall(db.update_job, jid, status="uploading", progress=0, output_size=osize)
        self.state.update(stage="uploading", t0=time.time(), done=0, total=osize)
        await self.refresh(job)

        def ul_progress(cur, total):
            self.state["done"] = cur

        last = None
        for attempt in range(3):
            try:
                if attempt < 2:
                    await self.app.send_video(
                        job["chat_id"], out, caption=caption, supports_streaming=True,
                        duration=int(oinfo.get("duration") or 0), width=oinfo.get("width") or 0,
                        height=oinfo.get("height") or 0, file_name=name, progress=ul_progress)
                else:  # last resort: send as a plain file
                    await self.app.send_document(job["chat_id"], out, caption=caption,
                                                 file_name=name, progress=ul_progress)
                last = None
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last = e
                log.warning("upload attempt %s failed: %s", attempt + 1, e)
                self.state["done"] = 0
                await asyncio.sleep(10)
        if last:
            raise RuntimeError("Upload to Telegram failed: " + short(last, 150))

        await db.acall(db.update_job, jid, status="completed", progress=100, completed_at=time.time())
        jobs = await db.acall(db.user_jobs, job["user_id"])
        await self.finish(job, f"✅ Job #{jid} completed.\n\n📋 Your Queue\n\n" + queue_lines(jobs))

    # ---------- failures ----------
    async def transient(self, job, exc):
        rc = (job["retry_count"] or 0) + 1
        reason = short(exc, 200)
        if rc <= config.MAX_RETRIES:
            await db.acall(db.update_job, job["id"], status="queued", retry_count=rc,
                           progress=0, error_message=reason)
            await self.finish(
                job,
                f"⚠️ Job #{job['id']} attempt {rc} failed ❌\nReason: {reason}\n"
                f"🔄 Retrying (attempt {rc + 1} of {config.MAX_RETRIES + 1}). It is back in the queue.",
                final=False,
            )
            await asyncio.sleep(5)
        else:
            await self.fail(job, reason)

    async def fail(self, job, reason):
        reason = short(reason, 250)
        await db.acall(db.update_job, job["id"], status="failed", error_message=reason,
                       completed_at=time.time())
        await self.finish(job, f"❌ Job #{job['id']} failed.")
        try:
            await self.app.send_message(
                job["chat_id"],
                f"❌ Compression failed\n\nJob: #{job['id']}\nReason: {reason}\n\nThe job can be retried.",
                reply_markup=KB([[Btn(f"🔄 Retry #{job['id']}", callback_data=f"retry:{job['id']}")]]),
            )
        except Exception:
            log.exception("could not send failure message")

    async def finish(self, job, text, final=True):
        async with self.lock:
            if final:
                self.state["finished"] = True
            await self.edit(job, text)

    # ---------- progress messages ----------
    async def edit(self, job, text, markup=None):
        try:
            await self.app.edit_message_text(job["chat_id"], job["status_msg_id"], text, reply_markup=markup)
        except Exception as e:
            log.debug("edit skipped: %s", e)

    def pct(self):
        st = self.state
        if st.get("stage") == "processing":
            d = st.get("duration") or 0
            return min(99, int(st.get("out_sec", 0) / d * 100)) if d else 0
        t = st.get("total") or 0
        return min(100, int(st.get("done", 0) / t * 100)) if t else 0

    async def render(self, job):
        st, stage = self.state, self.state.get("stage", "downloading")
        p = self.pct()
        jobs = await db.acall(db.user_jobs, job["user_id"])
        head = {"downloading": "⬇️ Downloading video...", "processing": "🎬 Compressing Video...",
                "uploading": "⬆️ Uploading result..."}.get(stage, "⏳ Starting...")
        L = [head, "", f"📁 {job['original_filename']}", f"Quality: {job['target_resolution']}p",
             f"FPS: {job['target_fps']}", f"Target: {target_label(job['target_size'])}"]
        if job.get("retry_count"):
            L.append(f"🔁 Attempt {job['retry_count'] + 1} of {config.MAX_RETRIES + 1}")
        elapsed = time.time() - st.get("t0", time.time())
        L += ["", "Progress:", f"{bar(p)} {p}%", "", f"⏱ Elapsed: {fmt_time(elapsed)}"]
        if stage == "processing":
            sp, dur = st.get("speed", 0), st.get("duration", 0)
            L.append(f"⚡ Speed: {sp:.2f}x" if sp else "⚡ Speed: measuring...")
            if sp > 0 and dur:
                L.append(f"⏳ ETA: {fmt_time((dur - st.get('out_sec', 0)) / sp)}")
            L.append(f"🎞 Encoding FPS: {st.get('fps', 0):.1f}")
            L.append(f"📦 Output so far: {fmt_size(st.get('size', 0))}")
        else:
            done, total = st.get("done", 0), st.get("total", 0)
            L.append(f"📊 {fmt_size(done)} / {fmt_size(total)}")
            if elapsed > 3 and done:
                rate = done / elapsed
                L.append(f"⚡ Speed: {fmt_size(rate)}/s")
                if total > done:
                    L.append(f"⏳ ETA: {fmt_time((total - done) / rate)}")
        L += ["", "Queue:", queue_lines(jobs)]
        return "\n".join(L)

    async def refresh(self, job):
        async with self.lock:
            if self.state.get("finished"):
                return
            await db.acall(db.update_job, job["id"], progress=self.pct())
            text = await self.render(job)
            kb = KB([[Btn("❌ Cancel Current Job", callback_data=f"cancel:{job['id']}")]])
            await self.edit(job, text, kb)

    async def ticker(self, job):
        while True:
            await asyncio.sleep(config.PROGRESS_INTERVAL)
            try:
                await self.refresh(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ticker error")
