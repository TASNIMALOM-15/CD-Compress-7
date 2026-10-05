# Telegram Video Compressor — সম্পূর্ণ গাইড (বাংলা)

## PART 1 — Feasibility (কোনটা সম্ভব, কোনটা শর্তসাপেক্ষ)

| বিষয় | অবস্থা | ব্যাখ্যা |
|---|---|---|
| সার্ভারে FFmpeg compression | ✅ সম্ভব | ফোন বন্ধ থাকলেও চলবে |
| Queue (একটার পর একটা) | ✅ সম্ভব | একসাথে সর্বোচ্চ ১টি FFmpeg |
| ২ GB পর্যন্ত ভিডিও | ⚠️ শর্তসাপেক্ষ | সাধারণ Bot API-তে ডাউনলোড মাত্র ২০ MB, আপলোড ৫০ MB। তাই **MTProto (Pyrofork)** ব্যবহার করা হয়েছে: বট প্রায় ২০০০ MB পর্যন্ত ডাউনলোড/আপলোড করতে পারে। |
| ২ GB-এর বেশি ভিডিও | ❌ অসম্ভব | টেলিগ্রাম বটকে এর বেশি ফাইল দেয়ই না। |
| Render Free-এ সবসময় চালু থাকা | ⚠️ শর্তসাপেক্ষ | ১৫ মিনিট কেউ HTTP request না করলে সার্ভার ঘুমিয়ে পড়ে → **UptimeRobot** (free) দিয়ে প্রতি ৫ মিনিটে ping করতে হবে। |
| Queue/ডাটা সংরক্ষণ | ⚠️ শর্তসাপেক্ষ | Render Free-এর ডিস্ক **ephemeral** (restart হলে মুছে যায়)। তাই queue রাখা হয় বাইরের free Postgres-এ (**Neon**)। |
| Resume | ❌ true resume নেই | FFmpeg মাঝপথ থেকে resume হয় না। Restart হলে job **শুরু থেকে** আবার চলে (queue হারায় না)। |
| গতি | ⚠️ খুব ধীর | Free instance = 0.1 CPU। একটা ছোট ভিডিও দিয়ে `Speed` দেখুন। মোট সময় ≈ ভিডিওর দৈর্ঘ্য ÷ speed। যেমন speed 0.05x হলে ২ ঘণ্টার মুভি ≈ ৪০ ঘণ্টা। এই সময়ে restart হলে আবার শুরু হবে — বড় ভিডিও কখনো শেষ না-ও হতে পারে। |
| RAM (512 MB) | ⚠️ ঝুঁকি | ভিডিও RAM-এ লোড হয় না (ফাইল-ভিত্তিক), thread=1। তবে 1080p-তে মেমরি বেশি লাগে; OOM হলে job fail/retry হবে। |
| ডিস্ক | ⚠️ অজানা | Free instance-এ কত GB ডিস্ক পাবেন তা নিশ্চিত নই। বট প্রতিবার free space মাপে; ২ GB ইনপুট + আউটপুট ধরে না গেলে বন্ধুত্বপূর্ণ error দেয়। |
| ফাইল সাইজ গ্যারান্টি | ❌ গ্যারান্টি নেই | Single-pass ABR; সাধারণত ±১৫% এর মধ্যে। |

**Codec: H.264 (libx264), preset `veryfast`।** কারণ: সব ডিভাইসে চলে, H.265/AV1 এই CPU-তে অসম্ভব ধীর। CRF নয়, **bitrate**: `(target size × 0.96 × 8 ÷ duration) − audio bitrate`। সোর্স bitrate-এর ৯০%-এর বেশি কখনো নেওয়া হয় না। Upscale হয় না; aspect ratio ও portrait ঠিক থাকে; অডিও না থাকলে অডিও যোগ হয় না। ট্রেড-অফ: `slow` preset-এ ছোট ফাইল/ভালো মান, কিন্তু এখানে কয়েকগুণ ধীর — তাই `veryfast`। চাইলে `X264_PRESET=ultrafast` দিয়ে আরও দ্রুত (সাইজ একটু বড়)।

**UI: দুই-ধাপ** (আগে quality, তারপর FPS)। কারণ: একসাথে ৯টা বাটন মোবাইলে ভিড় হয়, ভুল চাপার সম্ভাবনা বাড়ে; পরে নতুন অপশন যোগ করাও সহজ। Back বাটনও আছে।

## PART 2 — Architecture

