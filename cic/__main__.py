"""Command line: python -m cic <command>

  run         run tasks with the model agent or a scripted test agent
  gen-checks  have the model write checks from the instructions
  report      print the two measurements
  view        open the run viewer at http://127.0.0.1:8765
  ping        send one tiny request to check the model endpoint
  login       sign in with ChatGPT to run on your ChatGPT plan (on the computer with the browser)
  models      list the models your ChatGPT sign-in can use
  logout      revoke the ChatGPT sign-in and remove the saved tokens
  doctor      check this computer: Python packages, Chromium, the simulated inbox, the model
"""

import argparse
import json
import sys
from pathlib import Path

from cic.config import RUNS_DIR, model_name, model_settings


def _tasks(arg):
    from cic.sim.data import all_task_ids
    return all_task_ids() if arg in ("all", "", None) else [t.strip() for t in arg.split(",") if t.strip()]


def _client():
    s = model_settings()
    if s["backend"] == "chatgpt":
        from cic.agent import siwc
        from cic.agent.responses import ResponsesClient
        model = s["chatgpt_model"]
        if not model:
            try:
                listed = siwc.list_models(siwc.access_token())
            except (siwc.SignInError, siwc.OAuthError, *siwc.NETWORK_ERRORS) as error:
                raise SystemExit(f"Could not choose a model: {error}")
            if not listed:
                raise SystemExit("Your ChatGPT sign-in lists no models for this route.")
            model = listed[0]["slug"]
            print(f"CIC_CHATGPT_MODEL is not set; using the first listed model, {model}. "
                  f"Set it in .env to keep runs comparable.", file=sys.stderr)
        return ResponsesClient(model)
    if s["backend"] != "api":
        raise SystemExit(f"Unknown CIC_BACKEND {s['backend']!r}: use api or chatgpt.")
    from cic.agent.llm import ChatClient, LLMError
    key = s["api_key"]
    if not key:
        from cic.config import default_key_file, read_key_file
        key_file = s["api_key_file"] or default_key_file(s["base_url"])
        if key_file:
            try:
                key = read_key_file(key_file)
            except ValueError as error:
                raise SystemExit(str(error))
    try:
        return ChatClient(s["base_url"], key, s["model"], s["temperature"], s["json_mode"])
    except LLMError as error:
        raise SystemExit(str(error))


def _model_checks(arg):
    from cic.checks.model_checks import load_model_checks, safe_name
    if arg == "none":
        return None
    if arg:
        return load_model_checks(arg) or None
    root = RUNS_DIR / "model_checks"
    if not root.exists():
        return None
    preferred = root / safe_name(model_name() or "none")
    if preferred.exists():
        return load_model_checks(preferred) or None
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    return load_model_checks(dirs[0]) if dirs else None


def cmd_run(args):
    from cic.agent.runner import HarnessError, check_browser, completed_counts, run_task
    from cic.checks.report import evaluate_run
    problem = check_browser()
    if problem:
        raise SystemExit(f"{problem}\nNothing was run. Check everything with: python -m cic doctor")
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
    from cic.checks.report import label_of_agent
    label = label_of_agent(make_agent().describe())
    done = completed_counts(args.out, label, args.tag) if args.resume else {}
    for task_id in _tasks(args.tasks):
        already = done.get(task_id, 0)
        todo = max(0, args.repeats - already)
        if args.resume:
            print(f"{task_id}: {already} finished run(s) with {label} already; running {todo} more", flush=True)
        for i in range(todo):
            try:
                out = run_task(task_id, make_agent(), args.out, tag=args.tag, headed=args.headed,
                               slowmo=args.slowmo, max_steps=args.max_steps)
            except HarnessError as error:
                raise SystemExit(f"{error}\nStopped before {task_id} started; finished runs are kept. "
                                 f"Check everything with: python -m cic doctor")
            record = json.loads((out / "run.json").read_text(encoding="utf-8"))
            if record.get("fatal_error"):
                print(f"{task_id} #{already + i + 1}: stopped by an error after {len(record['steps'])} steps -> "
                      f"{out.name}", flush=True)
                print(f"  error: {record['fatal_error']}", file=sys.stderr)
                print("Stopped the batch. Finished runs are kept, and this run is left out of the measurements. "
                      "Fix the cause, then run the same command with --resume.", file=sys.stderr)
                raise SystemExit(2)
            ev = evaluate_run(record)
            failed = [r["id"] for r in ev["process"]["results"] if not r["passed"]]
            print(f"{task_id} #{already + i + 1}: {len(record['steps'])} steps, ended by {record['ended_by']}, "
                  f"{'accepted' if ev['accepted'] else 'NOT accepted'}, "
                  f"{len(failed)} instruction check(s) failed {failed if failed else ''} -> {out.name}", flush=True)


