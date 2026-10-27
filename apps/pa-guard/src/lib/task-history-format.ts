import { formatAuditDetails } from './audit-format.ts';

type UnknownRecord = Record<string, unknown>;

export type TaskHistoryMessage = {
  speaker?: string;
  message: string;
  timestamp?: string;
  additionalDetails?: string;
};

export type TaskHistoryDetail =
  | { kind: 'conversation'; messages: TaskHistoryMessage[]; additionalDetails?: string }
  | { kind: 'details'; text: string };

const CONVERSATION_KEYS = ['messages', 'conversation', 'conversation_history', 'chat_history', 'transcript'];
const BODY_KEYS = ['message', 'content', 'text', 'body', 'output', 'response'];
const SPEAKER_KEYS = ['speaker', 'role', 'type', 'actor', 'name'];
const TIME_KEYS = ['timestamp', 'created_at', 'sent_at', 'time', 'date'];
const MESSAGE_FIELDS = new Set([...BODY_KEYS, ...SPEAKER_KEYS, ...TIME_KEYS, 'data', 'kwargs', 'id', 'message_id']);

function isRecord(value: unknown): value is UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function nonEmptyText(value: unknown): string | undefined {
  if (typeof value === 'string') return value.trim() ? value : undefined;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return undefined;
}

function humanizeSpeaker(value: string): string {
  return value.replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function field(record: UnknownRecord, keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
}

function formatMessage(input: unknown): TaskHistoryMessage | undefined {
  if (!isRecord(input)) return undefined;
  const data = isRecord(input.data) ? input.data : undefined;
  const kwargs = isRecord(input.kwargs) ? input.kwargs : undefined;
  const records = [input, data, kwargs].filter((record): record is UnknownRecord => Boolean(record));
  const body = records.map((record) => field(record, BODY_KEYS)).find((value) => value !== undefined);
  if (body === undefined) return undefined;

  const speakerValue = records.map((record) => field(record, SPEAKER_KEYS)).find((value) => nonEmptyText(value) !== undefined);
  const timestampValue = records.map((record) => field(record, TIME_KEYS)).find((value) => nonEmptyText(value) !== undefined);
  const knownKeys = new Set([...MESSAGE_FIELDS]);
  const extraEntries: UnknownRecord = {};
  for (const [label, record] of [['Message details', input], ['Additional message data', data], ['Additional message fields', kwargs]] as const) {
    if (!record) continue;
    const entries = Object.fromEntries(Object.entries(record).filter(([key]) => !knownKeys.has(key)));
    if (Object.keys(entries).length) extraEntries[label] = entries;
  }
  const message: TaskHistoryMessage = {
    ...(nonEmptyText(speakerValue) ? { speaker: humanizeSpeaker(nonEmptyText(speakerValue)!) } : {}),
    message: body === '' ? 'No message text was recorded.' : formatAuditDetails(body),
    ...(nonEmptyText(timestampValue) ? { timestamp: nonEmptyText(timestampValue) } : {}),
    ...(Object.keys(extraEntries).length ? { additionalDetails: formatAuditDetails(extraEntries) } : {}),
  };
  return message;
}

function asConversation(value: unknown): TaskHistoryMessage[] | undefined {
  if (!Array.isArray(value) || value.length === 0) return undefined;
  const parsed = value.map(formatMessage);
  if (!parsed.some(Boolean)) return undefined;
  return parsed.map((message, index) => message ?? {
    speaker: `Message ${index + 1}`,
    message: formatAuditDetails(value[index]),
  });
}

function conversationContainer(value: unknown): { messages: TaskHistoryMessage[]; additionalDetails?: string } | undefined {
  if (Array.isArray(value)) {
    const messages = asConversation(value);
    return messages ? { messages } : undefined;
  }
  if (!isRecord(value)) return undefined;

  for (const key of CONVERSATION_KEYS) {
    const messages = asConversation(value[key]);
    if (messages) {
      const extras = Object.fromEntries(Object.entries(value).filter(([entryKey]) => !CONVERSATION_KEYS.includes(entryKey)));
      return { messages, ...(Object.keys(extras).length ? { additionalDetails: formatAuditDetails(extras) } : {}) };
    }
  }
  return undefined;
}

/** Formats mixed audit payloads for the task and decision history view only. */
export function formatTaskHistoryDetail(input: unknown): TaskHistoryDetail {
  let value = input;
  if (typeof input === 'string') {
    const source = input.trim();
    if (source) {
      try {
        value = JSON.parse(source) as unknown;
      } catch {
        return { kind: 'details', text: formatAuditDetails(input) };
      }
    }
  }

  const conversation = conversationContainer(value);
  return conversation
    ? { kind: 'conversation', ...conversation }
    : { kind: 'details', text: formatAuditDetails(value) };
}

export function historySourceLabel(decisionType: string, action: string): string {
  const type = decisionType.toUpperCase();
  const event = action.toUpperCase();
  if (event === 'CASE_AI_WORKFLOW_FINISHED') return 'Workflow completed';
  if (type.startsWith('AGENT_')) return 'AI recommendation';
  if (type === 'HUMAN_DECISION') return 'Human decision';
  if (type === 'HUMAN_OPERATIONAL_DECISION') return 'Human operational decision';
  if (type === 'HUMAN_WORKFLOW') return 'Human workflow action';
  if (type.startsWith('HUMAN')) return 'Human action';
  if (type.startsWith('SYSTEM')) return 'System event';
  return 'Recorded event';
}
