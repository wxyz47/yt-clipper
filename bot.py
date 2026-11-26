#!/usr/bin/env python3
"""yt-clipper — Telegram highlight clipper bot.

Long polling via python-telegram-bot. The bot token is read from the
BOT_TOKEN environment variable (preferred) or from a local token.txt
file (gitignored, for local development) — never hard-code it.

Pipeline: yt-dlp download -> faster-whisper transcription (subprocess,
separate venv) -> moment scoring -> ffmpeg vertical render -> send back.

Features: 5 vertical clips per video, live % progress in one status
message, automatic retries per stage, and detailed (untruncated) error
reports in chat. Optional: user-managed proxies via /setproxy and an
optional cookies.txt for YouTube bot-checks.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE, "token.txt")
OWNER_FILE = os.path.join(BASE, "owner.json")
SETTINGS_FILE = os.path.join(BASE, "settings.json")
JOBS = os.path.join(BASE, "jobs")
VENV_PY = os.path.join(BASE, "venv", "bin", "python")
YTDLP = os.path.join(BASE, "venv", "bin", "yt-dlp")
# Python interpreter of the separate transcription venv (faster-whisper).
# Override with the TRANSCRIBE_PYTHON environment variable.
TRANSCRIBE_PY = os.environ.get(
    "TRANSCRIBE_PYTHON",
    os.path.join(BASE, "transcribe-venv", "bin", "python"))
TRANSCRIBE_WORKER = os.path.join(BASE, "transcribe_worker.py")
COOKIES_FILE = os.path.join(BASE, "cookies.txt")
FONT_TITLE = os.path.join(BASE, "fonts", "Anton-Regular.ttf")
FONT_POP = os.path.join(BASE, "fonts", "Poppins-ExtraBold.ttf")
MAX_BYTES = 45 * 1024 * 1024  # stay under Telegram's 50 MB bot upload limit

DEFAULT_SETTINGS = {"subtitle": True, "watermark": True, "title": True,
                    "handle": "@yourhandle", "clips": 5,
                    "proxy_enabled": False, "proxies": []}

logging.basicConfig(filename=os.path.join(BASE, "bot.log"),
                    format="%(asctime)s %(levelname)s %(message)s",
                    level=logging.INFO)
log = logging.getLogger("yt-clipper")
log.addHandler(logging.StreamHandler(sys.stdout))
# httpx/telegram log full request URLs (which contain the token) at INFO —
# silence them so the token never lands in bot.log.
for _name in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# Hook/laughter markers used to score candidate moments. The list is
# tuned for Indonesian conversational content; edit freely for other
# languages — it only affects which moments get picked.
HOOK_WORDS = ["gila", "banget", "ternyata", "tiba-tiba", "malah", "jujur",
              "serius", "parah", "ngakak", "ketawa", "kaget", "rahasia",
              "pertama", "menang", "kalah", "unfollow", "viral", "wow",
              "masa sih", "gimana", "kenapa", "bhab", "bab"]
LAUGH = ["haha", "wkwk", "hehe", "[laughter]", "ketawa", "ngakak", "😂", "🤣"]


# ---------- state helpers ----------
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def get_owner():
    d = load_json(OWNER_FILE, {})
    return d.get("chat_id")


def get_settings(chat_id):
    all_s = load_json(SETTINGS_FILE, {})
    s = dict(DEFAULT_SETTINGS)
    s.update(all_s.get(str(chat_id), {}))
    return s


def set_setting(chat_id, key, value):
    all_s = load_json(SETTINGS_FILE, {})
    cur = dict(DEFAULT_SETTINGS)
    cur.update(all_s.get(str(chat_id), {}))
    cur[key] = value
    all_s[str(chat_id)] = cur
    save_json(SETTINGS_FILE, all_s)
    return cur


async def guard(update: Update) -> bool:
    """Owner lock. The first /start claims ownership. True if allowed."""
    chat_id = update.effective_chat.id
    owner = get_owner()
    if owner is None:
        return update.message and update.message.text and \
            update.message.text.startswith("/start")
    if chat_id != owner:
        if update.message:
            await update.message.reply_text(
                "Sorry, this bot is private and only its owner can use it. 🙏")
        elif update.callback_query:
            await update.callback_query.answer(
                "Private bot — owner only.", show_alert=True)
        return False
    return True


# ---------- proxy parsing ----------
def parse_proxy_line(line):
    """Normalize one proxy line to a URL, or return None if invalid.

    Accepted formats:
      http://user:pass@host:port     (any scheme: http/https/socks4/socks5)
      http://host:port
      host:port:user:pass            (Webshare-style export)
      host:port
    """
    line = line.strip()
    if not line:
        return None
    if "://" in line:
        try:
            u = urllib.parse.urlparse(line)
            port = u.port  # raises ValueError on a bad port
        except Exception:
            return None
        if u.scheme not in ("http", "https", "socks4", "socks5"):
            return None
        if not u.hostname or not port or not (1 <= port <= 65535):
            return None
        return line
    parts = line.split(":")
    if len(parts) == 2:
        host, port = parts
        if host and port.isdigit() and 1 <= int(port) <= 65535:
            return f"http://{host}:{port}"
        return None
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        if host and port.isdigit() and 1 <= int(port) <= 65535 and user:
            return f"http://{user}:{password}@{host}:{port}"
        return None
    return None


def mask_proxy(url):
    """Hide the password of a proxy URL for display purposes."""
    try:
        u = urllib.parse.urlparse(url)
        if u.password:
            netloc = f"{u.username}:***@{u.hostname}:{u.port}"
            return urllib.parse.urlunparse(
                (u.scheme, netloc, "", "", "", ""))
    except Exception:
        pass
    return url


# ---------- moment picking ----------
def score_segment(text):
    t = text.lower()
    score = 0.0
    for w in LAUGH:
        if w in t:
            score += 3
    score += text.count("!") * 2
    score += text.count("?") * 1.5
    if re.search(r"\d", text):
        score += 2
    for w in HOOK_WORDS:
        if w in t:
            score += 1
    score += min(len(text) / 60.0, 2.0)
    return score


def pick_moments(segments, n=5, min_len=30, max_len=90):
    if not segments:
        return []
    seg_scores = [score_segment(s["text"]) for s in segments]
    candidates = []
    for i in range(len(segments)):
        start = segments[i]["start"]
        total = 0.0
        for j in range(i, len(segments)):
            dur = segments[j]["end"] - start
            if dur > max_len:
                break
            total += seg_scores[j]
            if dur >= min_len:
                candidates.append((total / (dur ** 0.5), i, j))
    candidates.sort(reverse=True)
    chosen = []
    used = []
    for _score, i, j in candidates:
        s, e = segments[i]["start"], segments[j]["end"]
        if any(not (e < a or s > b) for a, b in used):
            continue
        chosen.append((i, j))
        used.append((s, e))
        if len(chosen) >= n:
            break
    chosen.sort()
    moments = []
    for i, j in chosen:
        words = []
        for s in segments[i:j + 1]:
            words.extend(s.get("words") or [])
        text = " ".join(s["text"] for s in segments[i:j + 1])
        moments.append({"start": segments[i]["start"],
                        "end": segments[j]["end"], "text": text,
                        "words": words})
    return moments


def make_title(text):
    """Punchy 2-6 word title from the moment's most hook-y words."""
    clean = re.sub(r"[^\w\s]", "", text)
    words = clean.split()
    if not words:
        return "BEST MOMENT"
    # start at a hook word if present
    start = 0
    for idx, w in enumerate(words):
        if w.lower() in HOOK_WORDS or w.isdigit():
            start = max(0, idx - 1)
            break
    title = " ".join(words[start:start + 5]).upper()
    return title if len(title.split()) >= 2 else \
        " ".join(words[:5]).upper()


