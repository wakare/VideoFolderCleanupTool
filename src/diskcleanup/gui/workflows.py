from __future__ import annotations

import csv
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Iterable

from ..cli import (
    fingerprint_profile,
    parse_extensions,
    resolve_roots,
    scan_one_video,
    select_root,
    should_rescan,
)
from ..db import connect, get_record, list_profiles, list_records, remove_missing_records, upsert_record
from ..evidence import build_evidence_report
from ..media import iter_video_files
from ..mover import load_plan, move_from_plan
from ..planner import build_cleanup_plan, plan_item_to_dict
from .jobs import ProgressCallback


DEFAULT_DB = Path(".diskcleanup/cache.sqlite")


@dataclass(frozen=True)
class ScanSettings:
    paths: list[str]
    db: Path = DEFAULT_DB
    interval: float = 20.0
    max_frames: int = 0
    fingerprint_mode: str = "seek"
    profile_name: str | None = None
    extensions: list[str] | None = None
    force: bool = False
    hash_mode: str = "quick"
    skip_sha256: bool = False
    workers: int = 4
    seek_workers: int = 1
    ffmpeg_workers: int = 0
    probe_timeout: int | None = 60
    fingerprint_timeout: int | None = None
    seek_timeout: int | None = 15
    prune_missing: bool = False


@dataclass(frozen=True)
class PlanSettings:
    db: Path = DEFAULT_DB
    fingerprint_profile: str | None = None
    output: Path = Path(".diskcleanup/gui-cleanup-plan.json")
    min_overlap: float = 0.9
    partial_overlap: float = 0.45
    near_duplicate_similarity: float = 0.9
    hash_distance: int = 10
    candidate_mode: str = "indexed"
    min_anchor_votes: int = 3
    anchor_stride: int = 1
    max_anchor_bucket: int = 200


@dataclass(frozen=True)
class EvidenceSettings:
    plan: Path
    db: Path = DEFAULT_DB
    fingerprint_profile: str | None = None
    output_dir: Path = Path(".diskcleanup/gui-evidence")
    title: str = "Video Overlap Evidence Report"
    max_samples: int = 6
    hash_distance: int = 10
    screenshots: bool = True
    screenshot_height: int = 360
    screenshot_timeout: int = 60
    screenshot_workers: int = 1
    include_manual: bool = True


@dataclass(frozen=True)
class MoveSettings:
    plan: Path
    quarantine: Path = Path(".diskcleanup/quarantine")
    manifest: Path = Path(".diskcleanup/move-manifest.jsonl")
    apply: bool = False


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value: object, default: int, *, minimum: int | None = None) -> int:
    if value in (None, ""):
        parsed = default
    else:
        parsed = int(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"value must be >= {minimum}")
    return parsed


def parse_float(value: object, default: float, *, minimum: float | None = None) -> float:
    if value in (None, ""):
        parsed = default
    else:
        parsed = float(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"value must be >= {minimum}")
    return parsed


def parse_path(value: object, default: str | Path) -> Path:
    text = str(value or default).strip()
    if not text:
        raise ValueError("path cannot be empty")
    return Path(text).expanduser()


def parse_optional_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def parse_path_list(value: object) -> list[str]:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        text = str(value or "")
        normalized = text.replace(";", "\n")
        items = [line.strip().strip('"') for line in normalized.splitlines() if line.strip()]
    if not items:
        raise ValueError("at least one scan path is required")
    return items


def parse_extensions_payload(value: object) -> list[str] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def scan_settings_from_payload(payload: dict[str, object]) -> ScanSettings:
    mode = str(payload.get("fingerprint_mode") or "seek")
    if mode not in {"interval", "seek", "pyav-seek"}:
        raise ValueError("fingerprint_mode must be interval, seek, or pyav-seek")
    hash_mode = str(payload.get("hash_mode") or "quick")
    if hash_mode not in {"quick", "sha256", "none"}:
        raise ValueError("hash_mode must be quick, sha256, or none")
    return ScanSettings(
        paths=parse_path_list(payload.get("paths")),
        db=parse_path(payload.get("db"), DEFAULT_DB),
        interval=parse_float(payload.get("interval"), 20.0, minimum=0.001),
        max_frames=parse_int(payload.get("max_frames"), 0, minimum=0),
        fingerprint_mode=mode,
        profile_name=parse_optional_text(payload.get("profile_name")),
        extensions=parse_extensions_payload(payload.get("extensions")),
        force=parse_bool(payload.get("force"), False),
        hash_mode=hash_mode,
        skip_sha256=parse_bool(payload.get("skip_sha256"), False),
        workers=parse_int(payload.get("workers"), 4, minimum=1),
        seek_workers=parse_int(payload.get("seek_workers"), 1, minimum=1),
        ffmpeg_workers=parse_int(payload.get("ffmpeg_workers"), 0, minimum=0),
        probe_timeout=parse_int(payload.get("probe_timeout"), 60, minimum=1),
        fingerprint_timeout=(
            None
            if payload.get("fingerprint_timeout") in (None, "")
            else parse_int(payload.get("fingerprint_timeout"), 60, minimum=1)
        ),
        seek_timeout=parse_int(payload.get("seek_timeout"), 15, minimum=1),
        prune_missing=parse_bool(payload.get("prune_missing"), False),
    )


