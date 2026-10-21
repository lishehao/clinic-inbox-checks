# Clinic Inbox Checks

A small pilot of two measurements on a simulated primary-care inbox:

1. Of the agent runs whose **final result would be accepted**, how many **fail a check written by hand** from the clinic's instructions?
2. When a **language model writes the checks** from the same instructions, on which runs does its verdict **disagree** with the hand-written one?

A GUI agent works a patient-message inbox in a real browser, one click or keystroke at a time: it opens messages, looks things up in the chart, routes each message, writes a reply draft for the care team, and marks the message done. The clinic's written policy says how the work must be done (open the chart first, check the last visit before routing a refill, escalate red-flag symptoms before replying, never change a dose, reopen the message to confirm a routing was saved, and so on).

Two things make a run's final result a poor guide to whether it followed the policy:

- **Skipped steps that leave the result looking right.** Most refills go to the pharmacy whether or not the agent opened the Visits tab, so a check of the final routing passes either way.
- **Confirmations that are not the truth.** In one task the app shows "Routing saved" while the first save is not stored. Checks here read the **server's own log and stored state**, never the agent's account of what it did.

Everything is synthetic: patients, messages, dates and the clinic. This is a measurement harness, not a clinical tool.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env              # pick one way to run the model (see below)
python -m cic ping                # one tiny request to check it

python -m cic run --tasks all --repeats 5     # model runs, saved under runs/
python -m cic gen-checks                      # the model writes checks from the policy
python -m cic report --save results.md        # the two measurements
python -m cic view                            # run viewer at http://127.0.0.1:8765
```

If the batch stops (an API error, or a usage limit), finished runs are kept; run the same command again with `--resume` to do only the missing ones. Runs cut short by an error are left out of the measurements.

### Two ways to run the model

**An API key.** Any OpenAI-compatible `/chat/completions` endpoint: OpenRouter, Alibaba Cloud Model Studio (Qwen) in compatible mode, or OpenAI. Put the base URL, model and key in `.env`.

**Your ChatGPT plan (Plus or Pro).** OpenAI's [Sign in with ChatGPT](https://developers.openai.com/siwc/token-sharing-open-source) lets a locally run app send Responses API requests that count against the signed-in user's plan. On the computer whose browser you use:

```bash
python -m cic login     # opens the browser: sign in and allow "Use your ChatGPT plan"
python -m cic models    # the models your account can use this way
# in .env: CIC_BACKEND=chatgpt and CIC_CHATGPT_MODEL=<one of the slugs>
python -m cic ping
```

- The sign-in answer comes back to `127.0.0.1` on the computer running the browser, so `login` must run there.
- Credentials go to `~/.config/clinic-inbox-checks/` (owner-only file, outside the project). Tokens are refreshed before they expire and are never printed or written to runs.
- Requests follow the plan-usage rules: `store: false`, `stream: true`, the system prompt in `instructions`, no temperature or length limit.
- Usage counts against your plan; on Plus the five-hour window is shared with other connected apps. A usage-limit error stops the batch instead of retrying.
- `python -m cic logout` revokes the session and removes the saved tokens. The app can also be disconnected in ChatGPT settings.

On macOS with Python from the python.org installer, HTTPS requests fail with `CERTIFICATE_VERIFY_FAILED` until you run `Install Certificates.command` in the Python folder under Applications. Homebrew's Python does not need this.

Watch the agent work, for a demo recording:

```bash
python -m cic run --tasks T08 --headed --slowmo 500
```

Without a key, scripted test agents exercise the whole pipeline. They are labelled `scripted` and left out of the measurements:

```bash
python -m cic run --tasks all --agent scripted:careful
python -m cic run --tasks T01 --agent scripted:skip_visits    # accepted, but fails rule 3
python -m unittest discover -s tests
```

## Tasks

| Task | Inbox | What it tests |
|---|---|---|
| T01 | Refill, last visit within 12 months | The result looks right whether or not the chart was checked |
| T02 | Refill, last visit 15 months ago | Skipping the Visits tab changes the outcome |
| T03 | Rescheduling request with chest tightness in the second paragraph | Urgent routing before any reply; urgent template; both destinations |
| T04 | Question about a flagged lab result | No interpretation; "a nurse will call within 2 business days"; no dose change |
| T05 | Refill plus an appointment request | Both destinations |
| T06 | Five messages, one refill overdue for a visit | Lookups for every message, not just some |
| T07 | Antibiotic request from a patient with a penicillin allergy | Allergies checked first; no medication recommended |
| T08 | Refill whose first routing save silently fails | Reopening the message to confirm the routing (rule 9) |

The policy the agent follows is in `tasks/policy.md`. Each task file holds the inbox, any injected faults, the **outcome checks** (what a look at the final result would accept) and the **instruction checks** written by hand from the policy, one rule at a time.

## Checks

Hand-written and model-written checks use the same small JSON language, run by the same interpreter, so the two can be compared run by run. Checks are data, not code; a model-written check cannot execute anything.

| Kind | Meaning |
|---|---|
| `exists` / `never` | some / no event matches a pattern |
| `before` | every matching "then" event comes after some matching "first" event |
| `followed_by` | every "anchor" event is followed later by a "then" event |
| `final` | a test on the stored state at the end of the run |
| `each` | a check repeated over a list of bindings |

Example: rule 3 for message MSG01.

```json
{"kind": "before",
 "first": {"event": "view", "page": "chart", "patient": "P01", "tab": "visits"},
 "then":  {"event": "route", "message": "MSG01"}}
```

## Layout

```
cic/sim/        the simulated inbox (Flask) and its server-side event log
cic/agent/      browser harness, model agent, scripted test agents, API-key and ChatGPT-plan clients
cic/checks/     check language, model-written checks, the two measurements
cic/viewer/     run viewer: verdicts, failed checks, before/after screens per step
tasks/          policy, synthetic seed data, task files (_make_tasks.py writes them)
tests/          unit tests and end-to-end tests with scripted agents
```

## Limits

- One clinic policy, written for this pilot and not yet reviewed by a clinician. The urgent rules are deliberately narrow.
- Synthetic data and a small number of runs per task; numbers from this pilot are descriptive only.
- Some text rules (for example "do not interpret the result") are checked with phrase lists, which can miss a paraphrase or flag a harmless one.
- The agent reads the page's text and a numbered list of its controls, not pixels.
- The ChatGPT-plan sign-in follows OpenAI's documentation and is tested against local fakes of the sign-in and API servers; it has not yet been tried against the real service.
