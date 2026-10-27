const LABEL_OVERRIDES: Record<string, string> = {
  case_id: 'Case', assigned_to: 'Assigned to', old_status: 'Previous status',
  new_status: 'New status', previous_state: 'Previous status', resulting_state: 'New status',
};

function readableLabel(key: string): string {
  return LABEL_OVERRIDES[key] ?? key
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function readableValue(value: unknown): string {
  if (value === null) return 'None';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'string') return value.replaceAll('_', ' ');
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(readableValue).join(', ') || 'None';
  if (typeof value === 'object') return formatObject(value as Record<string, unknown>).replace(/\.$/, '');
  return String(value);
}

function appendRemaining(summary: string, entries: [string, unknown][]): string {
  if (!entries.length) return summary;
  return `${summary} ${entries.map(([key, value]) => `${readableLabel(key)}: ${readableValue(value)}.`).join(' ')}`;
}

function formatObject(value: Record<string, unknown>): string {
  const entries = Object.entries(value).filter(([, item]) => item !== undefined);
  if (!entries.length) return 'No additional details.';

  const caseId = value.case_id;
  const oldStatus = value.old_status ?? value.previous_state;
  const newStatus = value.new_status ?? value.resulting_state;
  if (caseId != null && oldStatus != null && newStatus != null) {
    const summary = `Case ${readableValue(caseId)}: Status changed from ${readableValue(oldStatus)} to ${readableValue(newStatus)}.`;
    return appendRemaining(summary, entries.filter(([key]) => !['case_id', 'old_status', 'previous_state', 'new_status', 'resulting_state'].includes(key)));
  }

  if (value.assigned_to != null) {
    const summary = `Assigned to ${readableValue(value.assigned_to)}.${value.reason != null ? ` Reason: ${readableValue(value.reason)}.` : ''}`;
    return appendRemaining(summary, entries.filter(([key]) => !['assigned_to', 'reason'].includes(key)));
  }

  return entries.map(([key, item]) => `${readableLabel(key)}: ${readableValue(item)}.`).join(' ');
}

/** Converts mixed audit detail values to readable text without changing stored data. */
export function formatAuditDetails(input: unknown): string {
  if (input == null || input === '') return 'No details provided.';
  if (typeof input === 'number' || typeof input === 'boolean') return readableValue(input);
  if (typeof input === 'object') return Array.isArray(input)
    ? input.map((item, index) => `Item ${index + 1}: ${readableValue(item)}.`).join(' ')
    : formatObject(input as Record<string, unknown>);
  if (typeof input !== 'string') return String(input);

  const source = input.trim();
  if (!source) return 'No details provided.';
  try {
    const parsed: unknown = JSON.parse(source);
    if (typeof parsed === 'string') return parsed || 'No details provided.';
    if (parsed === null) return 'No details provided.';
    if (typeof parsed === 'object') return Array.isArray(parsed)
      ? parsed.map((item, index) => `Item ${index + 1}: ${readableValue(item)}.`).join(' ')
      : formatObject(parsed as Record<string, unknown>);
    return readableValue(parsed);
  } catch {
    return input;
  }
}
