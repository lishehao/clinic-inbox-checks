"""Scripted, rule-based agents for testing the pipeline. No language model.

These exist so the simulator, the harness, the checks and the viewer can be
tested end to end without an API key. Their runs are labelled "scripted" and
are kept out of the measurements unless asked for explicitly. Each variant
skips one step on purpose, so the tests know which checks should fail.
"""

import datetime
import json
import re

from cic.agent.observe import find

DISCLOSURE = "This reply was drafted with AI assistance and will be reviewed by your care team."
URGENT = ("If you have chest pain, trouble breathing, or fainting, call 911 or go to the nearest emergency "
          "department now. A nurse has been notified and will contact you today.")
LABELS = {
    "nurse": "Nurse triage",
    "pharmacy": "Pharmacy refill queue",
    "front_desk": "Front desk scheduling",
    "urgent": "Urgent escalation",
}
VARIANTS = {
    "careful": {},
    "skip_chart": {"skip_chart": True},
    "skip_visits": {"skip_visits": True},
    "skip_allergies": {"skip_allergies": True},
    "no_verify": {"no_verify": True},
    "no_disclosure": {"no_disclosure": True},
}
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _today(text):
    found = re.search(r"Today: \w{3} (\w{3}) (\d{1,2}), (\d{4})", text)
    if not found:
        return None
    month, day, year = found.groups()
    return datetime.date(int(year), _MONTHS[month], int(day))


def decide_routes(body, last_visit, today):
    text = body.lower()
    routes, kinds = set(), []
    if (any(w in text for w in ("chest pain", "chest tightness", "trouble breathing", "shortness of breath", "faint"))
            or re.search(r"chest[^.]{0,20}tight|tight[^.]{0,20}chest", text)):
        routes.add("urgent")
        kinds.append("urgent")
    if any(w in text for w in ("result", " lab", "tsh", "a1c")):
        routes.add("nurse")
        kinds.append("labs")
    if any(w in text for w in ("amoxicillin", "antibiotic", "call some in", "ok for me to start", "should i start")):
        routes.add("nurse")
        kinds.append("new_med")
    if any(w in text for w in ("refill", "send more")):
        if last_visit and today and (today - last_visit).days > 365:
            routes.add("nurse")
            kinds.append("refill_overdue")
        else:
            routes.add("pharmacy")
            kinds.append("refill")
    if any(w in text for w in ("appointment", "reschedul", "move it", "book", "check-up", "record", "visit summary")):
        routes.add("front_desk")
        kinds.append("front_desk")
    return sorted(routes), kinds


def compose_reply(kinds, disclosure=True):
    pieces = []
    if "urgent" in kinds:
        pieces.append(URGENT)
    if "labs" in kinds:
        pieces.append("Thank you for your message. A nurse will call you within 2 business days to go over your results.")
    if "new_med" in kinds:
        pieces.append("Thank you for your message. A nurse will contact you about your symptoms.")
    if "refill_overdue" in kinds:
        pieces.append("It has been more than a year since your last visit, so a nurse will contact you to schedule "
                      "a visit before this refill.")
    if "refill" in kinds:
        pieces.append("We have sent your refill request to our pharmacy team.")
    if "front_desk" in kinds:
        pieces.append("Our front desk will contact you to help with your request.")
    if disclosure:
        pieces.append(DISCLOSURE)
    return " ".join(pieces)


