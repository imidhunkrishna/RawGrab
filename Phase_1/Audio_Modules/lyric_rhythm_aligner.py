"""
Audio_Modules/lyric_rhythm_aligner.py
======================================
Musical Intelligence Report — ONE Gemini call, maximum value.

Gemini receives the raw BGM audio file and returns a full structured
intelligence report that drives ALL downstream rhythm editing decisions:

    1. Lyric timestamps + emotional weight per word/phrase
    2. Section map   (intro / verse / pre-chorus / chorus / drop / bridge / outro)
    3. Tension arc   (0-1 score per second — drives hold vs. cut decisions)
    4. Shot directives (what visual to use at which moment)
    5. Vibe tags     (feeds CreativeBrain niche alignment)
    6. Emotional peak moments (single timestamps for instant-cut triggers)

Why ONE call? Because all 6 outputs share the same audio context window —
splitting them into 6 calls would cost 6× the quota for the same source material.

Controlled by: ENABLE_LYRIC_SYNC=true (default: true)
Gracefully returns empty structure on failure / instrumental audio.
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    for _env_path in [
        os.path.join(_REPO_ROOT, ".env"),
        os.path.join(_REPO_ROOT, "Credentials", ".env"),
        os.path.join(os.path.dirname(__file__), ".env"),
        ".env"
    ]:
        if os.path.exists(_env_path):
            load_dotenv(_env_path, override=True)
except Exception:
    pass

try:
    from google import genai
except ImportError:
    try:
        import google.generativeai as genai
    except ImportError:
        genai = None

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger("lyric_rhythm_aligner")

# ─── Config ───────────────────────────────────────────────────────────────────
ENABLE_LYRIC_SYNC = os.getenv("ENABLE_LYRIC_SYNC", "true").lower() in ("true", "1", "yes")

# Minimum audio file size — skip analysis on tiny/corrupt extracts
_MIN_AUDIO_BYTES = 32_768  # 32 KB

# Section types the system understands downstream
VALID_SECTIONS = {"intro", "verse", "pre_chorus", "chorus", "drop", "bridge", "outro", "instrumental"}

# Emotion → visual shot directive mapping (used when Gemini returns emotion labels)
_EMOTION_DIRECTIVE = {
    "love":        "face_closeup",
    "longing":     "face_closeup",
    "nostalgia":   "face_closeup",
    "joy":         "wide_energetic",
    "euphoria":    "wide_energetic",
    "hype":        "fast_action",
    "power":       "low_angle",
    "sadness":     "slow_zoom_in",
    "intimacy":    "face_closeup",
    "freedom":     "wide_landscape",
    "anger":       "fast_action",
    "celebration": "wide_energetic",
}

# ─── Prompt ───────────────────────────────────────────────────────────────────

_PROMPT = """You are a world-class music supervisor and video editor with expertise in rhythm-based editing.

Listen to this audio track carefully. Extract EVERYTHING needed to edit a viral short-form video to this music.

Return ONLY a single strict JSON object — no markdown, no explanation, no extra text.

JSON schema:
{
  "has_vocals": true | false,
  "language": "Hindi" | "English" | "Telugu" | "Tamil" | "Spanish" | "Instrumental" | ...,
  "tempo_bpm": <float — overall BPM estimate>,
  "bar_duration_sec": <float — duration of one musical bar in seconds>,
  "dominant_emotion": <string — single best emotion label: joy | love | hype | power | sadness | euphoria | nostalgia | celebration | anger | intimacy | freedom | neutral>,
  "energy_profile": "low" | "medium" | "high" | "building" | "explosive",

  "sections": [
    {
      "start": <float seconds>,
      "end": <float seconds>,
      "type": "intro" | "verse" | "pre_chorus" | "chorus" | "drop" | "bridge" | "outro" | "instrumental",
      "energy": <float 0.0–1.0>,
      "mood": <string — 1-2 word description e.g. "playful", "intense", "melancholic">,
      "recommended_cut_pace": "hold" | "slow" | "medium" | "fast" | "rapid_fire"
    }
  ],

  "tension_arc": [
    { "time": <float seconds>, "tension": <float 0.0–1.0> }
  ],

  "lyrics": [
    {
      "time": <float seconds — when this phrase starts>,
      "end": <float seconds — when this phrase ends>,
      "text": "<lyric text or phonetic approximation>",
      "emotion_weight": <float 0.0–1.0 — how emotionally charged this phrase is>,
      "emotion_tag": <string — joy | love | hype | power | sadness | euphoria | nostalgia | celebration | anger | intimacy | freedom | neutral>,
      "section": <string — which section this lyric falls in>
    }
  ],

  "emotional_peak_moments": [<float seconds>, ...],

  "shot_directives": [
    {
      "time": <float seconds>,
      "duration": <float seconds — how long this directive applies>,
      "directive": "face_closeup" | "wide_energetic" | "fast_action" | "slow_zoom_in" | "wide_landscape" | "low_angle" | "match_cut_motion" | "hold_on_subject",
      "priority": <int 1-5 — 5 is most important>,
      "reason": "<brief reason, e.g.: 'chorus drop — maximum energy'>"
    }
  ],

  "vibe_tags": [<string>, ...],

  "instrumental_sections": [
    { "start": <float seconds>, "end": <float seconds> }
  ],

  "is_unusable": true | false,
  "unusable_reason": "<brief explanation if unusable: e.g. 'paparazzi shouting/chatter with no music', 'heavy traffic/car noise', 'trading floor shouting', 'pure mic static' or '' if usable>"
}

