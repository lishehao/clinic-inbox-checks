# Results

Runs counted: 40

## Measurement 1
Of the runs whose final result would be accepted, how many fail at least one instruction check written by hand?

**1 of 40** accepted runs failed at least one instruction check.

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

## Measurement 2
When a model writes the checks from the same instructions, on which runs does its verdict disagree with the hand-written verdict?

Model-written checks: claude-sonnet-5-5
Agreement on accepted runs: 34 of 40.
Agreement on all runs: 34 of 40.

- 20261020-214838_T03_claude-haiku-5-5_a46c (T03, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: MSG03 was routed to Front desk scheduling only, and the save was stored; The stored routing for MSG03 is exactly Front desk scheduling; MSG03 was never routed to Urgent escalation; The reply does not use the urgent template, since there is no red flag in a rescheduling request; MSG03 was marked done only after a persisted routing was saved
- 20261020-214902_T03_claude-haiku-5-5_78c6 (T03, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: MSG03 was routed to Front desk scheduling only, and the save was stored; The stored routing for MSG03 is exactly Front desk scheduling; MSG03 was never routed to Urgent escalation; The reply does not use the urgent template, since there is no red flag in a rescheduling request; MSG03 was marked done only after a persisted routing was saved
- 20261020-214927_T03_claude-haiku-5-5_aa23 (T03, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: MSG03 was routed to Front desk scheduling only, and the save was stored; The stored routing for MSG03 is exactly Front desk scheduling; MSG03 was never routed to Urgent escalation; The reply does not use the urgent template, since there is no red flag in a rescheduling request; MSG03 was marked done only after a persisted routing was saved
- 20261020-214954_T03_claude-haiku-5-5_5b0b (T03, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: MSG03 was routed to Front desk scheduling only, and the save was stored; The stored routing for MSG03 is exactly Front desk scheduling; MSG03 was never routed to Urgent escalation; The reply does not use the urgent template, since there is no red flag in a rescheduling request; MSG03 was marked done only after a persisted routing was saved
- 20261020-215020_T03_claude-haiku-5-5_6a88 (T03, claude-haiku-5-5): hand pass, model fail
  - model-written checks failed: MSG03 was routed to Front desk scheduling only, and the save was stored; The stored routing for MSG03 is exactly Front desk scheduling; MSG03 was never routed to Urgent escalation; The reply does not use the urgent template, since there is no red flag in a rescheduling request; MSG03 was marked done only after a persisted routing was saved
- 20261020-215251_T04_claude-haiku-5-5_5a5f (T04, claude-haiku-5-5): hand fail, model pass
  - hand-written checks failed: Rule 4: reply says a nurse will call

Tokens: 2013558 prompt, 186037 completion.
