from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path


class MoveError(RuntimeError):
    pass


def unique_destination(quarantine: Path, source: Path) -> Path:
    destination = quarantine / source.drive.replace(":", "") / source.relative_to(source.anchor)
    if not destination.exists():
        return destination

    stem = destination.stem
    suffix = destination.suffix
    parent = destination.parent
    for index in range(1, 10_000):
        candidate = parent / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise MoveError(f"could not find unique quarantine path for {source}")


def load_plan(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if isinstance(data, dict):
        items = data.get("items")
    else:
        items = data
    if not isinstance(items, list):
        raise MoveError("plan file must contain a list or an object with an items list")
    return [item for item in items if isinstance(item, dict)]


def move_from_plan(
    items: list[dict[str, object]],
    *,
    quarantine: Path,
    dry_run: bool = True,
    manifest_path: Path,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    quarantine.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    for item in items:
        if item.get("action") != "move":
            continue

        victim_value = item.get("victim")
        if not isinstance(victim_value, str):
            continue

        source = Path(victim_value).expanduser()
        if not source.is_absolute():
            source = source.resolve()
        destination = unique_destination(quarantine.resolve(), source)
        result = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "source": str(source),
            "destination": str(destination),
            "keeper": item.get("keeper"),
            "reason": item.get("reason"),
            "confidence": item.get("confidence"),
        }

        if not source.exists():
            result["status"] = "missing"
        elif dry_run:
            result["status"] = "would_move"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            result["status"] = "moved"

        results.append(result)

    if not dry_run:
        with manifest_path.open("a", encoding="utf-8") as manifest:
            for result in results:
                manifest.write(json.dumps(result, ensure_ascii=False) + "\n")

    return results
