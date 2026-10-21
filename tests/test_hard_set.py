"""The hard set: same policy, harder situations. Scripted agents show which checks each shortcut trips."""

import json
import tempfile
import unittest

from cic.checks.report import build_report, evaluate_run
from cic.sim.data import all_task_ids, load_seed, load_task
from tests.test_e2e import _browser_available


class HardSetDataTests(unittest.TestCase):
    def test_hard_tasks_use_the_hard_seed_and_the_same_policy(self):
        seed = load_seed()
        hard = [t for t in all_task_ids() if load_task(t).get("set") == "hard"]
        self.assertEqual(hard, ["T09", "T10", "T11", "T12"])
        for tid in hard:
            for mid in load_task(tid)["inbox"]:
                self.assertIn(mid, seed["messages"])
                self.assertIn(seed["messages"][mid]["patient"], seed["patients"])
        self.assertEqual(load_task("T09")["max_steps"], 161)
        self.assertEqual(len(load_task("T09")["inbox"]), 12)

    def test_base_tasks_are_unchanged_apart_from_their_set(self):
        for tid in ("T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08"):
            task = load_task(tid)
            self.assertEqual(task["set"], "base")
            self.assertNotIn("max_steps", task)


@unittest.skipUnless(_browser_available(), "Playwright Chromium is not installed")
class HardSetScriptedRuns(unittest.TestCase):
    def run_one(self, task_id, variant):
        from cic.agent.runner import run_task
        from cic.agent.scripted import RuleAgent
        with tempfile.TemporaryDirectory() as out:
            path = run_task(task_id, RuleAgent(variant), out)
            record = json.loads((path / "run.json").read_text(encoding="utf-8"))
        ev = evaluate_run(record)
        return ev["accepted"], sorted(r["id"] for r in ev["process"]["results"] if not r["passed"]), record

    def test_careful_agent_passes_every_hard_task(self):
        for tid in ("T10", "T11", "T12"):
            accepted, failed, _ = self.run_one(tid, "careful")
            self.assertTrue(accepted, tid)
            self.assertEqual(failed, [], tid)

    def test_skipping_allergies_on_t10_is_accepted_but_caught(self):
        accepted, failed, _ = self.run_one("T10", "skip_allergies")
        self.assertTrue(accepted)
        self.assertEqual(failed, ["R5_allergies_before_reply:MSG25", "R5_allergies_before_route:MSG25"])

    def test_not_reopening_on_t11_loses_the_fourth_refill(self):
        accepted, failed, record = self.run_one("T11", "no_verify")
        self.assertFalse(accepted)
        self.assertEqual(record["final_state"]["messages"]["MSG29"]["routes"], [])
        self.assertIn("R9_routing_checked:MSG29", failed)

    def test_explained_away_red_flag_is_still_urgent(self):
        accepted, failed, record = self.run_one("T12", "careful")
        self.assertIn("urgent", record["final_state"]["messages"]["MSG30"]["routes"])
        self.assertIn("pharmacy", record["final_state"]["messages"]["MSG30"]["routes"])

    def test_report_totals_by_set(self):
        from cic.agent.runner import run_task
        from cic.agent.scripted import RuleAgent
        with tempfile.TemporaryDirectory() as out:
            run_task("T01", RuleAgent("careful"), out)
            run_task("T10", RuleAgent("skip_allergies"), out)
            report = build_report(out, include_scripted=True)
        self.assertEqual(report["by_set"]["base"]["accepted_failed"], 0)
        self.assertEqual(report["by_set"]["hard"]["accepted_failed"], 1)


if __name__ == "__main__":
    unittest.main()
