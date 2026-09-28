# Agentic DB Change Platform POC

AI-assisted database change management. A change request (SQL) goes through a
pipeline of agents that analyze it, map its blast radius, score the risk, build
and rehearse a rollback, and test it with synthetic data, all before a human
approves it. Deploys, validation, rollbacks and every decision are audited.

Everything runs against an in-memory SQLite "production" database seeded with a
small commerce schema, so the demo needs no external services, credentials or API keys.

## Quick start (Windows / PowerShell)

From the project root:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
python -m pip install -r apps/api/requirements.txt
python -m uvicorn main:app --app-dir apps/api --port 8010
```

Then open:

- **UI:** http://localhost:8010
- **API docs (Swagger):** http://localhost:8010/docs
- **Health check:** http://localhost:8010/health

On macOS/Linux, activate with `source .venv/bin/activate` instead. Everything else is the same.

Requirements: Python 3.10+ (tested on 3.12). The Python build must bundle SQLite 3.35 or newer, which is needed for `ALTER TABLE ... DROP COLUMN`. Current Python releases already do.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `[Errno 10048] ... only one usage of each socket address` (Windows) or `address already in use` | Something else is already on port 8010, usually an earlier copy of this server. Stop it with Ctrl+C in its terminal, or start on another port: `--port 8011` |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Run the `Set-ExecutionPolicy -Scope Process ...` line above. It only applies to the current terminal |
| `ModuleNotFoundError: No module named 'agents'` | Start the server from the project root with `--app-dir apps/api` exactly as shown |
| Demo data looks different from the walkthrough | Click **Reset demo** in the UI (or `POST /api/reset`). The database also resets whenever the server restarts |

## Demo

The UI has three tabs:

- **Changes:** submit SQL (or pick one of 7 samples), then see the agent pipeline, risk score, dependencies, rollback plan, synthetic test results, approvals, deployment and rollback.
- **Schema:** the live database: tables, columns, row counts, foreign keys, indexes, views and triggers.
- **Audit log:** every agent step, approval, deploy and rollback, with actor and timestamp.

[docs/demo-script.md](docs/demo-script.md) is a 5-step stakeholder walkthrough. Built-in samples:

| Sample | Outcome |
|---|---|
| Add loyalty tier to customers | LOW, auto-approved, deployable |
| Close stale NEW orders | MEDIUM, needs LEAD, fires the audit trigger |
| Rename products.name to product_name | MEDIUM, needs LEAD |
| Retire products.active flag | HIGH, needs DBA. Best one for showing a verified rollback |
| Purge inactive products | BLOCKED: foreign key violation from `order_items` |
| Drop orders.total (legacy) | BLOCKED: breaks view `v_customer_revenue` |
| Clean up customers table | BLOCKED/CRITICAL: `DELETE` with no WHERE, FK violation |

Demo approver names: `lee.lead` (LEAD), `dana.dba` (DBA), `cab.board` (CAB). A requester can't approve their own change.

## How it works

```
Request -> Analyze (dry run) -> Dependency -> Risk -> Rollback (rehearsed) -> Test (synthetic data)
        -> Jira -> Git -> Approve -> Deploy -> Validate -> Monitor -> Audit
```

- **Analyze / dry run:** the change runs on a clone of production first. If it fails, the change is **BLOCKED** and can't be approved.
- **Rollback rehearsal:** on a second clone, the change is applied, then rolled back. The agent then checks that the schema and per-table row checksums match the original exactly.
- **Deploy:** pre-deploy backups and the change run in one transaction, and a full snapshot is taken first. If post-deploy validation fails, the change is rolled back automatically.
- **Rollback:** runs the rehearsed script, falling back to the snapshot if needed, then verifies the checksums against the pre-deploy state.

### Agents

| Agent | File | What it does |
|---|---|---|
| SQL analyzer | `agents/sql_parser.py` | Splits the script and classifies each statement: operation, target table/column, WHERE clause |
| Dependency | `agents/dependency_agent.py` | Introspects the schema for foreign keys, views, triggers and indexes the change touches, and marks what would break |
| Rollback | `agents/rollback_agent.py` | Builds an inverse script plus before-image backups, and rehearses it on a clone |
| Synthetic test data | `agents/test_data_agent.py` | Generates Faker rows against the post-change schema (respecting FKs), then runs regression checks: views, FK integrity, `integrity_check` |
| Risk scoring | `agents/risk_scoring_agent.py` | Scores 0–100 from operation type, rows affected, missing WHERE, data loss, dependents, reversibility, and dry-run and test outcomes |
| Orchestrator | `agents/orchestrator.py` | Runs the pipeline, simulates Jira and Git, enforces approvals and segregation of duties, and deploys, validates, monitors, rolls back and audits |

### Risk levels

| Score | Level | Required approval |
|---|---|---|
| 0–24 | LOW | None (auto-approved by policy) |
| 25–49 | MEDIUM | LEAD |
| 50–74 | HIGH | DBA |
| 75–100 | CRITICAL | DBA + CAB |

A change that fails its dry run is at least HIGH and always BLOCKED.

## API

| Method | Path | Body / notes |
|---|---|---|
| POST | `/api/changes` | `{title, sql, requested_by}`. Runs the full agent pipeline |
| GET | `/api/changes` | List of changes with status and risk |
| GET | `/api/changes/{id}` | Full detail: stages, risk, dependencies, rollback plan, tests, deployment |
| POST | `/api/changes/{id}/approve` | `{approver, role, comment}` |
| POST | `/api/changes/{id}/reject` | `{approver, reason}` |
| POST | `/api/changes/{id}/deploy` | `{actor}`. Only for APPROVED changes |
| POST | `/api/changes/{id}/rollback` | `{actor, reason}`. Only for DEPLOYED changes |
| GET | `/api/schema` | Live schema and row counts |
| GET | `/api/audit` | Audit log, newest first |
| GET | `/api/samples` | Demo change requests |
| POST | `/api/reset` | Re-seed the database and clear all changes |
| GET | `/health` | `{status, changes}` |

Invalid transitions return `409` with a reason, for example approving a BLOCKED change or approving your own change.

Change status lifecycle: `PENDING_APPROVAL` / `APPROVED` (auto) / `BLOCKED`, then `APPROVED` or `REJECTED`, then `DEPLOYED` or `FAILED`, then `ROLLED_BACK`.

## Project structure

```
agents/
  sql_parser.py          statement splitting + classification
  sandbox.py             seeded SQLite database, cloning, introspection, fingerprints
  dependency_agent.py
  risk_scoring_agent.py
  rollback_agent.py
  test_data_agent.py
  orchestrator.py        pipeline, lifecycle, audit
apps/api/
  main.py                FastAPI app and routes
  static/index.html      demo UI (single file, no build step)
  requirements.txt
docs/                    architecture, workflow, demo script
pipelines/               CI pipeline stubs (GitHub Actions, Azure DevOps)
```

## Limitations (POC)

- The target database is in-memory SQLite. All state resets when the server restarts.
- Jira and Git are simulated: ticket keys, branch names and migration files are shown in the UI but not created.
- The analysis is rule-based and deterministic. No LLM calls are made.
- The SQL parser handles common DDL/DML. Unrecognized statements are scored conservatively and roll back by restoring the snapshot.
- The CI pipelines in `pipelines/` are placeholders.
