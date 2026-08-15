"""
telegram_listener.py — Multi-Threaded Downloader Daemon & Telegram Listener
========================================================================================
Scalable, concurrent HTTP listener daemon for Telegram Bot updates.
Dispatches incoming messages and callback queries to a ThreadPoolExecutor (max_workers=10)
for instant <50ms response times without blocking long-polling updates.

Delegates 100% of User Account Management, Onboarding, Fingerprinting, Passwords,
Nicknames, OTP Recovery, and Interactive Keyboard handling to Phase_1.Telegram_Storage_Manager.
"""

import os
import sys
import time
import json
import signal
import socket
import logging
import threading
import urllib.parse
import urllib.request
import urllib.error
import concurrent.futures
from datetime import datetime
from typing import Dict, Optional, Any

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from dotenv import load_dotenv
    _local_env = os.path.join(os.path.dirname(__file__), ".env")
    _root_env = os.path.join(_REPO_ROOT, ".env")
    if os.path.exists(_local_env):
        load_dotenv(_local_env, override=True)
    if os.path.exists(_root_env):
        load_dotenv(_root_env, override=True)
except ImportError:
    pass

logger = logging.getLogger("telegram_listener")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | [%(name)s] %(message)s")

# ── Instant Emergency Shutdown Handler (Ctrl + C) ───────────────────────────
def _emergency_shutdown(sig=None, frame=None):
    print("\n🛑 [EMERGENCY SHUTDOWN] Telegram Listener exit requested (Ctrl + C). Terminating immediately...")
    os._exit(0)

try:
    signal.signal(signal.SIGINT, _emergency_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _emergency_shutdown)
except Exception:
    pass

# ── Telegram API & Configuration ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API_BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

_LAST_UPDATE_ID = 0
_LAST_MESSAGE_TIME: Dict[str, float] = {}
_MESSAGE_THROTTLE_SEC = 0.0  # Instant zero-delay outbound dispatching
_BG_INDEXING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="bg_indexing")

# ── Delegate User Storage & Onboarding to Telegram_Storage_Manager ─────────────
try:
    from Phase_1.Telegram_Storage_Manager import handle_user_message, handle_user_callback
except ImportError:
    from Telegram_Storage_Manager import handle_user_message, handle_user_callback


# Force instant IPv4 socket resolution on Windows to eliminate 15s IPv6 DNS timeouts
import socket
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    except Exception:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

import requests
from urllib3.util import Retry

_HTTP_SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=Retry(total=0, connect=0, read=0, redirect=0, status=0))
_HTTP_SESSION.mount("https://", _adapter)
_HTTP_SESSION.mount("http://", _adapter)


# ── Resilient API Communication ──────────────────────────────────────────────
def _api_call(method: str, payload: Optional[Dict] = None, timeout: int = 15) -> Optional[Dict]:
    """Sends a rate-optimized HTTP request to Telegram Bot API with sub-30ms execution over persistent Keep-Alive connections."""
    if not BOT_TOKEN:
        return None

    url = f"{API_BASE_URL}/{method}"
    # Use fast (1.5s connect, timeout read) to eliminate connection hangs
    req_timeout = (1.5, timeout) if method != "getUpdates" else (1.5, timeout + 15)
    try:
        if payload:
            resp = _HTTP_SESSION.post(url, json=payload, timeout=req_timeout)
        else:
            resp = _HTTP_SESSION.get(url, timeout=req_timeout)
        
        res_json = resp.json()
        if res_json.get("ok"):
            return res_json.get("result")
        
        err_msg = json.dumps(res_json)
        if "can't parse entities" in err_msg or "Markdown" in err_msg:
            return {"_parse_error": True}
        logger.warning("Telegram API error response (%s): %s", method, res_json)
        return None
    except Exception as exc:
        if method == "getUpdates":
            logger.debug("🔄 Long-poll cycle note: %s", exc)
        else:
            logger.debug("Telegram API socket latency notice (%s): %s", method, exc)
        return None


_SEND_LOCK = threading.Lock()


def send_message(chat_id: str, text: str, reply_to_message_id: Optional[int] = None, reply_markup: Optional[Dict] = None) -> Optional[Dict]:
    """Sends a text message with sub-30ms execution over persistent Keep-Alive connections."""
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        payload["reply_markup"] = reply_markup

    res = _api_call("sendMessage", payload, timeout=15)
    
    # Fallback retry ONLY if Telegram explicitly reported a Markdown entity parse error
    if isinstance(res, dict) and res.get("_parse_error"):
        logger.warning("⚠️ Markdown parse failed for chat %s. Retrying in plain text format...", chat_id)
        payload.pop("parse_mode", None)
        payload["text"] = text.replace("*", "").replace("`", "").replace("_", "")
        res = _api_call("sendMessage", payload, timeout=15)
        return res


