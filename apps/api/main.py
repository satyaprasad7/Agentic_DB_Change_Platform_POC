import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agents.orchestrator import Platform, public  # noqa: E402

app = FastAPI(title="Agentic DB Change Platform", version="0.2.0")
platform = Platform()
STATIC = Path(__file__).parent / "static"

SAMPLES = [
    {"title": "Add loyalty tier to customers", "requested_by": "priya.dev",
     "sql": "ALTER TABLE customers ADD COLUMN loyalty_tier TEXT DEFAULT 'STANDARD';\nCREATE INDEX idx_customers_country ON customers(country);"},
    {"title": "Close stale NEW orders", "requested_by": "ops.bot",
     "sql": "UPDATE orders\nSET status = 'CLOSED'\nWHERE status = 'NEW' AND ordered_at < '2025-06-01';"},
    {"title": "Rename products.name to product_name", "requested_by": "sam.dev",
     "sql": "ALTER TABLE products RENAME COLUMN name TO product_name;"},
    {"title": "Retire products.active flag", "requested_by": "sam.dev",
     "sql": "ALTER TABLE products DROP COLUMN active;"},
    {"title": "Purge inactive products", "requested_by": "ops.bot",
     "sql": "DELETE FROM products WHERE active = 0;"},
    {"title": "Drop orders.total (legacy)", "requested_by": "priya.dev",
     "sql": "ALTER TABLE orders DROP COLUMN total;"},
    {"title": "Clean up customers table", "requested_by": "intern",
     "sql": "DELETE FROM customers;"},
]


class ChangeIn(BaseModel):
    title: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    requested_by: str = "anonymous"


class Approval(BaseModel):
    approver: str = Field(min_length=1)
    role: str
    comment: str = ""


class Rejection(BaseModel):
    approver: str = Field(min_length=1)
    reason: str = ""


class Actor(BaseModel):
    actor: str = Field(min_length=1)
    reason: str = ""


def _run(fn, *args):
    try:
        return public(fn(*args))
    except KeyError:
        raise HTTPException(404, "Change not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"status": "running", "changes": len(platform.changes)}


@app.get("/api/samples")
def samples():
    return SAMPLES


@app.get("/api/schema")
def schema():
    return platform.schema()


@app.post("/api/changes")
def submit(body: ChangeIn):
    return public(platform.submit(body.title, body.sql, body.requested_by))


@app.get("/api/changes")
def list_changes():
    return [{"id": c["id"], "title": c["title"], "status": c["status"], "requested_by": c["requested_by"],
             "created_at": c["created_at"], "risk": (c.get("risk") or {}).get("level"),
             "score": (c.get("risk") or {}).get("score")} for c in reversed(list(platform.changes.values()))]


@app.get("/api/changes/{cid}")
def get_change(cid: str):
    return _run(platform.get, cid)


@app.post("/api/changes/{cid}/approve")
def approve(cid: str, body: Approval):
    return _run(platform.approve, cid, body.approver, body.role, body.comment)


@app.post("/api/changes/{cid}/reject")
def reject(cid: str, body: Rejection):
    return _run(platform.reject, cid, body.approver, body.reason)


@app.post("/api/changes/{cid}/deploy")
def deploy(cid: str, body: Actor):
    return _run(platform.deploy, cid, body.actor)


@app.post("/api/changes/{cid}/rollback")
def rollback(cid: str, body: Actor):
    return _run(platform.rollback_change, cid, body.actor, body.reason)


@app.get("/api/audit")
def audit():
    return list(reversed(platform.audit))


@app.post("/api/reset")
def reset():
    platform.reset()
    return {"status": "reset"}