# ---------- captions (ASS) ----------
def ass_time(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(path, words, clip_start, emphasis):
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080",
             "PlayResY: 1920", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, "
             "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
             "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
             "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
             "MarginV, Encoding",
             "Style: Cap,Poppins ExtraBold,64,&H00FFFFFF,&H0000D7FF,"
             "&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,2,2,40,40,"
             "420,1", "",
             "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
             "MarginV, Effect, Text"]
    chunks = []
    cur = []
    for w in words:
        cur.append(w)
        txt = "".join(x["word"] for x in cur).strip()
        if len(cur) >= 4 or (len(cur) >= 2 and
                             w["word"].strip().endswith((",", ".", "!", "?"))):
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)
    for ch in chunks:
        start = max(0.0, ch[0]["start"] - clip_start)
        end = max(start + 0.3, ch[-1]["end"] - clip_start)
        parts = []
        for w in ch:
            raw = w["word"].strip()
            esc = raw.replace("{", "").replace("}", "")
            if raw.lower().strip(".,!?") in emphasis:
                parts.append("{\\c&H00D7FF&}" + esc + "{\\c&HFFFFFF&}")
            else:
                parts.append(esc)
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},"
                     f"Cap,,0,0,0,,{' '.join(parts)}")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def esc_drawtext(s):
    return (s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "")
             .replace("%", "\\%"))


