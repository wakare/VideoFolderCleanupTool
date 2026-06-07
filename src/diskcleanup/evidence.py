from __future__ import annotations

import csv
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .db import connect, list_records
from .fingerprint import hamming64
from .models import VideoRecord


EvidenceProgressCallback = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class EvidenceRelation:
    relation_id: int
    reason: str
    score: float | None
    candidate: Path
    keeper: Path
    offset_seconds: float | None
    note: str
    automatic: bool


@dataclass(frozen=True)
class EvidenceSample:
    relation_id: int
    reason: str
    candidate: Path
    keeper: Path
    candidate_seconds: float
    keeper_seconds: float
    hamming_distance: int | None
    screenshot: Path | None
    screenshot_status: str


@dataclass(frozen=True)
class ScreenshotJob:
    sample_position: int
    relation_index: int
    relations_total: int
    sample_index: int
    sample_total: int
    reason: str
    candidate: Path
    keeper: Path
    candidate_seconds: float
    keeper_seconds: float
    output: Path


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return ""
    value = float(seconds)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    secs = value % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"
    return f"{minutes:02d}:{secs:05.2f}"


def safe_slug(value: str, *, max_length: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    slug = slug.strip("._-") or "item"
    return slug[:max_length]


def load_plan(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("evidence-report expects a plan JSON object")
    return data


def records_by_path(db_path: Path, profile: str | None) -> dict[str, VideoRecord]:
    connection = connect(db_path)
    try:
        return {str(record.path): record for record in list_records(connection, profile=profile)}
    finally:
        connection.close()


def relations_from_plan(plan: dict[str, object], *, include_manual: bool = True) -> list[EvidenceRelation]:
    relations: list[EvidenceRelation] = []
    relation_id = 1

    for item in plan.get("items", []):
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        if reason not in {"exact_duplicate", "quick_hash_duplicate", "near_duplicate", "contained_in"}:
            continue
        victim = item.get("victim")
        keeper = item.get("keeper")
        if not isinstance(victim, str) or not isinstance(keeper, str):
            continue
        relations.append(
            EvidenceRelation(
                relation_id=relation_id,
                reason=str(reason),
                score=_float_or_none(item.get("confidence")),
                candidate=Path(victim),
                keeper=Path(keeper),
                offset_seconds=_float_or_none(item.get("offset_seconds")),
                note=evidence_note_for_reason(str(reason)),
                automatic=True,
            )
        )
        relation_id += 1

    if include_manual:
        for item in plan.get("manual_review", []):
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind != "partial_overlap":
                continue
            left = item.get("left")
            right = item.get("right")
            if not isinstance(left, str) or not isinstance(right, str):
                continue
            relations.append(
                EvidenceRelation(
                    relation_id=relation_id,
                    reason="partial_overlap",
                    score=_float_or_none(item.get("score")),
                    candidate=Path(left),
                    keeper=Path(right),
                    offset_seconds=_float_or_none(item.get("offset_seconds")),
                    note="manual_review_only",
                    automatic=False,
                )
            )
            relation_id += 1

    return relations


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evidence_note_for_reason(reason: str) -> str:
    notes = {
        "exact_duplicate": "same_full_file_sha256",
        "quick_hash_duplicate": "same_size_and_edge_chunk_hash",
        "near_duplicate": "visual_hash_similarity",
        "contained_in": "visual_hash_containment",
    }
    return notes.get(reason, "automatic_cleanup_candidate")


def pick_evidence_pairs(
    candidate: VideoRecord,
    keeper: VideoRecord,
    *,
    offset_seconds: float | None,
    max_distance: int,
    max_samples: int,
) -> list[tuple[int, int, int]]:
    candidate_interval = candidate.fingerprint_interval or keeper.fingerprint_interval or 1.0
    keeper_interval = keeper.fingerprint_interval or candidate_interval
    offset_frames = round((offset_seconds or 0.0) / keeper_interval)

    matches: list[tuple[int, int, int]] = []
    for candidate_index, frame_hash in enumerate(candidate.fingerprint):
        keeper_index = offset_frames + round((candidate_index * candidate_interval) / keeper_interval)
        if keeper_index < 0 or keeper_index >= len(keeper.fingerprint):
            continue
        distance = hamming64(frame_hash, keeper.fingerprint[keeper_index])
        if distance <= max_distance:
            matches.append((candidate_index, keeper_index, distance))

    if len(matches) <= max_samples:
        return matches
    if max_samples == 1:
        return [matches[len(matches) // 2]]

    selected: list[tuple[int, int, int]] = []
    used: set[int] = set()
    targets = [round(index * (len(matches) - 1) / (max_samples - 1)) for index in range(max_samples)]
    for target in targets:
        chosen = None
        for delta in range(len(matches)):
            for position in (target - delta, target + delta):
                if 0 <= position < len(matches) and position not in used:
                    chosen = position
                    break
            if chosen is not None:
                break
        if chosen is None:
            continue
        used.add(chosen)
        selected.append(matches[chosen])
    return selected


def extract_comparison_screenshot(
    *,
    candidate: Path,
    keeper: Path,
    candidate_seconds: float,
    keeper_seconds: float,
    output: Path,
    height: int,
    timeout_seconds: int,
) -> str:
    if not candidate.exists():
        return "candidate_missing"
    if not keeper.exists():
        return "keeper_missing"

    output.parent.mkdir(parents=True, exist_ok=True)
    filter_complex = (
        f"[0:v]scale=-2:{height}:flags=lanczos[left];"
        f"[1:v]scale=-2:{height}:flags=lanczos[right];"
        "[left][right]hstack=inputs=2[out]"
    )
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{candidate_seconds:.3f}",
        "-i",
        str(candidate),
        "-ss",
        f"{keeper_seconds:.3f}",
        "-i",
        str(keeper),
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-frames:v",
        "1",
        str(output),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return "ffmpeg_timeout"
    except OSError as exc:
        return f"ffmpeg_error:{exc}"

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        return f"ffmpeg_failed:{stderr[:200]}"
    return "ok" if output.exists() else "missing_output"


def build_evidence_report(
    *,
    plan_path: Path,
    db_path: Path,
    profile: str | None,
    output_dir: Path,
    title: str,
    max_samples: int = 6,
    hash_distance: int = 10,
    screenshots: bool = True,
    screenshot_height: int = 360,
    screenshot_timeout: int = 60,
    screenshot_workers: int = 1,
    include_manual: bool = True,
    progress_callback: EvidenceProgressCallback | None = None,
) -> dict[str, object]:
    if screenshot_workers <= 0:
        raise ValueError("screenshot_workers must be positive")

    plan = load_plan(plan_path)
    records = records_by_path(db_path, profile)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir = output_dir / "screenshots"

    relations = relations_from_plan(plan, include_manual=include_manual)
    samples: list[EvidenceSample] = []
    screenshot_jobs: list[ScreenshotJob] = []
    relation_rows: list[dict[str, object]] = []
    screenshot_ok = 0

    def publish(update: dict[str, object]) -> None:
        if progress_callback:
            progress_callback(update)

    publish(
        {
            "phase": "evidence",
            "total": len(relations),
            "processed": 0,
            "relations_total": len(relations),
            "relation_index": 0,
            "samples": 0,
            "screenshot_ok": 0,
        }
    )

    for relation_index, relation in enumerate(relations, start=1):
        publish(
            {
                "phase": "evidence",
                "total": len(relations),
                "processed": relation_index - 1,
                "relations_total": len(relations),
                "relation_index": relation_index,
                "reason": relation.reason,
                "candidate": str(relation.candidate),
                "keeper": str(relation.keeper),
                "current": f"{relation.candidate} -> {relation.keeper}",
                "samples": len(samples),
                "screenshot_ok": screenshot_ok,
            }
        )
        candidate_record = records.get(str(relation.candidate))
        keeper_record = records.get(str(relation.keeper))
        relation_rows.append(relation_to_row(relation, candidate_record, keeper_record))
        if not candidate_record or not keeper_record:
            samples.append(
                EvidenceSample(
                    relation.relation_id,
                    relation.reason,
                    relation.candidate,
                    relation.keeper,
                    0.0,
                    relation.offset_seconds or 0.0,
                    None,
                    None,
                    "missing_fingerprint_record",
                )
            )
            publish(
                {
                    "phase": "evidence",
                    "total": len(relations),
                    "processed": relation_index,
                    "relation_index": relation_index,
                    "relations_total": len(relations),
                    "samples": len(samples),
                    "screenshot_ok": screenshot_ok,
                    "current": f"{relation.candidate} -> {relation.keeper}",
                }
            )
            continue

        pairs = pick_evidence_pairs(
            candidate_record,
            keeper_record,
            offset_seconds=relation.offset_seconds,
            max_distance=hash_distance,
            max_samples=max_samples,
        )
        if not pairs:
            samples.append(
                EvidenceSample(
                    relation.relation_id,
                    relation.reason,
                    relation.candidate,
                    relation.keeper,
                    0.0,
                    relation.offset_seconds or 0.0,
                    None,
                    None,
                    "no_matching_samples_under_threshold",
                )
            )
            publish(
                {
                    "phase": "evidence",
                    "total": len(relations),
                    "processed": relation_index,
                    "relation_index": relation_index,
                    "relations_total": len(relations),
                    "samples": len(samples),
                    "screenshot_ok": screenshot_ok,
                    "current": f"{relation.candidate} -> {relation.keeper}",
                }
            )
            continue

        candidate_interval = candidate_record.fingerprint_interval or keeper_record.fingerprint_interval or 1.0
        keeper_interval = keeper_record.fingerprint_interval or candidate_interval
        for sample_index, (candidate_index, keeper_index, distance) in enumerate(pairs, start=1):
            candidate_seconds = candidate_index * candidate_interval
            keeper_seconds = keeper_index * keeper_interval
            screenshot_path = None
            status = "screenshots_disabled"
            if screenshots:
                screenshot_name = (
                    f"relation-{relation.relation_id:03d}-sample-{sample_index:02d}-"
                    f"{safe_slug(relation.reason)}.jpg"
                )
                screenshot_path = screenshot_dir / screenshot_name
                status = "screenshot_pending"
                screenshot_jobs.append(
                    ScreenshotJob(
                        sample_position=len(samples),
                        relation_index=relation_index,
                        relations_total=len(relations),
                        sample_index=sample_index,
                        sample_total=len(pairs),
                        reason=relation.reason,
                        candidate=relation.candidate,
                        keeper=relation.keeper,
                        candidate_seconds=candidate_seconds,
                        keeper_seconds=keeper_seconds,
                        output=screenshot_path,
                    )
                )
            samples.append(
                EvidenceSample(
                    relation.relation_id,
                    relation.reason,
                    relation.candidate,
                    relation.keeper,
                    candidate_seconds,
                    keeper_seconds,
                    distance,
                    screenshot_path,
                    status,
                )
            )
            publish(
                {
                    "phase": "evidence",
                    "total": len(relations),
                    "processed": relation_index - 1,
                    "relation_index": relation_index,
                    "relations_total": len(relations),
                    "sample_index": sample_index,
                    "sample_total": len(pairs),
                    "samples": len(samples),
                    "screenshot_ok": screenshot_ok,
                    "reason": relation.reason,
                    "candidate": str(relation.candidate),
                    "keeper": str(relation.keeper),
                    "current": f"{relation.candidate} -> {relation.keeper}",
                }
            )

        publish(
            {
                "phase": "evidence",
                "total": len(relations),
                "processed": relation_index,
                "relation_index": relation_index,
                "relations_total": len(relations),
                "samples": len(samples),
                "screenshot_ok": screenshot_ok,
                "reason": relation.reason,
                "candidate": str(relation.candidate),
                "keeper": str(relation.keeper),
                "current": f"{relation.candidate} -> {relation.keeper}",
            }
        )

    if screenshot_jobs:
        publish(
            {
                "phase": "screenshots",
                "total": len(screenshot_jobs),
                "processed": 0,
                "relations_total": len(relations),
                "samples": len(samples),
                "screenshot_ok": 0,
            }
        )

        def run_screenshot(job: ScreenshotJob) -> tuple[ScreenshotJob, str]:
            status = extract_comparison_screenshot(
                candidate=job.candidate,
                keeper=job.keeper,
                candidate_seconds=job.candidate_seconds,
                keeper_seconds=job.keeper_seconds,
                output=job.output,
                height=screenshot_height,
                timeout_seconds=screenshot_timeout,
            )
            return job, status

        screenshot_done = 0
        if screenshot_workers == 1:
            for job in screenshot_jobs:
                    completed_job, status = run_screenshot(job)
                    screenshot_done, screenshot_ok = update_screenshot_sample(
                        samples,
                        completed_job,
                        status,
                        screenshot_done,
                        screenshot_ok,
                        len(screenshot_jobs),
                        publish,
                    )
        else:
            with ThreadPoolExecutor(max_workers=screenshot_workers) as executor:
                futures = [executor.submit(run_screenshot, job) for job in screenshot_jobs]
                for future in as_completed(futures):
                    completed_job, status = future.result()
                    screenshot_done, screenshot_ok = update_screenshot_sample(
                        samples,
                        completed_job,
                        status,
                        screenshot_done,
                        screenshot_ok,
                        len(screenshot_jobs),
                        publish,
                    )

    relation_csv = output_dir / "relations.csv"
    sample_csv = output_dir / "evidence-samples.csv"
    markdown_path = output_dir / "report.md"
    json_path = output_dir / "report.json"

    write_csv(relation_csv, relation_rows)
    write_csv(sample_csv, [sample_to_row(sample, output_dir) for sample in samples])
    write_markdown(markdown_path, title, relations, relation_rows, samples, output_dir)

    summary = {
        "title": title,
        "plan": str(plan_path),
        "db": str(db_path),
        "fingerprint_profile": profile,
        "relations": len(relations),
        "samples": len(samples),
        "screenshots": screenshots,
        "screenshot_ok": screenshot_ok,
        "outputs": {
            "markdown": str(markdown_path),
            "relations_csv": str(relation_csv),
            "evidence_csv": str(sample_csv),
            "screenshots_dir": str(screenshot_dir),
            "json": str(json_path),
        },
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    publish(
        {
            "phase": "completed",
            "total": len(relations),
            "processed": len(relations),
            "relations_total": len(relations),
            "relation_index": len(relations),
            "samples": len(samples),
            "screenshot_ok": screenshot_ok,
            "current": str(markdown_path),
        }
    )
    return summary


def update_screenshot_sample(
    samples: list[EvidenceSample],
    job: ScreenshotJob,
    status: str,
    screenshot_done: int,
    screenshot_ok: int,
    screenshot_total: int,
    publish: EvidenceProgressCallback,
) -> tuple[int, int]:
    samples[job.sample_position] = replace(samples[job.sample_position], screenshot_status=status)
    screenshot_done += 1
    if status == "ok":
        screenshot_ok += 1
    publish(
        {
            "phase": "screenshots",
            "total": screenshot_total,
            "processed": screenshot_done,
            "relation_index": job.relation_index,
            "relations_total": job.relations_total,
            "sample_index": job.sample_index,
            "sample_total": job.sample_total,
            "samples": len(samples),
            "screenshot_ok": screenshot_ok,
            "reason": job.reason,
            "candidate": str(job.candidate),
            "keeper": str(job.keeper),
            "current": str(job.output),
        }
    )
    return screenshot_done, screenshot_ok


def relation_to_row(
    relation: EvidenceRelation,
    candidate_record: VideoRecord | None,
    keeper_record: VideoRecord | None,
) -> dict[str, object]:
    return {
        "relation_id": relation.relation_id,
        "reason": relation.reason,
        "score": relation.score,
        "automatic": relation.automatic,
        "offset_seconds": relation.offset_seconds,
        "offset_hms": format_timestamp(relation.offset_seconds),
        "recommended_keeper": str(relation.keeper),
        "candidate": str(relation.candidate),
        "keeper_size": keeper_record.size if keeper_record else "",
        "candidate_size": candidate_record.size if candidate_record else "",
        "keeper_duration": keeper_record.duration if keeper_record else "",
        "candidate_duration": candidate_record.duration if candidate_record else "",
        "keeper_resolution": (
            f"{keeper_record.width}x{keeper_record.height}"
            if keeper_record and keeper_record.width and keeper_record.height
            else ""
        ),
        "candidate_resolution": (
            f"{candidate_record.width}x{candidate_record.height}"
            if candidate_record and candidate_record.width and candidate_record.height
            else ""
        ),
        "note": relation.note,
    }


def sample_to_row(sample: EvidenceSample, output_dir: Path) -> dict[str, object]:
    screenshot = ""
    if sample.screenshot:
        try:
            screenshot = str(sample.screenshot.relative_to(output_dir))
        except ValueError:
            screenshot = str(sample.screenshot)
    return {
        "relation_id": sample.relation_id,
        "reason": sample.reason,
        "candidate": str(sample.candidate),
        "keeper": str(sample.keeper),
        "candidate_seconds": round(sample.candidate_seconds, 3),
        "candidate_hms": format_timestamp(sample.candidate_seconds),
        "keeper_seconds": round(sample.keeper_seconds, 3),
        "keeper_hms": format_timestamp(sample.keeper_seconds),
        "hamming_distance": "" if sample.hamming_distance is None else sample.hamming_distance,
        "screenshot": screenshot,
        "screenshot_status": sample.screenshot_status,
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_markdown(
    path: Path,
    title: str,
    relations: list[EvidenceRelation],
    relation_rows: list[dict[str, object]],
    samples: list[EvidenceSample],
    output_dir: Path,
) -> None:
    samples_by_relation: dict[int, list[EvidenceSample]] = {}
    for sample in samples:
        samples_by_relation.setdefault(sample.relation_id, []).append(sample)
    relation_row_by_id = {
        int(row["relation_id"]): row
        for row in relation_rows
        if isinstance(row.get("relation_id"), int)
    }

    lines = [f"# {title}", ""]
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Relations: {len(relations)}")
    lines.append(f"- Evidence samples: {len(samples)}")
    lines.append(f"- Screenshot comparisons: {sum(1 for sample in samples if sample.screenshot_status == 'ok')}")
    lines.append("")

    for relation in relations:
        row = relation_row_by_id.get(relation.relation_id, {})
        lines.append(f"## {relation.relation_id}. {relation.reason} score={relation.score}")
        lines.append("")
        lines.append("### Recommendation")
        lines.append("")
        lines.append(f"- Recommended keeper: `{relation.keeper}`")
        lines.append(f"- Candidate: `{relation.candidate}`")
        if row.get("keeper_size") != "" or row.get("candidate_size") != "":
            lines.append(f"- Size: keeper={row.get('keeper_size', '')}, candidate={row.get('candidate_size', '')}")
        if row.get("keeper_duration") != "" or row.get("candidate_duration") != "":
            lines.append(
                f"- Duration: keeper={format_timestamp(_float_or_none(row.get('keeper_duration')))}, "
                f"candidate={format_timestamp(_float_or_none(row.get('candidate_duration')))}"
            )
        if row.get("keeper_resolution") or row.get("candidate_resolution"):
            lines.append(
                f"- Resolution: keeper={row.get('keeper_resolution', '')}, "
                f"candidate={row.get('candidate_resolution', '')}"
            )
        lines.append("")
        lines.append("### Evidence Chain")
        lines.append("")
        lines.append(f"- Evidence type: `{relation.note}`")
        lines.append(f"- Automatic cleanup candidate: `{relation.automatic}`")
        lines.append(f"- Offset: {format_timestamp(relation.offset_seconds) or 'N/A'}")
        lines.append(f"- Matching threshold: sampled frame hamming distance <= report setting")
        lines.append("")
        lines.append("### Matching Samples")
        lines.append("")
        lines.append("| Candidate Time | Keeper Time | Hamming | Screenshot | Status |")
        lines.append("|---:|---:|---:|---|---|")
        for sample in samples_by_relation.get(relation.relation_id, []):
            screenshot_md = ""
            if sample.screenshot:
                try:
                    rel_path = sample.screenshot.relative_to(output_dir).as_posix()
                except ValueError:
                    rel_path = sample.screenshot.as_posix()
                screenshot_md = f"![comparison]({rel_path})" if sample.screenshot_status == "ok" else f"`{rel_path}`"
            lines.append(
                "| "
                f"{format_timestamp(sample.candidate_seconds)} | "
                f"{format_timestamp(sample.keeper_seconds)} | "
                f"{'' if sample.hamming_distance is None else sample.hamming_distance} | "
                f"{screenshot_md} | "
                f"{sample.screenshot_status} |"
            )
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
