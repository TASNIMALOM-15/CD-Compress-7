import os
import re


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
API_ID = _int("API_ID", 0)
API_HASH = os.environ.get("API_HASH", "").strip()
ALLOWED_USER_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", ""))
    if x.strip().isdigit()
}
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SQLITE_PATH = os.environ.get("SQLITE_PATH", "/tmp/bot.db")

MAX_RETRIES = _int("MAX_RETRIES", 2)
PROGRESS_INTERVAL = max(4, _int("PROGRESS_INTERVAL", 8))
JOBS_DIR = os.environ.get("JOBS_DIR", "/tmp/jobs")
X264_PRESET = os.environ.get("X264_PRESET", "veryfast")
FFMPEG_THREADS = max(1, _int("FFMPEG_THREADS", 1))
MAX_INPUT_MB = _int("MAX_INPUT_MB", 2000)  # Telegram bot limit is ~2000 MB
DISK_MARGIN_MB = _int("DISK_MARGIN_MB", 300)
MAX_OUTPUT_MB = 2000  # Telegram upload limit for bots
PORT = _int("PORT", 10000)

# resolution -> label, audio kbps, {fps: target size in MB}
QUALITIES = {
    360: {"label": "Low Quality", "abr": 64, "fps": {24: 60, 30: 80}},
    480: {"label": "Standard Quality", "abr": 96, "fps": {24: 120, 30: 150}},
    720: {"label": "HD", "abr": 128, "fps": {24: 250, 30: 350, 60: 500}},
    1080: {"label": "Full HD", "abr": 128, "fps": {30: 700, 60: 1500}},
}
