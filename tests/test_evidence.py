import json
import tempfile
import unittest
from pathlib import Path

from diskcleanup.evidence import (
    build_evidence_report,
    format_timestamp,
    pick_evidence_pairs,
    relations_from_plan,
    safe_slug,
)
from diskcleanup.db import connect, upsert_record
from diskcleanup.models import VideoRecord


def record(path: str, fingerprint: tuple[int, ...], interval: float = 10) -> VideoRecord:
    return VideoRecord(
        path=Path(path),
        root=Path("D:/Videos"),
        size=100,
        mtime_ns=1,
        sha256=None,
        duration=len(fingerprint) * interval,
        width=1280,
        height=720,
        codec="h264",
        bit_rate=1000,
        fps=30,
        frames=None,
        fingerprint=fingerprint,
        fingerprint_interval=interval,
    )


class EvidenceTests(unittest.TestCase):
    def test_format_timestamp(self):
        self.assertEqual(format_timestamp(200), "03:20.00")
        self.assertEqual(format_timestamp(3723.5), "01:02:03.50")
        self.assertEqual(format_timestamp(None), "")

    def test_safe_slug_removes_path_separators_and_spaces(self):
        self.assertEqual(safe_slug("a/b c.mp4"), "a_b_c.mp4")

    def test_relations_from_plan_includes_automatic_and_manual_evidence(self):
        plan = {
            "items": [
                {
                    "reason": "exact_duplicate",
                    "victim": "D:/Videos/full-a.mp4",
                    "keeper": "D:/Videos/full-b.mp4",
                    "confidence": 1.0,
                },
                {
                    "reason": "quick_hash_duplicate",
                    "victim": "D:/Videos/a.mp4",
                    "keeper": "D:/Videos/b.mp4",
                    "confidence": 0.999,
                },
                {
                    "reason": "near_duplicate",
                    "victim": "D:/Videos/c.mp4",
                    "keeper": "D:/Videos/d.mp4",
                    "confidence": 0.95,
                },
                {
                    "reason": "unhandled_reason",
                    "victim": "D:/Videos/e.mp4",
                    "keeper": "D:/Videos/f.mp4",
                },
            ],
            "manual_review": [
                {
                    "kind": "partial_overlap",
                    "left": "D:/Videos/g.mp4",
                    "right": "D:/Videos/h.mp4",
                    "score": 0.6,
                    "offset_seconds": 120,
                }
            ],
        }

        relations = relations_from_plan(plan)

        self.assertEqual([relation.reason for relation in relations], [
            "exact_duplicate",
            "quick_hash_duplicate",
            "near_duplicate",
            "partial_overlap",
        ])
        self.assertTrue(relations[0].automatic)
        self.assertEqual(relations[0].note, "same_full_file_sha256")
        self.assertEqual(relations[1].note, "same_size_and_edge_chunk_hash")
        self.assertFalse(relations[3].automatic)
        self.assertEqual(relations[3].offset_seconds, 120)

    def test_relations_from_plan_can_exclude_manual_review(self):
        plan = {
            "items": [],
            "manual_review": [
                {
                    "kind": "partial_overlap",
                    "left": "D:/Videos/a.mp4",
                    "right": "D:/Videos/b.mp4",
                }
            ],
        }

        self.assertEqual(relations_from_plan(plan, include_manual=False), [])

    def test_pick_evidence_pairs_uses_offset_and_evenly_samples(self):
        candidate = record("D:/Videos/short.mp4", (10, 20, 30, 40, 50))
        keeper = record("D:/Videos/long.mp4", (1, 10, 20, 30, 40, 50, 2))

        pairs = pick_evidence_pairs(
            candidate,
            keeper,
            offset_seconds=10,
            max_distance=0,
            max_samples=3,
        )

        self.assertEqual(pairs, [(0, 1, 0), (2, 3, 0), (4, 5, 0)])

    def test_build_evidence_report_publishes_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "cache.sqlite"
            plan_path = root / "plan.json"
            output_dir = root / "evidence"
            candidate = root / "candidate.mp4"
            keeper = root / "keeper.mp4"
            connection = connect(db_path)
            upsert_record(connection, record(str(candidate), (10, 20, 30)))
            upsert_record(connection, record(str(keeper), (10, 20, 30)))
            connection.commit()
            connection.close()
            plan_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "reason": "near_duplicate",
                                "victim": str(candidate),
                                "keeper": str(keeper),
                                "confidence": 1.0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            progress = []

            summary = build_evidence_report(
                plan_path=plan_path,
                db_path=db_path,
                profile=None,
                output_dir=output_dir,
                title="Evidence",
                max_samples=2,
                screenshots=False,
                progress_callback=lambda update: progress.append(update),
            )

            self.assertEqual(summary["relations"], 1)
            self.assertEqual(summary["samples"], 2)
            self.assertEqual(progress[0]["relations_total"], 1)
            self.assertEqual(progress[-1]["phase"], "completed")
            self.assertEqual(progress[-1]["processed"], 1)
            self.assertEqual(progress[-1]["samples"], 2)


if __name__ == "__main__":
    unittest.main()