```
Telegram (আপনি ভিডিও পাঠান / forward করেন)
   ↓
Bot (Pyrofork/MTProto) → quality + FPS বাটন
   ↓
Queue (Neon Postgres: queued → downloading → processing → uploading → completed)
   ↓
Worker (একসাথে ১টি job)
   ↓ Downloader → /tmp/jobs/<id>/input
   ↓ ffprobe (validation) → bitrate হিসাব
   ↓ FFmpeg (libx264, nice, threads=1)
   ↓ Uploader → Telegram
   ↓
Cleanup (/tmp/jobs/<id> মুছে ফেলা)
UptimeRobot → /health (সার্ভার জাগিয়ে রাখে)
```

## PART 3 — File structure

```
telegram-video-compressor/
├── main.py            # শুরু: web health server + bot + worker
├── config.py          # env variables ও quality table
├── database.py        # job table (Postgres/SQLite), crash recovery
├── worker.py          # queue loop, download/encode/upload, retry, cancel, progress
├── ffmpeg_manager.py  # ffprobe, bitrate হিসাব, FFmpeg চালানো
├── handlers.py        # commands ও বাটন
├── ui.py              # progress bar, formatting
├── requirements.txt
├── Dockerfile
├── render.yaml        # (ঐচ্ছিক)
├── .gitignore
└── README.md
```

## PART 4 — Source code
প্রতিটি ফাইলের সম্পূর্ণ কোড এই ফোল্ডারেই আছে।

## PART 5 — Environment Variables

```
BOT_TOKEN=
API_ID=
API_HASH=
ALLOWED_USER_IDS=
DATABASE_URL=
MAX_RETRIES=2
```

| নাম | বাধ্যতামূলক? | কী দেবেন / কোথায় পাবেন |
|---|---|---|
| BOT_TOKEN | ✅ | টেলিগ্রামে **@BotFather** → /newbot → নাম দিন → token দেবে |
| API_ID | ✅ | https://my.telegram.org → লগইন → API development tools → App তৈরি → **api_id** (সংখ্যা) |
| API_HASH | ✅ | একই পেজে **api_hash** |
| ALLOWED_USER_IDS | ✅ | আপনার Telegram user ID (একাধিক হলে কমা দিয়ে)। ID না জানলে আগে খালি রেখে deploy করুন, বটকে /start দিন — বট আপনার ID দেখিয়ে দেবে; তারপর এখানে বসিয়ে save করুন |
| DATABASE_URL | ✅ (সুপারিশকৃত) | https://neon.tech → free account → Create project → Connection string কপি (`postgresql://...`)। না দিলে queue restart-এ হারাবে |
| MAX_RETRIES | ঐচ্ছিক | ডিফল্ট 2 |
| PROGRESS_INTERVAL | ঐচ্ছিক | কত সেকেন্ড পরপর message update (ডিফল্ট 8, ন্যূনতম 4) |
| X264_PRESET | ঐচ্ছিক | ডিফল্ট `veryfast`; দ্রুত চাইলে `ultrafast` |
| FFMPEG_THREADS | ঐচ্ছিক | ডিফল্ট 1 (কম RAM) |
| MAX_INPUT_MB | ঐচ্ছিক | ডিফল্ট 2000 |
| DISK_MARGIN_MB | ঐচ্ছিক | ডিফল্ট 300 |

`PORT` Render নিজেই দেয়, দিতে হবে না। **কোনো token/hash কখনো GitHub-এ রাখবেন না।**

## PART 6 — GitHub Upload Guide (Android)

1. Chrome-এ https://github.com খুলে account বানান/লগইন করুন (Chrome menu → **Desktop site** চালু করলে সহজ)।
2. উপরে **+ → New repository** → নাম `telegram-video-compressor` → **Private** → Create।
3. **uploading an existing file** লিংকে ট্যাপ করুন → **choose your files** → ফোনের Downloads থেকে এই ফাইলগুলো একসাথে সিলেক্ট করুন: `main.py, config.py, database.py, worker.py, ffmpeg_manager.py, handlers.py, ui.py, requirements.txt, render.yaml, README.md` → **Commit changes**।
4. `Dockerfile` ও `.gitignore` (ফাইল-ম্যানেজারে লুকানো/এক্সটেনশনহীন থাকতে পারে) তৈরি করুন: **Add file → Create new file** → নামের ঘরে ঠিক `Dockerfile` লিখুন → নিচের কোড পেস্ট → Commit। একইভাবে `.gitignore`।

