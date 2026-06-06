import unittest

from diskcleanup.matcher import aligned_similarity, best_containment


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


if __name__ == "__main__":
    unittest.main()
