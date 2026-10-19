import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "pa_guard.db"
DOCUMENT_STORAGE_DIR = Path(__file__).parent / "document_storage"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize() -> None:
    DOCUMENT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY,
                patient_reference TEXT NOT NULL,
                payer TEXT NOT NULL,
                service TEXT NOT NULL,
                submission_date TEXT NOT NULL,
                deadline TEXT NOT NULL,
                status TEXT NOT NULL,
                risk TEXT NOT NULL,
                coordinator TEXT NOT NULL,
                priority TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                category TEXT,
                original_filename TEXT,
                stored_filename TEXT,
                file_path TEXT,
                mime_type TEXT,
                file_size INTEGER,
                uploaded_at TEXT,
                processed_at TEXT,
                processing_status TEXT DEFAULT 'UPLOADED',
                ocr_status TEXT DEFAULT 'PENDING',
                extracted_text TEXT,
                page_text TEXT,
                processing_error TEXT,
                evidence_summary TEXT,
                reviewed_by TEXT,
                reviewed_at TEXT,
                FOREIGN KEY(case_id) REFERENCES cases(id)
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                decision_type TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changed_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                field_name TEXT NOT NULL,
                previous_value INTEGER NOT NULL,
                new_value INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sla_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                warning_hours INTEGER NOT NULL,
                urgent_hours INTEGER NOT NULL,
                escalation_one_hours INTEGER NOT NULL,
                escalation_two_hours INTEGER NOT NULL,
                remaining_time_weight INTEGER NOT NULL DEFAULT 60,
                priority_weight INTEGER NOT NULL DEFAULT 20,
                evidence_weight INTEGER NOT NULL DEFAULT 20
            );

            CREATE TABLE IF NOT EXISTS policy_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_id TEXT NOT NULL,
                document_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                expires_at TEXT,
                version TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0,
                embedding TEXT,
                embedding_engine TEXT,
                payer TEXT,
                service_type TEXT,
                source_reference TEXT,
                required_documents TEXT,
                department TEXT,
                UNIQUE(policy_id, version)
            );

            CREATE TABLE IF NOT EXISTS sla_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                remaining_hours REAL NOT NULL,
                priority TEXT NOT NULL,
                explanation TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                assigned_role TEXT NOT NULL,
                source_references TEXT NOT NULL,
                confidence REAL NOT NULL,
                needs_human_review INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evidence_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                required_documents TEXT NOT NULL,
                missing_documents TEXT NOT NULL,
                possible_mismatches TEXT NOT NULL,
                explanation TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                source_references TEXT NOT NULL,
                needs_human_review INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evidence_review_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                required_evidence TEXT NOT NULL
                    CHECK(length(trim(required_evidence)) BETWEEN 1 AND 200),
                ml_prediction TEXT
                    CHECK(ml_prediction IS NULL OR ml_prediction IN (
                        'PREDICTED_MISSING',
                        'NOT_PREDICTED_MISSING',
                        'UNKNOWN'
                    )),
                human_outcome TEXT NOT NULL DEFAULT 'UNKNOWN'
                    CHECK(human_outcome IN ('PRESENT', 'MISSING', 'UNKNOWN', 'NEEDS_REVIEW')),
                review_status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(review_status IN ('PENDING', 'CONFIRMED', 'REJECTED')),
                reviewer_identifier TEXT
                    CHECK(reviewer_identifier IS NULL OR length(trim(reviewer_identifier)) BETWEEN 1 AND 100),
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                CHECK(
                    (review_status = 'PENDING' AND reviewed_at IS NULL
                        AND reviewer_identifier IS NULL)
                    OR
                    (review_status IN ('CONFIRMED', 'REJECTED')
                        AND reviewed_at IS NOT NULL
                        AND reviewer_identifier IS NOT NULL)
                ),
                FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS escalation_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                escalation_required INTEGER NOT NULL,
                escalation_level TEXT NOT NULL,
                reason TEXT NOT NULL,
                responsible_role TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                approval_required INTEGER NOT NULL,
                source_references TEXT NOT NULL,
                notification_status TEXT NOT NULL,
                actor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS escalation_role_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                escalation_level TEXT NOT NULL,
                responsible_role TEXT NOT NULL,
                department TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                expires_at TEXT,
                UNIQUE(escalation_level, department, effective_date)
            );

            CREATE TABLE IF NOT EXISTS role_assignment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                previous_values TEXT,
                new_values TEXT,
                FOREIGN KEY(assignment_id) REFERENCES escalation_role_assignments(id)
            );

            CREATE TABLE IF NOT EXISTS workflow_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                error_code TEXT NOT NULL,
                error_details TEXT NOT NULL,
                workflow_state TEXT NOT NULL,
                FOREIGN KEY(case_id) REFERENCES cases(id)
            );

            CREATE TABLE IF NOT EXISTS recovery_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                error_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                failure_type TEXT NOT NULL,
                recommended_recovery TEXT NOT NULL,
                retry_allowed INTEGER NOT NULL,
                human_approval_required INTEGER NOT NULL,
                recovery_status TEXT NOT NULL,
                source_references TEXT NOT NULL,
                actor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manual_follow_ups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                responsible_role TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )

        document_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(documents)")
        }
        for name, default in (
            ("category", "Clinical Notes"),
            ("original_filename", ""),
            ("stored_filename", ""),
            ("file_path", ""),
            ("mime_type", "application/octet-stream"),
            ("file_size", 0),
            ("uploaded_at", ""),
            ("processed_at", ""),
            ("processing_status", "UPLOADED"),
            ("ocr_status", "PENDING"),
            ("extracted_text", ""),
            ("page_text", ""),
            ("processing_error", ""),
            ("evidence_summary", ""),
        ):
            if name not in document_columns:
                db.execute(
                    f"ALTER TABLE documents ADD COLUMN {name} TEXT DEFAULT '{default}'"
                )

        # Add columns if this database was created by an older version.
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(sla_settings)")
        }
        for name, default in (
            ("remaining_time_weight", 60),
            ("priority_weight", 20),
            ("evidence_weight", 20),
        ):
            if name not in columns:
                db.execute(
                    f"ALTER TABLE sla_settings ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}"
                )

        policy_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(policy_documents)")
        }
        for name in (
            "payer",
            "service_type",
            "source_reference",
            "required_documents",
            "department",
        ):
            if name not in policy_columns:
                db.execute(f"ALTER TABLE policy_documents ADD COLUMN {name} TEXT")

        # Seed sample cases and their documents only when the cases table is empty.
        if db.execute("SELECT COUNT(*) AS count FROM cases").fetchone()["count"] == 0:
            db.executemany(
                "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    ("PA-1042", "PT-FX-2048", "Northstar Health", "Lumbar MRI", "2026-10-08", "2026-10-10T14:30:00+00:00", "PAYER PENDING", "AT RISK", "Maya Chen", "HIGH"),
                    ("PA-1041", "PT-FX-1943", "Harbor Mutual", "Specialty infusion", "2026-10-07", "2026-10-10T11:00:00+00:00", "EVIDENCE REVIEW", "BREACHED", "Jordan Lee", "HIGH"),
                    ("PA-1040", "PT-FX-1881", "SummitCare", "Knee arthroscopy", "2026-10-09", "2026-10-11T16:00:00+00:00", "INTAKE REVIEW", "ON TRACK", "Avery Patel", "MEDIUM"),
                    ("PA-1039", "PT-FX-1776", "Northstar Health", "Cardiac CT", "2026-10-06", "2026-10-12T10:00:00+00:00", "MORE INFORMATION REQUIRED", "AT RISK", "Maya Chen", "HIGH"),
                    ("PA-1038", "PT-FX-1630", "Harbor Mutual", "Physical therapy", "2026-10-03", "2026-10-09T17:00:00+00:00", "APPROVED", "COMPLETE", "Jordan Lee", "LOW"),
                ],
            )
            db.executemany(
                "INSERT INTO documents(case_id,name,status) VALUES(?,?,?)",
                [
                    ("PA-1042", "Clinical notes", "RECEIVED"),
                    ("PA-1042", "Conservative treatment history", "NEEDS REVIEW"),
                    ("PA-1041", "Infusion order", "RECEIVED"),
                    ("PA-1041", "Lab results", "MISSING"),
                    ("PA-1039", "Specialist letter", "MISSING"),
                ],
            )

        if db.execute("SELECT COUNT(*) AS count FROM sla_settings").fetchone()["count"] == 0:
            db.execute("INSERT INTO sla_settings VALUES(1,24,8,4,0,60,20,20)")

        if db.execute(
            "SELECT COUNT(*) AS count FROM escalation_role_assignments"
        ).fetchone()["count"] == 0:
            db.executemany(
                """INSERT INTO escalation_role_assignments
                   (escalation_level,responsible_role,department,effective_date,expires_at)
                   VALUES(?,?,?,?,?)""",
                [
                    ("LEVEL_1", "Prior Authorization Supervisor", "Prior Authorization", "2026-01-01", None),
                    ("LEVEL_2", "SLA Manager", "Prior Authorization", "2026-01-01", None),
                ],
            )

        if db.execute(
            "SELECT COUNT(*) AS count FROM workflow_failures"
        ).fetchone()["count"] == 0:
            db.executemany(
                """INSERT INTO workflow_failures
                   (case_id,occurred_at,error_code,error_details,workflow_state)
                   VALUES(?,?,?,?,?)""",
                [
                    ("PA-1042", "2026-10-10T09:42:00+00:00", "NOTIFY_TIMEOUT", "Payer notification timed out without delivery confirmation.", "PAYER PENDING"),
                    ("PA-1041", "2026-10-10T08:15:00+00:00", "EVIDENCE_MISSING", "Submission halted because current lab results were not verified.", "EVIDENCE REVIEW"),
                    ("PA-1039", "2026-10-10T07:30:00+00:00", "INVALID_TRANSITION", "Attempted to move from MORE INFORMATION REQUIRED directly to APPROVED.", "MORE INFORMATION REQUIRED"),
                ],
            )

        # Seed policy records only if their policy IDs are not already present.
        policy_seeds = [
            (
                "EVD-NS-MRI", "PAYER_REQUIREMENT", "Northstar lumbar MRI requirements",
                "For lumbar MRI authorization, obtain clinical notes and conservative treatment history. Received documents remain unverified until reviewed by qualified staff.",
                "2026-01-01", None, "1.0", 1, "Northstar Health", "Lumbar MRI",
                "Northstar fictional provider manual §4.2",
                '["Clinical notes", "Conservative treatment history"]', None,
            ),
            (
                "EVD-HM-INF", "DOCUMENT_CHECKLIST", "Harbor infusion checklist",
                "For specialty infusion authorization, obtain the infusion order and current lab results. Filenames and receipt metadata are not proof of clinical validity.",
                "2026-01-01", None, "1.0", 1, "Harbor Mutual", "Specialty infusion",
                "Harbor fictional checklist INF-7",
                '["Infusion order", "Lab results"]', None,
            ),
            (
                "EVD-NS-CT", "CLINICAL_DOCUMENTATION_RULE", "Northstar cardiac CT evidence rule",
                "For cardiac CT authorization, obtain a specialist letter. A qualified human reviewer must assess clinical evidence.",
                "2026-01-01", None, "1.0", 1, "Northstar Health", "Cardiac CT",
                "Northstar fictional guideline CT-3", '["Specialist letter"]', None,
            ),
        ]
        for record in policy_seeds:
            exists = db.execute(
                "SELECT id FROM policy_documents WHERE policy_id=? AND version=?",
                (record[0], record[6]),
            ).fetchone()
            if not exists:
                db.execute(
                    """INSERT INTO policy_documents
                       (policy_id,document_type,title,content,effective_date,expires_at,
                        version,approved,payer,service_type,source_reference,
                        required_documents,department)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    record,
                )

        recovery_seeds = [
            ("REC-001", "RECOVERY_PROCEDURE", "Approved workflow recovery procedure",
             "For NOTIFY_TIMEOUT, confirm the recipient and service availability, then permit one simulated retry. For EVIDENCE_MISSING, stop and create a manual evidence follow-up. For INVALID_TRANSITION, do not retry; require operations review. For SERVICE_UNAVAILABLE, check availability and permit one retry.",
             "2026-01-01", None, "1.0", 1, "Recovery handbook §3"),
            ("RETRY-001", "RETRY_POLICY", "Bounded retry policy",
             "Permit no more than two recovery attempts for the same case and error code. Never repeat an action after confirmed success. Sensitive status and evidence actions require human approval.",
             "2026-01-01", None, "1.0", 1, "Recovery handbook §4"),
            ("ERR-001", "ERROR_DEFINITION", "Workflow error definitions",
             "EVIDENCE_MISSING means required evidence is absent or unverified. NOTIFY_TIMEOUT means delivery was not confirmed. INVALID_TRANSITION means the status change is not allowed. SERVICE_UNAVAILABLE means a dependency could not be reached.",
             "2026-01-01", None, "1.0", 1, "Error catalog §2"),
            ("MANUAL-001", "MANUAL_FALLBACK", "Manual recovery fallback",
             "When recovery cannot be verified or no approved procedure applies, create an open manual follow-up for the Prior Authorization Supervisor and stop automated action.",
             "2026-01-01", None, "1.0", 1, "Recovery handbook §7"),
        ]
        for record in recovery_seeds:
            exists = db.execute(
                "SELECT id FROM policy_documents WHERE policy_id=? AND version=?",
                (record[0], record[6]),
            ).fetchone()
            if not exists:
                db.execute(
                    """INSERT INTO policy_documents
                       (policy_id,document_type,title,content,effective_date,expires_at,
                        version,approved,source_reference)
                       VALUES(?,?,?,?,?,?,?, ?, ?)""",
                    record,
                )

        sla_seeds = [
            ("SLA-001", "SLA_POLICY", "Authorization deadline policy",
             "Calculate remaining time from the recorded case deadline. A passed deadline is BREACHED. Eight hours or less is HIGH risk. Twenty-four hours or less is MEDIUM risk. More than twenty-four hours is LOW unless unresolved evidence exists.",
             "2026-01-01", None, "1.0"),
            ("WF-001", "WORKFLOW_GUIDELINE", "Evidence workflow guideline",
             "Missing, received but unverified, or needs-review evidence is an unresolved dependency. Clinical evidence requires human verification. Recommendations must never approve or deny an authorization.",
             "2026-01-01", None, "1.0"),
            ("ESC-001", "ESCALATION_RULE", "SLA escalation rule",
             "Use configured escalation thresholds. Level 1 routes to the Prior Authorization Supervisor. Level 2 routes to the SLA Manager. Human approval is required before notification, duplicate notifications must be suppressed, and delivery may only be reported after service confirmation.",
             "2026-01-01", None, "1.0"),
            ("ROLE-001", "ROLE_RESPONSIBILITY", "Escalation role responsibilities",
             "Prior Authorization Supervisors own level 1 escalation review. SLA Managers own level 2 and breached-case review. Coordinators continue monitoring when no threshold is reached.",
             "2026-01-01", None, "1.0"),
            ("NOTIFY-001", "NOTIFICATION_PROCEDURE", "Escalation notification procedure",
             "Require human approval before sending escalation notifications. Suppress duplicates for the same case and level. Prototype notifications are simulated and must be labelled simulated.",
             "2026-01-01", None, "1.0"),
        ]
        for record in sla_seeds:
            exists = db.execute(
                "SELECT id FROM policy_documents WHERE policy_id=? AND version=?",
                (record[0], record[6]),
            ).fetchone()
            if not exists:
                db.execute(
                    """INSERT INTO policy_documents
                       (policy_id,document_type,title,content,effective_date,expires_at,
                        version,approved)
                       VALUES(?,?,?,?,?,?,?,1)""",
                    record,
                )