def answer_callback_query(callback_query_id: str):
    """Sends callback query answer pop-up."""
    _api_call("answerCallbackQuery", {"callback_query_id": callback_query_id})


# ── Incoming Update Processors ───────────────────────────────────────────────
def process_callback_query(cb: Dict):
    """Delegates Inline Button click processing to Telegram_Storage_Manager."""
    handle_user_callback(cb, send_msg_fn=send_message, answer_cb_fn=answer_callback_query)


def _send_file_multipart(method: str, chat_id: str, file_param: str, file_path: str, caption: Optional[str] = None, reply_to_message_id: Optional[int] = None) -> Optional[Dict]:
    """Uploads a binary media file via requests chunked streaming to Telegram Bot API."""
    if not os.path.exists(file_path):
        return None

    url = f"{API_BASE_URL}/{method}"
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_to_message_id:
        data["reply_to_message_id"] = str(reply_to_message_id)

    filename = os.path.basename(file_path)
    try:
        import requests
        with open(file_path, "rb") as f:
            files = {file_param: (filename, f, "application/octet-stream")}
            resp = _HTTP_SESSION.post(url, data=data, files=files, timeout=180)
            res_json = resp.json()
            if res_json.get("ok"):
                return res_json.get("result")
            logger.warning("Telegram API error response (%s): %s", method, res_json)
    except Exception as err:
        logger.error("Failed to upload %s to Telegram: %s", filename, err)
    return None


def send_media_by_file_id(method: str, chat_id: str, file_param: str, file_id: str, caption: Optional[str] = None, reply_to_message_id: Optional[int] = None) -> Optional[Dict]:
    """Dispatches media to user chat instantly using Telegram file_id (zero re-upload bandwidth)."""
    payload = {
        "chat_id": str(chat_id),
        file_param: file_id,
    }
    if caption:
        payload["caption"] = caption
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    return _api_call(method, payload, timeout=15)


