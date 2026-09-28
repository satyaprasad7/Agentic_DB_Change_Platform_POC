"""Builds a rollback plan for each statement and can rehearse it.

Planning and execution happen together: the agent walks the change statement by
statement against a live connection, capturing what it needs (original DDL,
before-images of rows) immediately before each statement runs. That way a plan
built for statement N reflects the schema left behind by statements 0..N-1.
"""
import re

from . import sandbox as sb


def _q(name: str) -> str:
    return f'"{name}"'


class RollbackAgent:
    name = "Rollback Agent"

    def _plan_one(self, conn, s, tag: str) -> dict:
        """Return {backup: [...], rollback: [...], reversible, strategy} for statement s."""
        bk = f"{sb.BACKUP_PREFIX}{tag}_{s.index}"
        k, t = s.kind, s.table
        plan = {"statement": s.index, "backup": [], "rollback": [], "reversible": True, "strategy": ""}

        if k == "SELECT":
            plan["strategy"] = "Read-only, nothing to undo"
        elif k in ("CREATE_TABLE", "CREATE_VIEW", "CREATE_INDEX", "CREATE_TRIGGER"):
            obj_type = k.split("_")[1]
            if s.if_exists and sb.exists(conn, s.object_name):
                plan["strategy"] = f"{obj_type.title()} already exists, statement is a no-op"
            else:
                plan["rollback"] = [f"DROP {obj_type} IF EXISTS {_q(s.object_name)}"]
                plan["strategy"] = f"Drop the new {obj_type.lower()}"
        elif k in ("DROP_VIEW", "DROP_INDEX", "DROP_TRIGGER"):
            ddl = sb.object_sql(conn, s.object_name)
            if ddl:
                plan["rollback"] = [ddl]
                plan["strategy"] = "Recreate from captured DDL"
            else:
                plan["strategy"] = "Object does not exist, statement is a no-op"
        elif k == "DROP_TABLE":
            ddl = sb.object_sql(conn, t)
            if ddl:
                deps = [r["sql"] for r in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE lower(tbl_name)=lower(?) AND type IN ('index','trigger') AND sql IS NOT NULL",
                    (t,))]
                plan["backup"] = [f"CREATE TABLE {_q(bk)} AS SELECT * FROM {_q(t)}"]
                plan["rollback"] = [ddl, f"INSERT INTO {_q(t)} SELECT * FROM {_q(bk)}", *deps]
                plan["strategy"] = f"Snapshot {sb.row_count(conn, t)} rows, recreate table + {len(deps)} index/trigger(s)"
            else:
                plan["strategy"] = "Table does not exist, statement is a no-op"
        elif k == "ADD_COLUMN":
            plan["rollback"] = [f"ALTER TABLE {_q(t)} DROP COLUMN {_q(s.column)}"]
            plan["strategy"] = "Drop the added column"
        elif k == "DROP_COLUMN":
            col = next((c for c in sb.columns(conn, t) if c["name"].lower() == s.column.lower()), None) if sb.exists(conn, t, "table") else None
            if col:
                d = f"{_q(col['name'])} {col['type'] or ''}".strip()
                if col["dflt_value"] is not None:
                    d += f" DEFAULT {col['dflt_value']}"
                    if col["notnull"]:
                        d += " NOT NULL"
                elif col["notnull"]:
                    plan["reversible"] = "partial"
                plan["backup"] = [f"CREATE TABLE {_q(bk)} AS SELECT rowid AS _rid, {_q(col['name'])} FROM {_q(t)}"]
                plan["rollback"] = [
                    f"ALTER TABLE {_q(t)} ADD COLUMN {d}",
                    f"UPDATE {_q(t)} SET {_q(col['name'])} = (SELECT b.{_q(col['name'])} FROM {_q(bk)} b WHERE b._rid = {_q(t)}.rowid)",
                ]
                plan["strategy"] = "Snapshot column values, re-add column and restore them"
                if plan["reversible"] == "partial":
                    plan["strategy"] += " (NOT NULL without default cannot be re-added)"
            else:
                plan["strategy"] = "Column not found"
        elif k == "RENAME_COLUMN":
            plan["rollback"] = [f"ALTER TABLE {_q(t)} RENAME COLUMN {_q(s.new_name)} TO {_q(s.column)}"]
            plan["strategy"] = "Rename column back"
        elif k == "RENAME_TABLE":
            plan["rollback"] = [f"ALTER TABLE {_q(s.new_name)} RENAME TO {_q(t)}"]
            plan["strategy"] = "Rename table back"
        elif k == "UPDATE":
            where = f" WHERE {s.where}" if s.where else ""
            cols = [c["name"] for c in sb.columns(conn, t) if not c["pk"]]
            sets = ", ".join(f"{_q(c)} = (SELECT b.{_q(c)} FROM {_q(bk)} b WHERE b._rid = {_q(t)}.rowid)" for c in cols)
            plan["backup"] = [f"CREATE TABLE {_q(bk)} AS SELECT rowid AS _rid, * FROM {_q(t)}{where}"]
            plan["rollback"] = [f"UPDATE {_q(t)} SET {sets} WHERE rowid IN (SELECT _rid FROM {_q(bk)})"]
            plan["strategy"] = "Capture before-image of matched rows, restore all columns"
        elif k == "DELETE":
            where = f" WHERE {s.where}" if s.where else ""
            plan["backup"] = [f"CREATE TABLE {_q(bk)} AS SELECT * FROM {_q(t)}{where}"]
            plan["rollback"] = [f"INSERT INTO {_q(t)} SELECT * FROM {_q(bk)}"]
            plan["strategy"] = "Capture deleted rows, re-insert them"
        elif k == "INSERT":
            plan["backup"] = [f"CREATE TABLE {_q(bk)} AS SELECT COALESCE(MAX(rowid), 0) AS m FROM {_q(t)}"]
            plan["rollback"] = [f"DELETE FROM {_q(t)} WHERE rowid > (SELECT m FROM {_q(bk)})"]
            plan["strategy"] = "Record high-water mark, delete rows above it"
        else:
            plan["reversible"] = False
            plan["strategy"] = "No automatic inverse. Rollback restores the pre-deploy snapshot"
        return plan

    def execute_with_plan(self, conn, statements, tag: str) -> list[dict]:
        """Run backups + statements on conn (caller owns the transaction). Returns the plans."""
        plans = []
        for s in statements:
            plan = self._plan_one(conn, s, tag)
            for b in plan["backup"]:
                conn.execute(b)
            conn.execute(s.sql)
            plans.append(plan)
        return plans

    @staticmethod
    def script(plans: list[dict]) -> list[str]:
        """Rollback statements in the order they must run (reverse of the change)."""
        return [sql for p in reversed(plans) for sql in p["rollback"]]

    @staticmethod
    def fully_reversible(plans: list[dict]) -> bool:
        return all(p["reversible"] is True for p in plans)

    def apply_rollback(self, conn, plans: list[dict], tag: str) -> None:
        for sql in self.script(plans):
            conn.execute(sql)
        self.drop_backups(conn, tag)

    @staticmethod
    def drop_backups(conn, tag: str) -> None:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                                             (f"{sb.BACKUP_PREFIX}{tag}_%",))]
        for n in names:
            conn.execute(f"DROP TABLE {_q(n)}")

    def rehearse(self, clone, statements, tag: str, affected: list[str]) -> dict:
        """Forward-apply on a clone, roll back, and prove the database is identical to where it started."""
        before = sb.fingerprint(clone, affected)
        result = {"forward_ok": False, "error": None, "plans": [], "rollback_ok": None, "verified": None, "diff": []}
        clone.execute("BEGIN")
        try:
            result["plans"] = self.execute_with_plan(clone, statements, tag)
            result["forward_ok"] = True
        except Exception as e:  # the change itself fails: that is the dry-run verdict
            clone.execute("ROLLBACK")
            result["error"] = str(e)
            result["failed_statement"] = _failed_index(statements, result["plans"])
            return result
        clone.execute("COMMIT")
        result["after"] = sb.fingerprint(clone, affected)
        if not self.fully_reversible(result["plans"]):
            result["rollback_ok"] = False
            result["verified"] = False
            result["diff"] = ["Not fully reversible by script; snapshot restore required"]
            return result
        try:
            clone.execute("BEGIN")
            self.apply_rollback(clone, result["plans"], tag)
            clone.execute("COMMIT")
            result["rollback_ok"] = True
        except Exception as e:
            clone.execute("ROLLBACK")
            result["rollback_ok"] = False
            result["verified"] = False
            result["diff"] = [f"Rollback script failed: {e}"]
            return result
        after_rb = sb.fingerprint(clone, affected)
        result["diff"] = diff_fingerprints(before, after_rb)
        result["verified"] = not result["diff"]
        return result


def _failed_index(statements, completed_plans) -> int:
    return statements[len(completed_plans)].index if len(completed_plans) < len(statements) else -1


def diff_fingerprints(a: dict, b: dict) -> list[str]:
    diffs = []
    for key in sorted(set(a["schema"]) | set(b["schema"])):
        if key not in b["schema"]:
            diffs.append(f"missing {key}")
        elif key not in a["schema"]:
            diffs.append(f"unexpected {key}")
        elif a["schema"][key] != b["schema"][key]:
            diffs.append(f"definition differs: {key}")
    for t in sorted(set(a["data"]) | set(b["data"])):
        da, db = a["data"].get(t), b["data"].get(t)
        if da != db:
            diffs.append(f"data differs: {t} ({(da or {}).get('rows')} -> {(db or {}).get('rows')} rows)")
    return diffs


def tag_for(change_id: str) -> str:
    return re.sub(r"\W", "", change_id).lower()
