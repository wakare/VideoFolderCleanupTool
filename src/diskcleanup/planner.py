from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .models import Match, PlanItem, VideoRecord
from .matcher import exact_duplicate_groups, find_visual_matches, quick_duplicate_groups


def keeper_rank(record: VideoRecord) -> tuple[int, float, int, int, str]:
    return (
        record.pixel_count,
        record.duration or 0.0,
        record.bit_rate or 0,
        record.size,
        str(record.path).lower(),
    )


def choose_keeper(records: list[VideoRecord]) -> VideoRecord:
    return max(records, key=keeper_rank)


def plan_exact_duplicates(records: list[VideoRecord]) -> list[PlanItem]:
    items: list[PlanItem] = []
    for group in exact_duplicate_groups(records):
        keeper = choose_keeper(group)
        for record in group:
            if record == keeper:
                continue
            items.append(
                PlanItem(
                    action="move",
                    reason="exact_duplicate",
                    victim=record.path,
                    keeper=keeper.path,
                    confidence=1.0,
                    overlap_ratio=1.0,
                    details={"sha256": record.sha256, "size": record.size},
                )
            )
    return items


def plan_quick_duplicates(records: list[VideoRecord], already_selected: set[str]) -> list[PlanItem]:
    items: list[PlanItem] = []
    for group in quick_duplicate_groups(records):
        keeper = choose_keeper(group)
        for record in group:
            if record == keeper or str(record.path) in already_selected:
                continue
            already_selected.add(str(record.path))
            items.append(
                PlanItem(
                    action="move",
                    reason="quick_hash_duplicate",
                    victim=record.path,
                    keeper=keeper.path,
                    confidence=0.999,
                    overlap_ratio=1.0,
                    details={"quick_hash": record.quick_hash, "size": record.size},
                )
            )
    return items


def plan_visual_matches(matches: list[Match], already_selected: set[str]) -> list[PlanItem]:
    items: list[PlanItem] = []
    for match in matches:
        if match.kind == "partial_overlap":
            continue

        if match.kind == "contained_in":
            victim = match.left
            keeper = match.right
            reason = "contained_in"
        elif match.kind == "near_duplicate":
            keeper = choose_keeper([match.left, match.right])
            victim = match.right if keeper == match.left else match.left
            reason = "near_duplicate"
        else:
            continue

        victim_key = str(victim.path)
        if victim_key in already_selected:
            continue
        already_selected.add(victim_key)
        items.append(
            PlanItem(
                action="move",
                reason=reason,
                victim=victim.path,
                keeper=keeper.path,
                confidence=match.score,
                overlap_ratio=match.score,
                offset_seconds=match.offset_seconds,
                details={
                    "keeper_duration": keeper.duration,
                    "victim_duration": victim.duration,
                    "keeper_resolution": [keeper.width, keeper.height],
                    "victim_resolution": [victim.width, victim.height],
                },
            )
        )
    return items


def build_cleanup_plan(
    records: list[VideoRecord],
    *,
    min_overlap: float = 0.9,
    partial_overlap: float = 0.45,
    near_duplicate_similarity: float = 0.9,
    hash_distance: int = 10,
    candidate_mode: str = "indexed",
    min_anchor_votes: int = 3,
    anchor_stride: int = 1,
    max_anchor_bucket: int = 200,
) -> tuple[list[PlanItem], list[Match]]:
    exact_items = plan_exact_duplicates(records)
    selected = {str(item.victim) for item in exact_items}
    quick_items = plan_quick_duplicates(records, selected)
    visual_matches = find_visual_matches(
        records,
        min_overlap=min_overlap,
        partial_overlap=partial_overlap,
        near_duplicate_similarity=near_duplicate_similarity,
        hash_distance=hash_distance,
        candidate_mode=candidate_mode,
        min_anchor_votes=min_anchor_votes,
        anchor_stride=anchor_stride,
        max_anchor_bucket=max_anchor_bucket,
    )
    visual_items = plan_visual_matches(visual_matches, selected)
    return normalize_keeper_chains(exact_items + quick_items + visual_items), visual_matches


def final_keeper(path: Path, victim_to_keeper: dict[str, Path]) -> Path:
    seen: set[str] = set()
    current = path
    while str(current) in victim_to_keeper and str(current) not in seen:
        seen.add(str(current))
        current = victim_to_keeper[str(current)]
    return current


def normalize_keeper_chains(items: list[PlanItem]) -> list[PlanItem]:
    victim_to_keeper = {str(item.victim): item.keeper for item in items}
    normalized: list[PlanItem] = []
    for item in items:
        keeper = final_keeper(item.keeper, victim_to_keeper)
        normalized.append(replace(item, keeper=keeper))
    return normalized


def plan_item_to_dict(item: PlanItem) -> dict[str, object]:
    return {
        "action": item.action,
        "reason": item.reason,
        "victim": str(item.victim),
        "keeper": str(item.keeper),
        "confidence": round(item.confidence, 6),
        "overlap_ratio": None if item.overlap_ratio is None else round(item.overlap_ratio, 6),
        "offset_seconds": item.offset_seconds,
        "details": item.details or {},
    }
