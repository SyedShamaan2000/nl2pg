# Reproduction Guide — NL-to-Safe-Query Agent for Postgres

This document is written for the hackathon evaluator (and for anyone who wants to reproduce the comparison from a fresh clone). Every claim in `README.md` has an evidence pointer to one of the files listed below.

---

## Prerequisites

- **Docker** + **Docker Compose** (to spin up Postgres; `docker-compose.yml` defines the service with named volume `pgdata`)
- **Python 3.11+** (project requires `>=3.11`; developed with **3.14.5**)
- **uv 0.12.5+** (used for dependency management; `pyproject.toml` + `uv.lock` present)
- **`.env` file** with one valid LLM API key (`GROQ_API_KEY` for Groq / `GEMINI_API_KEY` for Gemini; the agent defaults to Groq via `LLM_PROVIDER=groq` in `agent.py`; the baseline also supports Groq default — see `BASELINE_LLM_PROVIDER` env var)
- **Network access** for LLM API calls (the harness makes ~2 calls per case; 11 cases = ~22 LLM calls; with the default `LLM_MIN_REQUEST_INTERVAL_SECONDS=3.0` and rate-limit backoff, expect 3–12 minutes total depending on provider)

Note: this repo uses `uv` and `.venv`. If you prefer `pip`, the `pyproject.toml` describes the dependencies directly — but the reproduction commands below assume `uv`.

---

## 1. Fresh clone setup (exact commands)

```bash
git clone https://github.com/SyedShamaan2000/nl2pg nl2pg
cd nl2pg

# Confirm environment (optional sanity check)
python3 --version   # expect 3.11+; this was built with 3.14.5
uv --version        # expect 0.12.5+
```

---

## 2. Start Postgres (`docker-compose.yml`)

```bash
docker compose up -d
# or: docker-compose up -d (if your docker-compose plugin uses the legacy binary)
```

Expected output: `postgres_db` and `pgadmin` containers running; port `5432` exposed; `pgdata` volume created. Confirm with `docker compose ps`.

The `docker-compose.yml` uses an external named network (`shared_nl2pg_net`) — if that doesn't exist on your host, either create it (`docker network create shared_nl2pg_net`) or edit the compose file to remove the `external: true` line temporarily.

---

## 3. Configure environment (`.env` from `.env.example`)

```bash
cp .env.example .env
```

Edit `.env` with your API key. The project expects one of these two (both are optional — pick one):

```bash
# Option A (default agent provider):
GROQ_API_KEY=your-groq-key-here

# Option B (used if LLM_PROVIDER=gemini or if Groq is unavailable):
GEMINI_API_KEY=your-gemini-key-here

# Optional: rate-limit tuning
LLM_MIN_REQUEST_INTERVAL_SECONDS=3.0
EVAL_CASE_DELAY_SECONDS=12
```

**Important:** `.env` must never be committed. It is already in `.gitignore` (check with `git status` — `.env` should not appear). If it does, do not commit it.

---

## 4. Create virtual environment and install dependencies

