# PA Guard Route Security Inventory

Generated from `backend.main.app.routes` by `python backend/generate_api_security_inventory.py`.

All 68 `/api` operations pass through the central bearer-session middleware except the two explicitly listed public API routes. Every protected API operation also has a role policy; case and nested-resource routes are checked against case assignments. An empty role policy fails closed. Four public framework routes serve only OpenAPI/Swagger/ReDoc documentation and schemas.

| Method | Route | Handler | Authentication | Allowed roles | Sensitive data | Public rationale / enforcement |
|---|---|---|---|---|---|---|
| POST | /api/agents/cases/{case_id}/workflow | main.run_case_ai_workflow | Yes | MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/agents/cases/{case_id}/workflow/retry | main.retry_case_ai_workflow | Yes | MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/agents/escalation/cases/{case_id}/evaluate | main.evaluate_escalation | Yes | MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/agents/evidence/cases/{case_id}/predict | main.predict_case_evidence | Yes | MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/agents/evidence/cases/{case_id}/review | main.review_evidence | Yes | MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/agents/recovery/cases/{case_id}/recover | main.recover_case | Yes | MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/agents/sla/cases/{case_id}/recommend | main.recommend_case | Yes | MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/agents/sla/index | main.reindex_policies | Yes | ADMIN, MANAGER | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/agents/sla/run | main.run_sla | Yes | MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| GET | /api/agents/status | main.agent_status | Yes | MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| GET | /api/approval-policy | approval_api.get_approval_policy | Yes | ADMIN, MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| PUT | /api/approval-policy | approval_api.set_approval_policy | Yes | ADMIN | No | Bearer session required; role and case scope checked by middleware |
| GET | /api/approvals | approval_api.approvals | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/approvals/{case_id} | approval_api.approval_detail | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/approvals/{case_id}/action | approval_api.action | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/approvals/{case_id}/notes | approval_api.add_note | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/audit | main.list_audit | Yes | ADMIN, MANAGER | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/audit-events | approval_api.audit_events | Yes | ADMIN, MANAGER | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/auth/login | approval_api.login | No | Public | Yes | Password login bootstrap |
| POST | /api/auth/logout | approval_api.logout | Yes | ADMIN, MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/auth/me | approval_api.me | Yes | ADMIN, MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/cases | main.list_cases | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/cases | main.create_case | Yes | MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/cases/{case_id} | main.case_detail | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| PATCH | /api/cases/{case_id} | main.update_case | Yes | MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/cases/{case_id}/documents | main.list_case_documents | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/cases/{case_id}/documents | main.upload_document | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/cases/{case_id}/escalate | main.escalate | Yes | MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/cases/{case_id}/evidence-review-feedback | main.list_evidence_review_feedback | Yes | SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/cases/{case_id}/evidence-review-feedback | main.submit_evidence_review_feedback | Yes | SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/cases/{case_id}/sla-risk-prediction | main.predict_case_sla_risk | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/documents | main.list_documents | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/documents/{document_id} | main.get_document | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/documents/{document_id}/process | main.process_document | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/documents/{document_id}/text | main.get_document_text | Yes | MANAGER, SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/documents/{document_id}/verify | main.verify_document | Yes | SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/escalation-policies | main.list_escalation_policies | Yes | ADMIN, MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/escalation-policies | main.create_escalation_policy | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| PUT | /api/escalation-policies/{policy_id}/{version} | main.update_escalation_policy | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/escalation-policies/{policy_id}/{version}/approval | main.set_escalation_policy_approval | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| GET | /api/evidence-review-feedback/training-export | main.export_evidence_review_training_data | Yes | MANAGER | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/evidence-review-feedback/{feedback_id}/review | main.review_evidence_feedback | Yes | SPECIALIST, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/health | main.health | No | Public | No | No sensitive data; service readiness only |
| GET | /api/payer-requirements | main.list_payer_requirements | Yes | ADMIN, MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/payer-requirements | main.create_payer_requirement | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| PUT | /api/payer-requirements/{requirement_id}/{version} | main.update_payer_requirement | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/payer-requirements/{requirement_id}/{version}/approval | main.set_requirement_approval | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| GET | /api/policies | main.list_policies | Yes | ADMIN, MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/policies | main.create_policy | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| PUT | /api/policies/{policy_id}/{version} | main.update_policy | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| POST | /api/policies/{policy_id}/{version}/approval | main.set_policy_approval | Yes | ADMIN, MANAGER, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| GET | /api/role-assignments | main.list_role_assignments | Yes | ADMIN, MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/role-assignments | main.create_role_assignment | Yes | ADMIN, MANAGER | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/role-assignments/history | main.list_role_assignment_history | Yes | ADMIN, MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| PUT | /api/role-assignments/{assignment_id} | main.update_role_assignment | Yes | ADMIN, MANAGER | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/role-assignments/{assignment_id}/retire | main.retire_role_assignment | Yes | ADMIN, MANAGER | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/settings | main.get_settings | Yes | MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| PUT | /api/settings | main.update_settings | Yes | MANAGER | No | Bearer session required; role and case scope checked by middleware |
| GET | /api/settings/history | main.get_settings_history | Yes | MANAGER, SPECIALIST, SUPERVISOR | No | Bearer session required; role and case scope checked by middleware |
| GET | /api/staff | approval_api.staff | Yes | ADMIN, MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/staff | approval_api.create_staff | Yes | ADMIN | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/staff/{staff_id} | approval_api.staff_detail | Yes | ADMIN | Yes | Bearer session required; role and case scope checked by middleware |
| PATCH | /api/staff/{staff_id} | approval_api.update_staff | Yes | ADMIN | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/work-allocation/history | approval_api.allocation_history | Yes | ADMIN, MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/work-allocation/run | approval_api.run_allocation | Yes | ADMIN, MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/work-allocation/workload | approval_api.workload | Yes | ADMIN, MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| POST | /api/work-allocation/{case_id}/assign | approval_api.manual_assign | Yes | ADMIN, MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /api/workflow-failures | main.list_workflow_failures | Yes | MANAGER, SUPERVISOR | Yes | Bearer session required; role and case scope checked by middleware |
| GET | /docs | fastapi.applications.swagger_ui_html | No | Public | No | Framework API schema/documentation only; no case or account records |
| GET | /docs/oauth2-redirect | fastapi.applications.swagger_ui_redirect | No | Public | No | Framework API schema/documentation only; no case or account records |
| GET | /openapi.json | fastapi.applications.openapi | No | Public | No | Framework API schema/documentation only; no case or account records |
| GET | /redoc | fastapi.applications.redoc_html | No | Public | No | Framework API schema/documentation only; no case or account records |

## Public routes

- `GET /api/health`: returns API service readiness only.
- `POST /api/auth/login`: accepts credentials to establish a session; returns a bearer token only on successful password verification.
- `GET /openapi.json`, `GET /docs`, `GET /docs/oauth2-redirect`, `GET /redoc`: FastAPI schema/documentation pages only; these return route definitions, not application records.

Browser storage note: the local prototype stores the bearer token in `sessionStorage`; this avoids persistent storage and clears on logout/expiry, but remains accessible to same-origin JavaScript and is not a production healthcare authentication design.