# ---------- pipeline ----------
def run(cmd, **kw):
    log.info("run: %s", " ".join(cmd)[:200])
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


class StageError(RuntimeError):
    """A pipeline stage failed after all retries.

    Carries the stage label plus the raw tool output (err_text) so the
    final handler can send the FULL error detail to the chat — never a
    truncated one-liner.
    """

    def __init__(self, stage, attempts, err_text):
        self.stage = stage
        self.attempts = attempts
        self.err_text = err_text or ""
        super().__init__(f"Failed at the {stage} stage after "
                         f"{attempts} attempt(s)")


def error_detail(text, max_lines=15, max_chars=3000):
    """Full relevant error text: last up-to-15 non-empty lines of the
    tool output, capped at ~3000 characters. No one-line truncation."""
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ("(no error output was captured — the process may have "
                "been killed, e.g. out of memory or a server restart)")
    detail = "\n".join(lines[-max_lines:])
    if len(detail) > max_chars:
        detail = "…" + detail[-max_chars:]
    return detail


def progress_bar(pct):
    """10-block bar, e.g. pct=42 -> '████░░░░░░'."""
    filled = max(0, min(10, int(round(float(pct) / 10.0))))
    return "█" * filled + "░" * (10 - filled)


def parse_ffmpeg_progress(line):
    """Parse one ffmpeg -progress line -> out_time in seconds, or None."""
    if "=" not in line:
        return None
    key, _, val = line.partition("=")
    if key.strip() in ("out_time_ms", "out_time_us"):
        try:
            return int(val.strip()) / 1_000_000.0
        except ValueError:
            return None
    return None


class StatusEditor:
    """Edits the single status message in place.

    say()  — stage changes: always edits.
    prog() — progress updates: throttled to at most one edit per 10s AND
             only when pct moved >= 3 points (pct=None: time rule only).
    A failed edit never raises.
    """

    def __init__(self, message):
        self.msg = message
        self._last_edit = 0.0
        self._last_pct = -1000

    async def say(self, text):
        try:
            await self.msg.edit_text(text)
            self._last_edit = time.monotonic()
            self._last_pct = -1000
        except Exception:
            pass

    async def prog(self, text, pct=None, force=False):
        now = time.monotonic()
        if not force:
            if now - self._last_edit < 10:
                return
            if pct is not None and abs(pct - self._last_pct) < 3:
                return
        try:
            await self.msg.edit_text(text)
            self._last_edit = now
            if pct is not None:
                self._last_pct = pct
        except Exception:
            pass


def _build_filters(job_dir, idx, moment, settings):
    """Filter graph for one clip. Returns (filter_complex_str, out_label)."""
    start = moment["start"]
    vf = ("[0:v]split=2[bg][fg];"
          "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920,boxblur=20:5,eq=brightness=-0.15[bg2];"
          "[fg]scale=1080:-2[fgs];"
          "[bg2][fgs]overlay=(W-w)/2:(H-h)/2[v0]")
    filters = [vf]
    cur = "[v0]"
    if settings["subtitle"] and moment["words"]:
        ass = os.path.join(job_dir, f"clip_{idx}.ass")
        emphasis = {w.lower() for w in HOOK_WORDS}
        emphasis.update({"bhab", "bab", "bhap"})
        write_ass(ass, moment["words"], start, emphasis)
        filters.append(f"{cur}subtitles='{ass}'[vsub]")
        cur = "[vsub]"
    if settings["title"]:
        title = esc_drawtext(make_title(moment["text"]))
        filters.append(
            f"{cur}drawtext=fontfile='{FONT_TITLE}':text='{title}':"
            f"fontsize=72:fontcolor=white:borderw=3:bordercolor=black:"
            f"x=(w-text_w)/2:y=180[vt]")
        cur = "[vt]"
    if settings["watermark"]:
        handle = esc_drawtext(settings["handle"])
        filters.append(
            f"{cur}drawtext=fontfile='{FONT_POP}':text='{handle}':"
            f"fontsize=44:fontcolor=white@0.6:borderw=2:bordercolor=black@0.6:"
            f"x=w-text_w-40:y=60[vw]")
        cur = "[vw]"
    return ";".join(filters), cur


