from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Callable


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


def extract_video_fingerprint_seek(
    path: Path,
    *,
    duration_seconds: float,
    interval_seconds: float = 30.0,
    max_frames: int | None = None,
    timeout_per_frame_seconds: int | None = 15,
    progress_callback: Callable[[int, int, float, float], None] | None = None,
) -> tuple[int, ...]:
    if duration_seconds <= 0:
        raise FingerprintError("duration_seconds must be positive for seek extraction")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    frame_count = int(duration_seconds // interval_seconds) + 1
    if max_frames:
        frame_count = min(frame_count, max_frames)

    hashes: list[int] = []
    failures: list[str] = []
    for index in range(frame_count):
        timestamp = index * interval_seconds
        if progress_callback:
            progress_callback(index + 1, frame_count, timestamp, duration_seconds)
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=bilinear,format=gray",
            "-an",
            "-sn",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_per_frame_seconds,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{timestamp:.3f}s timed out")
            continue
        except OSError as exc:
            raise FingerprintError(f"failed to run ffmpeg: {exc}") from exc

        if completed.returncode != 0 or len(completed.stdout) < FRAME_BYTES:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            failures.append(f"{timestamp:.3f}s: {stderr or 'no frame'}")
            continue
        hashes.append(dhash_from_gray_9x8(completed.stdout[:FRAME_BYTES]))

    if not hashes:
        detail = "; ".join(failures[:3])
        raise FingerprintError(detail or "seek extraction produced no frames")
    return tuple(hashes)


def sampled_timestamps(
    *,
    duration_seconds: float,
    interval_seconds: float,
    max_frames: int | None = None,
) -> list[float]:
    if duration_seconds <= 0:
        raise FingerprintError("duration_seconds must be positive for seek extraction")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    frame_count = int(duration_seconds // interval_seconds) + 1
    if max_frames:
        frame_count = min(frame_count, max_frames)
    return [index * interval_seconds for index in range(frame_count)]


def dhash_from_pyav_frame(frame: object) -> int:
    small = frame.reformat(width=FRAME_WIDTH, height=FRAME_HEIGHT, format="gray")
    plane = small.planes[0]
    raw = bytes(plane)
    line_size = plane.line_size
    rows = [
        raw[row * line_size : row * line_size + FRAME_WIDTH]
        for row in range(FRAME_HEIGHT)
    ]
    return dhash_from_gray_9x8(b"".join(rows))


def extract_video_fingerprint_pyav_seek(
    path: Path,
    *,
    duration_seconds: float,
    interval_seconds: float = 30.0,
    max_frames: int | None = None,
    max_decode_frames_per_seek: int = 120,
) -> tuple[int, ...]:
    try:
        import av
    except ImportError as exc:
        raise FingerprintError(
            "PyAV is not installed; install the optional 'pyav' extra or use --fingerprint-mode seek"
        ) from exc
    av_error_module = getattr(av, "error", None)
    av_error = getattr(av, "AVError", None) or getattr(av_error_module, "FFmpegError", Exception)

    timestamps = sampled_timestamps(
        duration_seconds=duration_seconds,
        interval_seconds=interval_seconds,
        max_frames=max_frames,
    )
    hashes: list[int] = []
    failures: list[str] = []

    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            time_base = float(stream.time_base)
            for timestamp in timestamps:
                target_pts = int(timestamp / time_base) if time_base else 0
                try:
                    container.seek(target_pts, stream=stream, backward=True, any_frame=False)
                except av_error as exc:
                    failures.append(f"{timestamp:.3f}s seek failed: {exc}")
                    continue

                selected = None
                for frame_index, frame in enumerate(container.decode(stream)):
                    if frame.time is None or frame.time >= timestamp:
                        selected = frame
                        break
                    if frame_index >= max_decode_frames_per_seek:
                        break
                if selected is None:
                    failures.append(f"{timestamp:.3f}s: no frame")
                    continue
                hashes.append(dhash_from_pyav_frame(selected))
    except av_error as exc:
        raise FingerprintError(f"PyAV failed to read video: {exc}") from exc
    except OSError as exc:
        raise FingerprintError(f"PyAV failed to open video: {exc}") from exc

    if not hashes:
        detail = "; ".join(failures[:3])
        raise FingerprintError(detail or "PyAV seek extraction produced no frames")
    return tuple(hashes)
