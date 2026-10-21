"""Run one task with one agent and record everything needed to check it later."""

import datetime
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from werkzeug.serving import make_server

from cic.agent.observe import observe
from cic.sim.app import create_app
from cic.sim.data import build_state, load_seed, load_task

logging.getLogger("werkzeug").setLevel(logging.ERROR)


class HarnessError(Exception):
    """The browser or the simulated inbox could not start: not the agent's fault, and not a run."""


def _setup_problem(error):
    text = str(error).strip()
    first = text.splitlines()[0][:300] if text else type(error).__name__
    if "Executable doesn't exist" in text or "playwright install" in text:
        return f"Chromium is not installed for this Python ({first}). Run: python -m playwright install chromium"
    return f"Chromium or the simulated inbox could not start: {first}"


def check_browser():
    """Start Chromium once. Returns None if it works, else what is wrong and how to fix it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Playwright is not installed in this Python. Run: pip install -r requirements.txt"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<p>ok</p>")
            browser.close()
    except Exception as error:  # Playwright raises its own error types
        return _setup_problem(error)
    return None


def check_simulator():
    """Open the simulated inbox in Chromium. Returns None if it loads, else the problem."""
    from playwright.sync_api import sync_playwright
    state = build_state(load_seed(), load_task("T01"))
    server = make_server("127.0.0.1", 0, create_app(state, secrets.token_hex(8)), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/inbox", timeout=20000)
            heading = page.inner_text("h1")
            browser.close()
    except Exception as error:
        return _setup_problem(error)
    finally:
        server.shutdown()
    return None if heading.strip() == "Inbox" else f"The inbox page loaded, but its heading was {heading!r}."


def step_budget(task):
    """Room for a careful agent plus some fumbling: 20 steps plus 15 per message."""
    return 20 + 15 * len(task["inbox"])


def completed_counts(runs_dir, label, tag=""):
    """Finished runs per task for one agent label and tag. Runs cut short by a model error do not count."""
    from collections import Counter

    from cic.checks.report import label_of_agent
    counts = Counter()
    for path in Path(runs_dir).glob("*/run.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("ended_by") == "error" or record.get("tag", "") != tag:
            continue
        if label_of_agent(record.get("agent") or {}) != label:
            continue
        counts[record["task_id"]] += 1
    return counts


def _execute(page, decision):
    """Carry out one action. Returns an error string or None."""
    action = decision["action"]
    target = page.locator(f'[data-cic-id="{decision.get("id", "")}"]').first
    try:
        if action == "click":
            target.click(timeout=5000)
        elif action == "fill":
            target.fill(str(decision.get("text", "")), timeout=5000)
        elif action == "select":
            target.select_option(label=str(decision.get("option", "")), timeout=5000)
    except Exception as error:  # Playwright raises its own error types
        return f"{action} on {decision.get('id')} failed: {str(error).splitlines()[0][:200]}"
    try:
        page.wait_for_timeout(120)
        page.wait_for_load_state("load", timeout=10000)
    except Exception:
        pass
    return None


def _label_of(obs, eid):
    for el in obs["elements"]:
        if el["id"] == eid:
            return el["label"]
    return ""


def _history_line(n, decision, label, url_after, error):
    if decision is None:
        return f"{n}. (no valid action) -> {error}"
    action = decision["action"]
    line = f"{n}. {action}"
    if action != "done":
        line += f' {decision.get("id")} "{label}"'
    if action == "fill":
        text = str(decision.get("text", ""))
        line += f' text="{text[:70]}{"..." if len(text) > 70 else ""}"'
    if action == "select":
        line += f' option="{decision.get("option", "")}"'
    line += f" -> now at {url_after}"
    if error:
        line += f" (FAILED: {error})"
    return line


def run_task(task_id, agent, out_root, tag="", headed=False, slowmo=0, max_steps=None):
    from playwright.sync_api import sync_playwright

    seed = load_seed()
    task = load_task(task_id)
    max_steps = max_steps or step_budget(task)
    state = build_state(seed, task)
    token = secrets.token_hex(16)
    app = create_app(state, token)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    agent_label = agent.describe().get("model") or agent.describe().get("variant") or agent.name
    safe_label = "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in str(agent_label))
    run_id = f"{stamp}_{task_id}_{safe_label}_{secrets.token_hex(2)}"
    out = Path(out_root) / run_id  # created once the browser has the inbox open

    steps, history = [], []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    summary, ended_by, fatal, fatal_info = "", "max_steps", None, None
    started = time.time()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=not headed, slow_mo=slowmo)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(base + "/inbox", timeout=20000)
                page.wait_for_load_state("load")
                obs = observe(page)
            except Exception as error:  # nothing has happened yet: no run folder, no record
                raise HarnessError(_setup_problem(error)) from None
            (out / "shots").mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out / "shots" / "s000.jpg"), type="jpeg", quality=72, full_page=True)
            feedback = ""
            for n in range(1, max_steps + 1):
                events_before = len(state["events"])
                try:
                    decision, raw, usage, error = agent.decide(task, obs, history, feedback, n, max_steps)
                except Exception as exc:  # a model/API failure ends the run but keeps the record
                    fatal = f"{type(exc).__name__}: {str(exc)[:600]}"
                    fatal_info = {k: getattr(exc, k) for k in ("status", "code", "request_id", "param")
                                  if getattr(exc, k, None) is not None} or None
                    ended_by = "error"
                    break
                for key in usage_total:
                    usage_total[key] += int(usage.get(key, 0) or 0)
                label = _label_of(obs, decision.get("id")) if decision else ""
                url_before = obs["url"]
                if decision is not None and decision["action"] != "done":
                    error = _execute(page, decision)
                new_obs = observe(page) if (decision is None or decision["action"] != "done") else obs
                if decision is None or decision["action"] != "done":
                    page.screenshot(path=str(out / "shots" / f"s{n:03d}.jpg"), type="jpeg", quality=72, full_page=True)
                steps.append({
                    "n": n,
                    "url_before": url_before,
                    "url_after": new_obs["url"],
                    "decision": decision,
                    "target_label": label,
                    "error": error,
                    "raw": raw if isinstance(raw, str) else json.dumps(raw),
                    "events": [events_before + 1, len(state["events"])],
                    "obs_before": {"url": obs["url"], "text": obs["text"][:3000],
                                   "elements": obs["elements"]},
                    "shot_before": f"shots/s{n - 1:03d}.jpg" if n > 1 else "shots/s000.jpg",
                    "shot_after": f"shots/s{n:03d}.jpg" if (decision is None or decision["action"] != "done") else None,
                })
                history.append(_history_line(n, decision, label, new_obs["url"], error))
                feedback = error or ""
                if decision is not None and decision["action"] == "done":
                    summary = str(decision.get("summary", ""))
                    ended_by = "done"
                    break
                obs = new_obs
            browser.close()
    finally:
        server.shutdown()

    # Steps after a done action have no "after" screenshot; fix shot_before chains.
    last_shot = "shots/s000.jpg"
    for step in steps:
        step["shot_before"] = last_shot
        if step["shot_after"]:
            last_shot = step["shot_after"]

    record = {
        "run_id": run_id,
        "task_id": task_id,
        "task_title": task["title"],
        "tag": tag,
        "agent": agent.describe(),
        "started": datetime.datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "seconds": round(time.time() - started, 1),
        "ended_by": ended_by,
        "fatal_error": fatal,
        "fatal_info": fatal_info,
        "summary": summary,
        "usage": usage_total,
        "max_steps": max_steps,
        "steps": steps,
        "events": state["events"],
        "final_state": {"messages": state["messages"], "patients": state["patients"]},
        "faults_left": state["faults"],
    }
    tmp = out / "run.json.tmp"
    tmp.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out / "run.json")  # a run.json is either complete or absent
    return out
