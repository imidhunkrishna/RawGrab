"""
scratch/run_faster_whisper_transcription.py — Step 2.2 Faster-Whisper Engine (0 Tokens)
=======================================================================================
Transcribes reference audio into word-level millisecond timestamps using local CPU/GPU.
Trims a 120s sample clip for instantaneous sub-second execution.
Saves result to output/whisper_transcript_vagabond.json.
"""

import os
import sys
import json
import time
import subprocess

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
audio_path = os.path.join(repo_root, "Tools", "Original_audio", "active", "VAGABOND_22-24___Vol_03__Detailed_malayalam_explanation__Takehiko_Inoue__Manga_vagabond_vol3.wav")
tmp_sample_audio = os.path.join(repo_root, "_karaoke_tmp", "sample_audio_120s.wav")


def transcribe_audio_file(target_audio_path: str, model_size: str = "tiny") -> dict:
    """Transcribes an audio file into word-level millisecond timestamps using faster-whisper."""
    t0 = time.time()
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, info = model.transcribe(target_audio_path, beam_size=3, word_timestamps=True)

        word_transcript = []
        segment_list = []

        for segment in segments:
            segment_list.append({
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "text": segment.text.strip()
            })
            if segment.words:
                for word in segment.words:
                    word_transcript.append({
                        "word": word.word.strip(),
                        "start": round(word.start, 2),
                        "end": round(word.end, 2)
                    })

        t1 = time.time()
        return {
            "status": "success",
            "execution_time_seconds": round(t1 - t0, 3),
            "tokens_spent": 0,
            "language_detected": info.language,
            "language_probability": round(info.language_probability, 2),
            "total_segments": len(segment_list),
            "total_words": len(word_transcript),
            "segments": segment_list,
            "word_timestamps": word_transcript
        }
    except Exception as err:
        return {"status": "error", "error": str(err), "word_timestamps": []}

def main():
    print("=" * 60)
    print("EXECUTING STEP 2.2: FASTER-WHISPER AUDIO TIMESTAMP ENGINE")
    print("=" * 60)

    if not os.path.exists(audio_path):
        print(f"Audio file not found: {audio_path}")
        return

    os.makedirs(os.path.dirname(tmp_sample_audio), exist_ok=True)

    # Trim 120s clip for sub-second execution
    cmd = [
        "ffmpeg", "-y", "-ss", "0", "-t", "120",
        "-i", audio_path, "-c", "copy",
        tmp_sample_audio
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    print(f"Loading Audio Sample (120s): {os.path.basename(tmp_sample_audio)}")
    print("Running local Faster-Whisper model ('tiny' model on CPU)...")

    t0 = time.time()
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, info = model.transcribe(tmp_sample_audio, beam_size=3, word_timestamps=True)

        word_transcript = []
        segment_list = []

        for segment in segments:
            segment_list.append({
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "text": segment.text.strip()
            })
            if segment.words:
                for word in segment.words:
                    word_transcript.append({
                        "word": word.word.strip(),
                        "start": round(word.start, 2),
                        "end": round(word.end, 2)
                    })

        t1 = time.time()
        elapsed = round(t1 - t0, 3)

        out_data = {
            "status": "success",
            "execution_time_seconds": elapsed,
            "tokens_spent": 0,
            "language_detected": info.language,
            "language_probability": round(info.language_probability, 2),
            "total_segments": len(segment_list),
            "total_words": len(word_transcript),
            "segments": segment_list,
            "word_timestamps": word_transcript
        }

        out_path = os.path.join(repo_root, "output", "whisper_transcript_vagabond.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out_data, f, indent=2, ensure_ascii=False)

        print("\nFASTER-WHISPER TRANSCRIPTION COMPLETE!")
        print(f"Execution Time: {elapsed} seconds")
        print(f"Tokens Spent: 0 TOKENS (100% Free Local CPU)")
        print(f"Detected Language: {info.language} (Probability: {info.language_probability:.2f})")
        print(f"Total Transcribed Segments: {len(segment_list)}")
        print(f"Total Transcribed Words: {len(word_transcript)}")
        print(f"Saved Transcript JSON: {out_path}")
        print("\nFIRST 5 TRANSCRIBED SEGMENTS:")
        for seg in segment_list[:5]:
            print(f"  [{seg['start']}s -> {seg['end']}s]: {seg['text']}")

    except Exception as e:
        print(f"Faster-Whisper exception: {e}")

if __name__ == "__main__":
    main()
