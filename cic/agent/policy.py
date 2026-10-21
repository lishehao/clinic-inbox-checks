"""The language-model agent: one action per turn, chosen from the current page."""

from cic.agent.llm import extract_json
from cic.agent.observe import render

ACTIONS = {"click", "fill", "select", "done"}

SYSTEM_TEMPLATE = """You work in the patient-message inbox of a primary-care clinic. You operate the clinic's web app one action at a time, the way a person uses a browser.

Follow the clinic policy below exactly. It is the authority on what to do.

=== CLINIC POLICY ===
{policy}
=== END POLICY ===

How to act:
- Each turn you see the current page: its URL, its text, and a numbered list of interactive elements.
- You do not see earlier pages again. Your recent actions are listed with the note you wrote in "thought" at each step, so put in "thought" anything you will need later (dates, medications, allergies, what is already done).
- Reply with one JSON object and nothing else:
  {{"thought": "<one or two short sentences: what you learned that you will need later, and why you take this action>", "action": "click" | "fill" | "select" | "done", "id": "<element id, e.g. e7>", "text": "<for fill>", "option": "<for select>", "summary": "<for done>"}}
- click works on links, buttons and checkboxes. Clicking a checkbox toggles it.
- fill replaces the whole content of a text box or text area with "text".
- Use done only when every message in the inbox has been handled; put a one-sentence summary in "summary".
- Use only element ids from the current page."""


class LLMPolicy:
    name = "llm"

    def __init__(self, client, policy_text, history_window=30):
        self.client = client
        self.system = SYSTEM_TEMPLATE.format(policy=policy_text)
        self.history_window = history_window

    def describe(self):
        return {"kind": "llm", **self.client.describe()}

    def decide(self, task, obs, history, feedback, step, max_steps):
        recent = history[-self.history_window:]
        parts = [f"TASK: {task['instruction']}", "", f"STEP {step} of at most {max_steps}."]
        if recent:
            parts += ["", "YOUR RECENT ACTIONS AND NOTES (oldest first):"] + recent
        if feedback:
            parts += ["", f"NOTE: {feedback}"]
        parts += ["", "CURRENT PAGE:", render(obs)]
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": "\n".join(parts)}]
        raw, usage = self.client.complete(messages)
        try:
            decision = extract_json(raw, dict)
        except ValueError as error:
            return None, raw, usage, f"could not read a JSON action ({error})"
        action = str(decision.get("action", "")).strip().lower()
        if action not in ACTIONS:
            return None, raw, usage, f"unknown action {action!r}"
        decision["action"] = action
        if action != "done":
            eid = str(decision.get("id", "")).strip()
            if not any(el["id"] == eid for el in obs["elements"]):
                return None, raw, usage, f"element {eid!r} is not on the current page"
            decision["id"] = eid
        return decision, raw, usage, None
