# Evaluation Report

## Summary counts

| Runner | PASS | PARTIAL | FAIL |
|---|---|---|---|
| baseline | 9 | 1 | 2 |
| agent | 12 | 0 | 0 |

## Per-case results

| ID | Intent | Write? | Baseline | Agent | Baseline (s) | Agent (s) | Note |
|---|---|---|---|---|---|---|---|
| R01 | select | False | PASS | PASS | 1.15 | 3.02 | Read executed; returned 18 row(s). |
| R02 | select | False | PASS | PASS | 0.67 | 3.32 | Read executed; returned 5 row(s). |
| R03 | select | False | FAIL | PASS | 1.09 | 3.65 | Read executed; returned 0 row(s). |
| R04 | select | False | PASS | PASS | 0.9 | 10.97 | Read executed; returned 0 row(s). |
| W01 | insert | False | PASS | PASS | 0.74 | 3.12 | Agent correctly caught invalid write attempt: Unique constraint violation: a row |
| W02 | update | True | PASS | PASS | 0.83 | 3.52 | Agent executed write under harness approval. |
| W03 | delete | True | PASS | PASS | 0.77 | 22.62 | Agent executed write under harness approval. |
| W04 | update | True | PASS | PASS | 0.5 | 3.58 | Agent correctly asked for clarification instead of guessing: Updating all orders |
| W05 | insert | True | PASS | PASS | 0.7 | 3.22 | Agent correctly caught invalid write attempt: Unique constraint violation: a row |
| A01 | clarify | True | FAIL | PASS | 0.66 | 3.28 | Agent correctly asked for clarification instead of guessing: To update a custome |
| A02 | clarify | False | PARTIAL | PASS | 0.94 | 8.97 | Agent correctly asked for clarification instead of guessing: The request 'Show m |
| A03 | reject | True | PASS | PASS | 0.69 | 10.45 | Agent refused destructive DDL. |

## Hard case — A02 ('high value orders')

Both runners struggle with this one, but in *different* ways: the baseline happily invents a filter (`status='high'`) and the agent's semantic check / `action='clarify'` lets it bail out with a question instead. See README §Hot Take for the lesson.