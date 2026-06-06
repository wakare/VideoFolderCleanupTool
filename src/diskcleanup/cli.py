from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
from pathlib import Path

from . import __version__
from .db import connect, get_record, list_profiles, list_records, remove_missing_records, upsert_record
from .evidence import build_evidence_report
from .fingerprint import FingerprintError, extract_video_fingerprint, extract_video_fingerprint_seek
from .media import ProbeError, VIDEO_EXTENSIONS, iter_video_files, probe_video, quick_hash_file, sha256_file
from .models import VideoRecord
from .mover import MoveError, load_plan, move_from_plan
from .planner import build_cleanup_plan, plan_item_to_dict


DEFAULT_DB = Path(".diskcleanup/cache.sqlite")
DEFAULT_MANIFEST = Path(".diskcleanup/move-manifest.jsonl")


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def bounded_ratio(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def parse_extensions(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    extensions: set[str] = set()
    for value in values:
        for part in value.split(","):
            part = part.strip().lower()
            if not part:
                continue
            extensions.add(part if part.startswith(".") else f".{part}")
    return extensions


def resolve_roots(values: list[str]) -> list[Path]:
    return [Path(value).expanduser().resolve() for value in values]


def fingerprint_profile(args: argparse.Namespace) -> str:
    if args.profile_name:
        return args.profile_name
    max_frames = args.max_frames or 0
    return (
        f"dhash64-v1:{args.fingerprint_mode}:"
        f"interval={args.interval}:max_frames={max_frames}"
    )


def should_rescan(
    existing: VideoRecord | None,
    size: int,
    mtime_ns: int,
    force: bool,
    profile: str,
    needs_quick_hash: bool,
    needs_sha256: bool,
) -> bool:
    if force or existing is None:
        return True
    profile_changed = existing.fingerprint_profile != profile
    quick_hash_missing = needs_quick_hash and not existing.quick_hash
    sha256_missing = needs_sha256 and not existing.sha256
    return (
        existing.size != size
        or existing.mtime_ns != mtime_ns
        or profile_changed
        or quick_hash_missing
        or sha256_missing
        or not existing.has_fingerprint
    )


def select_root(path: Path, roots: list[Path]) -> Path:
    return next(
        (candidate for candidate in roots if path == candidate or candidate in path.parents),
        path.parent,
    )


def scan_one_video(
    path: Path,
    root: Path,
    *,
    existing: VideoRecord | None,
    interval: float,
    max_frames: int,
    profile: str,
    hash_mode: str,
    needs_sha256: bool,
    probe_timeout: int | None,
    fingerprint_timeout: int | None,
    fingerprint_mode: str,
    seek_timeout: int | None,
) -> VideoRecord:
    stat = path.stat()
    can_reuse = existing is not None and existing.size == stat.st_size and existing.mtime_ns == stat.st_mtime_ns
    metadata: dict[str, object] = {}
    fingerprint: tuple[int, ...] = ()
    quick_hash: str | None = existing.quick_hash if can_reuse and existing else None
    sha256: str | None = existing.sha256 if can_reuse and existing else None
    error: str | None = None

    if can_reuse and existing:
        metadata = {
            "duration": existing.duration,
            "width": existing.width,
            "height": existing.height,
            "codec": existing.codec,
            "bit_rate": existing.bit_rate,
            "fps": existing.fps,
            "frames": existing.frames,
        }

    try:
        if not metadata or metadata.get("duration") is None:
            metadata = probe_video(path, timeout_seconds=probe_timeout)
        if hash_mode != "none":
            quick_hash = quick_hash or quick_hash_file(path)
        if needs_sha256:
            sha256 = sha256 or sha256_file(path)
        if fingerprint_mode == "seek":
            duration = metadata.get("duration")
            if not isinstance(duration, (int, float)):
                raise FingerprintError("ffprobe did not return a duration for seek extraction")
            fingerprint = extract_video_fingerprint_seek(
                path,
                duration_seconds=float(duration),
                interval_seconds=interval,
                max_frames=max_frames or None,
                timeout_per_frame_seconds=seek_timeout,
            )
        elif fingerprint_mode == "pyav-seek":
            duration = metadata.get("duration")
            if not isinstance(duration, (int, float)):
                raise FingerprintError("ffprobe did not return a duration for PyAV seek extraction")
            from .fingerprint import extract_video_fingerprint_pyav_seek

            fingerprint = extract_video_fingerprint_pyav_seek(
                path,
                duration_seconds=float(duration),
                interval_seconds=interval,
                max_frames=max_frames or None,
            )
        else:
            fingerprint = extract_video_fingerprint(
                path,
                interval_seconds=interval,
                max_frames=max_frames or None,
                timeout_seconds=fingerprint_timeout,
            )
    except (ProbeError, FingerprintError, OSError) as exc:
        error = str(exc)
    except Exception as exc:  # Keep long batch scans moving after one bad file.
        error = f"{type(exc).__name__}: {exc}"

    return VideoRecord(
        path=path,
        root=root,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=sha256,
        duration=metadata.get("duration") if metadata else None,
        width=metadata.get("width") if metadata else None,
        height=metadata.get("height") if metadata else None,
        codec=metadata.get("codec") if metadata else None,
        bit_rate=metadata.get("bit_rate") if metadata else None,
        fps=metadata.get("fps") if metadata else None,
        frames=metadata.get("frames") if metadata else None,
        fingerprint=fingerprint,
        fingerprint_interval=interval,
        error=error,
        quick_hash=quick_hash,
        fingerprint_profile=profile,
    )


def scan_command(args: argparse.Namespace) -> int:
    roots = resolve_roots(args.paths)
    connection = connect(args.db)
    scanned = skipped = failed = 0
    extensions = parse_extensions(args.extensions)
    profile = fingerprint_profile(args)
    needs_sha256 = args.hash_mode == "sha256" and not args.skip_sha256
    needs_quick_hash = args.hash_mode != "none"
    jobs: list[tuple[Path, Path, VideoRecord | None]] = []

    for path in iter_video_files(roots, extensions):
        stat = path.stat()
        existing = get_record(connection, path, profile=profile)
        root = select_root(path, roots)
        if not should_rescan(
            existing,
            stat.st_size,
            stat.st_mtime_ns,
            args.force,
            profile,
            needs_quick_hash,
            needs_sha256,
        ):
            skipped += 1
            continue
        jobs.append((path, root, existing))

    def persist(record: VideoRecord) -> None:
        nonlocal scanned, failed
        if record.error:
            failed += 1
        upsert_record(connection, record)
        connection.commit()
        scanned += 1

        if args.verbose:
            status = "failed" if record.error else f"{len(record.fingerprint)} samples"
            print(f"{status}: {record.path}")

    scan_kwargs = {
        "interval": args.interval,
        "max_frames": args.max_frames,
        "profile": profile,
        "hash_mode": args.hash_mode,
        "needs_sha256": needs_sha256,
        "probe_timeout": args.probe_timeout,
        "fingerprint_timeout": args.fingerprint_timeout,
        "fingerprint_mode": args.fingerprint_mode,
        "seek_timeout": args.seek_timeout,
    }

    if args.workers <= 1:
        for path, root, existing in jobs:
            persist(scan_one_video(path, root, existing=existing, **scan_kwargs))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(scan_one_video, path, root, existing=existing, **scan_kwargs)
                for path, root, existing in jobs
            ]
            for future in as_completed(futures):
                persist(future.result())

    removed = remove_missing_records(connection) if args.prune_missing else 0
    connection.commit()
    print(f"Scanned: {scanned}, skipped: {skipped}, failed: {failed}, pruned missing: {removed}")
    return 1 if failed and args.fail_on_error else 0


def load_records(db_path: Path, profile: str | None = None) -> list[VideoRecord]:
    connection = connect(db_path)
    return list_records(connection, profile=profile)


def profiles_command(args: argparse.Namespace) -> int:
    profiles = list_profiles(connect(args.db))
    if not profiles:
        print("No fingerprint profiles cached.")
        return 0
    for profile, count in profiles:
        print(f"{count:5d}  {profile}")
    return 0


def print_match(match) -> None:
    left = match.left.path
    right = match.right.path
    offset = "" if match.offset_seconds is None else f", offset {match.offset_seconds:.1f}s"
    print(f"- {match.kind}: {match.score:.3f}{offset}")
    print(f"  keep/check: {right}")
    print(f"  candidate:  {left}")


def report_command(args: argparse.Namespace) -> int:
    records = load_records(args.db, args.fingerprint_profile)
    items, matches = build_cleanup_plan(
        records,
        min_overlap=args.min_overlap,
        partial_overlap=args.partial_overlap,
        near_duplicate_similarity=args.near_duplicate_similarity,
        hash_distance=args.hash_distance,
        candidate_mode=args.candidate_mode,
        min_anchor_votes=args.min_anchor_votes,
        anchor_stride=args.anchor_stride,
        max_anchor_bucket=args.max_anchor_bucket,
    )
    failures = [record for record in records if record.error]
    partials = [match for match in matches if match.kind == "partial_overlap"]

    print(f"Indexed videos: {len(records)}")
    print(f"Readable fingerprints: {sum(1 for record in records if record.has_fingerprint)}")
    print(f"Scan failures: {len(failures)}")
    print(f"Suggested moves: {len(items)}")

    if items:
        print("\nCleanup candidates:")
        for item in items:
            print(f"- {item.reason}: {item.confidence:.3f}")
            print(f"  keep: {item.keeper}")
            print(f"  move: {item.victim}")

    if partials:
        print("\nPartial overlaps for manual review:")
        for match in partials[: args.max_partial]:
            print_match(match)

    if failures and args.show_errors:
        print("\nScan failures:")
        for record in failures:
            print(f"- {record.path}: {record.error}")

    return 0


def plan_command(args: argparse.Namespace) -> int:
    records = load_records(args.db, args.fingerprint_profile)
    items, matches = build_cleanup_plan(
        records,
        min_overlap=args.min_overlap,
        partial_overlap=args.partial_overlap,
        near_duplicate_similarity=args.near_duplicate_similarity,
        hash_distance=args.hash_distance,
        candidate_mode=args.candidate_mode,
        min_anchor_votes=args.min_anchor_votes,
        anchor_stride=args.anchor_stride,
        max_anchor_bucket=args.max_anchor_bucket,
    )
    payload = {
        "version": 1,
        "db": str(args.db),
        "thresholds": {
            "min_overlap": args.min_overlap,
            "partial_overlap": args.partial_overlap,
            "near_duplicate_similarity": args.near_duplicate_similarity,
            "hash_distance": args.hash_distance,
            "candidate_mode": args.candidate_mode,
            "min_anchor_votes": args.min_anchor_votes,
            "anchor_stride": args.anchor_stride,
            "max_anchor_bucket": args.max_anchor_bucket,
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
    content = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(content + "\n", encoding="utf-8")
        print(f"Wrote {len(items)} move candidates to {args.output}")
    else:
        print(content)
    return 0


def move_command(args: argparse.Namespace) -> int:
    if args.apply and args.dry_run:
        raise MoveError("choose either --dry-run or --apply")
    dry_run = not args.apply
    items = load_plan(args.plan)
    results = move_from_plan(
        items,
        quarantine=args.quarantine,
        dry_run=dry_run,
        manifest_path=args.manifest,
    )
    for result in results:
        print(f"{result['status']}: {result['source']} -> {result['destination']}")
    print(f"Processed {len(results)} move candidates")
    return 0


def evidence_report_command(args: argparse.Namespace) -> int:
    summary = build_evidence_report(
        plan_path=args.plan,
        db_path=args.db,
        profile=args.fingerprint_profile,
        output_dir=args.output_dir,
        title=args.title,
        max_samples=args.max_samples,
        hash_distance=args.hash_distance,
        screenshots=args.screenshots,
        screenshot_height=args.screenshot_height,
        screenshot_timeout=args.screenshot_timeout,
        include_manual=args.include_manual,
    )
    outputs = summary["outputs"]
    if not isinstance(outputs, dict):
        raise ValueError("invalid evidence report output summary")

    print(f"Relations: {summary['relations']}")
    print(f"Evidence samples: {summary['samples']}")
    print(f"Screenshot comparisons: {summary['screenshot_ok']}")
    print(f"Markdown report: {outputs['markdown']}")
    print(f"Relation CSV: {outputs['relations_csv']}")
    print(f"Evidence CSV: {outputs['evidence_csv']}")
    if args.screenshots:
        print(f"Screenshots: {outputs['screenshots_dir']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diskcleanup")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="scan video files and cache metadata/fingerprints")
    scan.add_argument("paths", nargs="+", help="files or directories to scan")
    scan.add_argument("--db", type=Path, default=DEFAULT_DB)
    scan.add_argument("--interval", type=positive_float, default=2.0, help="seconds between sampled frames")
    scan.add_argument("--max-frames", type=int, default=0, help="limit sampled frames per file; 0 means unlimited")
    scan.add_argument("--fingerprint-mode", choices=["interval", "seek", "pyav-seek"], default="interval")
    scan.add_argument("--profile-name", help="override the generated fingerprint profile name")
    scan.add_argument("--extensions", nargs="*", help="video extensions, for example .mp4 .mkv or mp4,mkv")
    scan.add_argument("--force", action="store_true", help="rescan unchanged files")
    scan.add_argument("--hash-mode", choices=["quick", "sha256", "none"], default="quick")
    scan.add_argument("--skip-sha256", action="store_true", help="deprecated alias to suppress full SHA256")
    scan.add_argument("--workers", type=positive_int, default=1, help="number of videos to scan concurrently")
    scan.add_argument("--probe-timeout", type=int, default=60)
    scan.add_argument("--fingerprint-timeout", type=int, default=None)
    scan.add_argument("--seek-timeout", type=int, default=15, help="timeout per sampled frame in seek mode")
    scan.add_argument("--prune-missing", action="store_true", help="remove missing files from the cache")
    scan.add_argument("--fail-on-error", action="store_true")
    scan.add_argument("--verbose", "-v", action="store_true")
    scan.set_defaults(func=scan_command)

    profiles = subparsers.add_parser("profiles", help="list cached fingerprint profiles")
    profiles.add_argument("--db", type=Path, default=DEFAULT_DB)
    profiles.set_defaults(func=profiles_command)

    for name, help_text in [
        ("report", "print a human-readable duplicate/overlap report"),
        ("plan", "write a JSON cleanup plan"),
    ]:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--db", type=Path, default=DEFAULT_DB)
        command.add_argument("--fingerprint-profile", help="use a specific cached fingerprint profile")
        command.add_argument("--min-overlap", type=bounded_ratio, default=0.9)
        command.add_argument("--partial-overlap", type=bounded_ratio, default=0.45)
        command.add_argument("--near-duplicate-similarity", type=bounded_ratio, default=0.9)
        command.add_argument("--hash-distance", type=int, default=10)
        command.add_argument("--candidate-mode", choices=["indexed", "exhaustive"], default="indexed")
        command.add_argument("--min-anchor-votes", type=int, default=3)
        command.add_argument("--anchor-stride", type=int, default=1)
        command.add_argument("--max-anchor-bucket", type=int, default=200)
        if name == "report":
            command.add_argument("--show-errors", action="store_true")
            command.add_argument("--max-partial", type=int, default=20)
            command.set_defaults(func=report_command)
        else:
            command.add_argument("--output", type=Path)
            command.set_defaults(func=plan_command)

    move = subparsers.add_parser("move", help="move plan victims into quarantine")
    move.add_argument("--plan", type=Path, required=True)
    move.add_argument("--quarantine", type=Path, default=Path(".diskcleanup/quarantine"))
    move.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    move.add_argument("--dry-run", action="store_true", help="preview moves; this is the default")
    move.add_argument("--apply", action="store_true", help="actually move files")
    move.set_defaults(func=move_command)

    evidence = subparsers.add_parser(
        "evidence-report",
        help="write a detailed overlap report with evidence samples and screenshot comparisons",
    )
    evidence.add_argument("--plan", type=Path, required=True)
    evidence.add_argument("--db", type=Path, default=DEFAULT_DB)
    evidence.add_argument("--fingerprint-profile", help="use a specific cached fingerprint profile")
    evidence.add_argument("--output-dir", type=Path, default=Path(".diskcleanup/evidence-report"))
    evidence.add_argument("--title", default="Video Overlap Evidence Report")
    evidence.add_argument("--max-samples", type=positive_int, default=6)
    evidence.add_argument("--hash-distance", type=int, default=10)
    evidence.add_argument(
        "--screenshots",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="generate side-by-side frame screenshots; enabled by default",
    )
    evidence.add_argument("--screenshot-height", type=positive_int, default=360)
    evidence.add_argument("--screenshot-timeout", type=positive_int, default=60)
    manual_group = evidence.add_mutually_exclusive_group()
    manual_group.add_argument(
        "--include-manual",
        dest="include_manual",
        action="store_true",
        default=True,
        help="include partial overlaps that are marked for manual review; enabled by default",
    )
    manual_group.add_argument(
        "--exclude-manual",
        dest="include_manual",
        action="store_false",
        help="exclude partial overlaps that are marked for manual review",
    )
    evidence.set_defaults(func=evidence_report_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (KeyboardInterrupt, MoveError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
