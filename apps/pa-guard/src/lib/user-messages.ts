const technicalDetail = (detail: string) => {
  const text = detail.trim();
  return !text || text.length > 360 || /^(?:\{|\[)/.test(text) ||
    /traceback|stack trace|sqlite|sqlalchemy|exception:|\b(?:errno|httpx|uvicorn)\b| at [\w.$]+\(/i.test(text);
};

/** Turn API failures into clear user-facing messages without changing the API response. */
export function formatApiErrorMessage(status: number, detail?: unknown, action = 'complete this action'): string {
  const normalizedDetail = typeof detail === 'string' ? detail.trim() : '';
  const safeDetail = normalizedDetail && !technicalDetail(normalizedDetail) ? normalizedDetail : undefined;
  if (status === 401) return 'Your session has expired. Sign in again to continue.';
  if (status === 403) return 'You do not have permission to do this. Ask a Manager if you think you need access.';
  if (status === 404) return safeDetail || 'The requested record was not found. Refresh the page and try again.';
  if (status === 409) return safeDetail || 'This record changed or is not in a state that allows this action. Refresh it and try again.';
  if (status === 422) return safeDetail || 'Some information is missing or invalid. Check the fields and try again.';
  if (status >= 500) return 'PA Guard could not complete this action because a service is unavailable. Try again shortly.';
  if (safeDetail) return safeDetail;
  return `PA Guard could not ${action}. Check the information and try again.`;
}

const STATUS_LABELS: Record<string, string> = {
  AWAITING_MANAGER: 'Awaiting Manager review',
  AWAITING_SPECIALIST: 'Awaiting Specialist review',
  AWAITING_SUPERVISOR: 'Awaiting Supervisor review',
  EVIDENCE_REVIEW: 'Evidence review',
  IN_REVIEW: 'In review',
  INTAKE_REVIEW: 'Intake review',
  MORE_INFORMATION_REQUIRED: 'More information required',
  RETURNED_TO_SPECIALIST: 'Returned to Specialist',
  RETURNED_TO_SUPERVISOR: 'Returned to Supervisor',
  UNDER_REVIEW: 'Under review',
  OCR_COMPLETED: 'Text extracted',
  OCR_FAILED: 'Text extraction failed',
  NEEDS_REVIEW: 'Needs review',
};

export function formatStatusLabel(value: string): string {
  return STATUS_LABELS[value] ?? value.replaceAll('_', ' ').toLowerCase().replace(/^\w/, (letter) => letter.toUpperCase());
}

export function formatDiagnosticForUser(value: unknown, fallback: string): string {
  if (typeof value !== 'string') return fallback;
  const text = value.trim();
  return text && !technicalDetail(text) ? text : fallback;
}
