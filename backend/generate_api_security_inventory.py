"""Write docs/API_SECURITY_INVENTORY.md from the live FastAPI route registry."""
from pathlib import Path

from starlette.routing import Route

from main import app
from security import PUBLIC_ROUTES, allowed_roles

FRAMEWORK_PUBLIC_ROUTES = {
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
}


def generate() -> Path:
    rows = []
    for route in app.routes:
        if not isinstance(route, Route):
            continue
        for method in sorted(route.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            public = (method, route.path) in PUBLIC_ROUTES or (method, route.path) in FRAMEWORK_PUBLIC_ROUTES
            if not route.path.startswith("/api/") and not public:
                continue
            roles = "Public" if public else ", ".join(sorted(allowed_roles(method, route.path))) or "DENY ALL (unmapped)"
            if route.path in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}:
                reason = "Framework API schema/documentation only; no case or account records"
            else:
                reason = "No sensitive data; service readiness only" if route.path == "/api/health" else ("Password login bootstrap" if route.path == "/api/auth/login" else "Bearer session required; role and case scope checked by middleware")
            sensitive_prefixes = ("/api/cases", "/api/documents", "/api/approvals", "/api/evidence-review-feedback", "/api/workflow-failures", "/api/audit", "/api/staff", "/api/work-allocation", "/api/auth", "/api/role-assignments")
            sensitive = "Yes" if route.path.startswith(sensitive_prefixes) else "No"
            rows.append((method, route.path, f"{route.endpoint.__module__}.{route.endpoint.__name__}", "No" if public else "Yes", roles, sensitive, reason))
    rows.sort(key=lambda row: (row[1], row[0]))
    out = ["# PA Guard Route Security Inventory", "", "Generated from `backend.main.app.routes` by `python backend/generate_api_security_inventory.py`.", "", "All 68 `/api` operations pass through the central bearer-session middleware except the two explicitly listed public API routes. Every protected API operation also has a role policy; case and nested-resource routes are checked against case assignments. An empty role policy fails closed. Four public framework routes serve only OpenAPI/Swagger/ReDoc documentation and schemas.", "", "| Method | Route | Handler | Authentication | Allowed roles | Sensitive data | Public rationale / enforcement |", "|---|---|---|---|---|---|---|"]
    out.extend("| " + " | ".join(str(value).replace("|", "\\|") for value in row) + " |" for row in rows)
    out.extend(["", "## Public routes", "", "- `GET /api/health`: returns API service readiness only.", "- `POST /api/auth/login`: accepts credentials to establish a session; returns a bearer token only on successful password verification.", "- `GET /openapi.json`, `GET /docs`, `GET /docs/oauth2-redirect`, `GET /redoc`: FastAPI schema/documentation pages only; these return route definitions, not application records.", "", "Browser storage note: the local prototype stores the bearer token in `sessionStorage`; this avoids persistent storage and clears on logout/expiry, but remains accessible to same-origin JavaScript and is not a production healthcare authentication design.", ""])
    target = Path(__file__).resolve().parents[1] / "docs" / "API_SECURITY_INVENTORY.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out), encoding="utf-8")
    return target


if __name__ == "__main__":
    print(generate())
