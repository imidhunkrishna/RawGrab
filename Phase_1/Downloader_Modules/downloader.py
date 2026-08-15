"""
downloader.py  —  AMTCE Harvest Module
Architecturally-robust video downloader with atomic transactions,
indexed duplicate lookups, and multi-strategy auth fallback.
"""

import os
import sys

# Force UTF-8 stream encoding on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import shutil
import re
import json
import glob
import time
import hashlib
import logging
import argparse
import subprocess
import importlib
import threading
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Colorful Emoji Logger
# Must be set up before any other import so every sub-module that calls
# logging.getLogger() inherits the handler.
# ---------------------------------------------------------------------------

ANSI = {
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
    "red":     "\033[91m",
    "green":   "\033[92m",
    "yellow":  "\033[93m",
    "blue":    "\033[94m",
    "magenta": "\033[95m",
    "cyan":    "\033[96m",
    "white":   "\033[97m",
    "grey":    "\033[90m",
}

_LEVEL_STYLE: Dict[int, tuple] = {
    logging.DEBUG:    ("grey",    "🔍"),
    logging.INFO:     ("cyan",    "ℹ️ "),
    logging.WARNING:  ("yellow",  "⚠️ "),
    logging.ERROR:    ("red",     "❌"),
    logging.CRITICAL: ("magenta", "💀"),
}


class _ColourFormatter(logging.Formatter):
    """
    Renders log records with ANSI colour + emoji per level.
    Falls back gracefully when the stream is not a TTY.
    """

    _FMT = (
        "{dim}{time}{reset}  "
        "{colour}{bold}{emoji} {level:<8}{reset}  "
        "{colour}{message}{reset}"
        "{dim}{location}{reset}"
    )

    def __init__(self, use_colour: bool = True) -> None:
        super().__init__()
        self._use_colour = use_colour

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        colour_key, emoji = _LEVEL_STYLE.get(record.levelno, ("white", "•"))

        if self._use_colour:
            c     = ANSI[colour_key]
            bold  = ANSI["bold"]
            dim   = ANSI["dim"]
            reset = ANSI["reset"]
        else:
            c = bold = dim = reset = ""

        time_str = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        location = (
            f"  ({record.filename}:{record.lineno})"
            if record.levelno >= logging.WARNING
            else ""
        )

        return self._FMT.format(
            dim=dim, reset=reset, bold=bold, colour=c,
            time=time_str,
            emoji=emoji,
            level=record.levelname,
            message=record.getMessage(),
            location=location,
        )


def _setup_logging(level: int = logging.DEBUG) -> None:
    """Install the colourful handler on the root logger (idempotent)."""
    root = logging.getLogger()
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColourFormatter(use_colour=True))
    handler.setLevel(level)
    root.setLevel(level)
    root.addHandler(handler)


_setup_logging()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional heavy deps
# ---------------------------------------------------------------------------
try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None  # type: ignore
    np  = None  # type: ignore

try:
    import yt_dlp
    from yt_dlp.utils import DownloadError, ExtractorError
except ImportError as exc:
    raise SystemExit("yt-dlp is required: pip install yt-dlp") from exc


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

DOWNLOAD_DIR = os.path.join(_REPO_ROOT, "downloads")
CACHE_DIR    = os.path.join(_REPO_ROOT, "cache")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Smart Reuse Engine
# ---------------------------------------------------------------------------
try:
    from smart_reuse_engine import check_smart_reuse, register_video
    SMART_REUSE_AVAILABLE = True
    logger.info("🧠 Smart Reuse Engine loaded successfully from root")
except ImportError:
    try:
        from intelligence.smart_reuse_engine import check_smart_reuse, register_video
        SMART_REUSE_AVAILABLE = True
        logger.info("🧠 Smart Reuse Engine loaded successfully")
    except ImportError:
        try:
            from Intelligence_Modules.smart_reuse_engine import check_smart_reuse, register_video
            SMART_REUSE_AVAILABLE = True
            logger.info("🧠 Smart Reuse Engine loaded successfully")
        except ImportError as s_err:
            SMART_REUSE_AVAILABLE = False
            logger.warning("🧠 Smart Reuse Engine unavailable (%s) — reuse detection disabled", s_err)

_OLD_REEL_INDEX = os.path.join(CACHE_DIR, "reel_index.json")
if os.path.exists(_OLD_REEL_INDEX):
    try:
        os.remove(_OLD_REEL_INDEX)
        logger.info("🗑️  Removed legacy reel_index.json")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------
DOWNLOAD_RETRY_DELAY: int  = int(os.getenv("DOWNLOAD_RETRY_DELAY", "2"))
RATE_LIMIT_WAIT:      int  = int(os.getenv("RATE_LIMIT_WAIT",      "8"))
DEBUG_JSON:           bool = os.getenv("DEBUG_JSON", "0") == "1"

_COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")
_IG_USERNAME  = os.getenv("IG_USERNAME", "")
_IG_PASSWORD  = os.getenv("IG_PASSWORD", "")

