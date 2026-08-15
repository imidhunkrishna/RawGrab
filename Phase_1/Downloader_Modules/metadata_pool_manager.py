"""
Tools/Downloader_Modules/metadata_pool_manager.py
===================================================
Manages persistent metadata pool in Tools/Original_audio/metadata_pool.json.
Tracks processed YouTube reference clips with details:
  - content_id
  - channel_name
  - manga_name
  - chapter_or_vol
  - language
  - title
  - url
  - downloaded_at
  - video_path
  - audio_path
Prevents duplicate video downloads and duplicate processing.
"""

import os
import json
import time
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("metadata_pool_manager")

METADATA_POOL_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "Original_audio", "metadata_pool.json")
)


class MetadataPoolManager:
    def __init__(self, json_path: str = METADATA_POOL_FILE):
        self.json_path = json_path
        os.makedirs(os.path.dirname(self.json_path), exist_ok=True)
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"⚠️ Could not load {self.json_path}: {e}")
        return {}

    def _save(self):
        try:
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"❌ Failed to save {self.json_path}: {e}")

    def find_cached_clip(
        self,
        content_id: Optional[str] = None,
        url: Optional[str] = None,
        manga_name: Optional[str] = None,
        chapter_or_vol: Optional[str] = None,
        language: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Checks if clip matching content_id, url, or (manga_name + chapter_or_vol + language)
        already exists in metadata_pool.json and its files exist on disk.
        """
        for cid, record in self._data.items():
            # 1. Direct Content ID or URL match
            if content_id and (cid == content_id or record.get("content_id") == content_id):
                if self._verify_record_files(record):
                    return record
            if url and record.get("url") == url:
                if self._verify_record_files(record):
                    return record

            # 2. Topic/Niche metadata match (manga + chapter + language)
            if (
                manga_name and chapter_or_vol and language
                and record.get("manga_name", "").lower() == manga_name.lower()
                and str(record.get("chapter_or_vol", "")).lower() == str(chapter_or_vol).lower()
                and record.get("language", "").lower() == language.lower()
            ):
                if self._verify_record_files(record):
                    return record

        return None

    def _verify_record_files(self, record: Dict[str, Any]) -> bool:
        vpath = record.get("video_path")
        apath = record.get("audio_path")
        if vpath and os.path.exists(vpath):
            return True
        if apath and os.path.exists(apath):
            return True
        return False

    def record_clip(
        self,
        content_id: str,
        channel_name: str,
        manga_name: str,
        chapter_or_vol: str,
        language: str,
        title: str,
        url: str,
        video_path: Optional[str] = None,
        audio_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Records or updates a processed clip in metadata_pool.json.
        """
        record = {
            "content_id": content_id,
            "channel_name": channel_name,
            "manga_name": manga_name,
            "chapter_or_vol": str(chapter_or_vol),
            "language": language,
            "title": title,
            "url": url,
            "video_path": os.path.abspath(video_path) if video_path else None,
            "audio_path": os.path.abspath(audio_path) if audio_path else None,
            "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        self._data[content_id] = record
        self._save()
        logger.info(f"💾 Recorded metadata entry in {self.json_path} for '{title}' (ID: {content_id})")
        return record


# Global Singleton
metadata_pool_manager = MetadataPoolManager()
