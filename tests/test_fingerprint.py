import unittest

from pathlib import Path

from diskcleanup.fingerprint import (
    FRAME_BYTES,
    FingerprintError,
    dhash_from_gray_9x8,
    extract_video_fingerprint_pyav_seek,
    fingerprint_from_raw_frames,
    hamming64,
    sampled_timestamps,
)


class FingerprintTests(unittest.TestCase):
    def test_hamming64_counts_changed_bits(self):
        self.assertEqual(hamming64(0b1010, 0b0011), 2)

    def test_dhash_detects_horizontal_direction(self):
        descending = bytes([9, 8, 7, 6, 5, 4, 3, 2, 1] * 8)
        ascending = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9] * 8)

        self.assertEqual(dhash_from_gray_9x8(descending), (1 << 64) - 1)
        self.assertEqual(dhash_from_gray_9x8(ascending), 0)

    def test_fingerprint_ignores_incomplete_tail(self):
        raw = bytes([0] * FRAME_BYTES) + b"tail"
        self.assertEqual(fingerprint_from_raw_frames(raw), (0,))

    def test_sampled_timestamps_respects_max_frames(self):
        self.assertEqual(
            sampled_timestamps(duration_seconds=100, interval_seconds=30, max_frames=3),
            [0, 30, 60],
        )

    def test_pyav_seek_reports_missing_optional_dependency(self):
        try:
            import av  # noqa: F401
        except ImportError:
            with self.assertRaisesRegex(FingerprintError, "PyAV is not installed"):
                extract_video_fingerprint_pyav_seek(
                    Path("missing.mp4"),
                    duration_seconds=10,
                    interval_seconds=5,
                )


if __name__ == "__main__":
    unittest.main()