def plan_settings_from_payload(payload: dict[str, object]) -> PlanSettings:
    mode = str(payload.get("candidate_mode") or "indexed")
    if mode not in {"indexed", "exhaustive"}:
        raise ValueError("candidate_mode must be indexed or exhaustive")
    return PlanSettings(
        db=parse_path(payload.get("db"), DEFAULT_DB),
        fingerprint_profile=parse_optional_text(payload.get("fingerprint_profile")),
        output=parse_path(payload.get("output"), default_output("cleanup-plan", ".json")),
        min_overlap=parse_float(payload.get("min_overlap"), 0.9, minimum=0.0),
        partial_overlap=parse_float(payload.get("partial_overlap"), 0.45, minimum=0.0),
        near_duplicate_similarity=parse_float(payload.get("near_duplicate_similarity"), 0.9, minimum=0.0),
        hash_distance=parse_int(payload.get("hash_distance"), 10, minimum=0),
        candidate_mode=mode,
        min_anchor_votes=parse_int(payload.get("min_anchor_votes"), 3, minimum=1),
        anchor_stride=parse_int(payload.get("anchor_stride"), 1, minimum=1),
        max_anchor_bucket=parse_int(payload.get("max_anchor_bucket"), 200, minimum=1),
    )


def evidence_settings_from_payload(payload: dict[str, object]) -> EvidenceSettings:
    return EvidenceSettings(
        plan=parse_path(payload.get("plan"), ""),
        db=parse_path(payload.get("db"), DEFAULT_DB),
        fingerprint_profile=parse_optional_text(payload.get("fingerprint_profile")),
        output_dir=parse_path(payload.get("output_dir"), default_output("evidence", "")),
        title=str(payload.get("title") or "Video Overlap Evidence Report"),
        max_samples=parse_int(payload.get("max_samples"), 6, minimum=1),
        hash_distance=parse_int(payload.get("hash_distance"), 10, minimum=0),
        screenshots=parse_bool(payload.get("screenshots"), True),
        screenshot_height=parse_int(payload.get("screenshot_height"), 360, minimum=16),
        screenshot_timeout=parse_int(payload.get("screenshot_timeout"), 60, minimum=1),
        screenshot_workers=parse_int(payload.get("screenshot_workers"), 1, minimum=1),
        include_manual=parse_bool(payload.get("include_manual"), True),
    )


def move_settings_from_payload(payload: dict[str, object]) -> MoveSettings:
    return MoveSettings(
        plan=parse_path(payload.get("plan"), ""),
        quarantine=parse_path(payload.get("quarantine"), ".diskcleanup/quarantine"),
        manifest=parse_path(payload.get("manifest"), ".diskcleanup/move-manifest.jsonl"),
        apply=parse_bool(payload.get("apply"), False),
    )


