import assert from 'node:assert/strict';
import { canVerifyEvidenceDocument } from './evidence-permissions.ts';

assert.equal(canVerifyEvidenceDocument('SPECIALIST'), true);
assert.equal(canVerifyEvidenceDocument('SUPERVISOR'), true);
assert.equal(canVerifyEvidenceDocument('MANAGER'), false);
assert.equal(canVerifyEvidenceDocument('ADMIN'), false);
assert.equal(canVerifyEvidenceDocument(undefined), false);
console.log('Evidence verification permission checks passed for Specialist, Supervisor, Manager, Admin, and anonymous roles.');
