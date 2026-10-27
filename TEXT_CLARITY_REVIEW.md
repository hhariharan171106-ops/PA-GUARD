# PA Guard Text Clarity Review

## Focus

The review is limited to AI/SLA explanations, Evidence Review feedback, escalation and assignment feedback, and document upload feedback.

## Findings and planned changes

- `backend/rag.py` sends Ollama facts through prompts that ask for concise explanations but do not consistently request plain English, a practical next step, and no raw JSON. Keep the real model calls and case facts; tighten only the wording instructions and reject JSON-shaped responses so the existing fallback can be used.
- `backend/case_workflow.py` builds a dense fallback with technical labels and status codes. Present the same stored facts as short case-summary lines, clearly separating an experimental estimate from deadline rules and human document review.
- `apps/pa-guard/src/pages/evidence.tsx` and `apps/pa-guard/src/lib/pa-context.tsx` use jargon-heavy success and upload notices. Use short action/outcome messages and retain the actual safe reason when a document fails.
- `apps/pa-guard/src/pages/case-details.tsx` and `apps/pa-guard/src/pages/case-allocation.tsx` show raw HTTP details or technical upload states. Keep case IDs, staff names, and error reasons, while removing status-code prefixes and giving the next step.
- API errors need consistent wording for common authorization, validation, missing-record, conflict, and service failures. Add a shared formatter without changing requests or responses.

## Accuracy safeguards

Do not change LLM providers, facts, API contracts, workflows, database content, policy rules, or risk calculations. Do not describe document text matches as clinical verification or synthetic risk estimates as reliable real-world probabilities. If the AI response is unavailable or is not readable text, retain the existing factual fallback and say the AI explanation could not be generated.
