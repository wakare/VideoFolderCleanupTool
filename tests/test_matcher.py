import unittest

from pathlib import Path

from diskcleanup.matcher import aligned_similarity, best_containment, find_visual_matches
from diskcleanup.models import VideoRecord


def record(path: str, fingerprint: tuple[int, ...], duration: float) -> VideoRecord:
    return VideoRecord(
        path=Path(path),
        root=Path("D:/Videos"),
        size=100,
        mtime_ns=1,
        sha256=None,
        duration=duration,
        width=1280,
        height=720,
        codec="h264",
        bit_rate=1000,
        fps=30,
        frames=None,
        fingerprint=fingerprint,
        fingerprint_interval=10,
    )


class MatcherTests(unittest.TestCase):
    def test_aligned_similarity_uses_hamming_threshold(self):
        left = (0b0000, 0b1111)
        right = (0b0001, 0b0111)
        self.assertEqual(aligned_similarity(left, right, max_distance=1), 1.0)
        self.assertEqual(aligned_similarity(left, right, max_distance=0), 0.0)

    def test_best_containment_finds_offset(self):
        short = (10, 20, 30)
        long = (1, 2, 10, 20, 30, 4)

        score = best_containment(short, long, max_distance=0)

        self.assertEqual(score.coverage, 1.0)
        self.assertEqual(score.offset_frames, 2)

    def test_find_visual_matches_detects_near_duration_offset_containment(self):
        short = record("D:/Videos/short.mp4", (10, 20, 30, 40), 65)
        long = record("D:/Videos/long.mp4", (1, 2, 10, 20, 30, 40, 5), 70)

        matches = find_visual_matches(
            [short, long],
            min_overlap=0.9,
            partial_overlap=0.45,
            near_duplicate_similarity=0.9,
            hash_distance=0,
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].kind, "contained_in")
        self.assertEqual(matches[0].offset_seconds, 20)


if __name__ == "__main__":
    unittest.main()