async def render_clip_async(job_dir, idx, moment, settings, src, editor,
                            total_clips=5):
    """Render one clip with live % progress via ffmpeg -progress pipe:1.
    Raises RuntimeError with the FULL ffmpeg stderr tail on failure."""
    start, end = moment["start"], moment["end"]
    dur = end - start
    out = os.path.join(job_dir, f"clip_{idx}.mp4")
    filt, cur = _build_filters(job_dir, idx, moment, settings)
    err_path = os.path.join(job_dir, f"render_err_{idx}.log")
    for crf in (20, 24, 28):
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}",
               "-i", src, "-t", f"{dur:.2f}",
               "-filter_complex", filt,
               "-map", cur, "-map", "0:a", "-r", "30",
               "-c:v", "libx264", "-profile:v", "high", "-pix_fmt",
               "yuv420p", "-preset", "medium", "-crf", str(crf),
               "-c:a", "aac", "-ar", "48000", "-b:a", "192k",
               "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
               "-movflags", "+faststart",
               "-progress", "pipe:1", "-nostats", out]
        log.info("run: %s", " ".join(cmd)[:200])
        with open(err_path, "wb") as err_f:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=err_f)
            try:
                async for raw in proc.stdout:
                    t = parse_ffmpeg_progress(
                        raw.decode(errors="replace").strip())
                    if t is not None and dur > 0:
                        pct = max(0, min(99, int(t / dur * 100)))
                        await editor.prog(
                            f"🎬 Rendering clip {idx}/{total_clips}… "
                            f"{pct}% {progress_bar(pct)}", pct)
            finally:
                await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(_read_tail(err_path, 8000))
        if os.path.getsize(out) <= MAX_BYTES:
            break
    return out


def _read_tail(path, n=8000):
    try:
        with open(path, errors="replace") as f:
            return f.read()[-n:]
    except Exception:
        return ""


async def fetch_total_size(base_args, url):
    """Approximate total download size via yt-dlp --print, or None."""
    cmd = base_args + ["--simulate", "--print", "%(filesize_approx)s", url]
    try:
        r = await asyncio.to_thread(run, cmd, timeout=120)
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit() and int(line) > 0:
                return int(line)
    except Exception:
        pass
    return None


async def run_download(args, job_dir, total, editor):
    """Run yt-dlp, polling the growing part files for live %.
    Returns (returncode, stderr_tail)."""
    err_path = os.path.join(job_dir, "download_err.log")
    log.info("run: %s", " ".join(args)[:200])
    with open(err_path, "wb") as err_f:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=err_f)
        started = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
                break
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - started > 1800:
                proc.kill()
                await proc.wait()
                break
            downloaded = 0
            try:
                for name in os.listdir(job_dir):
                    if name.startswith("source"):
                        try:
                            downloaded += os.path.getsize(
                                os.path.join(job_dir, name))
                        except OSError:
                            pass
            except OSError:
                pass
            if total:
                pct = max(0, min(99, int(downloaded * 100 / total)))
                await editor.prog(
                    f"⬇️ Downloading… {pct}% {progress_bar(pct)}", pct)
            else:
                mb = downloaded / (1024 * 1024)
                await editor.prog(f"⬇️ Downloading… {mb:.1f} MB", None)
    return proc.returncode, _read_tail(err_path)


