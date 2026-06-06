import unittest
from pathlib import Path

from diskcleanup.models import Match, VideoRecord
from diskcleanup.planner import normalize_keeper_chains, plan_visual_matches


def record(path: str, *, duration: float, width: int = 1920, height: int = 1080) -> VideoRecord:
    return VideoRecord(
        path=Path(path),
        root=Path("D:/Videos"),
        size=100,
        mtime_ns=1,
        sha256=None,
        duration=duration,
        width=width,
        height=height,
        codec="h264",
        bit_rate=1000,
        fps=30,
        frames=None,
        fingerprint=(1, 2, 3),
        fingerprint_interval=2,
    )


class PlannerTests(unittest.TestCase):
    def test_contained_match_moves_shorter_file(self):
        short = record("D:/Videos/short.mp4", duration=10)
        long = record("D:/Videos/long.mp4", duration=60)

        items = plan_visual_matches(
            [Match("contained_in", short, long, 0.95, 12.0)],
            set(),
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].victim, short.path)
        self.assertEqual(items[0].keeper, long.path)

    def test_quick_hash_duplicate_moves_lower_ranked_file(self):
        low = record("D:/Videos/low.mp4", duration=10, width=640, height=360)
        high = record("D:/Videos/high.mp4", duration=10, width=1920, height=1080)
        low = low.__class__(**{**low.__dict__, "quick_hash": "abc"})
        high = high.__class__(**{**high.__dict__, "quick_hash": "abc"})

        from diskcleanup.planner import plan_quick_duplicates

        items = plan_quick_duplicates([low, high], set())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].victim, low.path)
        self.assertEqual(items[0].keeper, high.path)
        self.assertEqual(items[0].reason, "quick_hash_duplicate")

    def test_keeper_chains_resolve_to_final_survivor(self):
        short = record("D:/Videos/short.mp4", duration=10)
        copy = record("D:/Videos/short-copy.mp4", duration=10)
        long = record("D:/Videos/long.mp4", duration=60)

        items = normalize_keeper_chains(
            [
                plan_visual_matches([Match("near_duplicate", copy, short, 1.0)], set())[0],
                plan_visual_matches([Match("contained_in", short, long, 0.95)], set())[0],
            ]
        )

        self.assertEqual(items[0].keeper, long.path)


if __name__ == "__main__":
    unittest.main()
