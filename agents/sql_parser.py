"""Splits a SQL change script into statements and classifies each one."""
import re
from dataclasses import dataclass, field, asdict

ID = r'[`"\[]?(?:\w+\.)?[`"\[]?([A-Za-z_][\w$]*)[`"\]]?'
FLAGS = re.I | re.S


@dataclass
class Statement:
    index: int
    sql: str
    kind: str
    table: str | None = None
    object_name: str | None = None
    column: str | None = None
    new_name: str | None = None
    where: str | None = None
    column_def: str | None = None
    if_exists: bool = False

    @property
    def target(self) -> str:
        if self.column:
            return f"{self.table}.{self.column}"
        return self.object_name or self.table or "-"

    def to_dict(self):
        d = asdict(self)
        d["target"] = self.target
        return d


def split_statements(script: str) -> list[str]:
    """Split on top-level semicolons, ignoring quotes, comments and trigger bodies."""
    out, buf, i, quote = [], [], 0, None
    n = len(script)
    while i < n:
        ch = script[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif script.startswith("--", i):
            j = script.find("\n", i)
            i = n if j == -1 else j
            continue
        elif script.startswith("/*", i):
            j = script.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        elif ch == ";":
            text = "".join(buf)
            in_trigger = re.match(r"\s*CREATE\s+(TEMP\w*\s+)?TRIGGER", text, re.I) and not re.search(r"\bEND\s*$", text, re.I)
            if in_trigger:
                buf.append(ch)
            else:
                if text.strip():
                    out.append(text.strip())
                buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


PATTERNS = [
    ("CREATE_INDEX", rf"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(IF\s+NOT\s+EXISTS\s+)?{ID}\s+ON\s+{ID}"),
    ("CREATE_TABLE", rf"^CREATE\s+(?:TEMP\w*\s+)?TABLE\s+(IF\s+NOT\s+EXISTS\s+)?{ID}"),
    ("CREATE_VIEW", rf"^CREATE\s+(?:TEMP\w*\s+)?VIEW\s+(IF\s+NOT\s+EXISTS\s+)?{ID}"),
    ("CREATE_TRIGGER", rf"^CREATE\s+(?:TEMP\w*\s+)?TRIGGER\s+(IF\s+NOT\s+EXISTS\s+)?{ID}.*?\bON\s+{ID}"),
    ("DROP", rf"^DROP\s+(TABLE|VIEW|INDEX|TRIGGER)\s+(IF\s+EXISTS\s+)?{ID}"),
    ("RENAME_COLUMN", rf"^ALTER\s+TABLE\s+{ID}\s+RENAME\s+(?:COLUMN\s+)?{ID}\s+TO\s+{ID}"),
    ("RENAME_TABLE", rf"^ALTER\s+TABLE\s+{ID}\s+RENAME\s+TO\s+{ID}"),
    ("ADD_COLUMN", rf"^ALTER\s+TABLE\s+{ID}\s+ADD\s+(?:COLUMN\s+)?{ID}\s*(.*)$"),
    ("DROP_COLUMN", rf"^ALTER\s+TABLE\s+{ID}\s+DROP\s+(?:COLUMN\s+)?{ID}"),
    ("UPDATE", rf"^UPDATE\s+(?:OR\s+\w+\s+)?{ID}\s+SET\s+.+?(?:\s+WHERE\s+(.+))?$"),
    ("DELETE", rf"^DELETE\s+FROM\s+{ID}(?:\s+WHERE\s+(.+))?$"),
    ("INSERT", rf"^(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+{ID}"),
    ("TRUNCATE", rf"^TRUNCATE\s+(?:TABLE\s+)?{ID}"),
    ("SELECT", r"^(SELECT|WITH)\b"),
]


def classify(index: int, sql: str) -> Statement:
    norm = re.sub(r"\s+", " ", sql).strip()
    for kind, pat in PATTERNS:
        m = re.match(pat, norm, FLAGS)
        if not m:
            continue
        g = m.groups()
        s = Statement(index=index, sql=sql, kind=kind)
        if kind == "CREATE_INDEX":
            s.if_exists, s.object_name, s.table = bool(g[0]), g[1], g[2]
        elif kind in ("CREATE_TABLE", "CREATE_VIEW"):
            s.if_exists, s.object_name = bool(g[0]), g[1]
            if kind == "CREATE_TABLE":
                s.table = g[1]
        elif kind == "CREATE_TRIGGER":
            s.if_exists, s.object_name, s.table = bool(g[0]), g[1], g[2]
        elif kind == "DROP":
            obj = g[0].upper()
            s.kind = f"DROP_{obj}"
            s.if_exists, s.object_name = bool(g[1]), g[2]
            if obj == "TABLE":
                s.table = g[2]
        elif kind == "RENAME_COLUMN":
            s.table, s.column, s.new_name = g
        elif kind == "RENAME_TABLE":
            s.table, s.new_name = g
        elif kind == "ADD_COLUMN":
            s.table, s.column, s.column_def = g[0], g[1], g[2].strip()
        elif kind == "DROP_COLUMN":
            s.table, s.column = g
        elif kind in ("UPDATE", "DELETE"):
            s.table, s.where = g[0], (g[1].strip() if g[1] else None)
        elif kind in ("INSERT", "TRUNCATE"):
            s.table = g[0]
        return s
    return Statement(index=index, sql=sql, kind="OTHER", object_name=norm.split(" ")[0].upper() if norm else None)


def parse(script: str) -> list[Statement]:
    return [classify(i, s) for i, s in enumerate(split_statements(script))]