async def run_transcribe_once(src, tj, job_dir, editor):
    """One transcribe attempt, streaming PROGRESS lines from the worker.
    Returns (returncode, stderr_tail)."""
    err_path = os.path.join(job_dir, "transcribe_err.log")
    log.info("run: %s %s", TRANSCRIBE_WORKER, src)
    with open(err_path, "wb") as err_f:
        proc = await asyncio.create_subprocess_exec(
            TRANSCRIBE_PY, TRANSCRIBE_WORKER, src, tj,
            stdout=asyncio.subprocess.PIPE, stderr=err_f)
        try:
            async def consume():
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS"):
                        try:
                            pct = int(float(line.split()[1]))
                        except Exception:
                            continue
                        pct = max(0, min(99, pct))
                        await editor.prog(
                            f"🎙️ Transcribing… {pct}% {progress_bar(pct)}",
                            pct)
                await proc.wait()

            await asyncio.wait_for(consume(), timeout=3600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "transcription timed out (>60 minutes)"
    return proc.returncode, _read_tail(err_path)


async def process_clip_job(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           url: str):
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)
    job_dir = os.path.join(JOBS, f"{chat_id}_{int(time.time())}")
    os.makedirs(job_dir, exist_ok=True)
    status = await update.message.reply_text("⏳ Starting the clip job…")
    editor = StatusEditor(status)
    say = editor.say

    async def notify(text):
        """Send a NEW chat message (retry/failure info). Never raises."""
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            pass

    async def send_detail(stage_label, err_text):
        """Send the FULL error detail for a stage: last up-to-15 lines,
        ~3000 chars, as its own message; if the combined text would
        exceed Telegram's 4096-char message limit, attach the complete
        log as a .txt document instead."""
        detail = error_detail(err_text)
        text = f"🧾 {stage_label} — full error output:\n{detail}"
        if len(text) <= 4000:
            await notify(text)
            return
        log_path = os.path.join(job_dir, "error_log.txt")
        try:
            with open(log_path, "w") as f:
                f.write(err_text or detail)
            await notify(f"🧾 {stage_label} — the error output is long; "
                         f"the full log is attached as a file.")
            with open(log_path, "rb") as fh:
                await context.bot.send_document(
                    chat_id=chat_id, document=fh,
                    filename="error_log.txt")
        except Exception:
            await notify(text[:4000])

    try:
        # ---------- download (proxy rotation = the retries) ----------
        await say("⬇️ Downloading… 0% " + progress_bar(0))
        src = os.path.join(job_dir, "source.mp4")

        def dl_args(px, extra):
            a = [YTDLP, "--no-check-certificate", "--js-runtimes", "node"]
            if os.path.exists(COOKIES_FILE):
                a += ["--cookies", COOKIES_FILE]
            if px:
                a += ["--proxy", px]
            a += ["-f", "bv*[height<=1080]+ba/b"] + extra
            return a

        saved_proxies = settings.get("proxies") or []
        if settings.get("proxy_enabled") and saved_proxies:
            attempts = saved_proxies
        else:
            attempts = [None]  # single direct attempt
        total = await fetch_total_size(dl_args(attempts[0], []), url)
        rc, tail = -1, ""
        detail_sent = False
        for n_attempt, px in enumerate(attempts, 1):
            rc, tail = await run_download(
                dl_args(px, ["--merge-output-format", "mp4",
                             "--no-playlist", "-o", src, url]),
                job_dir, total, editor)
            if rc == 0 and os.path.exists(src):
                break
            if n_attempt < len(attempts):
                via = (f"proxy #{n_attempt} ({mask_proxy(px)})" if px
                       else "direct connection")
                await notify(f"⚠️ Download failed via {via} "
                             f"(attempt {n_attempt}/{len(attempts)}). "
                             f"Trying the next one…")
                if not detail_sent:
                    await send_detail(
                        f"Download attempt {n_attempt}", tail)
                    detail_sent = True
                await say("⬇️ Downloading… 0% " + progress_bar(0))
        if rc != 0 or not os.path.exists(src):
            if "not a bot" in tail or "Sign in" in tail:
                raise StageError(
                    "download (YouTube bot-check)", len(attempts),
                    tail + "\n\nHint: YouTube is refusing downloads from "
                    "this server's IP ('Sign in to confirm you're not a "
                    "bot'). This is a YouTube-side restriction, not a "
                    "problem with your link. Remedies: add proxies with "
                    "/setproxy and enable them in /setting, and/or place "
                    "a cookies.txt (exported from a logged-in browser) "
                    "next to bot.py on the server.")
            raise StageError("download", len(attempts), tail)

        # ---------- transcribe (up to 3 attempts) ----------
        await say("🎙️ Transcribing… 0% " + progress_bar(0))
        tj = os.path.join(job_dir, "transcript.json")
        transcript = None
        detail_sent = False
        for attempt in range(1, 4):
            rc, tail = await run_transcribe_once(src, tj, job_dir, editor)
            if rc == 0 and os.path.exists(tj):
                try:
                    with open(tj) as f:
                        transcript = json.load(f)
                    break
                except Exception as e:
                    tail = f"transcript file is corrupt: {e}"
            if attempt < 3:
                await notify(f"⚠️ Transcription failed "
                             f"(attempt {attempt}/3). Retrying…")
                if not detail_sent:
                    await send_detail(
                        f"Transcription attempt {attempt}", tail)
                    detail_sent = True
                await say("🎙️ Transcribing… 0% " + progress_bar(0))
            else:
                raise StageError("transcription", 3, tail)
        await editor.prog("🎙️ Transcribing… 100% " + progress_bar(100),
                          100, force=True)

        await say("🔎 Picking the 5 best moments…")
        moments = pick_moments(transcript["segments"],
                               n=int(settings.get("clips", 5)))
        if not moments:
            raise StageError("moment picking", 1,
                             "No moment of sufficient length could be "
                             "built from the transcript (the video may "
                             "have very little speech).")

        # ---------- render + send per clip ----------
        sent = 0
        for idx, moment in enumerate(moments, 1):
            await say(f"🎬 Rendering clip {idx}/{len(moments)}… 0% "
                      + progress_bar(0))
            out = None
            detail_sent = False
            for attempt in range(1, 4):
                try:
                    out = await render_clip_async(
                        job_dir, idx, moment, settings, src, editor,
                        total_clips=len(moments))
                    break
                except Exception as e:
                    err_text = str(e)
                    if attempt < 3:
                        await notify(
                            f"⚠️ Rendering clip {idx} failed "
                            f"(attempt {attempt}/3). Retrying…")
                        if not detail_sent:
                            await send_detail(
                                f"Render clip {idx}, attempt {attempt}",
                                err_text)
                            detail_sent = True
                    else:
                        raise StageError(f"render clip {idx}", 3, err_text)
            dur = moment["end"] - moment["start"]

            def fmt(t):
                return f"{int(t//60):02d}:{int(t%60):02d}"

            caption = (f"🎬 Clip {idx}: {make_title(moment['text'])}\n"
                       f"⏱️ {dur:.0f} sec • source "
                       f"{fmt(moment['start'])}–{fmt(moment['end'])}")
            await say(f"📤 Sending clip {idx}/{len(moments)}…")
            delivered = False
            send_err = ""
            for attempt in range(1, 4):
                try:
                    with open(out, "rb") as fh:
                        await context.bot.send_video(
                            chat_id=chat_id, video=fh, caption=caption,
                            supports_streaming=True)
                    delivered = True
                    break
                except Exception as e:
                    send_err = str(e)
                    if attempt < 3:
                        await asyncio.sleep(5)
            if delivered:
                sent += 1
            else:
                await notify(
                    f"❌ Clip {idx} could not be sent after 3 attempts. "
                    f"The clip file is kept in the job folder: {out}")
                await send_detail(f"Sending clip {idx}", send_err)
        await say(f"✅ Done! {sent}/{len(moments)} clips sent. "
                  f"Subtitles: {'ON' if settings['subtitle'] else 'OFF'} • "
                  f"Watermark: {'ON' if settings['watermark'] else 'OFF'} • "
                  f"Title: {'ON' if settings['title'] else 'OFF'} • "
                  f"Proxy: {'ON' if settings.get('proxy_enabled') else 'OFF'}"
                  f" ({len(settings.get('proxies') or [])} saved)")
    except StageError as e:
        log.exception("job failed")
        await say(f"❌ Failed at the {e.stage} stage after "
                  f"{e.attempts} attempt(s). Full error output below ⬇️")
        await send_detail(f"Stage: {e.stage}", e.err_text)
    except Exception as e:
        log.exception("job failed")
        await say("❌ The job failed with an unexpected error. "
                  "Full error output below ⬇️")
        await send_detail("Unexpected error", repr(e))


