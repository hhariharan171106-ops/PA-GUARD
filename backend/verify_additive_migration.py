"""Apply the app's additive SQLite schema migrations and verify record counts."""
from database import connect, initialize


def counts():
    with connect() as db:
        result = {}
        for table in ("cases", "pa_staff", "documents", "audit_events", "policy_documents",
                      "evidence_recommendations", "sla_recommendations", "workflow_failures",
                      "evidence_review_feedback"):
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            result[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if exists else None
        result["case_ids"] = tuple(row[0] for row in db.execute("SELECT id FROM cases ORDER BY id"))
        result["document_ids"] = tuple(row[0] for row in db.execute("SELECT id FROM documents ORDER BY id"))
        result["audit_ids"] = tuple(row[0] for row in db.execute("SELECT id FROM audit_events ORDER BY id"))
        result["policy_versions"] = tuple(tuple(row) for row in db.execute("SELECT policy_id,version FROM policy_documents ORDER BY policy_id,version"))
        return result


before = counts()
initialize()
after = counts()
if before != after:
    raise SystemExit(f"Record counts changed during additive migration: {before} -> {after}")
with connect() as db:
    staff_columns = {row["name"] for row in db.execute("PRAGMA table_info(pa_staff)")}
    history_table = db.execute(
        "SELECT name FROM sqlite_master WHERE type=? AND name=?", ("table", "pa_staff_history")
    ).fetchone()
if "service_types" not in staff_columns or not history_table:
    raise SystemExit("Expected additive staff-management schema is missing")
print("Additive migration verified; cases, staff, documents, audit IDs, policy versions, and other operational record counts preserved.")
