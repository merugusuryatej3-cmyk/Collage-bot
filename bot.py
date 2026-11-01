"""
TTC Collage Bot - private Telegram bot that turns screenshots into one collage.

FILES: bot.py (this file) + optional config.py (your settings). Nothing else is required.

=============================== CONFIGURATION ======================================
Every setting can come from an environment variable OR from config.py (next to bot.py).
Priority: environment variable > config.py > default. Blank values are ignored.

    Setting             Meaning
    BOT_TOKEN           token from @BotFather (required)
    WEBHOOK_SECRET      A-Z a-z 0-9 _ - only; required on Render (webhook mode)
    ADMIN_USERNAMES     admins by username, comma-separated, no @
    ADMIN_IDS           admins by numeric Telegram ID, comma-separated (safest)
    ALLOWED_USERNAMES   normal users by username
    ALLOWED_IDS         normal users by numeric ID (works even without a username)
    WATERMARK_TEXT      optional, default TTC (basic Latin only: the built-in font has
                        no accents or non-Latin scripts)
At least one admin/allowed entry is required. IDs beat usernames: usernames can be changed
or re-claimed by someone else, IDs cannot. To find an ID: message @userinfobot, send /id to
this bot, or read it from the "This bot is private" reply a stranger gets.
SECURITY: never put a real BOT_TOKEN in config.py in a PUBLIC repo. On a public repo leave
config.py out (or blank) and use Render environment variables instead.

=============================== SETUP ON RENDER (free) =============================
1. BotFather -> /newbot -> copy the token.
2. Webhook secret:  python -c "import secrets; print(secrets.token_urlsafe(32))"
3. Push bot.py (and config.py if you use it, private repo only) to GitHub.
4. Render -> New -> Web Service -> pick the repo, instance type Free, then set:
       Runtime        Python
       Build Command  pip install --no-cache-dir "python-telegram-bot[webhooks]~=22.8" "Pillow>=10.1,<13"
       Start Command  python bot.py
   Do NOT set a Health Check Path: the webhook server answers only POSTs on its secret path.
5. Environment variables (or put the same names in config.py):
       PYTHON_VERSION=3.12.8  PYTHONUNBUFFERED=1  MALLOC_ARENA_MAX=2   (always set on Render)
       BOT_TOKEN  WEBHOOK_SECRET  ADMIN_USERNAMES / ADMIN_IDS  ALLOWED_USERNAMES / ALLOWED_IDS
6. Deploy. The bot registers its own webhook from Render's RENDER_EXTERNAL_URL.

Local test (polling mode, WEBHOOK_SECRET not needed): fill in config.py, then
    pip install "python-telegram-bot[webhooks]~=22.8" "Pillow>=10.1,<13"
    python bot.py

================================== USING IT ========================================
Send up to 20 images -> the collage builds after 10 s of quiet, or send /done.
Delivered as a full-quality JPEG document. Commands:
    /layout (Auto, or 1-8 per row) /watermark /quality 1-95 /status /clear /id
Every image keeps its own aspect ratio (no cropping or stretching); rows are packed edge
to edge. An image that can't be read becomes a grey crossed-out tile and is counted as
"skipped" in the caption. For full resolution, send images as files (Telegram compresses
photos to ~1280 px). If the bot is busy your queued images are kept: send /done again in a
minute. While a collage is building, images beyond the 20-image queue are refused.

Admin panel (/admin): /allow @user|id (or reply), /ban @user|id (or reply), /unban,
/banlist, /broadcast <msg>, /setwelcome <text>, /resetwelcome, /stats

=========================== WHAT IS (NOT) STORED ===================================
* Images are never written to disk: each is downloaded into RAM, pasted onto the canvas,
  and discarded. A batch holds only Telegram file IDs and aspect ratios.
* Settings, bans, /allow additions, the custom welcome and user lists live in RAM only and
  reset when Render restarts the service (free instances have no disk). For permanent
  access changes, edit the allowlist settings above.
* /broadcast and /stats cover only users seen since the last restart.
* Telegram itself still keeps the chat's images.

=============================== RENDER FREE TIER ===================================
* 512 MB RAM, 0.1 CPU, sleeps after 15 min idle (~1 min wake-up; Telegram retries, so the
  first message usually still works). A restart drops any batch in progress.
* A 20-image collage takes roughly 10-60 s. One collage is built at a time; others queue
  (max 4 waiting).
* To avoid sleeping, ping your Render URL every ~10 min (e.g. UptimeRobot). A plain GET
  most likely returns 404 (only the secret path is served): the ping still wakes the
  service, but set the monitor to accept 404 or it will show "down". Uses ~744 of the 750
  free hours/month, so run only this one free service.
* Memory guards: inputs over 20 megapixels or 15 MB are skipped; output capped at 25 MP.

================================== SECURITY ========================================
* Only listed users can use the bot. Users with no match get the "private" reply (with
  their own ID); group chats are ignored. A ban always overrides an allowlist entry.
* Webhook uses a secret header and an unguessable URL path. The token is never logged.
  If it leaks, /revoke it in BotFather.
"""
import asyncio
import gc
import hashlib
import importlib.util
import io
import logging
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace

