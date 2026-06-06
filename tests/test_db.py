import tempfile
import unittest
from pathlib import Path

from diskcleanup.db import connect, get_record, list_profiles, list_records, upsert_record
from diskcleanup.models import VideoRecord


def record(path: Path, profile: str, fingerprint: tuple[int, ...]) -> VideoRecord:
    return VideoRecord(
        path=path,
        root=path.parent,
        size=10,
        mtime_ns=1,
        sha256=None,
        duration=60,
        width=1280,
        height=720,
        codec="h264",
        bit_rate=1000,
        fps=30,
        frames=None,
        fingerprint=fingerprint,
        fingerprint_interval=10,
        quick_hash="quick",
        fingerprint_profile=profile,
    )


class DbTests(unittest.TestCase):
    def test_stores_multiple_fingerprint_profiles_for_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "cache.sqlite"
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video")
            connection = connect(db_path)

            upsert_record(connection, record(path, "coarse", (1, 2, 3)))
            upsert_record(connection, record(path, "fine", (4, 5, 6, 7)))
            connection.commit()

            coarse = get_record(connection, path, profile="coarse")
            fine = get_record(connection, path, profile="fine")
            latest = get_record(connection, path)

            self.assertEqual(coarse.fingerprint, (1, 2, 3))
            self.assertEqual(fine.fingerprint, (4, 5, 6, 7))
            self.assertEqual(latest.fingerprint_profile, "fine")
            self.assertEqual(list_profiles(connection), [("coarse", 1), ("fine", 1)])
            connection.close()

    def test_list_records_with_missing_profile_keeps_metadata_without_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "cache.sqlite"
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video")
            connection = connect(db_path)
            upsert_record(connection, record(path, "coarse", (1, 2, 3)))
            connection.commit()

            records = list_records(connection, profile="fine")

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].duration, 60)
            self.assertEqual(records[0].fingerprint, ())
            self.assertIsNone(records[0].fingerprint_profile)
            connection.close()


if __name__ == "__main__":
    unittest.main()
