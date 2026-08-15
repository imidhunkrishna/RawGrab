"""
audio_extractor.py — Phase 1 Audio Extraction + Beat Analysis
==============================================================
Called immediately after a clip is downloaded in Phase 1.

Workflow:
  1. ffprobe: check if video has an audio stream (skip silently if not)
  2. ffmpeg: extract mono 16 kHz WAV → {clip_dir}/{stem}_extracted.wav
     (saved in the CLIP folder, NOT in Original_audio/ which is the curated BGM pool)
  3. BeatEngine: run beat + drop analysis
  4. Persist → downloads/{owner}_{shortcode}/audio_analysis.json

Phase 2 reads audio_analysis.json directly — no re-extraction needed.

Audio Pool Architecture:
  Original_audio/active/   ← curated BGM tracks for Gemini selection
  Original_audio/cooldown/ ← recently used BGM (rotation cooldown)
  clip_dir/                ← per-clip extracted ambient audio + analysis (THIS file writes here)
"""

import os
import json
import subprocess
import logging
from typing import Optional

logger = logging.getLogger("AMTE.audio_extractor")

FFMPEG_BIN  = os.getenv("FFMPEG_BIN",  "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")

_REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUDIO_DIR   = os.path.join(_REPO_ROOT, "Original_audio")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _has_audio_stream(video_path: str) -> bool:
    """Returns True if the file has at least one audio stream (fast ffprobe check)."""
    try:
        probe = subprocess.run(
            [
                FFPROBE_BIN, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "json", video_path,
            ],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(probe.stdout or "{}")
        return bool(data.get("streams"))
    except Exception:
        return True  # On probe failure, let FFmpeg try


def extract_audio(video_path: str, output_path: str) -> bool:
    """
    Extracts mono 16 kHz PCM WAV from video_path → output_path.
    Returns True on success, False if:
      - video has no audio stream
      - ffmpeg exits non-zero
    Captures FFmpeg stderr and logs it on failure so we know exactly what went wrong.
    """
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
        logger.info("♻️ [AUDIO] Extracted audio WAV already exists: %s (skipping duplicate FFmpeg)", os.path.basename(output_path))
        return True

    if not _has_audio_stream(video_path):
        logger.info("🔇 No audio stream in '%s' — skipping extraction.", os.path.basename(video_path))
        return False

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    cmd = [
        FFMPEG_BIN, "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        output_path,
    ]

    logger.info("🎙️ Extracting audio from %s...", os.path.basename(video_path))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error(
                "❌ FFmpeg audio extraction failed (exit %d):\n%s",
                result.returncode,
                result.stderr[-800:],   # last 800 chars — enough to see the error
            )
            return False
        logger.info("✅ Extracted: %s (%.0f KB)", os.path.basename(output_path),
                    os.path.getsize(output_path) / 1024)
        return True
    except subprocess.TimeoutExpired:
        logger.error("❌ FFmpeg audio extraction timed out for '%s'", video_path)
        return False
    except Exception as exc:
        logger.error("❌ Unexpected error during audio extraction: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 Post-Download Hook
# ─────────────────────────────────────────────────────────────────────────────

def run_phase1_audio_analysis(video_path: str, clip_dir: str) -> dict:
    """
    Phase 1 post-download hook. Called immediately after video.mp4 is saved.

    Steps:
      1. Extract audio WAV from video (skip if video is silent)
      2. Run BeatEngine analysis (tempo, energy, beats, drops, vibe)
      3. Save result to {clip_dir}/audio_analysis.json

    Returns the analysis dict (or a minimal silent-clip dict).
    """
    analysis_path = os.path.join(clip_dir, "audio_analysis.json")

    # Return cached result if already computed
    if os.path.exists(analysis_path):
        try:
            with open(analysis_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            logger.info("♻️ [AUDIO] Using cached audio_analysis.json for %s", os.path.basename(clip_dir))
            return cached
        except Exception:
            pass  # Re-run if cache is corrupt

    stem = os.path.splitext(os.path.basename(video_path))[0]
    # ── ARCHITECTURE NOTE ────────────────────────────────────────────────────────
    # Extracted clip audio goes into the CLIP FOLDER, not Original_audio/.
    # Original_audio/active/ is the curated BGM rotation pool for Gemini to select
    # tracks from. Dumping per-clip ambient audio there corrupts pool selection.
    # ─────────────────────────────────────────────────────────────────────────────
    os.makedirs(clip_dir, exist_ok=True)
    wav_path = os.path.join(clip_dir, f"{stem}_extracted.wav")

    # Step 1: Extract
    has_audio = extract_audio(video_path, wav_path)

    # Step 2: Beat analysis
    analysis: dict = {
        "has_audio":       has_audio,
        "has_vocals":      has_audio,
        "is_speech_vocal": has_audio,
        "wav_path":        wav_path if has_audio else None,
        "tempo_bpm":       0.0,
        "avg_energy":      0.0,
        "vibe":            "silent",
        "beats":           [],
        "beat_count":      0,
        "drops":           [],
        "drop_count":      0,
        "beat_score":      0.0,
        "source":          "phase1_extraction",
    }

    if has_audio and os.path.exists(wav_path):
        try:
            import sys
            sys.path.insert(0, _REPO_ROOT)
            from Tools.Audio_Modules.beat_engine import analyze_beats_with_drops

            beat_data = analyze_beats_with_drops(wav_path)
            vibe_str = str(beat_data.get("vibe", "unknown")).lower()
            has_v = beat_data.get("avg_energy", 0.0) > 0.01
            is_speech = has_v and ("speech" in vibe_str or "voice" in vibe_str or "dialogue" in vibe_str or "talk" in vibe_str or "vocal" in vibe_str or vibe_str in ("unknown", "silent"))

            analysis.update({
                "has_vocals":      has_v,
                "is_speech_vocal": is_speech,
                "tempo_bpm":  beat_data.get("tempo_bpm", 0.0),
                "avg_energy": beat_data.get("avg_energy", 0.0),
                "vibe":       beat_data.get("vibe", "unknown"),
                "beats":      beat_data.get("beats", []),
                "beat_count": len(beat_data.get("beats", [])),
                "drops":      beat_data.get("drops", []),
                "drop_count": len(beat_data.get("drops", [])),
                "beat_score": (
                    len(beat_data.get("beats", [])) * 1.0
                    + len(beat_data.get("drops", [])) * 3.0
                    + beat_data.get("avg_energy", 0.0) * 10.0
                ),
            })

            # Step 2b: Extract word-level speech boundaries using faster-whisper
            try:
                from Tools.Audio_Modules.speech_boundary_detector import extract_speech_boundaries
                s_bounds_path = os.path.join(clip_dir, "speech_boundaries.json")
                s_data = extract_speech_boundaries(wav_path, output_json_path=s_bounds_path)
                if s_data.get("has_speech"):
                    analysis["speech_boundaries"] = {
                        "words_count": len(s_data.get("words", [])),
                        "clean_cut_timestamps": [c["timestamp_sec"] for c in s_data.get("clean_cut_timestamps", [])]
                    }
                    logger.info("🎙️ [SPEECH BOUNDARY] Found %d words, %d clean sentence cut boundaries.",
                                len(s_data.get("words", [])), len(s_data.get("clean_cut_timestamps", [])))
            except Exception as _sb_err:
                logger.warning("⚠️ [Speech Boundary] Extraction skipped/failed: %s", _sb_err)

        except Exception as exc:
            logger.warning("⚠️ Beat analysis failed: %s — storing raw extraction only.", exc)
    else:
        logger.info("🔇 [BEAT] Silent clip — skipping beat analysis.")

    # Step 3: Persist local clip analysis
    try:
        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        logger.info("💾 [AUDIO] Saved audio_analysis.json → %s", clip_dir)
    except Exception as exc:
        logger.warning("⚠️ Failed to write audio_analysis.json: %s", exc)

    # Step 4: Register extracted audio into active BGM pool for future selection
    if has_audio and os.path.exists(wav_path) and analysis.get("tempo_bpm", 0) > 0:
        try:
            from Tools.Audio_Modules.audio_pool_manager import AudioPoolManager
            pool_mgr = AudioPoolManager()
            pool_mgr.process_new_audio(
                audio_path=wav_path,
                bpm=analysis["tempo_bpm"],
                energy=analysis["avg_energy"],
                beat_analysis=analysis
            )
            logger.info("🎵 [AUDIO POOL] Registered harvested audio '%s' into Original_audio/active pool!", os.path.basename(wav_path))
        except Exception as pool_err:
            logger.warning("⚠️ Failed to register harvested audio into active pool: %s", pool_err)

    # Step 4: Auto-ingest clean musical audio into central BGM pool (Original_audio/active/) & index
    if has_audio and os.path.exists(wav_path) and analysis.get("beat_count", 0) >= 5:
        _ingest_clip_audio_to_pool(stem, wav_path, analysis)

    return analysis


def _ingest_clip_audio_to_pool(stem: str, wav_path: str, analysis: dict):
    """
    Ingests clean musical audio extracted from a Phase 1 clip into the central
    Original_audio/active/ BGM pool and updates indexed_audio_analysis.json.
    """
    try:
        active_dir = os.path.join(_AUDIO_DIR, "active")
        index_path = os.path.join(_AUDIO_DIR, "indexed_audio_analysis.json")
        os.makedirs(active_dir, exist_ok=True)

        target_wav = os.path.join(active_dir, f"{stem}.wav")
        if not os.path.exists(target_wav):
            import shutil
            shutil.copy2(wav_path, target_wav)
            logger.info("🎶 [POOL_INGEST] Ingested clip audio into BGM pool: %s.wav", stem)

        # Update global indexed_audio_analysis.json
        index_data = {}
        if os.path.exists(index_path):
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    index_data = json.load(f)
            except Exception:
                index_data = {}

        index_data[stem] = {
            "video_id": stem,
            "file_name": f"{stem}.wav",
            "file_path": target_wav,
            "tempo_bpm": analysis.get("tempo_bpm", 0.0),
            "avg_energy": analysis.get("avg_energy", 0.0),
            "vibe": analysis.get("vibe", "unknown"),
            "beat_count": analysis.get("beat_count", 0),
            "drop_count": analysis.get("drop_count", 0),
            "drops": analysis.get("drops", []),
            "beat_score": analysis.get("beat_score", 0.0),
        }

        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, ensure_ascii=False)

        # Preserve extracted WAV for Telegram Storage Group Vault upload
        analysis["wav_path"] = wav_path if os.path.exists(wav_path) else target_wav
        logger.info("💾 [AUDIO PRESERVED] Extracted WAV preserved in clip dir for Telegram Vault upload: %s", os.path.basename(wav_path))
    except Exception as err:
        logger.warning("⚠️ Pool ingestion warning for '%s': %s", stem, err)


def load_audio_analysis(clip_dir: str) -> Optional[dict]:
    """
    Phase 2 helper: loads pre-computed audio_analysis.json from clip_dir.
    Returns None if not found (Phase 2 will run its own fallback).
    """
    analysis_path = os.path.join(clip_dir, "audio_analysis.json")
    if not os.path.exists(analysis_path):
        return None
    try:
        with open(analysis_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("⚠️ Failed to load audio_analysis.json: %s", exc)
        return None
