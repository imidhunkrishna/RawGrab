import os
import json
import time
import threading
import random
import logging
import numpy as np
import tempfile
from shutil import move
from typing import Dict, List, Optional, Any

logger = logging.getLogger("audio_pool_manager")

PIPELINE_BLOCKED_KWS = [
    "_reaction", "_textreaction", "first_shot", "first_shots",
    "general_intro", "watermark_clean", "intro_mixed_temp",
    "final_compilation", "tmp", "extracted_"
]

def _is_pipeline_artifact(filename: str) -> bool:
    lower_name = filename.lower()
    if lower_name.endswith(".wav") and ("tmp" in lower_name or "extracted" in lower_name):
        return True
    return any(kw in lower_name for kw in PIPELINE_BLOCKED_KWS)

class AudioPoolManager:
    """
    Manages the lifecycle of extracted audio clips.
    Pools:
      - active/: Eligible for selection.
      - cooldown/: Temporarily ineligible clips (recently used).
    """

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None or base_dir == "Original_audio":
            _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_dir = os.path.join(_repo, "Original_audio")
        self.base_dir = os.path.abspath(base_dir)
        self.active_dir = os.path.join(self.base_dir, "active")
        self.cooldown_dir = os.path.join(self.base_dir, "cooldown")
        self.beats_dir = os.path.join(self.base_dir, "beats")
        _phase_1_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _the_jsons_meta = os.path.join(_phase_1_root, "The jsons", "Audio_Modules_json", "metadata_pool.json")
        _local_meta = os.path.join(self.base_dir, "pool_metadata.json")
        self.meta_path = _the_jsons_meta if os.path.exists(_the_jsons_meta) else _local_meta

        self.lock = threading.RLock()
        self._cache_lock = threading.Lock()
        self._beat_cache = {}
        self.MAX_CACHE_SIZE = 20
        self.CURRENT_VERSION = 2
        
        # Ensure directories exist
        os.makedirs(self.active_dir, exist_ok=True)
        os.makedirs(self.cooldown_dir, exist_ok=True)
        os.makedirs(self.beats_dir, exist_ok=True)
        
        self.metadata = self._load_metadata()
        # Sync any loose files that landed in root (e.g. from extract_audio_from_video)
        # into active/ so select_best_audio() can find them immediately.
        self._sync_root_to_active()
        # Ensure all files in active/ are registered in metadata so they are not skipped.
        self._sync_active_to_metadata()

    def _load_metadata(self) -> Dict:
        """Loads metadata safely with two-section schema."""
        if not os.path.exists(self.meta_path):
            return {"version": self.CURRENT_VERSION, "updated_at": time.time(), "bgm_library": {}, "clip_source_math": {}}
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "bgm_library" not in data:
                        data = {"version": self.CURRENT_VERSION, "updated_at": time.time(), "bgm_library": data.get("files", {}), "clip_source_math": {}}
                    return data
        except Exception as e:
            logger.error(f"❌ Failed to load audio pool metadata: {e}")
        return {"version": self.CURRENT_VERSION, "updated_at": time.time(), "bgm_library": {}, "clip_source_math": {}}

    def _save_metadata(self):
        """Saves metadata atomically with file locking."""
        with self.lock:
            temp_path = self.meta_path + ".tmp"
            try:
                if "bgm_library" not in self.metadata:
                    self.metadata = {"version": self.CURRENT_VERSION, "updated_at": time.time(), "bgm_library": self.metadata, "clip_source_math": {}}
                self.metadata["updated_at"] = time.time()
                
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(self.metadata, f, indent=2, ensure_ascii=False)
                os.replace(temp_path, self.meta_path)
            except Exception as e:
                logger.error(f"❌ Failed to save audio pool metadata: {e}")
                if os.path.exists(temp_path):
                    try: os.remove(temp_path)
                    except: pass

    def _calculate_hash(self, path: str) -> str:
        """Fast size+mtime hash for integrity check."""
        try:
            stat = os.stat(path)
            return f"{stat.st_size}_{int(stat.st_mtime)}"
        except:
            return "unknown"

    def _safe_save_npz(self, path: str, **data):
        """Atomic NPZ save using tempfile + replace."""
        dir_name = os.path.dirname(path)
        fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix=".npz")
        os.close(fd)
        try:
            np.savez_compressed(temp_path, **data)
            os.replace(temp_path, path)
        except Exception as e:
            logger.error(f"❌ Atomic NPZ save failed for {path}: {e}")
            if os.path.exists(temp_path):
                try: os.remove(temp_path)
                except: pass

    def _get_file_metadata(self, filename: str) -> Optional[Dict]:
        """Helper to get file metadata accounting for schema version."""
        files = self.metadata.get("files", self.metadata)
        return files.get(filename)

    def get_track_intelligence(self, audio_name: str) -> Optional[Dict[str, Any]]:
        """
        Public API: Returns pre-computed intelligence (BPM, beats, drops, lyrics, sections)
        from pool_metadata.json for audio_name (e.g. 'Aditi_bhatia.mp3').
        Returns None if track is not indexed.
        """
        with self.lock:
            filename = os.path.basename(audio_name)
            return self._get_file_metadata(filename)

    def _set_file_metadata(self, filename: str, data: Dict):
        """Helper to set file metadata accounting for schema version."""
        if "files" not in self.metadata:
            self.metadata = {"version": self.CURRENT_VERSION, "files": self.metadata}
        self.metadata["files"][filename] = data

    def _sync_root_to_active(self):
        """
        [FIX] Move any loose .mp3/.wav files sitting in Original_audio/ root into
        active/ so select_best_audio() can find them.

        extract_audio_from_video() writes to root (not active/).  If the
        orchestrator's process_new_audio() call was skipped (e.g. beat analysis
        exception), the file stays in root forever and is invisible to the pool.
        This method runs at startup and repairs the dir structure.
        """
        try:
            for filename in os.listdir(self.base_dir):
                if not filename.lower().endswith((".mp3", ".wav")):
                    continue
                src = os.path.join(self.base_dir, filename)
                if not os.path.isfile(src):
                    continue
                # Safety: skip if already in metadata as being in active/
                meta = self._get_file_metadata(filename)
                # ── PIPELINE ARTIFACT GATE ──
                if _is_pipeline_artifact(filename):
                    logger.debug(f"[POOL_SYNC] Skipping pipeline artifact: {filename}")
                    continue

                # ── MUSIC GATE: Never re-ingest voice-only files via boot-sync ──────────
                if meta and meta.get("is_speech_only", False):
                    logger.debug(f"[POOL_SYNC] Skipping voice-only file (speech gate flag): {filename}")
                    continue

                # ── COOLDOWN GATE: Never move a file back to active if it's in cooldown ──
                # This is the second layer of the repeat-audio fix. Even if the file
                # ends up back in root (e.g. after a process restart), if it has a
                # recent last_used timestamp it should stay off the active pool.
                cooldown_path = os.path.join(self.cooldown_dir, filename)
                if os.path.exists(cooldown_path):
                    logger.debug(f"[POOL_SYNC] Skipping '{filename}' — already in cooldown/")
                    # Remove the duplicate from root to avoid confusion
                    try:
                        os.remove(src)
                    except Exception:
                        pass
                    continue

                # ── METADATA COOLDOWN GATE: Check last_used even if file isn't in cooldown/ ──
                # Covers the case where the cooldown/ file was already cleaned up by maintenance
                # but the 48h window hasn't elapsed.
                if meta and meta.get("last_used", 0) > 0:
                    hours_since_used = (time.time() - meta["last_used"]) / 3600
                    if hours_since_used < 48:
                        logger.info(
                            f"[POOL_SYNC] Skipping '{filename}' — used {hours_since_used:.1f}h ago "
                            f"(cooldown window: 48h). Not re-adding to active pool."
                        )
                        continue
                # ─────────────────────────────────────────────────────────────────────────
                dst = os.path.join(self.active_dir, filename)
                if os.path.exists(dst):
                    continue  # already there
                try:
                    move(src, dst)
                    logger.info(f"[POOL_SYNC] Moved loose audio to active/: {filename}")
                    # Stub metadata if absent so select_best_audio can score it
                    if not meta:
                        self._set_file_metadata(filename, {
                            "usage_count": 0,
                            "last_used":   0,
                            "bpm":         0.0,
                            "energy":      0.5,
                            "created_at":  time.time(),
                            "beat_data_path": None,
                            "drop_times":  [],
                            "sample_rate": 44100,
                            "audio_hash":  self._calculate_hash(dst),
                            "version":     self.CURRENT_VERSION,
                        })
                        self._save_metadata()
                except Exception as e:
                    logger.debug(f"[POOL_SYNC] Could not move {filename}: {e}")
        except Exception as e:
            logger.debug(f"[POOL_SYNC] Root sync failed (non-fatal): {e}")

    def _sync_active_to_metadata(self):
        """
        Scan active/ folder and ensure all files are registered in pool_metadata.json.
        """
        try:
            changed = False
            for filename in os.listdir(self.active_dir):
                if not filename.lower().endswith((".mp3", ".wav")):
                    continue
                path = os.path.join(self.active_dir, filename)
                if not os.path.isfile(path):
                    continue
                
                meta = self._get_file_metadata(filename)
                if not meta:
                    logger.info(f"[POOL_SYNC] Registering unstubbed active audio in metadata: {filename}")
                    self._set_file_metadata(filename, {
                        "usage_count": 0,
                        "last_used":   0,
                        "bpm":         0.0,
                        "energy":      0.5,
                        "created_at":  time.time(),
                        "beat_data_path": None,
                        "drop_times":  [],
                        "sample_rate": 44100,
                        "audio_hash":  self._calculate_hash(path),
                        "version":     self.CURRENT_VERSION,
                    })
                    changed = True
            if changed:
                self._save_metadata()
                logger.info(f"[POOL_SYNC] Active audio pool metadata successfully synced.")
        except Exception as e:
            logger.debug(f"[POOL_SYNC] Active-to-metadata sync failed: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # GEMINI POOL ENRICHMENT (background, non-blocking, cached)
    # ──────────────────────────────────────────────────────────────────────────

    def _gemini_enrich_background(self, dest_path: str, filename: str):
        """
        Daemon thread: analyze one BGM track with Gemini and write the result
        back into pool_metadata.json.

        Adds:  gemini_genre, gemini_mood_tags, gemini_energy_level,
               gemini_has_vocals, gemini_content_match, gemini_avoid_match,
               gemini_analyzed = True

        Safe to skip:  any exception → track stays unanalyzed, pipeline
                       falls back to existing BPM/energy scoring.
        """
        import os, json, re, shutil, subprocess, tempfile
        try:
            # 0. Flag guard
            if os.getenv("ENABLE_POOL_GEMINI_ENRICH", "no").lower() not in ("yes", "true", "1"):
                return

            # 1. Skip if already analyzed
            with self.lock:
                meta = self._get_file_metadata(filename)
            if meta and meta.get("gemini_analyzed"):
                logger.debug(f"[GEMINI_POOL] Already analyzed: {filename}")
                return

            # 2. Resolve path (file may be in active/ or cooldown/)
            track_path = dest_path
            for candidate in [dest_path,
                               os.path.join(self.active_dir, filename),
                               os.path.join(self.cooldown_dir, filename)]:
                if os.path.exists(candidate):
                    track_path = candidate
                    break
            else:
                return  # file not found anywhere

            # 3. Trim to 30 s → temp MP3 (keeps Gemini token cost minimal)
            ffmpeg_bin = os.getenv("FFMPEG_BIN", "ffmpeg")
            tmp_dir = tempfile.mkdtemp(prefix="gpool_")
            trimmed = os.path.join(tmp_dir, "trim30.mp3")
            try:
                subprocess.run(
                    [ffmpeg_bin, "-y", "-i", track_path,
                     "-t", "30", "-vn", "-ac", "1", "-ar", "22050", trimmed],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30
                )
                if not os.path.exists(trimmed) or os.path.getsize(trimmed) < 1024:
                    return
                with open(trimmed, "rb") as f:
                    audio_bytes = f.read()
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            # 4. Gemini call via official google.genai SDK (loads key from Credentials/.env)
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not api_key:
                try:
                    from dotenv import load_dotenv
                    _env_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "Credentials",
                        ".env",
                    )
                    if os.path.exists(_env_path):
                        load_dotenv(_env_path, override=False)
                        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
                except Exception:
                    pass

            if not api_key:
                logger.debug("[GEMINI_POOL] No API key found in .env — skipping enrichment.")
                return

            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/mpeg")

            prompt = (
                "Listen to this audio clip and respond with ONLY a valid JSON object. "
                "No markdown, no explanation, no code fences.\n"
                "{\n"
                '  "genre": "one word — lofi|phonk|hiphop|rnb|pop|edm|cinematic|'
                'acoustic|motivational|trap|drill|jazz|ambient|unknown",\n'
                '  "mood_tags": ["max 3 mood words"],\n'
                '  "energy_level": "low|medium|high",\n'
                '  "has_vocals": true_or_false,\n'
                '  "is_unusable": true_or_false,\n'
                '  "unusable_reason": "brief reason if audio is noisy paparazzi chatter, car traffic noise, stock market trading hall shouting, heavy static/distortion with no music backing",\n'
                '  "best_content_match": ["max 3 from: fashion|dance|fitness|comedy|'
                'motivational|aesthetic|food|travel|gaming|luxury|photography|'
                'sports|nature|educational"],\n'
                '  "avoid_content_match": ["max 2 categories this music does NOT fit"]\n'
                "}"
            )

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[audio_part, prompt],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=256,
                    response_mime_type="application/json",
                ),
            )
            raw = (response.text or "").strip()

            # 5. Parse JSON safely
            json_match = re.search(r"\{.*?\}", raw, re.DOTALL)
            if not json_match:
                logger.debug(f"[GEMINI_POOL] No JSON in response for {filename}: {raw[:80]}")
                return
            data = json.loads(json_match.group())

            is_unusable = bool(data.get("is_unusable", False))
            unusable_reason = str(data.get("unusable_reason", "")).strip()

            if is_unusable:
                quarantine_dir = os.path.join(self.base_dir, "quarantine")
                os.makedirs(quarantine_dir, exist_ok=True)
                quarantine_target = os.path.join(quarantine_dir, filename)
                try:
                    if os.path.exists(track_path) and os.path.abspath(track_path) != os.path.abspath(quarantine_target):
                        move(track_path, quarantine_target)
                        logger.warning(f"⚠️ [GEMINI_POOL] Quarantined unusable audio file '{filename}' -> quarantine/ (Reason: {unusable_reason})")
                except Exception as _qerr:
                    logger.warning(f"⚠️ [GEMINI_POOL] Could not quarantine file '{filename}': {_qerr}")

            # 6. Write back into pool metadata (thread-safe)
            with self.lock:
                meta = self._get_file_metadata(filename) or {}
                meta["gemini_genre"]        = str(data.get("genre", "unknown"))[:32]
                meta["gemini_mood_tags"]    = list(data.get("mood_tags", []))[:3]
                meta["gemini_energy_level"] = str(data.get("energy_level", "medium"))
                meta["gemini_has_vocals"]   = bool(data.get("has_vocals", False))
                meta["is_unusable"]         = is_unusable
                meta["unusable_reason"]     = unusable_reason
                meta["gemini_content_match"] = list(data.get("best_content_match", []))[:3]
                meta["gemini_avoid_match"]  = list(data.get("avoid_content_match", []))[:2]
                meta["gemini_analyzed"]     = True
                self._set_file_metadata(filename, meta)
            self._save_metadata()

            logger.info(
                f"🎵 [GEMINI_POOL] Enriched: {filename} → "
                f"genre={data.get('genre')} | energy={data.get('energy_level')} | "
                f"fits={data.get('best_content_match')}"
            )

        except Exception as _ge:
            # Non-fatal — track simply stays unenriched
            logger.debug(f"[GEMINI_POOL] Background enrichment failed for {filename} (non-fatal): {_ge}")

    def get_beat_data(self, filename: str) -> Optional[Dict]:
        """Lazy load beat data from cache or disk."""
        with self._cache_lock:
            if filename in self._beat_cache:
                return self._beat_cache[filename]

        meta = self._get_file_metadata(filename)
        if not meta or not meta.get("beat_data_path"):
            return None

        npz_path = os.path.join(self.base_dir, meta["beat_data_path"])
        if not os.path.exists(npz_path):
            return None

        try:
            with np.load(npz_path) as data:
                # Validation
                times = data.get("times", [])
                energies = data.get("energies", [])
                
                if len(times) == 0 or len(times) != len(energies):
                    logger.warning(f"⚠️ Validation failed for {filename} beat data. Length mismatch.")
                    return None
                
                beat_data = {
                    "times": times.tolist(),
                    "energies": energies.tolist(),
                    "sample_rate": meta.get("sample_rate", 44100)
                }
                
                # Update Cache (with growth control)
                with self._cache_lock:
                    if len(self._beat_cache) >= self.MAX_CACHE_SIZE:
                        # Simple FIFO pop
                        self._beat_cache.pop(next(iter(self._beat_cache)))
                    self._beat_cache[filename] = beat_data
                
                return beat_data
        except Exception as e:
            logger.error(f"❌ Failed to load beat data for {filename}: {e}")
            return None

    def process_new_audio(self, audio_path: str, bpm: float, energy: float, beat_analysis: Dict = None):
        """
        Moves newly extracted audio into pool and caches deep beat metadata.
        """
        if not os.path.exists(audio_path): return

        filename = os.path.basename(audio_path)
        dest_path = os.path.join(self.active_dir, filename)
        cooldown_path = os.path.join(self.cooldown_dir, filename)

        # ── COOLDOWN GUARD: If already in cooldown, do NOT move back to active ──
        # This is the primary fix for the repeat-audio bug. A file that was recently
        # used and moved to cooldown/ must stay there until maintenance() rotates it.
        if os.path.exists(cooldown_path):
            logger.info(
                f"[POOL] '{filename}' is in cooldown — skipping re-activation. "
                f"Metadata will be updated in-place."
            )
            # Still update BPM/energy if we have better data, but preserve usage history
            existing_meta = self._get_file_metadata(filename)
            if existing_meta:
                existing_meta["bpm"] = bpm if bpm > 0 else existing_meta.get("bpm", 0.0)
                existing_meta["energy"] = energy if energy > 0 else existing_meta.get("energy", 0.5)
                self._save_metadata()
            return

        try:
            # Move to active pool
            if os.path.abspath(audio_path) != os.path.abspath(dest_path):
                move(audio_path, dest_path)
            
            # ── Precompute Binary Data ──
            rel_npz_path = None
            drop_times = []
            
            if beat_analysis:
                # Quantize beats and energies
                raw_beats = beat_analysis.get("beats", []) # [{"time": t, "energy": e}, ...]
                times = np.array([round(b["time"], 3) for b in raw_beats], dtype=np.float32)
                energies = np.array([round(b["energy"], 3) for b in raw_beats], dtype=np.float32)
                
                # Precompute Drops directly if possible
                drop_times = [round(b["time"], 3) for b in raw_beats if b["time"] in beat_analysis.get("drops", [])]
                
                # Atomic Save to NPZ
                npz_filename = os.path.splitext(filename)[0] + ".npz"
                npz_path = os.path.join(self.beats_dir, npz_filename)
                self._safe_save_npz(npz_path, times=times, energies=energies)
                rel_npz_path = os.path.join("beats", npz_filename)

            # ── PRESERVE existing usage_count and last_used — NEVER reset to 0 ──
            # Resetting to 0 was the original bug: it erased cooldown history on
            # re-extraction, making the same track eligible immediately.
            existing_meta = self._get_file_metadata(filename)
            preserved_usage_count = existing_meta.get("usage_count", 0) if existing_meta else 0
            preserved_last_used   = existing_meta.get("last_used",   0) if existing_meta else 0

            # Initialize metadata
            self._set_file_metadata(filename, {
                "usage_count": preserved_usage_count,
                "last_used": preserved_last_used,
                "bpm": bpm,
                "energy": energy,
                "created_at": existing_meta.get("created_at", time.time()) if existing_meta else time.time(),
                "beat_data_path": rel_npz_path,
                "drop_times": drop_times,
                "sample_rate": 44100,
                "audio_hash": self._calculate_hash(dest_path),
                "version": self.CURRENT_VERSION
            })
            self._save_metadata()
            logger.info(f"🎵 [V{self.CURRENT_VERSION}] Processed: {filename} (usage_count kept={preserved_usage_count}, {len(drop_times)} drops cached)")

            # ── Gemini background enrichment (daemon, never blocks) ──────────
            # Runs only if ENABLE_POOL_GEMINI_ENRICH=yes. Analyzes the track
            # once and caches genre/vibe/content-match in pool_metadata.json.
            _enrich_thread = threading.Thread(
                target=self._gemini_enrich_background,
                args=(dest_path, filename),
                daemon=True,
                name=f"gemini_pool_{filename[:16]}",
            )
            _enrich_thread.start()

        except Exception as e:
            logger.error(f"❌ Failed processing {filename}: {e}")

    def recycle_cooldown(self, force: bool = False) -> int:
        """Rotate files from cooldown back to active. If force=True, recycle all files immediately."""
        count = 0
        if not os.path.exists(self.cooldown_dir):
            return 0
        for filename in os.listdir(self.cooldown_dir):
            if _is_pipeline_artifact(filename):
                continue
            src = os.path.join(self.cooldown_dir, filename)
            dst = os.path.join(self.active_dir, filename)
            meta = self._get_file_metadata(filename)
            last_used = meta.get("last_used", 0) if meta else 0
            if force or (time.time() - last_used > 60):
                try:
                    from shutil import move as _move
                    _move(src, dst)
                    count += 1
                    logger.info(f"♻️ [POOL] Recycled '{filename}' from cooldown -> active/")
                except Exception as _me:
                    logger.warning(f"Cooldown move failed for '{filename}': {_me}")
        return count

    def select_best_audio(
        self,
        video_bpm: float = 0,
        video_energy: float = 0,
        exclude_path: Optional[str] = None,
        recent_history: Optional[List[str]] = None,
        exclude_filenames: Optional[set] = None,
        target_bpm: float = 0,
        target_energy: float = 0,
        content_category: str = "",
    ) -> Optional[str]:
        if recent_history is None:
            recent_history = []
        
        _excluded_basenames: set = set(exclude_filenames or [])
        if exclude_path:
            _excluded_basenames.add(os.path.basename(exclude_path))
        
        best_audio = None
        best_score = -1.0

        active_files = os.listdir(self.active_dir)
        if not active_files:
            logger.info("ℹ️ Active audio pool is empty — auto-recycling tracks from cooldown...")
            self.recycle_cooldown(force=True)
            active_files = os.listdir(self.active_dir)
            if not active_files:
                return None

        for filename in active_files:
            # 1. Exclusion Logic
            if filename in _excluded_basenames:
                logger.debug(f"[POOL] Skipping self-selected audio: {filename}")
                continue
                
            if _is_pipeline_artifact(filename):
                logger.debug(f"[POOL] Skipping pipeline artifact: {filename}")
                continue
            
            meta = self._get_file_metadata(filename)
            if not meta:
                continue

            # Skip explicitly flagged non-music or unusable noise files
            if meta.get("is_speech_only", False) or meta.get("is_unusable", False):
                logger.debug(f"[POOL] Skipping unusable/speech audio: {filename}")
                continue

            # 2. Penalty/Recent Logic
            recent_penalty = (filename in recent_history)

            # 3. Match Logic
            # Resolve effective targets — support both old (video_bpm) and new (target_bpm) params
            _eff_bpm    = target_bpm    if target_bpm    > 0 else video_bpm
            _eff_energy = target_energy if target_energy > 0 else video_energy

            # BPM match: 1.0 when no preference, otherwise 1 - delta%
            if _eff_bpm > 0:
                bpm_match = max(0, 1 - abs(meta["bpm"] - _eff_bpm) / _eff_bpm)
            else:
                bpm_match = 1.0  # no preference → neutral score

            # Energy match: 1.0 when no preference
            if _eff_energy > 0:
                energy_match = max(0, 1 - abs(meta["energy"] - _eff_energy))
            else:
                energy_match = 1.0  # no preference → neutral score

            # Usage score: Inverse of count (favors NEW/LEAST USED)
            usage_score = 1 / (meta["usage_count"] + 1)

            # 4. Genre-Content & Lyric Semantic Compatibility Score
            # Checks both pool_metadata.json gemini fields AND persistent beats/<filename>_lyric.json cache
            genre_match = 0.5  # neutral default
            base_no_ext = os.path.splitext(filename)[0]
            lyric_cache_file = os.path.join(self.beats_dir, f"{base_no_ext}_lyric.json")
            cached_lyric_intel = None
            if os.path.exists(lyric_cache_file):
                try:
                    with open(lyric_cache_file, "r", encoding="utf-8") as lf:
                        cached_lyric_intel = json.load(lf)
                except Exception:
                    pass

            if content_category:
                _cat = content_category.lower().strip()
                _good = [c.lower() for c in (meta.get("gemini_content_match") or [])]
                _bad  = [c.lower() for c in (meta.get("gemini_avoid_match")  or [])]

                # Blend in persistent lyric intelligence vibe_tags and dominant_emotion if available
                if cached_lyric_intel:
                    _vibe_tags = [v.lower() for v in cached_lyric_intel.get("vibe_tags", [])]
                    _dom_emotion = str(cached_lyric_intel.get("dominant_emotion", "")).lower()
                    if _dom_emotion:
                        _good.append(_dom_emotion)
                    _good.extend(_vibe_tags)

                if any(_cat in g for g in _good):
                    genre_match = 1.0   # perfect fit
                elif any(_cat in b for b in _bad):
                    genre_match = 0.0   # active mismatch
                elif _good:             # has data but not a direct hit → slight boost over neutral
                    genre_match = 0.45

            # 5. Final Scoring
            # When genre/lyric data exists (gemini_analyzed or cached_lyric_intel) we rebalance weights to
            # give content-fit a meaningful seat.
            _has_semantic_intel = (bool(meta.get("gemini_analyzed")) or bool(cached_lyric_intel)) and bool(content_category)
            if _has_semantic_intel:
                score = (
                    bpm_match    * 0.40 +
                    energy_match * 0.20 +
                    genre_match  * 0.30 +
                    usage_score  * 0.10
                )
            else:
                score = (
                    bpm_match    * 0.60 +
                    energy_match * 0.20 +
                    usage_score  * 0.15
                )

            if recent_penalty:
                score *= 0.5

            # Human Variance
            score += random.uniform(0, 0.05)

            if score > best_score:
                best_score = score
                best_audio = filename

        if not best_audio:
            return None

        src = os.path.join(self.active_dir, best_audio)
        meta = self._get_file_metadata(best_audio)
        if meta:
            meta["usage_count"] = meta.get("usage_count", 0) + 1
            meta["last_used"] = time.time()
            self._save_metadata()

        return os.path.abspath(src)

    def use_audio(self, audio_path: str):
        """Mark a BGM track as used without moving to cooldown (rotation disabled per directive)."""
        filename = os.path.basename(audio_path)
        with self.lock:
            meta = self._get_file_metadata(filename)
            if meta:
                meta['usage_count'] = meta.get('usage_count', 0) + 1
                meta['last_used']   = time.time()
                self._set_file_metadata(filename, meta)
        self._save_metadata()
        logger.info(f"[POOL] Registered usage for {filename!r} (kept in active/ pool, rotation disabled).")

    def maintenance(self):
        """
        Rotates clips from cooldown back to active based on hybrid logic.
        Cleans up root directory of Original_audio.
        """
        now = time.time()
        
        # 1. 🔁 Cooldown → Active (Hybrid Logic: 48 hours OR implicit cycle gap via metadata analysis)
        # Note: 'usage_gap' isn't explicitly stored, but we can infer 'last_used' is the primary trigger.
        # User specified: now - last_used > 48h OR usage_gap >= 15.
        # Tracking "usage_gap" precisely requires a global count. 
        # For now, let's stick to the 48h time trigger provided in the skeleton.
        
        # 1. 🔁 Cooldown → Active rotation (48-hour rule)
        # Files that have been in cooldown for 48 hours are rotated back.
        count_rotated = 0
        for filename in os.listdir(self.cooldown_dir):
            if _is_pipeline_artifact(filename):
                continue
                
            path = os.path.join(self.cooldown_dir, filename)
            meta = self._get_file_metadata(filename)

            if not meta:
                # Orphaned cooldown file — move back to active for safety
                try: move(path, os.path.join(self.active_dir, filename)); count_rotated += 1
                except: pass
                continue

            time_passed = now - meta.get("last_used", 0)

            if time_passed > 48 * 3600:
                try:
                    move(path, os.path.join(self.active_dir, filename))
                    count_rotated += 1
                except Exception as e:
                    logger.error(f"Failed to rotate {filename} back to active: {e}")

        if count_rotated > 0:
            logger.info(f"🔁 Audio Maintenance: Rotated {count_rotated} clips from cooldown to active.")

        # 1b. 🧹 Orphaned NPZ cleanup
        try:
            meta_files = self.metadata.get("files", self.metadata)
            valid_npz = set()
            for f_meta in meta_files.values():
                if isinstance(f_meta, dict) and f_meta.get("beat_data_path"):
                    valid_npz.add(os.path.basename(f_meta["beat_data_path"]))
            
            count_npz_cleaned = 0
            for npz_file in os.listdir(self.beats_dir):
                if npz_file not in valid_npz:
                    try:
                        os.remove(os.path.join(self.beats_dir, npz_file))
                        count_npz_cleaned += 1
                    except: pass
            if count_npz_cleaned > 0:
                logger.info(f"🧹 Audio Maintenance: Cleaned {count_npz_cleaned} orphaned .npz files.")
        except Exception as e:
            logger.warning(f"⚠️ NPZ cleanup fail: {e}")

        # 2. 🧹 Root cleanup (Files > 6h old)
        count_cleaned = 0
        for filename in os.listdir(self.base_dir):
            path = os.path.join(self.base_dir, filename)

            # Skip subdirectories (active, cooldown)
            if os.path.isdir(path):
                continue

            # Skip metadata file
            if filename == os.path.basename(self.meta_path):
                continue

            try:
                created = os.path.getctime(path)
                if now - created > 6 * 3600:
                    os.remove(path)
                    count_cleaned += 1
            except Exception as e:
                logger.warning(f"Failed to clean root file {filename}: {e}")

        if count_cleaned > 0:
            logger.info(f"🧹 Audio Maintenance: Cleaned {count_cleaned} stale files from Original_audio root.")

    def get_files_index(self) -> Dict[str, Any]:
        """
        Return the pool_metadata["files"] dict — the unified audio track index.
        Keys are filenames (e.g. 'Zareena_khan.mp3'), values are the full metadata dicts.
        This is the single source of truth for select_best_audio_for_clip().
        """
        return dict(self.metadata.get("files", self.metadata))

    def merge_lyric_into_pool(self, track_filename: str, lyric_data: Dict[str, Any]) -> bool:
        """
        Merge rich lyric intelligence fields from a _lyric.json result INTO
        pool_metadata["files"][track_filename].  Called by analyze_music() after
        saving the _lyric.json so pool_metadata.json becomes the single unified
        index (usage_count + last_used + BPM + emotion + vibe_tags all in one place).

        Fields written (only if present in lyric_data):
          tempo_bpm, dominant_emotion, energy_profile, vibe_tags,
          has_vocals, language, sections_summary (start/end/type only — compact)

        Returns True if the entry was found and updated, False otherwise.
        """
        if not track_filename or not isinstance(lyric_data, dict):
            return False
        try:
            with self.lock:
                meta = self._get_file_metadata(track_filename)
                if meta is None:
                    # Track not yet registered — create a stub so data isn't lost
                    meta = {
                        "usage_count": 0,
                        "last_used": 0,
                        "bpm": lyric_data.get("tempo_bpm", 0.0),
                        "energy": 0.5,
                        "created_at": time.time(),
                        "beat_data_path": None,
                        "drop_times": [],
                        "sample_rate": 44100,
                        "audio_hash": "unknown",
                        "version": self.CURRENT_VERSION,
                    }

                # Always overwrite with fresh lyric intelligence
                if "tempo_bpm" in lyric_data:
                    meta["tempo_bpm"] = float(lyric_data["tempo_bpm"])
                    # Also keep legacy bpm field in sync for backward compat
                    if meta.get("bpm", 0.0) == 0.0:
                        meta["bpm"] = float(lyric_data["tempo_bpm"])
                if "dominant_emotion" in lyric_data:
                    meta["dominant_emotion"] = str(lyric_data["dominant_emotion"])
                if "energy_profile" in lyric_data:
                    meta["energy_profile"] = str(lyric_data["energy_profile"])
                if "vibe_tags" in lyric_data:
                    meta["vibe_tags"] = list(lyric_data["vibe_tags"])[:6]
                if "has_vocals" in lyric_data:
                    meta["has_vocals"] = bool(lyric_data["has_vocals"])
                if "language" in lyric_data:
                    meta["language"] = str(lyric_data["language"])
                if "sections" in lyric_data:
                    meta["sections_summary"] = [
                        {"start": s.get("start", 0), "end": s.get("end", 0),
                         "type": s.get("type", "unknown"), "energy": s.get("energy", 0.5)}
                        for s in lyric_data["sections"]
                    ]
                meta["lyric_intel_merged"] = True
                self._set_file_metadata(track_filename, meta)
            self._save_metadata()
            logger.debug(f"[POOL_MERGE] Merged lyric intel into pool entry: {track_filename}")
            return True
        except Exception as _e:
            logger.warning(f"[POOL_MERGE] Failed to merge lyric data for '{track_filename}': {_e}")
            return False


# Global Instance
pool_manager = AudioPoolManager()
