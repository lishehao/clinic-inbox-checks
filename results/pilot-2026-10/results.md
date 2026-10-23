# Results

Runs counted: 60

| Set | Runs | Accepted | Accepted but failed an instruction check | Model checks agree with hand verdict |
|---|---|---|---|---|
| base | 40 | 40 | 1 | 34 of 40 |
| hard | 20 | 20 | 0 | 20 of 20 |

## Measurement 1
Of the runs whose final result would be accepted, how many fail at least one instruction check written by hand?

**1 of 60** accepted runs failed at least one instruction check.

| Task | Agent | Runs | Accepted | Accepted but failed an instruction check | Most frequent failures |
|---|---|---|---|---|---|
| T01 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T02 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T03 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T04 | claude-haiku-5-5 | 5 | 5 | 1 | R4_nurse_will_call (1) |
| T05 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T06 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T07 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T08 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T09 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T10 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T11 | claude-haiku-5-5 | 5 | 5 | 0 | none |
| T12 | claude-haiku-5-5 | 5 | 5 | 0 | none |

## Measurement 2
When a model writes the checks from the same instructions, on which runs does its verdict disagree with the hand-written verdict?

Model-written checks: claude-sonnet-5-5
Agreement on accepted runs: 54 of 60.
Agreement on all runs: 54 of 60.

- 20261020-214438_T06_claude-haiku-5-5_3a6a (T06, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: Every saved reply event ends with the AI-assistance sentence.
- 20261020-214622_T06_claude-haiku-5-5_8d18 (T06, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: Every saved reply event ends with the AI-assistance sentence.
- 20261020-214810_T06_claude-haiku-5-5_4637 (T06, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: Every saved reply event ends with the AI-assistance sentence.
- 20261020-214946_T06_claude-haiku-5-5_5d7b (T06, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: Every saved reply event ends with the AI-assistance sentence.
- 20261020-215122_T06_claude-haiku-5-5_33bb (T06, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: Every saved reply event ends with the AI-assistance sentence.
- 20261020-215251_T04_claude-haiku-5-5_5a5f (T04, claude-haiku-5-5): hand fail, model pass
  - hand-written checks failed: Rule 4: reply says a nurse will call

Tokens: 6242449 prompt, 467064 completion.
