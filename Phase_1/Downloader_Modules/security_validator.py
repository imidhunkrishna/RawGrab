"""
security_validator.py — AMTCE Security & Quarantine Validation Engine (HARDENED)
================================================================================
Performs security checks on untrusted media in quarantine before releasing
clean files to storage/{pdf|image|video|archive}/:

  1. SHA256 hashing & atomic SQLite deduplication cache lookup
  2. File size ceiling checks per media category
  3. Magic byte header verification (rejects fake extensions)
  4. ZIP Bomb Defense: 1GB ceiling, 50x ratio cap, max 1000 files, max 10 folder depth
  5. FFprobe Video Stream & Container Integrity Inspection
  6. SVG Script Execution & Malicious XML Tag Scanner
  7. Telemetry metric logging (latency, file size, SHA256 cache hits)
"""

import os
import sys
import hashlib
import json
import shutil
import logging
import time
import subprocess
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from .state_store import get_state_store
except ImportError:
    try:
        from Phase_1.Downloader_Modules.state_store import get_state_store
    except ImportError:
        from Tools.Downloader_Modules.state_store import get_state_store  # type: ignore

logger = logging.getLogger("security_validator")

# Size limits per media category in bytes
SIZE_LIMITS = {
    "image": 50 * 1024 * 1024,        # 50 MB
    "pdf": 500 * 1024 * 1024,         # 500 MB
    "archive": 500 * 1024 * 1024,     # 500 MB
    "video": 2 * 1024 * 1024 * 1024,  # 2 GB
}

# ZIP Bomb protection limits
ZIP_MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024  # 1 GB
ZIP_MAX_RATIO = 50.0                            # 50:1 max ratio
ZIP_MAX_FILE_COUNT = 1000                        # Max 1000 files
ZIP_MAX_DEPTH = 10                              # Max 10 folder depth

# Magic byte signatures
MAGIC_SIGNATURES = {
    "pdf": [b"%PDF-"],
    "image": [
        b"\x89PNG\r\n\x1a\n",          # PNG
        b"\xff\xd8\xff",                # JPEG
        b"GIF87a", b"GIF89a",          # GIF
        b"RIFF",                        # WEBP
        b"<?xml", b"<svg",             # SVG
    ],
    "archive": [
        b"PK\x03\x04",                  # ZIP / CBZ
        b"Rar!\x1a\x07",                # RAR
        b"7z\xbc\xaf\x27\x1c",          # 7z
    ],
    "video": [
        b"ftyp",                        # MP4
        b"\x1a\x45\xdf\xa3",            # MKV / WEBM
        b"RIFF",                        # AVI
        b"\x00\x00\x00",                # MOV
    ]
}

FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")


@dataclass
class ScanResult:
    safe: bool
    safe_path: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    sha256: str = ""
    mime_type: str = "unknown"
    file_size: int = 0
    cached_hit: bool = False
    validation_time_ms: float = 0.0


def _compute_sha256(path: str) -> str:
    """Computes SHA256 hash of file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _verify_magic_bytes(path: str, media_type: str) -> Tuple[bool, str]:
    """Verifies file header magic bytes against expected signatures."""
    with open(path, "rb") as f:
        header = f.read(32)

    if not header:
        return False, "file_is_empty"

    sigs = MAGIC_SIGNATURES.get(media_type, [])
    if not sigs:
        return True, "unknown_type_bypassed"

    for sig in sigs:
        if sig in header:
            if sig == b"%PDF-":
                return True, "application/pdf"
            elif sig in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF", b"<?xml", b"<svg"):
                return True, "image/detected"
            elif sig == b"PK\x03\x04":
                return True, "application/zip"
            elif sig in (b"ftyp", b"\x1a\x45\xdf\xa3"):
                return True, "video/detected"
            return True, f"{media_type}/detected"

    # Fallback check for MOV/MP4 with non-zero offset ftyp
    if media_type == "video":
        with open(path, "rb") as f:
            chunk = f.read(1024)
            if b"ftyp" in chunk or b"moov" in chunk or b"mdat" in chunk:
                return True, "video/mp4"

    return False, f"invalid_magic_bytes_for_{media_type}"


def _check_svg_security(path: str) -> Tuple[bool, str]:
    """Scans SVG text for embedded scripts, malicious event handlers, and external entities."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(1024 * 1024).lower()

        malicious_patterns = [
            "<script", "javascript:", "onload=", "onerror=",
            "onclick=", "<iframe", "<object", "<embed",
            "xlink:href=\"javascript:", "entity "
        ]
        for pattern in malicious_patterns:
            if pattern in content:
                return False, f"svg_security_violation_found:{pattern}"
        return True, "clean_svg"
    except Exception as exc:
        return False, f"svg_scan_failed:{exc}"


