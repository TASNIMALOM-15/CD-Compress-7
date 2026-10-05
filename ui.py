def bar(p, n=10):
    p = max(0, min(100, int(p or 0)))
    f = int(p / 100 * n)
    return "█" * f + "░" * (n - f)


def fmt_size(b):
    b = float(b or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024


def fmt_time(s):
    s = int(max(0, s or 0))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def fmt_long(s):
    s = int(max(0, s or 0))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def target_label(mb):
    return f"~{mb / 1000:g} GB" if mb >= 1000 else f"~{mb} MB"


STAGES = {"downloading": "Downloading", "processing": "Processing", "uploading": "Uploading"}


def queue_lines(jobs):
    out = []
    for j in jobs:
        s, i = j["status"], f"#{j['id']}"
        if s == "completed":
            out.append(f"{i} {bar(100)} 100% ✅ Completed")
        elif s in STAGES:
            p = int(j.get("progress") or 0)
            out.append(f"{i} {bar(p)} {p}% 🔄 {STAGES[s]}")
        elif s == "queued":
            out.append(f"{i} ⏳ Waiting")
        elif s == "failed":
            out.append(f"{i} ❌ Failed")
        else:
            out.append(f"{i} 🚫 Cancelled")
    return "\n".join(out)
