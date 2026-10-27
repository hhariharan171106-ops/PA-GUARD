import csv
import hashlib
import json
import os
import re
import sqlite3
import uuid
from html import unescape
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener

import PIL.Image
import pytesseract
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pdf2image import convert_from_path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pypdf import PdfReader
from pytesseract.pytesseract import TesseractNotFoundError
from pdf2image.exceptions import PDFInfoNotInstalledError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from agents import run_sla_agent, run_sla_rag
from approval_api import case_access, current_user, router as approval_router, user_from_authorization
from case_deadline import InvalidCaseDeadline, validate_case_deadline
from case_workflow import (
    WorkflowRetryUnavailableError,
    case_explanation_status,
    request_case_explanation,
    run_case_workflow,
)
from evidence_agents import run_evidence_rag
from evidence_prediction_model import EVIDENCE_MODEL_VERSION, predict_evidence_gaps
from escalation_agents import run_escalation_rag
from recovery_agents import run_recovery_rag
from submission_api import router as submission_router
from database import DOCUMENT_STORAGE_DIR, connect, initialize
from rag import index_policies, ollama_status
from sla_risk_model import (
    InvalidRiskFeatures,
    ModelUnavailableError,
    predict_sla_breach_risk,
)
from security import PUBLIC_ROUTES, allowed_roles, enforce_resource_scope
from services.fastest_safe_path import Plan, build_plan