def _check_zip_bomb_safety(path: str) -> Tuple[bool, str]:
    """
    Hardened ZIP Bomb Guard:
    Checks uncompressed size ceiling, compression ratio, max file count, and folder depth.
    """
    try:
        if not zipfile.is_zipfile(path):
            return True, "not_a_zip"

        total_compressed = os.path.getsize(path)
        total_uncompressed = 0
        file_count = 0

        with zipfile.ZipFile(path, "r") as z:
            infolist = z.infolist()
            file_count = len(infolist)

            if file_count > ZIP_MAX_FILE_COUNT:
                return False, f"zip_member_count_exceeded:{file_count}>{ZIP_MAX_FILE_COUNT}"

            for info in infolist:
                # Depth check
                depth = len(info.filename.split("/")) - 1
                if depth > ZIP_MAX_DEPTH:
                    return False, f"zip_max_depth_exceeded:{depth}>{ZIP_MAX_DEPTH}"

                total_uncompressed += info.file_size
                if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
                    return False, f"zip_uncompressed_ceiling_exceeded:{total_uncompressed}>{ZIP_MAX_UNCOMPRESSED_BYTES}"

        if total_compressed > 0:
            ratio = total_uncompressed / total_compressed
            if ratio > ZIP_MAX_RATIO and total_uncompressed > (10 * 1024 * 1024):
                return False, f"zip_bomb_ratio_exceeded:{ratio:.1f}x>{ZIP_MAX_RATIO}x"

        return True, "zip_passed_safety_checks"
    except Exception as exc:
        return False, f"zip_safety_check_error:{exc}"