RULES:
- is_unusable: set to true ONLY if the audio is unusable non-music noise (e.g. paparazzi chatter, car traffic noise, stock market trading hall shouting, heavy static/distortion with no usable music).
- tension_arc: provide one entry every 1 second (or every beat if BPM > 100). Tension rises into a chorus/drop, falls during verse/outro.
- sections: cover the entire track with no gaps. Every second must fall in exactly one section.
- lyrics: only fill if vocals are clearly present. For instrumental tracks, return [] and set has_vocals=false.
- emotional_peak_moments: timestamps where the music hits its hardest emotional/energy peak (typically the first chorus or drop). Maximum 5 entries.
- shot_directives: minimum 3, maximum 12 entries. Focus on the most critical edit decision moments.
- vibe_tags: 3-6 lowercase tags that describe the vibe (e.g. ["festive", "dance", "bollywood", "high_energy", "romantic"]).
- If the audio is very short (< 15s), still provide full structure based on what you can hear.
"""

# ─── Empty / fallback structure ───────────────────────────────────────────────

def _empty_report() -> Dict[str, Any]:
    return {
        "has_vocals": False,
        "language": "Unknown",
        "tempo_bpm": 0.0,
        "bar_duration_sec": 0.0,
        "dominant_emotion": "neutral",
        "energy_profile": "medium",
        "sections": [],
        "tension_arc": [],
        "lyrics": [],
        "emotional_peak_moments": [],
        "shot_directives": [],
        "vibe_tags": [],
        "instrumental_sections": [],
        "is_unusable": False,
        "unusable_reason": "",
        "_source": "fallback",
    }


def _clean_json(text: str) -> str:
    """Strip markdown wrappers and extract/repair LLM JSON object."""
    if not text:
        return ""
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            text = re.sub(r"```(?:json)?", "", text).replace("```", "")
    j_start = text.find("{")
    j_end   = text.rfind("}")
    if j_start != -1 and j_end > j_start:
        text = text[j_start:j_end + 1].strip()
    else:
        text = text.strip()

    # Repair 1: Clean trailing commas before closing braces/brackets
    text = re.sub(r",\s*([\]}])", r"\1", text)
    # Repair 2: Convert single-quoted keys/strings to double quotes if valid JSON fails
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        # Quote unquoted property names: key: -> "key":
        repaired = re.sub(r'([{,]\s*)([a-zA-Z0-9_]+)\s*:', r'\1"\2":', text)
        return repaired


def _validate_and_enrich(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Post-process Gemini output:
    - Clamp numeric fields to valid ranges
    - Normalize section types to known values
    - Add `shot_directive` hints derived from lyric emotion tags
    - Sort tension_arc and sections by time
    """
    # Clamp tempo
    report["tempo_bpm"] = max(0.0, float(report.get("tempo_bpm", 0.0)))
    report["bar_duration_sec"] = max(0.0, float(report.get("bar_duration_sec", 0.0)))

    # Auto-compute bar duration if Gemini skipped it
    if report["bar_duration_sec"] == 0.0 and report["tempo_bpm"] > 0:
        report["bar_duration_sec"] = round(4 * 60.0 / report["tempo_bpm"], 3)

    # Normalize sections
    for sec in report.get("sections", []):
        sec["start"] = float(sec.get("start", 0.0))
        sec["end"]   = float(sec.get("end",   0.0))
        sec["energy"] = max(0.0, min(1.0, float(sec.get("energy", 0.5))))
        if sec.get("type") not in VALID_SECTIONS:
            sec["type"] = "verse"  # safe default
    report["sections"] = sorted(report.get("sections", []), key=lambda x: x["start"])

    # Sort tension arc
    report["tension_arc"] = sorted(
        report.get("tension_arc", []),
        key=lambda x: float(x.get("time", 0.0))
    )

    # Clamp tension values
    for pt in report["tension_arc"]:
        pt["tension"] = max(0.0, min(1.0, float(pt.get("tension", 0.5))))

    # Sort lyrics
    report["lyrics"] = sorted(
        report.get("lyrics", []),
        key=lambda x: float(x.get("time", 0.0))
    )
    for lyric in report["lyrics"]:
        lyric["emotion_weight"] = max(0.0, min(1.0, float(lyric.get("emotion_weight", 0.5))))

    # Derive shot directives from lyrics if Gemini didn't provide them
    existing_directive_times = {d.get("time") for d in report.get("shot_directives", [])}
    derived = []
    for lyric in report.get("lyrics", []):
        if lyric.get("emotion_weight", 0) >= 0.7 and lyric.get("time") not in existing_directive_times:
            emotion = lyric.get("emotion_tag", "neutral")
            directive = _EMOTION_DIRECTIVE.get(emotion, "hold_on_subject")
            derived.append({
                "time":      lyric["time"],
                "duration":  lyric.get("end", lyric["time"] + 2.0) - lyric["time"],
                "directive": directive,
                "priority":  4,
                "reason":    f"High-emotion lyric: '{lyric.get('text', '')}' ({emotion})",
            })
    report["shot_directives"] = sorted(
        report.get("shot_directives", []) + derived,
        key=lambda x: float(x.get("time", 0.0))
    )

    # Sort emotional peaks
    report["emotional_peak_moments"] = sorted(
        [float(t) for t in report.get("emotional_peak_moments", [])]
    )

    report["is_unusable"] = bool(report.get("is_unusable", False))
    report["unusable_reason"] = str(report.get("unusable_reason", "")).strip()

    report["_source"] = "gemini"
    return report


