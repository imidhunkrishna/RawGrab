"""
universal_downloader.py — AMTCE Universal Media Ingestion Gateway
====================================================================
The ONE function every part of the system calls to bring external
content into the pipeline — manga/comic PDFs, page images, archives, and video.

Architecture & Policies:
  - Single API entry point: ingest(source)
  - Auto-routing into Image, PDF, Video, Archive pipelines
  - Hard byte ceilings during streaming download (Zero external dependency via urllib)
  - Direct download into quarantine (eliminates double file moves)
  - Security quarantine & validation gate via security_validator.py
  - Releases safe media to storage/{image|pdf|video|archive}/
"""

from __future__ import annotations

import os
import sys
import time
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from urllib.parse import urlparse

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from . import security_validator as sv
except ImportError:
    try:
        from Phase_1.Downloader_Modules import security_validator as sv
    except ImportError:
        from Tools.Downloader_Modules import security_validator as sv  # type: ignore

logger = logging.getLogger("universal_downloader")

QUARANTINE_DIR = os.getenv("AMTCE_QUARANTINE_DIR", os.path.join(_REPO_ROOT, "quarantine"))
STORAGE_DIR = os.getenv("AMTCE_STORAGE_DIR", os.path.join(_REPO_ROOT, "storage"))

_MAX_DOWNLOAD_BYTES = 3 * 1024 * 1024 * 1024  # 3 GB streaming ceiling cap

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36")


class IngestionError(Exception):
    def __init__(self, reason: str, scan: Optional[sv.ScanResult] = None):
        super().__init__(reason)
        self.reason = reason
        self.scan = scan


def _is_url(s: str) -> bool:
    try:
        p = urlparse(s)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _stream_download(url: str, dest_path: str, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> None:
    """Download with a hard byte ceiling enforced DURING streaming using stdlib urllib."""
    written = 0
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            declared_len = r.headers.get("Content-Length")
            if declared_len and int(declared_len) > max_bytes:
                raise IngestionError(f"declared_content_length_exceeds_cap:{declared_len}")

            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        f.close()
                        try:
                            os.remove(dest_path)
                        except OSError:
                            pass
                        raise IngestionError(f"stream_exceeded_cap_bytes:{written}")
                    f.write(chunk)
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError(f"stream_download_failed:{exc}")


def _quarantine_path(suggested_name: str) -> str:
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    ts = int(time.time() * 1000)
    safe_name = "".join(c for c in suggested_name if c.isalnum() or c in "._-") or "file"
    return os.path.join(QUARANTINE_DIR, f"{ts}_{safe_name}")


# ---------------------------------------------------------------------------
# DOMAIN SECURITY CLASSIFICATION ENGINE (Whitelist / Greylist / Blacklist)
# ---------------------------------------------------------------------------
WHITELISTED_DOMAINS = frozenset([
    "instagram.com", "www.instagram.com", "cdninstagram.com",
    "youtube.com", "www.youtube.com", "youtu.be", "googlevideo.com",
    "tiktok.com", "www.tiktok.com", "viktokcdn.com",
    "twitter.com", "x.com", "twimg.com",
    "vimeo.com", "facebook.com", "fbcdn.net",
    "pinterest.com", "reddit.com", "redditmedia.com",
    "wikimedia.org", "githubusercontent.com",
    "imgur.com", "pexels.com", "pixabay.com", "unsplash.com"
])

BLACKLISTED_PATTERNS = frozenset([
    "localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "::1",
    "internal.", "intranet.", "malware", "phishing", "virus", "exploit",
    ".exe", ".bat", ".cmd", ".sh", ".vbs", ".ps1", ".msi"
])


def evaluate_domain_security(url: str) -> str:
    """
    Evaluates domain security tier:
      - 'WHITELIST': High-trust verified media platform domain.
      - 'BLACKLIST': SSRF attack, local network IP, executable, or known malicious pattern.
      - 'GREYLIST':  Unverified external domain (routed through sandbox quarantine & Apify scraper).
    """
    if not _is_url(url):
        return "LOCAL"

    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower().split(":")[0]  # strip port
        full_url = url.lower()

        # 1. SSRF & Blacklist Pattern Inspection
        for pattern in BLACKLISTED_PATTERNS:
            if pattern in netloc or pattern in full_url:
                logger.warning("⛔ [SECURITY BLACKLIST MATCH] Blocked untrusted/SSRF URL: %s (Matched: %s)", url, pattern)
                return "BLACKLIST"

        # 2. Whitelist Inspection
        for domain in WHITELISTED_DOMAINS:
            if netloc == domain or netloc.endswith("." + domain):
                return "WHITELIST"

        # 3. Greylist Fallback
        logger.info("⚠️ [SECURITY GREYLIST DOMAIN] Unverified external domain: %s (Routing to quarantine sandbox)", netloc)
        return "GREYLIST"
    except Exception as err:
        logger.error("Error parsing domain security: %s", err)
        return "BLACKLIST"


def _route_type(source: str) -> str:
    """Detects media category based on URL/file path extension and keywords. Rejects compressed archives."""
    path = urlparse(source).path if _is_url(source) else source
    ext = os.path.splitext(path.lower())[1].split("?")[0]

    if ext in (".cbz", ".cbr", ".zip", ".rar", ".7z", ".tar", ".gz", ".iso", ".bz2", ".xz"):
        return "archive_prohibited"
    elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg"):
        return "image"
    elif ext in (".pdf", ".epub"):
        return "pdf"
    elif ext in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".wmv", ".3gp"):
        return "video"
    elif ext in (".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"):
        return "audio"

    # Keywords fallback for social media video & streaming URLs
    low = source.lower()
    if any(k in low for k in ("instagram.com", "youtube.com", "youtu.be", "tiktok.com", "twitter.com", "x.com", "vimeo.com", "facebook.com", "reel", "shorts")):
        return "video"

    return "pdf" if ext == ".pdf" else ("video" if any(k in low for k in ("v=", "watch")) else "image")