def cmd_gen_checks(args):
    from cic.checks.model_checks import generate
    from cic.agent.llm import LLMError
    client = _client()
    for task_id in _tasks(args.tasks):
        for sample in range(1, args.samples + 1):
            try:
                path, record = generate(client, task_id, args.out, sample)
            except LLMError as error:
                raise SystemExit(f"Stopped at {task_id} sample {sample}: {error}")
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
    from cic.agent.llm import LLMError
    client = _client()
    try:
        reply, usage = client.complete([{"role": "user", "content": "Reply with the single word OK."}],
                                       max_tokens=400)
    except LLMError as error:
        raise SystemExit(f"Ping failed: {error}")
    backend = model_settings()["backend"]
    base = getattr(client, "base_url", None) or getattr(client, "api_base", "")
    print(f"backend={backend} model={client.model} endpoint={base} reply={reply.strip()[:40]!r} usage={usage}")


def cmd_doctor(args):
    import platform
    ok = True

    def line(passed, text):
        nonlocal ok
        ok = ok and passed
        print(("[ok]   " if passed else "[FAIL] ") + text, flush=True)

    print(f"Python {platform.python_version()} at {sys.executable}")
    missing = []
    for module in ("flask", "playwright"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    line(not missing, "flask and playwright installed" if not missing else
         f"missing: {', '.join(missing)}. Run: pip install -r requirements.txt")
    if not missing:
        from cic.agent.runner import check_browser, check_simulator
        problem = check_browser()
        line(problem is None, "Chromium starts" if problem is None else problem)
        if problem is None:
            problem = check_simulator()
            line(problem is None, "the simulated inbox opens in Chromium" if problem is None else problem)
    s = model_settings()
    print(f"Model: backend={s['backend']} model={model_name(s) or '(chosen at run time)'}", flush=True)
    try:
        client = _client()
        reply, usage = client.complete([{"role": "user", "content": "Reply with the single word OK."}],
                                       max_tokens=400)
        line(True, f"the model answered {reply.strip()[:20]!r}")
    except SystemExit as error:
        line(False, str(error))
    except Exception as error:  # report any model failure as a failed check
        line(False, f"model request failed: {error}")
    print("All checks passed." if ok else "Fix the lines marked FAIL, then run python -m cic doctor again.")
    raise SystemExit(0 if ok else 1)


def cmd_login(args):
    from cic.agent import siwc
    try:
        record = siwc.login(port=args.port, consent=args.consent, timeout=args.timeout)
    except siwc.SignInError as error:
        raise SystemExit(str(error))
    print("Signed in with ChatGPT.")
    print(f"'Use your ChatGPT plan' permission: {'granted' if record['plan_usage'] else 'NOT granted'}")
    if not record["plan_usage"]:
        print("To grant it, run: python -m cic login --consent. Or use an API key instead (see .env.example).")
    print(f"Credentials saved in {siwc.credentials_path()} (only your user can read the file; tokens are never "
          f"printed or written to runs).")
    print("Next: python -m cic models, then put CIC_BACKEND=chatgpt and CIC_CHATGPT_MODEL=<slug> in .env.")


def cmd_models(args):
    from cic.agent import siwc
    try:
        listed = siwc.list_models(siwc.access_token())
    except siwc.SignInError as error:
        raise SystemExit(str(error))
    except (siwc.OAuthError, *siwc.NETWORK_ERRORS) as error:
        raise SystemExit(f"Could not list models: {error}")
    if not listed:
        print("No models listed for this sign-in.")
    for item in listed:
        print(f"{item['slug']}\t{item['display_name']}")


def cmd_logout(args):
    from cic.agent import siwc
    confirmed = siwc.logout(forget_client=args.forget_client)
    if confirmed is None:
        print("No ChatGPT sign-in tokens are saved on this computer.")
        return
    print("Removed the saved ChatGPT tokens.")
    if not confirmed:
        print("OpenAI did not confirm the revocation. You can also disconnect the app in ChatGPT settings "
              "(Security and login > Sign in with ChatGPT).")


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
    run.add_argument("--resume", action="store_true",
                     help="count finished runs already in --out for this model and tag, and run only the rest")
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

    login = sub.add_parser("login", help="sign in with ChatGPT (run on the computer with the browser)")
    login.add_argument("--port", type=int, default=1455, help="loopback port for the sign-in answer")
    login.add_argument("--consent", action="store_true", help="ask for the plan permission again")
    login.add_argument("--timeout", type=int, default=300, help="seconds to wait for the browser")
    login.set_defaults(func=cmd_login)

    models = sub.add_parser("models", help="list models for your ChatGPT sign-in")
    models.set_defaults(func=cmd_models)

    doctor = sub.add_parser("doctor", help="check packages, Chromium, the simulated inbox and the model")
    doctor.set_defaults(func=cmd_doctor)

    logout = sub.add_parser("logout", help="revoke the ChatGPT sign-in and remove saved tokens")
    logout.add_argument("--forget-client", action="store_true", help="also forget the registered client id")
    logout.set_defaults(func=cmd_logout)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