Dockerfile:
```
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-u", "main.py"]
```
.gitignore:
```
__pycache__/
*.pyc
.env
*.session
*.db
downloads/
```
5. রিপোজিটরির root-এ সব ফাইল দেখা যাচ্ছে কিনা মিলিয়ে নিন (ফোল্ডারের ভেতরে নয়)।

## PART 7 — Render Deployment

1. https://render.com → GitHub দিয়ে Sign up।
2. **New + → Web Service** → GitHub repo সিলেক্ট (প্রথমবার GitHub-এ Render-কে access দিন)।
3. Settings:
   - **Language/Runtime: Docker** (Build/Start command লাগবে না; Dockerfile থেকে চলবে)
   - **Instance Type: Free**
   - Region: যেটা কাছে
   - **Advanced → Health Check Path:** `/health`
4. **Environment Variables** (Part 5) একটি একটি করে যোগ করুন।
5. **Create Web Service** → Logs-এ দেখুন `bot started`।
6. **ঘুমিয়ে পড়া ঠেকাতে:** https://uptimerobot.com → free account → Add monitor → HTTP(s) → URL = আপনার Render URL (`https://....onrender.com/health`) → interval 5 মিনিট।
7. ⚠️ Free plan-এ মাসে ৭৫০ instance-hour; একটি সার্ভিস ২৪×৭ চললে প্রায় পুরোটা লাগে। Render বিনা নোটিশে restart দিতে পারে — recovery এ কারণেই বানানো।

## PART 8 — First Test

1. বটকে `/start` দিন। (অনুমতি না থাকলে ID দেখাবে → ALLOWED_USER_IDS-এ বসান।)
2. **ছোট ভিডিও (১–২ মিনিট, <20 MB)** → 360p / 24 FPS → progress, speed, ফলাফল ঠিক আছে কিনা দেখুন। এই সময়ের `Speed` দিয়ে বাকি সময় অনুমান করুন।
3. 100–300 MB → 480p/720p
4. 500 MB+ → আগে ডিস্ক/মেমরি ঠিক আছে কিনা দেখুন
5. 1 GB+ → ধীর হবে, Render Logs দেখুন
6. 2 GB+ → বট জানাবে ~2000 MB-এর বেশি নেওয়া যায় না
7. /queue, /status, /cancel এবং একাধিক ভিডিও পরপর পাঠিয়ে queue পরীক্ষা করুন। Render-এ **Manual Deploy → Restart** দিয়ে recovery পরীক্ষা করুন।

## PART 9 — Troubleshooting

| সমস্যা | সমাধান |
|---|---|
| `FFmpeg not found` | Runtime অবশ্যই **Docker** হতে হবে |
| Out of memory / "killed" | 1080p এড়িয়ে 720p; FFMPEG_THREADS=1 রাখুন; X264_PRESET=ultrafast |
| No space left | বট নিজেই জানায়; ছোট ফাইল/কম quality, অথবা বড় ডিস্কের hosting লাগবে |
| Telegram file too large | বট ~2000 MB পর্যন্ত; তার বেশি অসম্ভব |
| Render restart | স্বাভাবিক; job আবার queue-তে ফেরে (শুরু থেকে)। Neon DATABASE_URL না থাকলে queue হারায় |
| Job stuck | Logs দেখুন; /cancel দিয়ে বাতিল করে আবার দিন |
| Upload failed | বট ৩ বার চেষ্টা করে (শেষবার ফাইল হিসেবে)। তবুও fail হলে পুরো job আবার চলে |
| Bot not responding | UptimeRobot চলছে কিনা, Logs-এ error, API_ID/API_HASH/BOT_TOKEN ঠিক কিনা |
| Compression failed | নষ্ট/অসমর্থিত ফাইল; Retry বাটন দিন |
| `Not authorized` | ALLOWED_USER_IDS-এ আপনার ID বসান |

## PART 10 — ভবিষ্যৎ উন্নতি
pause/resume, queue reorder, compression history, admin stats, custom bitrate/resolution, audio-only extraction, thumbnail, আপলোড ব্যর্থ হলে আউটপুট সংরক্ষণ (পুনরায় encode না করে), 2-pass encoding, এবং **বেশি CPU-র free/সস্তা hosting** (যেমন Hugging Face Spaces বা Oracle Always Free — চালু আছে কিনা ও শর্ত নিজে যাচাই করুন; একই Dockerfile ব্যবহার করা যাবে, শুধু PORT মিলাতে হবে)।