app = FastAPI(title="PA Guard API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"https?://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(approval_router)
app.include_router(submission_router)


@app.middleware("http")
async def enforce_api_security(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    if not path.startswith("/api/") or method == "OPTIONS" or (method, path) in PUBLIC_ROUTES:
        return await call_next(request)
    roles = allowed_roles(method, path)
    if not roles:
        response = JSONResponse({"detail": "Your role cannot access this API operation"}, status_code=403)
    else:
        try:
            user = user_from_authorization(request.headers.get("authorization"))
            enforce_resource_scope(user, method, path)
            if user["role"] not in roles:
                raise HTTPException(403, "Your role cannot access this API operation")
        except HTTPException as error:
            response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
        else:
            request.state.current_user = user
            return await call_next(request)
    origin = request.headers.get("origin")
    if origin:
        response.headers["access-control-allow-origin"] = origin
        response.headers["vary"] = "Origin"
    return response

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_CATEGORIES = [
    "Clinical Notes",
    "Clinical Reports",
    "Imaging Report",
    "Imaging Reports",
    "Treatment History",
    "Treatment Reports",
    "Laboratory Reports",
    "Medical Necessity Documentation",
    "Medical Necessity Documents",
    "Prescription or Medication Records",
    "Prescription",
    "Other Supporting Evidence",
]
ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
SYSTEM_DOCUMENT_ROOT = DOCUMENT_STORAGE_DIR
POPPLER_PATH = os.environ.get("POPPLER_PATH") or None


def _configure_tesseract() -> None:
    configured_path = os.environ.get("TESSERACT_PATH")
    candidates = ([Path(configured_path)] if configured_path else []) + [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            return


_configure_tesseract()


def _require_tesseract() -> None:
    try:
        pytesseract.get_tesseract_version()
    except TesseractNotFoundError as error:
        raise RuntimeError(
            "Tesseract OCR is unavailable. Install Tesseract OCR and either add "
            "tesseract.exe to PATH or set TESSERACT_PATH to its full path, then "
            "restart the backend."
        ) from error


def _read_magic_bytes(file_bytes: bytes) -> tuple[str | None, str | None]:
    if file_bytes.startswith(b"%PDF"):
        return ".pdf", "application/pdf"
    if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if file_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if file_bytes.startswith(b"II*\x00") or file_bytes.startswith(b"MM\x00*"):
        return ".tiff", "image/tiff"
    return None, None


def _safe_upload_path(case_id: str, original_name: str) -> Path:
    candidate = Path(original_name).name
    if not candidate or candidate in {".", ".."}:
        raise HTTPException(400, "Document name is invalid")
    suffix = Path(candidate).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Unsupported file type. Upload PDF, PNG, JPEG, or TIFF files only.")
    root = SYSTEM_DOCUMENT_ROOT.resolve()
    case_root = (root / case_id).resolve()
    if root not in case_root.parents:
        raise HTTPException(400, "Unsafe file path for upload")
    case_root.mkdir(parents=True, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex}{suffix}"
    requested = (case_root / unique_name).resolve()
    if root not in requested.parents:
        raise HTTPException(400, "Unsafe file path for upload")
    return requested


def _extract_pdf_text(file_path: Path) -> tuple[str, list[str]]:
    page_texts: list[str] = []
    reader = PdfReader(str(file_path))
    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        if not page_text.strip():
            _require_tesseract()
            try:
                images = convert_from_path(
                    str(file_path),
                    first_page=page_number,
                    last_page=page_number,
                    dpi=200,
                    thread_count=1,
                    poppler_path=POPPLER_PATH,
                )
            except PDFInfoNotInstalledError as error:
                raise RuntimeError(
                    "Scanned PDF OCR requires Poppler. Install Poppler for Windows "
                    "and add its Library\\bin directory to PATH, or set POPPLER_PATH "
                    "to that directory, then restart the backend."
                ) from error
            if not images:
                page_texts.append("")
                continue
            page_text = pytesseract.image_to_string(images[0]).strip()
            images[0].close()
        page_texts.append(page_text.strip())
    if not any(page_text.strip() for page_text in page_texts):
        raise RuntimeError("No readable text could be extracted from the PDF")
    return "\n\n".join(page_text for page_text in page_texts if page_text.strip()).strip(), page_texts


def _extract_image_text(file_path: Path) -> str:
    _require_tesseract()
    with PIL.Image.open(file_path) as image:
        return pytesseract.image_to_string(image).strip()


def _extract_text_from_file(file_path: Path, mime_type: str) -> tuple[str, list[str]]:
    try:
        if mime_type == "application/pdf":
            text, pages = _extract_pdf_text(file_path)
            if text:
                return text, pages
            return "", [""]
        if mime_type.startswith("image/"):
            text = _extract_image_text(file_path)
            return text, [text]
        raise ValueError("Unsupported document type")
    except Exception as error:
        raise RuntimeError(f"OCR failed: {error}") from error


class CaseCreate(BaseModel):
    patient_reference: str
    payer: str
    service: str
    deadline: str
    coordinator: str

    @field_validator("deadline")
    @classmethod
    def require_unambiguous_deadline(cls, value: str) -> str:
        try:
            return validate_case_deadline(value)
        except InvalidCaseDeadline as error:
            raise ValueError(str(error)) from error

class CaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_reference: str | None = Field(default=None, min_length=1, max_length=160)
    payer: str | None = Field(default=None, min_length=1, max_length=120)
    service: str | None = Field(default=None, min_length=1, max_length=160)
    deadline: str | None = None
    priority: str | None = None
    coordinator: str | None = Field(default=None, min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    expected_values: dict[str, str]

    @field_validator("patient_reference", "payer", "service", "coordinator")
    @classmethod
    def nonblank_case_edit_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("Case fields cannot be blank")
        return normalized

    @field_validator("reason")
    @classmethod
    def nonblank_case_update_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A nonblank correction reason is required")
        return value.strip()

class HumanAction(BaseModel):
    actor: str
    note: str

class EscalationRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    explanation: str = Field(min_length=1, max_length=2000)
    to_staff_id: int | None = None
    to_role: str | None = None

    @field_validator("reason", "explanation")
    @classmethod
    def nonblank_escalation_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Escalation reason and explanation must not be blank")
        return value.strip()

class AgentRun(BaseModel):
    actor: str = "Current user"
    approve_notification: bool = False

class RecoveryRun(BaseModel):
    actor: str
    error_code: str | None = None
    approve_recovery: bool = False
    preview_only: bool = False

class SlaSettingsUpdate(BaseModel):
    warning_hours: int
    urgent_hours: int
    escalation_one_hours: int
    escalation_two_hours: int
    remaining_time_weight: int
    priority_weight: int
    evidence_weight: int

class PolicyCreate(BaseModel):
    policy_id: str
    document_type: str
    title: str
    content: str
    effective_date: str
    expires_at: str | None = None
    version: str


class PolicyUpdate(BaseModel):
    document_type: str
    title: str
    content: str
    effective_date: str
    expires_at: str | None = None
    version: str


class PolicyApproval(BaseModel):
    actor: str
    approved: bool

class RequirementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: str
    document_type: str
    title: str
    payer: str
    service_type: str
    required_documents: list[str]
    content: str
    source_reference: str
    effective_date: str
    expires_at: str | None = None
    version: str

class RequirementUpdate(BaseModel):
    document_type: str
    title: str
    payer: str
    service_type: str
    required_documents: list[str]
    content: str
    source_reference: str
    effective_date: str
    expires_at: str | None = None
    version: str

class EscalationPolicyCreate(BaseModel):
    policy_id: str
    document_type: str
    title: str
    content: str
    department: str
    source_reference: str
    effective_date: str
    expires_at: str | None = None
    version: str

class EscalationPolicyUpdate(BaseModel):
    document_type: str
    title: str
    content: str
    department: str
    source_reference: str
    effective_date: str
    expires_at: str | None = None
    version: str

class RoleAssignmentCreate(BaseModel):
    escalation_level: str
    responsible_role: str
    department: str
    effective_date: str
    expires_at: str | None = None

class RoleAssignmentUpdate(RoleAssignmentCreate):
    pass

class EvidenceReviewFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_evidence: str = Field(min_length=1, max_length=200)
    document_id: int | None = Field(default=None, gt=0)
    ml_prediction: Literal[
        "PREDICTED_MISSING", "NOT_PREDICTED_MISSING", "UNKNOWN"
    ] | None = None

class EvidenceReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_status: Literal["CONFIRMED", "REJECTED"]
    human_outcome: Literal["PRESENT", "MISSING", "UNKNOWN", "NEEDS_REVIEW"] | None = None
    reviewer_identifier: str = Field(min_length=1, max_length=100)

class SourceCriterionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: str = Field(min_length=1, max_length=500)
    source_location: str = Field(min_length=1, max_length=500)
    supporting_excerpt: str = Field(min_length=1, max_length=4000)

class SourceVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str = Field(min_length=8, max_length=2048)
    authoritative_title: str = Field(min_length=1, max_length=500)
    authoritative_version: str = Field(min_length=1, max_length=200)
    source_effective_date: str | None = None
    verification_note: str = Field(min_length=10, max_length=2000)
    requirements_checked_against_source: bool
    requirement_evidence: list[SourceCriterionEvidence]

    @field_validator("source_url")
    @classmethod
    def require_https_source(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Source URL must be an HTTPS URL without embedded credentials")
        return value.strip()

class SourceVerificationApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=10, max_length=1000)

class EvidenceTrailReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: int | None = None
    assessment_status: Literal["MATCH_POSSIBLE", "NOT_FOUND", "UNCERTAIN", "CONFLICTING"]
    review_status: Literal["HUMAN_CONFIRMED", "HUMAN_REJECTED"]
    supporting_excerpt: str = Field(default="", max_length=2000)
    source_locator: str = Field(default="", max_length=300)
    review_notes: str = Field(min_length=1, max_length=1000)

def validate_role_assignment(values: dict, assignment_id: int | None = None) -> None:
    required = ("escalation_level", "responsible_role", "department", "effective_date")
    if not all(str(values[field]).strip() for field in required):
        raise HTTPException(400, "Complete all required role assignment fields")
    if values["escalation_level"] not in {"LEVEL_1", "LEVEL_2"}:
        raise HTTPException(400, "Escalation level must be LEVEL_1 or LEVEL_2")
    try:
        effective = date.fromisoformat(values["effective_date"])
        expires = date.fromisoformat(values["expires_at"]) if values["expires_at"] else None
    except ValueError as error:
        raise HTTPException(400, "Assignment dates must use YYYY-MM-DD") from error
    if expires and expires < effective:
        raise HTTPException(400, "Retirement date cannot precede the effective date")
    with connect() as db:
        conflict = db.execute("""
            SELECT id FROM escalation_role_assignments
            WHERE escalation_level=? AND department=? AND id != ?
            AND effective_date <= COALESCE(?, '9999-12-31')
            AND COALESCE(expires_at, '9999-12-31') >= ? LIMIT 1
        """,
            (values["escalation_level"], values["department"], assignment_id or -1, values["expires_at"], values["effective_date"])).fetchone()
    if conflict:
        raise HTTPException(409, "This assignment overlaps another assignment for the same department and escalation level")

def audit(case_id: str, actor: str, action: str, detail: str, decision_type: str="HUMAN") -> None:
    with connect() as db:
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)", (case_id,datetime.now(timezone.utc).isoformat(),actor,action,detail,decision_type))

DOCUMENT_RESPONSE_FIELDS = (
    "id",
    "case_id",
    "name",
    "status",
    "category",
    "original_filename",
    "mime_type",
    "file_size",
    "uploaded_at",
    "processed_at",
    "processing_status",
    "ocr_status",
    "extracted_text",
    "processing_error",
    "evidence_summary",
    "reviewed_by",
    "reviewed_at",
)

def _document_response(document: dict) -> dict:
    return {field: document[field] for field in DOCUMENT_RESPONSE_FIELDS if field in document}

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "PA Guard API"}

@app.get("/api/agents/status")
def agent_status() -> dict:
    try:
        with connect() as db:
            db.execute("SELECT 1")
    except sqlite3.Error:
        database_status = "unavailable"
    else:
        database_status = "ready"
    return {
        "api_status": "ready" if database_status == "ready" else "degraded",
        "database_status": database_status,
        "agents": {
            "sla": "ready",
            "evidence": "ready",
            "escalation": "ready",
            "recovery": "ready",
        },
        "ollama": ollama_status(),
    }

@app.on_event("startup")
def startup() -> None:
    initialize()
    index_policies()


def _authorize_case_workflow(case_id: str, user: dict) -> None:
    if user["role"] not in {"SPECIALIST", "SUPERVISOR", "MANAGER"}:
        raise HTTPException(403, "Your role cannot run case workflow analysis")
    with connect() as db:
        case_access(db, case_id, user)


@app.post("/api/agents/cases/{case_id}/workflow")
def run_case_workflow_endpoint(case_id: str, _payload: AgentRun, user: dict = Depends(current_user)) -> dict:
    _authorize_case_workflow(case_id, user)
    try:
        return run_case_workflow(case_id, user["display_name"])
    except ValueError as error:
        raise HTTPException(404, "Case not found") from error


@app.post("/api/agents/cases/{case_id}/workflow/retry")
def retry_case_workflow_endpoint(case_id: str, _payload: AgentRun, user: dict = Depends(current_user)) -> dict:
    _authorize_case_workflow(case_id, user)
    try:
        return run_case_workflow(case_id, user["display_name"], retry_failed_only=True)
    except WorkflowRetryUnavailableError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(404, "Case not found") from error


@app.get("/api/agents/cases/{case_id}/workflow/explanation")
async def get_case_explanation(case_id: str, user: dict = Depends(current_user)) -> dict:
    _authorize_case_workflow(case_id, user)
    try:
        return await case_explanation_status(case_id)
    except ValueError as error:
        raise HTTPException(404, "Case workflow results not found") from error


@app.post("/api/agents/cases/{case_id}/workflow/explanation")
async def start_case_explanation(case_id: str, user: dict = Depends(current_user)) -> dict:
    _authorize_case_workflow(case_id, user)
    try:
        return await request_case_explanation(case_id)
    except ValueError as error:
        raise HTTPException(404, "Case workflow results not found") from error

@app.get("/api/cases")
def list_cases(user: dict = Depends(current_user)) -> list[dict]:
    with connect() as db:
        query = """SELECT c.* FROM cases c
                   LEFT JOIN pa_case_assignments a ON a.case_id=c.id
                   WHERE c.status NOT IN ('APPROVED','DENIED')"""
        params: tuple = ()
        if user["role"] == "SPECIALIST":
            query += " AND a.specialist_id=?"; params = (user["id"],)
        elif user["role"] == "SUPERVISOR":
            query += " AND a.supervisor_id=?"; params = (user["id"],)
        query += " ORDER BY c.deadline"
        return [dict(row) for row in db.execute(query, params)]

@app.post("/api/cases")
def create_case(payload: CaseCreate) -> dict:
    with connect() as db:
        number = db.execute("SELECT COUNT(*) AS count FROM cases").fetchone()["count"] + 1043
        case = {"id":f"PA-{number}","patient_reference":payload.patient_reference,"payer":payload.payer,"service":payload.service,"submission_date":datetime.now(timezone.utc).date().isoformat(),"deadline":payload.deadline,"status":"NEW","risk":"ON TRACK","coordinator":payload.coordinator,"priority":"MEDIUM"}
        db.execute("INSERT INTO cases VALUES(:id,:patient_reference,:payer,:service,:submission_date,:deadline,:status,:risk,:coordinator,:priority)",case)
    audit(case["id"],payload.coordinator,"CASE_CREATED","Case created for intake review")
    return case

@app.patch("/api/cases/{case_id}")
def update_case(case_id: str, payload: CaseUpdate, user: dict = Depends(current_user)) -> dict:
    values = payload.model_dump(exclude={"reason", "expected_values"}, exclude_none=True)
    if not values: raise HTTPException(400,"No changes supplied")
    if set(payload.expected_values) != set(values):
        raise HTTPException(422, "expected_values must include exactly the fields being corrected")
    if "deadline" in values:
        try:
            parsed_deadline = datetime.fromisoformat(values["deadline"].replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise HTTPException(422, "Deadline must be a valid ISO-8601 timestamp with timezone") from error
        if parsed_deadline.tzinfo is None:
            raise HTTPException(422, "Deadline must include a timezone")
    if "priority" in values and values["priority"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise HTTPException(422, "Priority must be LOW, MEDIUM, or HIGH")
    with connect() as db:
        current = db.execute("SELECT * FROM cases WHERE id=?",(case_id,)).fetchone()
        if not current: raise HTTPException(404,"Case not found")
        if any(current[field] != expected for field, expected in payload.expected_values.items()):
            raise HTTPException(409,"Case changed since it was opened; refresh before saving")
        for field,value in values.items(): db.execute(f"UPDATE cases SET {field}=? WHERE id=?",(value,case_id))
        updated = dict(db.execute("SELECT * FROM cases WHERE id=?",(case_id,)).fetchone())
    audit(case_id,user["username"],"CASE_UPDATED",json.dumps({"reason":payload.reason,"before":payload.expected_values,"after":values},sort_keys=True))
    return updated

@app.get("/api/cases/{case_id}")
def case_detail(case_id: str) -> dict:
    with connect() as db:
        case = db.execute("SELECT * FROM cases WHERE id=?",(case_id,)).fetchone()
        if not case: raise HTTPException(404,"Case not found")
        documents = [_document_response(dict(row)) for row in db.execute("SELECT * FROM documents WHERE case_id=?",(case_id,))]
    return {"case":dict(case),"documents":documents,"agent_status":"SLA RAG CONNECTED — RECOMMENDATION ONLY"}


@app.get("/api/cases/{case_id}/fastest-safe-path")
def fastest_safe_path(case_id: str, user: dict = Depends(current_user)) -> Plan:
    """Read-only plan; existing middleware and case_access enforce scope first."""
    audit_enabled = True
    with connect() as db:
        case = case_access(db, case_id, user)
        documents = [dict(row) for row in db.execute(
            "SELECT id,name,status,category,processing_status,ocr_status FROM documents WHERE case_id=? ORDER BY id", (case_id,)
        )]
        policies = db.execute("SELECT * FROM policy_documents WHERE payer=? ORDER BY effective_date DESC,policy_id,version DESC", (case["payer"],)).fetchall()
        applicable = [p for p in policies if not p["service_type"] or p["service_type"].casefold() in case["service"].casefold() or case["service"].casefold() in p["service_type"].casefold()]
        policy = next((p for p in applicable if p["approved"] and p["verification_status"] == "VERIFIED_PUBLIC"), applicable[0] if applicable else None)
        policy_state = "UNKNOWN" if policy is None else "UNVERIFIED"
        criteria_states = []
        if policy is not None:
            policy_state = "VERIFIED" if policy["approved"] and policy["verification_status"] == "VERIFIED_PUBLIC" else ("UNSUPPORTED" if policy["verification_status"] == "UNSUPPORTED" else "UNVERIFIED")
            criteria = db.execute("SELECT * FROM policy_criteria WHERE policy_id=? AND policy_version=? ORDER BY criterion_id", (policy["policy_id"], policy["version"])).fetchall()
            for criterion in criteria:
                review = db.execute("SELECT * FROM case_evidence_reviews WHERE case_id=? AND policy_id=? AND policy_version=? AND criterion_id=? ORDER BY id DESC LIMIT 1", (case_id, policy["policy_id"], policy["version"], criterion["criterion_id"])).fetchone()
                if criterion["verification_status"] not in {"VERIFIED_PUBLIC", "VERIFIED"}:
                    status_value = "UNSUPPORTED_CRITERION" if criterion["verification_status"] == "UNSUPPORTED" else "UNKNOWN"
                elif review is None:
                    status_value = "UNKNOWN"
                elif review["review_status"] == "HUMAN_CONFIRMED":
                    status_value = "HUMAN_CONFIRMED"
                elif review["review_status"] == "HUMAN_REJECTED":
                    status_value = "NO_MATCH" if review["assessment_status"] == "NOT_FOUND" else "UNCERTAIN"
                elif review["assessment_status"] == "MATCH_POSSIBLE":
                    status_value = "MATCH_POSSIBLE"
                elif review["assessment_status"] == "NOT_FOUND":
                    status_value = "NO_MATCH"
                else:
                    status_value = "UNCERTAIN"
                criteria_states.append({"criterion_id": criterion["criterion_id"], "status": status_value, "document_id": review["document_id"] if review else None})
        else:
            criteria_states = []
        readiness = db.execute("SELECT status,version FROM pa_submission_readiness WHERE case_id=?", (case_id,)).fetchone()
        approval = db.execute("SELECT readiness_version FROM pa_submission_approvals WHERE case_id=?", (case_id,)).fetchone()
        approval_state = "APPROVED_FOR_SUBMISSION" if approval and readiness and approval["readiness_version"] == readiness["version"] else ("PENDING_MANAGER_APPROVAL" if readiness and readiness["status"] == "READY_FOR_REVIEW" else None)
        config_path = Path(__file__).resolve().parent / "config" / "fastest_safe_path.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            audit_enabled = bool(config.get("audit_enabled", True))
            plan = build_plan(dict(case) | {"policy_id": policy["policy_id"] if policy else None, "approval_state": approval_state}, criteria_states, documents, policy_state, user["role"], datetime.now(timezone.utc), config)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise HTTPException(503, "Fastest Safe Path configuration or case data is unavailable") from error
    if audit_enabled:
        audit(case_id, user["username"], "PLAN_GENERATED", json.dumps({"verdict": plan.verdict, "step_count": len(plan.steps)}, sort_keys=True), "SYSTEM_RECOMMENDATION")
    return plan


@app.get("/api/cases/{case_id}/evidence-trail")
def get_case_evidence_trail(case_id: str) -> dict:
    with connect() as db:
        case = db.execute("SELECT id,payer,service FROM cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        policies = db.execute(
            """SELECT policy_id,version,title,service_type,verification_status
               FROM policy_documents WHERE approved=1 AND verification_status='VERIFIED_PUBLIC'
                 AND payer=? ORDER BY effective_date DESC, policy_id, version DESC""",
            (case["payer"],),
        ).fetchall()
        applicable = [p for p in policies if not p["service_type"] or p["service_type"].casefold() in case["service"].casefold() or case["service"].casefold() in p["service_type"].casefold()]
        if not applicable:
            return {"case_id": case_id, "available": False, "policy": None, "criteria": [], "message": "No applicable verified public policy is available; policy-dependent evidence requirements remain unavailable and require human review."}
        policy = dict(applicable[0])
        rows = db.execute(
            """SELECT c.*,r.id AS review_id,r.document_id,r.assessment_status,r.review_status,
                      r.reviewer_id,r.reviewed_at,r.review_notes,r.source_locator AS reviewed_locator,
                      r.supporting_excerpt AS reviewed_excerpt,r.audit_event_id
               FROM policy_criteria c LEFT JOIN case_evidence_reviews r
                 ON r.policy_id=c.policy_id AND r.policy_version=c.policy_version
                 AND r.criterion_id=c.criterion_id AND r.case_id=?
               WHERE c.policy_id=? AND c.policy_version=? AND c.verification_status='VERIFIED_PUBLIC'
               ORDER BY c.criterion_id""",
            (case_id, policy["policy_id"], policy["version"]),
        ).fetchall()
        criteria = [dict(row) for row in rows]
        docs = [dict(row) for row in db.execute(
            "SELECT id,name,category,ocr_status,processing_status FROM documents WHERE case_id=? ORDER BY id", (case_id,)
        )]
    return {"case_id": case_id, "available": bool(criteria), "policy": policy, "criteria": criteria, "documents": docs,
            "message": "Machine text extraction is unverified; every criterion assessment requires human review." if criteria else "Policy is verified, but no criterion-level provenance is available; human review is required."}


@app.post("/api/cases/{case_id}/evidence-trail/{criterion_id}/review")
def review_case_evidence(case_id: str, criterion_id: str, payload: EvidenceTrailReview, user: dict = Depends(current_user)) -> dict:
    if user["role"] not in {"SUPERVISOR", "MANAGER"}:
        raise HTTPException(403, "A supervisor or manager must review policy evidence")
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        case = db.execute("SELECT payer,service FROM cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        criterion = db.execute(
            """SELECT c.* FROM policy_criteria c JOIN policy_documents p
                 ON p.policy_id=c.policy_id AND p.version=c.policy_version
               WHERE c.criterion_id=? AND c.verification_status='VERIFIED_PUBLIC'
                 AND p.approved=1 AND p.verification_status='VERIFIED_PUBLIC' AND p.payer=?
               ORDER BY p.effective_date DESC LIMIT 1""",
            (criterion_id, case["payer"]),
        ).fetchone()
        if not criterion:
            raise HTTPException(404, "Verified policy criterion not found for this case")
        document_text = ""
        if payload.document_id is not None:
            document = db.execute("SELECT extracted_text,page_text FROM documents WHERE id=? AND case_id=?", (payload.document_id, case_id)).fetchone()
            if not document:
                raise HTTPException(404, "Document not found for this case")
            document_text = (document["extracted_text"] or "") + " " + (document["page_text"] or "")
        if payload.supporting_excerpt and payload.supporting_excerpt.casefold() not in document_text.casefold():
            raise HTTPException(422, "The excerpt must appear in the selected document's extracted text; OCR remains unverified evidence")
        db.execute(
            """INSERT INTO case_evidence_reviews(case_id,policy_id,policy_version,criterion_id,document_id,
               assessment_status,review_status,reviewer_id,reviewed_at,review_notes,source_locator,supporting_excerpt,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (case_id, criterion["policy_id"], criterion["policy_version"], criterion_id, payload.document_id,
             payload.assessment_status, payload.review_status, user["username"], now, payload.review_notes.strip(),
             payload.source_locator.strip(), payload.supporting_excerpt.strip(), now),
        )
        review_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",
                   (case_id, now, user["username"], "CASE_EVIDENCE_REVIEWED", json.dumps({"review_id": review_id, "policy_id": criterion["policy_id"], "policy_version": criterion["policy_version"], "criterion_id": criterion_id, "review_status": payload.review_status}, sort_keys=True), "HUMAN_POLICY_REVIEW"))
        event_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("UPDATE case_evidence_reviews SET audit_event_id=? WHERE id=?", (event_id, review_id))
        result = dict(db.execute("SELECT * FROM case_evidence_reviews WHERE id=?", (review_id,)).fetchone())
    return result


@app.get("/api/cases/{case_id}/sla-risk-prediction")
def predict_case_sla_risk(case_id: str) -> dict:
    with connect() as db:
        case = db.execute(
            "SELECT id, deadline, status, payer, service FROM cases WHERE id=?",
            (case_id,),
        ).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        if case["status"] in {"APPROVED", "DENIED"}:
            raise HTTPException(
                409,
                "The case is already resolved; prospective SLA breach predictions "
                "are limited to unresolved cases.",
            )
        unresolved_count = db.execute(
            "SELECT COUNT(*) AS count FROM documents WHERE case_id=? AND status!='VERIFIED'",
            (case_id,),
        ).fetchone()["count"]
        failure_count = db.execute(
            "SELECT COUNT(*) AS count FROM workflow_failures WHERE case_id=?",
            (case_id,),
        ).fetchone()["count"]
        pending_task_count = db.execute(
            "SELECT COUNT(*) AS count FROM manual_follow_ups WHERE case_id=? AND status='OPEN'",
            (case_id,),
        ).fetchone()["count"]
    try:
        deadline = datetime.fromisoformat(case["deadline"])
    except ValueError as error:
        raise HTTPException(422, "The case deadline is not a valid ISO-8601 timestamp.") from error
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    hours_remaining = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
    if hours_remaining <= 0:
        raise HTTPException(
            409,
            "The case deadline has passed; use deterministic SLA status handling "
            "instead of a prospective breach prediction.",
        )
    try:
        return predict_sla_breach_risk(
            {
                "hours_remaining": hours_remaining,
                "payer": case["payer"],
                "service": case["service"],
                "case_status": case["status"],
                "unresolved_document_count": unresolved_count,
                "workflow_failure_count": failure_count,
                "pending_task_count": pending_task_count,
            },
            case_id,
        )
    except InvalidRiskFeatures as error:
        raise HTTPException(422, str(error)) from error
    except ModelUnavailableError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/cases/{case_id}/documents")
def list_case_documents(case_id: str) -> list[dict]:
    with connect() as db:
        case = db.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        return [_document_response(dict(row)) for row in db.execute("SELECT * FROM documents WHERE case_id=? ORDER BY id", (case_id,))]


@app.get("/api/documents")
def list_documents(user: dict = Depends(current_user)) -> list[dict]:
    with connect() as db:
        if user["role"] == "MANAGER":
            rows = db.execute("SELECT * FROM documents ORDER BY case_id,id").fetchall()
        else:
            column = "specialist_id" if user["role"] == "SPECIALIST" else "supervisor_id"
            rows = db.execute(f"SELECT d.* FROM documents d JOIN pa_case_assignments a ON a.case_id=d.case_id WHERE a.{column}=? ORDER BY d.case_id,d.id", (user["id"],)).fetchall()
        return [_document_response(dict(row)) for row in rows]


@app.get("/api/documents/{document_id}")
def get_document(document_id: int) -> dict:
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        return _document_response(dict(document))


@app.get("/api/documents/{document_id}/text")
def get_document_text(document_id: int) -> dict:
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
    return {"document_id": document_id, "case_id": document["case_id"], "text": document["extracted_text"] or ""}


def _authorize_document_upload(case_id: str, user: dict) -> None:
    if user["role"] not in {"SPECIALIST", "SUPERVISOR", "MANAGER"}:
        raise HTTPException(403, "Your role cannot upload case documents")
    try:
        with connect() as db:
            case_access(db, case_id, user)
    except HTTPException as error:
        if error.status_code == 403 and user["role"] in {"SPECIALIST", "SUPERVISOR"}:
            raise HTTPException(404, "Case not found") from error
        raise


@app.post("/api/cases/{case_id}/documents")
async def upload_document(
    case_id: str,
    file: UploadFile = File(...),
    category: str = Form(...),
    submission_id: str | None = Form(None),
    user: dict = Depends(current_user),
) -> dict:
    _authorize_document_upload(case_id, user)
    if submission_id is not None:
        submission_id = submission_id.strip()
        if not submission_id or len(submission_id) > 100:
            raise HTTPException(400, "Submission ID must contain 1 to 100 characters")
    with connect() as db:
        if not db.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone():
            raise HTTPException(404, "Case not found")
        if submission_id:
            existing = db.execute(
                "SELECT * FROM documents WHERE submission_id=?", (submission_id,)
            ).fetchone()
            if existing:
                if existing["case_id"] != case_id:
                    raise HTTPException(409, "Submission ID is already used for another case")
                return _document_response(dict(existing))
        if category not in ALLOWED_CATEGORIES:
            raise HTTPException(400, "Unsupported document category")

    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if not file_bytes:
        raise HTTPException(400, "The uploaded file is empty")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The uploaded file exceeds the 10 MB size limit")

    _, mime_type = _read_magic_bytes(file_bytes)
    original_name = Path((file.filename or "").replace("\\", "/")).name
    original_name = re.sub(r"[\x00-\x1f\x7f]", "", original_name).strip()
    file_extension = Path(original_name).suffix.lower() if original_name else ""
    if mime_type is None or (file_extension and file_extension not in ALLOWED_EXTENSIONS):
        raise HTTPException(400, "Invalid document content. Upload a real PDF, PNG, JPEG, or TIFF file.")
    if not file_extension or ALLOWED_EXTENSIONS[file_extension] != mime_type:
        raise HTTPException(400, "The filename extension does not match the uploaded document content.")
    if not original_name:
        raise HTTPException(400, "Document name is invalid")

    storage_path = _safe_upload_path(case_id, original_name)
    try:
        with storage_path.open("wb") as handle:
            handle.write(file_bytes)
    except OSError as error:
        storage_path.unlink(missing_ok=True)
        raise HTTPException(500, "Unable to store the uploaded document; check document-storage permissions.") from error

    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        try:
            cursor = db.execute(
                "INSERT INTO documents(case_id,name,status,category,original_filename,stored_filename,file_path,mime_type,file_size,uploaded_at,processing_status,ocr_status,processing_error,submission_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    category,
                    "UPLOADED",
                    category,
                    original_name,
                    storage_path.name,
                    str(storage_path),
                    mime_type,
                    len(file_bytes),
                    now,
                    "UPLOADED",
                    "PENDING",
                    "",
                    submission_id,
                ),
            )
            document_id = cursor.lastrowid
        except Exception:
            storage_path.unlink(missing_ok=True)
            raise
    return await run_in_threadpool(process_document, document_id)


@app.post("/api/cases/{case_id}/documents/batch")
async def upload_documents_batch(
    case_id: str,
    files: list[UploadFile] = File(default=[]),
    categories: list[str] = Form(default=[]),
    submission_ids: list[str] = Form(default=[]),
    user: dict = Depends(current_user),
) -> dict:
    if not files:
        raise HTTPException(400, "Select at least one document")
    if len(files) != len(categories) or len(files) != len(submission_ids):
        raise HTTPException(400, "Each file requires one category and one submission ID")
    normalized_ids = [submission_id.strip() for submission_id in submission_ids]
    if any(not submission_id or len(submission_id) > 100 for submission_id in normalized_ids):
        raise HTTPException(400, "Each submission ID must contain 1 to 100 characters")
    if len(set(normalized_ids)) != len(normalized_ids):
        raise HTTPException(400, "Submission IDs in a batch must be unique")

    _authorize_document_upload(case_id, user)
    documents: list[dict] = []
    failures: list[dict] = []
    saved_submission_ids: list[str] = []
    for file, category, submission_id in zip(files, categories, normalized_ids):
        try:
            document = await upload_document(case_id, file, category, submission_id, user)
        except HTTPException as error:
            failures.append({
                "filename": file.filename or "",
                "submission_id": submission_id,
                "status_code": error.status_code,
                "message": str(error.detail),
            })
        except Exception as error:
            failures.append({
                "filename": file.filename or "",
                "submission_id": submission_id,
                "status_code": 500,
                "message": f"Document processing failed ({type(error).__name__})",
            })
        else:
            documents.append(document)
            saved_submission_ids.append(submission_id)
            if document.get("ocr_status") != "COMPLETED":
                failures.append({
                    "filename": file.filename or "",
                    "submission_id": submission_id,
                    "status_code": 422,
                    "message": document.get("processing_error") or "OCR did not complete; human review is required.",
                })

    return {
        "case_id": case_id,
        "documents": documents,
        "saved_submission_ids": saved_submission_ids,
        "failures": failures,
        "partial_success": bool(documents and failures),
        "all_succeeded": bool(files) and not failures and len(documents) == len(files),
    }


@app.post("/api/documents/{document_id}/process")
def process_document(document_id: int) -> dict:
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        db.execute(
            "UPDATE documents SET status='RECEIVED', processing_status='PROCESSING', ocr_status='PROCESSING', processing_error='', extracted_text='', page_text='[]' WHERE id=?",
            (document_id,),
        )

    try:
        file_path = Path(document["file_path"]) if document["file_path"] else None
        if not file_path or not file_path.exists():
            raise RuntimeError("Stored document file is missing.")
        extracted_text, page_text = _extract_text_from_file(file_path, document["mime_type"] or "application/octet-stream")
        if not extracted_text.strip():
            raise RuntimeError("OCR produced no readable text.")
        now = datetime.now(timezone.utc).isoformat()
        with connect() as db:
            db.execute(
                "UPDATE documents SET status='RECEIVED', processing_status='OCR_COMPLETED', ocr_status='COMPLETED', processed_at=?, extracted_text=?, page_text=?, processing_error='' WHERE id=?",
                (now, extracted_text, json.dumps(page_text), document_id),
            )
        status = "REVIEW_REQUIRED"
        with connect() as db:
            db.execute(
                "UPDATE documents SET status=?, evidence_summary=NULL WHERE id=?",
                (status, document_id),
            )
        with connect() as db:
            return _document_response(dict(db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()))
    except Exception as error:
        error_text = str(error)
        if "Tesseract OCR is unavailable." in error_text or "Scanned PDF OCR requires Poppler." in error_text:
            safe_error = error_text
        elif "No readable text" in error_text:
            safe_error = "No readable text could be extracted from the document. Retry or request human review."
        else:
            safe_error = (
                "The document could not be read or text extraction failed. Verify that it is "
                "a valid, unencrypted PDF or image, then retry or request human review."
            )
        with connect() as db:
            db.execute(
                "UPDATE documents SET status='REVIEW_REQUIRED', processing_status='OCR_FAILED', ocr_status='FAILED', processed_at=?, extracted_text='', page_text='[]', processing_error=?, reviewed_by='System' WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), safe_error, document_id),
            )
            return _document_response(dict(db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()))

@app.post("/api/documents/{document_id}/verify")
def verify_document(document_id: int, payload: HumanAction, user: dict = Depends(current_user)) -> dict:
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE id=?",(document_id,)).fetchone()
        if not document: raise HTTPException(404,"Document not found")
        if document["status"] == "MISSING": raise HTTPException(409,"Missing documents cannot be verified")
        if document["status"] == "VERIFIED": raise HTTPException(409,"Document is already verified")
        reviewed_at = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE documents SET status='VERIFIED',reviewed_by=?,reviewed_at=? WHERE id=?",(user["username"],reviewed_at,document_id))
    audit(document["case_id"],user["username"],"EVIDENCE_VERIFIED",payload.note,"HUMAN_CLINICAL_REVIEW")
    return {"ok":True}

@app.post("/api/cases/{case_id}/escalate")
def escalate(case_id: str, payload: EscalationRequest, user: dict = Depends(current_user)) -> dict:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        case = case_access(db, case_id, user)
        assignments = db.execute("SELECT * FROM pa_case_assignments WHERE case_id=?", (case_id,)).fetchone()
        destination_role = "SUPERVISOR" if user["role"] == "SPECIALIST" else "MANAGER"
        assignment_column = "supervisor_id" if destination_role == "SUPERVISOR" else "manager_id"
        duplicate = db.execute("SELECT id FROM pa_escalations WHERE case_id=? AND status='ROUTED'", (case_id,)).fetchone()
        if duplicate:
            raise HTTPException(409, "An escalation is already awaiting review")
        if payload.reason.casefold() in {"not a reason", "no reason", "n/a", "none"}:
            raise HTTPException(422, "Escalation reason must describe the operational issue")
        if case["status"] in {"APPROVED", "DENIED"}:
            raise HTTPException(409, "Resolved cases cannot be escalated")
        if destination_role == "SUPERVISOR":
            allowed_states = {"NEW", "SPECIALIST_REVIEW", "RETURNED_TO_SPECIALIST", "INTAKE REVIEW", "EVIDENCE REVIEW", "MORE INFORMATION REQUIRED"}
            next_status = "AWAITING_SUPERVISOR"
        else:
            allowed_states = {"AWAITING_SUPERVISOR"}
            next_status = "AWAITING_MANAGER"
        if case["status"] not in allowed_states:
            raise HTTPException(409, f"Case cannot be escalated from {case['status']}")

        current_staff_id = assignments[assignment_column] if assignments else None
        candidates = db.execute(
            "SELECT * FROM pa_staff WHERE role=? AND active=1 AND available=1 ORDER BY id",
            (destination_role,),
        ).fetchall()
        eligible = []
        for candidate in candidates:
            service_types = json.loads(candidate["service_types"] or "[]")
            if service_types and case["service"] not in service_types:
                continue
            open_count = db.execute(
                f"SELECT COUNT(*) n FROM pa_case_assignments a JOIN cases c ON c.id=a.case_id WHERE a.{assignment_column}=? AND c.status NOT IN ('APPROVED','DENIED')",
                (candidate["id"],),
            ).fetchone()["n"]
            already_assigned = candidate["id"] == current_staff_id
            if open_count < candidate["max_workload"] or (already_assigned and open_count <= candidate["max_workload"]):
                eligible.append((candidate, open_count))
        if not eligible:
            raise HTTPException(409, f"No available {destination_role.title()} can receive this case")
        recipient, _ = min(eligible, key=lambda item: (item[0]["id"] != current_staff_id, item[1], item[0]["id"]))
        destination_id = recipient["id"]
        stamp = datetime.now(timezone.utc).isoformat()
        if not assignments:
            db.execute("INSERT INTO pa_case_assignments(case_id,updated_at) VALUES(?,?)", (case_id, stamp))
        db.execute(
            f"UPDATE pa_case_assignments SET {assignment_column}=?,updated_at=? WHERE case_id=?",
            (destination_id, stamp, case_id),
        )
        db.execute("UPDATE cases SET status=? WHERE id=?", (next_status, case_id))
        db.execute(
            "INSERT INTO pa_reviews(case_id,actor_id,action,reason,old_status,new_status,created_at) VALUES(?,?,?,?,?,?,?)",
            (case_id, user["id"], f"ESCALATE_TO_{destination_role}", payload.reason, case["status"], next_status, stamp),
        )
        cursor = db.execute("""INSERT INTO pa_escalations
            (case_id,from_staff_id,from_role,to_staff_id,to_role,reason,explanation,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'ROUTED',?,?)""",
            (case_id,user["id"],user["role"],destination_id,destination_role,payload.reason,payload.explanation,stamp,stamp))
        db.execute("""INSERT INTO pa_assignment_history
            (case_id,actor_id,from_staff_id,to_staff_id,role,reason,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (case_id,user["id"],user["id"],destination_id,destination_role,payload.reason,stamp))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",
            (case_id,stamp,user["display_name"],"CASE_ESCALATED",json.dumps({"reason":payload.reason,"from_status":case["status"],"to_status":next_status,"to_role":destination_role,"to_staff_id":destination_id},sort_keys=True),"HUMAN_OPERATIONAL_DECISION"))
        escalation_id = cursor.lastrowid
    return {"id":escalation_id,"case_id":case_id,"to_staff_id":destination_id,"to_staff_name":recipient["display_name"],"to_role":destination_role,"status":"ROUTED","case_status":next_status,"delivery_status":"RECORDED"}

@app.get("/api/settings")
def get_settings() -> dict:
    with connect() as db: settings=db.execute("SELECT * FROM sla_settings WHERE id=1").fetchone()
    if not settings: raise HTTPException(404,"SLA settings not found")
    return dict(settings)

@app.put("/api/settings")
def update_settings(payload: SlaSettingsUpdate) -> dict:
    values=payload.model_dump()
    if values["remaining_time_weight"]+values["priority_weight"]+values["evidence_weight"] != 100: raise HTTPException(400,"SLA scoring weights must total 100%")
    if any(value < 0 for value in values.values()): raise HTTPException(400,"SLA settings cannot be negative")
    with connect() as db:
        previous=dict(db.execute("SELECT * FROM sla_settings WHERE id=1").fetchone())
        changed_at=datetime.now(timezone.utc).isoformat()
        changes=[(field,previous[field],value) for field,value in values.items() if previous[field] != value]
        db.execute("UPDATE sla_settings SET warning_hours=?,urgent_hours=?,escalation_one_hours=?,escalation_two_hours=?,remaining_time_weight=?,priority_weight=?,evidence_weight=? WHERE id=1",tuple(values.values()))
        db.executemany("INSERT INTO settings_history(changed_at,actor,field_name,previous_value,new_value) VALUES(?,?,?,?,?)",[(changed_at,"Current user",field,old,new) for field,old,new in changes])
        settings=dict(db.execute("SELECT * FROM sla_settings WHERE id=1").fetchone())
    audit("SYSTEM","Current user","SLA_SETTINGS_UPDATED","; ".join(f"{f}: {o} → {n}" for f,o,n in changes) or "No values changed","HUMAN_OPERATIONAL_DECISION")
    return settings

@app.get("/api/settings/history")
def get_settings_history() -> list[dict]:
    with connect() as db: return [dict(row) for row in db.execute("SELECT * FROM settings_history ORDER BY changed_at DESC,id DESC LIMIT 100")]

@app.post("/api/agents/sla/index")
def reindex_policies(payload: AgentRun) -> dict:
    result=index_policies()
    audit("SYSTEM",payload.actor,"SLA_POLICY_INDEXED",f"Indexed {result['indexed']} approved policies using {result['embedding_engine']}.","HUMAN_OPERATIONAL_DECISION")
    return result


@app.get("/api/sla-risk-model-card")
def get_sla_risk_model_card() -> dict:
    model_card_path = Path(__file__).parent / "model_card.json"
    if not model_card_path.is_file():
        raise HTTPException(503, "Synthetic SLA model card is not available.")
    try:
        return json.loads(model_card_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(503, "Synthetic SLA model card could not be loaded.") from error

@app.post("/api/agents/sla/cases/{case_id}/recommend")

def recommend_case(case_id: str, payload: AgentRun) -> dict:
    try: return run_sla_rag(case_id)
    except ValueError as error: raise HTTPException(404,str(error)) from error

@app.post("/api/agents/sla/run")
def run_sla(payload: AgentRun) -> dict:
    with connect() as db:
        case_ids=[row["id"] for row in db.execute("SELECT id FROM cases WHERE status NOT IN ('APPROVED','DENIED')")]
    recommendations=run_sla_agent(case_ids)
    audit("SYSTEM",payload.actor,"SLA_RAG_AGENT_RUN",f"LangGraph RAG evaluated {len(recommendations)} active cases; recommendations only.","AGENT_RECOMMENDATION_REQUIRES_HUMAN_REVIEW")
    return {"run_at":datetime.now(timezone.utc).isoformat(),"mode":"recommendation_only","engine":"LangGraph RAG + deterministic Python rules + local Ollama explanations","recommendations":recommendations}
@app.post("/api/agents/escalation/cases/{case_id}/evaluate")
def evaluate_escalation(case_id: str, payload: AgentRun) -> dict:
    try:
        result = run_escalation_rag(case_id, payload.actor, payload.approve_notification)
        return {"mode": "recommendation_only", "engine": "LangGraph RAG + deterministic Python rules + local Ollama explanation", "decision": result}
    except ValueError as error:
        raise HTTPException(404, str(error)) from error

@app.get("/api/workflow-failures")
def list_workflow_failures(user: dict = Depends(current_user)) -> list[dict]:
    with connect() as db:
        if user["role"] == "MANAGER":
            rows = db.execute("SELECT * FROM workflow_failures ORDER BY occurred_at DESC").fetchall()
        else:
            assignment_column = "specialist_id" if user["role"] == "SPECIALIST" else "supervisor_id"
            rows = db.execute(f"SELECT f.* FROM workflow_failures f JOIN pa_case_assignments a ON a.case_id=f.case_id WHERE a.{assignment_column}=? ORDER BY f.occurred_at DESC", (user["id"],)).fetchall()
        return [dict(row) for row in rows]


@app.post("/api/agents/recovery/cases/{case_id}/recover")
def recover_case(case_id: str, payload: RecoveryRun, user: dict = Depends(current_user)) -> dict:
    if user["role"] == "SPECIALIST" and (payload.approve_recovery or not payload.preview_only):
        raise HTTPException(403, "Specialists may request preview-only recovery recommendations")
    try:
        result = run_recovery_rag(
            case_id,
            user["username"],
            payload.error_code,
            payload.approve_recovery,
            payload.preview_only,
        )
        return {"mode": "guarded_recovery", "engine": "LangGraph RAG + deterministic safety checks + local Ollama embeddings", "recovery": result}
    except ValueError as error:
        raise HTTPException(404, str(error)) from error






@app.post("/api/agents/evidence/cases/{case_id}/review")
def review_evidence(case_id: str, payload: AgentRun) -> dict:
    try:
        result = run_evidence_rag(case_id)
        return {"mode": "recommendation_only", "engine": "LangGraph RAG + deterministic metadata comparison + local Ollama explanation", "finding": result}
    except ValueError as error:
        raise HTTPException(404, str(error)) from error

@app.post("/api/agents/evidence/cases/{case_id}/predict")
def predict_case_evidence(case_id: str, payload: AgentRun) -> dict:
    try:
        finding = run_evidence_rag(case_id)
        with connect() as db:
            documents = [
                dict(row)
                for row in db.execute(
                    """SELECT category,status,processing_status,ocr_status,extracted_text
                       FROM documents WHERE case_id=? ORDER BY id""",
                    (case_id,),
                )
            ]
        prediction = predict_evidence_gaps(
            finding["required_documents"],
            documents,
            finding,
        )
        return {
            "mode": "experimental_prediction_only",
            "finding": finding,
            "prediction": prediction,
        }
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


def _validate_required_evidence_for_case(
    db, case: sqlite3.Row, required_evidence: str
) -> None:
    today = date.today().isoformat()
    requirements = db.execute(
        """SELECT policy_id,version,required_documents
           FROM policy_documents
           WHERE approved=1
             AND document_type IN (
                 'PAYER_REQUIREMENT',
                 'DOCUMENT_CHECKLIST',
                 'CLINICAL_DOCUMENTATION_RULE',
                 'AUTHORIZATION_GUIDELINE'
             )
             AND (payer IS NULL OR payer=? OR payer='ALL')
             AND (service_type IS NULL OR service_type=? OR service_type='ALL')
             AND effective_date<=?
             AND (expires_at IS NULL OR expires_at>=?)""",
        (case["payer"], case["service"], today, today),
    ).fetchall()
    if not requirements:
        raise HTTPException(422, "No active approved evidence requirements apply to this case")

    versions: dict[str, set[str]] = {}
    applicable_evidence: set[str] = set()
    try:
        for requirement in requirements:
            versions.setdefault(requirement["policy_id"], set()).add(requirement["version"])
            configured = json.loads(requirement["required_documents"] or "[]")
            if not isinstance(configured, list) or any(
                not isinstance(item, str) for item in configured
            ):
                raise ValueError("Required evidence list must contain strings")
            applicable_evidence.update(item.strip() for item in configured if item.strip())
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise HTTPException(409, "An applicable approved evidence requirement is invalid") from error

    if any(len(policy_versions) > 1 for policy_versions in versions.values()):
        raise HTTPException(409, "Conflicting active versions of an evidence requirement apply")
    if required_evidence not in applicable_evidence:
        raise HTTPException(
            422,
            "Required evidence is not listed by an active approved requirement for this case",
        )


@app.post("/api/cases/{case_id}/evidence-review-feedback")
def submit_evidence_review_feedback(
    case_id: str, payload: EvidenceReviewFeedbackCreate
) -> dict:
    required_evidence = payload.required_evidence.strip()
    if not required_evidence:
        raise HTTPException(422, "Required evidence cannot be blank")

    with connect() as db:
        case = db.execute(
            "SELECT id,payer,service FROM cases WHERE id=?", (case_id,)
        ).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        if payload.document_id is not None and not db.execute(
            "SELECT id FROM documents WHERE id=? AND case_id=?",
            (payload.document_id, case_id),
        ).fetchone():
            raise HTTPException(404, "Document not found for this case")
        _validate_required_evidence_for_case(db, case, required_evidence)
        cursor = db.execute(
            """INSERT INTO evidence_review_feedback
               (case_id,document_id,model_version,required_evidence,ml_prediction,human_outcome,review_status,created_at)
               VALUES(?,?,?,?,?,'UNKNOWN','PENDING',?)""",
            (
                case_id,
                payload.document_id,
                EVIDENCE_MODEL_VERSION,
                required_evidence,
                payload.ml_prediction,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        feedback = dict(
            db.execute(
                "SELECT * FROM evidence_review_feedback WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
        )
    return feedback


@app.get("/api/cases/{case_id}/evidence-review-feedback")
def list_evidence_review_feedback(case_id: str) -> list[dict]:
    with connect() as db:
        if not db.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone():
            raise HTTPException(404, "Case not found")
        rows = db.execute(
            """SELECT * FROM evidence_review_feedback
               WHERE case_id=? ORDER BY created_at,id""",
            (case_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/evidence-review-feedback/{feedback_id}/review")
def review_evidence_feedback(
    feedback_id: int, payload: EvidenceReviewDecision, user: dict = Depends(current_user)
) -> dict:
    reviewer_identifier = user["username"]
    if payload.review_status == "CONFIRMED" and payload.human_outcome is None:
        raise HTTPException(422, "A confirmed review requires a human-reviewed outcome")
    if payload.review_status == "REJECTED" and payload.human_outcome is not None:
        raise HTTPException(422, "A rejected review cannot set a human-reviewed outcome")

    reviewed_at = datetime.now(timezone.utc).isoformat()
    human_outcome = payload.human_outcome or "UNKNOWN"
    with connect() as db:
        feedback = db.execute(
            "SELECT * FROM evidence_review_feedback WHERE id=?", (feedback_id,)
        ).fetchone()
        if not feedback:
            raise HTTPException(404, "Evidence-review feedback not found")
        if feedback["review_status"] != "PENDING":
            raise HTTPException(409, "Only pending evidence-review feedback can be reviewed")
        updated = db.execute(
            """UPDATE evidence_review_feedback
               SET human_outcome=?,review_status=?,reviewer_identifier=?,reviewed_at=?
               WHERE id=? AND review_status='PENDING'""",
            (
                human_outcome,
                payload.review_status,
                reviewer_identifier,
                reviewed_at,
                feedback_id,
            ),
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "Only pending evidence-review feedback can be reviewed")
        result = dict(
            db.execute(
                "SELECT * FROM evidence_review_feedback WHERE id=?", (feedback_id,)
            ).fetchone()
        )
    audit(
        result["case_id"],
        reviewer_identifier,
        f"EVIDENCE_REVIEW_{payload.review_status}",
        f"Feedback {feedback_id} for {result['required_evidence']}: {human_outcome}.",
        "HUMAN_CLINICAL_REVIEW",
    )
    return result


@app.get("/api/evidence-review-feedback/training-export", response_model=None)
def export_evidence_review_training_data(
    format: Literal["csv", "json"] = "csv",
) -> dict | Response:
    with connect() as db:
        rows = db.execute(
            """SELECT id,required_evidence,ml_prediction,human_outcome,created_at,reviewed_at
               FROM evidence_review_feedback
               WHERE review_status='CONFIRMED'
                 AND human_outcome IN ('PRESENT','MISSING')
               ORDER BY reviewed_at,id"""
        ).fetchall()
    records = [dict(row) for row in rows]
    if format == "json":
        return {"count": len(records), "records": records}

    fields = (
        "id",
        "required_evidence",
        "ml_prediction",
        "human_outcome",
        "created_at",
        "reviewed_at",
    )
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for record in records:
        csv_record = dict(record)
        evidence = str(csv_record["required_evidence"])
        if evidence.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
            csv_record["required_evidence"] = f"'{evidence}"
        writer.writerow(csv_record)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="evidence-review-training.csv"'},
    )


@app.get("/api/policies")
def list_policies() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT policy_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine FROM policy_documents ORDER BY policy_id,version DESC")]


@app.post("/api/policies")
def create_policy(payload: PolicyCreate) -> dict:
    values = payload.model_dump()
    if not all(str(values[field]).strip() for field in ("policy_id","document_type","title","content","effective_date","version")):
        raise HTTPException(400,"Required policy fields cannot be blank")
    try:
        date.fromisoformat(values["effective_date"])
        if values["expires_at"]: date.fromisoformat(values["expires_at"])
    except ValueError as error:
        raise HTTPException(400,"Policy dates must use YYYY-MM-DD") from error
    with connect() as db:
        if db.execute("SELECT id FROM policy_documents WHERE policy_id=? AND version=?",(values["policy_id"],values["version"])).fetchone():
            raise HTTPException(409,"Policy ID and version already exist")
        db.execute("INSERT INTO policy_documents(policy_id,document_type,title,content,effective_date,expires_at,version,approved) VALUES(?,?,?,?,?,?,?,0)",(values["policy_id"],values["document_type"],values["title"],values["content"],values["effective_date"],values["expires_at"],values["version"]))
        policy=dict(db.execute("SELECT policy_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine FROM policy_documents WHERE policy_id=? AND version=?",(values["policy_id"],values["version"])).fetchone())
    audit("SYSTEM","Current user","SLA_POLICY_CREATED",f"Created draft {values['policy_id']} v{values['version']}.","HUMAN_OPERATIONAL_DECISION")
    return policy

@app.get("/api/payer-requirements")
def list_payer_requirements() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT policy_id AS requirement_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine,payer,service_type,source_reference,required_documents,verification_status FROM policy_documents WHERE document_type IN ('PAYER_REQUIREMENT','DOCUMENT_CHECKLIST','CLINICAL_DOCUMENTATION_RULE','AUTHORIZATION_GUIDELINE') ORDER BY payer,service_type,policy_id,version DESC")]


class _NoPolicyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _policy_source_host_allowed(host: str) -> bool:
    configured = os.getenv("PA_GUARD_POLICY_SOURCE_DOMAINS", "cms.gov")
    domains = {item.strip().lower().lstrip(".") for item in configured.split(",") if item.strip()}
    normalized = host.lower().rstrip(".")
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in domains)


def _fetch_policy_source(source_url: str) -> tuple[str, str]:
    """Fetch bounded public policy text from explicitly trusted HTTPS domains."""
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.hostname or not _policy_source_host_allowed(parsed.hostname):
        raise ValueError("Source host is not in PA_GUARD_POLICY_SOURCE_DOMAINS")
    opener = build_opener(_NoPolicyRedirect())
    request = UrlRequest(source_url, headers={"User-Agent": "PA-Guard-Policy-Review/1.0", "Accept": "text/html,text/plain,application/pdf"})
    try:
        with opener.open(request, timeout=8) as response:
            content_type = response.headers.get_content_type()
            body = response.read(2_000_001)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RuntimeError("The authoritative source could not be retrieved safely.") from error
    if len(body) > 2_000_000:
        raise RuntimeError("The authoritative source exceeded the 2 MB review limit.")
    if content_type == "application/pdf" or parsed.path.lower().endswith(".pdf"):
        try:
            reader = PdfReader(__import__("io").BytesIO(body))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as error:
            raise RuntimeError("The authoritative PDF could not be parsed safely.") from error
    elif content_type in {"text/html", "text/plain", "application/xhtml+xml"}:
        text = body.decode("utf-8", errors="replace")
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = unescape(text)
    else:
        raise RuntimeError("The authoritative source has an unsupported content type.")
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        raise RuntimeError("The authoritative source did not contain extractable text.")
    return normalized, hashlib.sha256(body).hexdigest()


def _source_attempt_audit(db, policy_id: str, version: str, actor: str, now: str, status: str, detail: str) -> None:
    db.execute(
        "INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES('SYSTEM',?,?,?,?,?)",
        (now, actor, "POLICY_SOURCE_VERIFICATION_ATTEMPT", json.dumps({"policy_id": policy_id, "version": version, "status": status, "detail": detail}, sort_keys=True), "POLICY_VERIFICATION"),
    )


@app.post("/api/payer-requirements/{requirement_id}/{version}/verify-source", status_code=202)
def verify_requirement_source(requirement_id: str, version: str, payload: SourceVerificationRequest, user: dict = Depends(current_user)) -> dict:
    if user["role"] not in {"ADMIN", "MANAGER"}:
        raise HTTPException(403, "A manager or policy administrator must initiate source verification")
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        policy_row = db.execute("SELECT * FROM policy_documents WHERE policy_id=? AND version=?", (requirement_id, version)).fetchone()
        if not policy_row:
            _source_attempt_audit(db, requirement_id, version, user["username"], now, "POLICY_NOT_FOUND", "No matching policy version exists.")
            db.commit()
            raise HTTPException(404, "Payer requirement version not found")
        policy = dict(policy_row)
        configured = json.loads(policy.get("specific_evidence_requirements") or "[]") or json.loads(policy.get("required_documents") or "[]")
        if not isinstance(configured, list) or any(not isinstance(item, str) for item in configured):
            raise HTTPException(409, "Stored policy criteria are invalid; policy remains unverified")
        expected = {item.strip() for item in configured if item.strip()}
        supplied = [item.model_dump() for item in payload.requirement_evidence]
        supplied_names = [item["requirement"].strip() for item in supplied]
        supplied_set = set(supplied_names)
        if not expected or supplied_set != expected or len(supplied_names) != len(supplied_set):
            status = "INCOMPLETE_CRITERION_EVIDENCE"
            _source_attempt_audit(db, requirement_id, version, user["username"], now, status, "Source evidence must be supplied exactly once for every stored criterion.")
            db.commit()
            raise HTTPException(422, "Source evidence is required for every stored evidence requirement")
        parsed = urlsplit(payload.source_url)
        trusted_host = bool(parsed.hostname and _policy_source_host_allowed(parsed.hostname))
        source_matches_record = payload.source_url == (policy.get("source_reference") or "")
        unsupported_attestations = [
            item for item in supplied
            if "no supporting language found" in item["supporting_excerpt"].casefold()
            or "not supported by this source" in item["supporting_excerpt"].casefold()
        ]
        if not trusted_host or not source_matches_record:
            attempt_status = "UNTRUSTED_SOURCE"
            criterion_results = [{**item, "criterion_status": "REVIEW_REQUIRED"} for item in supplied]
            source_status = "SOURCE_REJECTED"
            source_hash = None
            detail = "The source host is not trusted or differs from the policy's stored source reference."
            response_detail = "Source URL is not an approved authoritative source for this policy; policy remains unverified."
        elif unsupported_attestations:
            attempt_status = "UNSUPPORTED_CRITERIA"
            criterion_results = [{**item, "criterion_status": "UNSUPPORTED" if item in unsupported_attestations else "REVIEW_REQUIRED"} for item in supplied]
            source_status = "SOURCE_REVIEWED_BY_REQUESTER"
            source_hash = None
            detail = "The submitted review identifies criteria as unsupported; no policy approval was made."
            response_detail = "The cited source does not substantiate one or more proposed criteria; policy remains unverified."
        elif not payload.requirements_checked_against_source:
            attempt_status = "REVIEW_REQUIRED"
            criterion_results = [{**item, "criterion_status": "REVIEW_REQUIRED"} for item in supplied]
            source_status = "NOT_CHECKED"
            source_hash = None
            detail = "The request did not attest that each criterion was checked against the source."
            response_detail = "Every criterion must be reviewed against the source; policy remains unverified."
        else:
            try:
                source_text, source_hash = _fetch_policy_source(payload.source_url)
            except (ValueError, RuntimeError) as error:
                attempt_status = "SOURCE_UNAVAILABLE"
                criterion_results = [{**item, "criterion_status": "SOURCE_UNAVAILABLE"} for item in supplied]
                source_status = "SOURCE_UNAVAILABLE"
                source_hash = None
                detail = str(error)
                response_detail = "The authoritative source could not be retrieved and checked; policy remains unverified."
            else:
                normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
                criterion_results = [
                    {**item, "criterion_status": "SOURCE_SUPPORTED_PENDING_HUMAN" if normalize(item["supporting_excerpt"]) in normalize(source_text) else "REVIEW_REQUIRED"}
                    for item in supplied
                ]
                supported = all(item["criterion_status"] == "SOURCE_SUPPORTED_PENDING_HUMAN" for item in criterion_results)
                attempt_status = "SOURCE_MATCH_PENDING_HUMAN" if supported else "REVIEW_REQUIRED"
                source_status = "SOURCE_AVAILABLE"
                detail = "Retrieved source text was checked for exact excerpt matches; human policy approval is still required."
                response_detail = "Source text was retrieved, but every criterion needs exact source support before human approval."
        db.execute(
            "INSERT INTO policy_source_verification_attempts(policy_id,policy_version,attempted_at,attempted_by,source_url,authoritative_title,authoritative_version,source_effective_date,source_status,attempt_status,source_sha256,criteria_json,verification_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (requirement_id, version, now, user["username"], payload.source_url, payload.authoritative_title, payload.authoritative_version, payload.source_effective_date, source_status, attempt_status, source_hash, json.dumps(criterion_results, sort_keys=True), payload.verification_note.strip()),
        )
        _source_attempt_audit(db, requirement_id, version, user["username"], now, attempt_status, detail)
    if attempt_status == "UNSUPPORTED_CRITERIA":
        raise HTTPException(422, response_detail)
    if attempt_status == "INCOMPLETE_CRITERION_EVIDENCE":
        raise HTTPException(422, response_detail)
    if attempt_status in {"UNTRUSTED_SOURCE", "REVIEW_REQUIRED"}:
        raise HTTPException(422, response_detail)
    if attempt_status == "SOURCE_UNAVAILABLE":
        raise HTTPException(503, response_detail)
    return {
        "policy_id": requirement_id,
        "version": version,
        "verification_status": policy.get("verification_status") or "MOCK_DEMO",
        "attempt_status": attempt_status,
        "human_approval_required": True,
        "criteria": criterion_results,
        "message": "Source matching is advisory. A manager must review and approve this policy version before it becomes VERIFIED_PUBLIC.",
    }


@app.post("/api/payer-requirements/{requirement_id}/{version}/approve-source")
def approve_requirement_source(requirement_id: str, version: str, payload: SourceVerificationApproval, user: dict = Depends(current_user)) -> dict:
    if user["role"] not in {"ADMIN", "MANAGER"}:
        raise HTTPException(403, "A manager or policy administrator must approve source verification")
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        policy = db.execute("SELECT * FROM policy_documents WHERE policy_id=? AND version=?", (requirement_id, version)).fetchone()
        if not policy:
            raise HTTPException(404, "Payer requirement version not found")
        attempt = db.execute("SELECT * FROM policy_source_verification_attempts WHERE policy_id=? AND policy_version=? ORDER BY id DESC LIMIT 1", (requirement_id, version)).fetchone()
        if not attempt or attempt["attempt_status"] != "SOURCE_MATCH_PENDING_HUMAN" or attempt["source_status"] != "SOURCE_AVAILABLE" or not attempt["source_sha256"]:
            raise HTTPException(409, "A complete source-match attempt is required before human policy approval")
        criteria = json.loads(attempt["criteria_json"])
        if not criteria or any(item.get("criterion_status") != "SOURCE_SUPPORTED_PENDING_HUMAN" for item in criteria):
            raise HTTPException(409, "Every criterion must have exact source support before approval")
        if attempt["source_url"] != policy["source_reference"]:
            raise HTTPException(409, "Policy source changed after verification; start a new source review")
        for item in criteria:
            criterion_id = "CR-" + hashlib.sha256(f"{requirement_id}|{version}|{item['requirement']}".encode()).hexdigest()[:20]
            db.execute(
                "INSERT INTO policy_criteria(policy_id,policy_version,criterion_id,criterion_text,service_scope,source_url,source_version,supporting_excerpt,source_locator,verification_status,verification_notes,verified_by,verified_at) VALUES(?,?,?,?,?,?,?,?,?,'VERIFIED_PUBLIC',?,?,?) ON CONFLICT(policy_id,policy_version,criterion_id) DO UPDATE SET criterion_text=excluded.criterion_text,service_scope=excluded.service_scope,source_url=excluded.source_url,source_version=excluded.source_version,supporting_excerpt=excluded.supporting_excerpt,source_locator=excluded.source_locator,verification_status=excluded.verification_status,verification_notes=excluded.verification_notes,verified_by=excluded.verified_by,verified_at=excluded.verified_at",
                (requirement_id, version, criterion_id, item["requirement"], policy["service_type"], attempt["source_url"], attempt["authoritative_version"], item["supporting_excerpt"], item["source_location"], payload.reason.strip(), user["username"], now),
            )
        db.execute(
            "UPDATE policy_documents SET approved=1,verification_status='VERIFIED_PUBLIC',source_url=?,authoritative_title=?,authoritative_version=?,source_verified_by=?,source_verified_at=?,source_verification_note=?,source_requirement_evidence=? WHERE policy_id=? AND version=?",
            (attempt["source_url"], attempt["authoritative_title"], attempt["authoritative_version"], user["username"], now, payload.reason.strip(), attempt["criteria_json"], requirement_id, version),
        )
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES('SYSTEM',?,?,?,?,?)", (now, user["username"], "PAYER_POLICY_VERIFICATION_APPROVED", json.dumps({"policy_id": requirement_id, "version": version, "attempt_id": attempt["id"], "criterion_count": len(criteria), "reason": payload.reason.strip()}, sort_keys=True), "HUMAN_POLICY_REVIEW"))
    return {"policy_id": requirement_id, "version": version, "verification_status": "VERIFIED_PUBLIC", "verified_by": user["username"], "verified_at": now, "criterion_count": len(criteria)}

@app.post("/api/payer-requirements")
def create_payer_requirement(payload: RequirementCreate) -> dict:
    values = payload.model_dump()
    required = ("requirement_id","document_type","title","payer","service_type","content","source_reference","effective_date","version")
    if not all(str(values[field]).strip() for field in required) or not values["required_documents"]:
        raise HTTPException(400,"Complete all required payer requirement fields")
    if values["document_type"] not in {"PAYER_REQUIREMENT","DOCUMENT_CHECKLIST","CLINICAL_DOCUMENTATION_RULE","AUTHORIZATION_GUIDELINE"}:
        raise HTTPException(400,"Unsupported evidence requirement type")
    try:
        date.fromisoformat(values["effective_date"])
        if values["expires_at"]: date.fromisoformat(values["expires_at"])
    except ValueError as error:
        raise HTTPException(400,"Requirement dates must use YYYY-MM-DD") from error
    documents = [item.strip() for item in values["required_documents"] if item.strip()]
    if not documents: raise HTTPException(400,"Add at least one required document")
    with connect() as db:
        if db.execute("SELECT id FROM policy_documents WHERE policy_id=? AND version=?",(values["requirement_id"],values["version"])).fetchone():
            raise HTTPException(409,"Requirement ID and version already exist")
        db.execute("INSERT INTO policy_documents(policy_id,document_type,title,content,effective_date,expires_at,version,approved,payer,service_type,source_reference,required_documents) VALUES(?,?,?,?,?,?,?,0,?,?,?,?)",(values["requirement_id"],values["document_type"],values["title"],values["content"],values["effective_date"],values["expires_at"],values["version"],values["payer"],values["service_type"],values["source_reference"],json.dumps(documents)))
        result=dict(db.execute("SELECT policy_id AS requirement_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine,payer,service_type,source_reference,required_documents,verification_status FROM policy_documents WHERE policy_id=? AND version=?",(values["requirement_id"],values["version"])).fetchone())
    audit("SYSTEM","Current user","PAYER_REQUIREMENT_CREATED",f"Created draft {values['requirement_id']} v{values['version']} for {values['payer']} / {values['service_type']}.","HUMAN_OPERATIONAL_DECISION")
    return result
@app.get("/api/role-assignments/history")
def list_role_assignment_history() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT id,assignment_id,changed_at,actor,action,previous_values,new_values FROM role_assignment_history ORDER BY changed_at DESC,id DESC LIMIT 200")]


@app.get("/api/role-assignments")
def list_role_assignments() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM escalation_role_assignments ORDER BY department,escalation_level,effective_date DESC,id DESC")]


@app.post("/api/role-assignments")
def create_role_assignment(payload: RoleAssignmentCreate) -> dict:
    values = payload.model_dump()
    validate_role_assignment(values)
    changed_at = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        cursor = db.execute("INSERT INTO escalation_role_assignments(escalation_level,responsible_role,department,effective_date,expires_at) VALUES(?,?,?,?,?)", (values["escalation_level"], values["responsible_role"], values["department"], values["effective_date"], values["expires_at"]))
        result = dict(db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (cursor.lastrowid,)).fetchone())
        db.execute("INSERT INTO role_assignment_history(assignment_id,changed_at,actor,action,previous_values,new_values) VALUES(?,?,?,?,?,?)", (result["id"], changed_at, "Current user", "CREATED", None, json.dumps(result)))
    audit("SYSTEM", "Current user", "ROLE_ASSIGNMENT_CREATED", f"Assigned {values['responsible_role']} to {values['escalation_level']} for {values['department']} effective {values['effective_date']}.", "HUMAN_OPERATIONAL_DECISION")
    return result


@app.put("/api/role-assignments/{assignment_id}")
def update_role_assignment(assignment_id: int, payload: RoleAssignmentUpdate) -> dict:
    values = payload.model_dump()
    with connect() as db:
        existing = db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (assignment_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Role assignment not found")
        previous = dict(existing)
    validate_role_assignment(values, assignment_id)
    changed_at = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("UPDATE escalation_role_assignments SET escalation_level=?,responsible_role=?,department=?,effective_date=?,expires_at=? WHERE id=?", (values["escalation_level"], values["responsible_role"], values["department"], values["effective_date"], values["expires_at"], assignment_id))
        result = dict(db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (assignment_id,)).fetchone())
        db.execute("INSERT INTO role_assignment_history(assignment_id,changed_at,actor,action,previous_values,new_values) VALUES(?,?,?,?,?,?)", (assignment_id, changed_at, "Current user", "UPDATED", json.dumps(previous), json.dumps(result)))
    audit("SYSTEM", "Current user", "ROLE_ASSIGNMENT_UPDATED", f"Updated role assignment {assignment_id} for {values['department']}.", "HUMAN_OPERATIONAL_DECISION")
    return result


@app.post("/api/role-assignments/{assignment_id}/retire")
def retire_role_assignment(assignment_id: int) -> dict:
    retired_on = date.today().isoformat()
    changed_at = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        assignment = db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (assignment_id,)).fetchone()
        if not assignment:
            raise HTTPException(404, "Role assignment not found")
        if assignment["expires_at"] and assignment["expires_at"] <= retired_on:
            raise HTTPException(409, "Role assignment is already retired")
        if assignment["effective_date"] > retired_on:
            raise HTTPException(409, "Future assignments must be edited rather than retired")
        previous = dict(assignment)
        db.execute("UPDATE escalation_role_assignments SET expires_at=? WHERE id=?", (retired_on, assignment_id))
        result = dict(db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (assignment_id,)).fetchone())
        db.execute("INSERT INTO role_assignment_history(assignment_id,changed_at,actor,action,previous_values,new_values) VALUES(?,?,?,?,?,?)", (assignment_id, changed_at, "Current user", "RETIRED", json.dumps(previous), json.dumps(result)))
    audit("SYSTEM", "Current user", "ROLE_ASSIGNMENT_RETIRED", f"Retired role assignment {assignment_id} on {retired_on}.", "HUMAN_OPERATIONAL_DECISION")
    return result



@app.put("/api/payer-requirements/{requirement_id}/{version}")
def update_payer_requirement(requirement_id: str, version: str, payload: RequirementUpdate) -> dict:
    values = payload.model_dump()
    documents = [item.strip() for item in values["required_documents"] if item.strip()]
    if not documents:
        raise HTTPException(400, "Add at least one required document")
    with connect() as db:
        existing = db.execute("SELECT * FROM policy_documents WHERE policy_id=? AND version=?", (requirement_id, version)).fetchone()
        if not existing:
            raise HTTPException(404, "Payer requirement not found")
        if existing["approved"]:
            raise HTTPException(409, "Approved requirement versions are immutable; create a new version")
        db.execute("UPDATE policy_documents SET document_type=?,title=?,payer=?,service_type=?,required_documents=?,content=?,source_reference=?,effective_date=?,expires_at=?,version=?,embedding=NULL,embedding_engine=NULL WHERE policy_id=? AND version=?", (values["document_type"], values["title"], values["payer"], values["service_type"], json.dumps(documents), values["content"], values["source_reference"], values["effective_date"], values["expires_at"], values["version"], requirement_id, version))
        result = dict(db.execute("SELECT policy_id AS requirement_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine,payer,service_type,source_reference,required_documents FROM policy_documents WHERE policy_id=? AND version=?", (requirement_id, values["version"])).fetchone())
    audit("SYSTEM", "Current user", "PAYER_REQUIREMENT_UPDATED", f"Updated draft {requirement_id} v{version}.", "HUMAN_OPERATIONAL_DECISION")
    return result

@app.get("/api/escalation-policies")
def list_escalation_policies() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT policy_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine,department,source_reference FROM policy_documents WHERE document_type IN ('ESCALATION_RULE','ROLE_RESPONSIBILITY','NOTIFICATION_PROCEDURE') ORDER BY department,policy_id,version DESC")]


@app.post("/api/escalation-policies")
def create_escalation_policy(payload: EscalationPolicyCreate) -> dict:
    values = payload.model_dump()
    required = ("policy_id", "document_type", "title", "content", "department", "source_reference", "effective_date", "version")
    if not all(str(values[field]).strip() for field in required):
        raise HTTPException(400, "Complete all required escalation policy fields")
    if values["document_type"] not in {"ESCALATION_RULE", "ROLE_RESPONSIBILITY", "NOTIFICATION_PROCEDURE"}:
        raise HTTPException(400, "Unsupported escalation policy type")
    try:
        date.fromisoformat(values["effective_date"])
        if values["expires_at"]: date.fromisoformat(values["expires_at"])
    except ValueError as error:
        raise HTTPException(400, "Policy dates must use YYYY-MM-DD") from error
    with connect() as db:
        if db.execute("SELECT id FROM policy_documents WHERE policy_id=? AND version=?", (values["policy_id"], values["version"])).fetchone():
            raise HTTPException(409, "Policy ID and version already exist")
        db.execute("INSERT INTO policy_documents(policy_id,document_type,title,content,effective_date,expires_at,version,approved,department,source_reference) VALUES(?,?,?,?,?,?,?,0,?,?)", (values["policy_id"], values["document_type"], values["title"], values["content"], values["effective_date"], values["expires_at"], values["version"], values["department"], values["source_reference"]))
        result = dict(db.execute("SELECT policy_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine,department,source_reference FROM policy_documents WHERE policy_id=? AND version=?", (values["policy_id"], values["version"])).fetchone())
    audit("SYSTEM", "Current user", "ESCALATION_POLICY_CREATED", f"Created draft {values['policy_id']} v{values['version']} for {values['department']}.", "HUMAN_OPERATIONAL_DECISION")
    return result


@app.put("/api/escalation-policies/{policy_id}/{version}")
def update_escalation_policy(policy_id: str, version: str, payload: EscalationPolicyUpdate) -> dict:
    values = payload.model_dump()
    with connect() as db:
        existing = db.execute("SELECT * FROM policy_documents WHERE policy_id=? AND version=?", (policy_id, version)).fetchone()
        if not existing: raise HTTPException(404, "Escalation policy not found")
        if existing["approved"]: raise HTTPException(409, "Approved escalation policy versions are immutable; create a new version")
        db.execute("UPDATE policy_documents SET document_type=?,title=?,content=?,department=?,source_reference=?,effective_date=?,expires_at=?,version=?,embedding=NULL,embedding_engine=NULL WHERE policy_id=? AND version=?", (values["document_type"], values["title"], values["content"], values["department"], values["source_reference"], values["effective_date"], values["expires_at"], values["version"], policy_id, version))
        result = dict(db.execute("SELECT policy_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine,department,source_reference FROM policy_documents WHERE policy_id=? AND version=?", (policy_id, values["version"])).fetchone())
    audit("SYSTEM", "Current user", "ESCALATION_POLICY_UPDATED", f"Updated draft {policy_id} v{version}.", "HUMAN_OPERATIONAL_DECISION")
    return result


@app.post("/api/escalation-policies/{policy_id}/{version}/approval")
def set_escalation_policy_approval(policy_id: str, version: str, payload: PolicyApproval) -> dict:
    with connect() as db:
        policy = db.execute("SELECT * FROM policy_documents WHERE policy_id=? AND version=?", (policy_id, version)).fetchone()
        if not policy: raise HTTPException(404, "Escalation policy not found")
        db.execute("UPDATE policy_documents SET approved=?,embedding=NULL,embedding_engine=NULL WHERE policy_id=? AND version=?", (int(payload.approved), policy_id, version))
    result = index_policies() if payload.approved else {"indexed": 0, "embedding_engine": "not_applicable"}
    action = "ESCALATION_POLICY_APPROVED" if payload.approved else "ESCALATION_POLICY_RETIRED"
    audit("SYSTEM", payload.actor, action, f"{policy_id} v{version}; escalation retrieval index updated.", "HUMAN_OPERATIONAL_DECISION")
    return {"ok": True, "approved": payload.approved, "index": result}




@app.post("/api/payer-requirements/{requirement_id}/{version}/approval")
def set_requirement_approval(requirement_id: str, version: str, payload: PolicyApproval) -> dict:
    with connect() as db:
        requirement=db.execute("SELECT * FROM policy_documents WHERE policy_id=? AND version=?",(requirement_id,version)).fetchone()
        if not requirement: raise HTTPException(404,"Payer requirement not found")
        db.execute("UPDATE policy_documents SET approved=?,embedding=NULL,embedding_engine=NULL WHERE policy_id=? AND version=?",(int(payload.approved),requirement_id,version))
    result=index_policies() if payload.approved else {"indexed":0,"embedding_engine":"not_applicable"}
    action="PAYER_REQUIREMENT_APPROVED" if payload.approved else "PAYER_REQUIREMENT_RETIRED"
    audit("SYSTEM",payload.actor,action,f"{requirement_id} v{version}; evidence retrieval index updated.","HUMAN_OPERATIONAL_DECISION")
    return {"ok":True,"approved":payload.approved,"index":result}



@app.put("/api/policies/{policy_id}/{version}")
def update_policy(policy_id: str, version: str, payload: PolicyUpdate) -> dict:
    values=payload.model_dump()
    with connect() as db:
        existing=db.execute("SELECT * FROM policy_documents WHERE policy_id=? AND version=?",(policy_id,version)).fetchone()
        if not existing: raise HTTPException(404,"Policy not found")
        if existing["approved"]: raise HTTPException(409,"Approved policy versions are immutable; create a new version")
        db.execute("UPDATE policy_documents SET document_type=?,title=?,content=?,effective_date=?,expires_at=?,version=?,embedding=NULL,embedding_engine=NULL WHERE policy_id=? AND version=?",(values["document_type"],values["title"],values["content"],values["effective_date"],values["expires_at"],values["version"],policy_id,version))
        policy=dict(db.execute("SELECT policy_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine FROM policy_documents WHERE policy_id=? AND version=?",(policy_id,values["version"])).fetchone())
    audit("SYSTEM","Current user","SLA_POLICY_UPDATED",f"Updated draft {policy_id} v{version}.","HUMAN_OPERATIONAL_DECISION")
    return policy


@app.post("/api/policies/{policy_id}/{version}/approval")
def set_policy_approval(policy_id: str, version: str, payload: PolicyApproval) -> dict:
    with connect() as db:
        policy=db.execute("SELECT * FROM policy_documents WHERE policy_id=? AND version=?",(policy_id,version)).fetchone()
        if not policy: raise HTTPException(404,"Policy not found")
        db.execute("UPDATE policy_documents SET approved=?,embedding=NULL,embedding_engine=NULL WHERE policy_id=? AND version=?",(int(payload.approved),policy_id,version))
    result=index_policies() if payload.approved else {"indexed":0,"embedding_engine":"not_applicable"}
    action="SLA_POLICY_APPROVED" if payload.approved else "SLA_POLICY_RETIRED"
    audit("SYSTEM",payload.actor,action,f"{policy_id} v{version}; index result: {result['indexed']} policies.","HUMAN_OPERATIONAL_DECISION")
    return {"ok":True,"approved":payload.approved,"index":result}

@app.get("/api/audit")
def list_audit() -> list[dict]:
    with connect() as db: return [dict(row) for row in db.execute("SELECT * FROM audit_events ORDER BY timestamp DESC")]
