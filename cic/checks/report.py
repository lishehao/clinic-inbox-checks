"""The two measurements, computed from recorded runs.

Measurement 1. Of the runs whose final result would be accepted (every outcome
check passes), how many fail at least one instruction check written by hand?

Measurement 2. When a language model writes the checks from the same
instructions, on which runs does its verdict disagree with the hand-written
verdict (outcome and instruction checks together)?
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from cic.checks.dsl import run_checks
from cic.sim.data import load_task


def load_runs(runs_dir, include_scripted=False, include_errors=True):
    """Recorded runs. include_errors=False leaves out runs cut short by a model or API error."""
    runs = []
    for path in sorted(Path(runs_dir).glob("*/run.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"Skipping unreadable run record: {path}", file=sys.stderr)
            continue
        if record["agent"].get("kind") == "scripted" and not include_scripted:
            continue
        if record.get("ended_by") == "error" and not include_errors:
            continue
        record["_dir"] = str(path.parent)
        runs.append(record)
    return runs


def count_error_runs(runs_dir, include_scripted=False):
    total = len(load_runs(runs_dir, include_scripted, include_errors=True))
    return total - len(load_runs(runs_dir, include_scripted, include_errors=False))


def evaluate_run(record, model_checks=None):
    task = load_task(record["task_id"])
    events, state = record["events"], record["final_state"]
    outcome = run_checks(task["outcome_checks"], events, state)
    process = run_checks(task["process_checks"], events, state)
    result = {
        "accepted": outcome["passed"],
        "outcome": outcome,
        "process": process,
        "hand_verdict": outcome["passed"] and process["passed"],
        "model": None,
    }
    entry = (model_checks or {}).get(record["task_id"])
    if entry and entry.get("checks"):
        model = run_checks(entry["checks"], events, state)
        result["model"] = model
        result["model_source"] = entry["model"]
    return result


def step_of(record, seq):
    if seq is None:
        return None
    for step in record["steps"]:
        start, end = step["events"]
        if start <= seq <= end:
            return step["n"]
    return None


def label_of_agent(agent):
    """How runs are grouped: the model (plus any reasoning setting), or the scripted variant."""
    if agent.get("model"):
        effort = agent.get("reasoning_effort")
        return agent["model"] + (f" (reasoning {effort})" if effort else "")
    return f"scripted:{agent.get('variant')}"


def agent_label(record):
    return label_of_agent(record["agent"])


def build_report(runs_dir, model_checks=None, include_scripted=False):
    runs = load_runs(runs_dir, include_scripted, include_errors=False)
    left_out = count_error_runs(runs_dir, include_scripted)
    rows = []
    by_task = defaultdict(lambda: {"runs": 0, "accepted": 0, "accepted_failed": 0, "failed_checks": Counter()})
    disagreements = []
    agree = {"accepted": [0, 0], "all": [0, 0]}  # [agree, compared]
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    for record in runs:
        ev = evaluate_run(record, model_checks)
        failed_hand = [r for r in ev["process"]["results"] if not r["passed"]]
        label = agent_label(record)
        key = (record["task_id"], label)
        bucket = by_task[key]
        bucket["runs"] += 1
        if ev["accepted"]:
            bucket["accepted"] += 1
            if failed_hand:
                bucket["accepted_failed"] += 1
                for r in failed_hand:
                    bucket["failed_checks"][r["id"].split(":")[0]] += 1
        for k in tokens:
            tokens[k] += int(record.get("usage", {}).get(k, 0) or 0)
        model_verdict = ev["model"]["passed"] if ev["model"] else None
        if model_verdict is not None:
            same = model_verdict == ev["hand_verdict"]
            agree["all"][1] += 1
            agree["all"][0] += int(same)
            if ev["accepted"]:
                agree["accepted"][1] += 1
                agree["accepted"][0] += int(same)
            if not same:
                disagreements.append({
                    "run_id": record["run_id"],
                    "task_id": record["task_id"],
                    "agent": label,
                    "accepted": ev["accepted"],
                    "hand_verdict": ev["hand_verdict"],
                    "model_verdict": model_verdict,
                    "hand_failed": [r["desc"] for r in ev["outcome"]["results"] + ev["process"]["results"] if not r["passed"]],
                    "model_failed": [r["desc"] or r["id"] for r in ev["model"]["results"] if not r["passed"]],
                })
        rows.append({
            "run_id": record["run_id"],
            "task_id": record["task_id"],
            "agent": label,
            "steps": len(record["steps"]),
            "ended_by": record["ended_by"],
            "accepted": ev["accepted"],
            "hand_failed": [r["id"] for r in failed_hand],
            "model_verdict": model_verdict,
            "hand_verdict": ev["hand_verdict"],
        })
    accepted = sum(b["accepted"] for b in by_task.values())
    accepted_failed = sum(b["accepted_failed"] for b in by_task.values())
    tasks = []
    for (task_id, label), bucket in sorted(by_task.items()):
        tasks.append({
            "task_id": task_id,
            "agent": label,
            "runs": bucket["runs"],
            "accepted": bucket["accepted"],
            "accepted_failed": bucket["accepted_failed"],
            "top_failed": bucket["failed_checks"].most_common(5),
        })
    return {
        "runs": len(runs),
        "left_out_errors": left_out,
        "measurement_1": {"accepted": accepted, "accepted_failed": accepted_failed, "by_task": tasks},
        "measurement_2": {
            "compared_all": agree["all"][1], "agree_all": agree["all"][0],
            "compared_accepted": agree["accepted"][1], "agree_accepted": agree["accepted"][0],
            "disagreements": disagreements,
        },
        "tokens": tokens,
        "rows": rows,
    }


def to_markdown(report, model_source=None):
    m1, m2 = report["measurement_1"], report["measurement_2"]
    lines = ["# Results", "", f"Runs counted: {report['runs']}", ""]
    if report.get("left_out_errors"):
        lines += [f"Runs left out because the model service failed mid-run: {report['left_out_errors']}", ""]
    lines += ["## Measurement 1",
              "Of the runs whose final result would be accepted, how many fail at least one instruction check written by hand?", "",
              f"**{m1['accepted_failed']} of {m1['accepted']}** accepted runs failed at least one instruction check.", "",
              "| Task | Agent | Runs | Accepted | Accepted but failed an instruction check | Most frequent failures |",
              "|---|---|---|---|---|---|"]
    for t in m1["by_task"]:
        top = ", ".join(f"{name} ({count})" for name, count in t["top_failed"]) or "none"
        lines.append(f"| {t['task_id']} | {t['agent']} | {t['runs']} | {t['accepted']} | {t['accepted_failed']} | {top} |")
    lines += ["", "## Measurement 2",
              "When a model writes the checks from the same instructions, on which runs does its verdict disagree with the hand-written verdict?", ""]
    if m2["compared_all"]:
        if model_source:
            lines.append(f"Model-written checks: {model_source}")
        lines += [f"Agreement on accepted runs: {m2['agree_accepted']} of {m2['compared_accepted']}.",
                  f"Agreement on all runs: {m2['agree_all']} of {m2['compared_all']}.", ""]
        for d in m2["disagreements"]:
            lines.append(f"- {d['run_id']} ({d['task_id']}, {d['agent']}): hand {'pass' if d['hand_verdict'] else 'fail'}, "
                         f"model {'pass' if d['model_verdict'] else 'fail'}")
            if d["hand_failed"]:
                lines.append(f"  - hand-written checks failed: {'; '.join(d['hand_failed'][:6])}")
            if d["model_failed"]:
                lines.append(f"  - model-written checks failed: {'; '.join(d['model_failed'][:6])}")
    else:
        lines.append("No model-written checks loaded yet (run `python -m cic gen-checks`).")
    lines += ["", f"Tokens: {report['tokens']['prompt_tokens']} prompt, {report['tokens']['completion_tokens']} completion.", ""]
    return "\n".join(lines)
