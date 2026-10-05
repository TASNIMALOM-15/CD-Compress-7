import asyncio
import sqlite3
import threading
import time

import config

IS_PG = config.DATABASE_URL.startswith(("postgres://", "postgresql://"))
if IS_PG:
    import psycopg2

_lock = threading.Lock()
ACTIVE = ("queued", "downloading", "processing", "uploading")
RUNNING = ("downloading", "processing", "uploading")
FINISHED = ("completed", "failed", "cancelled")


def _in(vals):
    return "(" + ",".join("'%s'" % v for v in vals) + ")"


def _connect():
    if IS_PG:
        return psycopg2.connect(config.DATABASE_URL, connect_timeout=15)
    return sqlite3.connect(config.SQLITE_PATH, timeout=30)


def _exec(sql, params=(), fetch=None):
    if IS_PG:
        sql = sql.replace("?", "%s")
    with _lock:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            out = None
            if fetch:
                names = [d[0] for d in cur.description]
                if fetch == "all":
                    out = [dict(zip(names, r)) for r in cur.fetchall()]
                else:
                    r = cur.fetchone()
                    out = dict(zip(names, r)) if r else None
            conn.commit()
            return out
        finally:
            conn.close()


async def acall(fn, *a, **k):
    return await asyncio.to_thread(fn, *a, **k)


def init():
    pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    _exec(
        f"""CREATE TABLE IF NOT EXISTS jobs (
        id {pk},
        user_id BIGINT NOT NULL,
        chat_id BIGINT NOT NULL,
        src_msg_id BIGINT,
        status_msg_id BIGINT,
        telegram_file_id TEXT NOT NULL,
        file_unique_id TEXT,
        original_filename TEXT,
        input_size BIGINT,
        duration DOUBLE PRECISION,
        target_resolution INTEGER NOT NULL,
        target_fps INTEGER NOT NULL,
        target_size BIGINT NOT NULL,
        output_size BIGINT,
        status TEXT NOT NULL,
        progress DOUBLE PRECISION DEFAULT 0,
        created_at DOUBLE PRECISION,
        started_at DOUBLE PRECISION,
        completed_at DOUBLE PRECISION,
        error_message TEXT,
        retry_count INTEGER DEFAULT 0
    )"""
    )
    _exec("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")


def add_job(**f):
    f.setdefault("created_at", time.time())
    f.setdefault("status", "queued")
    f.setdefault("progress", 0.0)
    f.setdefault("retry_count", 0)
    keys = list(f)
    sql = "INSERT INTO jobs (%s) VALUES (%s) RETURNING id" % (
        ",".join(keys),
        ",".join("?" * len(keys)),
    )
    return _exec(sql, [f[k] for k in keys], "one")["id"]


def update_job(job_id, **f):
    if not f:
        return
    sets = ",".join("%s=?" % k for k in f)
    _exec("UPDATE jobs SET %s WHERE id=?" % sets, list(f.values()) + [job_id])


def get_job(job_id):
    return _exec("SELECT * FROM jobs WHERE id=?", (job_id,), "one")


def next_queued():
    return _exec("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1", (), "one")


def claim(job_id):
    r = _exec(
        "UPDATE jobs SET status='downloading', started_at=?, progress=0 "
        "WHERE id=? AND status='queued' RETURNING id",
        (time.time(), job_id),
        "one",
    )
    return r is not None


def cancel_if_queued(job_id):
    r = _exec(
        "UPDATE jobs SET status='cancelled', completed_at=? WHERE id=? AND status='queued' RETURNING id",
        (time.time(), job_id),
        "one",
    )
    return r is not None


def requeue(job_id, user_id):
    r = _exec(
        "UPDATE jobs SET status='queued', retry_count=0, progress=0, error_message=NULL, completed_at=NULL "
        "WHERE id=? AND user_id=? AND status IN ('failed','cancelled') RETURNING id",
        (job_id, user_id),
        "one",
    )
    return r is not None


def user_jobs(user_id):
    active = _exec(
        f"SELECT * FROM jobs WHERE user_id=? AND status IN {_in(ACTIVE)} ORDER BY id",
        (user_id,),
        "all",
    )
    recent = _exec(
        f"SELECT * FROM jobs WHERE user_id=? AND status IN {_in(FINISHED)} ORDER BY id DESC LIMIT 3",
        (user_id,),
        "all",
    )
    return list(reversed(recent)) + active


def jobs_ahead(job_id):
    r = _exec(
        f"SELECT COUNT(*) AS n FROM jobs WHERE id<? AND status IN {_in(ACTIVE)}",
        (job_id,),
        "one",
    )
    return int(r["n"]) if r else 0


def find_duplicate(user_id, unique_id, res, fps):
    return _exec(
        f"SELECT * FROM jobs WHERE user_id=? AND file_unique_id=? AND target_resolution=? "
        f"AND target_fps=? AND status IN {_in(ACTIVE)} LIMIT 1",
        (user_id, unique_id, res, fps),
        "one",
    )


def recover(max_retries):
    """Jobs that were mid-flight when the server died go back to the queue."""
    rows = _exec(f"SELECT * FROM jobs WHERE status IN {_in(RUNNING)} ORDER BY id", (), "all")
    for r in rows:
        rc = (r["retry_count"] or 0) + 1
        if rc > max_retries:
            update_job(
                r["id"],
                status="failed",
                retry_count=rc,
                completed_at=time.time(),
                error_message="Interrupted by server restarts too many times (possibly out of memory).",
            )
            r["status"] = "failed"
        else:
            update_job(r["id"], status="queued", retry_count=rc, progress=0)
            r["status"] = "queued"
        r["retry_count"] = rc
    return rows
