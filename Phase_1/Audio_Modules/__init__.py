"""
Phase_1 / Audio_Modules Package
================================
Core Audio Processing, Beat Analysis, Whisper Transcription, and Rhythm Alignment Pipeline.
"""

from .audio_extractor import extract_audio
from .beat_engine import BeatEngine
from .run_faster_whisper_transcription import transcribe_audio_file
from .lyric_rhythm_aligner import analyze_music
from .audio_pool_manager import AudioPoolManager

__all__ = [
    "extract_audio",
    "BeatEngine",
    "transcribe_audio_file",
    "analyze_music",
    "AudioPoolManager",
]
