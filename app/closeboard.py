"""Month-end close board: properties down the side, close steps across the top, one month at a time.

Rows are the projects made from the close template (default: the template whose name contains "close") that
start in the chosen month, labelled by their property. Columns are the template's top-level steps, in order.
Properties that closed last month but have no close project this month show up as "not started".
"""
from datetime import date

from .db import rows, tx


def _shift(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    m += n
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def board(month: str | None = None, template_id: int | None = None) -> dict:
    today = date.today()
    month = month or today.strftime("%Y-%m")
    with tx() as c:
        templates = rows(c.execute("""SELECT t.id, t.name, COUNT(p.id) AS projects FROM templates t
                                      LEFT JOIN projects p ON p.template_id=t.id GROUP BY t.id
                                      ORDER BY (lower(t.name) LIKE '%close%') DESC, COUNT(p.id) DESC, t.name"""))
        if not template_id and templates:
            template_id = templates[0]["id"]
        steps = [r["title"] for r in c.execute("""SELECT title FROM template_items WHERE template_id=? AND parent_item_id IS NULL
                                                  ORDER BY position, id""", (template_id,))]
        projects = rows(c.execute(
            """SELECT p.id, p.name, p.status, p.start_date, p.property_id, p.property, pr.name AS property_name
               FROM projects p LEFT JOIN properties pr ON pr.id=p.property_id
               WHERE p.template_id=? AND substr(p.start_date,1,7)=? AND p.is_general=0
               ORDER BY COALESCE(pr.name, p.property, p.name) COLLATE NOCASE""", (template_id, month)))
        months = [r[0] for r in c.execute("""SELECT DISTINCT substr(start_date,1,7) FROM projects WHERE template_id=?
                                             ORDER BY 1 DESC LIMIT 24""", (template_id,))]
        prev_props = rows(c.execute(
            """SELECT DISTINCT p.property_id, COALESCE(pr.name, p.property) AS label FROM projects p
               LEFT JOIN properties pr ON pr.id=p.property_id
               WHERE p.template_id=? AND substr(p.start_date,1,7)=? AND p.is_general=0 AND p.property_id IS NOT NULL""",
            (template_id, _shift(month, -1))))
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
    return {"month": month, "prev": _shift(month, -1), "next": _shift(month, 1), "template_id": template_id,
            "templates": templates, "months": months, "columns": cols, "rows": out_rows, "missing": missing,
            "totals": totals,
            "summary": {"properties": len(out_rows), "closed": sum(1 for r in out_rows if r["total"] and r["done"] == r["total"]),
                        "overdue": sum(r["overdue"] for r in out_rows), "blocked": sum(r["blocked"] for r in out_rows)}}