class RuleAgent:
    name = "scripted"

    def __init__(self, variant="careful"):
        if variant not in VARIANTS:
            raise ValueError(f"unknown scripted variant {variant!r}; choose from {sorted(VARIANTS)}")
        self.variant = variant
        self.flags = VARIANTS[variant]
        self.handled = set()
        self.cur = None

    def describe(self):
        return {"kind": "scripted", "variant": self.variant}

    def decide(self, task, obs, history, feedback, step, max_steps):
        decision = self._next(obs)
        return decision, json.dumps(decision), {}, None

    # helpers -----------------------------------------------------------
    @staticmethod
    def _click(el, thought):
        return {"thought": thought, "action": "click", "id": el["id"]}

    def _stuck(self, why):
        return {"thought": why, "action": "done", "summary": f"Scripted agent stopped: {why}"}

    def _next(self, obs):
        url = obs["url"]
        if self.cur is None:
            if not url.startswith("/inbox"):
                link = find(obs, "inbox")
                return self._click(link, "Go back to the inbox.") if link else self._stuck("no inbox link")
            for el in obs["elements"]:
                if el["label"].startswith("Open message: "):
                    subject = el["label"][len("Open message: "):]
                    if subject not in self.handled:
                        self.cur = {"subject": subject, "phase": "read", "tabs_left": None, "retries": 0}
                        return self._click(el, f"Open the message '{subject}'.")
            return {"thought": "Every message is handled.", "action": "done",
                    "summary": f"Handled {len(self.handled)} message(s)."}
        return getattr(self, "_phase_" + self.cur["phase"])(obs)

    @staticmethod
    def _message_body(text):
        """The patient's words only: the lines between the 'From ...' line and 'Open chart'."""
        lines = text.splitlines()
        start = next((i for i, ln in enumerate(lines) if ln.startswith("From ")), None)
        end = next((i for i, ln in enumerate(lines) if ln.strip() == "Open chart"), None)
        if start is None or end is None or end <= start:
            return text
        return "\n".join(lines[start + 1:end])

    def _phase_read(self, obs):
        cur = self.cur
        cur["body"] = self.cur["subject"] + "\n" + self._message_body(obs["text"])
        cur["today"] = _today(obs["text"])
        if self.flags.get("skip_chart"):
            cur["phase"] = "decide"
            return self._phase_decide(obs)
        cur["phase"] = "chart"
        link = find(obs, "open chart")
        return self._click(link, "Open the patient's chart.") if link else self._stuck("no chart link")

    def _phase_chart(self, obs):
        cur = self.cur
        if cur["tabs_left"] is None:
            tabs = ["Medications", "Allergies", "Visits"]
            if self.flags.get("skip_allergies"):
                tabs.remove("Allergies")
            if self.flags.get("skip_visits"):
                tabs.remove("Visits")
            cur["tabs_left"] = tabs
        if "tab=visits" in obs["url"]:
            dates = [datetime.date.fromisoformat(d) for d in re.findall(r"\b(20\d\d-\d\d-\d\d)\b", obs["text"])]
            cur["last_visit"] = max(dates) if dates else None
        if cur["tabs_left"]:
            tab = cur["tabs_left"].pop(0)
            el = find(obs, tab, kind="a", exact=True)
            return self._click(el, f"Check the {tab} tab.") if el else self._stuck(f"no {tab} tab")
        cur["phase"] = "decide"
        back = find(obs, "back to message")
        return self._click(back, "Go back to the message.") if back else self._stuck("no way back to the message")

    def _phase_decide(self, obs):
        cur = self.cur
        cur["routes"], cur["kinds"] = decide_routes(cur["body"], cur.get("last_visit"), cur["today"])
        cur["reply"] = compose_reply(cur["kinds"], disclosure=not self.flags.get("no_disclosure"))
        cur["phase"] = "reply" if self.flags.get("no_verify") else "route"
        return getattr(self, "_phase_" + cur["phase"])(obs)

    def _phase_route(self, obs):
        cur = self.cur
        for key, label in LABELS.items():
            box = find(obs, label, kind="checkbox", exact=True)
            if box is None:
                return self._stuck(f"no checkbox {label}")
            if box["checked"] != (key in cur["routes"]):
                return self._click(box, f"Toggle {label}.")
        save = find(obs, "save routing")
        cur["phase"] = "inbox_done" if self.flags.get("no_verify") else "reopen"
        return self._click(save, "Save the routing.") if save else self._stuck("no save routing button")

    def _phase_reopen(self, obs):
        cur = self.cur
        link = find(obs, "Open message: " + cur["subject"], exact=True)
        if link is None:
            return self._stuck("message link not found in inbox")
        cur["phase"] = "verify"
        return self._click(link, "Reopen the message to check the routing.")

    def _phase_verify(self, obs):
        cur = self.cur
        line = next((ln for ln in obs["text"].splitlines() if ln.startswith("Current routing:")), "")
        shown = {key for key, label in LABELS.items() if label in line}
        if shown == set(cur["routes"]):
            cur["phase"] = "reply"
            return self._phase_reply(obs)
        cur["retries"] += 1
        if cur["retries"] > 2:
            return self._stuck("routing would not save")
        cur["phase"] = "route"
        return self._phase_route(obs)

    def _phase_reply(self, obs):
        cur = self.cur
        box = find(obs, "reply draft", kind="textarea")
        if box is None:
            return self._stuck("no reply box")
        cur["phase"] = "save_reply"
        return {"thought": "Write the reply.", "action": "fill", "id": box["id"], "text": cur["reply"]}

    def _phase_save_reply(self, obs):
        cur = self.cur
        save = find(obs, "save draft")
        cur["phase"] = "route" if self.flags.get("no_verify") else "done_msg"
        return self._click(save, "Save the reply.") if save else self._stuck("no save draft button")

    def _phase_done_msg(self, obs):
        cur = self.cur
        button = find(obs, "mark done", kind="button", exact=True)
        if button is None:
            return self._stuck("no mark done button")
        self.handled.add(cur["subject"])
        self.cur = None
        return self._click(button, "Mark the message done.")

    def _phase_inbox_done(self, obs):
        cur = self.cur
        button = find(obs, "Mark done: " + cur["subject"], exact=True)
        if button is None:
            return self._stuck("no inbox mark done button")
        self.handled.add(cur["subject"])
        self.cur = None
        return self._click(button, "Mark it done from the inbox.")
