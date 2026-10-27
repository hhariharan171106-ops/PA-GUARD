import assert from 'node:assert/strict';
import { formatAuditDetails } from './audit-format.ts';

assert.equal(formatAuditDetails('{"case_id":"PA-1048","old_status":"NEW","new_status":"IN_REVIEW"}'), 'Case PA-1048: Status changed from NEW to IN REVIEW.');
assert.equal(formatAuditDetails('{"assigned_to":"Staff 04","reason":"High SLA risk"}'), 'Assigned to Staff 04. Reason: High SLA risk.');

assert.equal(formatAuditDetails({ request_id: 'r-1', context: { assigned_to: 'Maya' }, checks: [true, 3] }), 'Request Id: r-1. Context: Assigned to Maya. Checks: Yes, 3.');

assert.equal(formatAuditDetails('Manager approved after review.'), 'Manager approved after review.');
assert.equal(formatAuditDetails('{not valid json'), '{not valid json');

assert.equal(formatAuditDetails(null), 'No details provided.');
assert.equal(formatAuditDetails(''), 'No details provided.');
assert.equal(formatAuditDetails('   '), 'No details provided.');
assert.equal(formatAuditDetails('null'), 'No details provided.');
assert.equal(formatAuditDetails('false'), 'No');
assert.equal(formatAuditDetails('42'), '42');

console.log('Audit formatter checks passed: common summaries, nested values, plain text, malformed JSON, empty values, booleans, and numbers.');