def _apify_generic_fallback(url: str) -> Optional[str]:
    """Uses Apify generic website scraper actor (APIFY_GENERIC_ACTOR=apify/website-scraper) to scrape direct media CDN links."""
    try:
        try:
            from .apify_downloader import apify_get_video_url_any
        except ImportError:
            from Phase_1.Downloader_Modules.apify_downloader import apify_get_video_url_any
        actor_name = os.getenv("APIFY_GENERIC_ACTOR", "apify/website-scraper")
        return apify_get_video_url_any(url, actor_name=actor_name)
    except Exception as _e:
        logger.debug("Apify generic website scraper fallback notice: %s", _e)
        return None


# ---------------------------------------------------------------------------
# CORE UNIFIED INGESTION API — ingest(source)
# ---------------------------------------------------------------------------
def ingest(source: str) -> sv.ScanResult:
    """
    SINGLE UNIFIED ENTRY POINT for external content (video, pdf, image, audio).
    Enforces Domain Whitelist, Greylist, and Blacklist security screening.
    Archives (.zip, .rar, .7z, .cbz, etc.) are strictly PROHIBITED by policy to prevent zip bombs.
    """
    if not source:
        raise IngestionError("empty_source_provided")

    # 1. Domain Security Evaluation (Blacklist / Whitelist / Greylist)
    if _is_url(source):
        sec_tier = evaluate_domain_security(source)
        if sec_tier == "BLACKLIST":
            raise IngestionError(
                f"domain_blacklisted: URL '{source}' blocked due to security blacklist, SSRF protection, or malicious host pattern."
            )

    media_type = _route_type(source)

    if media_type == "archive_prohibited":
        raise IngestionError("archive_file_prohibited: Compressed archives (.zip, .rar, .7z, .cbz, .tar, etc.) are strictly prohibited by security policy to prevent zip bomb attacks and pipeline compromise.")
    elif media_type == "image":
        return ingest_image(source)
    elif media_type == "pdf":
        return ingest_pdf(source)
    elif media_type == "video":
        return ingest_video(source)
    elif media_type == "audio":
        return ingest_audio(source)
    else:
        raise IngestionError(f"unsupported_media_type:{media_type}")


def ingest_batch(sources: List[str], max_workers: int = 4) -> List[sv.ScanResult]:
    """Runs parallel batch ingestion for multiple URLs / files."""
    if not sources:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(sources))) as executor:
        return list(executor.map(ingest, sources))


# ---------------------------------------------------------------------------
# Category Ingest Handlers
# ---------------------------------------------------------------------------
def ingest_image(url: str) -> sv.ScanResult:
    """Fetch image from HTTP(S) URL and validate. Rejects direct raw local upload paths by policy."""
    if not _is_url(url):
        raise IngestionError(
            "image_raw_upload_rejected: images must be provided as an "
            "http(s) URL to reduce input attack vectors."
        )

    ext_guess = os.path.splitext(urlparse(url).path)[1] or ".png"
    q_path = _quarantine_path(f"img{ext_guess}")

    logger.info("🌐 [INGEST:image] Streaming %s -> %s", url, q_path)
    try:
        _stream_download(url, q_path)
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError(f"image_download_failed:{exc}")

    result = sv.validate_and_quarantine(q_path, "image", QUARANTINE_DIR, STORAGE_DIR)
    if not result.safe:
        raise IngestionError("image_rejected:" + "; ".join(result.reasons), scan=result)
    return result


def ingest_pdf(source: str) -> sv.ScanResult:
    """Accepts PDF URL or local disk path. Copies into quarantine and validates."""
    if _is_url(source):
        q_path = _quarantine_path("doc.pdf")
        logger.info("🌐 [INGEST:pdf] Streaming %s -> %s", source, q_path)
        try:
            _stream_download(source, q_path)
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError(f"pdf_download_failed:{exc}")
    else:
        if not os.path.exists(source):
            raise IngestionError(f"pdf_local_path_not_found:{source}")
        q_path = _quarantine_path(os.path.basename(source))
        import shutil
        shutil.copy2(source, q_path)

    result = sv.validate_and_quarantine(q_path, "pdf", QUARANTINE_DIR, STORAGE_DIR)
    if not result.safe:
        raise IngestionError("pdf_rejected:" + "; ".join(result.reasons), scan=result)
    return result


