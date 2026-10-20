"""End-to-end runs with scripted agents in a real headless browser.

Each scripted variant skips one step on purpose; the test asserts that exactly
the checks for that step fail and that the outcome verdict is what it should be.
Skipped automatically when Playwright's Chromium is not installed.
"""

import json
import tempfile
import unittest

from cic.agent.runner import run_task
from cic.agent.scripted import RuleAgent
from cic.checks.report import build_report, evaluate_run


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch().close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_browser_available(), "Playwright Chromium is not installed")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_and_check(self, task_id, variant):
        out = run_task(task_id, RuleAgent(variant), self.tmp.name, tag="test")
        record = json.loads((out / "run.json").read_text(encoding="utf-8"))
        ev = evaluate_run(record)
        failed = sorted(r["id"] for r in ev["process"]["results"] if not r["passed"])
        return record, ev, failed

    def test_careful_agent_passes_every_task(self):
        for task_id in ("T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08"):
            with self.subTest(task=task_id):
                record, ev, failed = self.run_and_check(task_id, "careful")
                self.assertEqual(record["ended_by"], "done")
                self.assertTrue(ev["accepted"], [r for r in ev["outcome"]["results"] if not r["passed"]])
                self.assertEqual(failed, [])

    def test_skipped_visits_tab_looks_fine_but_fails_rule_3(self):
        _, ev, failed = self.run_and_check("T01", "skip_visits")
        self.assertTrue(ev["accepted"])
        self.assertEqual(failed, ["R3_visits_before_route:MSG01"])

    def test_skipped_visits_tab_changes_the_outcome_when_overdue(self):
        _, ev, failed = self.run_and_check("T02", "skip_visits")
        self.assertFalse(ev["accepted"])
        self.assertIn("R3_visits_before_route:MSG02", failed)

    def test_no_verify_misses_the_silent_failure(self):
        _, ev, failed = self.run_and_check("T08", "no_verify")
        self.assertFalse(ev["accepted"])
        self.assertIn("R9_routing_checked:MSG12", failed)
        self.assertIn("R10_done_after_route:MSG12", failed)

    def test_skipped_allergies_fails_rule_5_only(self):
        _, ev, failed = self.run_and_check("T07", "skip_allergies")
        self.assertTrue(ev["accepted"])
        self.assertEqual(failed, ["R5_allergies_before_reply:MSG11", "R5_allergies_before_route:MSG11"])

    def test_report_counts_measurement_1(self):
        with tempfile.TemporaryDirectory() as own:
            run_task("T01", RuleAgent("careful"), own)
            run_task("T01", RuleAgent("skip_visits"), own)
            run_task("T02", RuleAgent("skip_visits"), own)
            report = build_report(own, include_scripted=True)
            self.assertEqual(report["runs"], 3)
            self.assertEqual(report["measurement_1"]["accepted"], 2)
            self.assertEqual(report["measurement_1"]["accepted_failed"], 1)
            self.assertEqual(build_report(own)["runs"], 0)  # scripted runs are left out by default


if __name__ == "__main__":
    unittest.main()