from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, TypeHandler, filters,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _load_config():
    """Optional config.py next to this file. Loaded by path so no installed 'config' package can shadow it."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("collage_config", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        raise SystemExit(f"config.py is invalid: {type(e).__name__}: {e}")


_CFG = _load_config()


def _cfg(name: str, default=""):
    """Environment variable wins (use this on Render); config.py is the fallback; blanks are ignored."""
    v = os.environ.get(name)
    if v is not None and v.strip():
        return v.strip()
    c = getattr(_CFG, name, None)
    if c is None or (isinstance(c, str) and not c.strip()):
        return default
    return c.strip() if isinstance(c, str) else c


def _items(raw) -> list:
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, str):
        return raw.replace(";", ",").split(",")
    return list(raw or [])


def _names(raw) -> set[str]:
    return {str(n).strip().lstrip("@").lower() for n in _items(raw) if str(n).strip()}


def _ids(raw, label: str) -> set[int]:
    out: set[int] = set()
    for n in _items(raw):
        s = str(n).strip()
        if not s:
            continue
        if not re.fullmatch(r"\d{1,15}", s):
            raise SystemExit(f"{label}: {s!r} is not a numeric Telegram user ID")
        out.add(int(s))
    return out


TOKEN = str(_cfg("BOT_TOKEN"))
WEBHOOK_SECRET = str(_cfg("WEBHOOK_SECRET"))               # A-Z a-z 0-9 _ -
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")  # set by Render (env only)
PORT = int(os.environ.get("PORT", "10000"))
ADMINS = _names(_cfg("ADMIN_USERNAMES"))
ALLOWED = _names(_cfg("ALLOWED_USERNAMES"))
ADMIN_UIDS = _ids(_cfg("ADMIN_IDS"), "ADMIN_IDS")          # numeric Telegram IDs (can't be re-claimed)
ALLOWED_UIDS = _ids(_cfg("ALLOWED_IDS"), "ALLOWED_IDS")

# Pillow's built-in font only has printable ASCII plus a few symbols; anything else renders as boxes.
_WM_EXTRA = "©«°±´·»"


def clean_wm(raw: str) -> str:
    return "".join(ch for ch in raw if 0x20 <= ord(ch) <= 0x7E or ch in _WM_EXTRA).strip()[:40]


DEFAULT_WATERMARK = clean_wm(str(_cfg("WATERMARK_TEXT", "TTC"))) or "TTC"

MAX_IMAGES = 20            # per collage
AUTO_BUILD_SEC = 10        # quiet time before auto-build
MAX_PER_ROW = 8
CELL_BASE = 540            # target width of one image, px
MAX_OUT_PX = 25_000_000    # output canvas cap (RGB = 3 bytes/px of RAM)
MAX_IN_PX = 20_000_000     # refuse to decode bigger inputs (RAM protection)
MAX_IN_MB = 15
BUILDS_PER_HOUR = 20
MAX_QUEUE = 4              # builds waiting globally
SESSION_TTL = 15 * 60
TARGET_ASPECT = 0.65       # auto layout aims for a slightly tall collage
WHITE = (255, 255, 255)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
for noisy in ("httpx", "httpcore", "telegram", "tornado"):  # httpx logs URLs containing the token
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("collage")

# --------------------------------------------------------------------------- #
# State (RAM only; resets when Render restarts the service)
# --------------------------------------------------------------------------- #
@dataclass
class Prefs:
    cols: int | None = None    # None = automatic layout, else images per row
    quality: int = 90
    wm: bool = False
    wm_text: str = DEFAULT_WATERMARK


@dataclass
class Session:
    items: list = field(default_factory=list)  # (file_id, aspect|None) - no pixels
    touched: float = field(default_factory=time.monotonic)  # last image received
    timer: asyncio.Task | None = None


PREFS: dict[int, Prefs] = {}
SESSIONS: dict[int, Session] = {}
ACTIVE: set[int] = set()                 # users with a build queued/running
BUILD_TASKS: dict[int, asyncio.Task] = {}  # uid -> running build (cancelled on /ban)
BUILD_TIMES: dict[int, deque] = {}
KNOWN: dict[int, str] = {}               # id -> username of users seen (for broadcast/ban)
BANNED_NAMES: set[str] = set()
BANNED_IDS: set[int] = set()
ADMIN_IDS: set[int] = set(ADMIN_UIDS)   # configured admin IDs + admins seen by username
WELCOME_OVERRIDE: str | None = None
BUILDS_DONE = 0
WAITING = 0
START = time.time()
BUILD_SEM = asyncio.Semaphore(1)
_throttle: dict[str, float] = {}


def retry_secs(e: RetryAfter) -> float:
    """PTB >= 22.2 may expose retry_after as int or timedelta; normalise to seconds."""
    v = e.retry_after
    return float(v.total_seconds() if hasattr(v, "total_seconds") else v)


def prefs_for(uid: int) -> Prefs:
    return PREFS.setdefault(uid, Prefs())


def throttle(key: str, seconds: float) -> bool:
    """True if allowed (and records it); False if called too recently."""
    now = time.monotonic()
    if now - _throttle.get(key, -1e9) < seconds:
        return False
    _throttle[key] = now
    if len(_throttle) > 2000:
        for k in [k for k, t in _throttle.items() if now - t > 3600]:
            del _throttle[k]
    return True


def drop_session(uid: int) -> None:
    s = SESSIONS.pop(uid, None)
    if s and s.timer and s.timer is not asyncio.current_task():
        s.timer.cancel()


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def _uname(user) -> str:
    return (user.username or "").lower()


def is_admin(user) -> bool:
    u = _uname(user)
    return user.id in ADMIN_UIDS or (bool(u) and u in ADMINS)


def is_allowed(user) -> bool:
    u = _uname(user)
    if user.id in BANNED_IDS or (u and u in BANNED_NAMES):  # a ban always wins
        return False
    if user.id in ADMIN_UIDS or user.id in ALLOWED_UIDS:    # ID match works even without a username
        return True
    return bool(u) and (u in ADMINS or u in ALLOWED)


async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler; anything unauthorised stops here."""
    user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
    if user is None or chat is None or chat.type != "private" or user.is_bot:
        raise ApplicationHandlerStop
    now = time.monotonic()
    for uid in [u for u, s in SESSIONS.items() if now - s.touched > SESSION_TTL]:
        drop_session(uid)
    if not is_allowed(user):
        if msg and throttle(f"deny:{user.id}", 60):
            try:
                await msg.reply_text(f"🔒 This bot is private.\nYour Telegram ID: {user.id}\n"
                                     "(send it to the admin if you want access)")
            except TelegramError:
                pass
        raise ApplicationHandlerStop
    KNOWN[user.id] = _uname(user)
    if is_admin(user):
        ADMIN_IDS.add(user.id)