def default_output(prefix: str, suffix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(".diskcleanup") / f"gui-{prefix}-{stamp}{suffix}"


def scan_videos(settings: ScanSettings, progress: ProgressCallback) -> dict[str, object]:
    args = ScanArgs(settings)
    roots = resolve_roots(settings.paths)
    connection = connect(settings.db)
    scanned = skipped = failed = 0
    extensions = parse_extensions(settings.extensions)
    profile = fingerprint_profile(args)
    needs_sha256 = settings.hash_mode == "sha256" and not settings.skip_sha256
    needs_quick_hash = settings.hash_mode != "none"
    jobs: list[tuple[Path, Path, object]] = []
    active_files: dict[str, dict[str, object]] = {}
    active_lock = Lock()
    ffmpeg_workers = settings.ffmpeg_workers or (
        max(settings.workers, settings.seek_workers) if settings.seek_workers > 1 else 0
    )
    ffmpeg_semaphore = BoundedSemaphore(ffmpeg_workers) if ffmpeg_workers > 0 else None

    progress({"phase": "indexing", "scanned": 0, "failed": 0, "skipped": 0}, "indexing videos")
    try:
        for path in iter_video_files(roots, extensions):
            stat = path.stat()
            existing = get_record(connection, path, profile=profile)
            root = select_root(path, roots)
            if not should_rescan(
                existing,
                stat.st_size,
                stat.st_mtime_ns,
                settings.force,
                profile,
                needs_quick_hash,
                needs_sha256,
            ):
                skipped += 1
                continue
            jobs.append((path, root, existing))

        total = len(jobs)
        progress(
            {"phase": "scanning", "total": total, "skipped": skipped, "profile": profile},
            f"queued {total} videos",
        )

        def publish_active(path: Path, details: dict[str, object] | None = None) -> None:
            active_key = str(path)
            with active_lock:
                active_files[active_key] = {
                    "path": active_key,
                    **(details or {"phase": "starting"}),
                }
                snapshot = list(active_files.values())
            progress(
                {
                    "phase": "scanning",
                    "total": total,
                    "scanned": scanned,
                    "failed": failed,
                    "skipped": skipped,
                    "current": active_key,
                    "active_files": snapshot,
                },
                None,
            )

        def clear_active(path: Path) -> None:
            with active_lock:
                active_files.pop(str(path), None)
                snapshot = list(active_files.values())
            progress({"active_files": snapshot}, None)

        def scan_with_progress(path: Path, root: Path, existing: object):
            publish_active(path)
            try:
                return scan_one_video(
                    path,
                    root,
                    existing=existing,
                    **scan_kwargs,
                    progress_callback=publish_active,
                )
            finally:
                clear_active(path)

        def persist(record) -> None:
            nonlocal scanned, failed
            if record.error:
                failed += 1
            upsert_record(connection, record)
            connection.commit()
            scanned += 1
            progress(
                {
                    "phase": "scanning",
                    "total": total,
                    "scanned": scanned,
                    "failed": failed,
                    "skipped": skipped,
                    "last_completed": str(record.path),
                },
                None,
            )

        scan_kwargs = {
            "interval": settings.interval,
            "max_frames": settings.max_frames,
            "profile": profile,
            "hash_mode": settings.hash_mode,
            "needs_sha256": needs_sha256,
            "probe_timeout": settings.probe_timeout,
            "fingerprint_timeout": settings.fingerprint_timeout,
            "fingerprint_mode": settings.fingerprint_mode,
            "seek_timeout": settings.seek_timeout,
            "seek_workers": settings.seek_workers,
            "ffmpeg_semaphore": ffmpeg_semaphore,
        }

        if settings.workers <= 1:
            for path, root, existing in jobs:
                persist(scan_with_progress(path, root, existing))
        else:
            with ThreadPoolExecutor(max_workers=settings.workers) as executor:
                futures = [
                    executor.submit(scan_with_progress, path, root, existing)
                    for path, root, existing in jobs
                ]
                for future in as_completed(futures):
                    persist(future.result())

        removed = remove_missing_records(connection) if settings.prune_missing else 0
        connection.commit()
    finally:
        connection.close()

    progress({"phase": "completed"}, "scan completed")
    return {
        "db": str(settings.db),
        "profile": profile,
        "scanned": scanned,
        "skipped": skipped,
        "failed": failed,
        "pruned_missing": removed,
    }


def build_plan(settings: PlanSettings, progress: ProgressCallback) -> dict[str, object]:
    progress({"phase": "loading"}, "loading cached fingerprints")
    connection = connect(settings.db)
    try:
        records = list_records(connection, profile=settings.fingerprint_profile)
    finally:
        connection.close()

    progress({"phase": "matching", "records": len(records)}, "building cleanup plan")
    items, matches = build_cleanup_plan(
        records,
        min_overlap=settings.min_overlap,
        partial_overlap=settings.partial_overlap,
        near_duplicate_similarity=settings.near_duplicate_similarity,
        hash_distance=settings.hash_distance,
        candidate_mode=settings.candidate_mode,
        min_anchor_votes=settings.min_anchor_votes,
        anchor_stride=settings.anchor_stride,
        max_anchor_bucket=settings.max_anchor_bucket,
    )
    payload = {
        "version": 1,
        "db": str(settings.db),
        "thresholds": {
            "min_overlap": settings.min_overlap,
            "partial_overlap": settings.partial_overlap,
            "near_duplicate_similarity": settings.near_duplicate_similarity,
            "hash_distance": settings.hash_distance,
            "candidate_mode": settings.candidate_mode,
            "min_anchor_votes": settings.min_anchor_votes,
            "anchor_stride": settings.anchor_stride,
            "max_anchor_bucket": settings.max_anchor_bucket,
        },
        "items": [plan_item_to_dict(item) for item in items],
        "manual_review": [
            {
                "kind": match.kind,
                "left": str(match.left.path),
                "right": str(match.right.path),
                "score": round(match.score, 6),
                "offset_seconds": match.offset_seconds,
            }
            for match in matches
            if match.kind == "partial_overlap"
        ],
    }
    settings.output.parent.mkdir(parents=True, exist_ok=True)
    settings.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    reason_counts = Counter(item.reason for item in items)
    progress({"phase": "completed"}, "plan completed")
    return {
        "db": str(settings.db),
        "profile": settings.fingerprint_profile,
        "plan": str(settings.output),
        "records": len(records),
        "items": len(items),
        "manual_review": len(payload["manual_review"]),
        "reason_counts": dict(reason_counts),
    }


def build_evidence(settings: EvidenceSettings, progress: ProgressCallback) -> dict[str, object]:
    progress({"phase": "evidence"}, "building evidence report")
    summary = build_evidence_report(
        plan_path=settings.plan,
        db_path=settings.db,
        profile=settings.fingerprint_profile,
        output_dir=settings.output_dir,
        title=settings.title,
        max_samples=settings.max_samples,
        hash_distance=settings.hash_distance,
        screenshots=settings.screenshots,
        screenshot_height=settings.screenshot_height,
        screenshot_timeout=settings.screenshot_timeout,
        screenshot_workers=settings.screenshot_workers,
        include_manual=settings.include_manual,
        progress_callback=lambda update: progress(update, None),
    )
    progress({"phase": "completed"}, "evidence report completed")
    return summary


def move_items(settings: MoveSettings, progress: ProgressCallback) -> dict[str, object]:
    progress({"phase": "loading"}, "loading move plan")
    items = load_plan(settings.plan)
    progress({"phase": "moving", "total": len(items)}, "processing move plan")
    results = move_from_plan(
        items,
        quarantine=settings.quarantine,
        dry_run=not settings.apply,
        manifest_path=settings.manifest,
    )
    counts = Counter(str(result.get("status")) for result in results)
    progress({"phase": "completed"}, "move plan processed")
    return {
        "plan": str(settings.plan),
        "quarantine": str(settings.quarantine),
        "manifest": str(settings.manifest),
        "apply": settings.apply,
        "processed": len(results),
        "status_counts": dict(counts),
        "results": results[:500],
    }


def list_cached_profiles(db: Path) -> list[dict[str, object]]:
    connection = connect(db)
    try:
        return [{"profile": profile, "count": count} for profile, count in list_profiles(connection)]
    finally:
        connection.close()


def read_plan_preview(path: Path, limit: int = 500) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    items = data.get("items", []) if isinstance(data, dict) else data
    manual = data.get("manual_review", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = []
    if not isinstance(manual, list):
        manual = []
    reason_counts = Counter(str(item.get("reason")) for item in items if isinstance(item, dict))
    return {
        "path": str(path),
        "items": items[:limit],
        "manual_review": manual[:limit],
        "item_count": len(items),
        "manual_review_count": len(manual),
        "reason_counts": dict(reason_counts),
        "thresholds": data.get("thresholds", {}) if isinstance(data, dict) else {},
    }


def read_evidence_preview(output_dir: Path, limit: int = 500) -> dict[str, object]:
    summary_path = output_dir / "report.json"
    relation_path = output_dir / "relations.csv"
    sample_path = output_dir / "evidence-samples.csv"
    summary: dict[str, object] = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    relations = read_csv_rows(relation_path, limit)
    samples = read_csv_rows(sample_path, limit)
    for sample in samples:
        screenshot = sample.get("screenshot")
        if screenshot:
            sample["screenshot_path"] = str(output_dir / str(screenshot))
    return {
        "output_dir": str(output_dir),
        "summary": summary,
        "relations": relations,
        "samples": samples,
        "report_markdown": str(output_dir / "report.md"),
    }


def read_csv_rows(path: Path, limit: int) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            rows.append(dict(row))
            if len(rows) >= limit:
                break
    return rows


class ScanArgs:
    def __init__(self, settings: ScanSettings) -> None:
        self.profile_name = settings.profile_name
        self.max_frames = settings.max_frames
        self.fingerprint_mode = settings.fingerprint_mode
        self.interval = settings.interval


def as_iterable_paths(values: Iterable[str]) -> list[Path]:
    return [Path(value).expanduser() for value in values]