def ingest_video(source: str, download_fn=None) -> sv.ScanResult:
    """Downloads video DIRECTLY into quarantine/ to eliminate double file moves."""
    if _is_url(source):
        try:
            try:
                from .metadata_pool_manager import metadata_pool_manager
            except ImportError:
                from Phase_1.Downloader_Modules.metadata_pool_manager import metadata_pool_manager
            url_id = None
            if "v=" in source:
                url_id = source.split("v=")[1].split("&")[0]
            elif "youtu.be/" in source:
                url_id = source.split("youtu.be/")[1].split("?")[0]

            cached_rec = metadata_pool_manager.find_cached_clip(content_id=url_id, url=source)
            if cached_rec and cached_rec.get("video_path") and os.path.exists(cached_rec["video_path"]):
                logger.info("♻️ [METADATA POOL INGEST HIT 🎯] Reusing existing video clip without ytdlp download: %s", cached_rec["video_path"])
                return sv.validate_and_quarantine(cached_rec["video_path"], "video", QUARANTINE_DIR, STORAGE_DIR)
        except Exception as _p_err:
            logger.debug(f"Ingest metadata pool check notice: {_p_err}")

        if download_fn is None:
            try:
                try:
                    from .downloader import download_video as download_fn  # type: ignore
                except ImportError:
                    from Phase_1.Downloader_Modules.downloader import download_video as download_fn  # type: ignore
            except ImportError as exc:
                raise IngestionError(f"no_video_downloader_available:{exc}")

        # Download directly into quarantine folder
        q_dir = os.path.join(QUARANTINE_DIR, f"vid_{int(time.time()*1000)}")
        os.makedirs(q_dir, exist_ok=True)
        raw_path, _is_cached = download_fn(source, destination_dir=q_dir)
        if not raw_path:
            raise IngestionError("video_download_failed_all_strategies_exhausted")
        q_path = raw_path
    else:
        if not os.path.exists(source):
            raise IngestionError(f"video_local_path_not_found:{source}")
        q_path = _quarantine_path(os.path.basename(source))
        import shutil
        shutil.copy2(source, q_path)

    result = sv.validate_and_quarantine(q_path, "video", QUARANTINE_DIR, STORAGE_DIR)
    if not result.safe:
        raise IngestionError("video_rejected:" + "; ".join(result.reasons), scan=result)

    if _is_url(source) and result.safe_path:
        try:
            try:
                from .metadata_pool_manager import metadata_pool_manager
            except ImportError:
                from Phase_1.Downloader_Modules.metadata_pool_manager import metadata_pool_manager
            url_id = None
            if "v=" in source:
                url_id = source.split("v=")[1].split("&")[0]
            elif "youtu.be/" in source:
                url_id = source.split("youtu.be/")[1].split("?")[0]
            
            _audio_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "Original_audio"))
            _stem = os.path.splitext(os.path.basename(result.safe_path))[0]
            _wav = os.path.join(_audio_dir, f"{_stem}.wav")

            metadata_pool_manager.record_clip(
                content_id=url_id or os.path.basename(result.safe_path),
                channel_name=os.environ.get("CREATOR_ID", "general"),
                manga_name=os.environ.get("MANGA", "General"),
                chapter_or_vol=os.environ.get("CHAPTER", "General"),
                language=os.environ.get("LANGUAGE", "en"),
                title=os.path.basename(result.safe_path),
                url=source,
                video_path=result.safe_path,
                audio_path=_wav if os.path.exists(_wav) else None
            )
        except Exception as _r_err:
            logger.debug("Post-quarantine record clip notice: %s", _r_err)

    return result


def ingest_archive(source: str) -> sv.ScanResult:
    """STRICTLY REJECTED: Archives (.zip, .rar, .7z, .cbz, .tar, etc.) are prohibited by security policy."""
    raise IngestionError("archive_file_prohibited: Compressed archives (.zip, .rar, .7z, .cbz, .tar, etc.) are strictly prohibited by security policy to prevent zip bomb attacks and pipeline compromise.")
    return result


def ingest_audio(source: str) -> sv.ScanResult:
    """Accepts MP3/WAV audio URL or local disk path."""
    if _is_url(source):
        q_path = _quarantine_path("audio.mp3")
        try:
            _stream_download(source, q_path)
        except Exception as exc:
            cdn_fallback = _apify_generic_fallback(source)
            if cdn_fallback:
                _stream_download(cdn_fallback, q_path)
            else:
                raise IngestionError(f"audio_download_failed:{exc}")
    else:
        if not os.path.exists(source):
            raise IngestionError(f"audio_local_path_not_found:{source}")
        q_path = _quarantine_path(os.path.basename(source))
        import shutil
        shutil.copy2(source, q_path)

    result = sv.validate_and_quarantine(q_path, "video", QUARANTINE_DIR, STORAGE_DIR)
    if not result.safe:
        raise IngestionError("audio_rejected:" + "; ".join(result.reasons), scan=result)
    return result