APIFY_GENERIC_FALLBACK: bool = os.getenv("APIFY_GENERIC_FALLBACK", "no").lower() in ("yes", "true", "1")

_UPDATE_LOCK      = threading.Lock()
_UPDATE_COOLDOWN  = 12 * 3600
_LAST_UPDATE_TIME: float = 0.0

_UPDATE_TRIGGER_KEYWORDS = frozenset([
    "no suitable extractor", "extractor", "unsupported url",
    "sign in", "login", "401", "unavailable", "dpapi",
    "rate-limit", "cookies", "not available", "this version", "upgrade",
])

_AUTH_STRATEGIES = [
    "no_auth",
    "cookies_file",
    "username_password",
    "browser_firefox",
    "browser_edge",
    "browser_brave",
    "browser_chrome",
    "browser_opera",
]

_PLATFORM_ICON = {
    "instagram": "📸",
    "facebook":  "👥",
    "tiktok":    "🎵",
    "youtube":   "▶️ ",
    "unknown":   "🌐",
}

_STRATEGY_ICON = {
    "no_auth":           "🔓",
    "cookies_file":      "🍪",
    "username_password": "🔑",
    "browser_firefox":   "🦊",
    "browser_edge":      "🌀",
    "browser_brave":     "🦁",
    "browser_chrome":    "🌈",
    "browser_opera":     "🎭",
}


# ---------------------------------------------------------------------------
# yt-dlp auto-updater
# ---------------------------------------------------------------------------

def _update_yt_dlp() -> bool:
    """Upgrade yt-dlp via pip at most once per _UPDATE_COOLDOWN. Thread-safe."""
    global _LAST_UPDATE_TIME

    with _UPDATE_LOCK:
        now = time.time()
        if now - _LAST_UPDATE_TIME < _UPDATE_COOLDOWN:
            logger.info("⏳ yt-dlp update cooldown active — skipping")
            return False

        logger.info("🚀 Triggering automatic yt-dlp update …")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                check=True, capture_output=True,
            )
            importlib.reload(yt_dlp)
            logger.info("✅ yt-dlp updated → v%s", yt_dlp.version.__version__)
            _LAST_UPDATE_TIME = now
            return True
        except Exception as exc:
            logger.error("💥 yt-dlp update failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Strip unsafe characters and normalise whitespace."""
    clean = re.sub(r"[^\w\s-]", "", name)
    return clean.replace(" ", "_").strip()


def _calculate_file_hash(path: str) -> str:
    """SHA-1 of entire file, truncated to 8 hex chars."""
    try:
        sha1 = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65_536), b""):
                sha1.update(chunk)
        return sha1.hexdigest()[:8]
    except OSError:
        return ""


def _calculate_content_fingerprint(video_path: str) -> str:
    """
    Perceptual hash of four evenly-spaced frames.
    Falls back to SHA-1 of whole file when cv2 is unavailable.
    """
    if cv2 is None or np is None:
        logger.debug("🔬 cv2 unavailable — using file hash for fingerprint")
        return _calculate_file_hash(video_path)

    try:
        cap         = cv2.VideoCapture(video_path)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        checkpoints = [0, int(frame_count * 0.25), int(frame_count * 0.50), int(frame_count * 0.75)]
        hashes: list = []

        for pos in checkpoints:
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            ret, frame = cap.read()
            if not ret:
                continue
            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (32, 32))
            phash = (small > small.mean()).astype(np.uint8)
            hashes.append(hashlib.sha1(phash.tobytes()).hexdigest())

        cap.release()
        return hashlib.sha1("".join(hashes).encode()).hexdigest()

    except Exception as exc:
        logger.warning("🔬 Perceptual hash failed (%s) — falling back to file hash", exc)
        return _calculate_file_hash(video_path)


def _get_video_duration(video_path: str) -> Optional[float]:
    """Return duration in seconds via cv2 or ffprobe."""
    try:
        if cv2 is not None:
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                fps    = cap.get(cv2.CAP_PROP_FPS)
                cap.release()
                if fps > 0:
                    return float(frames / fps)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception as exc:
        logger.warning("⏱️  Duration check failed: %s", exc)
    return None


def _atomic_rename(src: str, dst: str) -> bool:
    """
    Rename src -> dst only when dst does not exist.
    os.rename is atomic on POSIX; on Windows raises FileExistsError which we catch.
    """
    try:
        if os.path.exists(dst):
            return False
        os.rename(src, dst)
        return True
    except OSError:
        return False


def _detect_platform(url: str) -> str:
    lower = url.lower()
    if "instagram.com" in lower:                         return "instagram"
    if "facebook.com"  in lower or "fb.watch" in lower: return "facebook"
    if "tiktok.com"    in lower:                         return "tiktok"
    if "youtube.com"   in lower or "youtu.be" in lower: return "youtube"
    return "unknown"


