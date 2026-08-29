# Developer Readme — NL-to-Safe-Query Agent for Postgres

This file is a running log of development progress, setup steps taken, and important decisions. It will be updated as we move through each phase.

---

## Phase 1 — Setup (Completed)

### Date: 2023-08-29

### Actions Taken:
- Created `.gitignore` (database files, .env, Python cache, OS files)
- Copied `Dockerfile` (postgres:16 + pgvector) from docker-examples/
- Created `docker-compose.yml` (postgres_db + pgadmin services)
- Added `.env.example` with placeholder values (no real secrets committed)

### Key Decisions:
- Used `postgres:16` base image with pgvector extension for potential future vector operations
- Docker-compose uses `shared_nl2pg_net` (external) — requires manual creation first
- `.env` is in `.gitignore` from commit 1 (security best practice)

### Verification Steps:
```bash
docker network create shared_nl2pg_net
docker compose up --build
```

### Next Step:
Confirm Docker stack is running, then move to Schema Decision (Phase 2).

---
