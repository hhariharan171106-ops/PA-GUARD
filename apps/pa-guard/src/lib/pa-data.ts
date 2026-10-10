export type CaseStatus = 'NEW' | 'INTAKE REVIEW' | 'EVIDENCE REVIEW' | 'READY TO SUBMIT' | 'SUBMITTED' | 'PAYER PENDING' | 'APPROVED' | 'DENIED' | 'MORE INFORMATION REQUIRED';
export type Risk = 'ON TRACK' | 'AT RISK' | 'BREACHED' | 'COMPLETE';
export type Priority = 'HIGH' | 'MEDIUM' | 'LOW';
export type PaCase = { id: string; patientReference: string; payer: string; service: string; submissionDate: string; deadline: string; status: CaseStatus; risk: Risk; coordinator: string; priority: Priority };
export type Document = { id: number; caseId: string; name: string; status: 'RECEIVED' | 'MISSING' | 'NEEDS REVIEW' | 'VERIFIED' };
export type AuditEvent = { id: number; caseId: string; timestamp: string; actor: string; action: string; detail: string; decisionType: string };

export const initialCases: PaCase[] = [
  { id: 'PA-1042', patientReference: 'PT-FX-2048', payer: 'Northstar Health', service: 'Lumbar MRI', submissionDate: 'Oct 8', deadline: 'Today, 2:30 PM', status: 'PAYER PENDING', risk: 'AT RISK', coordinator: 'Maya Chen', priority: 'HIGH' },
  { id: 'PA-1041', patientReference: 'PT-FX-1943', payer: 'Harbor Mutual', service: 'Specialty infusion', submissionDate: 'Oct 7', deadline: 'Overdue 2h', status: 'EVIDENCE REVIEW', risk: 'BREACHED', coordinator: 'Jordan Lee', priority: 'HIGH' },
  { id: 'PA-1040', patientReference: 'PT-FX-1881', payer: 'SummitCare', service: 'Knee arthroscopy', submissionDate: 'Oct 9', deadline: 'Tomorrow, 4:00 PM', status: 'INTAKE REVIEW', risk: 'ON TRACK', coordinator: 'Avery Patel', priority: 'MEDIUM' },
  { id: 'PA-1039', patientReference: 'PT-FX-1776', payer: 'Northstar Health', service: 'Cardiac CT', submissionDate: 'Oct 6', deadline: 'Oct 12, 10:00 AM', status: 'MORE INFORMATION REQUIRED', risk: 'AT RISK', coordinator: 'Maya Chen', priority: 'HIGH' },
  { id: 'PA-1038', patientReference: 'PT-FX-1630', payer: 'Harbor Mutual', service: 'Physical therapy', submissionDate: 'Oct 3', deadline: 'Completed Oct 9', status: 'APPROVED', risk: 'COMPLETE', coordinator: 'Jordan Lee', priority: 'LOW' },
];
export const initialDocuments: Document[] = [
  { id: 1, caseId: 'PA-1042', name: 'Clinical notes', status: 'RECEIVED' },
  { id: 2, caseId: 'PA-1042', name: 'Conservative treatment history', status: 'NEEDS REVIEW' },
  { id: 3, caseId: 'PA-1041', name: 'Infusion order', status: 'RECEIVED' },
  { id: 4, caseId: 'PA-1041', name: 'Recent lab results', status: 'MISSING' },
  { id: 5, caseId: 'PA-1039', name: 'Specialist letter', status: 'MISSING' },
];
