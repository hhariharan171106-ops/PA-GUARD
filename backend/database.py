import sqlite3
try:
    from .config import DB_PATH, DOCUMENT_STORAGE_DIR
except ImportError:
    from config import DB_PATH, DOCUMENT_STORAGE_DIR






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
                verification_status TEXT DEFAULT 'MOCK_DEMO',
                required_categories TEXT DEFAULT '[]',
                specific_evidence_requirements TEXT DEFAULT '[]',
                UNIQUE(policy_id, version)
            );

            CREATE TABLE IF NOT EXISTS policy_criteria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                criterion_id TEXT NOT NULL,
                criterion_text TEXT NOT NULL,
                service_scope TEXT,
                source_url TEXT,
                source_version TEXT,
                supporting_excerpt TEXT,
                source_locator TEXT,
                verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                verification_notes TEXT,
                verified_by TEXT,
                verified_at TEXT,
                UNIQUE(policy_id, policy_version, criterion_id)
            );

            CREATE TABLE IF NOT EXISTS policy_source_verification_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                attempted_by TEXT NOT NULL,
                source_url TEXT NOT NULL,
                authoritative_title TEXT NOT NULL DEFAULT '',
                authoritative_version TEXT NOT NULL DEFAULT '',
                source_effective_date TEXT,
                source_status TEXT NOT NULL,
                attempt_status TEXT NOT NULL,
                source_sha256 TEXT,
                criteria_json TEXT NOT NULL,
                verification_note TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS case_evidence_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                criterion_id TEXT NOT NULL,
                document_id INTEGER,
                assessment_status TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'PENDING',
                reviewer_id TEXT,
                reviewed_at TEXT,
                review_notes TEXT NOT NULL DEFAULT '',
                source_locator TEXT,
                supporting_excerpt TEXT,
                audit_event_id INTEGER,
                created_at TEXT NOT NULL
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
                document_id INTEGER,
                model_version TEXT,
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

            CREATE TABLE IF NOT EXISTS case_workflow_state (
                case_id TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                result_json TEXT NOT NULL,
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

            CREATE TABLE IF NOT EXISTS pa_staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('SPECIALIST','SUPERVISOR','MANAGER','ADMIN')),
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                available INTEGER NOT NULL DEFAULT 1,
                max_workload INTEGER NOT NULL DEFAULT 10,
                service_types TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_staff_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_id INTEGER NOT NULL REFERENCES pa_staff(id),
                actor_id INTEGER NOT NULL REFERENCES pa_staff(id),
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_sessions (
                token_hash TEXT PRIMARY KEY,
                staff_id INTEGER NOT NULL REFERENCES pa_staff(id),
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_case_assignments (
                case_id TEXT PRIMARY KEY REFERENCES cases(id),
                specialist_id INTEGER REFERENCES pa_staff(id),
                supervisor_id INTEGER REFERENCES pa_staff(id),
                manager_id INTEGER REFERENCES pa_staff(id),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_assignment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(id),
                actor_id INTEGER NOT NULL REFERENCES pa_staff(id),
                from_staff_id INTEGER REFERENCES pa_staff(id),
                to_staff_id INTEGER NOT NULL REFERENCES pa_staff(id),
                role TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_escalations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(id),
                from_staff_id INTEGER NOT NULL REFERENCES pa_staff(id),
                from_role TEXT NOT NULL,
                to_staff_id INTEGER NOT NULL REFERENCES pa_staff(id),
                to_role TEXT NOT NULL,
                reason TEXT NOT NULL,
                explanation TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ROUTED','ACCEPTED','CLOSED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(id),
                actor_id INTEGER NOT NULL REFERENCES pa_staff(id),
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                old_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(id),
                actor_id INTEGER NOT NULL REFERENCES pa_staff(id),
                role TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_approval_policy (
                id INTEGER PRIMARY KEY CHECK(id=1),
                manager_final_required INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS pa_submission_readiness (
                case_id TEXT PRIMARY KEY REFERENCES cases(id) ON DELETE RESTRICT,
                version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('NOT_READY','NEEDS_HUMAN_REVIEW','READY_FOR_REVIEW')),
                fingerprint TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                evaluated_by INTEGER NOT NULL REFERENCES pa_staff(id),
                evaluated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_submission_approvals (
                case_id TEXT PRIMARY KEY REFERENCES cases(id) ON DELETE RESTRICT,
                readiness_version INTEGER NOT NULL,
                approved_by INTEGER NOT NULL REFERENCES pa_staff(id),
                reason TEXT NOT NULL,
                approved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pa_submission_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
                idempotency_key TEXT NOT NULL,
                adapter_type TEXT NOT NULL CHECK(adapter_type IN ('DEMO','PAYER')),
                status TEXT NOT NULL,
                reference_id TEXT,
                response_summary TEXT NOT NULL,
                attempted_by INTEGER NOT NULL REFERENCES pa_staff(id),
                attempted_at TEXT NOT NULL,
                UNIQUE(case_id,idempotency_key)
            );
            """
        )

        if db.execute("SELECT COUNT(*) AS count FROM pa_staff").fetchone()["count"] == 0:
            # Local jury demo accounts; passwords are PBKDF2 hashes, never plaintext.
            import hashlib
            import os
            demo_users = [
                ("specialist", "Maya Chen", "SPECIALIST", "SpecialistDemo1"),
                ("supervisor", "Jordan Lee", "SUPERVISOR", "SupervisorDemo1"),
                ("manager", "Avery Patel", "MANAGER", "ManagerDemo1"),
                ("admin", "Taylor Morgan", "ADMIN", "AdminDemo1"),
            ]
            for username, name, role, password in demo_users:
                salt = os.urandom(16)
                digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
                encoded = f"pbkdf2_sha256${salt.hex()}${digest.hex()}"
                db.execute("INSERT INTO pa_staff(username,display_name,role,password_hash,created_at) VALUES(?,?,?,?,datetime('now'))", (username,name,role,encoded))
        db.execute("INSERT OR IGNORE INTO pa_approval_policy(id,manager_final_required) VALUES(1,1)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_pa_history_case ON pa_reviews(case_id,created_at)")
        staff_columns = {row["name"] for row in db.execute("PRAGMA table_info(pa_staff)")}
        if "service_types" not in staff_columns:
            db.execute("ALTER TABLE pa_staff ADD COLUMN service_types TEXT NOT NULL DEFAULT '[]'")
        db.execute("CREATE INDEX IF NOT EXISTS idx_pa_staff_history_staff ON pa_staff_history(staff_id,created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_pa_assignments_specialist ON pa_case_assignments(specialist_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_pa_assignments_supervisor ON pa_case_assignments(supervisor_id)")

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
        if "submission_id" not in document_columns:
            db.execute("ALTER TABLE documents ADD COLUMN submission_id TEXT")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_submission_id "
            "ON documents(submission_id) WHERE submission_id IS NOT NULL"
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
            "verification_status",
            "required_categories",
            "specific_evidence_requirements",
            "source_url",
            "authoritative_title",
            "authoritative_version",
            "source_verified_by",
            "source_verified_at",
            "source_verification_note",
            "source_requirement_evidence",
        ):
            if name not in policy_columns:
                db.execute(f"ALTER TABLE policy_documents ADD COLUMN {name} TEXT")
        feedback_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(evidence_review_feedback)")
        }
        if "document_id" not in feedback_columns:
            db.execute("ALTER TABLE evidence_review_feedback ADD COLUMN document_id INTEGER")
        if "model_version" not in feedback_columns:
            db.execute("ALTER TABLE evidence_review_feedback ADD COLUMN model_version TEXT")
        verification_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(policy_source_verification_attempts)")
        }
        for name, definition in (
            ("authoritative_title", "TEXT NOT NULL DEFAULT ''"),
            ("authoritative_version", "TEXT NOT NULL DEFAULT ''"),
            ("source_effective_date", "TEXT"),
        ):
            if name not in verification_columns:
                db.execute(f"ALTER TABLE policy_source_verification_attempts ADD COLUMN {name} {definition}")
        db.execute("UPDATE policy_documents SET verification_status='MOCK_DEMO' WHERE verification_status IS NULL")
        # This demo row was labeled public without source-level support for its
        # prior-therapy/red-flag checklist. Keep the record for audit, but remove
        # it from active policy retrieval until an authorized source review.
        downgraded_policy = db.execute(
            """UPDATE policy_documents
               SET verification_status='MOCK_DEMO', approved=0,
                   effective_date='2018-04-10',
                   source_reference='https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?NCDId=177',
                   source_url=NULL, authoritative_title=NULL, authoritative_version=NULL,
                   source_verified_by=NULL, source_verified_at=NULL,
                   source_verification_note=NULL, source_requirement_evidence=NULL
               WHERE policy_id='EVD-CMS-MRI' AND version='1.0'
                 AND (verification_status!='MOCK_DEMO' OR approved!=0
                      OR effective_date!='2018-04-10'
                      OR source_url IS NOT NULL OR authoritative_title IS NOT NULL
                      OR authoritative_version IS NOT NULL OR source_verified_by IS NOT NULL
                      OR source_verified_at IS NOT NULL OR source_verification_note IS NOT NULL
                      OR source_requirement_evidence IS NOT NULL)"""
        )
        if downgraded_policy.rowcount:
            db.execute(
                """INSERT INTO audit_events
                   (case_id,timestamp,actor,action,detail,decision_type)
                   VALUES('SYSTEM',datetime('now'),'System migration',
                   'PAYER_POLICY_VERIFICATION_DOWNGRADED',
                   'EVD-CMS-MRI v1.0 was retained as unapproved MOCK_DEMO. CMS NCD 220.2 did not substantiate its stored lumbar MRI checklist; source-version date normalized to 2018-04-10 and public-verification metadata cleared.',
                   'SYSTEM_MIGRATION')"""
            )

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
                "MOCK_DEMO",
                '["Clinical Notes", "Treatment History"]',
                '["Clinical notes", "Conservative treatment history"]',
            ),
            (
                "EVD-HM-INF", "DOCUMENT_CHECKLIST", "Harbor infusion checklist",
                "For specialty infusion authorization, obtain the infusion order and current lab results. Filenames and receipt metadata are not proof of clinical validity.",
                "2026-01-01", None, "1.0", 1, "Harbor Mutual", "Specialty infusion",
                "Harbor fictional checklist INF-7",
                '["Infusion order", "Lab results"]', None,
                "MOCK_DEMO",
                '["Other Supporting Evidence", "Laboratory Reports"]',
                '["Infusion order", "Lab results"]',
            ),
            (
                "EVD-NS-CT", "CLINICAL_DOCUMENTATION_RULE", "Northstar cardiac CT evidence rule",
                "For cardiac CT authorization, obtain a specialist letter. A qualified human reviewer must assess clinical evidence.",
                "2026-01-01", None, "1.0", 1, "Northstar Health", "Cardiac CT",
                "Northstar fictional guideline CT-3", '["Specialist letter"]', None,
                "MOCK_DEMO",
                '["Medical Necessity Documentation"]',
                '["Specialist letter"]',
            ),
            (
                "EVD-CMS-MRI", "PAYER_REQUIREMENT", "CMS NCD 220.2 MRI scope (draft demo record)",
                "Draft only. CMS NCD 220.2 is a national MRI coverage policy and does not substantiate this prototype's prior-authorization document checklist. Do not use as an active requirement until each evidence requirement and scope is reviewed against the authoritative source.",
                "2018-04-10", None, "1.0", 0, "CMS Medicare", "Lumbar MRI",
                "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?NCDId=177",
                '["Clinical documentation of neurological deficit", "Prior conservative therapy records"]', None,
                "MOCK_DEMO",
                '["Clinical Notes", "Treatment History"]',
                '["Clinical documentation of neurological deficit", "Prior conservative therapy records"]',
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
                        required_documents,department,verification_status,
                        required_categories,specific_evidence_requirements)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
