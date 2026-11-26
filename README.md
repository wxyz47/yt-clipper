# yt-clipper-bot 🎬

A Telegram bot that turns a YouTube video into **5 vertical highlight
clips** (1080×1920, ready for Shorts/Reels/TikTok): send a link, the bot
downloads the video, transcribes it, scores and picks the best moments,
renders them, and sends the clips back to your chat.

The bot is **owner-locked**: the first chat that sends `/start` becomes
the owner, and every other chat is politely refused.

## Features

- **5 clips per video** — 30–90 s moments, sentence-aligned, scored for
  hooks (laughter, exclamations, questions, numbers, hook words)
- **Vertical 1080×1920 renders** — blurred-background fill, H.264/AAC,
  loudness-normalized to −14 LUFS, kept under Telegram's 50 MB limit
- **Live progress** — one status message updates in place with a
  percentage bar for downloading, transcribing, and rendering
- **Automatic retries** — downloads rotate through your proxies,
  transcription and rendering retry up to 3 times, sending retries 3
  times; the bot tells you in chat whenever a retry happens
- **Detailed error reports** — no generic one-line errors: failures
  include the failing tool's full error output (last 15 lines) in chat,
  or as an attached `.txt` log when it is too long for one message
- **In-bot proxy management** — save, enable/disable, and clear proxies
  from Telegram with `/setproxy` and `/setting` (no code edits needed)
- **Optional subtitles** — bold word-by-word captions with keyword
  highlighting, burned in from Whisper word timestamps
- **Optional cookies** — drop a `cookies.txt` next to `bot.py` to get
  past YouTube bot-checks (see *Notes* below)

## Requirements

- Python **3.12** (two virtualenvs: one for the bot, one for
  transcription)
- **ffmpeg** (with libx264, libass, and the drawtext/subtitles filters)
- **Node.js** — yt-dlp uses it as the JavaScript runtime for YouTube's
  challenge solver (`--js-runtimes node` + the `yt-dlp-ejs` package)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

1. **Get a token.** Talk to @BotFather → `/newbot` → copy the token.

2. **Clone and create the virtualenvs:**

   ```bash
   git clone <this-repo> yt-clipper-bot && cd yt-clipper-bot

   python3 -m venv venv
   venv/bin/pip install -r requirements.txt

   python3 -m venv transcribe-venv
   transcribe-venv/bin/pip install -r requirements-transcribe.txt
   ```

   The transcription venv is separate because faster-whisper is heavy;
   the bot only calls it as a subprocess. If yours lives elsewhere, set
   `TRANSCRIBE_PYTHON=/path/to/transcribe-venv/bin/python`.

3. **Provide the token** (never commit it):

   ```bash
   export BOT_TOKEN='123456789:AA...'   # preferred
   # …or, for local development only:
   echo '123456789:AA...' > token.txt && chmod 600 token.txt
   ```

   See `.env.example` for all supported environment variables.

4. **Run it:**

   ```bash
   ./start.sh        # supervisor loop with auto-restart
   ```

   …or with systemd:

   ```ini
   # ~/.config/systemd/user/yt-clipper-bot.service
   [Unit]
   Description=yt-clipper-bot Telegram clipper bot
   After=network-online.target

   [Service]
   WorkingDirectory=/path/to/yt-clipper-bot
   Environment=BOT_TOKEN=123456789:AA...
   ExecStart=/path/to/yt-clipper-bot/venv/bin/python bot.py
   Restart=always

   [Install]
   WantedBy=default.target
   ```

   ```bash
   systemctl --user enable --now yt-clipper-bot
   ```

5. **Claim ownership:** open your bot in Telegram and send `/start`.

## Commands

| Command | What it does |
|---|---|
| `/start` | Claim ownership (first run) / show help |
| `/help` | Show help |
| `/clip <YouTube link>` | Make 5 vertical clips from the video |
| `/setting` | Toggle subtitles, watermark, title, proxy; clear proxies |
| `/sethandle <@handle>` | Set the watermark handle |
| `/setproxy` | Show proxy instructions, or save proxies sent with the command |
| `/clearproxy` | Delete all saved proxies and turn proxy off |

## Settings

Settings are per-owner, stored in `settings.json`, and changed from the
`/setting` inline keyboard:

- **Subtitles** — burn word-by-word captions (keyword-highlighted) into
  each clip
- **Watermark** — show the handle at the top-right of each clip
- **Title on video** — show a punchy 2–6 word title at the top
- **Proxy (N saved)** — route downloads through your saved proxies

### Proxies, managed from inside Telegram

Send `/setproxy` with one proxy per line (paste several at once). The
new list replaces the old one and proxy turns ON automatically.
Accepted formats:

```
http://user:pass@host:port
http://host:port
host:port:user:pass        (Webshare-style export)
host:port
socks5://user:pass@host:port
```

The bot replies with how many lines were accepted and quotes back any
rejected lines so you can fix them. See `proxies.example.txt`. While
proxy is ON, every download attempt rotates to the next saved proxy
until one succeeds.

## How it works

```
YouTube link
  → yt-dlp download (≤1080p, optional cookies.txt + proxy rotation,
    EJS challenge solver via Node)
  → faster-whisper transcription (word timestamps, PROGRESS lines)
  → moment scoring (laughter / "!" / "?" / numbers / hook words)
  → ffmpeg render: 1080×1920 blurred-fill, optional ASS captions,
    title, watermark, loudnorm −14 LUFS, CRF steps 20→24→28 to fit 45 MB
  → clips sent back to the chat (3 send attempts each)
```

State files (`owner.json`, `settings.json`, per-job folders under
`jobs/`) are created at runtime and are gitignored, as are `token.txt`
and `cookies.txt`.

## Notes on YouTube bot-checks

YouTube increasingly refuses downloads from data-center IPs with
*"Sign in to confirm you're not a bot"*, and hides the actual video
formats until its JS challenge is solved. yt-clipper-bot counters this with
three layers, in order:

1. The **EJS challenge solver** (`yt-dlp-ejs` + Node) — always on.
2. **Proxies** — residential proxies work best; data-center proxies are
   often flagged too. Manage them in-bot with `/setproxy`.
3. **Cookies** — export `cookies.txt` from a browser where you are
   logged in to YouTube (e.g. with a "Get cookies.txt" extension) and
   place it next to `bot.py`. It is used automatically when present.
   Treat this file like a password: it is your logged-in session.

Transcription language auto-detects by default; set
`TRANSCRIBE_LANGUAGE=id` (or `en`, …) to force one. The moment-scoring
hook words in `bot.py` are tuned for Indonesian conversational content —
edit `HOOK_WORDS`/`LAUGH` for other languages.

## Fonts

`fonts/` bundles Anton and Poppins (ExtraBold), used for the on-video
title and captions/watermark. Both are licensed under the SIL Open Font
License 1.1.

## License

MIT — see [LICENSE](LICENSE).
