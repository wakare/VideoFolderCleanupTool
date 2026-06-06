import time
import unittest

from diskcleanup.gui.jobs import JobManager


class GuiJobTests(unittest.TestCase):
    def test_job_manager_tracks_progress_and_result(self):
        manager = JobManager(max_workers=1)
        try:
            job = manager.submit(
                "unit",
                lambda progress: (
                    progress({"step": 1}, "working"),
                    {"ok": True},
                )[1],
            )

            for _ in range(50):
                current = manager.get(job.id)
                if current and current.status == "completed":
                    break
                time.sleep(0.02)

            current = manager.get(job.id)
            self.assertIsNotNone(current)
            self.assertEqual(current.status, "completed")
            self.assertEqual(current.progress["step"], 1)
            self.assertEqual(current.result, {"ok": True})
            self.assertIn("working", "\n".join(current.logs))
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
