# Pilot runs, October 2026

The 60 runs behind the numbers in the top-level README: claude-haiku-5-5 as the agent, 5 runs of each of the 12 tasks (harness version 2), recorded on 20 October 2026. The model-written checks are claude-sonnet-5-5's, one sample per task.

| File | What it is |
|---|---|
| `runs/<run id>/run.json` | One run: every step's page text and controls as the agent received them, the agent's note and action, the server's event log and the stored state at the end |
| `results.md`, `results.json` | The two measurements, as written by `python -m cic report` |
| `flag-audit.md` | The six flagged runs, checked against their logs |
| `model_checks/claude-sonnet-5-5/` | The model-written checks used in `results.md` (check prompt version 2: subject, message text and chart) |
| `model_checks-v1-subject-only/`, `results-v1-subject-only.*` | The first version (subject line only), compared on the 40 base runs |

The screenshots each run saved for the viewer are not included, to keep the repository small. The agent never saw them: it reads the page's text and a numbered list of its controls, and all of that is in `run.json`.

Reproduce the report, or browse the runs:

```bash
python -m cic report --runs results/pilot-2026-10/runs --model-checks results/pilot-2026-10/model_checks/claude-sonnet-5-5
python -m cic view --runs results/pilot-2026-10/runs --model-checks results/pilot-2026-10/model_checks/claude-sonnet-5-5
```
