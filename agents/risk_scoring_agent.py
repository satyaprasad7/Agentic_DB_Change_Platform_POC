"""Scores a change from its statements, blast radius, reversibility and test outcome."""

BASE = {
    "SELECT": 0, "INSERT": 10, "CREATE_TABLE": 5, "CREATE_VIEW": 5, "CREATE_INDEX": 10, "CREATE_TRIGGER": 20,
    "ADD_COLUMN": 10, "RENAME_COLUMN": 30, "RENAME_TABLE": 35, "UPDATE": 25, "DELETE": 35,
    "DROP_INDEX": 20, "DROP_VIEW": 25, "DROP_TRIGGER": 20, "DROP_COLUMN": 45, "DROP_TABLE": 60,
    "TRUNCATE": 70, "OTHER": 30,
}

LEVELS = [(25, "LOW"), (50, "MEDIUM"), (75, "HIGH"), (101, "CRITICAL")]

APPROVALS = {
    "LOW": [],
    "MEDIUM": ["LEAD"],
    "HIGH": ["DBA"],
    "CRITICAL": ["DBA", "CAB"],
}


def level_for(score: int) -> str:
    return next(name for limit, name in LEVELS if score < limit)


def _volume_points(rows: int) -> int:
    return 20 if rows >= 1000 else 12 if rows >= 100 else 6 if rows >= 10 else 2 if rows >= 1 else 0


class RiskScoringAgent:
    name = "Risk Scoring Agent"

    def score(self, statements, impact: dict, dependencies: list[dict], rehearsal: dict, tests: dict | None) -> dict:
        per_stmt, factors = [], []
        plans = {p["statement"]: p for p in rehearsal.get("plans", [])}

        for s in statements:
            pts = BASE.get(s.kind, 30)
            f = [(f"{s.kind.replace('_', ' ').title()} on {s.target}", pts)]
            rows = impact.get(s.index, {}).get("rows")
            if s.kind in ("UPDATE", "DELETE") and not s.where:
                f.append(("No WHERE clause: every row is affected", 40))
            if rows and s.kind in ("UPDATE", "DELETE", "DROP_TABLE", "DROP_COLUMN", "CREATE_INDEX"):
                label = "Index build on" if s.kind == "CREATE_INDEX" else "Touches"
                f.append((f"{label} {rows:,} rows", _volume_points(rows)))
            if rows and s.kind in ("DELETE", "DROP_TABLE", "DROP_COLUMN", "TRUNCATE"):
                f.append(("Destroys data", 15))
            deps = [d for d in dependencies if d["statement"] == s.index and d["impact"] in ("BREAKS", "IMPACTED", "FIRES")]
            if deps:
                f.append((f"{len(deps)} dependent object(s) affected", min(20, 5 * len(deps))))
            plan = plans.get(s.index)
            if plan and plan["reversible"] is not True:
                f.append(("No clean automatic rollback", 20))
            total = sum(p for _, p in f)
            per_stmt.append({"statement": s.index, "score": total})
            factors += [{"statement": s.index, "factor": label, "points": p} for label, p in f]

        scores = sorted((p["score"] for p in per_stmt), reverse=True)
        score = (scores[0] + 0.2 * sum(scores[1:])) if scores else 0

        if not rehearsal.get("forward_ok"):
            factors.append({"statement": None, "factor": f"Dry run failed: {rehearsal.get('error')}", "points": 30})
            score += 30
        elif rehearsal.get("verified") is False:
            factors.append({"statement": None, "factor": "Rollback rehearsal did not restore the original state", "points": 15})
            score += 15
        if tests and not tests["passed"]:
            factors.append({"statement": None, "factor": "Synthetic data regression checks failed", "points": 15})
            score += 15

        score = int(min(100, round(score)))
        level = level_for(score)
        if not rehearsal.get("forward_ok") and level in ("LOW", "MEDIUM"):
            level = "HIGH"
        return {
            "score": score,
            "level": level,
            "factors": factors,
            "per_statement": per_stmt,
            "required_approvals": APPROVALS[level],
        }
