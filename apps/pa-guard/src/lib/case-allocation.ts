export type AllocationRole = 'SPECIALIST' | 'SUPERVISOR' | 'MANAGER';
export type AllocationCaseSummary = { id: string; status: string; service: string };
export type AllocationStaffSummary = {
  id: number; role: string; active: number; available: number; max_workload: number;
  active_cases: number; service_types: string[];
};

export function requiredAllocationRole(status: string): AllocationRole {
  if (['NEW', 'SPECIALIST_REVIEW', 'RETURNED_TO_SPECIALIST', 'INTAKE REVIEW', 'EVIDENCE REVIEW', 'MORE INFORMATION REQUIRED'].includes(status)) return 'SPECIALIST';
  if (status === 'AWAITING_SUPERVISOR') return 'SUPERVISOR';
  return 'MANAGER';
}

export function allocationIneligibility(staff: AllocationStaffSummary, selectedCase: AllocationCaseSummary): string | null {
  const role = requiredAllocationRole(selectedCase.status);
  if (staff.role !== role) return `This case requires a ${role.toLowerCase()}.`;
  if (!staff.active) return 'Staff account is inactive.';
  if (!staff.available) return 'Staff member is unavailable.';
  if (staff.service_types.length && !staff.service_types.includes(selectedCase.service)) return 'Case service is outside this staff member’s scope.';
  if (staff.active_cases >= staff.max_workload) return 'Staff member is at workload capacity.';
  return null;
}

export function assignmentRequest(caseId: string, staffId: number, reason: string) {
  return {
    path: `/work-allocation/${encodeURIComponent(caseId)}/assign`,
    init: { method: 'POST', body: JSON.stringify({ staff_id: staffId, reason: reason.trim() }) },
  };
}

export async function submitCaseAssignment<T>(
  caseId: string,
  staffId: number,
  reason: string,
  send: (path: string, init: RequestInit) => Promise<T>,
): Promise<T> {
  const request = assignmentRequest(caseId, staffId, reason);
  return send(request.path, request.init);
}
