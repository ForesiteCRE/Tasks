"""Tracking boards: properties down the side, a template's steps across the top.

Only templates with a board period get a board (Month-End Close, Onboarding, Budget…), so it never becomes
"everything". Periodic boards (month / quarter / year) show the projects started in that period, and properties
that had one last period but not this one show up as "not started". Ongoing boards (onboarding, sale hand-off)
show every open project plus those started in the last 90 days.
"""
from datetime import date

from .db import rows, tx


def _shift(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    m += n
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


PERIODS = ("month", "quarter", "year", "none")


def period_of(kind: str, d: date) -> str:
    """The period key a date falls in: 2026-11 · 2026-Q4 · 2026 · '' (ongoing boards have no periods)."""
    return {"month": d.strftime("%Y-%m"), "quarter": f"{d.year}-Q{(d.month - 1) // 3 + 1}", "year": str(d.year)}.get(kind, "")


def _range(kind: str, key: str) -> tuple[str, str]:
    """[first day, first day of the next period) for a period key."""
    if kind == "month":
        return f"{key}-01", f"{_shift(key, 1)}-01"
    if kind == "quarter":
        y, q = int(key[:4]), int(key[-1])
        a = f"{y:04d}-{3 * q - 2:02d}"
        return f"{a}-01", f"{_shift(a, 3)}-01"
    return f"{key}-01-01", f"{int(key) + 1:04d}-01-01"


def shift(kind: str, key: str, n: int) -> str:
    if kind == "month":
        return _shift(key, n)
    if kind == "quarter":
        y, q = int(key[:4]), int(key[-1]) - 1 + n
        return f"{y + q // 4}-Q{q % 4 + 1}"
    return str(int(key) + n)


def valid(kind: str, key: str) -> bool:
    import re
    pat = {"month": r"\d{4}-(0[1-9]|1[0-2])", "quarter": r"\d{4}-Q[1-4]", "year": r"\d{4}"}.get(kind)
    return bool(pat and re.fullmatch(pat, key or ""))


def label(kind: str, key: str) -> str:
    if kind == "month":
        return date.fromisoformat(key + "-01").strftime("%B %Y")
    if kind == "quarter":
        return f"Q{key[-1]} {key[:4]}"
    return key if kind == "year" else "Active"


def board(period: str | None = None, template_id: int | None = None) -> dict:
    """A tracking board for one template (only templates with a board period set get one)."""
    today = date.today()
    with tx() as c:
        templates = rows(c.execute("""SELECT t.id, t.name, t.board_period, COUNT(p.id) AS projects FROM templates t
                                      LEFT JOIN projects p ON p.template_id=t.id AND p.is_general=0
                                      WHERE t.board_period IS NOT NULL GROUP BY t.id
                                      ORDER BY (lower(t.name) LIKE '%close%') DESC, COUNT(p.id) DESC, t.name"""))
        ids = {t["id"] for t in templates}
        if template_id not in ids:
            template_id = templates[0]["id"] if templates else None
        kind = next((t["board_period"] for t in templates if t["id"] == template_id), "month")
        if kind == "none":
            period = ""
        elif not valid(kind, period or ""):
            period = period_of(kind, today)
        steps = [r["title"] for r in c.execute("""SELECT title FROM template_items WHERE template_id=? AND parent_item_id IS NULL
                                                  ORDER BY position, id""", (template_id,))]
        base = """SELECT p.id, p.name, p.status, p.start_date, p.property_id, p.property, pr.name AS property_name
                  FROM projects p LEFT JOIN properties pr ON pr.id=p.property_id
                  WHERE p.template_id=? AND p.is_general=0 AND p.status!='archived' AND {}
                  ORDER BY COALESCE(pr.name, p.property, p.name) COLLATE NOCASE, p.start_date"""
        if kind == "none":   # ongoing: everything still open, plus what started in the last 90 days
            projects = rows(c.execute(base.format("(p.status='active' OR p.start_date >= date('now','-90 day'))"), (template_id,)))
            periods, prev_props = [], []
        else:
            a, b = _range(kind, period)
            projects = rows(c.execute(base.format("p.start_date >= ? AND p.start_date < ?"), (template_id, a, b)))
            periods = sorted({period_of(kind, date.fromisoformat(r[0][:10])) for r in c.execute(
                "SELECT start_date FROM projects WHERE template_id=? AND is_general=0", (template_id,)) if r[0]}, reverse=True)[:24]
            pa, pb = _range(kind, shift(kind, period, -1))
            prev_props = rows(c.execute(
                """SELECT DISTINCT p.property_id, COALESCE(pr.name, p.property) AS label FROM projects p
                   LEFT JOIN properties pr ON pr.id=p.property_id
                   WHERE p.template_id=? AND p.start_date >= ? AND p.start_date < ? AND p.is_general=0
                     AND p.property_id IS NOT NULL""", (template_id, pa, pb)))
        out_rows, extra = [], []
        for p in projects:
            tasks = rows(c.execute(
                """SELECT t.id, t.title, t.status, t.due_date, t.completed_at, COALESCE(u.name,'Unassigned') AS assignee_name
                   FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id
                   WHERE t.project_id=? AND t.parent_id IS NULL ORDER BY t.position, t.id""", (p["id"],)))
            cells = {}
            for t in tasks:
                t["overdue"] = bool(t["status"] != "done" and t["due_date"] and t["due_date"] < today.isoformat())
                cells.setdefault(t["title"], t)
                if t["title"] not in steps and t["title"] not in extra:
                    extra.append(t["title"])
            nxt = next((t for t in tasks if t["status"] != "done"), None)
            out_rows.append({
                "project_id": p["id"], "project_name": p["name"], "property_id": p["property_id"],
                "label": p["property_name"] or p["property"] or p["name"], "cells": cells,
                "done": sum(t["status"] == "done" for t in tasks), "total": len(tasks),
                "overdue": sum(t["overdue"] for t in tasks), "blocked": sum(t["status"] == "blocked" for t in tasks),
                "next": nxt and {"id": nxt["id"], "title": nxt["title"], "assignee_name": nxt["assignee_name"],
                                 "due_date": nxt["due_date"], "overdue": nxt["overdue"]},
            })
    covered = {r["property_id"] for r in out_rows if r["property_id"]}
    missing = [{"property_id": p["property_id"], "label": p["label"]} for p in prev_props if p["property_id"] not in covered]
    cols = steps + extra
    totals = {col: sum(1 for r in out_rows if r["cells"].get(col, {}).get("status") == "done") for col in cols}
    nxt_key = shift(kind, period, 1) if kind != "none" else ""
    return {"period": period, "period_kind": kind, "label": label(kind, period) if kind != "none" else "Active",
            "prev": shift(kind, period, -1) if kind != "none" else "", "next": nxt_key,
            "month": period if kind == "month" else None,   # older clients
            "start_date": _range(kind, period)[0] if kind != "none" else today.isoformat(),
            "template_id": template_id, "templates": templates, "periods": periods, "months": periods,
            "columns": cols, "rows": out_rows, "missing": missing, "totals": totals,
            "summary": {"properties": len(out_rows), "closed": sum(1 for r in out_rows if r["total"] and r["done"] == r["total"]),
                        "overdue": sum(r["overdue"] for r in out_rows), "blocked": sum(r["blocked"] for r in out_rows)}}
