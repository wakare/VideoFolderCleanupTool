import json
import tempfile
import unittest
from pathlib import Path

from diskcleanup.gui.workflows import (
    parse_path_list,
    plan_settings_from_payload,
    read_evidence_preview,
    read_plan_preview,
    scan_settings_from_payload,
)


class GuiWorkflowTests(unittest.TestCase):
    def test_parse_path_list_accepts_lines_and_semicolons(self):
        self.assertEqual(
            parse_path_list('"D:/Videos"; G:/Download\nH:/More'),
            ["D:/Videos", "G:/Download", "H:/More"],
        )

    def test_scan_settings_validates_modes(self):
        with self.assertRaises(ValueError):
            scan_settings_from_payload({"paths": "D:/Videos", "fingerprint_mode": "bad"})

        settings = scan_settings_from_payload({"paths": "D:/Videos", "workers": 2})
        self.assertEqual(settings.paths, ["D:/Videos"])
        self.assertEqual(settings.fingerprint_mode, "seek")
        self.assertEqual(settings.workers, 2)

    def test_plan_settings_parses_numeric_thresholds(self):
        settings = plan_settings_from_payload(
            {
                "db": "cache.sqlite",
                "fingerprint_profile": "coarse20",
                "output": "plan.json",
                "min_overlap": "0.8",
                "candidate_mode": "exhaustive",
            }
        )

        self.assertEqual(settings.fingerprint_profile, "coarse20")
        self.assertEqual(settings.min_overlap, 0.8)
        self.assertEqual(settings.candidate_mode, "exhaustive")

    def test_read_plan_preview_summarizes_items_and_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(
                json.dumps(
                    {
                        "thresholds": {"candidate_mode": "indexed"},
                        "items": [
                            {"reason": "quick_hash_duplicate", "victim": "a", "keeper": "b"},
                            {"reason": "contained_in", "victim": "c", "keeper": "d"},
                        ],
                        "manual_review": [{"kind": "partial_overlap"}],
                    }
                ),
                encoding="utf-8",
            )

            preview = read_plan_preview(path)

            self.assertEqual(preview["item_count"], 2)
            self.assertEqual(preview["manual_review_count"], 1)
            self.assertEqual(preview["reason_counts"]["quick_hash_duplicate"], 1)

    def test_read_evidence_preview_adds_absolute_screenshot_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "report.json").write_text('{"samples": 1}', encoding="utf-8")
            (output / "relations.csv").write_text("relation_id,reason\n1,contained_in\n", encoding="utf-8")
            (output / "evidence-samples.csv").write_text(
                "relation_id,screenshot,screenshot_status\n1,screenshots/a.jpg,ok\n",
                encoding="utf-8",
            )

            preview = read_evidence_preview(output)

            self.assertEqual(preview["summary"]["samples"], 1)
            self.assertEqual(preview["relations"][0]["reason"], "contained_in")
            self.assertEqual(preview["samples"][0]["screenshot_path"], str(output / "screenshots/a.jpg"))


if __name__ == "__main__":
    unittest.main()
