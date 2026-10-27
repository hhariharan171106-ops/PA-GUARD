"""Shared role and case-scope policy for every registered /api route."""
from __future__ import annotations

from fastapi import HTTPException

from database import connect

ROLES = {"SPECIALIST", "SUPERVISOR", "MANAGER", "ADMIN"}
PUBLIC_ROUTES = {("GET", "/api/health"), ("POST", "/api/auth/login")}


def allowed_roles(method: str, path: str) -> set[str]:
    method=method.upper()
    parts=path.strip("/").split("/")
    if parts[:2] == ["api","auth"]:
        return ROLES
    if path.startswith("/api/staff"):
        return {"ADMIN","SUPERVISOR","MANAGER"} if method == "GET" and path.rstrip("/") == "/api/staff" else {"ADMIN"}
    if path.startswith("/api/work-allocation"):
        if path.endswith("/cases") or path.endswith("/bulk-assign"): return {"MANAGER"}
        if path.endswith("/assign"): return {"ADMIN","SUPERVISOR","MANAGER"}
        if path.endswith("/workload"): return {"ADMIN","SUPERVISOR","MANAGER"}
        if path.endswith("/history"): return {"ADMIN","SUPERVISOR","MANAGER"}
        return {"ADMIN","SUPERVISOR","MANAGER"}
    if path.startswith("/api/approvals"):
        return {"SPECIALIST","SUPERVISOR","MANAGER"}
    if path.startswith("/api/escalations/requests"):
        return {"SPECIALIST","SUPERVISOR","MANAGER"}
    if path.startswith("/api/approval-policy"):
        return ROLES if method=="GET" else {"ADMIN"}
    if path.startswith("/api/cases"):
        if "/evidence-trail/" in path and path.endswith("/review"):
            return {"SUPERVISOR", "MANAGER"}
        if path.endswith("/evidence-trail"):
            return {"SPECIALIST", "SUPERVISOR", "MANAGER"}
        if len(parts)==2: return {"SPECIALIST","SUPERVISOR","MANAGER"} if method=="GET" else {"MANAGER"}
        if "/submission/approve" in path or "/submission/submit-demo" in path:
            return {"MANAGER"}
        if "/submission/readiness" in path:
            return {"SPECIALIST","SUPERVISOR","MANAGER"}
        if method=="PATCH": return {"MANAGER"}
        if "/documents" in path:
            return {"SPECIALIST","SUPERVISOR","MANAGER"}
        if "/escalate" in path: return {"SPECIALIST","SUPERVISOR"}
        if "/evidence-review-feedback" in path: return {"SPECIALIST","SUPERVISOR"}
        return {"SPECIALIST","SUPERVISOR","MANAGER"}
    if path.startswith("/api/documents"):
        if method=="GET": return {"SPECIALIST","SUPERVISOR","MANAGER"}
        if path.endswith("/verify"): return {"SPECIALIST","SUPERVISOR"}
        return {"SPECIALIST","SUPERVISOR","MANAGER"}
    if path.startswith("/api/evidence-review-feedback"):
        if "training-export" in path: return {"MANAGER"}
        return {"SPECIALIST","SUPERVISOR"}
    if path.startswith("/api/settings"):
        if method=="GET": return {"SPECIALIST","SUPERVISOR","MANAGER"}
        return {"MANAGER"}
    if path.startswith("/api/agents/sla/index"): return {"ADMIN","MANAGER"}
    if path.startswith("/api/agents/sla/run"): return {"SUPERVISOR","MANAGER"}
    if path.startswith("/api/agents/recovery"):
        if method == "POST" and path.endswith("/recover"):
            return {"SPECIALIST", "SUPERVISOR", "MANAGER"}
        return {"SUPERVISOR", "MANAGER"}
    if path.startswith("/api/agents/escalation"):
        return {"SUPERVISOR","MANAGER"}
    if path.startswith("/api/agents"): return {"SPECIALIST","SUPERVISOR","MANAGER"}
    if path.startswith("/api/workflow-failures"):
        return {"SPECIALIST", "SUPERVISOR", "MANAGER"} if method == "GET" else set()
    if path == "/api/sla-risk-model-card": return ROLES
    if path.startswith("/api/audit"): return {"ADMIN","MANAGER"}
    if path.startswith("/api/role-assignments"):
        if method=="GET": return {"ADMIN","SUPERVISOR","MANAGER"}
        return {"ADMIN","MANAGER"}
    if path.startswith("/api/policies") or path.startswith("/api/payer-requirements") or path.startswith("/api/escalation-policies"):
        if path.endswith("/verify-source") or path.endswith("/approve-source"):
            return {"ADMIN", "MANAGER"}
        if method=="GET": return ROLES
        return {"ADMIN","SUPERVISOR","MANAGER"}
    # Fail closed for future API routes: authenticated roles must be added to this policy.
    return set()


def _case_id_for_path(path: str) -> str | None:
    parts=path.strip("/").split("/")
    if len(parts)>=3 and parts[:2]==["api","cases"]: return parts[2]
    if len(parts)>=3 and parts[:2]==["api","approvals"]: return parts[2]
    if "cases" in parts:
        index=parts.index("cases")
        if index+1<len(parts): return parts[index+1]
    if len(parts)>=4 and parts[:2]==["api","work-allocation"] and parts[3]=="assign": return parts[2]
    return None


def enforce_case_scope(user: dict, case_id: str) -> None:
    if user["role"]=="ADMIN": raise HTTPException(403,"System administrators do not access patient case records")
    with connect() as db:
        case=db.execute("SELECT id FROM cases WHERE id=?",(case_id,)).fetchone()
        if not case: raise HTTPException(404,"Case not found")
        if user["role"]=="MANAGER": return
        assignment=db.execute("SELECT specialist_id,supervisor_id FROM pa_case_assignments WHERE case_id=?",(case_id,)).fetchone()
    if user["role"]=="SPECIALIST" and assignment and assignment["specialist_id"]==user["id"]: return
    if user["role"]=="SUPERVISOR" and assignment and assignment["supervisor_id"]==user["id"]: return
    raise HTTPException(404,"Case not found")


def enforce_resource_scope(user: dict, method: str, path: str) -> None:
    parts=path.strip("/").split("/")
    # Admins may reassign a case without receiving its clinical record contents.
    if user["role"] == "ADMIN" and len(parts)==4 and parts[:2]==["api","work-allocation"] and parts[3]=="assign":
        return
    case_id=_case_id_for_path(path)
    if case_id:
        enforce_case_scope(user,case_id)
        return
    with connect() as db:
        if len(parts)>=3 and parts[:2]==["api","documents"] and parts[2].isdigit():
            row=db.execute("SELECT case_id FROM documents WHERE id=?",(int(parts[2]),)).fetchone()
            if not row: raise HTTPException(404,"Document not found")
            enforce_case_scope(user,row["case_id"])
        if len(parts)>=3 and parts[:2]==["api","evidence-review-feedback"] and parts[2].isdigit():
            row=db.execute("SELECT case_id FROM evidence_review_feedback WHERE id=?",(int(parts[2]),)).fetchone()
            if not row: raise HTTPException(404,"Feedback record not found")
            enforce_case_scope(user,row["case_id"])
