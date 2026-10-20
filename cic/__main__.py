"""Command line: python -m cic <command>

  run         run tasks with the model agent or a scripted test agent
  gen-checks  have the model write checks from the instructions
  report      print the two measurements
  view        open the run viewer at http://127.0.0.1:8765
  ping        send one tiny request to check the model endpoint
"""

import argparse
import json
import sys
from pathlib import Path

from cic.config import RUNS_DIR, model_settings


def _tasks(arg):
    from cic.sim.data import all_task_ids
    return all_task_ids() if arg in ("all", "", None) else [t.strip() for t in arg.split(",") if t.strip()]


def _client(max_tokens_hint=None):
    from cic.agent.llm import ChatClient
    s = model_settings()
    return ChatClient(s["base_url"], s["api_key"], s["model"], s["temperature"], s["json_mode"])


def _model_checks(arg):
    from cic.checks.model_checks import load_model_checks, safe_name
    if arg == "none":
        return None
    if arg:
        return load_model_checks(arg) or None
    root = RUNS_DIR / "model_checks"
    if not root.exists():
        return None
    preferred = root / safe_name(model_settings()["model"])
    if preferred.exists():
        return load_model_checks(preferred) or None
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    return load_model_checks(dirs[0]) if dirs else None


def cmd_run(args):
    from cic.agent.runner import run_task
    if args.agent.startswith("scripted"):
        from cic.agent.scripted import RuleAgent
        variant = args.agent.split(":", 1)[1] if ":" in args.agent else "careful"
        make_agent = lambda: RuleAgent(variant)  # noqa: E731
    else:
        from cic.agent.policy import LLMPolicy
        from cic.sim.data import load_policy
        client = _client()
        policy_text = load_policy()
        make_agent = lambda: LLMPolicy(client, policy_text)  # noqa: E731
    for task_id in _tasks(args.tasks):
        for i in range(args.repeats):
            out = run_task(task_id, make_agent(), args.out, tag=args.tag, headed=args.headed, slowmo=args.slowmo,
                           max_steps=args.max_steps)
            record = json.loads((out / "run.json").read_text(encoding="utf-8"))
            from cic.checks.report import evaluate_run
            ev = evaluate_run(record)
            failed = [r["id"] for r in ev["process"]["results"] if not r["passed"]]
            print(f"{task_id} #{i + 1}: {len(record['steps'])} steps, ended by {record['ended_by']}, "
                  f"{'accepted' if ev['accepted'] else 'NOT accepted'}, "
                  f"{len(failed)} instruction check(s) failed {failed if failed else ''} -> {out.name}", flush=True)
            if record.get("fatal_error"):
                print(f"  error: {record['fatal_error']}", file=sys.stderr)


def cmd_gen_checks(args):
    from cic.checks.model_checks import generate
    client = _client()
    for task_id in _tasks(args.tasks):
        for sample in range(1, args.samples + 1):
            path, record = generate(client, task_id, args.out, sample)
            print(f"{task_id} sample {sample}: {len(record['checks'])} valid, {len(record['invalid'])} invalid"
                  f"{' (parse error: ' + record['parse_error'] + ')' if record['parse_error'] else ''} -> {path}")


def cmd_report(args):
    from cic.checks.report import build_report, to_markdown
    checks = _model_checks(args.model_checks)
    report = build_report(args.runs, checks, include_scripted=args.include_scripted)
    source = next(iter(checks.values()))["model"] if checks else None
    text = to_markdown(report, source)
    print(text)
    if args.save:
        Path(args.save).write_text(text, encoding="utf-8")
        Path(args.save).with_suffix(".json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")


def cmd_view(args):
    from cic.viewer.app import create_viewer
    app = create_viewer(args.runs, _model_checks(args.model_checks), include_scripted=not args.hide_scripted)
    print(f"Run viewer: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=args.port, debug=False)


def cmd_ping(args):
    client = _client()
    reply, usage = client.complete([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=20)
    print(f"model={client.model} base_url={client.base_url} reply={reply.strip()[:40]!r} usage={usage}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m cic", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run tasks")
    run.add_argument("--tasks", default="all", help="comma-separated ids like T01,T06, or all")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--agent", default="llm", help="llm, or scripted:<careful|skip_chart|skip_visits|skip_allergies|no_verify|no_disclosure>")
    run.add_argument("--out", default=str(RUNS_DIR))
    run.add_argument("--tag", default="")
    run.add_argument("--headed", action="store_true", help="show the browser (for recording)")
    run.add_argument("--slowmo", type=int, default=0, help="milliseconds between browser actions")
    run.add_argument("--max-steps", type=int, default=None)
    run.set_defaults(func=cmd_run)

    gen = sub.add_parser("gen-checks", help="have the model write checks")
    gen.add_argument("--tasks", default="all")
    gen.add_argument("--samples", type=int, default=1)
    gen.add_argument("--out", default=str(RUNS_DIR / "model_checks"))
    gen.set_defaults(func=cmd_gen_checks)

    rep = sub.add_parser("report", help="print the measurements")
    rep.add_argument("--runs", default=str(RUNS_DIR))
    rep.add_argument("--model-checks", default="", help="directory of model-written checks, or none")
    rep.add_argument("--include-scripted", action="store_true")
    rep.add_argument("--save", default="", help="write the report to this .md path (and a .json next to it)")
    rep.set_defaults(func=cmd_report)

    view = sub.add_parser("view", help="open the run viewer")
    view.add_argument("--runs", default=str(RUNS_DIR))
    view.add_argument("--model-checks", default="")
    view.add_argument("--port", type=int, default=8765)
    view.add_argument("--hide-scripted", action="store_true")
    view.set_defaults(func=cmd_view)

    ping = sub.add_parser("ping", help="check the model endpoint")
    ping.set_defaults(func=cmd_ping)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
