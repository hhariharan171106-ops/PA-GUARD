import assert from 'node:assert/strict';
import { formatTaskHistoryDetail, historySourceLabel } from './task-history-format.ts';

const conversation = formatTaskHistoryDetail(JSON.stringify({
  case_id: 'PA-2041',
  messages: [
    { role: 'user', content: 'What is the next step?', timestamp: '2026-04-01T10:00:00Z' },
    { type: 'assistant', data: { content: 'A Manager should review the case.', timestamp: '2026-04-01T10:00:03Z' } },
  ],
}));
assert.equal(conversation.kind, 'conversation');
assert.deepEqual(conversation.messages.map(({ speaker, message, timestamp }) => ({ speaker, message, timestamp })), [
  { speaker: 'User', message: 'What is the next step?', timestamp: '2026-04-01T10:00:00Z' },
  { speaker: 'Assistant', message: 'A Manager should review the case.', timestamp: '2026-04-01T10:00:03Z' },
]);
assert.match(conversation.additionalDetails, /Case: PA-2041/);

const decision = formatTaskHistoryDetail({
  risk_level: 'HIGH',
  next_action: 'Route to Manager',
  reason: 'The review deadline is approaching.',
  source_references: [{ policy_id: 'SLA-001', version: '1.0' }],
});
assert.equal(decision.kind, 'details');
assert.match(decision.text, /Risk Level: HIGH/);
assert.match(decision.text, /Next Action: Route to Manager/);
assert.match(decision.text, /Reason: The review deadline is approaching/);
assert.match(decision.text, /Policy Id: SLA-001/);
assert.doesNotMatch(decision.text, /[{}\[\]"]/);

const nested = formatTaskHistoryDetail({
  checks: [{ name: 'Deadline check', result: { status: 'passed', hours: 168 } }, { name: 'Evidence', result: ['received', 'unverified'] }],
});
assert.match(nested.text, /Name: Deadline check/);
assert.match(nested.text, /Status: passed/);
assert.match(nested.text, /168/);
assert.match(nested.text, /received, unverified/);
assert.doesNotMatch(nested.text, /\[object Object\]|undefined/);

assert.deepEqual(formatTaskHistoryDetail('Manager recorded a review note.'), {
  kind: 'details', text: 'Manager recorded a review note.',
});
assert.deepEqual(formatTaskHistoryDetail('{not valid JSON'), {
  kind: 'details', text: '{not valid JSON',
});
assert.deepEqual(formatTaskHistoryDetail(null), { kind: 'details', text: 'No details provided.' });
assert.deepEqual(formatTaskHistoryDetail({ risk_level: null, next_action: undefined }), {
  kind: 'details', text: 'Risk Level: None.',
});
assert.deepEqual(formatTaskHistoryDetail({ messages: [{ role: 'assistant', content: null }] }), {
  kind: 'details', text: 'Messages: Role: assistant. Content: None.',
});

assert.equal(historySourceLabel('AGENT_RECOMMENDATION_REQUIRES_HUMAN_REVIEW', 'SLA_RAG_RECOMMENDATION'), 'AI recommendation');
assert.equal(historySourceLabel('HUMAN_DECISION', 'APPROVE'), 'Human decision');
assert.equal(historySourceLabel('SYSTEM_AI_EXPLANATION_TELEMETRY', 'CASE_AI_EXPLANATION_GENERATED'), 'System event');

console.log('Task history formatter checks passed: conversations, decisions, nested values, plain text, malformed JSON, missing fields, nulls, and source labels.');
