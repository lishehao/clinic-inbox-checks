"""Load the synthetic seed data and build a fresh state for one run."""

import copy
import json

from cic.config import TASKS_DIR

DESTINATIONS = [
    ("nurse", "Nurse triage"),
    ("pharmacy", "Pharmacy refill queue"),
    ("front_desk", "Front desk scheduling"),
    ("urgent", "Urgent escalation"),
]
DEST_LABELS = dict(DESTINATIONS)

CHART_TABS = [
    ("summary", "Summary"),
    ("meds", "Medications"),
    ("allergies", "Allergies"),
    ("visits", "Visits"),
    ("problems", "Problems"),
]


def load_seed():
    """The base seed, plus the hard set's patients and messages from seed_hard.json."""
    seed = json.loads((TASKS_DIR / "seed.json").read_text(encoding="utf-8"))
    extra = TASKS_DIR / "seed_hard.json"
    if extra.exists():
        more = json.loads(extra.read_text(encoding="utf-8"))
        for key in ("patients", "messages"):
            clash = set(seed[key]) & set(more.get(key, {}))
            if clash:
                raise ValueError(f"seed_hard.json reuses {key} ids already in seed.json: {sorted(clash)}")
            seed[key].update(more.get(key, {}))
    return seed


def load_policy():
    return (TASKS_DIR / "policy.md").read_text(encoding="utf-8").strip()


def load_task(task_id):
    matches = sorted(TASKS_DIR.glob(f"{task_id}_*.json"))
    if not matches:
        raise FileNotFoundError(f"No task file for {task_id} in {TASKS_DIR}")
    return json.loads(matches[0].read_text(encoding="utf-8"))


def all_task_ids():
    return sorted(p.name.split("_", 1)[0] for p in TASKS_DIR.glob("T*_*.json"))


def build_state(seed, task):
    """A fresh, isolated state holding only this task's inbox."""
    messages = {}
    for mid in task["inbox"]:
        msg = copy.deepcopy(seed["messages"][mid])
        msg.update({"id": mid, "status": "unread", "routes": [], "draft": ""})
        messages[mid] = msg
    patient_ids = {m["patient"] for m in messages.values()}
    return {
        "today": seed["today"],
        "clinic": seed["clinic"],
        "patients": {pid: copy.deepcopy(seed["patients"][pid]) for pid in sorted(patient_ids)},
        "messages": messages,
        "faults": copy.deepcopy(task.get("faults", [])),
        "events": [],
    }


def inbox_summary(seed, task):
    """Plain-text listing of the task's inbox, shared with the agent and the check writer."""
    lines = []
    for mid in task["inbox"]:
        msg = seed["messages"][mid]
        patient = seed["patients"][msg["patient"]]
        lines.append(
            f'{mid}: from {patient["name"]} (patient {msg["patient"]}), subject "{msg["subject"]}"'
        )
    return "\n".join(lines)