# ---------- handlers ----------
WELCOME = (
    "Hi! I'm *yt-clipper* 🎬 — a highlight clip maker bot.\n\n"
    "How to use:\n"
    "• `/clip <YouTube link>` — make 5 vertical 1080x1920 clips from "
    "the best moments of a video\n"
    "• `/setting` — toggle subtitles, watermark, title, and proxies\n"
    "• `/sethandle <@handle>` — change the watermark handle\n"
    "• `/setproxy` — save proxies for downloads (instructions + "
    "accepted formats)\n"
    "• `/clearproxy` — remove all saved proxies\n"
    "• `/help` — this help\n\n"
    "Processing takes a while (download + transcription + rendering); "
    "I'll keep you posted on every stage, with live progress and full "
    "error details if anything fails.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if get_owner() is None:
        save_json(OWNER_FILE, {"chat_id": chat_id})
        await update.message.reply_text(
            "🔐 You are now registered as this bot's owner. Other chats "
            "will not be able to use it.\n\n" + WELCOME,
            parse_mode="Markdown")
        return
    if not await guard(update):
        return
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


def settings_keyboard(s):
    def lbl(name, on):
        return f"{'✅' if on else '❌'} {name}"
    n_proxy = len(s.get("proxies") or [])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl("Subtitles", s["subtitle"]),
                              callback_data="toggle:subtitle")],
        [InlineKeyboardButton(lbl("Watermark", s["watermark"]),
                              callback_data="toggle:watermark")],
        [InlineKeyboardButton(lbl("Title on video", s["title"]),
                              callback_data="toggle:title")],
        [InlineKeyboardButton(
            lbl(f"Proxy ({n_proxy} saved)", s.get("proxy_enabled", False)),
            callback_data="toggle:proxy_enabled")],
        [InlineKeyboardButton("🗑 Clear proxies",
                              callback_data="clearproxies")],
        [InlineKeyboardButton(f"✏️ Handle: {s['handle']}",
                              callback_data="noop")],
    ])


