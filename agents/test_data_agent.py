"""Generates synthetic rows against the post-change schema and runs regression checks."""
import random
import uuid

from faker import Faker

from . import sandbox as sb


class SyntheticTestDataAgent:
    name = "Synthetic Test Data Agent"

    def __init__(self, rows_per_table: int = 5):
        self.rows_per_table = rows_per_table
        self.fake = Faker()
        self.rnd = random.Random()

    def _value(self, conn, table: str, col: dict, fks: dict):
        name, ctype = col["name"].lower(), (col["type"] or "").upper()
        if col["name"] in fks:
            ref_table, ref_col = fks[col["name"]]
            row = conn.execute(f'SELECT "{ref_col}" FROM "{ref_table}" ORDER BY RANDOM() LIMIT 1').fetchone()
            return row[0] if row else None
        tag = uuid.uuid4().hex[:6]
        f = self.fake
        if "email" in name:
            return f"{f.user_name()}.{tag}@example.test"
        if name == "sku":
            return f"SKU-T{tag.upper()}"
        if name in ("name", "full_name", "customer_name"):
            return f.name() if table.lower() in ("customers", "users") else f.catch_phrase()
        if "country" in name:
            return f.country_code()
        if "phone" in name:
            return f.phone_number()
        if "status" in name:
            return self.rnd.choice(["NEW", "PAID", "SHIPPED", "CLOSED"])
        if "tier" in name:
            return self.rnd.choice(["STANDARD", "SILVER", "GOLD"])
        if name.endswith("_at") or "date" in name:
            return f.date_time_between("-1y", "now").isoformat(sep=" ", timespec="seconds")
        if name in ("active", "enabled") or name.startswith("is_"):
            return self.rnd.randint(0, 1)
        if name in ("qty", "quantity"):
            return self.rnd.randint(1, 5)
        if any(w in name for w in ("price", "total", "amount", "revenue")):
            return round(self.rnd.uniform(1, 500), 2)
        if "INT" in ctype:
            return self.rnd.randint(1, 1000)
        if any(w in ctype for w in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
            return round(self.rnd.uniform(1, 1000), 2)
        return f"{f.word()}-{tag}"

    def run(self, clone, statements, affected: list[str]) -> dict:
        """clone must already have the change applied."""
        results, checks = [], []
        for table in affected:
            if not sb.exists(clone, table, "table"):
                continue
            cols = [c for c in sb.columns(clone, table) if not (c["pk"] and "INT" in (c["type"] or "").upper())]
            fks = {fk["from"]: (fk["table"], fk["to"]) for fk in sb.foreign_keys(clone, table)}
            entry = {"table": table, "generated": 0, "inserted": 0, "sample": [], "errors": []}
            for _ in range(self.rows_per_table):
                row = {c["name"]: self._value(clone, table, c, fks) for c in cols}
                entry["generated"] += 1
                names = ", ".join(f'"{k}"' for k in row)
                marks = ", ".join("?" for _ in row)
                try:
                    clone.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', list(row.values()))
                    entry["inserted"] += 1
                    if len(entry["sample"]) < 3:
                        entry["sample"].append(row)
                except Exception as e:
                    if str(e) not in entry["errors"]:
                        entry["errors"].append(str(e))
            results.append(entry)
            checks.append({"name": f"Insert synthetic rows into {table}",
                           "passed": entry["inserted"] == entry["generated"],
                           "detail": f"{entry['inserted']}/{entry['generated']} rows" +
                                     (f" · {entry['errors'][0]}" if entry["errors"] else "")})

        for v in sb.objects(clone, "view"):
            try:
                clone.execute(f'SELECT * FROM "{v["name"]}" LIMIT 5').fetchall()
                checks.append({"name": f"Query view {v['name']}", "passed": True, "detail": "ok"})
            except Exception as e:
                checks.append({"name": f"Query view {v['name']}", "passed": False, "detail": str(e)})

        fk_violations = clone.execute("PRAGMA foreign_key_check").fetchall()
        checks.append({"name": "Foreign key integrity", "passed": not fk_violations,
                       "detail": f"{len(fk_violations)} violation(s)"})
        integrity = clone.execute("PRAGMA integrity_check").fetchone()[0]
        checks.append({"name": "Database integrity check", "passed": integrity == "ok", "detail": integrity})
        return {"tables": results, "checks": checks, "passed": all(c["passed"] for c in checks)}
