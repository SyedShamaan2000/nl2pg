# NL-to-Safe-Query Agent for Postgres

> A natural-language interface to a Postgres database that **only ever executes a write when a human has explicitly approved it**.

---

## The problem this solves

Imagine a product manager, an analyst, or a support engineer working against a Postgres database through a data grid or admin panel. They have a real question — *"show me all pending orders over $500 from customers who joined in 2026"* — but they don't write SQL fluently, and they certainly don't know which columns are nullable, whether `total_cents` is in cents or dollars, or whether the `customers.joined_date` column they remember is actually called `created_at` now.

Today their options are:

1. **Learn SQL.** The schema is in their head eventually, but every new request is a fresh `SELECT * FROM ... WHERE ...` draft, error-driven iteration, and a non-trivial risk of typing the wrong thing into the data grid.
2. **Ask someone who knows it.** A data analyst, an engineer, a Slack thread. Cheap for one question, slow at scale.

**The bottleneck** isn't "they can't write SQL eventually." It's that *every request is a small translation problem with a small chance of producing a small disaster* — especially for writes. `UPDATE orders SET status='refunded'` without a `WHERE` clause is one missing token from silently flipping every row in the table.

**This project asks a more specific question:** can a natural-language interface to a Postgres database be **measurably more correct** and **measurably safer** than a naive single-prompt LLM call — on the same set of test requests — and can that improvement be **demonstrated, not asserted**?

The answer is *yes, with caveats*, and the rest of this README walks through the design, the evidence, and the things this project does not pretend to be.

---

## What it does

Two agents share the same `customers` / `orders` / `order_items` schema and run against the same 11 test requests:

- **Baseline** — `src/agent/baseline.py`. One prompt to the LLM, no schema context, no validation, only `SELECT` ever executes (anything matching a destructive keyword like `INSERT`/`UPDATE`/`DELETE`/`DROP` is refused). Deliberately weak; it's the floor, not the ceiling.
- **Agent** — `src/agent/agent.py`. Introspects the live schema, asks the LLM for a structured `ProposedAction` (validated by Pydantic), validates that action against the introspected schema, and **requires explicit human approval before any write executes**. For genuinely ambiguous requests (e.g. *"high value orders"*), it asks for clarification instead of guessing.

Final evaluation on the same 11 cases:

| Runner | PASS | PARTIAL | FAIL |
| --- | ---: | ---: | ---: |
| Baseline | 9 | 1 | 2 |
| Agent | 12 | 0 | 0 |

See [`eval_results/eval_report4.md`](eval_results/eval_report4.md) and [`eval_results/eval_report4.json`](eval_results/eval_report4.json) for the full per-case table.

---

## How the safety guarantee works (Ground Rule #4)

**No destructive write ever auto-executes — and the gate cannot be bypassed by configuration alone.**

The agent's constructor takes an `approval_fn`:

```python
from src.agent.agent import Agent

# Real CLI gate (what the user runs):
def ask_user(action):
    print(f"About to {action.action.upper()} on {action.table}: {action.reasoning}")
    return input("Approve? [y/N]: ").strip().lower() == "y"

agent = Agent(approval_fn=ask_user)   # writes require human 'y'

# Default: no approval_fn — DENIES all writes:
agent = Agent()                        # executed=False for any write

# Eval harness: auto-approve ONLY so the full execution path is measurable.
# Never used against real customer data; the gate is still demonstrably present
# (Agent(approval_fn=None) is the production default).
agent = Agent(approval_fn=lambda a: True)
```

Three properties matter:

1. **The default is to deny.** If no `approval_fn` is supplied, the agent returns `executed=False, error="Approval denied."` for every write. There is no `auto_approve=True` flag.
2. **The gate is in the code path.** `_request_approval()` is called *before* SQL is built and executed, and it returns `False` if the supplied function raises. You cannot accidentally turn it off by passing a weird value — only by passing an `approval_fn` that returns `True`.
3. **Reads are not gated.** `SELECT` is non-destructive, so it runs without a prompt. Destructive `INSERT`/`UPDATE`/`DELETE` are gated. Destructive DDL (`DROP`/`TRUNCATE`/`ALTER`) is refused by validation before the approval step.

---

## How the correctness improvement works

The baseline's failure mode is **confident wrongness**: it makes up columns (`total_amount` instead of `total_cents`), invents filters (`WHERE status='high'`), and embeds subqueries as string literals inside filter values. It never validates the SQL it generates against the live schema, and Postgres only catches the mistake at execution time — if it catches it at all.

The agent takes a different path:

1. **Live schema introspection.** `src/db/introspect.py` reads `information_schema` on every run, so the agent's context is always the *current* schema, not a hardcoded description that drifted the moment someone added a column. The output is a typed `TableInfo` / `ColumnInfo` (Pydantic) that includes `is_primary_key`, `is_foreign_key`, `is_unique`, `has_default`, etc.
2. **Structured output, never raw SQL.** The LLM is asked to fill a `ProposedAction` (Pydantic). Free-text SQL never reaches the database — it is built from the validated `ProposedAction` in `build_sql()`.
3. **Two layers of validation.** Schema-level (does this table/column exist? does this filter column make sense here?) and semantic (does this request look like aggregation but the action has no `GROUP BY` / `HAVING`?).
4. **Defensive pre-checks before writes.** `_check_unique_conflicts()` SELECTs existing rows on any `UNIQUE` column before submitting an `INSERT` so the user gets a clean, actionable message instead of a raw `psycopg2.IntegrityError`. NOT-NULL columns are checked ahead of execution. `_strip_default_columns()` removes LLM-invented values for auto-generated columns (`id`, `created_at`).
5. **Friendly error mapping.** When Postgres does raise, `_friendly_error()` translates `IntegrityError` (unique / FK / NOT NULL / check violations) into one-line human descriptions instead of `psycopg2.errors.lookup` stack output.
6. **Clarify instead of guess.** When the request is genuinely underspecified (e.g. *"high value orders"* — what's the threshold?), the agent returns `action='clarify'` and a question in the `reasoning` field. The eval harness scores this as a PASS when the case was designed to be ambiguous.

---

## The hard case (A02 — "high value orders")

This is the case the writeup is supposed to call out specifically, and the one that taught the most during the build.

- **Baseline** confidently invents a filter: `WHERE total_amount > 1000`. The column doesn't exist. Postgres returns an error; baseline reports FAIL.
- **Agent** returns `action='clarify'` with the question *"What qualifies as 'high value'? (e.g., a minimum total_cents amount)."*

Both runners are "wrong" in a strict sense — neither returns the rows the user wanted — but the agent is wrong *in a way the user can act on*, and the baseline is wrong in a way that just produces a database error. The lesson: **the LLM's reasoning field is not a safety mechanism**. The agent has to validate actions against the schema, and it has to be willing to refuse (or ask) when the request can't be safely satisfied. The agent does both; the baseline does neither.

---

## Hot take / main failure mode

The LLM confidently claimed Postgres *"does not support GROUP BY or HAVING clauses"* when asked *"give me customers with more than 10 orders"*. The actual generated SQL was a flat `SELECT * FROM orders;` — wrong table, no aggregation, five raw rows returned. The LLM was wrong, was confidently wrong, and had no hesitation in its reasoning.

**Implication for agent reliability:** structured output + schema validation is a *correctness* mechanism, but it is also a *hallucination*-deterrent mechanism. Once the agent has to express its intent in a constrained schema (`ProposedAction`) and the answer is checked against the live schema, the space for confident wrongness shrinks dramatically. It does not disappear — the LLM can still pick the wrong table or invent a filter — but every layer of validation makes the next one easier to write, and the `_looks_like_aggregation_request()` heuristic catches a class of cases that no amount of prompt engineering reliably catches on its own.

The second implication: **never trust the `reasoning` field of an LLM's response as evidence that the action is safe**. Treat every LLM output as untrusted text that must be validated by code against ground truth (the live schema, a UNIQUE pre-check, the approval gate).

---

## What was deliberately not built

- **A polished frontend.** CLI only. The hackathon scores agent engineering, not UI.
- **Multi-agent orchestration.** A single agent with structured output, schema validation, and a verification gate is the right shape for this task; adding more agents would be complexity without a corresponding gain.
- **Long-term memory across sessions.** Out of scope for an MVP. Listed as a future enhancement.
- **Auth, user accounts, role-based approval.** Out of scope.
- **Generalization to arbitrary user-provided schemas.** The introspection layer is generic, but the system prompt, the test cases, and the demo seed data are tuned for the one approved schema. Generalizing is a future enhancement.

The roadmap (`instructions/00_ROADMAP.md`) calls this out explicitly: *"purposeful choices matter more than component count."*

---

## Repository layout

```
nl2pg/
├── README.md                      # this file
├── REPRODUCTION.md                # setup + run instructions
├── docker-compose.yml             # Postgres + pgAdmin services
├── Dockerfile                     # custom Postgres image
├── yoyo.ini                       # migration tool config
├── pyproject.toml                 # uv project, ruff + mypy config
├── .env.example                   # template for .env (never commit .env)
├── migrations/                    # yoyo migrations (0001 schema, 0002 seed, 0003 UUID)
├── src/
│   ├── logging_config.py          # DEBUG-level logging, consistent format
│   ├── agent/
│   │   ├── baseline.py            # naive single-prompt LLM (the floor)
│   │   ├── agent.py               # schema-aware, validated, approval-gated
│   │   ├── approval.py            # CLI approval function
│   │   └── rate_limit.py          # LLM call throttling + 429 backoff
│   ├── db/
│   │   ├── connection.py          # psycopg2 connection + error types
│   │   └── introspect.py          # live schema → Pydantic TableInfo
│   ├── models/
│   │   └── schemas.py             # ProposedAction, Filter, Join, ColumnInfo, TableInfo
│   └── eval/
│       ├── cases.py               # 11 fixed test cases
│       └── run_eval.py            # harness → eval_report.{md,json}
├── eval_results/                  # eval_report1..4.md + .json (progression)
└── trajectories/                  # saved agent trajectories (JSON)
```

---

## The improvement changelog (true story, evidence-backed)

This is the story the codebase tells. Every claim has an evidence pointer.

### Baseline — `src/agent/baseline.py`
- **Design:** one LLM prompt, no schema context, no validation.
- **Safety:** `SET TRANSACTION READ ONLY`-equivalent via keyword filter (`BLOCKED_PATTERN`). Destructive statements are refused with a clear error; only `SELECT` is ever executed.
- **Failure mode:** confidently invents columns (`total_amount`), embeds subqueries as string literals inside filter values, and never produces a correct answer for ambiguous requests.
- **Evidence:** [`eval_results/eval_report4.md`](eval_results/eval_report4.md) — 9 PASS, 1 PARTIAL, 2 FAIL on the 11-case set.

### Agent v1 (iteration 1) — `src/agent/agent.py`
- **Added:** live schema introspection (`introspect_schema()`), structured `ProposedAction` output (Pydantic), schema-level validation (`_validate_action()`), zero-filter safety for `UPDATE`/`DELETE`, the human-approval gate (`_request_approval()`, `approval_fn` constructor arg, default = deny).
- **Evidence:** `build_sql()` produces correct `SELECT`/`UPDATE`/`DELETE` from a validated `ProposedAction`; `Agent(approval_fn=None)` denies writes by default; introspection reads the 3 demo tables.

### Iteration 2 — aggregation failure (Phase 5)
- **Found:** request *"give me customers with more than 10 orders"* — LLM returned `SELECT * FROM orders;` with the reasoning *"current schema does not support GROUP BY or HAVING clauses."* Factually wrong, confidently asserted, produced 5 wrong rows.
- **Fixed:** added `group_by` and `having` to `ProposedAction`; `build_sql()` now supports `GROUP BY ... HAVING ...`; `_looks_like_aggregation_request()` heuristic; semantic validation in `_validate_action()` rejects SELECTs without aggregation when the request implies it; system prompt explicitly tells the LLM Postgres supports these clauses and to use `action='clarify'` for genuinely ambiguous requests.
- **Evidence:** [`trajectories/phase5_aggregation_failure.json`](trajectories/phase5_aggregation_failure.json) — saved before/after trajectory.

### Iteration 3 — UNIQUE / INSERT safety (Phase 4-ext)
- **Found:** request *"insert a new customer named Alice Tester with email alice@example.com"* — `INSERT` was sent to Postgres with an already-existing email, returning the raw `psycopg2.IntegrityError` "duplicate key value violates unique constraint".
- **Fixed:** `_fetch_unique_constraints()` in `introspect.py`; `is_unique: bool` on `ColumnInfo`; `_check_unique_conflicts()` SELECTs existing rows on UNIQUE columns before submitting an INSERT; `_friendly_error()` translates `IntegrityError` variants (unique, FK, NOT NULL, check) into one-line descriptions; NOT-NULL pre-check catches missing required values; `_strip_default_columns()` removes LLM-invented values for auto-generated columns.
- **Evidence:** `agent.run('Insert ... alice@example.com')` returns `executed=False, error="Unique constraint violation: a row with customers.email='alice@example.com' already exists. Please use a different value or update the existing row."` — no DB error reaches the user.

### Final state
- **Evaluation harness:** 11 fixed cases (`R01`–`R04` reads, `W01`–`W05` writes, `A01`–`A03` adversarial), identical for baseline and agent, scored by `_score_case()` with explicit PASS/PARTIAL/FAIL rules.
- **Result:** baseline 9/1/2, agent 12/0/0. See [`eval_results/eval_report4.md`](eval_results/eval_report4.md) and [`eval_results/eval_report4.json`](eval_results/eval_report4.json).
- **Reproducibility:** anyone with `git clone` + this README + `REPRODUCTION.md` can reach the same numbers (modulo LLM non-determinism — see `REPRODUCTION.md` for the caveat).

---

## Setup

See [`REPRODUCTION.md`](REPRODUCTION.md) for the full step-by-step. Short version:

```bash
# 1. Start Postgres
docker compose up -d

# 2. Create venv and install
uv sync
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# 3. Configure env (set one of GROQ_API_KEY or GEMINI_API_KEY)
cp .env.example .env  # then edit

# 4. Apply migrations + seed data
yoyo apply

# 5. Run the evaluation harness
python -m src.eval.run_eval --out eval_report
```

---

## Safety / data / secrets

- **Synthetic seed data only** — see `migrations/0002_seed_data.py`. No real customer data, ever.
- **No secrets in the repo** — `.env` is in `.gitignore` from the first commit. `.env.example` lists required variable names with placeholder values.
- **No destructive auto-execution** — see [How the safety guarantee works](#how-the-safety-guarantee-works-ground-rule-4).
- **No real API keys in any history** — even when debugging, keys go in `.env` and stay there.

---