# --------------------------------------------------------------------------- #
# Image work (worker threads, RAM only)
# --------------------------------------------------------------------------- #
def _probe(buf: io.BytesIO) -> float | None:
    """Aspect ratio (w/h) from the header only, honouring EXIF rotation."""
    with Image.open(buf) as im:
        w, h = im.size
        if w * h > MAX_IN_PX:
            return None
        if im.getexif().get(0x0112) in (5, 6, 7, 8):
            w, h = h, w
    return w / h


def _rows_heights(asp: list[float], sizes: list[int], W: float, g: float) -> list[float]:
    """Justified rows: every image keeps its own aspect, each row spans width W."""
    hs, i = [], 0
    for k in sizes:
        hs.append((W - (k - 1) * g) / sum(asp[i:i + k]))
        i += k
    if len(hs) > 1 and sizes[-1] < max(sizes[:-1]):  # short last row: don't blow it up
        hs[-1] = min(hs[-1], max(hs[:-1]))
    return hs


def plan_layout(aspects: list[float], cols: int | None):
    """(canvas_w, canvas_h, slots[(x, y, w, h)]) built from the images actually sent."""
    asp = [min(max(a, 0.2), 5.0) for a in aspects]
    n = len(asp)

    if cols:  # fixed: `cols` images per row
        sizes = [min(cols, n - i) for i in range(0, n, cols)]
    else:     # auto: row split whose overall shape is closest to the target
        best = None
        for c in range(1, min(n, MAX_PER_ROW) + 1):
            rows = math.ceil(n / c)
            sz = [n // rows + (1 if i < n % rows else 0) for i in range(rows)]
            hs = _rows_heights(asp, sz, 1000.0, 4.0)
            score = abs(math.log(1000.0 / (sum(hs) + 4.0 * (rows + 1)) / TARGET_ASPECT))
            if best is None or score < best[0] - 1e-9:
                best = (score, sz)
        sizes = best[1]

    W, g = max(sizes) * CELL_BASE, max(3, CELL_BASE // 60)
    hs = _rows_heights(asp, sizes, W, g)
    H = sum(round(h) for h in hs) + (len(hs) + 1) * g
    if (W + 2 * g) * H > MAX_OUT_PX:  # shrink to stay inside the RAM budget
        k = math.sqrt(MAX_OUT_PX / ((W + 2 * g) * H))
        W, g = int(W * k), max(2, int(g * k))
        hs = _rows_heights(asp, sizes, W, g)

    slots, y, i = [], g, 0
    for k, h in zip(sizes, hs):
        hi = max(1, round(h))
        widths = [max(1, round(h * asp[i + j])) for j in range(k)]
        if k > 1 and h == (W - (k - 1) * g) / sum(asp[i:i + k]):  # full row: absorb rounding
            widths[-1] = max(1, W - (k - 1) * g - sum(widths[:-1]))
        x = g + (W - (sum(widths) + (k - 1) * g)) // 2
        for w in widths:
            slots.append((x, y, w, hi))
            x += w + g
        y += hi + g
        i += k
    return W + 2 * g, y, slots


def _paste(buf: io.BytesIO, canvas: Image.Image, slot) -> bool:
    x, y, w, h = slot
    with Image.open(buf) as src:
        if src.width * src.height > MAX_IN_PX:
            return False
        if src.format == "JPEG":
            src.draft("RGB", (w, h))  # decode at reduced size: faster, less RAM
        im = ImageOps.exif_transpose(src) if src.getexif().get(0x0112, 1) != 1 else src
        if im.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
            im = im.point(lambda v: v * (1 / 256)).convert("L")  # plain convert() clips 16-bit to white
        if im.mode in ("RGBA", "LA", "P", "PA", "RGBa"):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, WHITE)
            flat.paste(im, mask=im.getchannel("A"))
            im = flat
        elif im.mode != "RGB":
            im = im.convert("RGB")
        k = min(w / im.width, h / im.height)  # fit inside the slot, never crop
        size = (max(1, round(im.width * k)), max(1, round(im.height * k)))
        if size != im.size:
            im = im.resize(size, Image.Resampling.LANCZOS, reducing_gap=2.0)  # fast pre-reduce
        canvas.paste(im, (x + (w - im.width) // 2, y + (h - im.height) // 2))
    return True


def _placeholder(canvas: Image.Image, slot) -> None:
    """Grey crossed-out tile so a failed image never leaves an unexplained white hole."""
    x, y, w, h = slot
    d = ImageDraw.Draw(canvas)
    d.rectangle((x, y, x + w - 1, y + h - 1), fill=(232, 232, 232))
    lw = max(2, min(w, h) // 80)
    d.line((x, y, x + w - 1, y + h - 1), fill=(200, 200, 200), width=lw)
    d.line((x, y + h - 1, x + w - 1, y), fill=(200, 200, 200), width=lw)


def _finish(canvas: Image.Image, p: Prefs) -> io.BytesIO:
    """Optional watermark, then JPEG. Steps quality down if the file nears 50 MB."""
    if p.wm and p.wm_text:
        size = max(18, canvas.width // 40)
        font = ImageFont.load_default(size=size)
        l, t, r, b = font.getbbox(p.wm_text)
        pad = size // 2
        x = max(0, canvas.width - (r - l) - 3 * pad)
        y = max(0, canvas.height - (b - t) - 3 * pad)
        box = (x, y, min(canvas.width, x + (r - l) + 2 * pad), min(canvas.height, y + (b - t) + 2 * pad))
        region = canvas.crop(box).convert("RGBA")
        layer = Image.new("RGBA", region.size, (0, 0, 0, 110))
        ImageDraw.Draw(layer).text((pad - l, pad - t), p.wm_text, font=font, fill=(255, 255, 255, 235))
        canvas.paste(Image.alpha_composite(region, layer).convert("RGB"), box[:2])
    for q in [p.quality] + [x for x in (80, 70, 60) if x < p.quality]:
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=q, subsampling=0 if q >= 80 else 2)
        if out.tell() <= 45 * 1024 * 1024:  # Bot API upload cap is 50 MB
            out.seek(0)
            return out
    small = canvas.resize((canvas.width * 2 // 3, canvas.height * 2 // 3), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    small.save(out, "JPEG", quality=75)
    out.seek(0)
    return out


# --------------------------------------------------------------------------- #
# Build pipeline
# --------------------------------------------------------------------------- #
async def _say(bot, chat_id: int, text: str):
    try:
        return await bot.send_message(chat_id, text)
    except TelegramError:
        return None


async def _edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text)
    except TelegramError:
        pass


async def _with_image(bot, file_id: str, fn, *args):
    """Download one image into RAM, run fn(buf, *args) in a thread, drop it. None on failure."""
    try:
        f = await bot.get_file(file_id)
        buf = io.BytesIO()
        await f.download_to_memory(out=buf)
        buf.seek(0)
        return await asyncio.to_thread(fn, buf, *args)
    except Exception:
        return None


async def build_and_send(bot, chat_id: int, uid: int, items: list, p: Prefs) -> None:
    """Runs as a task. try_launch has already reserved a slot (WAITING += 1) for it."""
    global WAITING, BUILDS_DONE
    queued, status = True, None
    try:
        status = await _say(bot, chat_id, "⏳ Queued…")
        async with BUILD_SEM:
            WAITING -= 1
            queued = False
            if status:
                await _edit(status, f"🧩 Building collage from {len(items)} images…")

            # Pass 1: aspect ratios (known from Telegram metadata; unknown ones are probed).
            ids, aspects = [], []
            for file_id, asp in items:
                if asp is None:
                    asp = await _with_image(bot, file_id, _probe)
                if asp:
                    ids.append(file_id)
                    aspects.append(asp)
            if not ids:
                if status:
                    await _edit(status, "❌ None of those images could be read. Send screenshots as photos or image files.")
                return

            width, height, slots = plan_layout(aspects, p.cols)
            canvas = Image.new("RGB", (width, height), WHITE)

            # Pass 2: download -> paste -> discard, one image at a time (one retry for flaky downloads).
            placed = 0
            for file_id, slot in zip(ids, slots):
                ok = await _with_image(bot, file_id, _paste, canvas, slot)
                if not ok:
                    ok = await _with_image(bot, file_id, _paste, canvas, slot)
                if ok:
                    placed += 1
                else:
                    _placeholder(canvas, slot)
            if placed == 0:
                if status:
                    await _edit(status, "❌ None of those images could be read. Send screenshots as photos or image files.")
                return

            full = await asyncio.to_thread(_finish, canvas, p)
            del canvas
            skipped = len(items) - placed
            caption = f"Collage • {placed} image(s)" + (f" • {skipped} skipped (grey tiles)" if skipped else "")
            await bot.send_document(chat_id, full, filename="collage.jpg", caption=caption,
                                    write_timeout=180, read_timeout=120)
            full.close()
            BUILDS_DONE += 1
            if status:
                try:
                    await status.delete()
                except TelegramError:
                    pass
    except asyncio.CancelledError:  # /ban while building
        if status:
            await _edit(status, "❌ Cancelled.")
        raise
    except Exception:
        log.exception("build failed")
        if status:
            await _edit(status, "❌ Something went wrong building that collage. Please try again.")
    finally:
        if queued:
            WAITING -= 1
        ACTIVE.discard(uid)
        BUILD_TASKS.pop(uid, None)
        gc.collect()


async def try_launch(app: Application, chat_id: int, uid: int, s: Session) -> None:
    global WAITING
    if SESSIONS.get(uid) is not s:
        return
    if len(s.items) < 2:
        await _say(app.bot, chat_id, "Send at least 2 images, then /done (or wait a few seconds).")
        return
    if uid in ACTIVE:
        await _say(app.bot, chat_id, "⏳ Your previous collage is still building. Your images are kept — send /done when it finishes.")
        return
    if WAITING >= MAX_QUEUE:  # keep the images: nothing is lost, user just retries
        await _say(app.bot, chat_id, "⚠️ The bot is busy right now. Your images are kept — send /done in a minute.")
        return
    times = BUILD_TIMES.setdefault(uid, deque())
    while times and time.time() - times[0] > 3600:
        times.popleft()
    if len(times) >= BUILDS_PER_HOUR:
        drop_session(uid)
        await _say(app.bot, chat_id, "You've hit the hourly limit. Try again a bit later.")
        return
    drop_session(uid)
    ACTIVE.add(uid)
    WAITING += 1  # reserved here (no await between the check above and this line), released in build_and_send
    times.append(time.time())
    BUILD_TASKS[uid] = app.create_task(build_and_send(app.bot, chat_id, uid, s.items, replace(prefs_for(uid))))


async def _auto_build(app: Application, chat_id: int, uid: int, s: Session) -> None:
    try:
        await asyncio.sleep(AUTO_BUILD_SEC)
    except asyncio.CancelledError:
        return
    await try_launch(app, chat_id, uid, s)


async def _react(msg) -> None:
    try:
        await msg.set_reaction("👍")
    except TelegramError:
        pass


# --------------------------------------------------------------------------- #
# User commands
# --------------------------------------------------------------------------- #
def welcome() -> str:
    return WELCOME_OVERRIDE or (
        "Welcome to TTC Collage Bot!\n\n"
        "Send your screenshots and the bot builds a collage automatically.\n\n"
        "────────────────\n"
        "How to use:\n"
        f"Send up to {MAX_IMAGES} images — collage builds after {AUTO_BUILD_SEC} seconds of inactivity.\n"
        "Type /done to build immediately.\n\n"
        "Commands:\n"
        "/layout — choose grid style\n"
        "/watermark — add or remove watermark\n"
        "/quality — set JPEG quality (1–95)\n"
        "/status — current settings\n"
        "/clear — reset queue\n"
        "/id — show your Telegram ID\n"
        "────────────────\n"
        "Delivered as a full-quality document file.\n"
        "Every image keeps its own shape — nothing is cropped or stretched. "
        "Nothing is stored; images are processed in memory and discarded."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(f"Your Telegram ID: {u.id}\n" + (f"Username: @{u.username}" if u.username else "No username set"))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(welcome() + ("\n\n🛠 Admin: /admin" if is_admin(update.effective_user) else ""))


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, uid, chat_id, app = update.message, update.effective_user.id, update.effective_chat.id, context.application
    if msg.photo:
        p = msg.photo[-1]
        file_id, size, aspect = p.file_id, p.file_size or 0, (p.width / p.height if p.height else None)
    else:
        d = msg.document
        th = d.thumbnail  # same shape as the original: enough for layout
        file_id, size = d.file_id, d.file_size or 0
        aspect = th.width / th.height if th and th.height else None
    if size > MAX_IN_MB * 1024 * 1024:
        if throttle(f"big:{uid}", 5):
            await msg.reply_text(f"That file is over {MAX_IN_MB} MB — skipped.")
        return

    s = SESSIONS.get(uid)
    if s is not None and len(s.items) >= MAX_IMAGES:  # full queue waiting for a running build
        if throttle(f"full:{uid}", 5):
            await msg.reply_text(f"Queue is full ({MAX_IMAGES}/{MAX_IMAGES}) while your previous collage builds. "
                                 "Send /done once it finishes.")
        return
    if s is None:
        s = SESSIONS[uid] = Session()
        await msg.reply_text(f"📥 Collecting images (up to {MAX_IMAGES}). Builds after {AUTO_BUILD_SEC}s of quiet, or send /done.")
    s.touched = time.monotonic()
    s.items.append((file_id, aspect))
    app.create_task(_react(msg))  # fire-and-forget so the update loop stays fast
    if s.timer:
        s.timer.cancel()
    if len(s.items) >= MAX_IMAGES:
        await try_launch(app, chat_id, uid, s)
    else:
        s.timer = app.create_task(_auto_build(app, chat_id, uid, s))


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = SESSIONS.get(uid)
    if s is None:
        await update.message.reply_text("Nothing queued. Just send your screenshots.")
        return
    if s.timer:
        s.timer.cancel()
    await try_launch(context.application, update.effective_chat.id, uid, s)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    drop_session(update.effective_user.id)
    await update.message.reply_text("🗑️ Queue cleared.")


def _layout_name(p: Prefs) -> str:
    return "Auto" if p.cols is None else f"{p.cols} per row"


def _layout_kb(p: Prefs) -> InlineKeyboardMarkup:
    def b(label: str, val: str, on: bool) -> InlineKeyboardButton:
        return InlineKeyboardButton(("✓ " if on else "") + label, callback_data=f"layout:{val}")
    return InlineKeyboardMarkup([
        [b("Auto", "auto", p.cols is None)],
        [b(str(c), str(c), p.cols == c) for c in (2, 3, 4)],
        [b(str(c), str(c), p.cols == c) for c in (5, 6, 7)],
    ])


def _set_layout(p: Prefs, v: str) -> bool:
    if v == "auto":
        p.cols = None
    elif v.isdigit() and 1 <= int(v) <= MAX_PER_ROW:
        p.cols = int(v)
    else:
        return False
    return True


async def cmd_layout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = prefs_for(update.effective_user.id)
    if context.args:
        ok = _set_layout(p, context.args[0].lower())
        await update.message.reply_text(f"✅ Layout: {_layout_name(p)}" if ok else f"Use /layout auto or /layout 1–{MAX_PER_ROW}.")
        return
    await update.message.reply_text(
        f"Grid style: {_layout_name(p)}\nAuto picks the best fit for the images you send. "
        "A number = images per row (e.g. 4 → 4 across, as many rows as needed).",
        reply_markup=_layout_kb(p),
    )


async def cb_layout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    p = prefs_for(q.from_user.id)
    if not _set_layout(p, q.data.split(":", 1)[1]):
        await q.answer()
        return
    await q.answer("Saved")
    try:
        await q.edit_message_text(f"Grid style: {_layout_name(p)}", reply_markup=_layout_kb(p))
    except TelegramError:
        pass


async def cmd_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = prefs_for(update.effective_user.id)
    arg = " ".join(context.args or []).strip()
    if arg.lower() in ("off", "remove", "no"):
        p.wm = False
    elif arg.lower() in ("on", "yes"):
        p.wm = True
    elif arg:
        text = clean_wm(arg)
        if not text:
            await update.message.reply_text("Usage: /watermark on | off | your text\n"
                                            "Only basic Latin letters, digits and punctuation are supported.")
            return
        p.wm_text, p.wm = text, True
        if text != arg.strip()[:40]:
            await update.message.reply_text("ℹ️ Some characters were removed: the built-in font only supports "
                                            "basic Latin letters, digits and punctuation.")
    else:
        p.wm = not p.wm
    await update.message.reply_text(
        f"💧 Watermark: {'ON (“' + p.wm_text + '”)' if p.wm else 'OFF'}\n/watermark on | off | any text to change it")


async def cmd_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = prefs_for(update.effective_user.id)
    if context.args and context.args[0].isdigit() and 1 <= int(context.args[0]) <= 95:
        p.quality = int(context.args[0])
        await update.message.reply_text(f"✅ JPEG quality: {p.quality}")
    else:
        await update.message.reply_text(f"JPEG quality is {p.quality}. Set it with /quality 1–95 (e.g. /quality 92).")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    p, s = prefs_for(uid), SESSIONS.get(uid)
    await update.message.reply_text(
        f"⚙️ Settings\nLayout: {_layout_name(p)}\nQuality: {p.quality}\n"
        f"Watermark: {'ON (“' + p.wm_text + '”)' if p.wm else 'OFF'}\n\n"
        f"📥 {f'{len(s.items)}/{MAX_IMAGES} images queued' if s else 'queue empty'}"
        + ("\n⏳ A collage is currently building." if uid in ACTIVE else ""))


# --------------------------------------------------------------------------- #
# Admin commands (RAM only: they reset on restart; edit env vars to persist)
# --------------------------------------------------------------------------- #
ADMIN_PANEL = (
    "Admin Panel — TTC Collage Bot\n\n"
    "────────────────\n"
    "User management:\n"
    "/allow <@username|id> — allow a user (or reply to their message)\n"
    "/ban <@username|id> — ban a user (or reply to their message)\n"
    "/unban <@username|id> — unban a user\n"
    "/banlist — list all banned users\n\n"
    "Broadcast:\n"
    "/broadcast <message> — send a message to all users seen since the last restart\n\n"
    "Welcome message:\n"
    "/setwelcome <text> — set a custom welcome message\n"
    "/resetwelcome — restore the default welcome message\n\n"
    "Stats:\n"
    "/stats — users, active queues, banned count\n\n"
    "────────────────\n"
    "Tip: reply to any user message and use /ban or /unban without an ID.\n"
    "Changes reset when the service restarts (edit ALLOWED_USERNAMES in Render to keep them)."
)


def _admin(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if is_admin(update.effective_user):  # everyone else is silently ignored
            await fn(update, context)
    return wrapper


def _after_command(msg) -> str:
    parts = (msg.text or "").split(None, 1)  # keeps the author's line breaks
    return parts[1].strip() if len(parts) > 1 else ""


def _target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(user_id|None, username|None) from the replied-to message, else from the argument."""
    r = update.message.reply_to_message
    if r:
        fo = getattr(r, "forward_origin", None)
        u = getattr(fo, "sender_user", None) if fo is not None else None
        if u is None and r.from_user and not r.from_user.is_bot:
            u = r.from_user
        if u is not None:
            return u.id, (u.username or "").lower() or None
    a = (context.args[0] if context.args else "").strip().lstrip("@")
    if a.isdigit():
        return int(a), KNOWN.get(int(a)) or None
    if re.fullmatch(r"[A-Za-z0-9_]{4,32}", a):
        return None, a.lower()
    return None, None


def _ids_for(uid, name) -> set[int]:
    return ({uid} if uid else set()) | ({i for i, n in KNOWN.items() if n == name} if name else set())


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(ADMIN_PANEL)


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, name = _target(update, context)
    if uid is None and not name:
        await update.message.reply_text("Usage: /allow <@username|id>  — or reply to the user's message.")
        return
    if name:
        BANNED_NAMES.discard(name)
        ALLOWED.add(name)
    if uid:
        ALLOWED_UIDS.add(uid)
    BANNED_IDS.difference_update(_ids_for(uid, name))
    who = " ".join(x for x in (f"@{name}" if name else "", f"(id {uid})" if uid else "") if x)
    await update.message.reply_text(f"✅ {who} allowed (until restart — add to ALLOWED_USERNAMES / ALLOWED_IDS to keep).")


async def _ban(update: Update, context: ContextTypes.DEFAULT_TYPE, ban: bool) -> None:
    cmd = "ban" if ban else "unban"
    uid, name = _target(update, context)
    if uid is None and not name:
        await update.message.reply_text(f"Usage: /{cmd} <@username|id>  — or reply to the user's message.")
        return
    ids = _ids_for(uid, name)
    if ban:
        if (name and name in ADMINS) or ids & (ADMIN_IDS | {update.effective_user.id}):
            await update.message.reply_text("Can't ban an admin.")
            return
        if name:
            BANNED_NAMES.add(name)
        BANNED_IDS.update(ids)
        for i in ids:
            drop_session(i)
            t = BUILD_TASKS.get(i)
            if t and not t.done():
                t.cancel()
        await update.message.reply_text(f"⛔ Banned {('@' + name) if name else ''} {('(id ' + str(uid) + ')') if uid else ''}".strip() + ".")
    else:
        found = (name in BANNED_NAMES) or bool(ids & BANNED_IDS)
        BANNED_NAMES.discard(name)
        BANNED_IDS.difference_update(ids)
        await update.message.reply_text("✅ Unbanned." if found else "That user isn't on the ban list.")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban(update, context, True)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban(update, context, False)


async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [f"@{n}" for n in sorted(BANNED_NAMES)]
    lines += [f"id {i}" + (f" (@{KNOWN[i]})" if i in KNOWN else "") for i in sorted(BANNED_IDS)]
    await update.message.reply_text("🚫 Banned users\n" + "\n".join(lines) if lines else "No banned users.")


async def _do_broadcast(app: Application, admin_chat: int, ids: list[int], text: str) -> None:
    ok = failed = 0
    for i in ids:
        for _ in range(2):
            try:
                await app.bot.send_message(i, f"📢 {text}")
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(retry_secs(e) + 1)
            except TelegramError:  # blocked the bot, deleted account, ...
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.06)  # well under Telegram's ~30 msg/s limit
    await _say(app.bot, admin_chat, f"📢 Broadcast done: {ok} delivered, {failed} failed.")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _after_command(update.message)[:3500]
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    me = update.effective_user.id
    ids = [i for i, n in KNOWN.items() if i != me and i not in BANNED_IDS and n not in BANNED_NAMES]
    if not ids:
        await update.message.reply_text("No other users seen since the last restart.")
        return
    if not throttle("broadcast", 30):
        await update.message.reply_text("Please wait 30 seconds between broadcasts.")
        return
    await update.message.reply_text(f"📤 Sending to {len(ids)} user(s)…")
    context.application.create_task(_do_broadcast(context.application, update.effective_chat.id, ids, text))


async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global WELCOME_OVERRIDE
    text = _after_command(update.message)[:3500]
    if not text:
        await update.message.reply_text("Usage: /setwelcome <text>")
        return
    WELCOME_OVERRIDE = text
    await update.message.reply_text("✅ Welcome message updated. Preview with /start.")


async def cmd_resetwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global WELCOME_OVERRIDE
    WELCOME_OVERRIDE = None
    await update.message.reply_text("✅ Default welcome message restored.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    up = int(time.time() - START)
    await update.message.reply_text(
        "📊 Stats\n"
        f"Users seen (since restart): {len(KNOWN)}\n"
        f"Active queues: {len(SESSIONS)} ({sum(len(x.items) for x in SESSIONS.values())} images waiting)\n"
        f"Building now: {len(ACTIVE)}   Waiting in line: {WAITING}\n"
        f"Collages built: {BUILDS_DONE}\n"
        f"Banned: {len(BANNED_NAMES)} username(s), {len(BANNED_IDS)} id(s)\n"
        f"Uptime: {up // 3600}h {up % 3600 // 60}m")


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("done", "Build the collage now"),
        BotCommand("layout", "Choose grid style"),
        BotCommand("watermark", "Add or remove watermark"),
        BotCommand("quality", "Set JPEG quality (1-95)"),
        BotCommand("status", "Current settings"),
        BotCommand("clear", "Reset queue"),
        BotCommand("id", "Show your Telegram ID"),
        BotCommand("start", "Help"),
    ])


async def on_error(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler error: %s", context.error)  # message only: never log payloads


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not set (set the env var or fill it in config.py)")
    if not (ADMINS or ALLOWED or ADMIN_UIDS or ALLOWED_UIDS):
        raise SystemExit("Set ADMIN_USERNAMES/ADMIN_IDS and/or ALLOWED_USERNAMES/ALLOWED_IDS "
                         "(env vars or config.py)")

    app = (Application.builder().token(TOKEN).post_init(post_init)
           .read_timeout(30).write_timeout(60).connect_timeout(15).pool_timeout(30)
           .connection_pool_size(8).build())

    app.add_handler(TypeHandler(Update, gatekeeper), group=-1)  # runs first, blocks strangers
    for names, fn in ((["start", "help"], cmd_start), ("done", cmd_done), ("clear", cmd_clear),
                      ("layout", cmd_layout), ("watermark", cmd_watermark), ("quality", cmd_quality),
                      ("status", cmd_status), ("id", cmd_id)):
        app.add_handler(CommandHandler(names, fn))
    app.add_handler(CallbackQueryHandler(cb_layout, pattern=r"^layout:"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    for names, fn in ((["admin"], cmd_admin), ("allow", cmd_allow), ("ban", cmd_ban), ("unban", cmd_unban),
                      ("banlist", cmd_banlist), ("broadcast", cmd_broadcast), ("setwelcome", cmd_setwelcome),
                      ("resetwelcome", cmd_resetwelcome), ("stats", cmd_stats)):
        app.add_handler(CommandHandler(names, _admin(fn)))
    app.add_error_handler(on_error)

    updates = ["message", "callback_query"]
    if BASE_URL:
        if not WEBHOOK_SECRET:
            raise SystemExit("WEBHOOK_SECRET is not set (set the env var or fill it in config.py)")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):  # Telegram rejects anything else
            raise SystemExit("WEBHOOK_SECRET may only contain A-Z a-z 0-9 _ - (1-256 chars)")
        path = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]  # unguessable path
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=path, webhook_url=f"{BASE_URL}/{path}",
                        secret_token=WEBHOOK_SECRET, allowed_updates=updates, drop_pending_updates=False)
    else:
        log.info("RENDER_EXTERNAL_URL not set -> polling (local dev)")
        app.run_polling(allowed_updates=updates)


if __name__ == "__main__":
    main()
