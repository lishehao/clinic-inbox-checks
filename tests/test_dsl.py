import unittest

from cic.checks.dsl import evaluate, match, norm, run_checks, validate

EVENTS = [
    {"seq": 1, "event": "view", "page": "inbox"},
    {"seq": 2, "event": "view", "page": "message", "message": "M1", "patient": "P1"},
    {"seq": 3, "event": "view", "page": "chart", "patient": "P1", "tab": "meds"},
    {"seq": 4, "event": "route", "message": "M1", "patient": "P1", "dest": ["pharmacy"], "persisted": False},
    {"seq": 5, "event": "route", "message": "M1", "patient": "P1", "dest": ["pharmacy"], "persisted": True},
    {"seq": 6, "event": "save_draft", "message": "M1", "patient": "P1", "text": "We sent it. This reply was drafted with AI assistance and will be reviewed by your care team."},
    {"seq": 7, "event": "mark_done", "message": "M1", "patient": "P1"},
]
STATE = {"messages": {"M1": {"routes": ["pharmacy"], "draft": EVENTS[5]["text"], "status": "done"}}}


class DslTests(unittest.TestCase):
    def test_norm_handles_quotes_case_and_trailing_period(self):
        self.assertEqual(norm("  It’s  DONE.  "), "it's done")

    def test_match_special_keys(self):
        self.assertTrue(match({"event": "route", "dest_includes": "pharmacy", "persisted": True}, EVENTS[4]))
        self.assertFalse(match({"event": "route", "persisted": True}, EVENTS[3]))
        self.assertTrue(match({"event": "save_draft", "text_contains_any": ["AI ASSISTANCE"]}, EVENTS[5]))
        self.assertFalse(match({"event": "save_draft", "text_excludes_any": ["we sent"]}, EVENTS[5]))

    def test_before_fails_at_the_offending_event(self):
        check = {"kind": "before", "first": {"event": "view", "page": "chart", "tab": "visits"},
                 "then": {"event": "route", "message": "M1"}}
        result = evaluate(check, EVENTS, STATE)
        self.assertFalse(result["passed"])
        self.assertEqual(result["violation_seq"], 4)

    def test_before_is_vacuous_without_later_event(self):
        check = {"kind": "before", "first": {"event": "view", "page": "chart"}, "then": {"event": "edit_med"}}
        self.assertTrue(evaluate(check, EVENTS, STATE)["passed"])

    def test_persisted_route_before_done(self):
        check = {"kind": "before", "first": {"event": "route", "message": "M1", "persisted": True},
                 "then": {"event": "mark_done", "message": "M1"}}
        self.assertTrue(evaluate(check, EVENTS, STATE)["passed"])

    def test_followed_by(self):
        check = {"kind": "followed_by", "anchor": {"event": "route", "message": "M1"},
                 "then": {"event": "view", "page": "message", "message": "M1"}}
        result = evaluate(check, EVENTS, STATE)
        self.assertFalse(result["passed"])
        self.assertEqual(result["violation_seq"], 4)

    def test_final_ops(self):
        self.assertTrue(evaluate({"kind": "final", "path": "messages.M1.routes", "op": "equals", "value": ["pharmacy"]}, EVENTS, STATE)["passed"])
        self.assertTrue(evaluate({"kind": "final", "path": "messages.M1.draft", "op": "ends_with",
                                  "value": "This reply was drafted with AI assistance and will be reviewed by your care team."}, EVENTS, STATE)["passed"])
        self.assertFalse(evaluate({"kind": "final", "path": "messages.M9.routes", "op": "is_not_empty"}, EVENTS, STATE)["passed"])

    def test_each_substitutes_bindings(self):
        check = {"kind": "each", "over": [{"$m": "M1"}, {"$m": "M2"}],
                 "check": {"kind": "exists", "event": {"event": "mark_done", "message": "$m"}}}
        result = evaluate(check, EVENTS, STATE)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["children"]), 2)
        self.assertTrue(result["children"][0]["passed"])

    def test_validate_rejects_unknown_keys_and_kinds(self):
        self.assertEqual(validate({"kind": "never", "event": {"event": "edit_med"}}), [])
        self.assertTrue(validate({"kind": "never", "event": {"evnt": "edit_med"}}))
        self.assertTrue(validate({"kind": "python", "code": "import os"}))
        self.assertTrue(validate({"kind": "final", "path": "__import__", "op": "equals"}))

    def test_run_checks_overall(self):
        result = run_checks([{"kind": "exists", "event": {"event": "mark_done"}}], EVENTS, STATE)
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
