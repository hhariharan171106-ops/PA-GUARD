import assert from 'node:assert/strict';
import { allocationIneligibility, assignmentRequest, requiredAllocationRole, submitCaseAssignment } from './case-allocation.ts';

const specialist = { id: 12, role: 'SPECIALIST', active: 1, available: 1, max_workload: 8, active_cases: 3, service_types: ['Lumbar MRI'] };
const supervisor = { ...specialist, id: 25, role: 'SUPERVISOR', service_types: [] };
const caseForSpecialist = { id: 'PA/1048', status: 'NEW', service: 'Lumbar MRI' };
const caseForSupervisor = { id: 'PA-1050', status: 'AWAITING_SUPERVISOR', service: 'Lumbar MRI' };

assert.equal(requiredAllocationRole(caseForSpecialist.status), 'SPECIALIST');
assert.equal(requiredAllocationRole(caseForSupervisor.status), 'SUPERVISOR');
assert.equal(allocationIneligibility(specialist, caseForSpecialist), null);
assert.equal(allocationIneligibility(supervisor, caseForSupervisor), null);
assert.match(allocationIneligibility(supervisor, caseForSpecialist), /requires a specialist/);
assert.match(allocationIneligibility({ ...specialist, available: 0 }, caseForSpecialist), /unavailable/);
assert.match(allocationIneligibility({ ...specialist, active: 0 }, caseForSpecialist), /inactive/);
assert.match(allocationIneligibility({ ...specialist, active_cases: 8 }, caseForSpecialist), /capacity/);
assert.match(allocationIneligibility({ ...specialist, service_types: ['Other'] }, caseForSpecialist), /outside this staff member’s scope/);

const request = assignmentRequest(caseForSpecialist.id, specialist.id, '  Allocate to specialist  ');
assert.equal(request.path, '/work-allocation/PA%2F1048/assign');
assert.deepEqual(JSON.parse(request.init.body), { staff_id: 12, reason: 'Allocate to specialist' });

let sent;
assert.deepEqual(await submitCaseAssignment(caseForSpecialist.id, specialist.id, 'reason', async (path, init) => {
  sent = { path, init };
  return { case_id: 'PA/1048', staff_id: 12, role: 'SPECIALIST' };
}), { case_id: 'PA/1048', staff_id: 12, role: 'SPECIALIST' });
assert.equal(sent.path, request.path);
await assert.rejects(() => submitCaseAssignment('PA-1048', 12, 'reason', async () => { throw new Error('API rejected assignment'); }), /API rejected assignment/);
console.log('Case allocation checks passed: case role, staff eligibility, selected case/staff IDs, success response, and API failure propagation.');
