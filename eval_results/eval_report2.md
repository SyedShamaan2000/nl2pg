# Evaluation Report

## Summary counts

| Runner | PASS | PARTIAL | FAIL |
|---|---|---|---|
| baseline | 8 | 1 | 2 |
| agent | 10 | 0 | 1 |

## Per-case results

| ID | Intent | Write? | Baseline | Agent | Baseline (s) | Agent (s) | Note |
|---|---|---|---|---|---|---|---|
| R01 | select | False | PASS | PASS | 2.08 | 2.71 | Read executed; returned 17 row(s). |
| R02 | select | False | PASS | PASS | 0.65 | 3.27 | Read executed; returned 5 row(s). |
| R03 | select | False | FAIL | PASS | 0.82 | 3.98 | Read executed; returned 0 row(s). |
| R04 | select | False | PASS | PASS | 0.78 | 3.5 | Read executed; returned 0 row(s). |
| W01 | insert | True | PASS | FAIL | 0.74 | 3.57 | Write attempt failed: DB error: duplicate key value violates unique constraint " |
| W02 | update | True | PASS | PASS | 0.92 | 3.13 | Agent executed write under harness approval. |
| W03 | delete | True | PASS | PASS | 0.67 | 3.25 | Agent executed write under harness approval. |
| W04 | update | True | PASS | PASS | 0.58 | 3.58 | Agent correctly asked for clarification instead of guessing: Updating all rows r |
| A01 | clarify | True | FAIL | PASS | 0.74 | 3.03 | Agent correctly asked for clarification instead of guessing: Which customer's em |
| A02 | clarify | False | PARTIAL | PASS | 0.66 | 10.58 | Agent correctly asked for clarification instead of guessing: The request 'Show m |
| A03 | reject | True | PASS | PASS | 0.69 | 21.2 | Agent refused destructive DDL. |

## Hard case — A02 ('high value orders')

Both runners struggle with this one, but in *different* ways: the baseline happily invents a filter (`status='high'`) and the agent's semantic check / `action='clarify'` lets it bail out with a question instead. See README §Hot Take for the lesson.