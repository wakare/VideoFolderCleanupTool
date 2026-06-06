from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoRecord:
    path: Path
    root: Path
    size: int
    mtime_ns: int
    sha256: str | None
    duration: float | None
    width: int | None
    height: int | None
    codec: str | None
    bit_rate: int | None
    fps: float | None
    frames: int | None
    fingerprint: tuple[int, ...]
    fingerprint_interval: float | None
    error: str | None = None
    quick_hash: str | None = None
    fingerprint_profile: str | None = None

    @property
    def pixel_count(self) -> int:
        return (self.width or 0) * (self.height or 0)

    @property
    def has_fingerprint(self) -> bool:
        return bool(self.fingerprint) and not self.error


@dataclass(frozen=True)
class Match:
    kind: str
    left: VideoRecord
    right: VideoRecord
    score: float
    offset_seconds: float | None = None
    note: str | None = None


@dataclass(frozen=True)
class PlanItem:
    action: str
    reason: str
    victim: Path
    keeper: Path
    confidence: float
    overlap_ratio: float | None = None
    offset_seconds: float | None = None
    details: dict[str, object] | None = None