def settings_text(s):
    n_proxy = len(s.get("proxies") or [])
    return ("⚙️ *Clip settings*\n\n"
            f"Subtitles: {'ON' if s['subtitle'] else 'OFF'}\n"
            f"Watermark: {'ON' if s['watermark'] else 'OFF'}\n"
            f"Title on video: {'ON' if s['title'] else 'OFF'}\n"
            f"Proxy: {'ON' if s.get('proxy_enabled') else 'OFF'} "
            f"({n_proxy} saved)\n"
            f"Watermark handle: {s['handle']}\n"
            f"Clips per /clip: {s.get('clips', 5)}\n\n"
            "Tap a button to change it. Add proxies with `/setproxy`, "
            "change the handle with `/sethandle <@handle>`.")


async def setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    s = get_settings(update.effective_chat.id)
    await update.message.reply_text(settings_text(s),
                                    reply_markup=settings_keyboard(s),
                                    parse_mode="Markdown")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await guard(update):
        return
    chat_id = update.effective_chat.id
    if q.data.startswith("toggle:"):
        key = q.data.split(":", 1)[1]
        s = get_settings(chat_id)
        s = set_setting(chat_id, key, not s.get(key, False))
        await q.answer(f"{key}: {'ON' if s[key] else 'OFF'}")
        try:
            await q.edit_message_text(settings_text(s),
                                      reply_markup=settings_keyboard(s),
                                      parse_mode="Markdown")
        except Exception:
            pass
    elif q.data == "clearproxies":
        s = set_setting(chat_id, "proxies", [])
        s = set_setting(chat_id, "proxy_enabled", False)
        await q.answer("All saved proxies cleared.")
        try:
            await q.edit_message_text(settings_text(s),
                                      reply_markup=settings_keyboard(s),
                                      parse_mode="Markdown")
        except Exception:
            pass
    else:
        await q.answer("Change handle: /sethandle <@handle>")


