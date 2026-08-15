"""
Beat Engine
-----------
Zero-dependency Beat Detection for Viral Edits.
Uses FFmpeg to decode audio to raw PCM/WAV, then analyzes amplitude peaks using standard Python libraries.

Usage:
    beats = beat_engine.analyze_beats("music.mp3")
    # beats = [0.54, 1.23, ...] (Seconds)
"""

import os
import logging
import subprocess
import wave
import struct
import math
import tempfile
import shutil
from typing import List, Optional

logger = logging.getLogger("beat_engine")

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

class BeatEngine:
    def __init__(self):
        self.sensitivity = 1.3 # Multiplier above local average to count as beat
        self.min_beat_interval = 0.4 # Minimum seconds between beats (prevent rapid fire)
        self.window_size = 0.05 # 50ms window for smoothing

    def _has_audio_stream(self, path: str) -> bool:
        """
        Returns True if the file contains at least one audio stream.
        Used as a pre-flight guard before FFmpeg audio extraction to prevent
        'Output file does not contain any stream' crashes on video-only files.
        On probe failure, returns True so FFmpeg can attempt and catch its own error.
        """
        try:
            import json as _json
            probe_cmd = [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "json", path,
            ]
            probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
            probe_data = _json.loads(probe_res.stdout)
            return bool(probe_data.get("streams"))
        except Exception:
            return True  # Probe failure → let FFmpeg attempt and catch its own error

    def analyze_beats(self, audio_path: str) -> List[float]:
        """
        Analyzes an audio file and returns a list of significant beat timestamps.
        """
        if not os.path.exists(audio_path):
            logger.error(f"❌ Audio file not found: {audio_path}")
            return []

        # 0. Size Check
        if os.path.getsize(audio_path) < 1024:
            logger.error(f"❌ Audio file is too small or corrupted: {audio_path} ({os.path.getsize(audio_path)} bytes)")
            return []

        # 0b. Audio-stream guard — video-only files (no audio track) crash FFmpeg
        if not self._has_audio_stream(audio_path):
            logger.warning(f"⚠️ analyze_beats: no audio stream in '{audio_path}' — skipping beat analysis.")
            return []

        # 1. Convert to temporary WAV (16-bit PCM, Mono, 44.1kHz)
        # We use a temp file
        fd, temp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        try:
            cmd = [
                FFMPEG_BIN, "-y", "-i", audio_path,
                "-ac", "1", # Mono
                "-ar", "44100", # 44.1kHz
                "-acodec", "pcm_s16le", # 16-bit raw PCM
                temp_wav
            ]
            
            # Run ffmpeg and capture errors
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=True)
            
            # 2. Analyze PCM Data
            return self._process_wav(temp_wav)

        except subprocess.CalledProcessError as e:
            logger.error(f"⚠️ Beat analysis failed (FFmpeg Error): {e.stderr if e.stderr else str(e)}")
            logger.error(f"   └─ Command: {' '.join(cmd)}")
            return []
        except Exception as e:
            logger.error(f"⚠️ Beat analysis failed: {e}")
            return []
        finally:
            if os.path.exists(temp_wav):
                try: os.remove(temp_wav)
                except: pass

    def _process_wav(self, wav_path: str) -> List[float]:
        beats = []
        try:
            with wave.open(wav_path, 'rb') as wf:
                framerate = wf.getframerate()
                nframes = wf.getnframes()
                sampwidth = wf.getsampwidth() # Should be 2 (16-bit)
                
                if sampwidth != 2:
                    logger.warning("⚠️ WAV is not 16-bit. Skipping.")
                    return []

                # Read all frames (memory safe for typical 3-5min songs ~30MB)
                raw_data = wf.readframes(nframes)
                
                # Convert to integers
                # 'h' = short (2 bytes)
                count = len(raw_data) // 2
                fmt = f"<{count}h" 
                samples = struct.unpack(fmt, raw_data)
                
                # Calculate Envelope (RMS / Amplitude)
                # We group samples into windows
                window_samples = int(framerate * self.window_size)
                envelopes = []
                
                for i in range(0, len(samples), window_samples):
                    chunk = samples[i:i+window_samples]
                    if not chunk: continue
                    
                    # RMS calculation
                    sum_sq = sum(s*s for s in chunk)
                    rms = math.sqrt(sum_sq / len(chunk))
                    envelopes.append(rms)

                # Peak Detection
                # Calculate local average to determine threshold
                # Simple Moving Average
                local_window = 40 # Look at ~2 seconds context (40 * 50ms)
                
                last_beat_time = -self.min_beat_interval
                
                for i, amp in enumerate(envelopes):
                    # Context window
                    start = max(0, i - local_window // 2)
                    end = min(len(envelopes), i + local_window // 2)
                    context = envelopes[start:end]
                    avg_energy = sum(context) / len(context) if context else 0
                    
                    # Threshold logic
                    threshold = avg_energy * self.sensitivity
                    
                    # Time of this window
                    time_sec = i * self.window_size
                    
                    if amp > threshold and amp > 1000: # Must be somewhat loud
                        # Check debounce
                        if time_sec - last_beat_time >= self.min_beat_interval:
                            beats.append(time_sec)
                            last_beat_time = time_sec
                            
            logger.info(f"🥁 Detected {len(beats)} beats in track.")
            return beats

        except Exception as e:
            logger.error(f"❌ WAV processing failed: {e}")
            return []

    def _detect_drops(self, envelopes: list, beats: list, window_size: float,
                      drop_ratio: float = 2.5, look_back_sec: float = 0.6,
                      look_ahead_sec: float = 0.3) -> list:
        """
        Detect beat DROPS: moments where energy surges suddenly after relative quiet.

        A drop is identified when:
          post_energy / pre_energy > drop_ratio

        Args:
            envelopes   : RMS envelope list from _process_wav
            beats       : List of beat timestamps (seconds)
            window_size : Duration of each envelope window (seconds)
            drop_ratio  : How much louder the post-window must be vs pre-window (default 2.5x)
            look_back_sec : How far before the beat to sample pre-energy
            look_ahead_sec: How far after the beat to sample post-energy

        Returns:
            List of drop timestamps (subset of beats)
        """
        drops = []
        total_windows = len(envelopes)
        back_windows  = max(1, int(look_back_sec  / window_size))
        ahead_windows = max(1, int(look_ahead_sec / window_size))

        for beat_time in beats:
            beat_idx = int(beat_time / window_size)

            # Pre-beat energy window
            pre_start = max(0, beat_idx - back_windows)
            pre_end   = beat_idx
            pre_slice = envelopes[pre_start:pre_end] if pre_end > pre_start else []

            # Post-beat energy window
            post_start = beat_idx
            post_end   = min(total_windows, beat_idx + ahead_windows)
            post_slice = envelopes[post_start:post_end] if post_end > post_start else []

            if not pre_slice or not post_slice:
                continue

            pre_energy  = sum(pre_slice)  / len(pre_slice)
            post_energy = sum(post_slice) / len(post_slice)

            # Avoid division by zero; also require post_energy to be meaningful
            if pre_energy < 50:  # Very quiet pre-window — classic drop setup
                pre_energy = max(pre_energy, 50)

            ratio = post_energy / pre_energy
            if ratio >= drop_ratio:
                drops.append(beat_time)
                logger.debug(f"💥 Drop detected at {beat_time:.2f}s "
                             f"(pre={pre_energy:.0f}, post={post_energy:.0f}, ratio={ratio:.1f}x)")

        logger.info(f"💥 Beat drops detected: {len(drops)} / {len(beats)} beats")
        return drops

    def analyze_beats_with_drops(self, audio_path: str) -> dict:
        """
        Full psycho-acoustic analysis: beats, drops, BPM, avg_energy, vibe.

        Returns:
            {
              "beats":      [{"time": float, "energy": float}],
              "drops":      [float],
              "tempo":      float,   # BPM
              "avg_energy": float,   # 0-1 normalised mean energy
              "vibe":       str,     # explosive|hype|groove|cinematic|ambient
            }
        """
        if not os.path.exists(audio_path):
            logger.error(f"❌ Audio file not found: {audio_path}")
            return {"beats": [], "drops": []}

        if os.path.getsize(audio_path) < 1024:
            return {"beats": [], "drops": []}

        # ── Guard: do not attempt WAV extraction if no audio stream present ──
        if not self._has_audio_stream(audio_path):
            logger.warning(f"⚠️ analyze_beats_with_drops: no audio stream in '{audio_path}' — returning empty beats.")
            return {"beats": [], "drops": []}

        import tempfile
        fd, temp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        try:
            cmd = [
                FFMPEG_BIN, "-y", "-i", audio_path,
                "-ac", "1", "-ar", "44100", "-acodec", "pcm_s16le",
                temp_wav
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True, check=True)

            # Re-use the WAV processing but also capture the envelope list for drop detection
            # beats is now a list of float timestamps, but we want to return {"time": t, "energy": e}
            beats_raw, envelopes = self._process_wav_with_envelopes(temp_wav)
            drops_raw = self._detect_drops(envelopes, beats_raw, self.window_size)
            
            # Now we must construct the {"time": float, "energy": float} structure
            max_energy = max(envelopes) if envelopes else 1.0
            if max_energy == 0:
                max_energy = 1.0

            beats_full = []
            for b in beats_raw:
                idx = int(b / self.window_size)
                # Take maximum around the beat for robustness
                start_i = max(0, idx - 2)
                end_i = min(len(envelopes), idx + 2)
                local_env = envelopes[start_i:end_i]
                e_val = max(local_env) if local_env else 0
                beats_full.append({
                    "time": b,
                    "energy": round(min(1.0, e_val / max_energy), 3)
                })

            drops_full = [b["time"] for b in beats_full if b["time"] in drops_raw]

            # ── Psycho-acoustic metadata ───────────────────────────────────
            # BPM from inter-beat intervals
            tempo = 120.0
            if len(beats_raw) >= 4:
                intervals = [
                    beats_raw[i+1] - beats_raw[i]
                    for i in range(len(beats_raw)-1)
                    if beats_raw[i+1] - beats_raw[i] > 0
                ]
                if intervals:
                    avg_interval = sum(intervals) / len(intervals)
                    tempo = round(60.0 / avg_interval, 1)

            # Average energy of detected beats (0-1)
            avg_energy = 0.5
            if beats_full:
                avg_energy = round(
                    sum(b["energy"] for b in beats_full) / len(beats_full), 3
                )

            # Vibe classification
            def _vibe(bpm, eng):
                if bpm > 145 or (bpm > 120 and eng > 0.75): return "explosive"
                if bpm > 115: return "hype"
                if bpm > 85:  return "groove"
                if bpm > 60:  return "cinematic"
                return "ambient"

            vibe = _vibe(tempo, avg_energy)
            logger.info(
                f"🎧 [BeatEngine] tempo={tempo}BPM avg_energy={avg_energy:.2f} "
                f"vibe={vibe} beats={len(beats_full)} drops={len(drops_full)}"
            )

            return {
                "beats":      beats_full,
                "drops":      drops_full,
                "tempo":      tempo,
                "avg_energy": avg_energy,
                "vibe":       vibe,
            }

        except subprocess.CalledProcessError as e:
            logger.error(f"⚠️ analyze_beats_with_drops FFmpeg error: {e.stderr}")
            return {"beats": [], "drops": []}
        except Exception as e:
            logger.error(f"⚠️ analyze_beats_with_drops failed: {e}")
            return {"beats": [], "drops": []}
        finally:
            if os.path.exists(temp_wav):
                try: os.remove(temp_wav)
                except: pass

    def _process_wav_with_envelopes(self, wav_path: str):
        """Like _process_wav but also returns the envelope list for drop detection."""
        beats = []
        envelopes = []
        try:
            with wave.open(wav_path, 'rb') as wf:
                framerate   = wf.getframerate()
                nframes     = wf.getnframes()
                sampwidth   = wf.getsampwidth()
                if sampwidth != 2:
                    return [], []

                raw_data = wf.readframes(nframes)
                count    = len(raw_data) // 2
                samples  = struct.unpack(f"<{count}h", raw_data)

                window_samples = int(framerate * self.window_size)
                for i in range(0, len(samples), window_samples):
                    chunk = samples[i:i + window_samples]
                    if not chunk: continue
                    rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
                    envelopes.append(rms)

                local_window   = 40
                last_beat_time = -self.min_beat_interval

                for i, amp in enumerate(envelopes):
                    start      = max(0, i - local_window // 2)
                    end        = min(len(envelopes), i + local_window // 2)
                    context    = envelopes[start:end]
                    avg_energy = sum(context) / len(context) if context else 0
                    threshold  = avg_energy * self.sensitivity
                    time_sec   = i * self.window_size

                    if amp > threshold and amp > 1000:
                        if time_sec - last_beat_time >= self.min_beat_interval:
                            beats.append(time_sec)
                            last_beat_time = time_sec

            return beats, envelopes

        except Exception as e:
            logger.error(f"❌ _process_wav_with_envelopes failed: {e}")
            return [], []

# Global Instance
engine = BeatEngine()


def get_beats(path: str) -> list:
    return engine.analyze_beats(path)


def get_beats_with_drops(path: str) -> dict:
    return engine.analyze_beats_with_drops(path)


def get_beats_preferring_original_audio(video_path: str) -> dict:
    use_original = os.getenv('USE_ORIGINAL_AUDIO_BEATS', 'yes').lower() in ('yes', 'true', '1')
    if use_original:
        video_stem = os.path.splitext(os.path.basename(video_path))[0]
        original_audio_dir = os.getenv('ORIGINAL_AUDIO_DIR', 'Original_audio')
        mp3_name = f'{video_stem}.mp3'
        candidates = [
            os.path.join(original_audio_dir, 'active', mp3_name),
            os.path.join(original_audio_dir, 'cooldown', mp3_name),
            os.path.join(original_audio_dir, mp3_name),
        ]
        for mp3_path in candidates:
            if os.path.isfile(mp3_path) and os.path.getsize(mp3_path) > 1024:
                logger.info(f'[BEAT_ENGINE] Using clean Original_audio track: {mp3_path}')
                return engine.analyze_beats_with_drops(mp3_path)
        logger.debug(f'[BEAT_ENGINE] No Original_audio match for {video_stem!r} -- falling back.')
    return engine.analyze_beats_with_drops(video_path)


def get_candidate_audio_metadata(audio_dir=None, max_tracks: int = 5) -> list:
    """Scans Original_audio/active/ for curated BGM candidates. Returns list of dicts for Gemini.
    Reuses pre-computed metadata from pool_metadata.json or indexed_audio_analysis.json when available.
    """
    if audio_dir is None:
        _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        audio_dir = os.path.join(_repo_root, 'Original_audio')

    active_dir = os.path.join(audio_dir, 'active')
    if not os.path.isdir(active_dir):
        logger.debug(f'[BGM_POOL] No active/ pool directory at {active_dir}')
        return []

    # Load pre-computed cache from pool_metadata.json & indexed_audio_analysis.json if available
    pool_meta_path = os.path.join(audio_dir, 'pool_metadata.json')
    indexed_meta_path = os.path.join(audio_dir, 'indexed_audio_analysis.json')
    meta_cache = {}
    indexed_cache = {}

    if os.path.exists(pool_meta_path):
        try:
            with open(pool_meta_path, 'r', encoding='utf-8') as f:
                pm = json.load(f)
                meta_cache = pm.get('files', {})
        except Exception:
            pass

    if os.path.exists(indexed_meta_path):
        try:
            with open(indexed_meta_path, 'r', encoding='utf-8') as f:
                indexed_cache = json.load(f)
        except Exception:
            pass

    valid_exts = ('.mp3', '.wav', '.m4a', '.aac')
    _artifact_kws = ('_extracted', 'tmp', '_reaction', '_textreaction', 'watermark',
                     'first_shot', 'first_shots', 'general_intro', 'intro_mixed_temp',
                     'final_compilation')

    audio_files = []
    for fname in os.listdir(active_dir):
        if not fname.lower().endswith(valid_exts):
            continue
        if any(kw in fname.lower() for kw in _artifact_kws):
            logger.debug(f'[BGM_POOL] Skipping artifact: {fname}')
            continue
        audio_files.append(os.path.join(active_dir, fname))
        if len(audio_files) >= max_tracks:
            break

    if not audio_files:
        logger.info('[BGM_POOL] No curated BGM tracks found in active/ pool.')
        return []

    _engine = None
    candidates = []
    for file_path in audio_files[:max_tracks]:
        fname = os.path.basename(file_path)
        stem = os.path.splitext(fname)[0]

        # 1. Reuse JSON metadata cache if available
        cached_info = meta_cache.get(fname) or indexed_cache.get(stem) or indexed_cache.get(fname)
        if cached_info and (cached_info.get('bpm', 0.0) > 0 or cached_info.get('tempo_bpm', 0.0) > 0):
            bpm_val = cached_info.get('bpm') or cached_info.get('tempo_bpm', 120.0)
            energy_val = cached_info.get('energy') or cached_info.get('avg_energy', 0.5)
            drops_val = cached_info.get('drop_times') or cached_info.get('drops', [])
            beats_val = cached_info.get('beats', [])
            candidates.append({
                'track_name':  fname,
                'file_path':   os.path.abspath(file_path),
                'bpm':         bpm_val,
                'avg_energy':  energy_val,
                'beats':       beats_val,
                'drops':       drops_val[:3],
                'num_beats':   len(beats_val) if beats_val else int(bpm_val * 0.25),
                'pool':        'active',
            })
            continue

        # 2. Live analysis ONLY for new/uncached tracks
        try:
            if _engine is None:
                _engine = BeatEngine()
            res = _engine.analyze_beats_with_drops(file_path)
            candidates.append({
                'track_name':  fname,
                'file_path':   os.path.abspath(file_path),
                'bpm':         res.get('bpm', 120.0),
                'avg_energy':  res.get('avg_energy', 0.5),
                'beats':       res.get('beats', []),
                'drops':       res.get('drops', [])[:3],
                'num_beats':   len(res.get('beats', [])),
                'pool':        'active',
            })
        except Exception as err:
            logger.debug(f'[BGM_POOL] Failed analysis for {file_path}: {err}')

    logger.info(f'[BGM_POOL] Scanned {len(candidates)} candidate BGM track(s) from active/ pool.')
    return candidates


def analyze_beats_with_drops(audio_path: str) -> dict:
    """Top-level convenience wrapper for BeatEngine().analyze_beats_with_drops()."""
    _engine = BeatEngine()
    return _engine.analyze_beats_with_drops(audio_path)
