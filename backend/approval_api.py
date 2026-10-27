"""Authenticated prototype approval workflow and deterministic staff allocation."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from math import ceil
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from database import connect
from sla_risk_model import predict_sla_breach_risk
from sla_risk_service import predict_case_sla_risk

router = APIRouter(prefix="/api", tags=["approvals"])
ROLES = {"SPECIALIST", "SUPERVISOR", "MANAGER", "ADMIN"}
FINAL = {"APPROVED", "DENIED"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class Action(BaseModel):
    action: str
    reason: str = Field(min_length=1, max_length=1000)
    expected_updated_at: str | None = None

    @field_validator("reason")
    @classmethod
    def require_nonblank_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A nonblank reason is required")
        return value


class Note(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


class StaffUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    role: str
    active: bool = True
    available: bool = True
    max_workload: int = Field(default=10, ge=1, le=200)
    service_types: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str) -> str:
        value=value.strip()
        if not value: raise ValueError("Display name cannot be blank")
        return value

    @field_validator("service_types")
    @classmethod
    def clean_service_types(cls, values: list[str]) -> list[str]:
        cleaned=[]
        for value in values:
            value=value.strip()
            if not value or len(value)>100: raise ValueError("Service types must contain 1–100 non-space characters")
            if value not in cleaned: cleaned.append(value)
        return cleaned


class StaffCreate(StaffUpdate):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=12, max_length=200)

    @field_validator("username")
    @classmethod
    def clean_username(cls, value: str) -> str:
        value=value.strip()
        if len(value)<2: raise ValueError("Username must contain at least two non-space characters")
        return value


class Assignment(BaseModel):
    staff_id: int
    reason: str = Field(min_length=1, max_length=1000)


class BulkAssignment(BaseModel):
    case_ids: list[str] = Field(min_length=1, max_length=100)
    staff_id: int
    reason: str = Field(min_length=1, max_length=1000)


class ApprovalPolicy(BaseModel):
    manager_final_required: bool


def user_from_authorization(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Log in to access approval operations")
    token_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with connect() as db:
        row = db.execute("""SELECT s.* FROM pa_sessions x JOIN pa_staff s ON s.id=x.staff_id
            WHERE x.token_hash=? AND x.expires_at>?""", (token_hash, now())).fetchone()
    if not row or not row["active"]:
        raise HTTPException(401, "Session is invalid or account is inactive")
    return dict(row)


def current_user(authorization: str | None = Header(default=None)) -> dict:
    return user_from_authorization(authorization)


def require(user: dict, *roles: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(403, "Your role cannot perform this action")


def case_access(db, case_id: str, user: dict) -> dict:
    case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Case not found")
    if user["role"] == "ADMIN":
        raise HTTPException(403, "System administrators do not review patient cases")
    assignments = db.execute("SELECT * FROM pa_case_assignments WHERE case_id=?", (case_id,)).fetchone()
    if user["role"] == "SPECIALIST" and (not assignments or assignments["specialist_id"] != user["id"]):
        raise HTTPException(403, "This case is not allocated to you")
    if user["role"] == "SUPERVISOR" and (not assignments or assignments["supervisor_id"] != user["id"]):
        raise HTTPException(403, "This case is outside your supervisory queue")
    return dict(case)


@router.post("/auth/login")
def login(payload: Login):
    with connect() as db:
        staff = db.execute("SELECT * FROM pa_staff WHERE username=?", (payload.username,)).fetchone()
        if not staff or not staff["active"] or not verify_password(payload.password, staff["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        token = secrets.token_urlsafe(36)
        db.execute("INSERT INTO pa_sessions(token_hash,staff_id,expires_at) VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), staff["id"], (datetime.now(timezone.utc)+timedelta(hours=12)).isoformat()))
    return {"token": token, "user": {"id": staff["id"], "username": staff["username"], "display_name": staff["display_name"], "role": staff["role"]}}


@router.post("/auth/logout")
def logout(user: dict = Depends(current_user), authorization: str = Header()):
    token_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with connect() as db:
        db.execute("DELETE FROM pa_sessions WHERE token_hash=?", (token_hash,))
    return {"ok": True}


@router.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"], "role": user["role"]}


@router.get("/approval-policy")
def get_approval_policy(user: dict = Depends(current_user)):
    with connect() as db:
        row=db.execute("SELECT manager_final_required FROM pa_approval_policy WHERE id=1").fetchone()
    return {"manager_final_required":bool(row["manager_final_required"])}


@router.put("/approval-policy")
def set_approval_policy(payload: ApprovalPolicy, user: dict = Depends(current_user)):
    require(user,"ADMIN")
    with connect() as db:
        db.execute("UPDATE pa_approval_policy SET manager_final_required=? WHERE id=1",(int(payload.manager_final_required),))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES('SYSTEM',?,?,?,?,?)",(now(),user["display_name"],"APPROVAL_POLICY_UPDATED",f"Manager final decision required: {payload.manager_final_required}","HUMAN_OPERATIONAL_DECISION"))
    return {"manager_final_required":payload.manager_final_required}


@router.get("/approvals")
def approvals(
    user: dict = Depends(current_user),
    q: str = Query(default="", max_length=160),
    status: str | None = None,
    assigned_role: str | None = None,
    assigned_staff_id: int | None = Query(default=None, ge=1),
    payer: str | None = Query(default=None, max_length=160),
    service: str | None = Query(default=None, max_length=160),
    sla_status: str | None = None,
    ml_risk: Literal["HIGH", "MEDIUM", "LOW", "UNAVAILABLE"] | None = None,
    evidence_status: str | None = None,
    overdue: bool | None = None,
    submission_from: str | None = None,
    submission_to: str | None = None,
    deadline_from: str | None = None,
    deadline_to: str | None = None,
    sort_by: Literal["deadline", "time_remaining", "submission_date", "case_id", "workflow_status", "sla_risk", "last_updated"] = "deadline",
    sort_order: Literal["asc", "desc"] = "asc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if user["role"] == "ADMIN": raise HTTPException(403,"Administrators manage staff and do not access patient queues")
    if user["role"] not in {"SPECIALIST","SUPERVISOR","MANAGER"}: raise HTTPException(403,"Role cannot access the approval queue")
    if assigned_role and assigned_role not in {"SPECIALIST","SUPERVISOR","MANAGER"}: raise HTTPException(400,"Invalid assignment role")
    for label,value in (("submission_from",submission_from),("submission_to",submission_to),("deadline_from",deadline_from),("deadline_to",deadline_to)):
        if value:
            try: date.fromisoformat(value)
            except ValueError as error: raise HTTPException(400,f"{label} must use YYYY-MM-DD") from error
    joins=""" FROM cases c LEFT JOIN pa_case_assignments a ON a.case_id=c.id
        LEFT JOIN pa_staff s ON s.id=a.specialist_id LEFT JOIN pa_staff v ON v.id=a.supervisor_id
        LEFT JOIN pa_staff m ON m.id=a.manager_id """
    evidence_sql="(SELECT er.verification_status FROM evidence_recommendations er WHERE er.case_id=c.id ORDER BY er.id DESC LIMIT 1)"
    updated_sql="COALESCE(a.updated_at,(SELECT MAX(ae.timestamp) FROM audit_events ae WHERE ae.case_id=c.id),c.submission_date)"
    clauses=[]; params=[]
    if user["role"]=="SPECIALIST": clauses.append("a.specialist_id=?"); params.append(user["id"])
    elif user["role"]=="SUPERVISOR": clauses.append("(a.supervisor_id=? OR EXISTS (SELECT 1 FROM pa_escalations pe WHERE pe.case_id=c.id AND pe.to_staff_id=? AND pe.status='ROUTED'))"); params.extend([user["id"],user["id"]])
    if user["role"]=="SUPERVISOR": clauses.append("(c.status IN ('AWAITING_SUPERVISOR','RETURNED_TO_SPECIALIST','SPECIALIST_REVIEW') OR EXISTS (SELECT 1 FROM pa_escalations pe WHERE pe.case_id=c.id AND pe.to_staff_id=? AND pe.status='ROUTED'))"); params.append(user["id"])
    elif user["role"]=="SPECIALIST": clauses.append("c.status NOT IN ('APPROVED','DENIED','AWAITING_MANAGER')")
    elif user["role"] == "MANAGER" and status not in FINAL:
        # The default Manager queue is a handoff queue. Non-final states such
        # as NEW belong to earlier workflow stages and cannot be approved.
        clauses.append("c.status IN ('AWAITING_MANAGER','UNDER_REVIEW')")
    if status is None:
        clauses.append("c.status NOT IN ('APPROVED','DENIED')")
    if q:
        clauses.append("(c.id LIKE ? OR c.patient_reference LIKE ? OR c.payer LIKE ? OR c.service LIKE ? OR s.display_name LIKE ? OR v.display_name LIKE ? OR m.display_name LIKE ?)")
        params.extend([f"%{q}%"]*7)
    if status: clauses.append("c.status=?"); params.append(status)
    if payer: clauses.append("c.payer LIKE ?"); params.append(f"%{payer}%")
    if service: clauses.append("c.service LIKE ?"); params.append(f"%{service}%")
    if assigned_role:
        col={"SPECIALIST":"a.specialist_id","SUPERVISOR":"a.supervisor_id","MANAGER":"a.manager_id"}[assigned_role]
        clauses.append(f"{col} IS NOT NULL")
    if assigned_staff_id:
        clauses.append("(?=a.specialist_id OR ?=a.supervisor_id OR ?=a.manager_id)"); params.extend([assigned_staff_id]*3)
    if evidence_status: clauses.append(f"COALESCE({evidence_sql},'UNAVAILABLE')=?"); params.append(evidence_status)
    if submission_from: clauses.append("date(c.submission_date)>=date(?)"); params.append(submission_from)
    if submission_to: clauses.append("date(c.submission_date)<=date(?)"); params.append(submission_to)
    if deadline_from: clauses.append("date(c.deadline)>=date(?)"); params.append(deadline_from)
    if deadline_to: clauses.append("date(c.deadline)<=date(?)"); params.append(deadline_to)
    if overdue is not None:
        clauses.append("(datetime(c.deadline)<datetime('now') AND c.status NOT IN ('APPROVED','DENIED'))" if overdue else "(datetime(c.deadline)>=datetime('now') OR c.status IN ('APPROVED','DENIED'))")
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    with connect() as db:
        settings=db.execute("SELECT warning_hours FROM sla_settings WHERE id=1").fetchone()
        warning_hours=int(settings["warning_hours"]) if settings else 24
        sla_sql=f"CASE WHEN c.status IN ('APPROVED','DENIED') THEN c.risk WHEN datetime(c.deadline)<datetime('now') THEN 'BREACHED' WHEN (julianday(c.deadline)-julianday('now'))*24<={warning_hours} OR EXISTS(SELECT 1 FROM documents d WHERE d.case_id=c.id AND d.status!='VERIFIED') THEN 'AT RISK' ELSE 'ON TRACK' END"
        if sla_status:
            if sla_status not in {"ON TRACK","AT RISK","BREACHED"}: raise HTTPException(400,"Invalid deterministic SLA status")
            clauses.append(sla_sql+"=?"); params.append(sla_status)
            where=" WHERE "+" AND ".join(clauses)
        sort_expr={"deadline":"c.deadline","time_remaining":"c.deadline","submission_date":"c.submission_date","case_id":"c.id","workflow_status":"c.status","sla_risk":f"CASE ({sla_sql}) WHEN 'BREACHED' THEN 0 WHEN 'AT RISK' THEN 1 WHEN 'ON TRACK' THEN 2 ELSE 3 END","last_updated":updated_sql}[sort_by]
        select_sql="SELECT c.id,c.payer,c.service,c.submission_date,c.deadline,c.status,"+sla_sql+" risk,"+sla_sql+" sla_status,c.priority,a.specialist_id,a.supervisor_id,a.manager_id,s.display_name specialist_name,v.display_name supervisor_name,m.display_name manager_name,"+evidence_sql+" evidence_status,"+updated_sql+" last_updated"+joins+where
        def model_prediction(case_id):
            try: return predict_case_sla_risk(case_id,connection_factory=connect,prediction_function=predict_sla_breach_risk)
            except HTTPException: return {"available":False}
        if ml_risk:
            candidates=[dict(row) for row in db.execute(select_sql,params)]
            for row in candidates:
                prediction=model_prediction(row["id"])
                row["ml_prediction_available"]=bool(prediction.get("available"))
                row["ml_risk_level"]=prediction.get("risk_level") if prediction.get("available") else None
                row["ml_risk_score"]=prediction.get("risk_score") if prediction.get("available") else None
            candidates=[row for row in candidates if (row["ml_risk_level"] or "UNAVAILABLE")==ml_risk]
            risk_rank={"BREACHED":0,"AT RISK":1,"ON TRACK":2}
            python_key={"deadline":lambda r:r["deadline"],"time_remaining":lambda r:r["deadline"],"submission_date":lambda r:r["submission_date"],"case_id":lambda r:r["id"],"workflow_status":lambda r:r["status"],"sla_risk":lambda r:risk_rank.get(r["risk"],3),"last_updated":lambda r:r["last_updated"]}[sort_by]
            candidates.sort(key=python_key,reverse=sort_order=="desc")
            total=len(candidates); start=(page-1)*page_size; items=candidates[start:start+page_size]
        else:
            total=db.execute("SELECT COUNT(*) n"+joins+where,params).fetchone()["n"]
            items=[dict(row) for row in db.execute(select_sql+f" ORDER BY {sort_expr} {sort_order.upper()},c.id LIMIT ? OFFSET ?",[*params,page_size,(page-1)*page_size])]
            for row in items:
                prediction=model_prediction(row["id"])
                row["ml_prediction_available"]=bool(prediction.get("available"))
                row["ml_risk_level"]=prediction.get("risk_level") if prediction.get("available") else None
                row["ml_risk_score"]=prediction.get("risk_score") if prediction.get("available") else None
        total_pages=ceil(total/page_size) if total else 0
        for row in items:
            try:
                deadline=datetime.fromisoformat(row["deadline"].replace("Z","+00:00"))
                if deadline.tzinfo is None: deadline=deadline.replace(tzinfo=timezone.utc)
                row["hours_remaining"]=round((deadline-datetime.now(timezone.utc)).total_seconds()/3600,1)
            except (ValueError,TypeError): row["hours_remaining"]=None
        scope_sql=""
        scope_params=[]
        if user["role"]=="SPECIALIST": scope_sql=" WHERE a.specialist_id=?"; scope_params.append(user["id"])
        elif user["role"]=="SUPERVISOR": scope_sql=" WHERE a.supervisor_id=?"; scope_params.append(user["id"])
        all_cases=[dict(row) for row in db.execute("SELECT c.status,"+sla_sql+" risk,c.deadline FROM cases c LEFT JOIN pa_case_assignments a ON a.case_id=c.id"+scope_sql,scope_params)]
        summary={"awaiting_specialist":0,"awaiting_supervisor":0,"awaiting_manager":0,"more_information_required":0,"approved":0,"denied":0,"high_sla_risk":0,"overdue":0}
        for row in all_cases:
            key={"SPECIALIST_REVIEW":"awaiting_specialist","NEW":"awaiting_specialist","INTAKE REVIEW":"awaiting_specialist","EVIDENCE REVIEW":"awaiting_specialist","AWAITING_SUPERVISOR":"awaiting_supervisor","AWAITING_MANAGER":"awaiting_manager","MORE INFORMATION REQUIRED":"more_information_required","APPROVED":"approved","DENIED":"denied"}.get(row["status"])
            if key: summary[key]+=1
            if row["risk"] in ("AT RISK","BREACHED"): summary["high_sla_risk"]+=1
            try:
                if datetime.fromisoformat(row["deadline"].replace("Z","+00:00"))<datetime.now(timezone.utc) and row["status"] not in FINAL: summary["overdue"]+=1
            except (ValueError,TypeError): pass
    return {"items":items,"summary":summary,"page":page,"page_size":page_size,"total_items":total,"total_pages":total_pages}


@router.get("/approvals/{case_id}")
def approval_detail(case_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        case = case_access(db, case_id, user)
        documents = [dict(r) for r in db.execute("SELECT id,name,status,category,ocr_status,processing_status,processing_error,evidence_summary FROM documents WHERE case_id=?",(case_id,))]
        notes = [dict(r) for r in db.execute("SELECT n.*,s.display_name FROM pa_notes n JOIN pa_staff s ON s.id=n.actor_id WHERE case_id=? ORDER BY n.id DESC",(case_id,))]
        history = [dict(r) for r in db.execute("SELECT r.*,s.display_name FROM pa_reviews r JOIN pa_staff s ON s.id=r.actor_id WHERE case_id=? ORDER BY r.id DESC",(case_id,))]
        audit = [dict(r) for r in db.execute("SELECT * FROM audit_events WHERE case_id=? ORDER BY id DESC LIMIT 100",(case_id,))]
        assignments = dict(db.execute("SELECT * FROM pa_case_assignments WHERE case_id=?",(case_id,)).fetchone() or {})
        evidence = [dict(r) for r in db.execute("SELECT created_at,verification_status,required_documents,missing_documents,possible_mismatches,explanation,recommended_action,needs_human_review FROM evidence_recommendations WHERE case_id=? ORDER BY id DESC LIMIT 5",(case_id,))]
        sla = [dict(r) for r in db.execute("SELECT created_at,risk_level,remaining_hours,explanation,recommended_action,assigned_role,confidence,needs_human_review FROM sla_recommendations WHERE case_id=? ORDER BY id DESC LIMIT 5",(case_id,))]
        escalations = [dict(r) for r in db.execute("SELECT created_at,escalation_required,escalation_level,reason,responsible_role,recommended_action,notification_status FROM escalation_recommendations WHERE case_id=? ORDER BY id DESC LIMIT 20",(case_id,))]
        escalation_history = [dict(r) for r in db.execute("""SELECT e.*,f.display_name from_staff_name,t.display_name to_staff_name
            FROM pa_escalations e JOIN pa_staff f ON f.id=e.from_staff_id JOIN pa_staff t ON t.id=e.to_staff_id
            WHERE e.case_id=? ORDER BY e.id DESC""",(case_id,))]
        failures = [dict(r) for r in db.execute("SELECT occurred_at,error_code,error_details,workflow_state FROM workflow_failures WHERE case_id=? ORDER BY id DESC LIMIT 20",(case_id,))]
        follow_ups = [dict(r) for r in db.execute("SELECT created_at,responsible_role,reason,status FROM manual_follow_ups WHERE case_id=? ORDER BY id DESC LIMIT 20",(case_id,))]
    try:
        sla_prediction = predict_case_sla_risk(case_id, connection_factory=connect, prediction_function=predict_sla_breach_risk)
    except HTTPException as error:
        sla_prediction = {"available":False,"reason":error.detail}
    return {"case":case,"documents":documents,"notes":notes,"history":history,"audit":audit,"assignments":assignments,"evidence_recommendations":evidence,"sla_recommendations":sla,"sla_prediction":sla_prediction,"escalations":escalations,"escalation_history":escalation_history,"workflow_failures":failures,"follow_ups":follow_ups}


@router.post("/approvals/{case_id}/notes")
def add_note(case_id: str, payload: Note, user: dict = Depends(current_user)):
    if user["role"] not in {"SPECIALIST","SUPERVISOR","MANAGER"}: raise HTTPException(403,"Administrators do not review clinical cases")
    with connect() as db:
        case_access(db,case_id,user)
        db.execute("INSERT INTO pa_notes(case_id,actor_id,role,note,created_at) VALUES(?,?,?,?,?)",(case_id,user["id"],user["role"],payload.note,now()))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",(case_id,now(),user["display_name"],"REVIEW_NOTE_ADDED",payload.note,"HUMAN"))
    return {"ok":True}


@router.post("/approvals/{case_id}/action")
def action(case_id: str, payload: Action, user: dict = Depends(current_user)):
    if payload.action == "ACKNOWLEDGE_ESCALATION":
        require(user,"SUPERVISOR","MANAGER")
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            case=case_access(db,case_id,user)
            if case["status"] in FINAL: raise HTTPException(409,"Finalized cases cannot be changed")
            row=db.execute("SELECT id FROM pa_escalations WHERE case_id=? AND to_staff_id=? AND to_role=? AND status='ROUTED' ORDER BY id DESC LIMIT 1",(case_id,user["id"],user["role"])).fetchone()
            if not row: raise HTTPException(409,"No routed escalation is awaiting your review")
            stamp=now()
            db.execute("UPDATE pa_escalations SET status='ACCEPTED',updated_at=? WHERE id=?",(stamp,row["id"]))
            db.execute("INSERT INTO pa_reviews(case_id,actor_id,action,reason,old_status,new_status,created_at) VALUES(?,?,?,?,?,?,?)",(case_id,user["id"],payload.action,payload.reason,case["status"],case["status"],stamp))
            db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",(case_id,stamp,user["display_name"],"ESCALATION_ACCEPTED",payload.reason,"HUMAN_OPERATIONAL_DECISION"))
        return {"case_id":case_id,"old_status":case["status"],"status":case["status"],"escalation_status":"ACCEPTED"}
    allowed = {
        "SUBMIT": ({"SPECIALIST"}, {"NEW","SPECIALIST_REVIEW","RETURNED_TO_SPECIALIST","INTAKE REVIEW","EVIDENCE REVIEW"}, "AWAITING_SUPERVISOR"),
        "SUPERVISOR_APPROVE": ({"SUPERVISOR"}, {"AWAITING_SUPERVISOR"}, "APPROVED"),
        "FORWARD": ({"SUPERVISOR"}, {"AWAITING_SUPERVISOR"}, "AWAITING_MANAGER"),
        "RETURN_SPECIALIST": ({"SUPERVISOR","MANAGER"}, {"AWAITING_SUPERVISOR","AWAITING_MANAGER"}, "RETURNED_TO_SPECIALIST"),
        "REQUEST_INFORMATION": ({"SPECIALIST","SUPERVISOR","MANAGER"}, {"NEW","SPECIALIST_REVIEW","AWAITING_SUPERVISOR","AWAITING_MANAGER","UNDER_REVIEW"}, "MORE INFORMATION REQUIRED"),
        "APPROVED": ({"MANAGER"}, {"AWAITING_MANAGER","UNDER_REVIEW"}, "APPROVED"),
        "DENIED": ({"MANAGER"}, {"AWAITING_MANAGER","UNDER_REVIEW"}, "DENIED"),
        "UNDER_REVIEW": ({"MANAGER"}, {"AWAITING_MANAGER","UNDER_REVIEW"}, "UNDER_REVIEW"),
        "RETURN_SUPERVISOR": ({"MANAGER"}, {"AWAITING_MANAGER","UNDER_REVIEW"}, "AWAITING_SUPERVISOR"),
    }
    if payload.action not in allowed: raise HTTPException(400,"Unknown workflow action")
    roles, states, next_status = allowed[payload.action]
    require(user,*roles)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        case = case_access(db,case_id,user)
        if case["status"] in FINAL: raise HTTPException(409,"Finalized cases cannot be changed")
        if case["status"] not in states: raise HTTPException(409,f"Action is invalid from {case['status']}")
        if payload.expected_updated_at:
            current = db.execute("SELECT updated_at FROM pa_case_assignments WHERE case_id=?",(case_id,)).fetchone()
            actual = current["updated_at"] if current else None
            if actual != payload.expected_updated_at: raise HTTPException(409,"Case changed since it was opened; refresh before acting")
        # Supervisor approval is a stage review unless policy explicitly allows it as final.
        if payload.action == "SUPERVISOR_APPROVE":
            policy = db.execute("SELECT manager_final_required FROM pa_approval_policy WHERE id=1").fetchone()
            if policy["manager_final_required"]: next_status = "AWAITING_MANAGER"
        old = case["status"]
        routed_role=None
        if payload.action in {"SUBMIT","FORWARD"} or (payload.action=="SUPERVISOR_APPROVE" and next_status=="AWAITING_MANAGER"):
            routed_role="SUPERVISOR" if payload.action=="SUBMIT" else "MANAGER"
            column="supervisor_id" if routed_role=="SUPERVISOR" else "manager_id"
            assignment=db.execute("SELECT * FROM pa_case_assignments WHERE case_id=?",(case_id,)).fetchone()
            target=db.execute("SELECT * FROM pa_staff WHERE id=? AND role=? AND active=1 AND available=1",(assignment[column],routed_role)).fetchone() if assignment and assignment[column] else None
            if target and json.loads(target["service_types"] or "[]") and case["service"] not in json.loads(target["service_types"] or "[]"):
                target=None
            if not target:
                target=db.execute("""SELECT s.* FROM pa_staff s WHERE s.role=? AND s.active=1 AND s.available=1
                    AND (s.service_types='[]' OR s.service_types LIKE ?)
                    AND (SELECT COUNT(*) FROM pa_case_assignments a JOIN cases c ON c.id=a.case_id
                         WHERE a."""+column+"""=s.id AND c.status NOT IN ('APPROVED','DENIED'))<s.max_workload
                    ORDER BY (SELECT COUNT(*) FROM pa_case_assignments a JOIN cases c ON c.id=a.case_id
                         WHERE a."""+column+"""=s.id AND c.status NOT IN ('APPROVED','DENIED')),s.id LIMIT 1""",(routed_role,f'%"{case["service"]}"%')).fetchone()
            if not target: raise HTTPException(409,f"No available {routed_role.title()} can receive this case")
            db.execute(f"UPDATE pa_case_assignments SET {column}=?,updated_at=? WHERE case_id=?",(target["id"],now(),case_id))
            db.execute("INSERT INTO pa_assignment_history(case_id,actor_id,from_staff_id,to_staff_id,role,reason,created_at) VALUES(?,?,?,?,?,?,?)",(case_id,user["id"],assignment[column],target["id"],routed_role,f"Workflow action {payload.action}: {payload.reason}",now()))
            db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",(case_id,now(),user["display_name"],"CASE_ROUTED",f"{payload.action} routed to {target['display_name']} ({routed_role})","HUMAN_WORKFLOW"))
        db.execute("UPDATE cases SET status=? WHERE id=?",(next_status,case_id))
        db.execute("INSERT INTO pa_reviews(case_id,actor_id,action,reason,old_status,new_status,created_at) VALUES(?,?,?,?,?,?,?)",(case_id,user["id"],payload.action,payload.reason,old,next_status,now()))
        audit_detail = json.dumps({
            "reason": payload.reason,
            "previous_state": old,
            "resulting_state": next_status,
        }, sort_keys=True)
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",(case_id,now(),user["display_name"],payload.action,audit_detail,"HUMAN_DECISION" if next_status in FINAL else "HUMAN_WORKFLOW"))
    return {"case_id":case_id,"old_status":old,"status":next_status}


@router.get("/staff")
def staff(user: dict = Depends(current_user)):
    require(user,"ADMIN","MANAGER","SUPERVISOR")
    with connect() as db:
        rows=[dict(r) for r in db.execute("""SELECT s.id,s.username,s.display_name,s.role,s.active,s.available,s.max_workload,s.service_types,
            COUNT(DISTINCT CASE WHEN c.status NOT IN ('APPROVED','DENIED') THEN c.id END) active_cases,
            COUNT(DISTINCT CASE WHEN c.status NOT IN ('APPROVED','DENIED') AND c.risk IN ('AT RISK','BREACHED') THEN c.id END) high_risk_cases,
            COUNT(DISTINCT CASE WHEN c.status NOT IN ('APPROVED','DENIED') AND datetime(c.deadline)<datetime('now') THEN c.id END) overdue_cases,
            COUNT(DISTINCT CASE WHEN c.status NOT IN ('APPROVED','DENIED') AND s.id IN (a.specialist_id,a.supervisor_id,a.manager_id) THEN c.id END) open_assignments
            FROM pa_staff s LEFT JOIN pa_case_assignments a ON s.id IN (a.specialist_id,a.supervisor_id,a.manager_id)
            LEFT JOIN cases c ON c.id=a.case_id GROUP BY s.id ORDER BY s.role,s.display_name""")]
    for row in rows:
        row["service_types"] = json.loads(row.get("service_types") or "[]")
    return rows


@router.get("/staff/{staff_id}")
def staff_detail(staff_id: int, user: dict = Depends(current_user)):
    require(user,"ADMIN")
    with connect() as db:
        row=db.execute("SELECT id,username,display_name,role,active,available,max_workload,service_types,created_at FROM pa_staff WHERE id=?",(staff_id,)).fetchone()
        if not row: raise HTTPException(404,"Staff member not found")
        assignments=[dict(x) for x in db.execute("""SELECT a.case_id,a.role,a.reason,a.created_at,c.status,c.deadline
            FROM pa_assignment_history a JOIN cases c ON c.id=a.case_id WHERE a.to_staff_id=? AND c.status NOT IN ('APPROVED','DENIED') ORDER BY a.id DESC""",(staff_id,))]
        assignment_history=[dict(x) for x in db.execute("SELECT case_id,from_staff_id,to_staff_id,role,reason,created_at FROM pa_assignment_history WHERE from_staff_id=? OR to_staff_id=? ORDER BY id DESC LIMIT 200",(staff_id,staff_id))]
        history=[dict(x) for x in db.execute("SELECT id,actor_id,action,detail,created_at FROM pa_staff_history WHERE staff_id=? ORDER BY id DESC LIMIT 200",(staff_id,))]
        audit=[dict(x) for x in db.execute("SELECT timestamp,actor,action,detail FROM audit_events WHERE detail LIKE ? ORDER BY id DESC LIMIT 100",(f'%staff_id={staff_id};%',))]
        open_assignments=[dict(x) for x in db.execute("""SELECT c.id case_id,c.status,c.deadline,
          CASE WHEN c.status IN ('NEW','SPECIALIST_REVIEW','RETURNED_TO_SPECIALIST','INTAKE REVIEW','EVIDENCE REVIEW') THEN 'SPECIALIST' WHEN c.status='AWAITING_SUPERVISOR' THEN 'SUPERVISOR' ELSE 'MANAGER' END role,
          CASE WHEN c.status IN ('NEW','SPECIALIST_REVIEW','RETURNED_TO_SPECIALIST','INTAKE REVIEW','EVIDENCE REVIEW') THEN a.specialist_id WHEN c.status='AWAITING_SUPERVISOR' THEN a.supervisor_id ELSE a.manager_id END assigned_staff_id
          FROM pa_case_assignments a JOIN cases c ON c.id=a.case_id
          WHERE c.status NOT IN ('APPROVED','DENIED') AND ?=CASE WHEN c.status IN ('NEW','SPECIALIST_REVIEW','RETURNED_TO_SPECIALIST','INTAKE REVIEW','EVIDENCE REVIEW') THEN a.specialist_id WHEN c.status='AWAITING_SUPERVISOR' THEN a.supervisor_id ELSE a.manager_id END ORDER BY c.deadline""",(staff_id,))]
    result=dict(row); result["service_types"]=json.loads(result.get("service_types") or "[]")
    return {"staff":result,"assignments":open_assignments,"open_assignments":open_assignments,"assignment_history":assignment_history,"staff_history":history,"audit":audit,"audit_events":audit}


@router.get("/audit-events")
def audit_events(user: dict = Depends(current_user)):
    require(user,"ADMIN","MANAGER")
    with connect() as db:
        if user["role"]=="ADMIN":
            rows=db.execute("SELECT id,case_id,timestamp,actor,action,detail,decision_type FROM audit_events WHERE case_id='SYSTEM' ORDER BY id DESC LIMIT 500")
        else:
            rows=db.execute("SELECT id,case_id,timestamp,actor,action,detail,decision_type FROM audit_events ORDER BY id DESC LIMIT 500")
        return [dict(r) for r in rows]


@router.post("/staff", status_code=status.HTTP_201_CREATED)
def create_staff(payload: StaffCreate, user: dict = Depends(current_user)):
    require(user,"ADMIN")
    if payload.role not in ROLES: raise HTTPException(400,"Invalid role")
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac("sha256",payload.password.encode(),salt,310000)
    encoded=f"pbkdf2_sha256${salt.hex()}${digest.hex()}"
    try:
        with connect() as db:
            cursor=db.execute("INSERT INTO pa_staff(username,display_name,role,password_hash,active,available,max_workload,service_types,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(payload.username.strip(),payload.display_name.strip(),payload.role,encoded,int(payload.active),int(payload.available),payload.max_workload,json.dumps(payload.service_types),now()))
            staff_id=cursor.lastrowid
            detail=f"staff_id={staff_id}; username={payload.username.strip()}; role={payload.role}; active={payload.active}; available={payload.available}; max_workload={payload.max_workload}"
            db.execute("INSERT INTO pa_staff_history(staff_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",(staff_id,user["id"],"ACCOUNT_CREATED",detail,now()))
            db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES('SYSTEM',?,?,?,?,?)",(now(),user["display_name"],"STAFF_ACCOUNT_CREATED",detail,"HUMAN_OPERATIONAL_DECISION"))
    except Exception as error:
        if "UNIQUE" in str(error): raise HTTPException(409,"Username already exists") from error
        raise
    return {"id":cursor.lastrowid,"username":payload.username.strip(),"role":payload.role}


@router.patch("/staff/{staff_id}")
def update_staff(staff_id: int, payload: StaffUpdate, user: dict = Depends(current_user)):
    require(user,"ADMIN")
    if payload.role not in ROLES: raise HTTPException(400,"Invalid role")
    with connect() as db:
        old=db.execute("SELECT * FROM pa_staff WHERE id=?",(staff_id,)).fetchone()
        if not old: raise HTTPException(404,"Staff member not found")
        old_values={"display_name":old["display_name"],"role":old["role"],"active":bool(old["active"]),"available":bool(old["available"]),"max_workload":old["max_workload"],"service_types":json.loads(old["service_types"] or "[]")}
        new_values={"display_name":payload.display_name.strip(),"role":payload.role,"active":payload.active,"available":payload.available,"max_workload":payload.max_workload,"service_types":payload.service_types}
        changes={key:{"from":old_values[key],"to":new_values[key]} for key in old_values if old_values[key]!=new_values[key]}
        db.execute("UPDATE pa_staff SET display_name=?,role=?,active=?,available=?,max_workload=?,service_types=? WHERE id=?",(payload.display_name.strip(),payload.role,int(payload.active),int(payload.available),payload.max_workload,json.dumps(payload.service_types),staff_id))
        if old["role"]!=payload.role or (old["active"] and not payload.active):
            db.execute("DELETE FROM pa_sessions WHERE staff_id=?",(staff_id,))
        if changes:
            detail=f"staff_id={staff_id}; changes={json.dumps(changes,ensure_ascii=False)}"
            db.execute("INSERT INTO pa_staff_history(staff_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",(staff_id,user["id"],"ACCOUNT_UPDATED",detail,now()))
            db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES('SYSTEM',?,?,?,?,?)",(now(),user["display_name"],"STAFF_ACCOUNT_UPDATED",detail,"HUMAN_OPERATIONAL_DECISION"))
    return {"id":staff_id,"updated":True,"changes":changes}


@router.get("/work-allocation/workload")
def workload(user: dict = Depends(current_user)):
    require(user,"ADMIN","MANAGER","SUPERVISOR")
    with connect() as db:
        return [dict(r) for r in db.execute("""SELECT s.id,s.display_name,s.role,s.available,s.max_workload,
          SUM(CASE WHEN c.status NOT IN ('APPROVED','DENIED') AND (a.specialist_id=s.id OR a.supervisor_id=s.id OR a.manager_id=s.id) THEN 1 ELSE 0 END) active_cases
          FROM pa_staff s LEFT JOIN pa_case_assignments a ON (a.specialist_id=s.id OR a.supervisor_id=s.id OR a.manager_id=s.id)
          LEFT JOIN cases c ON c.id=a.case_id GROUP BY s.id ORDER BY s.role,s.display_name""")]


@router.get("/work-allocation/cases")
def allocation_cases(user: dict = Depends(current_user)):
    require(user,"MANAGER")
    with connect() as db:
        rows=db.execute("""SELECT c.*,a.specialist_id,a.supervisor_id,a.manager_id,
          s.display_name specialist_name,v.display_name supervisor_name,m.display_name manager_name
          FROM cases c LEFT JOIN pa_case_assignments a ON a.case_id=c.id
          LEFT JOIN pa_staff s ON s.id=a.specialist_id LEFT JOIN pa_staff v ON v.id=a.supervisor_id
          LEFT JOIN pa_staff m ON m.id=a.manager_id WHERE c.status IN ('NEW','SPECIALIST_REVIEW','RETURNED_TO_SPECIALIST','INTAKE REVIEW','EVIDENCE REVIEW','MORE INFORMATION REQUIRED','AWAITING_SUPERVISOR')
          ORDER BY c.deadline,c.id""").fetchall()
        return [dict(r) for r in rows]


@router.get("/work-allocation/metrics")
def allocation_metrics(user: dict = Depends(current_user)):
    require(user,"MANAGER")
    with connect() as db:
        settings=db.execute("SELECT warning_hours FROM sla_settings WHERE id=1").fetchone()
        warning_hours=int(settings["warning_hours"]) if settings else 24
        active="c.status NOT IN ('APPROVED','DENIED')"
        totals=db.execute(f"""SELECT COUNT(*) total,
          COALESCE(SUM(CASE WHEN a.case_id IS NULL OR (a.specialist_id IS NULL AND a.supervisor_id IS NULL AND a.manager_id IS NULL) THEN 1 ELSE 0 END),0) unassigned,
          COALESCE(SUM(CASE WHEN a.case_id IS NOT NULL AND (a.specialist_id IS NOT NULL OR a.supervisor_id IS NOT NULL OR a.manager_id IS NOT NULL) THEN 1 ELSE 0 END),0) assigned,
          COALESCE(SUM(CASE WHEN datetime(c.deadline)<datetime('now') OR c.risk='BREACHED' THEN 1 ELSE 0 END),0) sla_breaches,
          COALESCE(SUM(CASE WHEN (datetime(c.deadline)>=datetime('now') AND (julianday(c.deadline)-julianday('now'))*24<=? OR c.risk='AT RISK' OR EXISTS(SELECT 1 FROM documents d WHERE d.case_id=c.id AND d.status!='VERIFIED')) THEN 1 ELSE 0 END),0) sla_warnings
          FROM cases c LEFT JOIN pa_case_assignments a ON a.case_id=c.id WHERE {active}""",(warning_hours,)).fetchone()
        pending=db.execute("SELECT COUNT(*) n FROM pa_escalations WHERE status='ROUTED'").fetchone()["n"]
        priority={r["priority"]:r["n"] for r in db.execute(f"SELECT c.priority,COUNT(*) n FROM cases c WHERE {active} GROUP BY c.priority")}
    return {**dict(totals),"pending_escalations":pending,"by_priority":priority}


@router.post("/work-allocation/bulk-assign")
def bulk_assign(payload: BulkAssignment, user: dict = Depends(current_user)):
    require(user,"MANAGER")
    ids=list(dict.fromkeys(case_id.strip() for case_id in payload.case_ids))
    reason=payload.reason.strip()
    if not ids or not reason: raise HTTPException(400,"Select cases and provide an allocation reason")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        destination=db.execute("SELECT * FROM pa_staff WHERE id=?",(payload.staff_id,)).fetchone()
        if not destination or destination["role"] not in {"SPECIALIST","SUPERVISOR"} or not destination["active"] or not destination["available"]:
            raise HTTPException(400,"Destination must be an active, available Specialist or Supervisor")
        column=destination["role"].lower()+"_id"
        cases=[]
        for case_id in ids:
            case=db.execute("SELECT * FROM cases WHERE id=?",(case_id,)).fetchone()
            if not case: raise HTTPException(404,f"Case {case_id} not found")
            if case["status"] in FINAL: raise HTTPException(409,f"Case {case_id} is resolved and cannot be assigned")
            expected="SPECIALIST" if case["status"] in ("NEW","SPECIALIST_REVIEW","RETURNED_TO_SPECIALIST","INTAKE REVIEW","EVIDENCE REVIEW","MORE INFORMATION REQUIRED") else ("SUPERVISOR" if case["status"]=="AWAITING_SUPERVISOR" else "MANAGER")
            if destination["role"]!=expected: raise HTTPException(400,f"{case_id} requires assignment to a {expected}")
            service_types=json.loads(destination["service_types"] or "[]")
            if service_types and case["service"] not in service_types: raise HTTPException(400,f"{case_id} service is outside the destination's scope")
            cases.append((case_id,case))
        open_count=db.execute(f"SELECT COUNT(*) n FROM pa_case_assignments a JOIN cases c ON c.id=a.case_id WHERE a.{column}=? AND c.status NOT IN ('APPROVED','DENIED')",(payload.staff_id,)).fetchone()["n"]
        additions=sum(1 for case_id,_ in cases if (db.execute(f"SELECT {column} FROM pa_case_assignments WHERE case_id=?",(case_id,)).fetchone() or {column:None})[column]!=payload.staff_id)
        if open_count+additions>destination["max_workload"]: raise HTTPException(409,"Destination staff member does not have capacity for the selected cases")
        for case_id,_ in cases:
            db.execute("INSERT OR IGNORE INTO pa_case_assignments(case_id,updated_at) VALUES(?,?)",(case_id,now()))
            previous=db.execute(f"SELECT {column} staff_id FROM pa_case_assignments WHERE case_id=?",(case_id,)).fetchone()["staff_id"]
            db.execute(f"UPDATE pa_case_assignments SET {column}=?,updated_at=? WHERE case_id=?",(payload.staff_id,now(),case_id))
            db.execute("INSERT INTO pa_assignment_history(case_id,actor_id,from_staff_id,to_staff_id,role,reason,created_at) VALUES(?,?,?,?,?,?,?)",(case_id,user["id"],previous,payload.staff_id,destination["role"],reason,now()))
            db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",(case_id,now(),user["display_name"],"CASE_ASSIGNED" if previous is None else "CASE_REASSIGNED",reason,"HUMAN_OPERATIONAL_DECISION"))
    return {"case_ids":ids,"staff_id":payload.staff_id,"role":destination["role"],"count":len(ids)}


@router.post("/work-allocation/run")
def run_allocation(user: dict = Depends(current_user)):
    require(user,"ADMIN","MANAGER","SUPERVISOR")
    assigned=[]
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if user["role"]=="SUPERVISOR":
            cases=db.execute("SELECT c.* FROM cases c JOIN pa_case_assignments scope ON scope.case_id=c.id WHERE scope.supervisor_id=? AND c.status IN ('NEW','SPECIALIST_REVIEW','RETURNED_TO_SPECIALIST','INTAKE REVIEW','EVIDENCE REVIEW','MORE INFORMATION REQUIRED','AWAITING_SUPERVISOR') ORDER BY CASE WHEN c.risk IN ('BREACHED','AT RISK') THEN 0 ELSE 1 END,c.deadline",(user["id"],)).fetchall()
        else:
            cases=db.execute("SELECT * FROM cases WHERE status IN ('NEW','SPECIALIST_REVIEW','RETURNED_TO_SPECIALIST','INTAKE REVIEW','EVIDENCE REVIEW','MORE INFORMATION REQUIRED','AWAITING_SUPERVISOR') ORDER BY CASE WHEN risk IN ('BREACHED','AT RISK') THEN 0 ELSE 1 END,deadline").fetchall()
        predictions={}
        for case in cases:
            try:
                predictions[case["id"]]=predict_case_sla_risk(case["id"], connection_factory=connect, prediction_function=predict_sla_breach_risk)
            except HTTPException:
                predictions[case["id"]]={"available":False}
        def allocation_priority(case):
            prediction=predictions.get(case["id"],{})
            if case["risk"]=="BREACHED": return (0,case["deadline"])
            if prediction.get("available") and prediction.get("predicted_sla_breach"): return (1,case["deadline"])
            if case["risk"]=="AT RISK": return (2,case["deadline"])
            return (3,case["deadline"])
        cases=sorted(cases,key=allocation_priority)
        for case in cases:
            role="SPECIALIST" if case["status"] in ("NEW","SPECIALIST_REVIEW","RETURNED_TO_SPECIALIST","INTAKE REVIEW","EVIDENCE REVIEW","MORE INFORMATION REQUIRED") else ("SUPERVISOR" if case["status"]=="AWAITING_SUPERVISOR" else "MANAGER")
            column={"SPECIALIST":"specialist_id","SUPERVISOR":"supervisor_id","MANAGER":"manager_id"}[role]
            if db.execute(f"SELECT 1 FROM pa_case_assignments WHERE case_id=? AND {column} IS NOT NULL",(case["id"],)).fetchone(): continue
            staff_row=db.execute(f"""SELECT s.id,COUNT(DISTINCT a.case_id) n FROM pa_staff s LEFT JOIN pa_case_assignments a ON a.{column}=s.id
                LEFT JOIN cases c ON c.id=a.case_id AND c.status NOT IN ('APPROVED','DENIED')
                WHERE s.role=? AND s.active=1 AND s.available=1 GROUP BY s.id HAVING COUNT(DISTINCT CASE WHEN c.id IS NOT NULL THEN c.id END)<s.max_workload ORDER BY COUNT(DISTINCT CASE WHEN c.id IS NOT NULL THEN c.id END),s.id LIMIT 1""",(role,)).fetchone()
            if not staff_row: continue
            db.execute("INSERT OR IGNORE INTO pa_case_assignments(case_id,updated_at) VALUES(?,?)",(case["id"],now()))
            db.execute(f"UPDATE pa_case_assignments SET {column}=?,updated_at=? WHERE case_id=?",(staff_row["id"],now(),case["id"]))
            prediction=predictions.get(case["id"],{})
            priority_note=(f"SLA model predicted elevated breach risk (score {prediction.get('risk_score')}); prioritized for allocation. " if prediction.get("available") and prediction.get("predicted_sla_breach") else ("SLA model prediction unavailable; deterministic SLA status and deadline used. " if not prediction.get("available") else ""))
            reason=priority_note+"Assigned to eligible active, available staff member with the lowest open workload for this workflow role."
            db.execute("INSERT INTO pa_assignment_history(case_id,actor_id,to_staff_id,role,reason,created_at) VALUES(?,?,?,?,?,?)",(case["id"],user["id"],staff_row["id"],role,reason,now()))
            assigned.append(case["id"])
    return {"assigned_case_ids":assigned,"count":len(assigned)}


@router.get("/work-allocation/history")
def allocation_history(user: dict = Depends(current_user)):
    require(user,"ADMIN","MANAGER","SUPERVISOR")
    with connect() as db:
        if user["role"]=="SUPERVISOR":
            rows=db.execute("SELECT h.*,c.payer,c.service,s.display_name staff_name,a.display_name actor_name FROM pa_assignment_history h JOIN cases c ON c.id=h.case_id JOIN pa_case_assignments scope ON scope.case_id=h.case_id JOIN pa_staff s ON s.id=h.to_staff_id JOIN pa_staff a ON a.id=h.actor_id WHERE scope.supervisor_id=? ORDER BY h.id DESC LIMIT 300",(user["id"],))
        elif user["role"]=="ADMIN":
            rows=db.execute("SELECT h.id,h.case_id,h.actor_id,h.from_staff_id,h.to_staff_id,h.role,h.reason,h.created_at,s.display_name staff_name,a.display_name actor_name FROM pa_assignment_history h JOIN pa_staff s ON s.id=h.to_staff_id JOIN pa_staff a ON a.id=h.actor_id ORDER BY h.id DESC LIMIT 300")
        else:
            rows=db.execute("SELECT h.*,c.payer,c.service,s.display_name staff_name,a.display_name actor_name FROM pa_assignment_history h JOIN cases c ON c.id=h.case_id JOIN pa_staff s ON s.id=h.to_staff_id JOIN pa_staff a ON a.id=h.actor_id ORDER BY h.id DESC LIMIT 300")
        return [dict(r) for r in rows]


@router.get("/escalations/requests")
def user_escalations(user: dict = Depends(current_user), status_filter: Literal["ROUTED","ACCEPTED","CLOSED","ALL"] = Query(default="ROUTED", alias="status")):
    if user["role"] not in {"SPECIALIST","SUPERVISOR","MANAGER"}: raise HTTPException(403,"Administrators do not access patient escalation records")
    clauses=[]; params=[]
    if user["role"]=="SPECIALIST": clauses.append("e.from_staff_id=?"); params.append(user["id"])
    elif user["role"]=="SUPERVISOR": clauses.append("(e.to_staff_id=? OR e.from_staff_id=?)"); params.extend([user["id"],user["id"]])
    if status_filter!="ALL": clauses.append("e.status=?"); params.append(status_filter)
    where=" WHERE "+" AND ".join(clauses) if clauses else ""
    with connect() as db:
        return [dict(row) for row in db.execute("""SELECT e.*,c.payer,c.service,c.status case_status,c.deadline,
          f.display_name from_staff_name,t.display_name to_staff_name
          FROM pa_escalations e JOIN cases c ON c.id=e.case_id
          JOIN pa_staff f ON f.id=e.from_staff_id JOIN pa_staff t ON t.id=e.to_staff_id"""+where+" ORDER BY e.id DESC LIMIT 300",params)]


@router.post("/work-allocation/{case_id}/assign")
def manual_assign(case_id: str, payload: Assignment, user: dict = Depends(current_user)):
    require(user,"ADMIN","MANAGER","SUPERVISOR")
    reason=payload.reason.strip()
    if not reason: raise HTTPException(400,"A reassignment reason is required")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        case=db.execute("SELECT * FROM cases WHERE id=?",(case_id,)).fetchone()
        if not case: raise HTTPException(404,"Case not found")
        if case["status"] in FINAL: raise HTTPException(409,"Resolved cases cannot be assigned")
        column,role=("specialist_id","SPECIALIST") if case["status"] in ("NEW","SPECIALIST_REVIEW","RETURNED_TO_SPECIALIST","INTAKE REVIEW","EVIDENCE REVIEW","MORE INFORMATION REQUIRED") else (("supervisor_id","SUPERVISOR") if case["status"]=="AWAITING_SUPERVISOR" else ("manager_id","MANAGER"))
        destination=db.execute("SELECT * FROM pa_staff WHERE id=?",(payload.staff_id,)).fetchone()
        if not destination or destination["role"]!=role or not destination["active"] or not destination["available"]:
            raise HTTPException(400,"Destination must be active, available, and have the required workflow role")
        service_types=json.loads(destination["service_types"] or "[]")
        if service_types and case["service"] not in service_types:
            raise HTTPException(400,"Destination is not authorized for this service type")
        count=db.execute(f"SELECT COUNT(*) n FROM pa_case_assignments a JOIN cases c ON c.id=a.case_id WHERE a.{column}=? AND c.status NOT IN ('APPROVED','DENIED')",(payload.staff_id,)).fetchone()["n"]
        if count>=destination["max_workload"]: raise HTTPException(409,"Destination staff member is at workload capacity")
        db.execute("INSERT OR IGNORE INTO pa_case_assignments(case_id,updated_at) VALUES(?,?)",(case_id,now()))
        previous=db.execute(f"SELECT {column} staff_id FROM pa_case_assignments WHERE case_id=?",(case_id,)).fetchone()["staff_id"]
        db.execute(f"UPDATE pa_case_assignments SET {column}=?,updated_at=? WHERE case_id=?",(payload.staff_id,now(),case_id))
        db.execute("INSERT INTO pa_assignment_history(case_id,actor_id,from_staff_id,to_staff_id,role,reason,created_at) VALUES(?,?,?,?,?,?,?)",(case_id,user["id"],previous,payload.staff_id,role,reason,now()))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",(case_id,now(),user["display_name"],"CASE_REASSIGNED",reason,"HUMAN_OPERATIONAL_DECISION"))
    return {"case_id":case_id,"staff_id":payload.staff_id,"role":role}

