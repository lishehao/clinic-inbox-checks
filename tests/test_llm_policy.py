"""The model agent's plumbing, tested with a fake model (no network, no key)."""

import json
import tempfile
import unittest

from cic.agent.llm import extract_json
from cic.agent.policy import LLMPolicy
from cic.agent.scripted import RuleAgent
from cic.checks.report import evaluate_run
from cic.sim.data import load_policy
from tests.test_e2e import _browser_available


class FakeModel:
    """Answers like a chat model, but the choices come from the careful scripted agent.

    The first reply of every run is deliberately malformed to exercise the retry path.
    """

    model = "fake/scripted-careful"

    def __init__(self):
        self.agent = RuleAgent("careful")
        self.calls = 0
        self.prompts = []

    def describe(self):
        return {"base_url": "fake://", "model": self.model, "temperature": None}

    def complete(self, messages, max_tokens=900):
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        if self.calls == 1:
            return "I think I should open the message.", {"prompt_tokens": 10, "completion_tokens": 5}
        obs = self._obs_from_prompt(messages[-1]["content"])
        decision = self.agent._next(obs)
        return "Sure.\n```json\n" + json.dumps(decision) + "\n```", {"prompt_tokens": 100, "completion_tokens": 20}

    @staticmethod
    def _obs_from_prompt(prompt):
        page = prompt.split("CURRENT PAGE:\n", 1)[1]
        url = page.split("\n", 1)[0].replace("URL: ", "")
        text = page.split("PAGE TEXT:\n", 1)[1].split("\n\nELEMENTS:\n", 1)[0]
        elements = []
        for line in page.split("\n\nELEMENTS:\n", 1)[1].splitlines():
            eid, rest = line[1:].split("] ", 1)
            kind, label = rest.split(" ", 1)
            label = label.split('"')[1]
            el = {"id": eid, "tag": {"link": "a", "button": "button", "textarea": "textarea"}.get(kind, "input"),
                  "type": kind if kind in ("checkbox", "text") else "", "label": label}
            if kind == "checkbox":
                el["checked"] = "(checked)" in rest
            elements.append(el)
        return {"url": url, "text": text, "elements": elements}


class FakeCheckWriter:
    model = "fake/check-writer"

    def complete(self, messages, max_tokens=900):
        self.prompt = messages[-1]["content"]
        checks = [
            {"id": "chart", "desc": "chart first", "kind": "before",
             "first": {"event": "view", "page": "chart", "patient": "P01"}, "then": {"event": "route", "message": "MSG01"}},
            {"id": "chart", "desc": "duplicate id gets renamed", "kind": "never", "event": {"event": "edit_med"}},
            {"id": "bad", "desc": "unknown key", "kind": "exists", "event": {"evnt": "view"}},
            {"id": "code", "kind": "python", "code": "print(1)"},
        ]
        return "Here are the checks:\n" + json.dumps(checks), {}


class ModelChecksTests(unittest.TestCase):
    def test_generate_keeps_only_valid_checks(self):
        from cic.checks.model_checks import generate, load_model_checks
        writer = FakeCheckWriter()
        with tempfile.TemporaryDirectory() as out:
            path, record = generate(writer, "T01", out)
            self.assertEqual([c["id"] for c in record["checks"]], ["chart", "chart_"])
            self.assertEqual(len(record["invalid"]), 2)
            loaded = load_model_checks(path.parent)
            self.assertIn("T01", loaded)
        self.assertIn("MSG01: from Dana Whitfield (patient P01)", writer.prompt)
        self.assertIn("CLINIC POLICY", writer.prompt)
        self.assertNotIn("R3_visits_before_route", writer.prompt)  # hand-written checks are not shown


class ExtractJsonTests(unittest.TestCase):
    def test_fenced_and_prose(self):
        self.assertEqual(extract_json('Sure!\n```json\n{"a": "}"}\n```'), {"a": "}"})
        self.assertEqual(extract_json('[{"x": 1}] trailing', list), [{"x": 1}])
        with self.assertRaises(ValueError):
            extract_json("no json here")


@unittest.skipUnless(_browser_available(), "Playwright Chromium is not installed")
class LLMPolicyEndToEnd(unittest.TestCase):
    def test_model_agent_runs_and_recovers_from_a_bad_reply(self):
        from cic.agent.runner import run_task
        fake = FakeModel()
        with tempfile.TemporaryDirectory() as out:
            path = run_task("T08", LLMPolicy(fake, load_policy()), out)
            record = json.loads((path / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["agent"]["model"], "fake/scripted-careful")
        self.assertIsNone(record["steps"][0]["decision"])
        self.assertIn("could not read a JSON action", record["steps"][0]["error"])
        self.assertIn("NOTE:", fake.prompts[1])
        ev = evaluate_run(record)
        self.assertTrue(ev["accepted"])
        self.assertEqual([r["id"] for r in ev["process"]["results"] if not r["passed"]], [])
        self.assertGreater(record["usage"]["prompt_tokens"], 0)
        self.assertIn("CLINIC POLICY", LLMPolicy(fake, load_policy()).system)


if __name__ == "__main__":
    unittest.main()
