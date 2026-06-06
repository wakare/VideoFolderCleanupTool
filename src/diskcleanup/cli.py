from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .db import connect, get_record, list_records, remove_missing_records, upsert_record
from .fingerprint import FingerprintError, extract_video_fingerprint
from .media import ProbeError, VIDEO_EXTENSIONS, iter_video_files, probe_video, sha256_file
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


def should_rescan(
    existing: VideoRecord | None,
    size: int,
    mtime_ns: int,
    force: bool,
    interval: float,
) -> bool:
    if force or existing is None:
        return True
    interval_changed = existing.fingerprint_interval != interval
    return (
        existing.size != size
        or existing.mtime_ns != mtime_ns
        or interval_changed
        or not existing.has_fingerprint
    )


def scan_command(args: argparse.Namespace) -> int:
    roots = resolve_roots(args.paths)
    connection = connect(args.db)
    scanned = skipped = failed = 0
    extensions = parse_extensions(args.extensions)

    for path in iter_video_files(roots, extensions):
        stat = path.stat()
        existing = get_record(connection, path)
        root = next((candidate for candidate in roots if path == candidate or candidate in path.parents), path.parent)
        if not should_rescan(existing, stat.st_size, stat.st_mtime_ns, args.force, args.interval):
            skipped += 1
            continue

        metadata: dict[str, object] = {}
        fingerprint: tuple[int, ...] = ()
        sha256: str | None = None
        error: str | None = None

        try:
            metadata = probe_video(path, timeout_seconds=args.probe_timeout)
            if not args.skip_sha256:
                sha256 = sha256_file(path)
            fingerprint = extract_video_fingerprint(
                path,
                interval_seconds=args.interval,
                max_frames=args.max_frames or None,
                timeout_seconds=args.fingerprint_timeout,
            )
        except (ProbeError, FingerprintError, OSError) as exc:
            error = str(exc)
            failed += 1

        record = VideoRecord(
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
            fingerprint_interval=args.interval,
            error=error,
        )
        upsert_record(connection, record)
        connection.commit()
        scanned += 1

        if args.verbose:
            status = "failed" if error else f"{len(fingerprint)} samples"
            print(f"{status}: {path}")

    removed = remove_missing_records(connection) if args.prune_missing else 0
    connection.commit()
    print(f"Scanned: {scanned}, skipped: {skipped}, failed: {failed}, pruned missing: {removed}")
    return 1 if failed and args.fail_on_error else 0


def load_records(db_path: Path) -> list[VideoRecord]:
    connection = connect(db_path)
    return list_records(connection)


def print_match(match) -> None:
    left = match.left.path
    right = match.right.path
    offset = "" if match.offset_seconds is None else f", offset {match.offset_seconds:.1f}s"
    print(f"- {match.kind}: {match.score:.3f}{offset}")
    print(f"  keep/check: {right}")
    print(f"  candidate:  {left}")


def report_command(args: argparse.Namespace) -> int:
    records = load_records(args.db)
    items, matches = build_cleanup_plan(
        records,
        min_overlap=args.min_overlap,
        partial_overlap=args.partial_overlap,
        near_duplicate_similarity=args.near_duplicate_similarity,
        hash_distance=args.hash_distance,
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
    records = load_records(args.db)
    items, matches = build_cleanup_plan(
        records,
        min_overlap=args.min_overlap,
        partial_overlap=args.partial_overlap,
        near_duplicate_similarity=args.near_duplicate_similarity,
        hash_distance=args.hash_distance,
    )
    payload = {
        "version": 1,
        "db": str(args.db),
        "thresholds": {
            "min_overlap": args.min_overlap,
            "partial_overlap": args.partial_overlap,
            "near_duplicate_similarity": args.near_duplicate_similarity,
            "hash_distance": args.hash_distance,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diskcleanup")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="scan video files and cache metadata/fingerprints")
    scan.add_argument("paths", nargs="+", help="files or directories to scan")
    scan.add_argument("--db", type=Path, default=DEFAULT_DB)
    scan.add_argument("--interval", type=positive_float, default=2.0, help="seconds between sampled frames")
    scan.add_argument("--max-frames", type=int, default=0, help="limit sampled frames per file; 0 means unlimited")
    scan.add_argument("--extensions", nargs="*", help="video extensions, for example .mp4 .mkv or mp4,mkv")
    scan.add_argument("--force", action="store_true", help="rescan unchanged files")
    scan.add_argument("--skip-sha256", action="store_true", help="skip full-file hashing")
    scan.add_argument("--probe-timeout", type=int, default=60)
    scan.add_argument("--fingerprint-timeout", type=int, default=None)
    scan.add_argument("--prune-missing", action="store_true", help="remove missing files from the cache")
    scan.add_argument("--fail-on-error", action="store_true")
    scan.add_argument("--verbose", "-v", action="store_true")
    scan.set_defaults(func=scan_command)

    for name, help_text in [
        ("report", "print a human-readable duplicate/overlap report"),
        ("plan", "write a JSON cleanup plan"),
    ]:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--db", type=Path, default=DEFAULT_DB)
        command.add_argument("--min-overlap", type=bounded_ratio, default=0.9)
        command.add_argument("--partial-overlap", type=bounded_ratio, default=0.45)
        command.add_argument("--near-duplicate-similarity", type=bounded_ratio, default=0.9)
        command.add_argument("--hash-distance", type=int, default=10)
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (KeyboardInterrupt, MoveError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
