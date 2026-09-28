"""Runs a change request through the agent pipeline and owns its lifecycle.

Request -> Analyze -> Dependency -> Risk -> Rollback -> Test -> Jira -> Git
        -> Approve -> Deploy -> Validate -> Monitor -> (Rollback) with every step audited.
"""
import hashlib
import re
import threading
from datetime import datetime, timezone

from . import sandbox as sb
from .dependency_agent import DependencyAgent
from .risk_scoring_agent import RiskScoringAgent, APPROVALS
from .rollback_agent import RollbackAgent, diff_fingerprints, tag_for
from .sql_parser import parse
from .test_data_agent import SyntheticTestDataAgent

STAGES = [
    ("analyze", "Analyze", "SQL Analyzer"),
    ("dependency", "Dependencies", DependencyAgent.name),
    ("risk", "Risk", RiskScoringAgent.name),
    ("rollback", "Rollback plan", RollbackAgent.name),
    ("test", "Synthetic test", SyntheticTestDataAgent.name),
    ("jira", "Jira", "Ticketing"),
    ("git", "Git", "Version control"),
    ("approval", "Approval", "Policy engine"),
    ("deploy", "Deploy", "Deployer"),
    ("validate", "Validate", "Validator"),
    ("monitor", "Monitor", "Monitor"),
]

