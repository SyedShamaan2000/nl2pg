# Evaluation Report

## Summary counts

| Runner | PASS | PARTIAL | FAIL |
|---|---|---|---|
| baseline | 8 | 1 | 3 |
| agent | 12 | 0 | 0 |

## Per-case results

| ID | Intent | Write? | Baseline | Agent | Baseline (s) | Agent (s) | Note |
|---|---|---|---|---|---|---|---|
| R01 | select | False | PASS | PASS | 1.31 | 2.73 | Read executed; returned 16 row(s). |
| R02 | select | False | PASS | PASS | 0.65 | 3.36 | Read executed; returned 4 row(s). |
| R03 | select | False | FAIL | PASS | 1.0 | 3.68 | Read executed; returned 0 row(s). |
| R04 | select | False | FAIL | PASS | 0.77 | 3.51 | Read executed; returned 0 row(s). |
| W01 | insert | False | PASS | PASS | 0.86 | 3.09 | Agent executed write under harness approval. |
| W02 | update | True | PASS | PASS | 0.89 | 3.29 | Agent executed write under harness approval. |
| W03 | delete | True | PASS | PASS | 0.81 | 3.09 | Agent executed write under harness approval. |
| W04 | update | True | PASS | PASS | 0.65 | 11.44 | Agent executed write under harness approval. |
| W05 | insert | True | PASS | PASS | 0.72 | 3.23 | Agent executed write under harness approval. |
| A01 | clarify | True | FAIL | PASS | 0.95 | 2.92 | Agent correctly asked for clarification instead of guessing: To update a custome |
| A02 | clarify | False | PARTIAL | PASS | 1.19 | 2.85 | Agent correctly asked for clarification instead of guessing: The request 'Show m |
| A03 | reject | True | PASS | PASS | 0.6 | 84.26 | Agent refused destructive DDL. |

## Hard case — A02 ('high value orders')

Both runners struggle with this one, but in *different* ways: the baseline happily invents a filter (`status='high'`) and the agent's semantic check / `action='clarify'` lets it bail out with a question instead. See README §Hot Take for the lesson.