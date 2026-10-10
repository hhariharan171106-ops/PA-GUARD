import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from agents import run_sla_agent, run_sla_rag
from evidence_agents import run_evidence_rag
from escalation_agents import run_escalation_rag
from database import connect, initialize
from rag import index_policies

app = FastAPI(title="PA Guard API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"https?://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    approve_notification: bool = False
    actor: str

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
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "PA Guard API"}


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
        documents = [dict(row) for row in db.execute("SELECT * FROM documents WHERE case_id=?",(case_id,))]
    return {"case":dict(case),"documents":documents,"agent_status":"SLA RAG CONNECTED — RECOMMENDATION ONLY"}

@app.get("/api/documents")
def list_documents() -> list[dict]:
    with connect() as db: return [dict(row) for row in db.execute("SELECT * FROM documents ORDER BY case_id,id")]

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
@app.post("/api/agents/escalation/cases/{case_id}/evaluate")
def evaluate_escalation(case_id: str, payload: AgentRun) -> dict:
    try:
        result = run_escalation_rag(case_id, payload.actor, payload.approve_notification)
        return {"mode": "recommendation_only", "engine": "LangGraph RAG + deterministic Python rules + local Ollama explanation", "decision": result}
    except ValueError as error:
        raise HTTPException(404, str(error)) from error




@app.post("/api/agents/evidence/cases/{case_id}/review")
def review_evidence(case_id: str, payload: AgentRun) -> dict:
    try:
        result = run_evidence_rag(case_id)
        return {"mode": "recommendation_only", "engine": "LangGraph RAG + deterministic metadata comparison + local Ollama explanation", "finding": result}
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    return {"run_at":datetime.now(timezone.utc).isoformat(),"mode":"recommendation_only","engine":"LangGraph RAG + deterministic Python rules + local Ollama explanations","recommendations":recommendations}

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
@app.get("/api/role-assignments")
def list_role_assignments() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM escalation_role_assignments ORDER BY department,escalation_level,effective_date DESC,id DESC")]


@app.post("/api/role-assignments")
def create_role_assignment(payload: RoleAssignmentCreate) -> dict:
    values = payload.model_dump()
    validate_role_assignment(values)
    with connect() as db:
        cursor = db.execute("INSERT INTO escalation_role_assignments(escalation_level,responsible_role,department,effective_date,expires_at) VALUES(?,?,?,?,?)", (values["escalation_level"], values["responsible_role"], values["department"], values["effective_date"], values["expires_at"]))
        result = dict(db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (cursor.lastrowid,)).fetchone())
    audit("SYSTEM", "Current user", "ROLE_ASSIGNMENT_CREATED", f"Assigned {values['responsible_role']} to {values['escalation_level']} for {values['department']} effective {values['effective_date']}.", "HUMAN_OPERATIONAL_DECISION")
    return result


@app.put("/api/role-assignments/{assignment_id}")
def update_role_assignment(assignment_id: int, payload: RoleAssignmentUpdate) -> dict:
    values = payload.model_dump()
    with connect() as db:
        if not db.execute("SELECT id FROM escalation_role_assignments WHERE id=?", (assignment_id,)).fetchone():
            raise HTTPException(404, "Role assignment not found")
    validate_role_assignment(values, assignment_id)
    with connect() as db:
        db.execute("UPDATE escalation_role_assignments SET escalation_level=?,responsible_role=?,department=?,effective_date=?,expires_at=? WHERE id=?", (values["escalation_level"], values["responsible_role"], values["department"], values["effective_date"], values["expires_at"], assignment_id))
        result = dict(db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (assignment_id,)).fetchone())
    audit("SYSTEM", "Current user", "ROLE_ASSIGNMENT_UPDATED", f"Updated role assignment {assignment_id} for {values['department']}.", "HUMAN_OPERATIONAL_DECISION")
    return result


@app.post("/api/role-assignments/{assignment_id}/retire")
def retire_role_assignment(assignment_id: int) -> dict:
    retired_on = date.today().isoformat()
    with connect() as db:
        assignment = db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (assignment_id,)).fetchone()
        if not assignment:
            raise HTTPException(404, "Role assignment not found")
        if assignment["expires_at"] and assignment["expires_at"] <= retired_on:
            raise HTTPException(409, "Role assignment is already retired")
        if assignment["effective_date"] > retired_on:
            raise HTTPException(409, "Future assignments must be edited rather than retired")
        db.execute("UPDATE escalation_role_assignments SET expires_at=? WHERE id=?", (retired_on, assignment_id))
        result = dict(db.execute("SELECT * FROM escalation_role_assignments WHERE id=?", (assignment_id,)).fetchone())
    audit("SYSTEM", "Current user", "ROLE_ASSIGNMENT_RETIRED", f"Retired role assignment {assignment_id} on {retired_on}.", "HUMAN_OPERATIONAL_DECISION")
    return result


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
