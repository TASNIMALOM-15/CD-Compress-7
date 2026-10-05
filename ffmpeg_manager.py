import asyncio
import json
import os

import config


class FFmpegError(Exception):
    def __init__(self, reason, killed=False):
        super().__init__(reason)
        self.reason = reason
        self.killed = killed


def _fps(s):
    try:
        a, _, b = (s or "0/1").partition("/")
        b = float(b or 1)
        return float(a) / b if b else 0.0
    except Exception:
        return 0.0


async def probe(path):
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await p.communicate()
    if p.returncode != 0:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info = {"has_video": v is not None, "has_audio": a is not None, "duration": 0.0}
    try:
        info["duration"] = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    except Exception:
        pass
    if v:
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        rot = 0
        try:
            rot = int(float((v.get("tags") or {}).get("rotate", 0)))
        except Exception:
            pass
        for sd in v.get("side_data_list") or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        if abs(rot) % 360 in (90, 270):
            w, h = h, w
        vb = int(v.get("bit_rate") or 0)
        if not vb:
            try:
                vb = max(0, int(fmt.get("bit_rate") or 0) - int((a or {}).get("bit_rate") or 0))
            except Exception:
                vb = 0
        info.update(width=w, height=h, fps=_fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate")),
                    vcodec=v.get("codec_name"), vbitrate=vb)
    if a:
        info.update(acodec=a.get("codec_name"), channels=int(a.get("channels") or 2))
    return info


def build_plan(info, res, fps, target_mb, audio_kbps, inp, out):
    dur = info["duration"]
    ak = audio_kbps if info["has_audio"] else 0
    total_bps = target_mb * 1024 * 1024 * 8 * 0.96 / dur  # 4% reserved for container overhead
    v_bps = total_bps - ak * 1000
    notes = []
    src_v = info.get("vbitrate") or 0
    if src_v and v_bps > src_v * 0.9:
        v_bps = src_v * 0.9
        notes.append("Source is already smaller than the target, so the result will be smaller than the target.")
    if v_bps < 80_000:
        v_bps = 80_000
        notes.append("Video is very long for this target; the result will be larger than the target.")
    v_bps = int(v_bps)

    h = res
    vf = [f"scale='if(gt(iw,ih),-2,min(iw,{h}))':'if(gt(iw,ih),min(ih,{h}),-2)'"]
    if info.get("fps", 0) > fps + 0.5:
        vf.append(f"fps={fps}")
    vf.append("format=yuv420p")

    cmd = [
        "nice", "-n", "10",
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", inp,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-map_chapters", "-1",
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-preset", config.X264_PRESET,
        "-b:v", str(v_bps), "-maxrate", str(int(v_bps * 1.5)), "-bufsize", str(int(v_bps * 3)),
        "-threads", str(config.FFMPEG_THREADS), "-filter_threads", "1",
    ]
    if info["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", f"{ak}k", "-ac", str(min(2, info.get("channels", 2)))]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]
    return {"cmd": cmd, "notes": notes, "v_bps": v_bps}


def _parse(d):
    def f(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    us = d.get("out_time_us") or d.get("out_time_ms") or "0"
    return {
        "out_sec": max(0.0, f(us) / 1e6),
        "speed": f((d.get("speed") or "").replace("x", "").strip()),
        "fps": f(d.get("fps")),
        "size": int(f(d.get("total_size"))),
    }


def _tail(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 2000))
            lines = [l.strip() for l in fh.read().decode("utf-8", "ignore").splitlines() if l.strip()]
        return " | ".join(lines[-2:])[:300] or "unknown error"
    except Exception:
        return "unknown error"


async def _terminate(proc):
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def run_ffmpeg(cmd, on_progress, stderr_path):
    with open(stderr_path, "wb") as ef:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=ef)
    data = {}
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            k, _, v = line.decode("utf-8", "ignore").strip().partition("=")
            data[k] = v
            if k == "progress":
                on_progress(_parse(data))
                data = {}
        rc = await proc.wait()
    except BaseException:
        await _terminate(proc)
        raise
    if rc != 0:
        raise FFmpegError(_tail(stderr_path), killed=rc < 0)