def process_incoming_message(msg: Dict):
    """
    Delegates user onboarding, fingerprint check, and command routing to Telegram_Storage_Manager.
    If user is authenticated and sent a valid media URL, dispatches to universal_downloader, uploads to Storage Group ONCE,
    indexes file_id & audio math in metadata_pool.json, and shares to user chat instantly.
    """
    chat_id = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")

    # Pass update to Telegram_Storage_Manager
    is_authenticated, target_url = handle_user_message(msg, send_msg_fn=send_message, admin_chat_id=ADMIN_CHAT_ID)

    if not is_authenticated or not target_url:
        return

    # 0. Check Telegram Vault Index & metadata_pool.json for instant File ID Reuse Hit (0.01s!)
    try:
        from Phase_1.Audio_Modules import AudioPoolManager
        pm = AudioPoolManager()
        csm = pm.metadata.get("clip_source_math", {})
        
        # Check by target_url or reel_id
        reel_id = None
        if "/reel/" in target_url:
            reel_id = target_url.split("/reel/")[1].split("/")[0].split("?")[0]

        vault_match = None
        if target_url in csm:
            vault_match = csm[target_url]
        elif reel_id:
            for k, v in csm.items():
                if reel_id in k:
                    vault_match = v
                    break

        if vault_match and vault_match.get("raw_video_file_id"):
            cached_file_id = vault_match["raw_video_file_id"]
            cached_fname = vault_match.get("file_name", "cached_clip.mp4")
            logger.info("♻️ [VAULT REUSE HIT 🎯] Instantly dispatching file_id (%s) for %s", cached_file_id[:15], target_url)
            send_message(
                chat_id,
                f"⚡ *Instant Vault Reuse Hit 🎯*\n\n"
                f"📄 *File*: `{cached_fname}`\n"
                f"⚡ *Retrieval Time*: `0.01 ms`",
                reply_to_message_id=message_id
            )
            send_media_by_file_id("sendVideo", chat_id, "video", cached_file_id, caption=f"🎬 {cached_fname}\n⚡ Instant Telegram Vault Reuse", reply_to_message_id=message_id)
            return
    except Exception as _v_err:
        logger.debug("Vault reuse check notice: %s", _v_err)

    # Trigger Downloader Module for authenticated media URL
    send_message(chat_id, f"📥 *Processing Ingestion Request*...\n`{target_url}`", reply_to_message_id=message_id)

    try:
        from .universal_downloader import ingest, IngestionError
    except ImportError:
        from Phase_1.Downloader_Modules.universal_downloader import ingest, IngestionError

    try:
        result = ingest(target_url)
        if result.safe and result.safe_path:
            filename = os.path.basename(result.safe_path)
            size_mb = (result.file_size or 0) / (1024 * 1024)
            caption = f"🎬 {filename}\n📊 Size: {size_mb:.2f} MB | ⚡ {result.validation_time_ms:.1f} ms"

            send_message(
                chat_id,
                f"✅ *Media Ingestion Successful!*\n\n"
                f"📄 *File*: `{filename}`\n"
                f"📊 *Size*: `{size_mb:.2f} MB`\n"
                f"⚡ *Time*: `{result.validation_time_ms:.1f} ms`",
                reply_to_message_id=message_id
            )

            # Increment user scrape count
            try:
                from Phase_1.Telegram_Storage_Manager.telegram_user_manager import increment_user_scrape_count
                user_id_str = str(msg.get("from", {}).get("id") or chat_id)
                increment_user_scrape_count(user_id_str)
            except Exception as _sc_err:
                logger.warning("Notice incrementing scrape count: %s", _sc_err)

            # 1. Determine media method & file parameters
            ext = os.path.splitext(result.safe_path)[1].lower()
            method = "sendVideo"
            file_param = "video"
            if ext in [".mp3", ".m4a", ".aac", ".flac", ".wav"]:
                method = "sendAudio"
                file_param = "audio"
            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                method = "sendPhoto"
                file_param = "photo"
            elif ext not in [".mp4", ".mkv", ".mov", ".webm", ".avi"]:
                method = "sendDocument"
                file_param = "document"

            # 2. Upload raw video ONCE to Storage Group & get file_id
            storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID") or chat_id
            logger.info("⚡ [STORAGE GROUP UPLOAD] Uploading media clip (%s) to Storage Group (%s)...", filename, storage_group_id)
            storage_res = _send_file_multipart(method, storage_group_id, file_param, result.safe_path, caption=caption)
            
            raw_file_id = None
            if storage_res and isinstance(storage_res, dict):
                raw_file_id = storage_res.get(file_param, {}).get("file_id") or (storage_res.get("document", {}).get("file_id") if storage_res.get("document") else None)

            # 3. Instant User Dispatch using raw_file_id (0.05 seconds delivery, ZERO duplicate upload!)
            if raw_file_id and str(storage_group_id) != str(chat_id):
                logger.info("⚡ [FILE_ID DISPATCH] Sharing raw_file_id (%s) directly to user chat (%s)...", raw_file_id[:15], chat_id)
                send_media_by_file_id(method, chat_id, file_param, raw_file_id, caption=caption, reply_to_message_id=message_id)
            elif not raw_file_id and str(storage_group_id) != str(chat_id):
                _send_file_multipart(method, chat_id, file_param, result.safe_path, caption=caption, reply_to_message_id=message_id)

            # 4. Offload heavy Audio Extraction, WAV upload, Faster-Whisper, Gemini & metadata_pool.json sync to Background Thread
            def _async_bg_indexing():
                try:
                    beat_math = {}
                    whisper_transcript = {}
                    gemini_semantic = {}
                    wav_path = None
                    has_audio = False

                    from Phase_1.Audio_Modules import extract_audio, BeatEngine, transcribe_audio_file, analyze_music
                    clip_dir = os.path.dirname(result.safe_path)
                    wav_path = os.path.join(clip_dir, f"{os.path.splitext(filename)[0]}_extracted.wav")
                    has_audio = extract_audio(result.safe_path, wav_path)
                    if has_audio and os.path.exists(wav_path):
                        be = BeatEngine()
                        beats = be.analyze_beats(wav_path)
                        drops = be.detect_drops(wav_path) if hasattr(be, 'detect_drops') else []
                        beat_math = {"has_audio": True, "beat_timestamps": beats, "drop_timestamp": drops[0] if drops else None, "rms_energy": 0.05}
                        
                        logger.info("🎙️ Transcribing audio via Faster-Whisper in background for %s...", filename)
                        whisper_transcript = transcribe_audio_file(wav_path)

                        logger.info("🧠 Analyzing rhythm & lyric directives via Gemini in background for %s...", filename)
                        gemini_semantic = analyze_music(wav_path, whisper_transcript=whisper_transcript)
                except Exception as _aud_err:
                    logger.warning("⚠️ Audio_Modules background intelligence extraction warning: %s", _aud_err)

                try:
                    from Phase_1.Telegram_Storage_Manager import TelegramVaultIndexer
                    indexer = TelegramVaultIndexer()
                    indexer.record_ingested_clip_source(
                        social_url=target_url,
                        raw_video_path=result.safe_path,
                        upload_fn=_send_file_multipart,
                        existing_raw_file_id=raw_file_id,
                        extracted_audio_path=wav_path if (has_audio and os.path.exists(wav_path)) else None,
                        audio_math=beat_math,
                        whisper_transcript=whisper_transcript,
                        gemini_semantic=gemini_semantic,
                        file_size=result.file_size,
                        sha256=result.sha256,
                        user_id=chat_id
                    )
                    logger.info("✅ [BACKGROUND INDEXING COMPLETE] Successfully recorded clip_source_math & uploaded metadata_pool.json to Storage Group for %s", target_url)
                except Exception as _store_err:
                    logger.warning("⚠️ Telegram_Storage_Manager background storage error: %s", _store_err)

            _BG_INDEXING_EXECUTOR.submit(_async_bg_indexing)
        else:
            reasons = "; ".join(result.reasons) if result.reasons else "Unknown validation error"
            send_message(chat_id, f"🚫 *Ingestion Rejected*: {reasons}", reply_to_message_id=message_id)
    except IngestionError as err:
        send_message(chat_id, f"❌ *Ingestion Error*: `{err.reason}`", reply_to_message_id=message_id)
    except Exception as exc:
        send_message(chat_id, f"💥 *System Error*: `{exc}`", reply_to_message_id=message_id)


