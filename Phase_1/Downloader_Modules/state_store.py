"""
state_store.py — Thread-Safe SQLite State & Telemetry Store
=============================================================
Replaces multi-file JSON state with an atomic, thread-safe SQLite database:

  - SHA256 Processed Media Cache (`processed_cache` table)
  - Account Rotation Pointer State (`rotation_state` table)
  - Telemetry Event Logging (`telemetry_events` table)
"""

import os
import sqlite3
import time
import json
import logging
from typing import Optional, Dict, Any, List, Tuple

from contextlib import contextmanager
import threading

logger = logging.getLogger("state_store")

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DB_PATH = os.path.join(_REPO_ROOT, "storage", "amtce_state.db")


class StateStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_tables()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS processed_cache (
                        sha256 TEXT PRIMARY KEY,
                        safe_path TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        mime_type TEXT,
                        file_size INTEGER,
                        timestamp REAL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS rotation_state (
                        key TEXT PRIMARY KEY,
                        pointer INTEGER NOT NULL,
                        last_selected TEXT,
                        timestamp REAL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS telemetry_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        latency_ms REAL,
                        file_size INTEGER,
                        sha256 TEXT,
                        success INTEGER NOT NULL DEFAULT 1,
                        details TEXT,
                        timestamp REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS manga_analysis_cache (
                        cache_key TEXT PRIMARY KEY,
                        result_json TEXT NOT NULL,
                        model TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        analysis_resolution INTEGER NOT NULL,
                        page_count INTEGER NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS story_bibles (
                        bible_key TEXT PRIMARY KEY,
                        chapter_id TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        title TEXT,
                        total_pages INTEGER NOT NULL,
                        synthesis_method TEXT NOT NULL,
                        model TEXT,
                        prompt_version TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        bible_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS creator_profiles (
                        creator_id TEXT NOT NULL,
                        chapter_id TEXT NOT NULL,
                        profile_key TEXT NOT NULL,
                        profile_version TEXT NOT NULL,
                        video_count INTEGER NOT NULL,
                        profile_json TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (creator_id, chapter_id)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS creator_registry (
                        creator_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL DEFAULT 'youtube',
                        channel_id TEXT NOT NULL,
                        channel_name TEXT NOT NULL,
                        language TEXT NOT NULL DEFAULT 'ml',
                        content_niche TEXT NOT NULL DEFAULT 'general',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        human_rating INTEGER NOT NULL DEFAULT 5,
                        added_via TEXT NOT NULL DEFAULT 'admin',
                        created_at REAL NOT NULL
                    )
                """)
                # Migrations: Add content_niche column if missing
                try:
                    conn.execute("ALTER TABLE creator_registry ADD COLUMN content_niche TEXT NOT NULL DEFAULT 'general'")
                except Exception:
                    pass

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS reference_discovery_cache (
                        discovery_key TEXT PRIMARY KEY,
                        series TEXT NOT NULL,
                        chapter_id TEXT NOT NULL,
                        language TEXT NOT NULL,
                        results_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS synthesized_profiles (
                        fusion_key TEXT PRIMARY KEY,
                        chapter_id TEXT NOT NULL,
                        algorithm_version TEXT NOT NULL,
                        profile_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                # Auto-migrate telemetry_events table if missing sha256 or success columns
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(telemetry_events)")
                existing_cols = {row[1] for row in cursor.fetchall()}
                if "sha256" not in existing_cols:
                    conn.execute("ALTER TABLE telemetry_events ADD COLUMN sha256 TEXT")
                if "success" not in existing_cols:
                    conn.execute("ALTER TABLE telemetry_events ADD COLUMN success INTEGER NOT NULL DEFAULT 1")
        except Exception as exc:
            logger.error(f"❌ Failed to initialize SQLite state store: {exc}")

    # ---------------------------------------------------------------------------
    # SHA256 Processed Cache Operations
    # ---------------------------------------------------------------------------
    def get_cached_media(self, sha256: str) -> Optional[Dict[str, Any]]:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT safe_path, media_type, mime_type, file_size, timestamp FROM processed_cache WHERE sha256 = ?",
                    (sha256,)
                )
                row = cursor.fetchone()
                if row:
                    safe_path, media_type, mime_type, file_size, ts = row
                    if safe_path and os.path.exists(safe_path):
                        return {
                            "safe_path": safe_path,
                            "media_type": media_type,
                            "mime_type": mime_type,
                            "file_size": file_size,
                            "timestamp": ts
                        }
        except Exception as exc:
            logger.warning(f"Cache lookup failed for {sha256}: {exc}")
        return None

    def save_cached_media(self, sha256: str, safe_path: str, media_type: str, mime_type: str, file_size: int, director_json: Optional[str] = None) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO processed_cache (sha256, safe_path, media_type, mime_type, file_size, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (sha256, safe_path, media_type, mime_type, file_size, time.time())
                )
        except Exception as exc:
            logger.warning(f"Failed to save cached media {sha256}: {exc}")

    # ---------------------------------------------------------------------------
    # Rotation State Pointer Operations
    # ---------------------------------------------------------------------------
    def get_rotation_pointer(self, key: str = "paparazzi") -> Tuple[int, List[str]]:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT pointer, last_selected FROM rotation_state WHERE key = ?", (key,))
                row = cursor.fetchone()
                if row:
                    pointer, last_sel_json = row
                    last_selected = json.loads(last_sel_json) if last_sel_json else []
                    return pointer, last_selected
        except Exception as exc:
            logger.warning(f"Failed to load rotation pointer: {exc}")
        return 0, []

    def set_rotation_pointer(self, key: str, pointer: int, last_selected: List[str]) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rotation_state (key, pointer, last_selected, timestamp)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, pointer, json.dumps(last_selected), time.time())
                )
        except Exception as exc:
            logger.warning(f"Failed to set rotation pointer: {exc}")

    # ---------------------------------------------------------------------------
    # Telemetry Metric Operations
    # ---------------------------------------------------------------------------
    def record_telemetry(self, event_type: str, latency_ms: float, file_size: int = 0, sha256: str = "", success: bool = True, details: str = "") -> None:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO telemetry_events (event_type, latency_ms, file_size, sha256, success, details, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_type, latency_ms, file_size, sha256, 1 if success else 0, details, time.time())
                )
        except Exception as exc:
            logger.warning(f"Failed to record telemetry: {exc}")
    # ---------------------------------------------------------------------------
    # Manga Vision Analysis Cache Operations
    # ---------------------------------------------------------------------------
    def get_manga_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT result_json FROM manga_analysis_cache WHERE cache_key = ?",
                    (cache_key,)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception as exc:
            logger.warning(f"Manga cache lookup failed for {cache_key}: {exc}")
        return None

    def save_manga_cache(self, cache_key: str, result_dict: Dict[str, Any], model: str, prompt_version: str, schema_version: str, resolution: int, page_count: int) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO manga_analysis_cache
                    (cache_key, result_json, model, prompt_version, schema_version, analysis_resolution, page_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        result_json = excluded.result_json,
                        model = excluded.model,
                        prompt_version = excluded.prompt_version,
                        schema_version = excluded.schema_version,
                        analysis_resolution = excluded.analysis_resolution,
                        page_count = excluded.page_count,
                        created_at = excluded.created_at
                    """,
                    (cache_key, json.dumps(result_dict), model, prompt_version, schema_version, resolution, page_count, time.time())
                )
        except Exception as exc:
            logger.warning(f"Failed to save manga cache {cache_key}: {exc}")

    # ---------------------------------------------------------------------------
    # Story Bible Persistence Operations
    # ---------------------------------------------------------------------------
    def save_story_bible(
        self,
        bible_key: str,
        chapter_id: str,
        source_hash: str,
        title: str,
        total_pages: int,
        synthesis_method: str,
        model: Optional[str],
        prompt_version: str,
        schema_version: str,
        bible_json: str
    ) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO story_bibles
                    (bible_key, chapter_id, source_hash, title, total_pages, synthesis_method, model, prompt_version, schema_version, bible_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bible_key) DO UPDATE SET
                        chapter_id = excluded.chapter_id,
                        source_hash = excluded.source_hash,
                        title = excluded.title,
                        total_pages = excluded.total_pages,
                        synthesis_method = excluded.synthesis_method,
                        model = excluded.model,
                        prompt_version = excluded.prompt_version,
                        schema_version = excluded.schema_version,
                        bible_json = excluded.bible_json,
                        created_at = excluded.created_at
                    """,
                    (bible_key, chapter_id, source_hash, title, total_pages, synthesis_method, model or "", prompt_version, schema_version, bible_json, time.time())
                )
        except Exception as exc:
            logger.warning(f"Failed to save story bible {bible_key}: {exc}")

    def get_story_bible(self, bible_key: str) -> Optional[Dict[str, Any]]:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT bible_json FROM story_bibles WHERE bible_key = ?",
                    (bible_key,)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception as exc:
            logger.warning(f"Story Bible lookup failed for {bible_key}: {exc}")
        return None

    def get_latest_bible(self, chapter_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the newest, highest-priority Story Bible for a given chapter_id.
        Prioritizes 'gemini' synthesis over 'rule_based' fallbacks using SQL CASE ordering.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT bible_json FROM story_bibles
                    WHERE chapter_id = ?
                    ORDER BY
                        CASE synthesis_method
                            WHEN 'gemini' THEN 2
                            WHEN 'rule_based' THEN 1
                            ELSE 0
                        END DESC,
                        created_at DESC
                    LIMIT 1
                    """,
                    (chapter_id,)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception as exc:
            logger.warning(f"Latest Story Bible lookup failed for chapter {chapter_id}: {exc}")
        return None

    # ---------------------------------------------------------------------------
    # Creator Profiles SQLite Operations
    # ---------------------------------------------------------------------------
    def save_creator_profile(
        self,
        creator_id: str,
        chapter_id: str,
        profile_key: str,
        profile_version: str,
        video_count: int,
        profile_json: str
    ) -> bool:
        """Saves or updates a creator behavior profile in SQLite."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO creator_profiles (
                        creator_id, chapter_id, profile_key, profile_version,
                        video_count, profile_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(creator_id, chapter_id) DO UPDATE SET
                        profile_key = excluded.profile_key,
                        profile_version = excluded.profile_version,
                        video_count = excluded.video_count,
                        profile_json = excluded.profile_json,
                        updated_at = excluded.updated_at
                    """,
                    (creator_id, chapter_id, profile_key, profile_version, video_count, profile_json, time.time())
                )
                logger.info(f"💾 [SQLITE] Saved Creator Profile '{creator_id}' (chapter '{chapter_id}') to state store.")
                return True
        except Exception as exc:
            logger.error(f"Failed to save Creator Profile ({creator_id}, {chapter_id}): {exc}")
            return False

    def get_creator_profile(self, creator_id: str, chapter_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves specific Creator Profile by (creator_id, chapter_id)."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT profile_json FROM creator_profiles
                    WHERE creator_id = ? AND chapter_id = ?
                    LIMIT 1
                    """,
                    (creator_id, chapter_id)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception as exc:
            logger.warning(f"Creator Profile lookup failed for ({creator_id}, {chapter_id}): {exc}")
        return None

    def get_creator_profiles_by_chapter(self, chapter_id: str) -> List[Dict[str, Any]]:
        """Retrieves all Creator Profiles associated with a specific chapter for downstream FusionEngine."""
        results = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT profile_json FROM creator_profiles
                    WHERE chapter_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (chapter_id,)
                )
                rows = cursor.fetchall()
                for r in rows:
                    if r and r[0]:
                        results.append(json.loads(r[0]))
        except Exception as exc:
            logger.warning(f"Profiles query failed for chapter {chapter_id}: {exc}")
        return results

    def add_creator_to_registry(
        self,
        creator_id: str,
        channel_id: str,
        channel_name: str,
        platform: str = "youtube",
        language: str = "ml",
        content_niche: str = "general",
        enabled: bool = True,
        human_rating: int = 5,
        added_via: str = "admin"
    ) -> bool:
        """Upserts creator entry into creator_registry table."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO creator_registry (
                        creator_id, platform, channel_id, channel_name, language, content_niche, enabled, human_rating, added_via, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(creator_id) DO UPDATE SET
                        platform=excluded.platform,
                        channel_id=excluded.channel_id,
                        channel_name=excluded.channel_name,
                        language=excluded.language,
                        content_niche=excluded.content_niche,
                        enabled=excluded.enabled,
                        human_rating=excluded.human_rating,
                        added_via=excluded.added_via
                    """,
                    (creator_id, platform, channel_id, channel_name, language, content_niche, 1 if enabled else 0, human_rating, added_via, time.time())
                )
                conn.commit()
                return True
        except Exception as exc:
            logger.error(f"Failed to add creator to registry: {exc}")
            return False

    def get_registry_creators(self, language: Optional[str] = None, enabled_only: bool = True) -> List[Dict[str, Any]]:
        """Retrieves registered creator channels."""
        creators = []
        try:
            query = "SELECT creator_id, platform, channel_id, channel_name, language, content_niche, enabled, human_rating, added_via, created_at FROM creator_registry WHERE 1=1"
            params = []
            if enabled_only:
                query += " AND enabled = 1"
            if language:
                query += " AND language = ?"
                params.append(language)
            query += " ORDER BY human_rating DESC, created_at DESC"

            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, tuple(params))
                for row in cursor.fetchall():
                    creators.append({
                        "creator_id": row[0],
                        "platform": row[1],
                        "channel_id": row[2],
                        "channel_name": row[3],
                        "language": row[4],
                        "content_niche": row[5],
                        "enabled": bool(row[6]),
                        "human_rating": row[7],
                        "added_via": row[8],
                        "created_at": row[9]
                    })
        except Exception as exc:
            logger.warning(f"Failed to query registry creators: {exc}")
        return creators

    def save_discovery_cache(self, discovery_key: str, series: str, chapter_id: str, language: str, results_json: str) -> bool:
        """Caches reference video discovery query to avoid YouTube API quota exhaustion."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO reference_discovery_cache (discovery_key, series, chapter_id, language, results_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(discovery_key) DO UPDATE SET results_json=excluded.results_json, created_at=excluded.created_at
                    """,
                    (discovery_key, series, chapter_id, language, results_json, time.time())
                )
                conn.commit()
                return True
        except Exception as exc:
            logger.error(f"Failed to save discovery cache: {exc}")
            return False

    def get_discovery_cache(self, discovery_key: str) -> Optional[str]:
        """Retrieves cached discovery results by discovery_key."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT results_json FROM reference_discovery_cache WHERE discovery_key = ? LIMIT 1", (discovery_key,))
                row = cursor.fetchone()
                if row:
                    return row[0]
        except Exception as exc:
            logger.warning(f"Failed to read discovery cache: {exc}")
        return None

    def save_synthesized_profile(self, fusion_key: str, chapter_id: str, algorithm_version: str, profile_json: str) -> bool:
        """Stores Synthesized Creator Profile (compressed behavioral prior) in SQLite."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO synthesized_profiles (fusion_key, chapter_id, algorithm_version, profile_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(fusion_key) DO UPDATE SET profile_json=excluded.profile_json, created_at=excluded.created_at
                    """,
                    (fusion_key, chapter_id, algorithm_version, profile_json, time.time())
                )
                conn.commit()
                return True
        except Exception as exc:
            logger.error(f"Failed to save synthesized profile: {exc}")
            return False

    def get_synthesized_profile(self, fusion_key: str) -> Optional[Dict[str, Any]]:
        """Retrieves Synthesized Creator Profile by canonical fusion_key."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT profile_json FROM synthesized_profiles WHERE fusion_key = ? LIMIT 1", (fusion_key,))
                row = cursor.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception as exc:
            logger.warning(f"Failed to get synthesized profile: {exc}")
        return None



# Global singleton instance
_GLOBAL_STORE: Optional[StateStore] = None
_SINGLETON_LOCK = threading.Lock()

def get_state_store() -> StateStore:
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        with _SINGLETON_LOCK:
            if _GLOBAL_STORE is None:
                _GLOBAL_STORE = StateStore()
    return _GLOBAL_STORE
