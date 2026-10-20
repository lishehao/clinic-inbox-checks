import json
import unittest

from cic.checks.dsl import validate
from cic.sim.app import create_app
from cic.sim.data import all_task_ids, build_state, load_seed, load_task


class SimTests(unittest.TestCase):
    def setUp(self):
        self.seed = load_seed()

    def client(self, task_id):
        state = build_state(self.seed, load_task(task_id))
        return state, create_app(state, "secret").test_client()

    def test_every_task_loads_and_every_check_is_valid(self):
        ids = all_task_ids()
        self.assertEqual(ids, [f"T0{i}" for i in range(1, 9)])
        for task_id in ids:
            task = load_task(task_id)
            for message_id in task["inbox"]:
                self.assertIn(message_id, self.seed["messages"])
            for check in task["outcome_checks"] + task["process_checks"]:
                self.assertEqual(validate(check), [], (task_id, check["id"]))

    def test_pages_render_and_views_are_logged(self):
        state, c = self.client("T01")
        self.assertEqual(c.get("/inbox").status_code, 200)
        self.assertEqual(c.get("/message/MSG01").status_code, 200)
        page = c.get("/chart/P01?tab=visits&from=MSG01")
        self.assertIn(b"2026-06-02", page.data)
        kinds = [(e["event"], e.get("page"), e.get("tab")) for e in state["events"]]
        self.assertEqual(kinds, [("view", "inbox", None), ("view", "message", None), ("view", "chart", "visits")])

    def test_silent_route_failure_is_logged_but_not_stored(self):
        state, c = self.client("T08")
        c.post("/message/MSG12/route", data={"dest": ["pharmacy"]})
        self.assertEqual(state["messages"]["MSG12"]["routes"], [])
        self.assertFalse(state["events"][-1]["persisted"])
        c.post("/message/MSG12/route", data={"dest": ["pharmacy"]})
        self.assertEqual(state["messages"]["MSG12"]["routes"], ["pharmacy"])
        self.assertTrue(state["events"][-1]["persisted"])

    def test_unknown_destinations_are_ignored(self):
        state, c = self.client("T01")
        c.post("/message/MSG01/route", data={"dest": ["pharmacy", "billing"]})
        self.assertEqual(state["messages"]["MSG01"]["routes"], ["pharmacy"])

    def test_harness_state_needs_token(self):
        _, c = self.client("T01")
        self.assertEqual(c.get("/__harness/state").status_code, 404)
        body = c.get("/__harness/state", headers={"X-Harness-Token": "secret"}).data
        self.assertIn("messages", json.loads(body))

    def test_dose_edit_is_logged(self):
        state, c = self.client("T04")
        c.post("/chart/P04/med/P04-M1/dose", data={"dose": "100 mcg", "from": "MSG04"})
        self.assertEqual(state["patients"]["P04"]["meds"][0]["dose"], "100 mcg")
        self.assertEqual(state["events"][-1]["event"], "edit_med")


if __name__ == "__main__":
    unittest.main()
