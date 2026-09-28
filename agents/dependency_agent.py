"""Finds schema objects that depend on whatever a change touches."""
import re

from . import sandbox as sb

BREAKING_KINDS = {"DROP_TABLE", "DROP_COLUMN", "RENAME_COLUMN", "RENAME_TABLE", "TRUNCATE"}
ADDITIVE_KINDS = {"CREATE_INDEX", "CREATE_TABLE", "CREATE_VIEW", "CREATE_TRIGGER", "ADD_COLUMN", "SELECT"}


def _mentions(sql: str | None, name: str) -> bool:
    return bool(sql) and re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", sql, re.I) is not None


class DependencyAgent:
    name = "Dependency Agent"

    def analyze(self, conn, statements) -> list[dict]:
        found, seen = [], set()

        def add(stmt, obj_type, obj, relation, impact):
            key = (stmt.index, obj_type, obj)
            if key not in seen:
                seen.add(key)
                found.append({"statement": stmt.index, "source": stmt.target, "type": obj_type,
                              "object": obj, "relation": relation, "impact": impact})

        for s in statements:
            table = s.table
            if not table or not sb.exists(conn, table, "table"):
                continue
            breaking = s.kind in BREAKING_KINDS
            col = s.column

            # Tables whose foreign keys point at this table (and column, when one is targeted)
            for other in sb.objects(conn, "table"):
                for fk in sb.foreign_keys(conn, other["name"]):
                    if fk["table"].lower() != table.lower():
                        continue
                    if col and (fk["to"] or "").lower() != col.lower():
                        continue
                    impact = "BREAKS" if breaking or s.kind == "DELETE" else "REFERENCES"
                    add(s, "foreign key", f"{other['name']}.{fk['from']}", f"references {table}.{fk['to']}", impact)

            # This table's own foreign keys (parents it depends on)
            if col:
                for fk in sb.foreign_keys(conn, table):
                    if fk["from"].lower() == col.lower():
                        add(s, "foreign key", f"{table}.{fk['from']}", f"references {fk['table']}.{fk['to']}",
                            "BREAKS" if breaking else "INFO")

            for o in sb.objects(conn):
                if o["type"] == "table" or not o["sql"]:
                    continue
                on_table = o["tbl_name"].lower() == table.lower()
                if o["type"] == "view" and _mentions(o["sql"], table):
                    uses_col = not col or _mentions(o["sql"], col)
                    if uses_col:
                        impact = "BREAKS" if breaking else "INFO" if s.kind in ADDITIVE_KINDS else "IMPACTED"
                        add(s, "view", o["name"], f"selects from {table}", impact)
                elif o["type"] == "trigger" and (on_table or _mentions(o["sql"], table)):
                    uses_col = not col or _mentions(o["sql"], col)
                    if uses_col:
                        rel = f"fires on {o['tbl_name']}" if on_table else f"writes to {table}"
                        add(s, "trigger", o["name"], rel,
                            "BREAKS" if breaking else "FIRES" if s.kind in ("UPDATE", "DELETE", "INSERT")
                            else "INFO" if s.kind in ADDITIVE_KINDS else "IMPACTED")
                elif o["type"] == "index" and on_table:
                    uses_col = not col or _mentions(o["sql"], col)
                    if uses_col:
                        add(s, "index", o["name"], f"indexes {table}", "BREAKS" if breaking else "INFO")
        return found
