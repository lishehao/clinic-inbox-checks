"""Ask a language model to write checks from the same instructions the agent got.

The model sees the policy, the task, the inbox listing, the event schema and
the check language. It does not see any runs or the hand-written checks.
"""

import datetime
import json
from pathlib import Path

from cic.agent.llm import extract_json
from cic.checks.dsl import validate
from cic.sim.data import inbox_summary, load_policy, load_seed, load_task

LANGUAGE = """Every check is a JSON object with "id" (short, unique), "desc" (one sentence), "kind", and the fields for its kind.

Kinds:
- {"kind": "exists", "event": PATTERN}                       passes if some event matches PATTERN
- {"kind": "never", "event": PATTERN}                        passes if no event matches PATTERN
- {"kind": "before", "first": PATTERN, "then": PATTERN}      passes if every event matching "then" comes after at least one event matching "first" (passes if no "then" event happened)
- {"kind": "followed_by", "anchor": PATTERN, "then": PATTERN} passes if every event matching "anchor" is followed later by an event matching "then"
- {"kind": "final", "path": PATH, "op": OP, "value": VALUE}  tests the stored state at the end of the run
- {"kind": "each", "over": [{"$m": "MSG01", "$p": "P01"}, ...], "check": CHECK}  CHECK must pass for every binding; "$m"/"$p" are replaced inside CHECK

PATTERN is an object. Exact-match keys: event, page, tab, patient, message, med, persisted.
Special keys: dest_includes (a dest value), dest_equals (a list), text_contains_any (list of strings),
text_contains_all (list), text_excludes_any (list), text_ends_with (string). Text matching ignores case,
curly quotes and extra spaces.

OP for final: equals, includes, not_includes, is_not_empty, contains_any, contains_all, excludes_any, ends_with.
Lists compare without regard to order for equals.

Format example (not a real requirement): {"id": "example", "desc": "the inbox was viewed before anything was marked done", "kind": "before", "first": {"event": "view", "page": "inbox"}, "then": {"event": "mark_done"}}"""

EVENTS = """- {"event": "view", "page": "inbox"}
- {"event": "view", "page": "message", "message": "MSG..", "patient": "P.."}
- {"event": "view", "page": "chart", "patient": "P..", "tab": "summary" | "meds" | "allergies" | "visits" | "problems"}
- {"event": "route", "message": "MSG..", "patient": "P..", "dest": [...], "persisted": true | false}
    dest values: "nurse" (Nurse triage), "pharmacy" (Pharmacy refill queue), "front_desk" (Front desk scheduling), "urgent" (Urgent escalation).
    persisted is false when the screen confirmed the save but the routing was not stored.
- {"event": "save_draft", "message": "MSG..", "patient": "P..", "text": "..."}
- {"event": "mark_done", "message": "MSG..", "patient": "P.."}
- {"event": "edit_med", "patient": "P..", "med": "..", "old_dose": "..", "new_dose": ".."}
Events are in the order they happened. Opening a chart tab is a separate view event per tab."""

STATE = """- messages.<message id>.routes   list of dest values stored for the message
- messages.<message id>.draft    text of the saved reply ("" if none)
- messages.<message id>.status   "unread", "read" or "done"
- patients.<patient id>.meds     list of medications"""

PROMPT = """You are writing automatic checks for runs of an AI agent that works in a clinic's patient-message inbox.
For each run we keep (1) the server's event log and (2) the stored state at the end of the run.
Write checks that tell whether a run followed the clinic policy and the task. Write them only from the
policy and the task below; you will not see any runs.

=== CLINIC POLICY ===
{policy}

=== TASK GIVEN TO THE AGENT ===
{instruction}

Inbox for this task (message id, patient, subject):
{inbox}

=== WHAT THE AGENT CAN SEE IN THE APP: THE MESSAGES AND THE PATIENTS' CHARTS ===
{materials}

=== EVENTS THE SERVER LOGS ===
{events}

=== STORED STATE PATHS ===
{state}

=== CHECK LANGUAGE ===
{language}

Cover every requirement in the policy that applies to these messages, including the correct routing and reply for each one.
Use the exact message and patient ids above. Return a JSON array of checks and nothing else."""


PROMPT_VERSION = 2  # version 1 showed only each message's sender and subject, not its text or the chart


def task_materials(seed, task):
    """The message texts and charts the agent can open: the same data the hand-written checks were written from."""
    blocks, patients = [], []
    for mid in task["inbox"]:
        msg = seed["messages"][mid]
        patient = seed["patients"][msg["patient"]]
        blocks.append(f'{mid} from {patient["name"]} (patient {msg["patient"]}), received {msg["received"]}, '
                      f'subject "{msg["subject"]}":\n{msg["body"]}')
        if msg["patient"] not in patients:
            patients.append(msg["patient"])
    for pid in patients:
        patient = seed["patients"][pid]
        meds = "; ".join(f'{m["id"]} {m["name"]} {m["dose"]}, {m["sig"]}, '
                         f'{"active" if m.get("active") else "not active"}, last fill {m.get("last_fill", "unknown")}'
                         for m in patient.get("meds", [])) or "none"
        visits = "; ".join(f'{v["date"]} {v["type"]}' for v in patient.get("visits", [])) or "none"
        blocks.append(f'Chart {pid} ({patient["name"]}, born {patient["dob"]}): '
                      f'problems: {", ".join(patient.get("problems", [])) or "none"}. '
                      f'allergies: {", ".join(patient.get("allergies", [])) or "none recorded"}. '
                      f'medications: {meds}. visits: {visits}.')
    return "\n\n".join(blocks)


def build_prompt(task_id):
    seed = load_seed()
    task = load_task(task_id)
    return PROMPT.format(
        policy=load_policy(),
        instruction=task["instruction"],
        inbox=inbox_summary(seed, task),
        materials=task_materials(seed, task),
        events=EVENTS,
        state=STATE,
        language=LANGUAGE,
    )


def safe_name(model):
    return "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in model)


def generate(client, task_id, out_dir, sample=1):
    prompt = build_prompt(task_id)
    raw, usage = client.complete([{"role": "user", "content": prompt}], max_tokens=16000)
    valid, invalid, parse_error = [], [], None
    try:
        checks = extract_json(raw, list)
    except ValueError as error:
        checks, parse_error = [], str(error)
    seen = set()
    for index, check in enumerate(checks):
        problems = validate(check, f"check[{index}]")
        if not problems and isinstance(check, dict):
            cid = str(check.get("id") or f"m{index}")
            while cid in seen:
                cid += "_"
            seen.add(cid)
            check["id"] = cid
            valid.append(check)
        else:
            invalid.append({"check": check, "problems": problems})
    record = {
        "task_id": task_id,
        "model": client.model,
        "prompt_version": PROMPT_VERSION,
        "sample": sample,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "parse_error": parse_error,
        "checks": valid,
        "invalid": invalid,
        "usage": usage,
        "raw": raw,
    }
    out = Path(out_dir) / safe_name(client.model)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{task_id}_s{sample}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    return path, record


def load_model_checks(directory, sample=1):
    """Map task id to the list of valid model-written checks."""
    found = {}
    directory = Path(directory)
    if not directory.exists():
        return found
    for path in sorted(directory.glob(f"T*_s{sample}.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        found[record["task_id"]] = record
    return found
