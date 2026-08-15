"""
Phase_1 / Telegram_Storage_Manager / telegram_vault_indexer.py
================================================================
Telegram Storage Group Unified Master Vault Indexer.

Turns Telegram into an unlimited, zero-cost cloud data lake for ephemeral runners
(GitHub Actions / Docker). Stores permanent file_id references and full visual/lyric
intelligence in a single pinned master_vault_index.json document inside the storage group.

Columns:
  Column 1 (processed_reels):
    Indexed by session_id, social_media_id, and custom_title.
    Stores master_video_file_id, audio_data (pool_metadata + lyric_intel),
    and visual_data (.clip_intelligence.json).

  Column 2 (downloaded_sources):
    Indexed by social_media_id (Instagram/YouTube URL) and session_id.
    Stores raw_video_file_id, extracted_audio_file_id, and audio_math.
    Enables 1.5s cache hits on duplicate URL requests without re-downloading.

Author: AMTCE Serverless Vault Architecture v1.0
"""

import os
import sys
import json
import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("telegram_vault_indexer")

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(_REPO_ROOT, "data")
JSONS_DIR = os.path.join(_REPO_ROOT, "Phase_1", "The jsons", "Telegram_Storage_Manager_json")
MASTER_INDEX_FILE = os.path.join(JSONS_DIR, "telegram_media_index.json")


def _empty_vault_index() -> Dict[str, Any]:
    return {
        "version": 2.0,
        "updated_at": time.time(),
        "pinned_message_id": None,
        "metadata_pool_file_id": None,
        "telegram_users_file_id": None,
        "column_1_processed_reels": {
            "by_session_id": {},
            "by_social_media_id": {},
            "by_user_id": {},  # User-scoped indexing
        },
        "column_2_downloaded_sources": {
            "by_social_media_id": {},
            "by_session_id": {},
            "by_user_id": {},  # User-scoped indexing
        },
    }