# ─── Main API ─────────────────────────────────────────────────────────────────

def analyze_music(
    audio_path: str,
    whisper_transcript: Optional[Dict[str, Any]] = None,
    forensic_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Run the full Musical & Transcribed Speech Intelligence Report on `audio_path`.
    Integrates Faster-Whisper ASR transcript + Forensic Visual Cache from previous Gemini calls.

    Returns a dict matching _empty_report() structure on any failure.
    This function NEVER raises — all errors are caught and logged.

    Args:
        audio_path: Absolute or relative path to an MP3/WAV audio file.
        whisper_transcript: Optional Faster-Whisper ASR transcript dict ({segments: [...]}).
        forensic_context: Optional previous Gemini call cache (forensic_report.json).

    Returns:
        Musical intelligence report dict.
    """
    if not ENABLE_LYRIC_SYNC:
        logger.info("[LYRIC_ALIGNER] ENABLE_LYRIC_SYNC=false — skipping.")
        return _empty_report()

    if not audio_path or not os.path.exists(audio_path):
        logger.warning(f"[LYRIC_ALIGNER] Audio file not found: {audio_path}")
        return _empty_report()

    file_size = os.path.getsize(audio_path)
    if file_size < _MIN_AUDIO_BYTES:
        logger.warning(f"[LYRIC_ALIGNER] Audio too small ({file_size}B) — skipping.")
        return _empty_report()

    # Resolve persistent cache file path (Original_audio/beats/<basename>_lyric.json)
    audio_basename = os.path.splitext(os.path.basename(audio_path))[0]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(repo_root, "Original_audio", "beats")
    os.makedirs(cache_dir, exist_ok=True)
    cache_json_path = os.path.join(cache_dir, f"{audio_basename}_lyric.json")

    # 💾 CACHE HIT GUARD: If persistent lyric intelligence exists and contains semantic data, return immediately
    # Skip cache hit guard if explicit whisper_transcript or forensic_context is provided
    if not whisper_transcript and not forensic_context and os.path.exists(cache_json_path) and os.path.getsize(cache_json_path) > 50:
        try:
            with open(cache_json_path, "r", encoding="utf-8") as f:
                cached_report = json.load(f)
            # Only count as cache hit if it was generated by Gemini or has non-empty lyrics/directives
            if isinstance(cached_report, dict) and (cached_report.get("_source") == "gemini" or cached_report.get("lyrics") or cached_report.get("shot_directives")):
                cached_report["_source"] = "cache_hit"
                logger.info(f"[LYRIC_ALIGNER] 💾 Persistent Cache Hit for '{audio_basename}' -> Loaded from {cache_json_path}")
                return cached_report
        except Exception as _ce:
            logger.warning(f"[LYRIC_ALIGNER] Cache read fallback for '{audio_basename}': {_ce}")

    # ── Lookup pool_metadata.json by name (0.000s single source of truth lookup) ────
    pool_meta = None
    if not whisper_transcript and not forensic_context:
        try:
            from .audio_pool_manager import AudioPoolManager
            audio_filename = os.path.basename(audio_path)
            pool_meta = AudioPoolManager().get_track_intelligence(audio_filename)
            if pool_meta and isinstance(pool_meta, dict):
                if pool_meta.get("lyrics") or pool_meta.get("shot_directives"):
                    logger.info(f"[LYRIC_ALIGNER] ⚡ pool_metadata.json Hit for '{audio_filename}' -> Loaded full semantic intel (0.000s).")
                    pool_meta["_source"] = "pool_metadata_hit"
                    return pool_meta
        except Exception as _pme:
            logger.debug(f"[LYRIC_ALIGNER] pool_metadata lookup notice: {_pme}")

    # ⚡ STEP 1: Compute or Reuse BeatEngine Rhythm Math (BPM, Beats, Drops, Tension Arc)
    fast_report = _empty_report()
    if pool_meta and pool_meta.get("tempo_bpm"):
        # REUSE pre-computed rhythm math from pool_metadata.json without running FFmpeg BeatEngine!
        bpm = float(pool_meta.get("tempo_bpm", 120.0))
        beats = pool_meta.get("beats", [])
        drops = pool_meta.get("drops", [])
        bar_dur = float(pool_meta.get("bar_duration_sec", round(4 * 60.0 / (bpm or 120.0), 3)))
        fast_report = {
            "has_vocals": pool_meta.get("has_vocals", False),
            "language": pool_meta.get("language", "Instrumental"),
            "tempo_bpm": bpm,
            "bar_duration_sec": bar_dur,
            "dominant_emotion": pool_meta.get("dominant_emotion", "hype" if len(drops) > 0 else "joy"),
            "energy_profile": pool_meta.get("energy_profile", "medium"),
            "sections": pool_meta.get("sections", []),
            "tension_arc": pool_meta.get("tension_arc", []),
            "lyrics": pool_meta.get("lyrics", []),
            "emotional_peak_moments": drops[:5] if drops else pool_meta.get("emotional_peak_moments", []),
            "shot_directives": pool_meta.get("shot_directives", []),
            "vibe_tags": pool_meta.get("vibe_tags", []),
            "instrumental_sections": [],
            "is_unusable": False,
            "unusable_reason": "",
            "_source": "pool_metadata_fastpath"
        }
        logger.info(f"⚡ [LYRIC_ALIGNER] Reused pre-computed rhythm math from pool_metadata.json: bpm={bpm:.1f}")
    else:
        # Fallback: Run BeatEngine to calculate beats/drops if missing from pool_metadata.json
        try:
            from .beat_engine import BeatEngine
            be = BeatEngine()
            beats = be.analyze_beats(audio_path)
            drops = be.detect_drops(audio_path) if hasattr(be, 'detect_drops') else []
            bpm = 120.0
            bar_dur = round(4 * 60.0 / (bpm or 120.0), 3)
            fast_report = {
                "has_vocals": False,
                "language": "Instrumental",
                "tempo_bpm": bpm,
                "bar_duration_sec": bar_dur,
                "dominant_emotion": "hype" if len(drops) > 0 else "joy",
                "energy_profile": "high" if len(drops) > 0 else "medium",
                "sections": [
                    {"start": 0.0, "end": 15.0, "type": "chorus" if len(drops) > 0 else "verse", "energy": 0.85, "mood": "energetic", "recommended_cut_pace": "fast"}
                ],
                "tension_arc": [{"time": b, "tension": 0.8 if b in drops else 0.5} for b in beats[:20]],
                "lyrics": [],
                "emotional_peak_moments": drops[:5],
                "shot_directives": [],
                "vibe_tags": ["groove"],
                "instrumental_sections": [],
                "is_unusable": False,
                "unusable_reason": "",
                "_source": "beat_engine_fastpath"
            }
            logger.info(f"⚡ [LYRIC_ALIGNER] BeatEngine computed rhythm math: bpm={bpm:.1f}, drops={len(drops)}")
        except Exception as _fe:
            logger.warning(f"Beat engine computation notice: {_fe}")


    # 🎙️ STEP 2: Faster-Whisper ASR Speech Boundary Pass + Forensic Cache Integration
    whisper_prompt_text = ""
    transcript_segments = []
    if whisper_transcript and isinstance(whisper_transcript, dict):
        transcript_segments = whisper_transcript.get("segments") or whisper_transcript.get("sentences") or []
    else:
        try:
            from .run_faster_whisper_transcription import transcribe_audio_file
            w_res = transcribe_audio_file(audio_path)
            if w_res and w_res.get("segments"):
                transcript_segments = w_res.get("segments")
        except Exception as _we:
            logger.debug(f"[LYRIC_ALIGNER] Local speech boundary fallback notice: {_we}")

    if transcript_segments:
        transcript_lines = []
        for s in transcript_segments:
            t_str = f"[{s.get('start', 0.0):.2f}s - {s.get('end', 0.0):.2f}s]: \"{s.get('text', '')}\""
            transcript_lines.append(t_str)
        whisper_prompt_text += (
            "\n\n### FASTER-WHISPER TIMESTAMPED TRANSCRIPT REFERENCE (AUTHORITATIVE ASR):\n"
            + "\n".join(transcript_lines[:40]) + "\n\n"
            + "STRICT INSTRUCTION: Match Gemini lyrics[] array and shot_directives[] timestamps EXACTLY "
            + "to the vocal phrases listed above. Translate regional/Hinglish/Malayalam lyrics into English meaning "
            + "so visual shot directives match lyric semantics (e.g. 'muskurana' -> face_closeup, 'dance' -> wide_energetic).\n"
        )
        logger.info(f"🎙️ [LYRIC_ALIGNER] Attached Whisper transcript ({len(transcript_lines)} sentences) to Gemini prompt.")

    if forensic_context and isinstance(forensic_context, dict):
        clean_forensic = {
            "intent": forensic_context.get("intent"),
            "safety": forensic_context.get("safety"),
            "content_director": forensic_context.get("content_director"),
            "visual_energy": forensic_context.get("visual_energy"),
            "pacing_feel": forensic_context.get("pacing_feel"),
            "mood": forensic_context.get("mood"),
            "_shot_order": forensic_context.get("_shot_order"),
        }
        whisper_prompt_text += (
            "\n### PREVIOUS GEMINI CALL CACHE (FORENSIC & VISUAL CONTEXT):\n"
            + json.dumps(clean_forensic, indent=2, default=str) + "\n\n"
            + "INSTRUCTION: Ground the audio rhythm & lyrics against this visual context. "
            + "Ensure shot directives match visual energy, intent, and pacing.\n"
        )
        logger.info(f"🧠 [LYRIC_ALIGNER] Attached Forensic Cache (visual context) to Gemini prompt.")

    # 🧠 STEP 3: Gemini Multimodal Audio Call (Semantic Lyric & Emotion Extraction)
    gemini_router = None
    try:
        from Phase_1.Gemini_Modules import gemini_router
    except Exception as _e0:
        try:
            from Gemini_Modules.gemini_router_module import gemini_router
        except Exception as _e1:
            try:
                from Gemini_Modules.gemini_router_module.gemini_governor import gemini_router
            except Exception as _e2:
                try:
                    from gemini_governor import gemini_router
                except Exception as _e3:
                    logger.warning(f"[LYRIC_ALIGNER] gemini_router import failed: {_e0} | {_e1} | {_e2} | {_e3}")
                    return fast_report if fast_report.get("tempo_bpm", 0) > 0 else _empty_report()

    analysis_res = []
    _uploaded_file = None

    def _worker():
        nonlocal _uploaded_file
        try:
            from google import genai as _genai_client_mod
            import os as _os
            _api_key = _os.getenv("GEMINI_API_KEY", "") or _os.getenv("GOOGLE_API_KEY", "")
            _client = _genai_client_mod.Client(api_key=_api_key)
            print(f"  └─ 📤 [LYRIC_ALIGNER] Uploading {os.path.basename(audio_path)} to Gemini API...", flush=True)
            uf = _client.files.upload(file=audio_path)
            _uploaded_file = uf
            _wait = 0
            while getattr(getattr(uf, "state", None), "name", "ACTIVE") == "PROCESSING" and _wait < 5:
                time.sleep(1)
                uf = _client.files.get(name=uf.name)
                _wait += 1
            if getattr(getattr(uf, "state", None), "name", "ACTIVE") != "ACTIVE":
                return
            print(f"  └─ ⏳ [LYRIC_ALIGNER] Audio upload ready. Requesting Musical Intelligence Report...", flush=True)
            t_start = time.time()
            full_prompt = _PROMPT + whisper_prompt_text
            raw_response = gemini_router.generate(
                task_type="analysis",
                prompt=[uf, full_prompt],
                module_name="lyric_rhythm_aligner",
            )
            latency = time.time() - t_start
            print(f"  └─ ✅ [LYRIC_ALIGNER] Musical Intelligence received in {latency:.1f}s.", flush=True)
            analysis_res.append(raw_response)
        except Exception as _ex:
            analysis_res.append(_ex)

    import threading
    a_thread = threading.Thread(target=_worker, daemon=True)
    a_thread.start()
    a_thread.join(timeout=45.0)

    if not analysis_res or isinstance(analysis_res[0], Exception):
        logger.warning(f"[LYRIC_ALIGNER] Gemini Audio Call Timeout / Error for '{os.path.basename(audio_path)}' — using BeatEngine fallback.")
        # Save fast_report to persistent cache so app can continue
        try:
            with open(cache_json_path, "w", encoding="utf-8") as cf:
                json.dump(fast_report, cf, indent=2)
        except Exception: pass
        return fast_report if fast_report.get("tempo_bpm", 0) > 0 else _empty_report()

    raw_response = analysis_res[0]

    if not raw_response or len(str(raw_response).strip()) < 10:
        logger.warning("[LYRIC_ALIGNER] Empty response from Gemini — using BeatEngine fallback.")
        return fast_report if fast_report.get("tempo_bpm", 0) > 0 else _empty_report()

    try:
        cleaned = _clean_json(str(raw_response))
        report  = json.loads(cleaned)

        if not isinstance(report, dict):
            logger.warning("[LYRIC_ALIGNER] Gemini returned non-dict JSON — using BeatEngine fallback.")
            return fast_report if fast_report.get("tempo_bpm", 0) > 0 else _empty_report()

        # 🔀 STEP 4: Merge BeatEngine Math + Gemini Semantic Intelligence
        if fast_report.get("tempo_bpm", 0) > 0:
            report["tempo_bpm"] = fast_report["tempo_bpm"]
            report["bar_duration_sec"] = fast_report["bar_duration_sec"]
            if not report.get("tension_arc"):
                report["tension_arc"] = fast_report["tension_arc"]
            if not report.get("emotional_peak_moments"):
                report["emotional_peak_moments"] = fast_report["emotional_peak_moments"]

        report = _validate_and_enrich(report)
        report["_source"] = "gemini"

        # 💾 SAVE TO PERSISTENT CACHE
        try:
            temp_cache = cache_json_path + ".tmp"
            with open(temp_cache, "w", encoding="utf-8") as cf:
                json.dump(report, cf, indent=2)
            os.replace(temp_cache, cache_json_path)
            logger.info(f"[LYRIC_ALIGNER] 💾 Persisted lyric intelligence to disk: {cache_json_path}")
            # ✅ Merge lyric intel into pool_metadata.json (unified index)
            try:
                from Tools.Audio_Modules.audio_pool_manager import AudioPoolManager
                AudioPoolManager().merge_lyric_into_pool(
                    os.path.basename(audio_path), report
                )
            except Exception as _pm:
                logger.debug(f"[LYRIC_ALIGNER] pool merge (gemini_path): {_pm}")
        except Exception as _swe:
            logger.warning(f"[LYRIC_ALIGNER] Failed to persist lyric cache: {_swe}")

        # Summary log
        n_sec  = len(report.get("sections", []))
        n_lyr  = len(report.get("lyrics", []))
        n_dir  = len(report.get("shot_directives", []))
        n_arc  = len(report.get("tension_arc", []))
        n_peak = len(report.get("emotional_peak_moments", []))
        logger.info(
            f"[LYRIC_ALIGNER] 🎶 FUSED Musical Intelligence Report | "
            f"vocals={report.get('has_vocals')} lang={report.get('language')} "
            f"bpm={report.get('tempo_bpm')} emotion={report.get('dominant_emotion')} "
            f"sections={n_sec} lyrics={n_lyr} directives={n_dir} "
            f"tension_arc={n_arc}pts peaks={n_peak}"
        )
        return report

    except json.JSONDecodeError as _jde:
        logger.error(f"[LYRIC_ALIGNER] JSON parse error: {_jde}")
        return fast_report if fast_report.get("tempo_bpm", 0) > 0 else _empty_report()
    except Exception as _e:
        logger.warning(f"[LYRIC_ALIGNER] Gemini processing exception: {_e}")
        return fast_report if fast_report.get("tempo_bpm", 0) > 0 else _empty_report()

    finally:
        # Clean up the uploaded file from Gemini to avoid storage accumulation
        if _uploaded_file:
            try:
                _client.files.delete(name=_uploaded_file.name)
                logger.debug(f"[LYRIC_ALIGNER] Cleaned up uploaded file: {_uploaded_file.name}")
            except Exception:
                pass


def get_tension_at(tension_arc: List[Dict], time_sec: float) -> float:
    """
    Interpolate tension score at a specific timestamp from the tension arc.
    Returns 0.5 (neutral) if the arc is empty.
    """
    if not tension_arc:
        return 0.5
    arc = sorted(tension_arc, key=lambda x: x.get("time", 0.0))
    if time_sec <= arc[0].get("time", 0.0):
        return float(arc[0].get("tension", 0.5))
    if time_sec >= arc[-1].get("time", 0.0):
        return float(arc[-1].get("tension", 0.5))
    for i in range(len(arc) - 1):
        t0 = float(arc[i].get("time", 0.0))
        t1 = float(arc[i + 1].get("time", 0.0))
        if t0 <= time_sec <= t1:
            if t1 == t0:
                return float(arc[i].get("tension", 0.5))
            alpha = (time_sec - t0) / (t1 - t0)
            v0 = float(arc[i].get("tension", 0.5))
            v1 = float(arc[i + 1].get("tension", 0.5))
            return round(v0 + alpha * (v1 - v0), 3)
    return 0.5


def get_section_at(sections: List[Dict], time_sec: float) -> Optional[Dict]:
    """
    Return the section dict that contains `time_sec`, or None.
    """
    for sec in sections:
        if float(sec.get("start", 0)) <= time_sec < float(sec.get("end", 0)):
            return sec
    return None


def get_directive_at(directives: List[Dict], time_sec: float) -> Optional[Dict]:
    """
    Return the highest-priority shot directive active at `time_sec`, or None.
    """
    active = [
        d for d in directives
        if float(d.get("time", 0)) <= time_sec < float(d.get("time", 0)) + float(d.get("duration", 2.0))
    ]
    if not active:
        return None
    return max(active, key=lambda x: int(x.get("priority", 1)))


def select_best_audio_for_clip(
    clip_id: str,
    clip_folder: Optional[str] = None,
    audio_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Gemini Call 2 — BGM Selector.
    Receives current clip's visual_context + audio_data (math + context)
    + ALL pooled clip audio_data records from ClipIntelligenceStore.
    Gemini cross-matches visual intent vs pooled audio behavior to select the single best BGM track.
    Saves decision to clip intelligence JSON and returns selection dict.
    """
    from Gemini_Modules.clip_intelligence_store import ClipIntelligenceStore

    store = ClipIntelligenceStore()
    clip_data = store.load(clip_id, clip_folder) or store.create_blank(clip_id, clip_folder or "")

    visual_ctx = clip_data.get("visual_context", {})
    current_audio = clip_data.get("audio_data", {})
    pooled_audio_list = store.get_all_audio_data(limit=25)

    # ── Load the unified pool index (single source of truth) ──────────────────
    from Tools.Audio_Modules.audio_pool_manager import AudioPoolManager
    _pm = AudioPoolManager()
    pool_files = _pm.get_files_index()  # pool_metadata["files"] — has bpm, emotion, usage, last_used

    if not pool_files:
        logger.warning("🎶 [BGM Selector] pool_metadata['files'] is empty — no candidates available.")
        return {"selected_audio_track": None, "alignment_score": 0.0, "reasoning": "Empty pool index."}

    # Identify previously selected track for this clip (if re-editing)
    previous_bgm = current_audio.get("selected_bgm_track") or current_audio.get("selected_audio_track")
    disqualified_tracks = set()
    if previous_bgm:
        disqualified_tracks.add(previous_bgm.lower())

    # Build candidate list from pool_metadata["files"] — no disk scan needed
    all_candidates = [
        fname for fname, meta in pool_files.items()
        if isinstance(meta, dict)
        and not meta.get("is_unusable", False)
        and not meta.get("is_speech_only", False)
        and fname.lower().endswith((".mp3", ".wav", ".m4a"))
    ]

    if not all_candidates:
        logger.warning("🎶 [BGM Selector] No valid candidates found in pool_metadata['files'].")
        return {"selected_audio_track": None, "alignment_score": 0.0, "reasoning": "No valid BGM tracks in pool."}

    # Filter out previous BGM track if alternatives exist
    fresh_candidates = [c for c in all_candidates if c.lower() not in disqualified_tracks]
    available_candidates = fresh_candidates if fresh_candidates else all_candidates

    # Format candidate BGM tracks — ALL data comes from pool_metadata["files"] entries
    candidate_lines = {}  # fname -> formatted metadata line
    candidate_scores = []

    clip_intent = str(visual_ctx.get("intent", "viral_reel")).lower()
    clip_tone = str(visual_ctx.get("tone", "aspirational")).lower()
    clip_bpm = float(current_audio.get("math", {}).get("tempo_bpm", 120.0))
    now = time.time()

    for c_file in available_candidates:
        meta = pool_files.get(c_file, {})

        # Usage / recency data (already in pool_metadata)
        last_used = meta.get("last_used", 0)
        u_count = meta.get("usage_count", 0)

        # BPM / emotion / vibe data — merged from _lyric.json via merge_lyric_into_pool()
        # Falls back to the basic bpm/energy fields if lyric intel hasn't been merged yet
        c_bpm = float(meta.get("tempo_bpm") or meta.get("bpm") or 120.0)
        c_emotion = str(meta.get("dominant_emotion", "hype")).lower()
        c_vibe = str(meta.get("energy_profile", "medium")).lower()
        c_vocals = bool(meta.get("has_vocals", False))
        c_lang = str(meta.get("language", "unknown"))

        # LRU Recency Decay Math
        hrs_since_used = (now - last_used) / 3600.0 if last_used > 0 else 999.0
        recency_penalty = min(1.0, hrs_since_used / 12.0) if last_used > 0 else 1.0
        usage_penalty = 1.0 / (1.0 + u_count * 0.25)

        bpm_match = max(0.0, 1.0 - (abs(clip_bpm - c_bpm) / 100.0))
        emotion_match = 1.0 if c_emotion in clip_tone or clip_tone in c_emotion else 0.5
        math_score = (bpm_match * 0.35 + emotion_match * 0.45) * max(0.05, recency_penalty) * usage_penalty

        candidate_scores.append((math_score, c_file))
        candidate_lines[c_file] = (
            f"- '{c_file}': bpm={c_bpm:.1f}, emotion='{c_emotion}', energy='{c_vibe}', "
            f"vocals={c_vocals}, lang='{c_lang}', last_used={hrs_since_used:.1f}h_ago, usage_count={u_count}"
        )

    candidate_scores.sort(key=lambda x: x[0], reverse=True)
    best_math_candidate = candidate_scores[0][1] if candidate_scores else available_candidates[0]
    best_math_score = candidate_scores[0][0] if candidate_scores else 0.75

    # Send only top-10 by math score to Gemini — prevents equal-score hallucination
    top_candidates = candidate_scores[:10]
    top_lines = []
    for rank, (sc, fname) in enumerate(top_candidates, start=1):
        line = candidate_lines.get(fname, f"- '{fname}': score={sc:.3f}")
        top_lines.append(f"#{rank} {line}")
    candidates_str = "\n".join(top_lines)
    forbidden_str = ", ".join([f"'{t}'" for t in disqualified_tracks]) or "None"

    prompt = f"""You are a BGM Music Selector for short-form video clips.

Rules:
- NEVER pick a track from FORBIDDEN list
- You are given the top-10 pre-scored candidates, ranked #1 (best math fit) to #10
- The math pre-scorer already weighted BPM match, emotional tone, and recency — your job is to confirm or override with semantic reasoning
- If no better semantic match exists, confirm the #1 math pick
- Do NOT reference any RAG history or past clips — judge only on the clip context and track metadata below

[FORBIDDEN TRACKS — DO NOT SELECT]
{forbidden_str}

[CURRENT CLIP CONTEXT]
- Intent: '{visual_ctx.get('intent', 'viral_reel')}'
- Tone: '{visual_ctx.get('tone', 'aspirational')}'
- Narrative: '{visual_ctx.get('recommended_narrative', 'lifestyle')}'
- BPM: {clip_bpm}
- Speech mode: '{current_audio.get('context', {}).get('speech_mode', 'on_camera_dialogue')}'

[TOP-10 CANDIDATE TRACKS (ranked by BPM+emotion+recency math score)]
{candidates_str}

[MATH PRE-SELECTED WINNER]
'{best_math_candidate}' — override only if you have a strong semantic reason.

Return ONLY valid JSON:
{{
  "selected_audio_track": "chosen_filename.mp3",
  "alignment_score": 0.95,
  "reasoning": "One sentence: why this specific track fits this clip's intent/tone."
}}
"""

    selected_track = best_math_candidate
    reasoning = f"Smart Mathematical & Semantic Audio Match (score={candidate_scores[0][0]:.2f})."
    alignment_score = min(0.95, round(candidate_scores[0][0], 2)) if candidate_scores else 0.75

    try:
        try:
            from Gemini_Modules.gemini_router_module.gemini_governor import gemini_router
        except Exception:
            try:
                from Intelligence_Modules.gemini_governor import gemini_router
            except Exception:
                from gemini_governor import gemini_router

        if gemini_router:
            logger.info("🎶 [BGM Selector] Querying Gemini Router across model vanguard...")
            raw_response = gemini_router.generate(
                task_type="analysis",
                prompt=prompt,
                module_name="bgm_selector",
                gen_config={"temperature": 0.3}
            )
            if raw_response:
                data = json.loads(_clean_json(raw_response))
                win_track = data.get("selected_audio_track")
                if win_track and any(c.lower() == win_track.lower() for c in available_candidates) and win_track.lower() not in disqualified_tracks:
                    selected_track = win_track
                    reasoning = data.get("reasoning", reasoning)
                    alignment_score = float(data.get("alignment_score", 0.90))
                    logger.info(f"🎶 [BGM Selector - Gemini Call 2] Winner: '{selected_track}' (score={alignment_score:.2f})")
                else:
                    logger.warning(f"🎶 [BGM Selector] Gemini returned disqualified/invalid track '{win_track}' — forcing math winner '{selected_track}'.")
    except Exception as e:
        logger.warning(f"🎶 [BGM Selector - Gemini Call 2] Router fallback to smart math match: {e}")

    # Patch selection into clip intelligence store & AudioPoolManager persistent state
    store.patch_bgm_selection(clip_data, selected_track, reasoning, alignment_score)
    store.save(clip_id, clip_data, clip_folder)

    try:
        from Tools.Audio_Modules.audio_pool_manager import AudioPoolManager
        AudioPoolManager().use_audio(selected_track)
    except Exception as _pe:
        logger.warning(f"🎶 [BGM Selector] Could not register usage with AudioPoolManager: {_pe}")

    return {
        "selected_audio_track": selected_track,
        "alignment_score": alignment_score,
        "reasoning": reasoning,
    }


def compute_routing_parameters(
    lyric_intel: Optional[Dict[str, Any]],
    forensic_context: Optional[Dict[str, Any]],
    selected_bgm_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Translates lyric & music intelligence + visual context into mathematical FFmpeg synthesis parameters.
    Replaces standalone audio_rhythm_router.py by integrating semantic routing directly.
    """
    emotion = str((lyric_intel or {}).get("dominant_emotion", "hype")).lower().strip()
    energy  = str((lyric_intel or {}).get("energy_profile", "medium")).lower().strip()

    # Visual intent & tone from forensic analyzer
    v_ctx = forensic_context or {}
    intent  = str(v_ctx.get("intent", "viral_reel")).lower()
    tone    = str(v_ctx.get("tone", "aspirational")).lower()

    if emotion in ("sadness", "melancholic", "nostalgia") or tone in ("dramatic", "melancholic"):
        strategy_name = "CINEMATIC_EMOTIONAL_SLOWMO"
        speed_factor  = 0.75
        transition_type = "dissolve"
        transition_duration = 0.8
        ducking_db = -3.0
        target_cut_interval = 2.8
    elif intent in ("podcast_speech", "speech") or emotion == "conversational":
        strategy_name = "VOICEOVER_PODCAST_DUCKING"
        speed_factor  = 1.0
        transition_type = "fade"
        transition_duration = 0.4
        ducking_db = -18.0
        target_cut_interval = 3.5
    elif energy in ("explosive", "high") or emotion in ("hype", "power", "euphoria", "joy", "celebration"):
        strategy_name = "BEAT_SNAPPED_HIGH_ENERGY"
        speed_factor  = 1.1
        transition_type = "glitch"
        transition_duration = 0.2
        ducking_db = -6.0
        target_cut_interval = 0.8
    else:
        strategy_name = "DYNAMIC_CONTRAST_REEL"
        speed_factor  = 1.0
        transition_type = "whip_pan"
        transition_duration = 0.3
        ducking_db = -6.0
        target_cut_interval = 1.5

    return {
        "strategy_name": strategy_name,
        "speed_factor": speed_factor,
        "transition_type": transition_type,
        "transition_duration": transition_duration,
        "bgm_ducking_db": ducking_db,
        "music_volume": 0.85 if strategy_name != "VOICEOVER_PODCAST_DUCKING" else 0.15,
        "target_cut_interval": target_cut_interval,
        "selected_audio_path": selected_bgm_path,
        "dominant_emotion": emotion,
        "energy_profile": energy,
        "recommended_editing_mode": strategy_name
    }