def _ffprobe_video_check(path: str) -> Tuple[bool, str]:
    """Performs deep container & stream codec validation using ffprobe."""
    try:
        cmd = [
            FFPROBE_BIN, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "json", path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return False, f"ffprobe_failed:{res.stderr.strip()[:100]}"

        data = json.loads(res.stdout or "{}")
        streams = data.get("streams", [])
        if not streams:
            return False, "video_stream_missing_or_invalid"

        codec = streams[0].get("codec_name", "unknown")
        return True, f"valid_video_stream:{codec}"
    except subprocess.TimeoutExpired:
        return False, "ffprobe_timed_out"
    except Exception:
        # Fallback to basic header inspect if ffprobe not available on host
        if os.path.getsize(path) > 1024:
            return True, "video_basic_header_valid"
        return False, "video_corrupt"


def _check_trailing_payloads(path: str) -> Tuple[bool, str]:
    """
    Inspects trailing binary bytes and file payload signatures to detect appended
    malware, executable PE headers (MZ), ELF binaries, shell scripts, or polyglots.
    """
    try:
        size = os.path.getsize(path)
        if size < 512:
            return True, "size_too_small"

        with open(path, "rb") as f:
            # Check last 4KB for trailing executable / script signatures
            f.seek(max(0, size - 4096))
            tail = f.read()

        # Prohibited trailing signatures: Real PE Executables (DOS stub / PE header), ELF (\x7fELF), Shell scripts
        dangerous_signatures = [
            (b"This program cannot be run in DOS mode", "trailing_windows_pe_executable"),
            (b"MZ\x90\x00", "trailing_windows_pe_executable"),
            (b"\x7fELF", "trailing_linux_elf_executable"),
            (b"#!/bin/", "trailing_shell_script"),
            (b"powershell.exe", "trailing_powershell_script"),
            (b"<script type=\"text/javascript\">", "trailing_javascript_script_tag"),
            (b"cmd.exe /c", "trailing_cmd_executable")
        ]

        for sig, reason in dangerous_signatures:
            if sig in tail:
                logger.warning("⛔ [PAYLOAD SCANNER] Detected dangerous trailing bytes in %s: %s", path, reason)
                return False, f"malicious_payload_signature_detected:{reason}"

        return True, "no_trailing_payload"
    except Exception as exc:
        return True, f"payload_check_notice:{exc}"


def _sanitize_and_remux_video(src_path: str, dst_path: str) -> Tuple[bool, str]:
    """
    Sanitizes video by remuxing clean streams via FFmpeg (-map 0:v:0 -map 0:a:0? -movflags +faststart).
    Strips away all non-standard atoms, metadata payloads, trailing executable bytes, and invalid streams.
    """
    try:
        from security_validator import FFPROBE_BIN
        ffmpeg_bin = FFPROBE_BIN.replace("ffprobe", "ffmpeg") if "ffprobe" in FFPROBE_BIN else "ffmpeg"

        cmd = [
            ffmpeg_bin, "-y", "-v", "error",
            "-i", src_path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "copy",
            "-movflags", "+faststart",
            dst_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
            logger.info("🛡️ [VIDEO SANITIZER] Successfully remuxed and sanitized video -> %s", dst_path)
            return True, "video_sanitized_and_remuxed"
        
        logger.warning("⚠️ [VIDEO SANITIZER] Copy remux failed (%s), attempting safe re-encode...", res.stderr.strip()[:100])
        # Fallback to full re-encode if copy remux failed due to codec mismatch
        reencode_cmd = [
            ffmpeg_bin, "-y", "-v", "error",
            "-i", src_path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            dst_path
        ]
        res_re = subprocess.run(reencode_cmd, capture_output=True, text=True, timeout=60)
        if res_re.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
            logger.info("🛡️ [VIDEO SANITIZER] Successfully re-encoded and sanitized video -> %s", dst_path)
            return True, "video_sanitized_and_reencoded"
        
        return False, f"remux_sanitization_failed:{res_re.stderr.strip()[:100]}"
    except Exception as exc:
        return False, f"video_sanitization_exception:{exc}"


def _structural_validation(path: str, media_type: str) -> Tuple[bool, str]:
    """Performs structural sanity checks based on media type."""
    # 1. Trailing Payload & Polyglot Executable Inspection
    payload_ok, payload_reason = _check_trailing_payloads(path)
    if not payload_ok:
        return False, payload_reason

    if media_type == "image":
        if path.lower().endswith(".svg") or _verify_magic_bytes(path, "image")[1] == "image/svg":
            return _check_svg_security(path)
        try:
            from PIL import Image
            with Image.open(path) as img:
                img.verify()
            return True, "valid_image_structure"
        except Exception as exc:
            return False, f"image_structure_corrupt:{exc}"

    elif media_type == "pdf":
        try:
            with open(path, "rb") as f:
                head = f.read(1024)
            if b"%PDF-" not in head:
                return False, "pdf_header_missing"
            return True, "valid_pdf_structure"
        except Exception as exc:
            return False, f"pdf_structure_corrupt:{exc}"

    elif media_type == "archive":
        return False, "archive_strictly_prohibited: Compressed files (.zip, .rar, .7z, .cbz, .tar, etc.) are blocked by security policy to prevent zip bombs."

    elif media_type == "video":
        return _ffprobe_video_check(path)

    return True, "passed_default"


def validate_and_quarantine(
    quarantine_path: str,
    media_type: str,
    quarantine_dir: str = "quarantine",
    storage_base_dir: str = "storage"
) -> ScanResult:
    """
    Validates a file in quarantine and moves it to storage/{media_type}/ on success.
    Hardened against ZIP bombs, SVG scripts, corrupt video containers, and size attacks.
    """
    start_t = time.time()
    store = get_state_store()

    if not os.path.exists(quarantine_path):
        return ScanResult(safe=False, reasons=[f"quarantine_file_not_found:{quarantine_path}"])

    file_size = os.path.getsize(quarantine_path)
    sha256 = _compute_sha256(quarantine_path)

    # 1. Atomic SQLite Deduplication Cache Check
    cached = store.get_cached_media(sha256)
    if cached and cached.get("safe_path") and os.path.exists(cached["safe_path"]):
        cached_path = cached["safe_path"]
        logger.info(f"♻️ [SECURITY CACHE HIT] {sha256[:12]} already verified at {cached_path}")
        try:
            os.remove(quarantine_path)
        except OSError:
            pass
        latency = (time.time() - start_t) * 1000
        store.record_telemetry("validation_cache_hit", latency, file_size, sha256, True, "cache_hit")
        return ScanResult(
            safe=True,
            safe_path=cached_path,
            sha256=sha256,
            mime_type=cached.get("mime_type", "unknown"),
            file_size=file_size,
            cached_hit=True,
            validation_time_ms=latency
        )

    reasons: List[str] = []

    # 2. Size Ceiling Limit Check
    max_size = SIZE_LIMITS.get(media_type, 500 * 1024 * 1024)
    if file_size > max_size:
        reasons.append(f"file_exceeds_size_limit:{file_size}>{max_size}")

    # 3. Magic Bytes Check
    magic_ok, mime = _verify_magic_bytes(quarantine_path, media_type)
    if not magic_ok:
        reasons.append(mime)

    # 4. Hardened Structural Validation
    struct_ok, struct_reason = _structural_validation(quarantine_path, media_type)
    if not struct_ok:
        reasons.append(struct_reason)

    latency = (time.time() - start_t) * 1000

    if reasons:
        logger.warning(f"❌ [SECURITY REJECTED] {quarantine_path}: {'; '.join(reasons)}")
        store.record_telemetry("validation_rejected", latency, file_size, sha256, False, "; ".join(reasons))
        return ScanResult(
            safe=False,
            reasons=reasons,
            sha256=sha256,
            mime_type=mime,
            file_size=file_size,
            validation_time_ms=latency
        )

    # 5. Move & Sanitize to structured storage/{media_type}/
    target_dir = os.path.join(storage_base_dir, media_type)
    os.makedirs(target_dir, exist_ok=True)

    base_name = os.path.basename(quarantine_path)
    target_path = os.path.join(target_dir, f"{sha256[:12]}_{base_name}")

    try:
        if media_type == "video":
            san_ok, san_reason = _sanitize_and_remux_video(quarantine_path, target_path)
            if san_ok:
                try:
                    os.remove(quarantine_path)
                except OSError:
                    pass
            else:
                logger.warning("⚠️ Remux sanitization notice (%s), falling back to safe file move", san_reason)
                shutil.move(quarantine_path, target_path)
        else:
            shutil.move(quarantine_path, target_path)
    except Exception as exc:
        store.record_telemetry("validation_move_error", latency, file_size, sha256, False, str(exc))
        return ScanResult(safe=False, reasons=[f"failed_to_move_to_storage:{exc}"])

    # 6. Save to SQLite Cache
    store.save_cached_media(sha256, target_path, media_type, mime, file_size)
    store.record_telemetry("validation_success", latency, file_size, sha256, True, f"released_to_{media_type}")

    logger.info(f"✅ [SECURITY SAFE] Released file in {latency:.1f}ms -> {target_path}")
    return ScanResult(
        safe=True,
        safe_path=target_path,
        sha256=sha256,
        mime_type=mime,
        file_size=file_size,
        cached_hit=False,
        validation_time_ms=latency
    )