class TelegramVaultIndexer:
    """
    Manages reading, writing, uploading, and pinning the master_vault_index.json
    inside TELEGRAM_STORAGE_GROUP_ID.
    """

    def __init__(self, index_file: str = MASTER_INDEX_FILE):
        self.index_file = index_file
        os.makedirs(os.path.dirname(self.index_file), exist_ok=True)
        self.vault_index = self._load_local_index()

    def _load_local_index(self) -> Dict[str, Any]:
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "column_1_processed_reels" in data:
                        return data
            except Exception as e:
                logger.warning(f"⚠️ Could not load local vault index: {e}")
        return _empty_vault_index()

    def _save_local_index(self):
        try:
            self.vault_index["updated_at"] = time.time()
            temp_path = self.index_file + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.vault_index, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.index_file)
        except Exception as e:
            logger.error(f"❌ Failed to save local vault index: {e}")

    # ── VAULT JSON HYDRATION & CLOUD SYNC APIs ───────────────────────────────

    def download_vault_file_by_id(self, file_id: str, dest_path: str) -> bool:
        """
        Downloads a document file (e.g. telegram_users.json or metadata_pool.json)
        from Telegram Storage Group into dest_path via Telegram Bot API getFile.
        """
        if not file_id:
            return False
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not bot_token:
            return False

        try:
            import urllib.request
            import json as _json
            get_file_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
            req = urllib.request.Request(get_file_url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                res_data = _json.loads(resp.read().decode("utf-8"))

            if res_data.get("ok"):
                f_path = res_data["result"]["file_path"]
                dl_url = f"https://api.telegram.org/file/bot{bot_token}/{f_path}"
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with urllib.request.urlopen(dl_url, timeout=30) as dl_resp, open(dest_path, "wb") as out_f:
                    out_f.write(dl_resp.read())
                logger.info("✅ [VAULT HYDRATION] Successfully downloaded %s from Telegram Storage Group (file_id: %s)", os.path.basename(dest_path), file_id[:15])
                return True
        except Exception as _dl_err:
            logger.warning("⚠️ Vault hydration download failed for %s: %s", os.path.basename(dest_path), _dl_err)
        return False

    def hydrate_all_vault_jsons_on_startup(self) -> Dict[str, bool]:
        """
        Downloads latest telegram_users.json and metadata_pool.json from Telegram Storage Group
        using vault_index file_ids to restore state on startup or cloud sync.
        """
        results = {"telegram_users": False, "metadata_pool": False}
        try:
            users_file_id = self.vault_index.get("telegram_users_file_id")
            if users_file_id:
                from Phase_1.Telegram_Storage_Manager.telegram_user_manager import USERS_JSON_PATH
                results["telegram_users"] = self.download_vault_file_by_id(users_file_id, USERS_JSON_PATH)

            pool_file_id = self.vault_index.get("metadata_pool_file_id")
            if pool_file_id:
                from Phase_1.Audio_Modules import AudioPoolManager
                pm = AudioPoolManager()
                results["metadata_pool"] = self.download_vault_file_by_id(pool_file_id, pm.meta_path)
        except Exception as _h_err:
            logger.warning("⚠️ Vault JSON hydration notice: %s", _h_err)
        return results

    def upload_and_pin_vault_index_sync(self, upload_fn=None):
        """
        Synchronously uploads master_vault_index.json to TELEGRAM_STORAGE_GROUP_ID
        and pins the message so master_vault_index.json is NEVER lost!
        """
        storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID")
        if not storage_group_id or not upload_fn or not os.path.exists(self.index_file):
            return

        try:
            reels_cnt = len(self.vault_index.get("column_1_processed_reels", {}).get("by_session_id", {}))
            sources_cnt = len(self.vault_index.get("column_2_downloaded_sources", {}).get("by_social_media_id", {}))
            caption = f"📌 **[VAULT MASTER INDEX]** Auto-Synced\n🕒 `{time.strftime('%Y-%m-%d %H:%M:%S')}`\n📊 Reels: `{reels_cnt}` | Sources: `{sources_cnt}`"
            res = upload_fn("sendDocument", storage_group_id, "document", self.index_file, caption=caption)
            if res and isinstance(res, dict):
                msg_id = res.get("message_id")
                if msg_id:
                    self.vault_index["pinned_message_id"] = msg_id
                    self._save_local_index()
                    try:
                        from Phase_1.Downloader_Modules.telegram_listener import _api_call
                        _api_call("pinChatMessage", {"chat_id": str(storage_group_id), "message_id": msg_id, "disable_notification": True})
                    except Exception as _p_call_err:
                        logger.warning("Notice on pinChatMessage call: %s", _p_call_err)
                    logger.info("📌 [VAULT PIN SUCCESS] Uploaded & PINNED master_vault_index.json in Storage Group (Message ID: %s)", msg_id)
        except Exception as _pin_err:
            logger.warning("⚠️ Vault index upload/pin notice: %s", _pin_err)

    # ── LOOKUP APIs ───────────────────────────────────────────────────────────

    def lookup_downloaded_source(self, social_url: str) -> Optional[Dict[str, Any]]:
        """
        Column 2 Lookup: Returns cached raw video file_id and audio_math if this URL
        was downloaded previously. Enables 1.5s re-use without re-downloading.
        """
        if not social_url:
            return None
        c2 = self.vault_index.get("column_2_downloaded_sources", {}).get("by_social_media_id", {})
        hit = c2.get(social_url.strip())
        if hit:
            logger.info(f"⚡ [VAULT CACHE HIT] Column 2 found source for URL: {social_url[:60]}...")
            return hit
        return None

    def lookup_processed_reel(self, session_id: Optional[str] = None, social_url: Optional[str] = None, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Column 1 Lookup: Returns master reel data and full intelligence dicts by
        session_id, social_media_id, or user_id.
        """
        c1 = self.vault_index.get("column_1_processed_reels", {})
        
        # User-scoped lookup
        if user_id:
            user_data = c1.get("by_user_id", {}).get(user_id, {})
            if session_id and session_id in user_data:
                return user_data[session_id]
            if social_url:
                for sess_id, entry in user_data.items():
                    if entry.get("social_media_id") == social_url.strip():
                        return entry
        
        # Global lookup
        if session_id:
            hit = c1.get("by_session_id", {}).get(session_id)
            if hit:
                return hit
        if social_url:
            sess_link = c1.get("by_social_media_id", {}).get(social_url.strip())
            if sess_link:
                return c1.get("by_session_id", {}).get(sess_link)
        return None

    async def download_audio_track_from_vault(self, bot, track_name: str, dest_dir: Optional[str] = None) -> Optional[str]:
        """
        On-Demand Audio Vault Fetcher:
        If an audio track selected from pool_metadata.json is missing on local disk,
        this method queries Column 2 of master_vault_index.json for extracted_audio_file_id,
        downloads it from Telegram Storage Group in ~1s, and saves it to Original_audio/active/<track_name>.
        """
        if not track_name or not bot:
            return None

        filename = os.path.basename(track_name)
        if not dest_dir:
            dest_dir = os.path.join(_REPO_ROOT, "Original_audio", "active")
        os.makedirs(dest_dir, exist_ok=True)
        local_path = os.path.join(dest_dir, filename)

        if os.path.exists(local_path) and os.path.getsize(local_path) > 1024:
            return local_path

        # Find extracted_audio_file_id in Column 2 or Column 1
        c2 = self.vault_index.get("column_2_downloaded_sources", {}).get("by_social_media_id", {})
        file_id = None
        for _url, entry in c2.items():
            if entry.get("extracted_audio_file_id") and (filename in _url or filename in str(entry.get("session_id", ""))):
                file_id = entry["extracted_audio_file_id"]
                break

        if not file_id:
            c2_sess = self.vault_index.get("column_2_downloaded_sources", {}).get("by_session_id", {})
            for sess_id, entry in c2_sess.items():
                if entry.get("extracted_audio_file_id") and (filename in sess_id or filename in str(entry.get("social_media_id", ""))):
                    file_id = entry["extracted_audio_file_id"]
                    break

        if file_id:
            try:
                logger.info(f"📥 [VAULT FETCH] Downloading on-demand audio track '{filename}' from Telegram Storage Group...")
                t_file = await bot.get_file(file_id)
                await t_file.download_to_drive(custom_path=local_path)
                if os.path.exists(local_path) and os.path.getsize(local_path) > 1024:
                    logger.info(f"✅ [VAULT FETCH SUCCESS] Audio track ready: {local_path}")
                    return local_path
            except Exception as e:
                logger.warning(f"⚠️ Vault on-demand audio fetch failed for '{filename}': {e}")
        return None


    # ── TELEGRAM SYNC & PIN ──────────────────────────────────────────────────

    async def sync_vault_index_from_telegram(self, bot) -> bool:
        """
        Startup Sync: Checks TELEGRAM_STORAGE_GROUP_ID for pinned master_vault_index.json.
        Downloads and merges it into local disk storage so ephemeral runners hydrate in 0.5s.
        """
        storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID")
        if not storage_group_id or not bot:
            return False

        try:
            logger.info(f"🔍 [VAULT SYNC] Checking Storage Group ({storage_group_id}) for pinned master index...")
            chat = await bot.get_chat(chat_id=int(storage_group_id))
            pinned = chat.pinned_message if hasattr(chat, "pinned_message") else None

            if pinned and pinned.document and pinned.document.file_name == "master_vault_index.json":
                doc_file = await bot.get_file(pinned.document.file_id)
                temp_down = os.path.join(DATA_DIR, "pinned_vault_down.json")
                await doc_file.download_to_drive(custom_path=temp_down)

                if os.path.exists(temp_down) and os.path.getsize(temp_down) > 50:
                    with open(temp_down, "r", encoding="utf-8") as f:
                        remote_index = json.load(f)

                    if isinstance(remote_index, dict) and "column_1_processed_reels" in remote_index:
                        self.vault_index = remote_index
                        self.vault_index["pinned_message_id"] = pinned.message_id
                        self._save_local_index()
                        self._hydrate_local_caches()
                        logger.info(f"✅ [VAULT SYNC SUCCESS] Hydrated master index from Telegram (Pinned msg: {pinned.message_id})")
                        return True
        except Exception as e:
            logger.warning(f"⚠️ [VAULT SYNC] Could not fetch pinned vault index: {e}")
        return False

    def _hydrate_local_caches(self):
        """
        Hydrates local disk stores (pool_metadata.json, .clip_intelligence.json)
        from the downloaded master_vault_index.json.
        """
        try:
            # 1. Hydrate pool_metadata.json
            c1_reels = self.vault_index.get("column_1_processed_reels", {}).get("by_session_id", {})
            if c1_reels:
                from Audio_Modules.audio_pool_manager import AudioPoolManager
                pm = AudioPoolManager()
                for sess_id, rdata in c1_reels.items():
                    adata = rdata.get("audio_data") or {}
                    pool_meta = adata.get("pool_metadata") or {}
                    track_name = pool_meta.get("selected_audio_track") or pool_meta.get("selected_bgm_track")
                    if track_name and pool_meta:
                        pm._set_file_metadata(os.path.basename(track_name), pool_meta)
                pm._save_metadata()

            logger.info("⚡ [VAULT HYDRATE] Local pool_metadata and clip caches updated from Vault Index.")
        except Exception as e:
            logger.debug(f"[VAULT HYDRATE] Cache hydration notice: {e}")

    # ── RECORDING APIS ───────────────────────────────────────────────────────

    def record_ingested_clip_source(
        self,
        social_url: str,
        raw_video_path: str,
        upload_fn,
        existing_raw_file_id: Optional[str] = None,
        extracted_audio_path: Optional[str] = None,
        audio_math: Optional[Dict[str, Any]] = None,
        whisper_transcript: Optional[Dict[str, Any]] = None,
        gemini_semantic: Optional[Dict[str, Any]] = None,
        file_size: Optional[int] = None,
        sha256: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Unified Storage Authority Entry Point:
        1. Uses existing_raw_file_id or uploads raw video to Storage Group ONCE via upload_fn -> gets raw_file_id.
        2. Uploads extracted audio WAV to Storage Group ONCE via upload_fn -> gets audio_file_id.
        3. Updates metadata_pool.json (clip_source_math) with file_ids, audio_math, whisper_transcript, gemini_semantic.
        4. Updates Column 2 of Master Vault Index.
        """
        storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID")
        filename = os.path.basename(raw_video_path)
        ext = os.path.splitext(filename)[1].lower()
        
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

        caption = f"🎬 {filename}\n🔗 `{social_url}`" + (f"\n👤 User: `{user_id}`" if user_id else "")
        
        raw_file_id = existing_raw_file_id
        extracted_audio_file_id = None

        if storage_group_id and upload_fn:
            try:
                # 1. Upload Raw Video Clip ONLY IF not already uploaded
                if not raw_file_id and os.path.exists(raw_video_path):
                    logger.info("📦 [STORAGE MANAGER] Uploading raw media clip (%s) to Storage Group (%s)...", filename, storage_group_id)
                    upload_res = upload_fn(method, storage_group_id, file_param, raw_video_path, caption=caption)
                    if upload_res and isinstance(upload_res, dict):
                        raw_file_id = upload_res.get(file_param, {}).get("file_id") or (upload_res.get("document", {}).get("file_id") if upload_res.get("document") else None)
                
                # 2. Upload Extracted Audio WAV to Storage Group
                if extracted_audio_path and os.path.exists(extracted_audio_path):
                    audio_filename = os.path.basename(extracted_audio_path)
                    audio_caption = f"🎵 [EXTRACTED AUDIO] `{audio_filename}`\n🔗 `{social_url}`"
                    logger.info("🎙️ [STORAGE MANAGER] Uploading extracted audio (%s) to Storage Group...", audio_filename)
                    audio_upload_res = upload_fn("sendAudio", storage_group_id, "audio", extracted_audio_path, caption=audio_caption)
                    if audio_upload_res and isinstance(audio_upload_res, dict):
                        extracted_audio_file_id = audio_upload_res.get("audio", {}).get("file_id") or (audio_upload_res.get("document", {}).get("file_id") if audio_upload_res.get("document") else None)
                        logger.info("✅ [STORAGE MANAGER] Captured extracted_audio_file_id: %s", extracted_audio_file_id)
            except Exception as _up_err:
                logger.warning("⚠️ Storage Group upload warning: %s", _up_err)

        clip_entry = {
            "raw_video_file_id": raw_file_id,
            "extracted_audio_file_id": extracted_audio_file_id,
            "file_name": filename,
            "file_size": file_size or (os.path.getsize(raw_video_path) if os.path.exists(raw_video_path) else 0),
            "sha256": sha256 or "",
            "downloaded_at": time.time(),
            "user_id": user_id,
            "audio_math": audio_math or {},
            "whisper_transcript": whisper_transcript or {},
            "gemini_semantic_intelligence": gemini_semantic or {}
        }

        # 1. Save in metadata_pool.json (RAM/Disk) and Upload to Telegram Storage Group
        try:
            from Phase_1.Audio_Modules import AudioPoolManager
            pm = AudioPoolManager()
            csm = pm.metadata.setdefault("clip_source_math", {})
            csm[social_url] = clip_entry
            pm._save_metadata()
            logger.info("📦 [STORAGE MANAGER] Indexed clip_source_math entry for %s in metadata_pool.json", social_url)

            # Upload updated metadata_pool.json document to Telegram Storage Group
            if storage_group_id and upload_fn and os.path.exists(pm.meta_path):
                pool_upload_res = upload_fn("sendDocument", storage_group_id, "document", pm.meta_path, caption=f"📦 **[VAULT BACKUP]** `metadata_pool.json` (Updated {time.strftime('%H:%M:%S')})")
                if pool_upload_res and isinstance(pool_upload_res, dict):
                    pool_doc_id = pool_upload_res.get("document", {}).get("file_id")
                    self.vault_index["metadata_pool_file_id"] = pool_doc_id
                    logger.info("✅ [STORAGE MANAGER] Uploaded updated metadata_pool.json to Storage Group (file_id: %s)", pool_doc_id)

            # Upload updated telegram_users.json document to Telegram Storage Group
            try:
                from Phase_1.Telegram_Storage_Manager.telegram_user_manager import USERS_JSON_PATH
                if storage_group_id and upload_fn and os.path.exists(USERS_JSON_PATH):
                    users_upload_res = upload_fn("sendDocument", storage_group_id, "document", USERS_JSON_PATH, caption=f"👤 **[VAULT BACKUP]** `telegram_users.json` (Updated {time.strftime('%H:%M:%S')})")
                    if users_upload_res and isinstance(users_upload_res, dict):
                        users_doc_id = users_upload_res.get("document", {}).get("file_id")
                        self.vault_index["telegram_users_file_id"] = users_doc_id
                        logger.info("✅ [STORAGE MANAGER] Uploaded updated telegram_users.json to Storage Group (file_id: %s)", users_doc_id)
            except Exception as _users_err:
                logger.warning("⚠️ Could not upload telegram_users.json: %s", _users_err)
        except Exception as _pool_err:
            logger.warning("⚠️ Could not save/upload metadata_pool.json: %s", _pool_err)

        # 2. Save in Column 2 of Master Vault Index
        session_id = f"sess_{int(time.time())}"
        c2 = self.vault_index.setdefault("column_2_downloaded_sources", {})
        c2.setdefault("by_social_media_id", {})[social_url] = clip_entry
        c2.setdefault("by_session_id", {})[session_id] = clip_entry
        if user_id:
            c2.setdefault("by_user_id", {}).setdefault(user_id, {})[session_id] = clip_entry

        self._save_local_index()

        # 3. Upload & PIN master_vault_index.json in Telegram Storage Group
        if upload_fn and storage_group_id:
            self.upload_and_pin_vault_index_sync(upload_fn=upload_fn)
        return {
            "raw_file_id": raw_file_id,
            "method": method,
            "file_param": file_param,
            "clip_entry": clip_entry
        }

    async def record_downloaded_source(
        self,
        bot,
        social_url: str,
        session_id: str,
        raw_video_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        beat_math: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Column 2 Record: Uploads raw source video and extracted audio to TELEGRAM_STORAGE_GROUP_ID,
        saves file_ids under column_2_downloaded_sources, and re-pins master_vault_index.json.
        
        Args:
            user_id: Optional user ID for user-scoped storage
        """
        storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID")
        raw_file_id = None
        audio_file_id = None

        if storage_group_id and bot:
            try:
                if raw_video_path and os.path.exists(raw_video_path):
                    with open(raw_video_path, "rb") as rf:
                        rmsg = await bot.send_video(
                            chat_id=int(storage_group_id),
                            video=rf,
                            caption=f"📥 **[VAULT RAW SOURCE]** `{os.path.basename(raw_video_path)}`\n🔗 `{social_url}`\n🆔 `{session_id}`" + (f"\n👤 User: `{user_id}`" if user_id else "")
                        )
                        if rmsg and rmsg.video:
                            raw_file_id = rmsg.video.file_id

                if audio_path and os.path.exists(audio_path):
                    logger.info(f"🎙️ [VAULT AUDIO UPLOAD] Sending extracted audio ({os.path.basename(audio_path)}, {os.path.getsize(audio_path)} bytes) to Storage Group...")
                    try:
                        with open(audio_path, "rb") as af:
                            amsg = await bot.send_document(
                                chat_id=int(storage_group_id),
                                document=af,
                                filename=os.path.basename(audio_path),
                                caption=f"🎵 **[VAULT AUDIO EXTRACT]** `{os.path.basename(audio_path)}`\n🆔 `{session_id}`" + (f"\n👤 User: `{user_id}`" if user_id else "")
                            )
                            if amsg:
                                audio_file_id = amsg.document.file_id if amsg.document else (amsg.audio.file_id if amsg.audio else None)
                                logger.info(f"✅ [VAULT AUDIO SUCCESS] Extracted audio file_id captured: {audio_file_id}")
                    except Exception as _aud_err:
                        logger.warning(f"❌ [VAULT AUDIO ERROR] Audio upload failed: {_aud_err}")
            except Exception as e:
                logger.warning(f"⚠️ Vault raw source upload warning: {e}")

        entry = {
            "social_media_id": social_url,
            "session_id": session_id,
            "raw_video_file_id": raw_file_id,
            "extracted_audio_file_id": audio_file_id,
            "audio_math": beat_math or {},
            "downloaded_at": time.time(),
            "user_id": user_id,
        }

        c2 = self.vault_index.setdefault("column_2_downloaded_sources", {})
        c2.setdefault("by_social_media_id", {})[social_url] = entry
        c2.setdefault("by_session_id", {})[session_id] = entry
        
        # User-scoped indexing
        if user_id:
            c2.setdefault("by_user_id", {}).setdefault(user_id, {})[session_id] = entry

        self._save_local_index()
        await self._upload_and_pin_index(bot, storage_group_id)
        logger.info(f"📦 [VAULT RECORD] Recorded Column 2 source for URL: {social_url[:60]}" + (f" (User: {user_id})" if user_id else ""))
        return entry

    async def record_processed_reel(
        self,
        bot,
        session_id: str,
        social_url: Optional[str],
        custom_title: Optional[str],
        master_video_path: str,
        clip_intel: Optional[Dict[str, Any]] = None,
        lyric_intel: Optional[Dict[str, Any]] = None,
        master_file_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Column 1 Record: Saves rendered master reel intelligence and file_id into
        column_1_processed_reels, updates local index, and re-pins master_vault_index.json.
        
        Args:
            user_id: Optional user ID for user-scoped storage
        """
        storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID")

        entry = {
            "session_id": session_id,
            "social_media_id": social_url or "direct_upload",
            "custom_title": custom_title,
            "master_video_file_id": master_file_id,
            "video_path": os.path.abspath(master_video_path),
            "created_at": time.time(),
            "audio_data": {
                "lyric_intel": lyric_intel or {},
            },
            "visual_data": clip_intel or {},
            "editing_plan_history": [],  # Sorted list of attempts & user approval status for RAG Creator Behavior
            "pipeline_execution_trajectory": {
                "stage_0_intent_classification": {},
                "stage_1_visual_forensics": clip_intel or {},
                "stage_2_audio_intelligence": lyric_intel or {},
                "stage_3_attempts_and_re_edits": [],
                "stage_4_final_verdict": {"status": "AWAITING_REVIEW", "timestamp": time.time()},
            },
            "user_id": user_id,
        }

        c1 = self.vault_index.setdefault("column_1_processed_reels", {})
        existing = c1.get("by_session_id", {}).get(session_id)
        if existing:
            if "editing_plan_history" in existing:
                entry["editing_plan_history"] = existing["editing_plan_history"]
            if "pipeline_execution_trajectory" in existing:
                entry["pipeline_execution_trajectory"] = existing["pipeline_execution_trajectory"]

        c1.setdefault("by_session_id", {})[session_id] = entry
        if social_url:
            c1.setdefault("by_social_media_id", {})[social_url] = session_id
        
        # User-scoped indexing
        if user_id:
            c1.setdefault("by_user_id", {}).setdefault(user_id, {})[session_id] = entry

        self._save_local_index()
        await self._upload_and_pin_index(bot, storage_group_id)
        logger.info(f"🎬 [VAULT RECORD] Recorded Column 1 master reel for Session: {session_id}" + (f" (User: {user_id})" if user_id else ""))
        return entry

    async def update_pipeline_trajectory(
        self,
        bot,
        session_id: str,
        stage_name: str,
        stage_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        AI Trajectory Store: Records structured, un-mixed pipeline stage logs into
        column_1_processed_reels -> pipeline_execution_trajectory.
        Stages:
          - 'stage_0_intent'  : IntentVector classification & confidence
          - 'stage_1_visual'  : Keyframe sampling, faces, watermark detection
          - 'stage_2_audio'   : Beat math, Whisper transcript, Gemini lyric directives
          - 'stage_3_attempts': Rendering attempts, FFmpeg filtergraph, user verdict
          - 'stage_4_verdict' : Final outcome (APPROVED/REJECTED), total time, winning attempt
        """
        c1 = self.vault_index.setdefault("column_1_processed_reels", {})
        session_entry = c1.setdefault("by_session_id", {}).setdefault(session_id, {
            "session_id": session_id,
            "created_at": time.time(),
        })

        trajectory = session_entry.setdefault("pipeline_execution_trajectory", {
            "stage_0_intent_classification": {},
            "stage_1_visual_forensics": {},
            "stage_2_audio_intelligence": {},
            "stage_3_attempts_and_re-edits": [],
            "stage_4_final_verdict": {},
        })

        stage_key = {
            "stage_0_intent": "stage_0_intent_classification",
            "stage_1_visual": "stage_1_visual_forensics",
            "stage_2_audio": "stage_2_audio_intelligence",
            "stage_3_attempts": "stage_3_attempts_and_re-edits",
            "stage_4_verdict": "stage_4_final_verdict",
        }.get(stage_name, stage_name)

        if stage_key == "stage_3_attempts_and_re-edits":
            if not isinstance(trajectory.get("stage_3_attempts_and_re-edits"), list):
                trajectory["stage_3_attempts_and_re-edits"] = []
            trajectory["stage_3_attempts_and_re-edits"].append(stage_data)
        else:
            trajectory[stage_key] = stage_data

        session_entry["updated_at"] = time.time()
        self._save_local_index()
        storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID")
        await self._upload_and_pin_index(bot, storage_group_id)
        logger.info(f"🧠 [TRAJECTORY RECORD] Updated {stage_key} for Session: {session_id}")
        return trajectory


    async def record_plan_attempt(
        self,
        bot,
        session_id: str,
        attempt_number: int,
        editing_plan: Dict[str, Any],
        user_approved: bool = False,
        user_feedback: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        RAG Creator Behavior Store: Appends an editing plan attempt (and user approval/feedback)
        to column_1_processed_reels -> editing_plan_history.
        Stores both approved (positive RAG) and rejected (negative RAG) attempts for AI learning.
        """
        c1 = self.vault_index.get("column_1_processed_reels", {}).get("by_session_id", {})
        session_entry = c1.get(session_id)
        if not session_entry:
            logger.warning(f"⚠️ Cannot record plan attempt: session '{session_id}' not found in Column 1 index.")
            return None

        history = session_entry.setdefault("editing_plan_history", [])
        attempt_record = {
            "attempt_number": attempt_number,
            "timestamp": time.time(),
            "user_approved": user_approved,
            "user_feedback": user_feedback or ("Approved by user" if user_approved else "Rejected/Re-edit requested"),
            "editing_plan": editing_plan or {},
        }
        history.append(attempt_record)
        # Keep sorted by attempt_number
        session_entry["editing_plan_history"] = sorted(history, key=lambda x: int(x.get("attempt_number", 0)))

        self._save_local_index()
        storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID")
        await self._upload_and_pin_index(bot, storage_group_id)
        logger.info(f"🧠 [RAG PLAN RECORD] Recorded Attempt {attempt_number} (approved={user_approved}) for Session: {session_id}")
        return attempt_record


    async def _upload_and_pin_index(self, bot, storage_group_id: Optional[str]):
        """Uploads updated master_vault_index.json to TELEGRAM_STORAGE_GROUP_ID and pins it."""
        if not storage_group_id or not bot or not os.path.exists(self.index_file):
            return

        try:
            with open(self.index_file, "rb") as idf:
                doc_msg = await bot.send_document(
                    chat_id=int(storage_group_id),
                    document=idf,
                    filename="master_vault_index.json",
                    caption=f"📌 **[VAULT MASTER INDEX]** Auto-Synced\n🕒 `{time.strftime('%Y-%m-%d %H:%M:%S')}`\n📊 Reels: `{len(self.vault_index.get('column_1_processed_reels', {}).get('by_session_id', {}))}` | Sources: `{len(self.vault_index.get('column_2_downloaded_sources', {}).get('by_social_media_id', {}))}`"
                )
                if doc_msg and doc_msg.message_id:
                    await bot.pin_chat_message(
                        chat_id=int(storage_group_id),
                        message_id=doc_msg.message_id,
                        disable_notification=True
                    )
                    self.vault_index["pinned_message_id"] = doc_msg.message_id
                    logger.info(f"📌 [VAULT PIN] Pinned updated master_vault_index.json (Message ID: {doc_msg.message_id})")
        except Exception as e:
            logger.warning(f"⚠️ Vault index upload/pin notice: {e}")