```bash
uv sync
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

If you prefer `pip`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The `pyproject.toml` declares everything needed: `pydantic`, `psycopg2-binary`, `python-dotenv`, `yoyo-migrations`, `langchain-groq`, `langchain-google-genai`. `ruff` and `mypy` are in the `dev` extra.

---

## 5. Apply migrations and seed data (`yoyo.ini`)

```bash
yoyo apply
```

This runs in order:

- `migrations/0001_create_schema.py` — creates `customers`, `orders`, `order_items`
- `migrations/0002_seed_data.py` — inserts 15 synthetic customers, 5 orders, 5 order_items (see the file for the exact inserts)
- `migrations/0003_convert_ids_to_uuid.py` — converts all IDs to UUID (preserves data via `customers_new` / `orders_new` / `order_items_new` swap pattern)

Confirm with a quick query:

```bash
PGPASSWORD=syed123 psql -h localhost -U syed -d nl2pg -c "SELECT * FROM customers LIMIT 3;"
```

Expected: 3 rows with `id` as UUID (e.g., `5405eee9-76f4-409a-8bb4-8f2ee42b9cfe`), `name`, `email`, `created_at`.

---

## 6. Confirm code quality (`ruff` / `pyproject.toml`)

```bash
ruff check src/ migrations/
ruff format --check src/ migrations/
```

Expected: clean (no errors). If not, fix with `ruff check --fix src/ migrations/` and `ruff format src/ migrations/`.

---

## 7. Run the baseline (deliberately naive comparison point)

```bash
python -m src.agent.baseline
# Then enter a request when prompted, or use:
python -c "
from src.agent.baseline import BaselineAgent
b = BaselineAgent(provider='groq')
print(b.run('Show me all orders with status pending.'))
"
```

The baseline:
- Uses a single `ChatPromptTemplate` with no schema context
- Returns whatever SQL the LLM produces
- Only executes if `SELECT`; refuses destructive keywords via `BLOCKED_PATTERN`
- Never validates against `information_schema`
- `validated=False`; `approval_required=False`; `approval_fn` is not part of its interface

---

## 8. Run the agent (schema-aware, validated, approval-gated)

```bash
python -m src.agent.agent
# Enter request when prompted, or use:
python -c "
from src.agent.agent import Agent
agent = Agent(approval_fn=lambda a: True, provider='groq')
print(agent.run('Show me all orders over \$500 from customers who joined in 2026.'))
"
```

The agent:
- Introspects the live DB via `introspect_schema()`
- Sends structured `ProposedAction` to the LLM (via `with_structured_output`)
- Validates action against `TableInfo` / `ColumnInfo`
- Builds SQL via `build_sql()` only from the validated object
- For writes, requires `approval_fn`; default (`None`) denies all writes
- Has `DEBUG`-level logs at every decision point (see `src/logging_config.py`)

---

## 9. Run the evaluation harness (`eval_results/`)

```bash
python -m src.eval.run_eval --json --out eval_report
```

This produces:

- `eval_report.md` — markdown table with per-case PASS/PARTIAL/FAIL
- `eval_report.json` — raw JSON with rows + summary counts

Both runners use the same `TEST_CASES` from `src/eval/cases.py` (11 cases: 4 reads, 5 writes, 3 adversarial). The agent uses `approval_fn=lambda a: True` so execution paths can be measured; the real gate (`approval_fn=None`) is still demonstrably present.

Expected final output (from `eval_report4.md`):

| Runner | PASS | PARTIAL | FAIL |
| --- | ---: | ---: | ---: |
| baseline | 9 | 1 | 2 |
| agent | 12 | 0 | 0 |

The hard case **A02** (`"Show me high value orders."`) is called out explicitly in the markdown: baseline invents `total_amount`; agent returns `action='clarify'`.

Approximate runtime: **3–12 minutes** depending on LLM provider and rate-limit window (`EVAL_CASE_DELAY_SECONDS=12` between cases; `LLM_MIN_REQUEST_INTERVAL_SECONDS=3.0` between calls; retries with exponential backoff on 429).

---

## 10. Reproduce the trajectory deliverable

The trajectory `trajectories/phase5_aggregation_failure.json` is already saved and readable. It contains:

- `failure_mode`: LLM ignores GROUP BY / HAVING
- `before`: wrong SQL (`SELECT * FROM orders;`) and wrong reasoning
- `after_fix`: list of changes (schema fields, SQL builder, semantic validation, prompt reinforcement)
- `evidence`: log snippet showing execution of wrong query

To generate a new trajectory (e.g., an approved write or a rejected query), run the agent interactively and observe the `DEBUG` log output (see `src/logging_config.py`). Every agent decision point logs at DEBUG: context sent, query generated, validation result, approval requested/given/denied, execution result.

---

## 11. Cost / runtime estimate

The evaluation harness runs the LLM twice per case (baseline + agent = 22 calls for 11 cases). With `LLM_MIN_REQUEST_INTERVAL_SECONDS=3.0`, that's at least 66 seconds of throttle time, plus the LLM response time (~1–5 seconds per call depending on provider/model), plus rate-limit retries (the `invoke_with_backoff()` wrapper in `rate_limit.py` handles 429s with exponential backoff + `Retry-After` header respect).

Real-world observed times from `eval_report4.md`: baseline ~0.5–1.3s/case; agent ~2.7–11.4s/case. Total ~3–12 minutes for the full harness.

Token usage is low — structured JSON output is short, and both agents use `temperature=0.3`, not high-temperature sampling. No caching layer is implemented; a production version might cache `introspect_schema()` outputs (currently read fresh per `Agent.run()`).

---

## 12. Known limitations / caveats

- **LLM non-determinism:** even with `temperature=0.3`, the same prompt can return slightly different `ProposedAction` objects (e.g., different filter order, different `reasoning` text). The evaluation is designed to be robust to this: scoring rules check `executed`, `approval_required`, and whether the SQL is correct — not whether the reasoning text matches an expected string.
- **Rate limits:** the harness pauses `EVAL_CASE_DELAY_SECONDS=12` between cases specifically to avoid cascading 429 errors (see `rate_limit.py` comments for the design rationale — a single 429 retry burns the next case's rate-limit window).
- **Provider differences:** `Groq` (`openai/gpt-oss-120b`) is the default; `Gemini` (`gemini-3.5-flash`) works but requires `GEMINI_API_KEY`. The baseline and agent both use the same `get_llm()` factory (`agent.py` and `baseline.py`) so they can be switched independently.
- **Schema changes:** if you add a new table or change `status` enum values, apply via `yoyo` and the agent will pick it up on the next `Agent.run()` (introspection is live, not cached).

---

## Files referenced in this guide (and where to find them)

| File | What it contains |
| --- | --- |
| `README.md` | Project overview, problem statement, evidence pointers |
| `docker-compose.yml` | Postgres (`syed:syed123`) + pgAdmin (`syed@syed.com:syed123`) |
| `migrations/0001_create_schema.py` | Schema: `customers`, `orders`, `order_items` (initial SERIAL IDs) |
| `migrations/0002_seed_data.py` | Synthetic seed (15 customers, 5 orders, 5 items) |
| `migrations/0003_convert_ids_to_uuid.py` | UUID conversion (preserves data via table swaps) |
| `yoyo.ini` | Migration config (`uri = postgresql://syed:syed123@localhost:5432/nl2pg`) |
| `.env.example` | Template for required env vars |
| `pyproject.toml` | Project config (`name = "nl2pg"`, `requires-python = ">=3.11"`) |
| `src/logging_config.py` | DEBUG-level logging setup |
| `src/db/connection.py` | `get_connection()`, `db_connection()`, `ConnectionError`/`QueryExecutionError` |
| `src/db/introspect.py` | Live `information_schema` reading → `TableInfo` / `ColumnInfo` |
| `src/models/schemas.py` | Pydantic models (`ProposedAction`, `Filter`, `Join`, etc.) |
| `src/agent/baseline.py` | Naive baseline (`ChatPromptTemplate`, `validated=False`) |
| `src/agent/agent.py` | Schema-aware agent (`introspect`, `build_sql`, `_validate_action`, `_request_approval`) |
| `src/agent/approval.py` | CLI approval function (`cli_approval`) |
| `src/agent/rate_limit.py` | `throttle()`, `invoke_with_backoff()` for LLM calls |
| `src/eval/cases.py` | 11 fixed NL test requests |
| `src/eval/run_eval.py` | Harness (`run_all()`, `render_markdown()`, `_score_case()`) |
| `eval_results/eval_report4.md` | Final evaluation report |
| `eval_results/eval_report4.json` | Raw JSON results |
| `trajectories/phase5_aggregation_failure.json` | Saved agent trajectory (before/after fix) |
