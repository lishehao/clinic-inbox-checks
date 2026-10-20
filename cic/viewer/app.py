"""Run viewer: a local web page for reviewing runs and recording the demo video."""

import json
from pathlib import Path

from flask import Flask, abort, render_template, send_from_directory

from cic.checks.report import build_report, evaluate_run, load_runs, step_of, to_markdown
from cic.sim.data import load_policy, load_task

HERE = Path(__file__).resolve().parent


def create_viewer(runs_dir, model_checks=None, include_scripted=True):
    runs_dir = Path(runs_dir)
    app = Flask(__name__, template_folder=str(HERE / "templates"), static_folder=str(HERE / "static"))

    def find_run(run_id):
        path = runs_dir / run_id / "run.json"
        if not path.exists() or "/" in run_id or ".." in run_id:
            abort(404)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["_dir"] = str(path.parent)
        return record

    @app.get("/")
    def index():
        rows = []
        for record in reversed(load_runs(runs_dir, include_scripted)):
            ev = evaluate_run(record, model_checks)
            rows.append({"record": record, "ev": ev,
                         "failed": [r for r in ev["process"]["results"] if not r["passed"]]})
        return render_template("runs.html", rows=rows, has_model=bool(model_checks))

    @app.get("/run/<run_id>")
    def run(run_id):
        record = find_run(run_id)
        task = load_task(record["task_id"])
        ev = evaluate_run(record, model_checks)
        annotations = {}
        for group, results in (("outcome", ev["outcome"]["results"]), ("instruction", ev["process"]["results"]),
                               ("model", (ev["model"] or {}).get("results", []))):
            for r in results:
                r["step"] = step_of(record, r["violation_seq"])
                if not r["passed"] and r["step"]:
                    annotations.setdefault(r["step"], []).append((group, r))
        events_by_seq = {e["seq"]: e for e in record["events"]}
        for step in record["steps"]:
            start, end = step["events"]
            step["event_list"] = [events_by_seq[s] for s in range(start, end + 1) if s in events_by_seq]
        return render_template("run.html", record=record, task=task, ev=ev, annotations=annotations)

    @app.get("/report")
    def report():
        rep = build_report(runs_dir, model_checks, include_scripted=False)
        source = next(iter(model_checks.values()))["model"] if model_checks else None
        return render_template("report.html", rep=rep, markdown=to_markdown(rep, source), source=source)

    @app.get("/policy")
    def policy():
        return render_template("policy.html", policy=load_policy())

    @app.get("/shots/<run_id>/<name>")
    def shot(run_id, name):
        record = find_run(run_id)
        return send_from_directory(Path(record["_dir"]) / "shots", name)

    return app
