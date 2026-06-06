from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from math import ceil

from .fingerprint import hamming64
from .models import Match, VideoRecord


@dataclass(frozen=True)
class ContainmentScore:
    coverage: float
    offset_frames: int


def frame_match(left_hash: int, right_hash: int, max_distance: int) -> bool:
    return hamming64(left_hash, right_hash) <= max_distance


def aligned_similarity(
    left: tuple[int, ...],
    right: tuple[int, ...],
    *,
    max_distance: int = 10,
) -> float:
    length = min(len(left), len(right))
    if length == 0:
        return 0.0
    matches = sum(
        1 for index in range(length) if frame_match(left[index], right[index], max_distance)
    )
    return matches / length


def _offset_score(
    short: tuple[int, ...],
    long: tuple[int, ...],
    offset: int,
    *,
    max_distance: int,
    best_matches: int,
) -> int:
    matches = 0
    length = len(short)
    for index, frame_hash in enumerate(short):
        remaining = length - index
        if matches + remaining < best_matches:
            break
        if frame_match(frame_hash, long[offset + index], max_distance):
            matches += 1
    return matches


def best_containment(
    short: tuple[int, ...],
    long: tuple[int, ...],
    *,
    max_distance: int = 10,
    max_offsets: int = 5000,
) -> ContainmentScore:
    if not short or len(short) > len(long):
        return ContainmentScore(0.0, 0)

    possible_offsets = len(long) - len(short) + 1
    step = max(1, ceil(possible_offsets / max_offsets))
    offsets = list(range(0, possible_offsets, step))
    if offsets[-1] != possible_offsets - 1:
        offsets.append(possible_offsets - 1)

    best_offset = 0
    best_matches = -1
    for offset in offsets:
        matches = _offset_score(
            short,
            long,
            offset,
            max_distance=max_distance,
            best_matches=best_matches,
        )
        if matches > best_matches:
            best_matches = matches
            best_offset = offset

    if step > 1:
        start = max(0, best_offset - step)
        end = min(possible_offsets, best_offset + step + 1)
        for offset in range(start, end):
            matches = _offset_score(
                short,
                long,
                offset,
                max_distance=max_distance,
                best_matches=best_matches,
            )
            if matches > best_matches:
                best_matches = matches
                best_offset = offset

    return ContainmentScore(best_matches / len(short), best_offset)


def exact_duplicate_groups(records: list[VideoRecord]) -> list[list[VideoRecord]]:
    groups: dict[tuple[str, int], list[VideoRecord]] = defaultdict(list)
    for record in records:
        if record.sha256:
            groups[(record.sha256, record.size)].append(record)
    return [group for group in groups.values() if len(group) > 1]


def find_visual_matches(
    records: list[VideoRecord],
    *,
    min_overlap: float = 0.9,
    partial_overlap: float = 0.45,
    near_duplicate_similarity: float = 0.9,
    hash_distance: int = 10,
) -> list[Match]:
    candidates = [record for record in records if record.has_fingerprint]
    matches: list[Match] = []

    for left, right in combinations(candidates, 2):
        if left.sha256 and right.sha256 and left.sha256 == right.sha256:
            continue
        if left.fingerprint_interval != right.fingerprint_interval:
            continue

        left_duration = left.duration or len(left.fingerprint) * (left.fingerprint_interval or 1)
        right_duration = right.duration or len(right.fingerprint) * (right.fingerprint_interval or 1)
        duration_ratio = min(left_duration, right_duration) / max(left_duration, right_duration)

        if duration_ratio >= 0.92:
            similarity = aligned_similarity(
                left.fingerprint,
                right.fingerprint,
                max_distance=hash_distance,
            )
            if similarity >= near_duplicate_similarity:
                matches.append(Match("near_duplicate", left, right, similarity))
                continue

        short, long = (left, right) if len(left.fingerprint) <= len(right.fingerprint) else (right, left)
        score = best_containment(
            short.fingerprint,
            long.fingerprint,
            max_distance=hash_distance,
        )
        interval = short.fingerprint_interval or long.fingerprint_interval or 1.0
        offset_seconds = score.offset_frames * interval
        if score.coverage >= min_overlap:
            matches.append(
                Match("contained_in", short, long, score.coverage, offset_seconds)
            )
        elif score.coverage >= partial_overlap:
            matches.append(
                Match("partial_overlap", short, long, score.coverage, offset_seconds)
            )

    return sorted(matches, key=lambda match: match.score, reverse=True)
