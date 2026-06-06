import tempfile
import unittest
from pathlib import Path

from diskcleanup.media import quick_hash_file


class MediaTests(unittest.TestCase):
    def test_quick_hash_includes_size_and_sampled_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.bin"
            second = root / "second.bin"
            third = root / "third.bin"
            first.write_bytes(b"a" * 20)
            second.write_bytes(b"a" * 20)
            third.write_bytes(b"a" * 19 + b"b")

            self.assertEqual(quick_hash_file(first, chunk_size=4), quick_hash_file(second, chunk_size=4))
            self.assertNotEqual(quick_hash_file(first, chunk_size=4), quick_hash_file(third, chunk_size=4))


if __name__ == "__main__":
    unittest.main()
