import csv
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Literal

import PIL.Image
import pytesseract
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pdf2image import convert_from_path
from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from pytesseract.pytesseract import TesseractNotFoundError
from pdf2image.exceptions import PDFInfoNotInstalledError
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from agents import run_sla_agent, run_sla_rag
from evidence_agents import run_evidence_rag
from evidence_prediction_model import predict_evidence_gaps
from escalation_agents import run_escalation_rag
from recovery_agents import run_recovery_rag
from database import DOCUMENT_STORAGE_DIR, connect, initialize
from rag import index_policies, ollama_status
from sla_risk_model import (
    InvalidRiskFeatures,
    ModelUnavailableError,
    predict_sla_breach_risk,
)

app = FastAPI(title="PA Guard API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"https?://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_CATEGORIES = [
    "Clinical Notes",
    "Imaging Report",
    "Treatment History",
    "Laboratory Reports",
    "Medical Necessity Documentation",
    "Prescription or Medication Records",
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

class CaseUpdate(BaseModel):
    status: str | None = None
    coordinator: str | None = None

class HumanAction(BaseModel):
    actor: str
    note: str

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
    ml_prediction: Literal[
        "PREDICTED_MISSING", "NOT_PREDICTED_MISSING", "UNKNOWN"
    ] | None = None


class EvidenceReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_status: Literal["CONFIRMED", "REJECTED"]
    human_outcome: Literal["PRESENT", "MISSING", "UNKNOWN", "NEEDS_REVIEW"] | None = None
    reviewer_identifier: str = Field(min_length=1, max_length=100)


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
        conflict = db.execute("""SELECT id FROM escalation_role_assignments
            WHERE escalation_level=? AND department=? AND id != ?
            AND effective_date <= COALESCE(?, '9999-12-31')
            AND COALESCE(expires_at, '9999-12-31') >= ? LIMIT 1""",
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

@app.get("/api/cases")
def list_cases() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM cases ORDER BY deadline")]

@app.post("/api/cases")
def create_case(payload: CaseCreate) -> dict:
    with connect() as db:
        number = db.execute("SELECT COUNT(*) AS count FROM cases").fetchone()["count"] + 1043
        case = {"id":f"PA-{number}","patient_reference":payload.patient_reference,"payer":payload.payer,"service":payload.service,"submission_date":datetime.now(timezone.utc).date().isoformat(),"deadline":payload.deadline,"status":"NEW","risk":"ON TRACK","coordinator":payload.coordinator,"priority":"MEDIUM"}
        db.execute("INSERT INTO cases VALUES(:id,:patient_reference,:payer,:service,:submission_date,:deadline,:status,:risk,:coordinator,:priority)",case)
    audit(case["id"],payload.coordinator,"CASE_CREATED","Case created for intake review")
    return case

@app.patch("/api/cases/{case_id}")
def update_case(case_id: str, payload: CaseUpdate) -> dict:
    changes = payload.model_dump(exclude_none=True)
    if not changes: raise HTTPException(400,"No changes supplied")
    with connect() as db:
        if not db.execute("SELECT id FROM cases WHERE id=?",(case_id,)).fetchone(): raise HTTPException(404,"Case not found")
        for field,value in changes.items(): db.execute(f"UPDATE cases SET {field}=? WHERE id=?",(value,case_id))
        updated = dict(db.execute("SELECT * FROM cases WHERE id=?",(case_id,)).fetchone())
    audit(case_id,"Current user","CASE_UPDATED",str(changes))
    return updated

@app.get("/api/cases/{case_id}")
def case_detail(case_id: str) -> dict:
    with connect() as db:
        case = db.execute("SELECT * FROM cases WHERE id=?",(case_id,)).fetchone()
        if not case: raise HTTPException(404,"Case not found")
        documents = [_document_response(dict(row)) for row in db.execute("SELECT * FROM documents WHERE case_id=?",(case_id,))]
    return {"case":dict(case),"documents":documents,"agent_status":"SLA RAG CONNECTED — RECOMMENDATION ONLY"}


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
def list_documents() -> list[dict]:
    with connect() as db: return [_document_response(dict(row)) for row in db.execute("SELECT * FROM documents ORDER BY case_id,id")]


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


@app.post("/api/cases/{case_id}/documents")
async def upload_document(case_id: str, file: UploadFile = File(...), category: str = Form(...)) -> dict:
    with connect() as db:
        if not db.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone():
            raise HTTPException(404, "Case not found")
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
                "INSERT INTO documents(case_id,name,status,category,original_filename,stored_filename,file_path,mime_type,file_size,uploaded_at,processing_status,ocr_status,processing_error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            document_id = cursor.lastrowid
        except Exception:
            storage_path.unlink(missing_ok=True)
            raise
    return await run_in_threadpool(process_document, document_id)


@app.post("/api/documents/{document_id}/process")
def process_document(document_id: int) -> dict:
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        db.execute(
            "UPDATE documents SET status='RECEIVED', processing_status='PROCESSING', ocr_status='PROCESSING', processing_error='' WHERE id=?",
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
        try:
            evidence = run_evidence_rag(document["case_id"])
        except Exception as error:
            evidence = {
                "verification_status": "NEEDS_REVIEW",
                "possible_mismatches": [f"Evidence Agent could not complete its review: {error}"],
                "recommended_action": "Retry the Evidence Agent or route the uploaded document to a human evidence reviewer.",
                "needs_human_review": True,
            }
        status = "REVIEW_REQUIRED"
        with connect() as db:
            db.execute(
                "UPDATE documents SET status=?, evidence_summary=? WHERE id=?",
                (status, json.dumps(evidence, sort_keys=True), document_id),
            )
        with connect() as db:
            return _document_response(dict(db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()))
    except Exception as error:
        with connect() as db:
            db.execute(
                "UPDATE documents SET status='REVIEW_REQUIRED', processing_status='OCR_FAILED', ocr_status='FAILED', processed_at=?, processing_error=?, reviewed_by='System' WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), str(error), document_id),
            )
            return _document_response(dict(db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()))

@app.post("/api/documents/{document_id}/verify")
def verify_document(document_id: int, payload: HumanAction) -> dict:
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE id=?",(document_id,)).fetchone()
        if not document: raise HTTPException(404,"Document not found")
        db.execute("UPDATE documents SET status='VERIFIED',reviewed_by=?,reviewed_at=? WHERE id=?",(payload.actor,datetime.now(timezone.utc).isoformat(),document_id))
    audit(document["case_id"],payload.actor,"EVIDENCE_VERIFIED",payload.note,"HUMAN_CLINICAL_REVIEW")
    return {"ok":True}

@app.post("/api/cases/{case_id}/escalate")
def escalate(case_id: str, payload: HumanAction) -> dict:
    audit(case_id,payload.actor,"CASE_ESCALATED",payload.note,"HUMAN_OPERATIONAL_DECISION")
    return {"ok":True}

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
def list_workflow_failures() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM workflow_failures ORDER BY occurred_at DESC")]


@app.post("/api/agents/recovery/cases/{case_id}/recover")
def recover_case(case_id: str, payload: RecoveryRun) -> dict:
    try:
        result = run_recovery_rag(
            case_id,
            payload.actor,
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
        _validate_required_evidence_for_case(db, case, required_evidence)
        cursor = db.execute(
            """INSERT INTO evidence_review_feedback
               (case_id,required_evidence,ml_prediction,human_outcome,review_status,created_at)
               VALUES(?,?,?,'UNKNOWN','PENDING',?)""",
            (
                case_id,
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
    feedback_id: int, payload: EvidenceReviewDecision
) -> dict:
    reviewer_identifier = payload.reviewer_identifier.strip()
    if not reviewer_identifier:
        raise HTTPException(422, "Reviewer identifier cannot be blank")
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
        return [dict(row) for row in db.execute("SELECT policy_id AS requirement_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine,payer,service_type,source_reference,required_documents FROM policy_documents WHERE document_type IN ('PAYER_REQUIREMENT','DOCUMENT_CHECKLIST','CLINICAL_DOCUMENTATION_RULE','AUTHORIZATION_GUIDELINE') ORDER BY payer,service_type,policy_id,version DESC")]

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
        result=dict(db.execute("SELECT policy_id AS requirement_id,document_type,title,content,effective_date,expires_at,version,approved,embedding_engine,payer,service_type,source_reference,required_documents FROM policy_documents WHERE policy_id=? AND version=?",(values["requirement_id"],values["version"])).fetchone())
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