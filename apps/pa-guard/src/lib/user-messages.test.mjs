import assert from 'node:assert/strict';
import { formatApiErrorMessage, formatDiagnosticForUser, formatStatusLabel } from './user-messages.ts';

assert.match(formatApiErrorMessage(401), /session has expired/i);
assert.match(formatApiErrorMessage(403, 'Your role cannot access this API operation'), /do not have permission/i);
assert.equal(formatApiErrorMessage(404, 'Case PA-1048 not found'), 'Case PA-1048 not found');
assert.equal(formatApiErrorMessage(409, 'Case changed since it was opened; refresh before saving'), 'Case changed since it was opened; refresh before saving');
assert.match(formatApiErrorMessage(422, '{"detail":"invalid"}'), /missing or invalid/i);
assert.match(formatApiErrorMessage(422, [{ msg: 'technical validation payload' }]), /missing or invalid/i);
assert.match(formatApiErrorMessage(500, 'sqlite3.OperationalError: internal detail'), /service is unavailable/i);
assert.match(formatApiErrorMessage(503), /try again shortly/i);
assert.equal(formatStatusLabel('AWAITING_SUPERVISOR'), 'Awaiting Supervisor review');
assert.equal(formatStatusLabel('OCR_FAILED'), 'Text extraction failed');
assert.equal(formatDiagnosticForUser('The file could not be read.', 'Try again.'), 'The file could not be read.');
assert.equal(formatDiagnosticForUser('Traceback (most recent call last): sqlite3.Error', 'The review step could not finish.'), 'The review step could not finish.');

console.log('User-facing API error message checks passed.');