def _extract_url_id(url: str) -> str:
    """Extract a platform-specific content ID from the URL, or empty string."""
    if "instagram.com" in url:
        m = re.search(r"/(?:reel|p)/([A-Za-z0-9_-]+)", url)
        if m: return m.group(1)
        m = re.search(r"/stories/[^/]+/(\d+)", url)
        if m: return m.group(1)
    if "facebook.com" in url or "fb.watch" in url:
        m = re.search(r"/(?:reel|videos)/(\d+)", url)
        if m: return m.group(1)
    if "tiktok.com" in url:
        m = re.search(r"/video/(\d+)", url)
        if m: return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Persistent Download Index
# ---------------------------------------------------------------------------

import sqlite3
from contextlib import contextmanager

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

_OLD_DB_PATH = os.path.join(DOWNLOAD_DIR, "index.db")
_DB_PATH = os.path.join(DATA_DIR, "index.db")

# Auto-migrate legacy index.db from downloads/ to data/ to prevent accidental sweep deletion
if os.path.exists(_OLD_DB_PATH) and not os.path.exists(_DB_PATH):
    try:
        shutil.move(_OLD_DB_PATH, _DB_PATH)
        logger.info("📦 Migrated index.db out of volatile downloads/ into persistent data/index.db")
    except Exception as _mig_err:
        logger.warning(f"Failed to migrate index.db: {_mig_err}")

@contextmanager
def _get_db():
    conn = sqlite3.connect(_DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent readers, one writer
    conn.execute("PRAGMA synchronous=NORMAL") # safe + fast
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

class DownloadIndex:
    
    @classmethod
    def _ensure_schema(cls):
        with _get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    url_id       TEXT,
                    content_hash TEXT,
                    file_path    TEXT NOT NULL,
                    created_at   REAL,
                    PRIMARY KEY (url_id, content_hash)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_url_id ON downloads(url_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_hash ON downloads(content_hash)")

    @classmethod
    def register(cls, video_path: str, meta: Dict) -> None:
        cls._ensure_schema()
        url_id = str(meta.get("id") or meta.get("url_id", "") or "")
        c_hash = str(meta.get("content_hash", "") or "")
        with _get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO downloads (url_id, content_hash, file_path, created_at) VALUES (?,?,?,?)",
                (url_id or None, c_hash or None, video_path, time.time())
            )

    @classmethod
    def find_by_id(cls, url_id: str) -> Optional[str]:
        if not url_id: return None
        cls._ensure_schema()
        with _get_db() as db:
            row = db.execute(
                "SELECT file_path FROM downloads WHERE url_id=? LIMIT 1", (str(url_id),)
            ).fetchone()
        if row and os.path.exists(row[0]):
            return row[0]
        return None

    @classmethod
    def find_by_hash(cls, target_hash: str) -> Optional[str]:
        if not target_hash: return None
        cls._ensure_schema()
        with _get_db() as db:
            row = db.execute(
                "SELECT file_path FROM downloads WHERE content_hash=? LIMIT 1", (target_hash,)
            ).fetchone()
        if row and os.path.exists(row[0]):
            return row[0]
        return None

def migrate_json_to_sqlite():
    if not os.path.exists(os.path.join(DOWNLOAD_DIR, "index.json")):
        return
    with open(os.path.join(DOWNLOAD_DIR, "index.json"), "r", encoding="utf-8") as f:
        try:
            old = json.load(f)
        except json.JSONDecodeError:
            old = {}
            
    DownloadIndex._ensure_schema()
    with _get_db() as db:
        for url_id, path in old.get("ids", {}).items():
            db.execute("INSERT OR IGNORE INTO downloads (url_id, file_path, created_at) VALUES (?,?,?)",
                       (url_id, path, time.time()))
        for c_hash, path in old.get("hashes", {}).items():
            db.execute("INSERT OR IGNORE INTO downloads (content_hash, file_path, created_at) VALUES (?,?,?)",
                       (c_hash, path, time.time()))
    logger.info("✅ Migrated index.json → index.db")

# Run migration on load
migrate_json_to_sqlite()


# ---------------------------------------------------------------------------
# yt-dlp strategy builder
# ---------------------------------------------------------------------------

def _build_strategy_opts(base_opts: Dict, strategy: str) -> Optional[Dict]:
    """Return augmented opts for strategy, or None if strategy is unusable."""
    opts = base_opts.copy()

    if strategy == "no_auth":
        return opts

    if strategy == "cookies_file":
        if os.path.exists(_COOKIES_FILE) and os.path.getsize(_COOKIES_FILE) > 100:
            opts["cookiefile"] = _COOKIES_FILE
            return opts
        return None

    if strategy == "username_password":
        if _IG_USERNAME and _IG_PASSWORD:
            opts["username"] = _IG_USERNAME
            opts["password"] = _IG_PASSWORD
            return opts
        return None

    if strategy.startswith("browser_"):
        opts["cookiesfrombrowser"] = (strategy.split("_", 1)[1],)
        return opts

    logger.warning("❓ Unknown strategy '%s' — skipping", strategy)
    return None


# ---------------------------------------------------------------------------
# Direct CDN Downloader
# ---------------------------------------------------------------------------

def _download_cdn_direct(url: str, output_dir: str, filename_base: str) -> Optional[str]:
    """
    Downloads a direct CDN URL (e.g. from Apify) and saves it to output_dir.
    Returns the path to the downloaded file, or None on failure.
    """
    import urllib.request
    
    ext = "mp4"
    logger.info("🌐 Downloading direct CDN link to %s ...", output_dir)
    
    temp_path = os.path.join(output_dir, f"{filename_base}.tmp_cdn")
    try:
        try:
            import requests
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/115.0.0.0 Safari/537.36"
                )
            }
            with requests.get(url, headers=headers, stream=True, timeout=30) as r:
                r.raise_for_status()
                content_type = r.headers.get("content-type", "")
                if "video/mp4" in content_type:
                    ext = "mp4"
                elif "video/quicktime" in content_type:
                    ext = "mov"
                
                with open(temp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            
        except ImportError:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/115.0.0.0 Safari/537.36"
                    )
                }
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                content_type = response.info().get_content_type()
                if "video/mp4" in content_type:
                    ext = "mp4"
                elif "video/quicktime" in content_type:
                    ext = "mov"
                
                with open(temp_path, "wb") as f:
                    f.write(response.read())
                    
        final_path = os.path.join(output_dir, f"{filename_base}.{ext}")
        if os.path.exists(final_path):
            try: os.remove(final_path)
            except OSError: pass
        os.rename(temp_path, final_path)
        return final_path
        
    except Exception as exc:
        logger.error("💥 Direct CDN download failed: %s", exc)
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError: pass
        return None


