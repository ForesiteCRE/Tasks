"""Load demo people, templates and a monthly recurrence. Run: python seed.py"""
from datetime import date, timedelta
from app.db import init_db, tx
from app import services

init_db()
with tx() as c:
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        raise SystemExit("Already seeded.")
    for n, e, b in [("Alex Rivera", "alex@example.com", "blink-alex"),
                    ("Jordan Lee", "jordan@example.com", "blink-jordan"),
                    ("Sam Patel", "sam@example.com", "blink-sam")]:
        c.execute("INSERT INTO users(name,email,blink_user_id) VALUES(?,?,?)", (n, e, b))
    t1 = c.execute("INSERT INTO templates(name,description) VALUES('Month-End Close','Recurring monthly checklist')").lastrowid
    c.executemany("INSERT INTO template_items(template_id,title,default_assignee_id,due_offset_days,position) VALUES(?,?,?,?,?)",
        [(t1, "Reconcile rent roll", 1, 3, 0), (t1, "Send owner statements", 2, 5, 1),
         (t1, "Review delinquencies", 1, 5, 2), (t1, "Update pipeline report", 3, 7, 3)])
    t2 = c.execute("INSERT INTO templates(name,description) VALUES('New Listing Launch','Use whenever a new listing is signed')").lastrowid
    c.executemany("INSERT INTO template_items(template_id,title,default_assignee_id,due_offset_days,position) VALUES(?,?,?,?,?)",
        [(t2, "Order photos & drone", 3, 2, 0), (t2, "Draft offering memorandum", 2, 7, 1),
         (t2, "Create flyer", 3, 7, 2), (t2, "Post to CoStar / LoopNet", 2, 9, 3), (t2, "Email blast to broker list", 1, 10, 4)])
    c.execute("INSERT INTO recurrences(template_id,name_pattern,day_of_month) VALUES(?, '{month} {year} – Month-End Close', 1)", (t1,))
services.run_recurrences()
services.create_project_from_template(2, "123 Main St – Retail Listing", date.today() - timedelta(days=8))
print("Seeded.")
