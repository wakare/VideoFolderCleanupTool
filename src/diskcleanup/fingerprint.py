from __future__ import annotations

import math
import subprocess
from pathlib import Path


FRAME_WIDTH = 9
FRAME_HEIGHT = 8
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT


class FingerprintError(RuntimeError):
    pass


def hamming64(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def dhash_from_gray_9x8(frame: bytes) -> int:
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"expected {FRAME_BYTES} bytes, got {len(frame)}")

    value = 0
    bit = 0
    for row in range(FRAME_HEIGHT):
        base = row * FRAME_WIDTH
        for col in range(FRAME_WIDTH - 1):
            left = frame[base + col]
            right = frame[base + col + 1]
            if left > right:
                value |= 1 << bit
            bit += 1
    return value


def fingerprint_from_raw_frames(raw: bytes) -> tuple[int, ...]:
    usable = len(raw) - (len(raw) % FRAME_BYTES)
    return tuple(
        dhash_from_gray_9x8(raw[index : index + FRAME_BYTES])
        for index in range(0, usable, FRAME_BYTES)
    )


def build_ffmpeg_fps(interval_seconds: float) -> str:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    fps = 1.0 / interval_seconds
    if math.isclose(fps, round(fps)):
        return str(int(round(fps)))
    return f"{fps:.6f}".rstrip("0").rstrip(".")


def extract_video_fingerprint(
    path: Path,
    *,
    interval_seconds: float = 2.0,
    max_frames: int | None = None,
    timeout_seconds: int | None = None,
) -> tuple[int, ...]:
    filters = (
        f"fps={build_ffmpeg_fps(interval_seconds)},"
        f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=bilinear,"
        "format=gray"
    )
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        filters,
        "-an",
        "-sn",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
    ]
    if max_frames:
        command.extend(["-frames:v", str(max_frames)])
    command.append("pipe:1")

    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError(f"ffmpeg timed out after {timeout_seconds}s") from exc
    except OSError as exc:
        raise FingerprintError(f"failed to run ffmpeg: {exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FingerprintError(stderr or f"ffmpeg exited with {completed.returncode}")

    return fingerprint_from_raw_frames(completed.stdout)