# ---------------------------------------------------------------------------
# Core downloader
# ---------------------------------------------------------------------------

def download_video(
    url:            str,
    custom_title:   Optional[str] = None,
    force_filename: Optional[str] = None,
    destination_dir: Optional[str] = None,
) -> Tuple[Optional[str], bool]:
    """
    Download *url* and return ``(local_path, is_cached)``.
    Always returns a 2-tuple; ``(None, False)`` signals total failure.

    Phases
    ------
    1. ID extraction  → O(1) index duplicate check
    2. Multi-strategy yt-dlp download with auto-update on extractor failure
    3. Partial-file rescue (post-loop, Windows file-lock workaround)
    4. Atomic commit: fingerprint → hash dedup → rename → index
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    platform  = _detect_platform(url)
    url_id    = _extract_url_id(url)
    p_icon    = _PLATFORM_ICON.get(platform, "🌐")

    logger.info("━" * 58)
    logger.info("%s  Download request  [%s]", p_icon, platform.upper())
    logger.info("🔗  %s", url)
    if url_id:
        logger.info("🪪  Content ID: %s", url_id)
    logger.info("━" * 58)

    # ── Phase 1 · Duplicate detection ────────────────────────────────────
    try:
        try:
            from .metadata_pool_manager import metadata_pool_manager
        except ImportError:
            try:
                from Phase_1.Downloader_Modules.metadata_pool_manager import metadata_pool_manager
            except ImportError:
                from Tools.Downloader_Modules.metadata_pool_manager import metadata_pool_manager
        manga = os.environ.get("MANGA", "")
        ch = os.environ.get("CHAPTER", "")
        lang = os.environ.get("LANGUAGE", "")
        cached_rec = metadata_pool_manager.find_cached_clip(
            content_id=url_id,
            url=url,
            manga_name=manga if manga else None,
            chapter_or_vol=ch if ch else None,
            language=lang if lang else None
        )
        if cached_rec and cached_rec.get("video_path") and os.path.exists(cached_rec["video_path"]):
            logger.info("♻️  [METADATA POOL HIT 🎯] Reusing existing downloaded clip → %s", os.path.basename(cached_rec["video_path"]))
            return cached_rec["video_path"], True
    except Exception as _pool_err:
        logger.debug("Metadata pool check notice: %s", _pool_err)

    if url_id:
        existing = DownloadIndex.find_by_id(url_id)
        if existing and os.path.exists(existing):
            logger.info("♻️  Index hit (by ID) → %s", os.path.basename(existing))
            return existing, True

        if SMART_REUSE_AVAILABLE:
            existing, _ = check_smart_reuse("", reel_id=url_id)
            if existing:
                logger.info("♻️  Smart-reuse hit → %s", os.path.basename(existing))
                return existing, True

    # ── Phase 2 · Download ───────────────────────────────────────────────
    target_dir = destination_dir or DOWNLOAD_DIR
    os.makedirs(target_dir, exist_ok=True)
    temp_base = f"dl_{timestamp}_{url_id or 'unknown'}"
    temp_tmpl = os.path.abspath(os.path.join(target_dir, f"{temp_base}.%(ext)s"))

    base_ydl_opts: Dict = {
        "outtmpl":          temp_tmpl,
        "format": (
            "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[height<=480]+bestaudio"
            "/best[height<=480]"
            "/bestvideo[height<=720]+bestaudio"
            "/best[ext=mp4]/best"
        ),
        "noplaylist":        True,
        "quiet":             True,
        "no_warnings":       True,
        "ignoreerrors":      True,
        "restrictfilenames": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"]
            }
        },
    }

    success          = False
    downloaded_path: Optional[str] = None
    info_dict:       Dict          = {}

    logger.info("🎯  Trying %d auth strategies …", len(_AUTH_STRATEGIES))

    for strategy in _AUTH_STRATEGIES:
        if success:
            break

        opts = _build_strategy_opts(base_ydl_opts, strategy)
        if opts is None:
            logger.debug("⏭️  Strategy '%s' not available — skipping", strategy)
            continue

        s_icon = _STRATEGY_ICON.get(strategy, "🔧")
        logger.info("%s  Attempting strategy: %s", s_icon, strategy)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    info_dict  = info
                    search_dir = destination_dir or DOWNLOAD_DIR
                    candidates = [
                        c for c in glob.glob(os.path.join(search_dir, f"{temp_base}.*"))
                        if not c.endswith((".part", ".ytdl", ".json"))
                    ]
                    if candidates:
                        downloaded_path = candidates[0]
                        success = True
                        logger.info("📦  Downloaded: %s", os.path.basename(downloaded_path))

        except (DownloadError, ExtractorError) as exc:
            err = str(exc).lower()
            logger.warning("🚨  yt-dlp error [%s]: %.120s …", strategy, err)

            if any(kw in err for kw in _UPDATE_TRIGGER_KEYWORDS):
                logger.warning("🔄  Extractor/site breakage — triggering yt-dlp update")
                _update_yt_dlp()
            elif "geo" in err or "country" in err:
                logger.error("🌍  Geo-blocked: content unavailable in this region")

            time.sleep(DOWNLOAD_RETRY_DELAY)

        except Exception as exc:
            err = str(exc).lower()
            logger.warning("💢  Unexpected error [%s]: %.100s", strategy, err)
            if any(kw in err for kw in ("rate-limit", "cookies", "login", "unavailable")):
                _update_yt_dlp()
            time.sleep(DOWNLOAD_RETRY_DELAY)

    # ── Phase 3 · Partial-file rescue (post-loop) ────────────────────────
    if not success:
        parts = glob.glob(os.path.join(DOWNLOAD_DIR, f"{temp_base}.*.part"))
        if parts:
            part_file    = parts[0]
            rescued_name = part_file[:-5]   # strip ".part"
            logger.info("🚑  Rescue attempt: %s", os.path.basename(part_file))

            for attempt in range(1, 6):
                try:
                    if os.path.exists(rescued_name):
                        os.remove(rescued_name)
                    os.rename(part_file, rescued_name)
                    downloaded_path = rescued_name
                    success         = True
                    logger.info("✅  Rescue succeeded (attempt %d/5)", attempt)
                    break
                except OSError as exc:
                    logger.warning("🔒  Rescue blocked (attempt %d/5): %s", attempt, exc)
                    time.sleep(2)

            if not success:
                logger.error("💀  Rescue failed — could not acquire file lock")

    # ── Phase 3.5 · Apify Last-Resort Fallback ──────────────────────────────
    apify_token = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_TOKEN")
    if not success and apify_token:
        logger.warning("💰 [TIER 9] All yt-dlp strategies exhausted → Apify fallback")
        try:
            try:
                from .apify_downloader import apify_get_video_url_any, apify_get_video_url
            except ImportError:
                try:
                    from Phase_1.Downloader_Modules.apify_downloader import apify_get_video_url_any, apify_get_video_url
                except ImportError:
                    from Tools.Downloader_Modules.apify_downloader import apify_get_video_url_any, apify_get_video_url

            cdn_url = None
            if platform == "instagram" and (url.startswith("https://www.instagram.com/") or url.startswith("https://instagram.com/")):
                cdn_url = apify_get_video_url(url)
            if not cdn_url:
                cdn_url = apify_get_video_url_any(url)

            if cdn_url:
                logger.info("Apify returned direct video CDN URL -- downloading directly")
                timestamp_now = datetime.now().strftime("%Y%m%d_%H%M%S")
                base = f"apify_{timestamp_now}_{url_id or 'cdn'}"
                cdn_path = _download_cdn_direct(cdn_url, DOWNLOAD_DIR, base)
                if cdn_path:
                    downloaded_path = cdn_path
                    success = True
                    logger.info("CDN direct download succeeded: %s", os.path.basename(cdn_path))
                else:
                    logger.error("CDN direct download also failed for: %s", url)
            else:
                logger.error("Apify returned no direct video URL for: %s", url)
        except Exception as exc:
            logger.error("💥 Apify Tier 9 exception: %s", exc)

    # ─────────────────────────────────────────────────────────────────────────

    # If yt-dlp failed and Apify also failed/skipped → total failure
    if not success:
        logger.error("All download strategies exhausted")
        return None, False


    # ── Phase 4 · Atomic commit ───────────────────────────────────────────
    try:
        logger.info("🔬  Computing content fingerprint …")
        content_hash = _calculate_content_fingerprint(downloaded_path)
        file_size    = os.path.getsize(downloaded_path)
        logger.info("🔏  Hash: %s  |  Size: %.1f MB", content_hash[:12], file_size / 1_048_576)

        # Hash-level dedup
        existing_hash = DownloadIndex.find_by_hash(content_hash)
        if existing_hash:
            logger.info("♻️  Content hash match → %s", os.path.basename(existing_hash))
            try: os.remove(downloaded_path)
            except OSError: pass
            if custom_title:
                existing_hash = _maybe_rename_to_title(existing_hash, custom_title)
            return existing_hash, True

        # Build metadata
        duration = info_dict.get("duration") or _get_video_duration(downloaded_path)
        meta: Dict = {
            "id":                 url_id,
            "url":                url,
            "title":              info_dict.get("title", "video"),
            "uploader":           info_dict.get("uploader", "unknown"),
            "content_hash":       content_hash,
            "file_size":          file_size,
            "duration":           duration,
            "download_timestamp": time.time(),
            "source_platform":    platform,
        }

        # Resolve final path
        ext        = downloaded_path.rsplit(".", 1)[-1]
        final_path = _resolve_final_path(
            downloaded_path, ext, meta, custom_title, force_filename, content_hash, target_dir=target_dir
        )

        if final_path is None:
            logger.error("💥  Could not resolve a unique filename — aborting")
            try: os.remove(downloaded_path)
            except OSError: pass
            return None, False

        # Metadata sidecar
        json_path = os.path.splitext(final_path)[0] + ".json"
        try:
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
            logger.debug("📝  Metadata saved → %s", os.path.basename(json_path))
        except Exception as exc:
            logger.error("📝  Failed to write metadata sidecar: %s", exc)

        # Smart Reuse registration
        if SMART_REUSE_AVAILABLE:
            register_video(final_path, url_id)
            logger.debug("🧠  Registered in Smart Reuse Engine")

        # Index commit
        DownloadIndex.register(final_path, meta)

        dur_str = f"  ⏱️  {duration:.0f}s" if duration else ""
        logger.info("━" * 58)
        logger.info("🎉  COMMITTED: %s%s", os.path.basename(final_path), dur_str)
        logger.info("━" * 58)

        # ── Audio extraction ──────────────────────────────────────────────
        # Extract audio to Original_audio/ immediately after download so the
        # full continuous source audio is available for beat analysis and scene
        # mapping. Non-fatal — source video audio acts as fallback if this fails.
        #
        # Set SKIP_AUDIO_EXTRACT=true (in-process os.environ) to bypass this
        # entirely — used by the actress auto-scheduler to avoid wasted CPU/disk
        # on raw source reels that aren't being immediately compiled.
        if os.environ.get("SKIP_AUDIO_EXTRACT", "").lower() == "true":
            logger.info("🔇  Audio extraction skipped (SKIP_AUDIO_EXTRACT=true)")
        else:
            try:
                try:
                    from Audio_Modules.audio_extractor import extract_audio
                except ImportError:
                    from Tools.Audio_Modules.audio_extractor import extract_audio
                _audio_dir = os.path.join(DOWNLOAD_DIR, "Original_audio")
                os.makedirs(_audio_dir, exist_ok=True)
                _stem = os.path.splitext(os.path.basename(final_path))[0]
                _wav_out = os.path.join(_audio_dir, f"{_stem}.wav")
                
                if extract_audio(final_path, _wav_out):
                    logger.info("🎵  Audio extracted → %s", os.path.basename(_wav_out))

                    # ── Audio Pool Integration ────────────────────────────────
                    try:
                        try:
                            from Audio_Modules.audio_pool_manager import pool_manager
                            from Audio_Modules.beat_engine import BeatEngine
                        except ImportError:
                            from Tools.Audio_Modules.audio_pool_manager import pool_manager
                            from Tools.Audio_Modules.beat_engine import BeatEngine

                        be = BeatEngine()
                        analysis = be.analyze_beats_with_drops(_wav_out)
                        beats = analysis.get("beats", [])

                        # Calculate BPM
                        bpm = 0.0
                        if len(beats) >= 4:
                            intervals = [beats[i+1]["time"] - beats[i]["time"] for i in range(len(beats)-1)]
                            avg_interval = sum(intervals) / len(intervals)
                            if avg_interval > 0:
                                bpm = round(60.0 / avg_interval, 1)

                        # Calculate Average Energy
                        avg_energy = 0.0
                        if beats:
                            avg_energy = sum(b["energy"] for b in beats) / len(beats)

                        # ── MUSIC GATE v2: Skip non-music audio from BGM pool ──
                        _dl_drops = analysis.get("drops", [])
                        _dl_beat_times = [b["time"] for b in beats]

                        # Condition A: pure speech / silence
                        _dl_cond_a = (
                            len(beats) < 20
                            and len(_dl_drops) == 0
                            and avg_energy < 0.20
                        )

                        # Condition B: irregular beat rhythm → crowd/ambient noise
                        _dl_cond_b = False
                        _dl_cv = 0.0
                        _dl_drop_rate = len(_dl_drops) / max(1, len(beats))
                        _dl_cond_c = False
                        if len(_dl_beat_times) >= 4:
                            _dl_ivals = [
                                _dl_beat_times[i + 1] - _dl_beat_times[i]
                                for i in range(len(_dl_beat_times) - 1)
                            ]
                            _dl_mean_iv = sum(_dl_ivals) / len(_dl_ivals)
                            if _dl_mean_iv > 0:
                                _dl_std_iv = (
                                    sum((x - _dl_mean_iv) ** 2 for x in _dl_ivals) / len(_dl_ivals)
                                ) ** 0.5
                                _dl_cv = _dl_std_iv / _dl_mean_iv
                                _dl_drop_rate = len(_dl_drops) / max(1, len(beats))
                                _dl_cond_b = (
                                    _dl_cv > 0.40
                                    and _dl_drop_rate > 0.15
                                    and len(beats) < 60
                                )
                                _dl_cond_c = (
                                    len(_dl_drops) == 0
                                    and analysis.get("tempo", 999) < 75.0
                                    and len(beats) >= 20
                                    and _dl_cv > 0.40
                                )

                                # Condition D: mid-energy crowd/polluted/overcrowded audio
                                # Targets: applause, stadium crowd, event chatter, venue noise.
                                # These pass Cond A (energy > 0.20) and may pass B/C (cv < 0.40
                                # due to pseudo-rhythmic clapping) but have ZERO musical drops.
                                _dl_cond_d = (
                                    len(_dl_drops) == 0          # no musical climax / drop
                                    and len(beats) >= 30         # enough signal to be confident
                                    and 0.20 <= avg_energy <= 0.55  # mid-energy band (not silence, not clean music)
                                    and _dl_cv > 0.30            # irregular beat grid (real music has cv < 0.25)
                                )

                        _dl_is_non_music = _dl_cond_a or _dl_cond_b or _dl_cond_c or _dl_cond_d

                        if _dl_is_non_music:
                            _dl_reason = (
                                f"crowd/ambient noise (cv={_dl_cv:.2f}, drop_rate={_dl_drop_rate:.2f})"
                                if _dl_cond_b
                                else (
                                    f"crowded/polluted audio (beats={len(beats)}, drops=0, energy={avg_energy:.3f}, cv={_dl_cv:.2f})"
                                    if _dl_cond_d
                                    else (
                                        f"slow ambient/speech (bpm={analysis.get('tempo',0):.1f}, drops=0, cv={_dl_cv:.2f})"
                                        if _dl_cond_c
                                        else f"speech/silence (beats={len(beats)}, drops=0, energy={avg_energy:.3f})"
                                    )
                                )
                            )
                            logger.info(
                                "🗣️  [MUSIC_GATE] Non-music audio — skipping BGM pool: %s | %s",
                                os.path.basename(_wav_out), _dl_reason,
                            )
                            try:
                                pool_manager._set_file_metadata(
                                    os.path.basename(_wav_out),
                                    {
                                        "is_speech_only": True,
                                        "usage_count": 0, "last_used": 0,
                                        "bpm": 0.0, "energy": round(avg_energy, 3),
                                        "created_at": time.time(),
                                        "beat_data_path": None, "drop_times": [],
                                        "sample_rate": 44100, "audio_hash": "",
                                        "version": pool_manager.CURRENT_VERSION,
                                    },
                                )
                                pool_manager._save_metadata()
                            except Exception:
                                pass
                        else:
                            pool_manager.process_new_audio(_wav_out, bpm, avg_energy, analysis)

                    except Exception as _pe:
                        logger.warning("⚠️  Audio pool processing failed (non-fatal): %s", _pe)

            except Exception as _ae:
                logger.debug("🎵  Audio extraction skipped (non-fatal): %s", _ae)


        # ── Record in Original_audio/metadata_pool.json ──────────────────────
        try:
            try:
                from .metadata_pool_manager import metadata_pool_manager
            except ImportError:
                try:
                    from Phase_1.Downloader_Modules.metadata_pool_manager import metadata_pool_manager
                except ImportError:
                    from Tools.Downloader_Modules.metadata_pool_manager import metadata_pool_manager
            content_id = meta.get("id") or meta.get("hash") or content_hash
            channel_name = meta.get("channel") or meta.get("uploader") or meta.get("creator") or "unknown"
            manga_name = os.environ.get("MANGA", "General")
            chapter_or_vol = os.environ.get("CHAPTER", "General")
            language = os.environ.get("LANGUAGE", "en")
            title = meta.get("title") or os.path.basename(final_path)
            url = meta.get("webpage_url") or meta.get("url") or f"https://www.youtube.com/watch?v={content_id}"
            
            _audio_dir = os.path.join(DOWNLOAD_DIR, "Original_audio")
            _stem = os.path.splitext(os.path.basename(final_path))[0]
            _wav_out = os.path.join(_audio_dir, f"{_stem}.wav")
            audio_file = _wav_out if os.path.exists(_wav_out) else None

            metadata_pool_manager.record_clip(
                content_id=content_id,
                channel_name=channel_name,
                manga_name=manga_name,
                chapter_or_vol=chapter_or_vol,
                language=language,
                title=title,
                url=url,
                video_path=final_path,
                audio_path=audio_file
            )
        except Exception as _mp_err:
            logger.warning(f"⚠️ Could not record entry in metadata_pool.json: {_mp_err}")

        return final_path, False

    except Exception as exc:
        logger.error("💥  Commit phase failed: %s", exc, exc_info=True)
        if downloaded_path and os.path.exists(downloaded_path):
            try: os.remove(downloaded_path)
            except OSError: pass
        return None, False


# ---------------------------------------------------------------------------
# Commit-phase helpers
# ---------------------------------------------------------------------------

def _maybe_rename_to_title(existing_path: str, custom_title: str) -> str:
    """Atomically rename a cached file to reflect custom_title, if needed."""
    title_slug   = _sanitize_filename(custom_title)[:100]
    current_name = os.path.basename(existing_path)

    if title_slug.lower() in current_name.lower():
        return existing_path   # already correct

    ext  = existing_path.rsplit(".", 1)[-1]
    dir_ = os.path.dirname(existing_path)

    for i in range(1, 200):
        candidate = os.path.join(dir_, f"{title_slug}_{i}.{ext}")
        if _atomic_rename(existing_path, candidate):
            logger.info("✏️  Renamed cached file → %s", os.path.basename(candidate))
            old_json = os.path.splitext(existing_path)[0] + ".json"
            new_json = os.path.splitext(candidate)[0]     + ".json"
            if os.path.exists(old_json):
                try: _atomic_rename(old_json, new_json)
                except OSError: pass
            try:
                meta: Dict = {}
                if os.path.exists(new_json):
                    with open(new_json, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                meta["title"] = custom_title
                DownloadIndex.register(candidate, meta)
            except Exception as exc:
                logger.warning("📂  Index update after rename failed: %s", exc)
            return candidate

    logger.warning("✏️  Could not find a free name for title rename — keeping original")
    return existing_path


def _resolve_final_path(
    temp_path:      str,
    ext:            str,
    meta:           Dict,
    custom_title:   Optional[str],
    force_filename: Optional[str],
    content_hash:   str,
    target_dir:     Optional[str] = None,
) -> Optional[str]:
    """
    Atomically rename temp_path to its final location.
    Returns the final path, or None on total collision failure.
    """
    out_dir = target_dir or DOWNLOAD_DIR
    if force_filename:
        # Use stem from force_filename but the real detected extension
        stem      = os.path.splitext(force_filename)[0]
        candidate = os.path.join(out_dir, f"{stem}.{ext}")
        if os.path.exists(candidate):   # forced overwrite is intentional
            try: os.remove(candidate)
            except OSError: pass
        if _atomic_rename(temp_path, candidate):
            return candidate
        return None

    title_slug = _sanitize_filename(custom_title or meta.get("title", ""))[:100]
    if not title_slug:
        title_slug = f"video_{meta.get('id', 'unknown')}"

    candidates = (
        os.path.join(out_dir, name)
        for name in (
            [f"{title_slug}.{ext}"]
            + [f"{title_slug}_{i}.{ext}" for i in range(1, 200)]
            + [f"{title_slug}_{content_hash[:6]}.{ext}"]
        )
    )
    for candidate in candidates:
        if _atomic_rename(temp_path, candidate):
            return candidate

    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="🎬  AMTCE Harvest Module — standalone video downloader"
    )
    parser.add_argument("--input",  "-i", required=True, help="Target URL")
    parser.add_argument("--title",  "-t", help="Custom title for the video")
    parser.add_argument("--force",  "-f", help="Force a specific output filename")
    parser.add_argument("--debug",  action="store_true", help="Verbose debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.getLogger().setLevel(level)

    print()
    print(f"  {'━' * 54}")
    print(f"  📥  AMTCE Harvester  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {'━' * 54}")
    print(f"  🔗  {args.input}")
    print(f"  {'━' * 54}")
    print()

    try:
        path, is_cached = download_video(
            args.input,
            custom_title=args.title,
            force_filename=args.force,
        )
        if path:
            status = "♻️  REUSED (cached)" if is_cached else "✅  DOWNLOADED"
            print()
            print(f"  {status}")
            print(f"  📁  {os.path.abspath(path)}")
            print()
        else:
            print()
            print("  ❌  FAILED — all strategies exhausted")
            print()
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n  🛑  Cancelled by user.\n")
        sys.exit(0)
    except Exception as exc:
        print(f"\n  💥  ERROR: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()