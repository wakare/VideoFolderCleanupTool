from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Iterable


VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}


class ProbeError(RuntimeError):
    pass


def iter_video_files(roots: Iterable[Path], extensions: set[str] | None = None) -> Iterable[Path]:
    allowed = {ext.lower() for ext in (extensions or VIDEO_EXTENSIONS)}
    for root in roots:
        if root.is_file() and root.suffix.lower() in allowed:
            yield root
            continue
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in allowed:
                yield path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quick_hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    size = path.stat().st_size
    offsets = {
        0,
        max(0, (size // 2) - (chunk_size // 2)),
        max(0, size - chunk_size),
    }
    digest = hashlib.sha256()
    digest.update(f"quick-v1:size={size}:chunk={chunk_size}".encode("ascii"))
    with path.open("rb") as file:
        for offset in sorted(offsets):
            file.seek(offset)
            chunk = file.read(min(chunk_size, max(0, size - offset)))
            digest.update(offset.to_bytes(8, "little", signed=False))
            digest.update(len(chunk).to_bytes(8, "little", signed=False))
            digest.update(chunk)
    return digest.hexdigest()


def parse_ratio(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return float(numerator) / denominator_value
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if value in (None, "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: object) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def probe_video(path: Path, timeout_seconds: int | None = 60) -> dict[str, object]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        (
            "stream=codec_type,codec_name,width,height,avg_frame_rate,"
            "r_frame_rate,duration,nb_frames,bit_rate:"
            "format=duration,size,bit_rate"
        ),
        "-print_format",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {timeout_seconds}s") from exc
    except OSError as exc:
        raise ProbeError(f"failed to run ffprobe: {exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ProbeError(stderr or f"ffprobe exited with {completed.returncode}")

    try:
        data = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"invalid ffprobe JSON: {exc}") from exc

    streams = data.get("streams") or []
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if not video_stream:
        raise ProbeError("no video stream found")

    format_data = data.get("format") or {}
    duration = _number(video_stream.get("duration")) or _number(format_data.get("duration"))
    bit_rate = _integer(video_stream.get("bit_rate")) or _integer(format_data.get("bit_rate"))
    frames = _integer(video_stream.get("nb_frames"))

    return {
        "duration": duration,
        "width": _integer(video_stream.get("width")),
        "height": _integer(video_stream.get("height")),
        "codec": video_stream.get("codec_name"),
        "bit_rate": bit_rate,
        "fps": parse_ratio(video_stream.get("avg_frame_rate"))
        or parse_ratio(video_stream.get("r_frame_rate")),
        "frames": frames,
    }