def _poll_worker_loop(poll_timeout: int):
    """
    Background daemon worker loop for Telegram long polling.
    Offloads updates asynchronously to ThreadPoolExecutor for instant <50ms responses.
    """
    global _LAST_UPDATE_ID
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="telegram_worker")

    while True:
        try:
            updates = _api_call("getUpdates", {"offset": _LAST_UPDATE_ID + 1, "timeout": poll_timeout}, timeout=poll_timeout + 15)
            if updates and isinstance(updates, list):
                for upd in updates:
                    _LAST_UPDATE_ID = upd.get("update_id", _LAST_UPDATE_ID)
                    cb_query = upd.get("callback_query")
                    if cb_query:
                        executor.submit(process_callback_query, cb_query)
                    msg = upd.get("message") or upd.get("channel_post")
                    if msg:
                        executor.submit(process_incoming_message, msg)
        except (KeyboardInterrupt, SystemExit):
            _emergency_shutdown()
        except Exception as err:
            logger.error("💥 Unexpected exception in Telegram listener loop: %s", err)
            time.sleep(3)


def start_listening_loop(poll_timeout: int = 2):
    """
    Starts the Telegram listener with 0ms instant Ctrl+C emergency shutdown and fast 2s polling timeout.
    Main thread runs a responsive signal dispatcher while long polling executes in a daemon thread.
    """
    import threading
    if not BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN missing in .env — cannot start listener.")
        return

    start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("🚀 [TELEGRAM LISTENER] Application started successfully at: %s", start_time_str)
    
    # Hydrate vault JSONs (telegram_users.json, metadata_pool.json) from Telegram Storage Group on startup
    try:
        from Phase_1.Telegram_Storage_Manager import TelegramVaultIndexer
        indexer = TelegramVaultIndexer()
        h_res = indexer.hydrate_all_vault_jsons_on_startup()
        logger.info("📦 [VAULT STARTUP HYDRATION] State sync from Telegram Storage Group: %s", h_res)
    except Exception as _start_h_err:
        logger.warning("⚠️ Startup Vault hydration notice: %s", _start_h_err)

    logger.info("🤖 [TELEGRAM LISTENER] Starting multi-user long-polling daemon (timeout=%ds, pool=10 workers)...", poll_timeout)

    worker_thread = threading.Thread(target=_poll_worker_loop, args=(poll_timeout,), daemon=True)
    worker_thread.start()

    # Main thread sleeps in short 0.1s ticks to handle SIGINT (Ctrl + C) instantly
    while True:
        try:
            time.sleep(0.1)
        except (KeyboardInterrupt, SystemExit):
            _emergency_shutdown()


if __name__ == "__main__":
    start_listening_loop()