async def sethandle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /sethandle <@handle>")
        return
    handle = context.args[0].strip()
    if not handle.startswith("@"):
        handle = "@" + handle
    set_setting(update.effective_chat.id, "handle", handle)
    await update.message.reply_text(f"✅ Watermark handle changed: {handle}")


SETPROXY_HELP = (
    "🌐 *Proxy settings*\n\n"
    "Send your proxies right after the command, one per line — you can "
    "paste several at once:\n\n"
    "`/setproxy`\n"
    "`proxy line 1`\n"
    "`proxy line 2`\n\n"
    "Accepted formats:\n"
    "• `http://user:pass@host:port`\n"
    "• `http://host:port`\n"
    "• `host:port:user:pass` (Webshare-style export)\n"
    "• `host:port`\n"
    "• `socks5://user:pass@host:port`\n\n"
    "The new list *replaces* the previously saved one, and proxies are "
    "enabled automatically. Downloads then rotate through your proxies "
    "until one works. Manage them in /setting, or remove them all "
    "with /clearproxy.")


async def setproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = update.message.text or ""
    # Everything after the command line itself is the proxy list.
    lines = text.split("\n")[1:]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        await update.message.reply_text(SETPROXY_HELP,
                                        parse_mode="Markdown")
        return
    accepted, rejected = [], []
    for ln in lines:
        parsed = parse_proxy_line(ln)
        if parsed:
            accepted.append(parsed)
        else:
            rejected.append(ln.strip())
    chat_id = update.effective_chat.id
    if accepted:
        set_setting(chat_id, "proxies", accepted)
        set_setting(chat_id, "proxy_enabled", True)
    msg = (f"✅ {len(accepted)} proxy(ies) accepted and saved "
           f"(proxy is now ON).")
    if rejected:
        msg += (f"\n\n❌ {len(rejected)} line(s) rejected:\n"
                + "\n".join(f"• {ln}" for ln in rejected)
                + "\n\nAccepted formats:\n"
                  "• `http://user:pass@host:port`\n"
                  "• `http://host:port`\n"
                  "• `host:port:user:pass`\n"
                  "• `host:port`\n"
                  "• `socks5://user:pass@host:port`")
    await update.message.reply_text(msg, parse_mode="Markdown")


async def clearproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    chat_id = update.effective_chat.id
    n = len(get_settings(chat_id).get("proxies") or [])
    set_setting(chat_id, "proxies", [])
    set_setting(chat_id, "proxy_enabled", False)
    await update.message.reply_text(
        f"🗑 Cleared {n} saved proxy(ies). Proxy is now OFF.")


async def clip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args or "youtu" not in context.args[0]:
        await update.message.reply_text(
            "Usage: /clip <YouTube link>\nExample: /clip https://youtu.be/xxxx")
        return
    if context.application.bot_data.get("busy"):
        await update.message.reply_text(
            "⏳ A clip job is already running. Please wait for it to finish.")
        return
    context.application.bot_data["busy"] = True
    try:
        await process_clip_job(update, context, context.args[0])
    finally:
        context.application.bot_data["busy"] = False


def load_token():
    """BOT_TOKEN env var first; token.txt (gitignored) as local fallback."""
    token = os.environ.get("BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def main():
    # Some environments ship malformed NO_PROXY entries (e.g. bracketed
    # IPv6) that crash httpx; Telegram's API is external, so a simple
    # localhost list is enough.
    for var in ("NO_PROXY", "no_proxy"):
        os.environ[var] = "localhost,127.0.0.1"
    token = load_token()
    if not token:
        sys.exit("No bot token found. Set the BOT_TOKEN environment "
                 "variable or put the token in token.txt (see README).")
    app = Application.builder().token(token).build()
    app.bot_data["busy"] = False
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clip", clip))
    app.add_handler(CommandHandler("setting", setting))
    app.add_handler(CommandHandler("sethandle", sethandle))
    app.add_handler(CommandHandler("setproxy", setproxy))
    app.add_handler(CommandHandler("clearproxy", clearproxy))
    app.add_handler(CallbackQueryHandler(on_callback))
    log.info("yt-clipper starting (long polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
