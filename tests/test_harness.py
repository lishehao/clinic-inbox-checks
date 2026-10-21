"""The agent keeps its own notes in its history, and runs are tagged with the harness version."""

import json
import tempfile
import unittest
from pathlib import Path

from cic.agent.runner import HARNESS_VERSION, _history_line, completed_counts


def _write_run(root, name, task_id, model, version, ended_by="done"):
    folder = Path(root) / name
    folder.mkdir()
    record = {"run_id": name, "task_id": task_id, "tag": "", "agent": {"kind": "llm", "model": model},
              "ended_by": ended_by, "steps": [], "events": [], "usage": {},
              "final_state": {"messages": {}, "patients": {}}}
    if version is not None:
        record["harness_version"] = version
    (folder / "run.json").write_text(json.dumps(record), encoding="utf-8")


class HistoryTests(unittest.TestCase):
    def test_the_note_is_kept_with_the_action(self):
        decision = {"action": "click", "id": "e5", "thought": "Last visit 2026-06-02,\n within 12 months."}
        line = _history_line(4, decision, "Visits", "/chart/P01?tab=visits", None)
        self.assertIn('click e5 "Visits" -> now at /chart/P01?tab=visits', line)
        self.assertIn('note: "Last visit 2026-06-02, within 12 months."', line)

    def test_no_note_when_there_is_no_thought(self):
        line = _history_line(1, {"action": "click", "id": "e1"}, "Inbox", "/inbox", None)
        self.assertNotIn("note:", line)


class HarnessVersionTests(unittest.TestCase):
    def test_older_runs_are_left_out_of_resume_and_report(self):
        from cic.checks.report import build_report
        with tempfile.TemporaryDirectory() as root:
            _write_run(root, "a", "T01", "m", None)                 # recorded before versions existed
            _write_run(root, "b", "T01", "m", HARNESS_VERSION - 1)
            _write_run(root, "c", "T01", "m", HARNESS_VERSION)
            self.assertEqual(completed_counts(root, "m")["T01"], 1)
            report = build_report(root)
            self.assertEqual((report["runs"], report["left_out_old_harness"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
