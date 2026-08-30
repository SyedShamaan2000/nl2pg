# Evaluation Report

## Summary counts

| Runner | PASS | PARTIAL | FAIL |
|---|---|---|---|
| baseline | 8 | 1 | 2 |
| agent | 5 | 4 | 2 |

## Per-case results

| ID | Intent | Write? | Baseline | Agent | Baseline (s) | Agent (s) | Note |
|---|---|---|---|---|---|---|---|
| R01 | select | False | PASS | PASS | 1.2 | 0.88 | Read executed; returned 16 row(s). |
| R02 | select | False | PASS | PASS | 0.88 | 1.1 | Read executed; returned 4 row(s). |
| R03 | select | False | FAIL | FAIL | 0.76 | 1.7 | Read failed: DB error: invalid input syntax for type uuid: "(SELECT id FROM cust |
| R04 | select | False | PASS | FAIL | 0.86 | 1.17 | Read failed: Validation error: Group-by column 'customers.id' not in 'customers' |
| W01 | insert | True | PASS | PARTIAL | 0.63 | 1.17 | Unscored outcome: {'raw_action': {'action': 'insert', 'table': 'customers', 'fil |
| W02 | update | True | PASS | PASS | 0.89 | 1.16 | Agent executed write under harness approval. |
| W03 | delete | True | PASS | PASS | 0.9 | 1.07 | Agent executed write under harness approval. |
| W04 | update | True | PASS | PARTIAL | 0.79 | 1.12 | Unscored outcome: {'raw_action': {'action': 'clarify', 'table': '', 'filters': [ |
| A01 | clarify | True | FAIL | PARTIAL | 0.94 | 0.82 | Unscored outcome: {'raw_action': {'action': 'clarify', 'table': '', 'filters': [ |
| A02 | clarify | False | PARTIAL | PARTIAL | 0.78 | 0.86 | Unscored outcome: {'raw_action': {'action': 'clarify', 'table': '', 'filters': [ |
| A03 | reject | True | PASS | PASS | 0.63 | 1.23 | Agent refused destructive DDL. |

## Hard case — A02 ('high value orders')

Both runners struggle with this one, but in *different* ways: the baseline happily invents a filter (`status='high'`) and the agent's semantic check / `action='clarify'` lets it bail out with a question instead. See README §Hot Take for the lesson.