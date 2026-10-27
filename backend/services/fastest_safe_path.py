"""Pure, deterministic recommendations for the human actions a case may need."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Verdict = Literal["ACHIEVABLE", "TIGHT", "INFEASIBLE_WITHOUT_EXTENSION", "BLOCKED_BY_PERMISSION", "UNCERTAIN", "READY_FOR_HUMAN_SUBMISSION_REVIEW"]
DISCLAIMER = "Recommendation only. Does not confirm evidence, verify policy, or approve authorization."


class PlanStep(BaseModel):
    order: int
    action_type: str
    description: str
    required_role: str | None
    estimated_minutes: int | None
    is_estimate: bool
    blocking: bool
    criterion_id: str | None = None
    document_id: int | None = None
    can_current_user_perform: bool
    dependencies: list[str] = Field(default_factory=list)
    parallelizable: bool = False
    external_wait_unknown: bool = False


class Plan(BaseModel):
    case_id: str
    generated_at: datetime
    time_remaining_minutes: float | None
    verdict: Verdict
    verdict_explanation: str
    critical_path_minutes: int | None
    steps: list[PlanStep]
    unresolved_gaps: list[str]
    assumptions: list[str]
    disclaimer: str = DISCLAIMER
    duration_is_estimate: bool = True
    external_wait_unknown: bool = False
    deadline_unknown: bool = False


def _aware(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline and now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _actor_can(role: str, required: str | None) -> bool:
    # Existing application permissions: supervisor/manager review evidence;
    # only a manager may perform manager review or policy-owner work.
    if required is None:
        return False
    if required == "SPECIALIST":
        return role in {"SPECIALIST", "SUPERVISOR", "MANAGER"}
    if required == "SUPERVISOR":
        return role in {"SUPERVISOR", "MANAGER"}
    if required == "SPECIALIST_OR_SUPERVISOR":
        return role in {"SPECIALIST", "SUPERVISOR"}
    return role == required


def build_plan(case: dict[str, Any], criteria_states: list[dict[str, Any]], documents: list[dict[str, Any]], policy_state: str, user_role: str, now: datetime, config: dict[str, Any]) -> Plan:
    """Create an immutable recommendation from already-authorized, persisted facts.

    `estimated_minutes` are configured handling-time assumptions. External wait is
    never included in the numeric critical-path estimate.
    """
    now_utc = _aware(now)
    deadline_unknown = False
    try:
        remaining = (_aware(case.get("deadline")) - now_utc).total_seconds() / 60
        remaining = round(remaining, 2)
    except (TypeError, ValueError, OverflowError):
        remaining, deadline_unknown = None, True

    catalog = config.get("actions")
    required_actions = {"CONFIRM_EVIDENCE_MATCH", "REVIEW_UNCERTAIN_MATCH", "REQUEST_MISSING_DOCUMENT", "RESOLVE_POLICY_VERIFICATION", "REQUEST_MANAGER_REVIEW", "ESCALATE_FOR_PAYER_EXTENSION"}
    if not isinstance(catalog, dict) or not required_actions.issubset(catalog):
        raise ValueError("invalid action catalog")
    for spec in catalog.values():
        duration = spec.get("estimated_minutes") if isinstance(spec, dict) else None
        if not isinstance(spec, dict) or not isinstance(spec.get("required_role"), str) or (duration is not None and (not isinstance(duration, int) or duration < 0)):
            raise ValueError("invalid action configuration")
    steps_data: list[dict[str, Any]] = []
    gaps: list[str] = []
    assumptions: list[str] = ["Action durations are configured operational estimates, not measured or validated service times."]
    unknown_state = deadline_unknown or policy_state not in {"VERIFIED", "UNVERIFIED", "UNSUPPORTED"}
    unsupported = policy_state == "UNSUPPORTED"
    critical_path_unknown = deadline_unknown or policy_state not in {"VERIFIED", "UNVERIFIED", "UNSUPPORTED"}
    terminal = str(case.get("status", "UNKNOWN")).upper() in {"APPROVED", "DENIED", "CANCELLED", "CLOSED"}

    def add(action: str, *, criterion_id: str | None = None, document_id: int | None = None, detail: str | None = None) -> None:
        nonlocal unknown_state, critical_path_unknown
        if terminal:
            return
        spec = catalog.get(action)
        if not isinstance(spec, dict) or "required_role" not in spec or "estimated_minutes" not in spec:
            gaps.append(f"No valid configured resolution action for {detail or action}.")
            unknown_state = True
            return
        if spec.get("workflow_supported") is False:
            gaps.append(f"No supported workflow action is configured for {detail or action}.")
            unknown_state = True
            critical_path_unknown = True
            return
        steps_data.append({"action_type": action, "spec": spec, "criterion_id": criterion_id, "document_id": document_id})

    status_map = {"HUMAN_CONFIRMED": "CONFIRMED", "MATCH_POSSIBLE": "MATCH", "UNCERTAIN": "UNCERTAIN", "NO_MATCH": "NO_MATCH", "UNSUPPORTED_CRITERION": "UNSUPPORTED"}
    for criterion in sorted(criteria_states, key=lambda c: str(c.get("criterion_id", ""))):
        cid = str(criterion.get("criterion_id") or "UNKNOWN")
        status = str(criterion.get("status", "UNKNOWN")).upper()
        mapped = status_map.get(status)
        if mapped is None:
            gaps.append(f"Criterion {cid} has unmappable status {status}; human review is required.")
            unknown_state = True
            critical_path_unknown = True
        elif mapped == "CONFIRMED":
            continue
        elif mapped == "MATCH":
            gaps.append(f"Criterion {cid} has a possible match requiring human confirmation.")
            add("CONFIRM_EVIDENCE_MATCH", criterion_id=cid, document_id=criterion.get("document_id"), detail=f"criterion {cid}")
        elif mapped == "UNCERTAIN":
            gaps.append(f"Criterion {cid} remains uncertain and requires human review.")
            add("REVIEW_UNCERTAIN_MATCH", criterion_id=cid, document_id=criterion.get("document_id"), detail=f"criterion {cid}")
        elif mapped == "NO_MATCH":
            doc_id = criterion.get("document_id")
            doc = next((d for d in documents if d.get("id") == doc_id), None)
            if doc is None:
                gaps.append(f"Criterion {cid} has no linked document record; missing versus unavailable is unknown.")
                unknown_state = True
                critical_path_unknown = True
            elif str(doc.get("processing_status", "")).upper() in {"FAILED", "ERROR"} or str(doc.get("ocr_status", "")).upper() in {"FAILED", "ERROR"}:
                gaps.append(f"Criterion {cid} document {doc_id} is unavailable or failed; the gap cannot safely be classified as a missing document.")
                unknown_state = True
                critical_path_unknown = True
            else:
                gaps.append(f"Criterion {cid} has no evidence match recorded.")
                add("REQUEST_MISSING_DOCUMENT", criterion_id=cid, document_id=doc_id, detail=f"criterion {cid}")
        else:
            gaps.append(f"Criterion {cid} is unsupported and cannot be resolved by evidence confirmation.")
            unsupported = True
            critical_path_unknown = True

    if policy_state == "UNVERIFIED":
        gaps.append("Applicable policy is unverified; its criteria cannot be treated as authoritative.")
        add("RESOLVE_POLICY_VERIFICATION", detail="policy verification")
    elif policy_state == "UNSUPPORTED":
        gaps.append("Applicable policy is unsupported; no safe policy resolution path is configured.")
        unsupported = True
    elif policy_state not in {"VERIFIED", "UNVERIFIED", "UNSUPPORTED"}:
        gaps.append("Applicable policy state is unknown; policy-dependent requirements cannot be determined.")
        unknown_state = True
        critical_path_unknown = True

    if terminal:
        assumptions.append("Case is terminal; no contradictory workflow action was added.")
    elif case.get("approval_state") == "PENDING_MANAGER_APPROVAL" and not steps_data:
        add("REQUEST_MANAGER_REVIEW", detail="manager review")

    edges: list[tuple[int, int]] = []
    for target, row in enumerate(steps_data):
        dependencies = row["spec"].get("dependencies", [])
        if not isinstance(dependencies, list):
            raise ValueError("action dependencies must be a list")
        for dependency in dependencies:
            if dependency == "prerequisites":
                edges.extend((source, target) for source, candidate in enumerate(steps_data) if source != target and candidate["spec"].get("blocking", True))
            else:
                edges.extend((source, target) for source, candidate in enumerate(steps_data) if source != target and candidate["action_type"] == dependency)
    # Deterministic topological order: eligible shortest duration, then action/id.
    incoming = {i: {a for a, b in edges if b == i} for i in range(len(steps_data))}
    ordered: list[int] = []
    remaining_indices = set(range(len(steps_data)))
    while remaining_indices:
        eligible = [i for i in remaining_indices if not (incoming[i] & remaining_indices)]
        if not eligible:
            raise ValueError("action dependencies contain a cycle")
        eligible.sort(key=lambda i: (steps_data[i]["spec"].get("estimated_minutes") is None, steps_data[i]["spec"].get("estimated_minutes") or 0, steps_data[i]["action_type"], str(steps_data[i]["criterion_id"] or "")))
        chosen = eligible[0]
        ordered.append(chosen)
        remaining_indices.remove(chosen)

    external_unknown = False
    for index in ordered:
        row = steps_data[index]
        spec = row["spec"]
        external_unknown |= bool(spec.get("external_dependency") and not spec.get("duration_known"))
    critical: dict[int, int] = {}
    for i in ordered:
        duration = steps_data[i]["spec"].get("estimated_minutes")
        predecessors = [a for a, b in edges if b == i]
        critical[i] = (max((critical.get(a, 0) for a in predecessors), default=0) + int(duration or 0))
    path = max(critical.values(), default=0)
    blocking_steps = [x for x in steps_data if x["spec"].get("blocking", True)]
    permission_blocked = any(not _actor_can(user_role, x["spec"].get("required_role")) for x in blocking_steps)
    unsupported |= any("unsupported" in gap for gap in gaps)
    if terminal:
        verdict, explanation = "UNCERTAIN", "The case is terminal; this planner does not recommend further workflow actions."
    elif unknown_state or unsupported:
        verdict: Verdict = "UNCERTAIN"
        explanation = "Required policy, deadline, criterion, or action information is unresolved; a reliable feasibility conclusion is unavailable."
    elif permission_blocked:
        verdict, explanation = "BLOCKED_BY_PERMISSION", "At least one required action needs a role the current user does not hold."
    elif external_unknown:
        verdict, explanation = "UNCERTAIN", "A required external response has unknown waiting time; the numeric estimate excludes that wait."
    elif remaining is None:
        verdict, explanation = "UNCERTAIN", "The case deadline is unavailable."
    elif remaining <= 0:
        verdict, explanation = "INFEASIBLE_WITHOUT_EXTENSION", "The deadline has passed; known action handling estimates cannot restore the missed deadline."
    elif not blocking_steps and policy_state == "VERIFIED" and all(status_map.get(str(c.get("status", "")).upper()) == "CONFIRMED" for c in criteria_states) and not terminal:
        verdict, explanation = "READY_FOR_HUMAN_SUBMISSION_REVIEW", "No modeled blocking gaps remain; continue through the existing human submission review workflow."
    elif path > remaining:
        verdict, explanation = "INFEASIBLE_WITHOUT_EXTENSION", "The estimated critical path exceeds the remaining time; an extension may be considered but is not guaranteed."
    elif path > remaining * float(config.get("tightness_fraction", 0.8)):
        verdict, explanation = "TIGHT", "The estimated critical path fits within the deadline but exceeds the configured tightness threshold."
    else:
        verdict, explanation = "ACHIEVABLE", "The configured handling-time estimate fits the deadline under the stated assumptions."
    if verdict == "INFEASIBLE_WITHOUT_EXTENSION":
        add("ESCALATE_FOR_PAYER_EXTENSION", detail="extension")

    # Parallel actions share a level unless dependency edges order them. Longest path is
    # calculated over the DAG; extension recommendation is excluded from case readiness.
    output_steps: list[PlanStep] = []
    for order, i in enumerate(ordered + [j for j in range(len(steps_data)) if j not in ordered], 1):
        row = steps_data[i]
        spec = row["spec"]
        output_steps.append(PlanStep(order=order, action_type=row["action_type"], description=spec["description_template"].format(criterion_id=row["criterion_id"] or "", policy_id=case.get("policy_id", "unknown")), required_role=spec.get("required_role"), estimated_minutes=spec.get("estimated_minutes"), is_estimate=bool(spec.get("duration_known")), blocking=bool(spec.get("blocking", True)), criterion_id=row["criterion_id"], document_id=row["document_id"], can_current_user_perform=_actor_can(user_role, spec.get("required_role")), dependencies=[steps_data[a]["action_type"] for a, b in edges if b == i], parallelizable=bool(spec.get("parallelizable")), external_wait_unknown=bool(spec.get("external_dependency") and not spec.get("duration_known"))))
    if external_unknown:
        assumptions.append("External waiting time is unknown and excluded from critical_path_minutes.")
    if unsupported:
        assumptions.append("Unsupported policy or criterion remains unresolved; no substitute requirement was inferred.")
    if remaining is not None and remaining < 0:
        assumptions.append("Deadline is overdue; remaining time is negative.")
    return Plan(case_id=str(case.get("id", "")), generated_at=now_utc, time_remaining_minutes=remaining, verdict=verdict, verdict_explanation=explanation, critical_path_minutes=path if not critical_path_unknown and not any(x["spec"].get("estimated_minutes") is None for x in steps_data if x["spec"].get("blocking", True)) else None, steps=output_steps, unresolved_gaps=sorted(set(gaps)), assumptions=assumptions, duration_is_estimate=True, external_wait_unknown=external_unknown, deadline_unknown=deadline_unknown)