JIRA_PRIORITY = {"LOW": "Low", "MEDIUM": "Medium", "HIGH": "High", "CRITICAL": "Highest"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "change"


class Platform:
    def __init__(self):
        self.lock = threading.RLock()
        self.sandbox = sb.Sandbox()
        self.deps = DependencyAgent()
        self.risk = RiskScoringAgent()
        self.rollback = RollbackAgent()
        self.testdata = SyntheticTestDataAgent()
        self._init_state()

    def _init_state(self):
        self.changes: dict[str, dict] = {}
        self.audit: list[dict] = []
        self._snapshots: dict[str, object] = {}
        self._seq = 0

    def reset(self):
        with self.lock:
            self.sandbox.reset()
            self._init_state()
            self.log("system", None, "RESET", "Sandbox database re-seeded, all change requests cleared")

    # ---------- helpers ----------
    def log(self, actor, change_id, action, detail=""):
        self.audit.append({"at": now(), "actor": actor, "change_id": change_id, "action": action, "detail": detail})

    def _stage(self, ch, key, status, summary):
        st = next(s for s in ch["stages"] if s["key"] == key)
        st.update(status=status, summary=summary, at=now())
        self.log(st["agent"], ch["id"], f"{st['name'].upper()}:{status.upper()}", summary)

    def get(self, cid) -> dict:
        ch = self.changes.get(cid)
        if not ch:
            raise KeyError(cid)
        return ch

    @staticmethod
    def _affected(statements) -> list[str]:
        out = []
        for s in statements:
            for t in (s.table, s.new_name if s.kind == "RENAME_TABLE" else None):
                if t and t not in out:
                    out.append(t)
        return out

    @staticmethod
    def _impact(conn, statements) -> dict:
        impact = {}
        for s in statements:
            rows = None
            try:
                if s.kind in ("UPDATE", "DELETE"):
                    where = f" WHERE {s.where}" if s.where else ""
                    rows = conn.execute(f'SELECT COUNT(*) FROM "{s.table}"{where}').fetchone()[0]
                elif s.kind in ("DROP_TABLE", "CREATE_INDEX", "TRUNCATE", "ADD_COLUMN", "RENAME_COLUMN", "RENAME_TABLE"):
                    rows = sb.row_count(conn, s.table)
                elif s.kind == "DROP_COLUMN":
                    rows = conn.execute(f'SELECT COUNT("{s.column}") FROM "{s.table}"').fetchone()[0]
            except Exception:
                rows = None
            impact[s.index] = {"rows": rows}
        return impact

    # ---------- pipeline ----------
    def submit(self, title: str, sql: str, requested_by: str) -> dict:
        with self.lock:
            self._seq += 1
            cid = f"CHG-{1000 + self._seq}"
            tag = tag_for(cid)
            ch = {
                "id": cid, "title": title.strip() or "Untitled change", "sql": sql, "requested_by": requested_by or "anonymous",
                "created_at": now(), "status": "ANALYZING",
                "stages": [{"key": k, "name": n, "agent": a, "status": "pending", "summary": "", "at": None} for k, n, a in STAGES],
                "approvals": [], "deployment": None, "rollback_result": None,
            }
            self.changes[cid] = ch
            self.log(ch["requested_by"], cid, "SUBMITTED", ch["title"])

            statements = parse(sql)
            ch["statements"] = [s.to_dict() for s in statements]
            if not statements:
                self._stage(ch, "analyze", "failed", "No SQL statements found")
                ch["status"] = "BLOCKED"
                return ch
            affected = self._affected(statements)
            ch["affected_tables"] = affected

            prod_view = self.sandbox.clone()
            impact = self._impact(prod_view, statements)
            for s in ch["statements"]:
                s["rows_affected"] = impact[s["index"]]["rows"]

            # Rollback agent also performs the dry run: forward apply + rollback on a throwaway clone
            rehearsal = self.rollback.rehearse(self.sandbox.clone(), statements, tag, affected)
            kinds = sorted({s.kind.replace("_", " ").lower() for s in statements})
            if rehearsal["forward_ok"]:
                self._stage(ch, "analyze", "done",
                            f"{len(statements)} statement(s): {', '.join(kinds)}. Dry run succeeded on a production clone")
            else:
                self._stage(ch, "analyze", "failed", f"Dry run failed at statement #{rehearsal.get('failed_statement', -1) + 1}: {rehearsal['error']}")

            dependencies = self.deps.analyze(prod_view, statements)
            ch["dependencies"] = dependencies
            breaking = sum(1 for d in dependencies if d["impact"] == "BREAKS")
            self._stage(ch, "dependency", "warn" if breaking else "done",
                        f"{len(dependencies)} related object(s), {breaking} would break" if dependencies else "No dependent objects")

            tests = None
            if rehearsal["forward_ok"]:
                test_clone = self.sandbox.clone()
                test_clone.execute("BEGIN")
                for s in statements:
                    test_clone.execute(s.sql)
                test_clone.execute("COMMIT")
                tests = self.testdata.run(test_clone, statements, affected)
            ch["tests"] = tests

            risk = self.risk.score(statements, impact, dependencies, rehearsal, tests)
            ch["risk"] = risk
            self._stage(ch, "risk", {"LOW": "done", "MEDIUM": "done"}.get(risk["level"], "warn"),
                        f"{risk['level']} ({risk['score']}/100). Approvals: {', '.join(risk['required_approvals']) or 'auto-approve'}")

            plans = rehearsal.get("plans", [])
            ch["rollback_plan"] = plans
            ch["rollback_script"] = self.rollback.script(plans)
            ch["rehearsal"] = {k: rehearsal.get(k) for k in ("forward_ok", "error", "rollback_ok", "verified", "diff")}
            if not rehearsal["forward_ok"]:
                self._stage(ch, "rollback", "skipped", "Not planned: change does not apply")
            elif rehearsal["verified"]:
                self._stage(ch, "rollback", "done", f"{len(ch['rollback_script'])} step(s). Rehearsed on a clone: state restored exactly (schema + data checksums)")
            else:
                self._stage(ch, "rollback", "warn", "; ".join(rehearsal["diff"]) or "Rehearsal did not verify")

            if tests is None:
                self._stage(ch, "test", "skipped", "Skipped: dry run failed")
            else:
                failed = [c for c in tests["checks"] if not c["passed"]]
                inserted = sum(t["inserted"] for t in tests["tables"])
                self._stage(ch, "test", "failed" if failed else "done",
                            f"{inserted} synthetic row(s), {len(tests['checks']) - len(failed)}/{len(tests['checks'])} checks passed")

            n = self._seq
            ch["jira"] = {"key": f"DBCHG-{100 + n}", "summary": f"[DB] {ch['title']}", "priority": JIRA_PRIORITY[risk["level"]],
                          "labels": ["db-change", f"risk-{risk['level'].lower()}"], "assignee": "dba-oncall" if risk["level"] in ("HIGH", "CRITICAL") else ch["requested_by"]}
            self._stage(ch, "jira", "done", f"{ch['jira']['key']} created, priority {ch['jira']['priority']} (simulated)")

            name = f"V{n:03d}__{slug(ch['title']).replace('-', '_')}"
            forward = ";\n".join(s.sql for s in statements) + ";"
            rb = ";\n".join(ch["rollback_script"]) + (";" if ch["rollback_script"] else "")
            migration = (f"-- {cid} / {ch['jira']['key']}: {ch['title']}\n-- risk: {risk['level']} ({risk['score']})\n"
                         f"-- requested by: {ch['requested_by']}\n\n{forward}\n")
            ch["git"] = {"branch": f"dbchange/{ch['jira']['key'].lower()}-{slug(ch['title'])}", "files": {
                f"migrations/{name}.sql": migration, f"migrations/{name}.rollback.sql": f"-- rollback for {cid}\n{rb}\n"},
                "commit": hashlib.sha1((migration + rb).encode()).hexdigest()[:10]}
            self._stage(ch, "git", "done", f"Branch {ch['git']['branch']} with {len(ch['git']['files'])} file(s) (simulated)")

            if not rehearsal["forward_ok"]:
                ch["status"] = "BLOCKED"
                self._stage(ch, "approval", "failed", "Blocked: change fails its dry run and cannot be approved")
            elif not risk["required_approvals"]:
                ch["status"] = "APPROVED"
                ch["approvals"].append({"approver": "policy-engine", "role": "AUTO", "decision": "APPROVED", "comment": "Low risk", "at": now()})
                self._stage(ch, "approval", "done", "Auto-approved by policy (LOW risk)")
            else:
                ch["status"] = "PENDING_APPROVAL"
                self._stage(ch, "approval", "active", f"Waiting for: {', '.join(risk['required_approvals'])}")
            return ch

    def approve(self, cid, approver, role, comment=""):
        with self.lock:
            ch = self.get(cid)
            if ch["status"] != "PENDING_APPROVAL":
                raise ValueError(f"Change is {ch['status']}, not awaiting approval")
            role = role.upper()
            needed = ch["risk"]["required_approvals"]
            if role not in needed:
                raise ValueError(f"Role {role} is not a required approver. Needed: {', '.join(needed)}")
            if approver.strip().lower() == ch["requested_by"].strip().lower():
                raise ValueError("Requester cannot approve their own change (segregation of duties)")
            ch["approvals"].append({"approver": approver, "role": role, "decision": "APPROVED", "comment": comment, "at": now()})
            self.log(approver, cid, "APPROVED", f"as {role}. {comment}".strip())
            got = {a["role"] for a in ch["approvals"] if a["decision"] == "APPROVED"}
            missing = [r for r in needed if r not in got]
            if missing:
                self._stage(ch, "approval", "active", f"Approved by {', '.join(sorted(got))}; waiting for {', '.join(missing)}")
            else:
                ch["status"] = "APPROVED"
                self._stage(ch, "approval", "done", "All required approvals received: " + ", ".join(
                    f"{a['approver']} ({a['role']})" for a in ch["approvals"]))
            return ch

    def reject(self, cid, approver, reason=""):
        with self.lock:
            ch = self.get(cid)
            if ch["status"] not in ("PENDING_APPROVAL", "APPROVED"):
                raise ValueError(f"Change is {ch['status']} and cannot be rejected")
            ch["approvals"].append({"approver": approver, "role": "-", "decision": "REJECTED", "comment": reason, "at": now()})
            ch["status"] = "REJECTED"
            self._stage(ch, "approval", "failed", f"Rejected by {approver}: {reason}")
            return ch

    def _validate(self, conn, ch, statements) -> list[dict]:
        checks = []
        for s in statements:
            k = s.kind
            ok, label = None, None
            if k.startswith("CREATE_") and s.object_name:
                ok, label = sb.exists(conn, s.object_name), f"{s.object_name} exists"
            elif k.startswith("DROP_") and k != "DROP_COLUMN" and s.object_name:
                ok, label = not sb.exists(conn, s.object_name), f"{s.object_name} is gone"
            elif k == "ADD_COLUMN":
                ok, label = any(c["name"].lower() == s.column.lower() for c in sb.columns(conn, s.table)), f"{s.table}.{s.column} exists"
            elif k == "DROP_COLUMN":
                ok, label = not any(c["name"].lower() == s.column.lower() for c in sb.columns(conn, s.table)), f"{s.table}.{s.column} is gone"
            elif k == "RENAME_COLUMN":
                ok, label = any(c["name"].lower() == s.new_name.lower() for c in sb.columns(conn, s.table)), f"{s.table}.{s.new_name} exists"
            elif k == "RENAME_TABLE":
                ok, label = sb.exists(conn, s.new_name, "table"), f"{s.new_name} exists"
            elif k in ("UPDATE", "DELETE") and s.where and k == "DELETE":
                left = conn.execute(f'SELECT COUNT(*) FROM "{s.table}" WHERE {s.where}').fetchone()[0]
                ok, label = left == 0, f"no rows left matching delete predicate ({left})"
            if label:
                checks.append({"name": label, "passed": bool(ok)})
        for v in sb.objects(conn, "view"):
            try:
                conn.execute(f'SELECT * FROM "{v["name"]}" LIMIT 1').fetchall()
                checks.append({"name": f"view {v['name']} queryable", "passed": True})
            except Exception as e:
                checks.append({"name": f"view {v['name']} queryable: {e}", "passed": False})
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks.append({"name": f"foreign key check ({len(fk)} violations)", "passed": not fk})
        checks.append({"name": "integrity check", "passed": conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"})
        return checks

    def deploy(self, cid, actor):
        with self.lock:
            ch = self.get(cid)
            if ch["status"] != "APPROVED":
                raise ValueError(f"Change is {ch['status']}; only APPROVED changes can be deployed")
            statements = parse(ch["sql"])
            affected = ch["affected_tables"]
            prod = self.sandbox.conn
            self._snapshots[cid] = self.sandbox.clone()
            before = sb.fingerprint(prod, affected)
            counts_before = {t: d["rows"] for t, d in before["data"].items()}
            self.log(actor, cid, "DEPLOY:STARTED", "Pre-deploy snapshot taken")
            prod.execute("BEGIN")
            try:
                plans = self.rollback.execute_with_plan(prod, statements, tag_for(cid))
                prod.execute("COMMIT")
            except Exception as e:
                prod.execute("ROLLBACK")
                ch["status"] = "FAILED"
                ch["deployment"] = {"by": actor, "at": now(), "error": str(e)}
                self._stage(ch, "deploy", "failed", f"Transaction rolled back: {e}")
                return ch
            ch["rollback_plan"] = plans
            ch["rollback_script"] = self.rollback.script(plans)
            ch["_before"] = before
            self._stage(ch, "deploy", "done", f"{len(statements)} statement(s) committed by {actor}")

            checks = self._validate(prod, ch, statements)
            failed = [c for c in checks if not c["passed"]]
            after = sb.fingerprint(prod, affected)
            counts_after = {t: d["rows"] for t, d in after["data"].items()}
            ch["deployment"] = {"by": actor, "at": now(), "checks": checks,
                                "row_counts": [{"table": t, "before": counts_before.get(t), "after": counts_after.get(t)}
                                               for t in affected]}
            ch["status"] = "DEPLOYED"
            if failed:
                self._stage(ch, "validate", "failed", f"{len(failed)} check(s) failed. Rolling back automatically")
                self.rollback_change(cid, "auto-validator", "Post-deploy validation failed")
                return ch
            self._stage(ch, "validate", "done", f"{len(checks)} post-deploy checks passed")
            deltas = [f"{r['table']} {r['before']}→{r['after']}" for r in ch["deployment"]["row_counts"] if r["before"] != r["after"]]
            self._stage(ch, "monitor", "done", "Row counts: " + (", ".join(deltas) if deltas else "unchanged") + ". No errors observed")
            return ch

    def rollback_change(self, cid, actor, reason=""):
        with self.lock:
            ch = self.get(cid)
            if ch["status"] != "DEPLOYED":
                raise ValueError(f"Change is {ch['status']}; only DEPLOYED changes can be rolled back")
            prod, tag = self.sandbox.conn, tag_for(cid)
            method, error = "script", None
            if self.rollback.fully_reversible(ch["rollback_plan"]):
                prod.execute("BEGIN")
                try:
                    self.rollback.apply_rollback(prod, ch["rollback_plan"], tag)
                    prod.execute("COMMIT")
                except Exception as e:
                    prod.execute("ROLLBACK")
                    error, method = str(e), "snapshot"
            else:
                method = "snapshot"
            if method == "snapshot":
                self.sandbox.restore(self._snapshots[cid])
            diff = diff_fingerprints(ch["_before"], sb.fingerprint(prod, ch["affected_tables"]))
            ch["rollback_result"] = {"by": actor, "at": now(), "reason": reason, "method": method,
                                     "script_error": error, "verified": not diff, "diff": diff}
            ch["status"] = "ROLLED_BACK"
            self._stage(ch, "monitor", "warn", f"Rolled back via {method} by {actor}. " +
                        ("Verified: schema and data match pre-deploy checksums" if not diff else "Drift: " + "; ".join(diff)))
            self.log(actor, cid, "ROLLED_BACK", reason)
            return ch

    def schema(self):
        with self.lock:
            return sb.describe(self.sandbox.conn)


def public(ch: dict) -> dict:
    return {k: v for k, v in ch.items() if not k.startswith("_")}


__all__ = ["Platform", "public", "APPROVALS"]
