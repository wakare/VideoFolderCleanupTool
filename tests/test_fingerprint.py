import unittest

from diskcleanup.fingerprint import FRAME_BYTES, dhash_from_gray_9x8, fingerprint_from_raw_frames, hamming64


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


if __name__ == "__main__":
    unittest.main()
