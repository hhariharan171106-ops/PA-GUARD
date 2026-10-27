import type { AuditEvent, CaseStatus, Document, PaCase } from '@/lib/pa-data';
import { getAuthToken, notifyAuthExpired } from '@/lib/auth-session';
import { formatApiErrorMessage } from '@/lib/user-messages';

const configuredApiUrl = import.meta.env.VITE_API_BASE_URL as string | undefined;
const isLocalPreview = typeof window !== 'undefined' && ['localhost', '127.0.0.1'].includes(window.location.hostname);
const isNgrokPreview = typeof window !== 'undefined' && [
  'ngrok-free.dev',
  'ngrok-free.app',
  'ngrok.app',
  'ngrok.io',
].some((domain) => window.location.hostname === domain || window.location.hostname.endsWith(`.${domain}`));
export const isPaApiConfigured = Boolean(configuredApiUrl) || isLocalPreview || isNgrokPreview;
// Use the Vite /api proxy for ngrok origins; local development keeps its direct API URL.
export const API_BASE_URL = configuredApiUrl ?? (isLocalPreview ? 'http://localhost:8000' : '');

export class ApiUnavailableError extends Error {
  constructor() {
    super('PA Guard could not be reached. Check the connection and try again.');
    this.name = 'ApiUnavailableError';
  }
}

export class ApiResponseError extends Error {
  readonly status: number;

  constructor(status: number, message?: unknown) {
    super(formatApiErrorMessage(status, message));
    this.name = 'ApiResponseError';
    this.status = status;
  }
}

type ApiCase = { id: string; patient_reference: string; payer: string; service: string; submission_date: string; deadline: string; status: CaseStatus; risk: PaCase['risk']; coordinator: string; priority: PaCase['priority'] };
type ApiDocument = { id: number; case_id: string; name: string; status: Document['status']; category?: string; original_filename?: string; submission_id?: string | null; uploaded_at?: string; processing_status?: string; ocr_status?: string; extracted_text?: string; evidence_summary?: string; processing_error?: string };
type ApiBatchUpload = { case_id: string; documents: ApiDocument[]; saved_submission_ids: string[]; failures: { filename: string; submission_id: string; status_code: number; message: string }[]; partial_success: boolean; all_succeeded: boolean };
type ApiAudit = { id: number; case_id: string; timestamp: string; actor: string; action: string; detail: string; decision_type: string };
type CaseCreatePayload = Pick<PaCase, 'patientReference' | 'payer' | 'service' | 'deadline' | 'coordinator'>;
export type PolicyReference = { policyId: string; documentType: string; effectiveDate: string; version: string; similarity: number };
export type SlaRecommendation = { caseId: string; rank: number; riskLevel: 'BREACHED' | 'HIGH' | 'MEDIUM' | 'LOW' | 'NEEDS_REVIEW'; remainingTime: string; remainingHours: number; priority: 'HIGH' | 'MEDIUM' | 'LOW' | 'NEEDS_REVIEW'; explanation: string; explanationEngine?: string; recommendedAction: string; assignedRole: string; sourceReferences: PolicyReference[]; confidence: number; needsHumanReview: boolean };
export type SlaRun = { runAt: string; mode: 'recommendation_only'; engine: string; recommendations: SlaRecommendation[] };
export type AgentSystemStatus = {
  api_status: 'ready' | 'degraded';
  database_status: 'ready' | 'unavailable';
  agents: { sla: string; evidence: string; escalation: string; recovery: string };
  ollama: { status: 'ready' | 'model_missing' | 'unavailable'; model: string; model_available: boolean; message: string };
};
export type OperationalRisk = { category: 'RESOLVED' | 'BREACHED' | 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'; breached: boolean; deadline: string; remainingHours: number; reasons: string[]; recommendedAction: string; escalationRequired: boolean; ruleVersion: string };
type ApiOperationalRisk = { risk_category: OperationalRisk['category']; breached: boolean; deadline: string; remaining_hours: number; reasons: string[]; recommended_action: string; escalation_required: boolean; configuration: { rule_version: string }; };
export type SlaRiskPrediction = { caseId: string; available: boolean; predictedSlaBreach?: boolean; breachProbability?: number; modelVersion?: string; predictionTimestamp?: string; evaluationProvenance?: string; decisionScope?: string; riskLevel: 'LOW' | 'MEDIUM' | 'HIGH' | 'NEEDS_REVIEW'; riskScore: number | null; scoreMeaning: string; factors: { factor: string; description: string; importance: number }[]; reliable: boolean; reliabilityNote: string; trainingDataSource: string; operationalRisk?: OperationalRisk };
type ApiSlaRiskPrediction = { case_id: string; available: boolean; predicted_sla_breach?: boolean; breach_probability?: number; model_version?: string; prediction_timestamp?: string; evaluation_provenance?: string; decision_scope?: string; risk_level: SlaRiskPrediction['riskLevel']; risk_score: number | null; score_meaning: string; factors: SlaRiskPrediction['factors']; reliable: boolean; reliability_note: string; training_data_source: string; operational_risk?: ApiOperationalRisk };
type ApiWorkflowStage<T = Record<string, unknown>> = { status: 'completed' | 'failed' | 'skipped'; result?: T; error?: string; reason?: string; retryable?: boolean };
type ApiWorkflowDocument = { document_id: number; filename: string; category: string; verification_status: string; reviewer?: string | null; ocr_status: string; processing_status: string; extracted_text_available: boolean; page_text_available: boolean; processing_error: string; file_size: number; uploaded_at: string | null };
type ApiWorkflowEvidenceItem = { requirement: string; status: string; supporting_documents: { filename: string; category: string; page_number: number | null; excerpt: string }[]; explanation: string; human_review_required: boolean };
type ApiWorkflowSummary = { case_id: string; case_status: string; sla_status: string; deadline: string; documents: ApiWorkflowDocument[]; unverified_documents: ApiWorkflowDocument[]; unverified_document_count: number; evidence_reference_count: number; responsible_staff: { name: string; role: string; source: string } | null; active_escalation: { status: string; assignee: { name: string | null; role: string; status: string } | null } | null; ml_priority_human_review_recommended: boolean; evidence_items: ApiWorkflowEvidenceItem[]; unresolved_requirements: ApiWorkflowEvidenceItem[]; human_review_items: string[]; escalation: ApiEscalationDecision | null; escalation_reason: string; recommended_next_action: string };
type ApiCaseWorkflow = {
  case_id: string;
  workflow_status: 'completed' | 'partial';
  stages: {
    evidence_agent: ApiWorkflowStage<ApiEvidenceFinding>;
    evidence_prediction: ApiWorkflowStage<ApiEvidencePrediction>;
    sla_agent: ApiWorkflowStage;
    sla_risk_prediction: ApiWorkflowStage<ApiSlaRiskPrediction>;
    escalation_agent: ApiWorkflowStage<ApiEscalationDecision>;
    recovery_agent: ApiWorkflowStage<ApiRecoveryDecision>;
    llm_explanation: ApiWorkflowStage<{ explanation: string; engine: string }>;
    summary: ApiWorkflowStage<ApiWorkflowSummary>;
  };
  llm_explanation: { explanation: string; engine: string } | null;
  summary: ApiWorkflowSummary;
};
export type CaseExplanationStatus = { status: 'READY' | 'GENERATING' | 'UNAVAILABLE' | 'STALE' | 'NOT_REQUESTED'; explanation?: string; engine?: string; detail?: string; request_id?: string; cache_hit?: boolean };
export type WorkflowStage<T = Record<string, unknown>> = { status: 'completed' | 'failed' | 'skipped'; result?: T; error?: string; reason?: string; retryable?: boolean };
export type CaseWorkflowDocument = { documentId: number; filename: string; category: string; verificationStatus: string; reviewer?: string | null; ocrStatus: string; processingStatus: string; extractedTextAvailable: boolean; pageTextAvailable: boolean; processingError: string; fileSize: number; uploadedAt: string | null };
export type CaseWorkflowEvidenceItem = { requirement: string; status: string; supportingDocuments: { filename: string; category: string; pageNumber: number | null; excerpt: string }[]; explanation: string; humanReviewRequired: boolean };
export type CaseWorkflowSummary = { caseId: string; caseStatus: string; slaStatus: string; deadline: string; documents: CaseWorkflowDocument[]; unverifiedDocuments: CaseWorkflowDocument[]; unverifiedDocumentCount: number; evidenceReferenceCount: number; responsibleStaff: { name: string; role: string; source: string } | null; activeEscalation: { status: string; assignee: { name: string | null; role: string; status: string } | null } | null; mlPriorityHumanReviewRecommended: boolean; evidenceItems: CaseWorkflowEvidenceItem[]; unresolvedRequirements: CaseWorkflowEvidenceItem[]; humanReviewItems: string[]; escalation: EscalationDecision | null; escalationReason: string; recommendedNextAction: string };
export type CaseWorkflowResult = {
  caseId: string;
  workflowStatus: 'completed' | 'partial';
  stages: {
    evidenceAgent: WorkflowStage<EvidenceFinding>;
    evidencePrediction: WorkflowStage<EvidencePrediction>;
    slaAgent: WorkflowStage;
    slaRiskPrediction: WorkflowStage<SlaRiskPrediction>;
    escalationAgent: WorkflowStage<EscalationDecision>;
    recoveryAgent: WorkflowStage<RecoveryDecision>;
    llmExplanation: WorkflowStage<{ explanation: string; engine: string }>;
    summary: WorkflowStage<CaseWorkflowSummary>;
  };
  llmExplanation: { explanation: string; engine: string } | null;
  summary: CaseWorkflowSummary;
};
type ApiPolicyReference = { policy_id: string; document_type: string; effective_date: string; version: string; similarity: number };
type ApiSlaRecommendation = { case_id: string; rank?: number; risk_level: SlaRecommendation['riskLevel']; remaining_time: string; remaining_hours: number; priority: SlaRecommendation['priority']; explanation: string; explanation_engine?: string; recommended_action: string; assigned_role: string; source_references: ApiPolicyReference[]; confidence: number; needs_human_review: boolean };
export type PolicyDocument = { policyId: string; documentType: string; title: string; content: string; effectiveDate: string; expiresAt: string | null; version: string; approved: boolean; embeddingEngine: string | null };
export type PolicyPayload = Omit<PolicyDocument, 'approved' | 'embeddingEngine'>;
type ApiPolicyDocument = { policy_id: string; document_type: string; title: string; content: string; effective_date: string; expires_at: string | null; version: string; approved: number; embedding_engine: string | null };
export type EvidenceSourceReference = { policyId: string; documentType: string; effectiveDate: string; version: string; payer: string | null; serviceType: string | null; sourceReference: string | null; verificationStatus?: 'MOCK_DEMO' | 'VERIFIED_PUBLIC'; policyUse?: 'DEMO_ONLY' | 'AUTHORITATIVE'; sourceUrl?: string | null; sourceVerifiedBy?: string | null; sourceVerifiedAt?: string | null; similarity: number };
export type EvidenceDocumentMatch = { requiredDocument: string; documentName: string; category: string; ocrStatus: string; pageNumber: number; excerpt: string };
export type UnreadableEvidenceDocument = { documentName: string; category: string; ocrStatus: string; reason: string };
export type EvidenceRequirementValidation = { requirement: string; policyRequiredCategories: string[]; validationStatus: 'SATISFIED' | 'MISSING' | 'UNCERTAIN' | 'CONFLICTING'; categoryMatched: boolean; textMatched: boolean; clinicallyVerified: boolean; reason: string; humanReviewRequired: boolean };
export type EvidenceFinding = { caseId: string; policyMode?: 'UNAVAILABLE' | 'DEMO_ONLY' | 'AUTHORITATIVE'; requiredDocuments: string[]; foundDocuments: EvidenceDocumentMatch[]; possibleEvidenceMatches: EvidenceDocumentMatch[]; missingDocuments: string[]; unreadableDocuments: UnreadableEvidenceDocument[]; possibleMismatches: string[]; evidenceValidations: EvidenceRequirementValidation[]; verificationStatus: 'VERIFIED' | 'NEEDS_REVIEW'; explanation: string; explanationEngine?: string; recommendedAction: string; sourceReferences: EvidenceSourceReference[]; needsHumanReview: boolean };
export type EvidenceGapPrediction = { requiredDocument: string; mlPrediction: 'PREDICTED_MISSING' | 'NOT_PREDICTED_MISSING' | 'UNKNOWN'; verificationStatus: 'FOUND_IN_TEXT' | 'VERIFIED_MISSING' | 'UNKNOWN' | 'NEEDS_REVIEW' };
export type EvidencePrediction = { available: boolean; requirementsAvailable: boolean; experimental: boolean; validated: boolean; trainingDataSource: string; modelName: string; message: string; predictions: EvidenceGapPrediction[] };
export type EvidencePredictionResult = { finding: EvidenceFinding; prediction: EvidencePrediction };
type ApiEvidenceReference = { policy_id: string; document_type: string; effective_date: string; version: string; payer: string | null; service_type: string | null; source_reference: string | null; verification_status?: 'MOCK_DEMO' | 'VERIFIED_PUBLIC'; policy_use?: 'DEMO_ONLY' | 'AUTHORITATIVE'; source_url?: string | null; source_verified_by?: string | null; source_verified_at?: string | null; similarity: number };
type ApiEvidenceMatch = { required_document: string; document_name: string; category?: string; ocr_status?: string; page_number: number; excerpt: string };
type ApiUnreadableEvidenceDocument = { document_name: string; category?: string; ocr_status?: string; reason: string };
type ApiEvidenceFinding = { case_id: string; policy_mode?: EvidenceFinding['policyMode']; required_documents: string[]; found_documents: ApiEvidenceMatch[]; possible_evidence_matches: ApiEvidenceMatch[]; missing_documents: string[]; unreadable_documents: ApiUnreadableEvidenceDocument[]; possible_mismatches: string[]; evidence_validations?: { requirement: string; policy_required_categories?: string[]; validation_status: EvidenceRequirementValidation['validationStatus']; category_matched: boolean; text_matched: boolean; clinically_verified: boolean; reason: string; human_review_required: boolean }[]; verification_status: EvidenceFinding['verificationStatus']; explanation: string; explanation_engine?: string; recommended_action: string; source_references: ApiEvidenceReference[]; needs_human_review: boolean };
type ApiEvidenceGapPrediction = { required_document: string; ml_prediction: EvidenceGapPrediction['mlPrediction']; verification_status: EvidenceGapPrediction['verificationStatus'] };
type ApiEvidencePrediction = { available: boolean; requirements_available: boolean; experimental: boolean; validated: boolean; training_data_source: string; model_name: string; message: string; predictions: ApiEvidenceGapPrediction[] };
export type PayerRequirement = { requirementId: string; documentType: string; title: string; payer: string; serviceType: string; requiredDocuments: string[]; content: string; sourceReference: string; effectiveDate: string; expiresAt: string | null; version: string; approved: boolean; embeddingEngine: string | null; verificationStatus?: 'MOCK_DEMO' | 'VERIFIED_PUBLIC'; sourceUrl?: string | null; authoritativeTitle?: string | null; authoritativeVersion?: string | null; sourceVerifiedBy?: string | null; sourceVerifiedAt?: string | null };
export type PayerRequirementPayload = Omit<PayerRequirement, 'approved' | 'embeddingEngine' | 'verificationStatus' | 'sourceUrl' | 'authoritativeTitle' | 'authoritativeVersion' | 'sourceVerifiedBy' | 'sourceVerifiedAt'>;
type ApiPayerRequirement = { requirement_id: string; document_type: string; title: string; payer: string; service_type: string; required_documents: string; content: string; source_reference: string; effective_date: string; expires_at: string | null; version: string; approved: number; embedding_engine: string | null; verification_status?: 'MOCK_DEMO' | 'VERIFIED_PUBLIC'; source_url?: string | null; authoritative_title?: string | null; authoritative_version?: string | null; source_verified_by?: string | null; source_verified_at?: string | null };
export type EscalationReference = PolicyReference;
export type EscalationDecision = { caseId: string; escalationRequired: boolean; escalationLevel: 'NONE' | 'LEVEL_1' | 'LEVEL_2' | 'NEEDS_REVIEW'; reason: string; explanationEngine?: string; responsibleRole: string; recommendedAction: string; approvalRequired: boolean; sourceReferences: EscalationReference[]; notificationStatus: 'NOT_REQUIRED' | 'NOT_SENT' | 'PENDING_APPROVAL' | 'DUPLICATE_SUPPRESSED' | 'SIMULATED_DELIVERED' | 'SIMULATED_FAILED' };
type ApiEscalationDecision = { case_id: string; escalation_required: boolean; escalation_level: EscalationDecision['escalationLevel']; reason: string; explanation_engine?: string; responsible_role: string; recommended_action: string; approval_required: boolean; source_references: ApiPolicyReference[]; notification_status: EscalationDecision['notificationStatus'] };
export type EscalationPolicy = { policyId: string; documentType: 'ESCALATION_RULE' | 'ROLE_RESPONSIBILITY' | 'NOTIFICATION_PROCEDURE'; title: string; content: string; department: string; sourceReference: string; effectiveDate: string; expiresAt: string | null; version: string; approved: boolean; embeddingEngine: string | null };
const mapEscalationPolicy = (item: ApiEscalationPolicy): EscalationPolicy => ({ policyId: item.policy_id, documentType: item.document_type, title: item.title, content: item.content, department: item.department ?? 'All departments', sourceReference: item.source_reference ?? '', effectiveDate: item.effective_date, expiresAt: item.expires_at, version: item.version, approved: Boolean(item.approved), embeddingEngine: item.embedding_engine });
export type EscalationPolicyPayload = Omit<EscalationPolicy, 'approved' | 'embeddingEngine'>;
type ApiEscalationPolicy = { policy_id: string; document_type: EscalationPolicy['documentType']; title: string; content: string; department: string | null; source_reference: string | null; effective_date: string; expires_at: string | null; version: string; approved: number; embedding_engine: string | null };

export type RoleAssignment = { id: number; escalationLevel: 'LEVEL_1' | 'LEVEL_2'; responsibleRole: string; department: string; effectiveDate: string; expiresAt: string | null };
export type RoleAssignmentPayload = Omit<RoleAssignment, 'id'>;
type ApiRoleAssignment = { id: number; escalation_level: RoleAssignment['escalationLevel']; responsible_role: string; department: string; effective_date: string; expires_at: string | null };
const mapRoleAssignment = (item: ApiRoleAssignment): RoleAssignment => ({ id: item.id, escalationLevel: item.escalation_level, responsibleRole: item.responsible_role, department: item.department, effectiveDate: item.effective_date, expiresAt: item.expires_at });
export type RoleAssignmentHistoryItem = { id: number; assignmentId: number; changedAt: string; actor: string; action: 'CREATED' | 'UPDATED' | 'RETIRED'; previousValues: RoleAssignment | null; newValues: RoleAssignment };
type ApiRoleAssignmentHistoryItem = { id: number; assignment_id: number; changed_at: string; actor: string; action: RoleAssignmentHistoryItem['action']; previous_values: string | null; new_values: string };
const mapRoleAssignmentHistory = (item: ApiRoleAssignmentHistoryItem): RoleAssignmentHistoryItem => ({ id: item.id, assignmentId: item.assignment_id, changedAt: new Date(item.changed_at).toLocaleString(), actor: item.actor, action: item.action, previousValues: item.previous_values ? mapRoleAssignment(JSON.parse(item.previous_values) as ApiRoleAssignment) : null, newValues: mapRoleAssignment(JSON.parse(item.new_values) as ApiRoleAssignment) });
export type WorkflowFailure = { id: number; caseId: string; occurredAt: string; errorCode: string; errorDetails: string; workflowState: string };
type ApiWorkflowFailure = { id: number; case_id: string; occurred_at: string; error_code: string; error_details: string; workflow_state: string };
export type RecoveryDecision = { caseId: string; failureType: string; errorDetails: string; likelyCause: string; recommendedRecovery: string; narrative?: string; explanationEngine?: string; retryAllowed: boolean; humanApprovalRequired: boolean; recoveryStatus: 'RECOMMENDATION_ONLY' | 'PENDING_APPROVAL' | 'APPROVED' | 'SUCCESS' | 'FAILED' | 'MANUAL_FOLLOW_UP_CREATED'; sourceReferences: PolicyReference[] };
type ApiRecoveryDecision = { case_id: string; failure_type: string; error_details: string; likely_cause: string; recommended_recovery: string; narrative?: string; explanation_engine?: string; retry_allowed: boolean; human_approval_required: boolean; recovery_status: RecoveryDecision['recoveryStatus']; source_references: ApiPolicyReference[] };
const mapWorkflowFailure = (item: ApiWorkflowFailure): WorkflowFailure => ({ id: item.id, caseId: item.case_id, occurredAt: new Date(item.occurred_at).toLocaleString(), errorCode: item.error_code, errorDetails: item.error_details, workflowState: item.workflow_state });
const mapRecoveryDecision = (item: ApiRecoveryDecision): RecoveryDecision => ({ caseId: item.case_id, failureType: item.failure_type, errorDetails: item.error_details, likelyCause: item.likely_cause, recommendedRecovery: item.recommended_recovery, narrative: item.narrative, explanationEngine: item.explanation_engine, retryAllowed: item.retry_allowed, humanApprovalRequired: item.human_approval_required, recoveryStatus: item.recovery_status, sourceReferences: item.source_references.map((source: ApiPolicyReference) => ({ policyId: source.policy_id, documentType: source.document_type, effectiveDate: source.effective_date, version: source.version, similarity: source.similarity })) });

const mapPayerRequirement = (item: ApiPayerRequirement): PayerRequirement => ({ requirementId: item.requirement_id, documentType: item.document_type, title: item.title, payer: item.payer, serviceType: item.service_type, requiredDocuments: JSON.parse(item.required_documents || '[]') as string[], content: item.content, sourceReference: item.source_reference, effectiveDate: item.effective_date, expiresAt: item.expires_at, version: item.version, approved: Boolean(item.approved), embeddingEngine: item.embedding_engine, verificationStatus: item.verification_status, sourceUrl: item.source_url, authoritativeTitle: item.authoritative_title, authoritativeVersion: item.authoritative_version, sourceVerifiedBy: item.source_verified_by, sourceVerifiedAt: item.source_verified_at });
const mapEscalationDecision = (item: ApiEscalationDecision): EscalationDecision => ({ caseId: item.case_id, escalationRequired: item.escalation_required, escalationLevel: item.escalation_level, reason: item.reason, explanationEngine: item.explanation_engine, recommendedAction: item.recommended_action, responsibleRole: item.responsible_role, approvalRequired: item.approval_required, sourceReferences: item.source_references.map((source: ApiPolicyReference) => ({ policyId: source.policy_id, documentType: source.document_type, effectiveDate: source.effective_date, version: source.version, similarity: source.similarity })), notificationStatus: item.notification_status });
const mapRecommendation = (item: ApiSlaRecommendation, index: number): SlaRecommendation => ({ caseId: item.case_id, rank: item.rank ?? index + 1, riskLevel: item.risk_level, remainingTime: item.remaining_time, remainingHours: item.remaining_hours, priority: item.priority, explanation: item.explanation, explanationEngine: item.explanation_engine, recommendedAction: item.recommended_action, assignedRole: item.assigned_role, sourceReferences: item.source_references.map((source: ApiPolicyReference) => ({ policyId: source.policy_id, documentType: source.document_type, effectiveDate: source.effective_date, version: source.version, similarity: source.similarity })), confidence: item.confidence, needsHumanReview: item.needs_human_review });
const mapEvidenceFinding = (item: ApiEvidenceFinding): EvidenceFinding => ({ caseId: item.case_id, policyMode: item.policy_mode, requiredDocuments: item.required_documents, foundDocuments: item.found_documents.map((match) => ({ requiredDocument: match.required_document, documentName: match.document_name, category: match.category ?? '', ocrStatus: match.ocr_status ?? '', pageNumber: match.page_number, excerpt: match.excerpt })), possibleEvidenceMatches: item.possible_evidence_matches.map((match) => ({ requiredDocument: match.required_document, documentName: match.document_name, category: match.category ?? '', ocrStatus: match.ocr_status ?? '', pageNumber: match.page_number, excerpt: match.excerpt })), missingDocuments: item.missing_documents, unreadableDocuments: item.unreadable_documents.map((document) => ({ documentName: document.document_name, category: document.category ?? '', ocrStatus: document.ocr_status ?? '', reason: document.reason })), possibleMismatches: item.possible_mismatches, evidenceValidations: (item.evidence_validations ?? []).map((validation) => ({ requirement: validation.requirement, policyRequiredCategories: validation.policy_required_categories ?? [], validationStatus: validation.validation_status, categoryMatched: validation.category_matched, textMatched: validation.text_matched, clinicallyVerified: validation.clinically_verified, reason: validation.reason, humanReviewRequired: validation.human_review_required })), verificationStatus: item.verification_status, explanation: item.explanation, explanationEngine: item.explanation_engine, recommendedAction: item.recommended_action, sourceReferences: item.source_references.map((source: ApiEvidenceReference) => ({ policyId: source.policy_id, documentType: source.document_type, effectiveDate: source.effective_date, version: source.version, payer: source.payer, serviceType: source.service_type, sourceReference: source.source_reference, verificationStatus: source.verification_status, policyUse: source.policy_use, sourceUrl: source.source_url, sourceVerifiedBy: source.source_verified_by, sourceVerifiedAt: source.source_verified_at, similarity: source.similarity })), needsHumanReview: item.needs_human_review });
const mapEvidencePrediction = (item: ApiEvidencePrediction): EvidencePrediction => ({ available: item.available, requirementsAvailable: item.requirements_available, experimental: item.experimental, validated: item.validated, trainingDataSource: item.training_data_source, modelName: item.model_name, message: item.message, predictions: item.predictions.map((prediction) => ({ requiredDocument: prediction.required_document, mlPrediction: prediction.ml_prediction, verificationStatus: prediction.verification_status })) });
const mapPolicy = (item: ApiPolicyDocument): PolicyDocument => ({ policyId: item.policy_id, documentType: item.document_type, title: item.title, content: item.content, effectiveDate: item.effective_date, expiresAt: item.expires_at, version: item.version, approved: Boolean(item.approved), embeddingEngine: item.embedding_engine });
const mapCase = (item: ApiCase): PaCase => ({ id: item.id, patientReference: item.patient_reference, payer: item.payer, service: item.service, submissionDate: item.submission_date, deadline: item.deadline, status: item.status, risk: item.risk, coordinator: item.coordinator, priority: item.priority });
const mapDocument = (item: ApiDocument): Document => ({ id: item.id, caseId: item.case_id, name: item.name, status: item.status, category: item.category ?? item.name, originalFilename: item.original_filename, uploadedAt: item.uploaded_at, processingStatus: item.processing_status, ocrStatus: item.ocr_status, extractedText: item.extracted_text ?? '', evidenceSummary: item.evidence_summary, processingError: item.processing_error ?? '' });
const mapAudit = (item: ApiAudit): AuditEvent => ({ id: item.id, caseId: item.case_id, timestamp: new Date(item.timestamp).toLocaleString(), actor: item.actor, action: item.action.replaceAll('_', ' '), detail: item.detail, decisionType: item.decision_type.replaceAll('_', ' ') });

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  if (!isPaApiConfigured) throw new Error('PA Guard API is not configured for this environment');
  let response: Response;
  try {
    const headers = new Headers(options?.headers);
    const token = getAuthToken();
    if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`);
    if (options?.body && !(typeof FormData !== 'undefined' && options.body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });
  } catch {
    throw new ApiUnavailableError();
  }
  if (!response.ok) {
    const body = await response.json().catch((): { detail?: unknown } => ({})) as { detail?: unknown };
    if (response.status === 401) notifyAuthExpired();
    throw new ApiResponseError(response.status, body.detail);
  }
  return response.json() as Promise<T>;
}

export type SlaSettings = { warningHours: number; urgentHours: number; escalationOneHours: number; escalationTwoHours: number; remainingTimeWeight: number; priorityWeight: number; evidenceWeight: number };
type ApiSlaSettings = { warning_hours: number; urgent_hours: number; escalation_one_hours: number; escalation_two_hours: number; remaining_time_weight: number; priority_weight: number; evidence_weight: number };
const mapSettings = (item: ApiSlaSettings): SlaSettings => ({ warningHours: item.warning_hours, urgentHours: item.urgent_hours, escalationOneHours: item.escalation_one_hours, escalationTwoHours: item.escalation_two_hours, remainingTimeWeight: item.remaining_time_weight, priorityWeight: item.priority_weight, evidenceWeight: item.evidence_weight });
export type SettingsHistoryItem = { id: number; changedAt: string; actor: string; fieldName: string; previousValue: number; newValue: number };
type ApiSettingsHistoryItem = { id: number; changed_at: string; actor: string; field_name: string; previous_value: number; new_value: number };
const mapSettingsHistory = (item: ApiSettingsHistoryItem): SettingsHistoryItem => ({ id: item.id, changedAt: new Date(item.changed_at).toLocaleString(), actor: item.actor, fieldName: item.field_name, previousValue: item.previous_value, newValue: item.new_value });
const mapSlaRiskPrediction = (item: ApiSlaRiskPrediction): SlaRiskPrediction => ({ caseId: item.case_id, available: item.available, predictedSlaBreach: item.predicted_sla_breach, breachProbability: item.breach_probability, modelVersion: item.model_version, predictionTimestamp: item.prediction_timestamp, evaluationProvenance: item.evaluation_provenance, decisionScope: item.decision_scope, riskLevel: item.risk_level, riskScore: item.risk_score, scoreMeaning: item.score_meaning, factors: item.factors, reliable: item.reliable, reliabilityNote: item.reliability_note, trainingDataSource: item.training_data_source, operationalRisk: item.operational_risk ? { category: item.operational_risk.risk_category, breached: item.operational_risk.breached, deadline: item.operational_risk.deadline, remainingHours: item.operational_risk.remaining_hours, reasons: item.operational_risk.reasons, recommendedAction: item.operational_risk.recommended_action, escalationRequired: item.operational_risk.escalation_required, ruleVersion: item.operational_risk.configuration.rule_version } : undefined });
const mapWorkflowSummary = (item: ApiWorkflowSummary): CaseWorkflowSummary => ({
  caseId: item.case_id,
  caseStatus: item.case_status,
  slaStatus: item.sla_status,
  deadline: item.deadline,
  documents: item.documents.map((document) => ({
    documentId: document.document_id,
    filename: document.filename,
    category: document.category,
    verificationStatus: document.verification_status,
    reviewer: document.reviewer,
    ocrStatus: document.ocr_status,
    processingStatus: document.processing_status,
    extractedTextAvailable: document.extracted_text_available,
    pageTextAvailable: document.page_text_available,
    processingError: document.processing_error,
    fileSize: document.file_size,
    uploadedAt: document.uploaded_at,
  })),
  unverifiedDocuments: item.unverified_documents.map((document) => ({
    documentId: document.document_id, filename: document.filename, category: document.category,
    verificationStatus: document.verification_status, reviewer: document.reviewer,
    ocrStatus: document.ocr_status, processingStatus: document.processing_status,
    extractedTextAvailable: document.extracted_text_available, pageTextAvailable: document.page_text_available,
    processingError: document.processing_error, fileSize: document.file_size, uploadedAt: document.uploaded_at,
  })),
  unverifiedDocumentCount: item.unverified_document_count,
  evidenceReferenceCount: item.evidence_reference_count,
  responsibleStaff: item.responsible_staff,
  activeEscalation: item.active_escalation,
  mlPriorityHumanReviewRecommended: item.ml_priority_human_review_recommended,
  evidenceItems: item.evidence_items.map((evidence) => ({
    requirement: evidence.requirement,
    status: evidence.status,
    supportingDocuments: evidence.supporting_documents.map((document) => ({
      filename: document.filename,
      category: document.category,
      pageNumber: document.page_number,
      excerpt: document.excerpt,
    })),
    explanation: evidence.explanation,
    humanReviewRequired: evidence.human_review_required,
  })),
  unresolvedRequirements: item.unresolved_requirements.map((evidence) => ({
    requirement: evidence.requirement,
    status: evidence.status,
    supportingDocuments: evidence.supporting_documents.map((document) => ({
      filename: document.filename,
      category: document.category,
      pageNumber: document.page_number,
      excerpt: document.excerpt,
    })),
    explanation: evidence.explanation,
    humanReviewRequired: evidence.human_review_required,
  })),
  humanReviewItems: item.human_review_items,
  escalation: item.escalation ? mapEscalationDecision(item.escalation) : null,
  escalationReason: item.escalation_reason,
  recommendedNextAction: item.recommended_next_action,
});
const mapWorkflowStage = <TInput, TOutput>(
  stage: ApiWorkflowStage<TInput>,
  mapResult: (value: TInput) => TOutput,
): WorkflowStage<TOutput> => ({
  status: stage.status,
  ...(stage.result === undefined ? {} : { result: mapResult(stage.result) }),
  ...(stage.error ? { error: stage.error } : {}),
  ...(stage.reason ? { reason: stage.reason } : {}),
  ...(stage.retryable ? { retryable: true } : {}),
});
const mapCaseWorkflow = (item: ApiCaseWorkflow): CaseWorkflowResult => ({
  caseId: item.case_id,
  workflowStatus: item.workflow_status,
  stages: {
    evidenceAgent: mapWorkflowStage(item.stages.evidence_agent, mapEvidenceFinding),
    evidencePrediction: mapWorkflowStage(item.stages.evidence_prediction, mapEvidencePrediction),
    slaAgent: mapWorkflowStage(item.stages.sla_agent, (result) => result),
    slaRiskPrediction: mapWorkflowStage(item.stages.sla_risk_prediction, mapSlaRiskPrediction),
    escalationAgent: mapWorkflowStage(item.stages.escalation_agent, mapEscalationDecision),
    recoveryAgent: mapWorkflowStage(item.stages.recovery_agent, mapRecoveryDecision),
    llmExplanation: mapWorkflowStage(item.stages.llm_explanation, (result) => result),
    summary: mapWorkflowStage(item.stages.summary, mapWorkflowSummary),
  },
  llmExplanation: item.llm_explanation,
  summary: mapWorkflowSummary(item.summary),
});

export type SubmissionReadiness = {
  case: { id: string; payer: string; service: string; submission_date: string; deadline: string; status: string };
  status: 'NOT_READY' | 'NEEDS_HUMAN_REVIEW' | 'READY_FOR_REVIEW' | 'APPROVED_FOR_SUBMISSION' | 'SUBMITTED_DEMO' | 'DRAFT';
  version?: number;
  evaluated_at?: string;
  required_evidence: Array<{ requirement: string; status: string; provenance: Array<{ document_id: number; filename: string; category: string; page_number: number; excerpt: string }> }>;
  missing_items: string[];
  conflicts: string[];
  possible_matches: string[];
  unreadable_documents: Array<{ document_id: number; filename: string; ocr_status: string }>;
  evidence_provenance: Array<{ document_id: number; filename: string; category: string; page_number: number; excerpt: string }>;
  sla_status: string;
  hours_remaining: number | null;
  sla_risk_prediction: { available: boolean; reason?: string; risk_level?: string; risk_score?: number; score_meaning?: string; reliability_note?: string };
  applicable_requirements: Array<{ policy_id: string; title: string; version: string; source_reference: string | null }>;
};
export type SubmissionStatus = {
  status: SubmissionReadiness['status'];
  readiness: SubmissionReadiness | null;
  approval: { readiness_version: number; reason: string; approved_at: string; display_name: string } | null;
  attempts: Array<{ idempotency_key: string; adapter_type: string; status: string; reference_id: string | null; response_summary: string; attempted_at: string }>;
};
export type EvidenceTrail = {
  case_id: string;
  available: boolean;
  policy: { policy_id: string; version: string; title: string; verification_status: string } | null;
  criteria: Array<{ criterion_id: string; criterion_text: string; verification_status: string; review_status: string | null; assessment_status: string | null; reviewer_id: string | null; reviewed_at: string | null; review_notes: string | null }>;
  documents?: Array<{ id: number; name: string; category: string | null; ocr_status: string | null; processing_status: string | null }>;
  message: string;
};

export type FastestSafePath = {
  case_id: string; generated_at: string; time_remaining_minutes: number | null;
  verdict: 'ACHIEVABLE' | 'TIGHT' | 'INFEASIBLE_WITHOUT_EXTENSION' | 'BLOCKED_BY_PERMISSION' | 'UNCERTAIN' | 'READY_FOR_HUMAN_SUBMISSION_REVIEW';
  verdict_explanation: string; critical_path_minutes: number | null;
  steps: Array<{ order: number; action_type: string; description: string; required_role: string | null; estimated_minutes: number | null; is_estimate: boolean; blocking: boolean; criterion_id: string | null; document_id: number | null; can_current_user_perform: boolean; dependencies: string[]; parallelizable: boolean; external_wait_unknown: boolean }>;
  unresolved_gaps: string[]; assumptions: string[]; disclaimer: string; duration_is_estimate: boolean; external_wait_unknown: boolean; deadline_unknown: boolean;
};

export const paApi = {
  async getFastestSafePath(caseId: string): Promise<FastestSafePath> { return request<FastestSafePath>(`/api/cases/${encodeURIComponent(caseId)}/fastest-safe-path`); },
  async getEvidenceTrail(caseId: string): Promise<EvidenceTrail> { return request<EvidenceTrail>(`/api/cases/${encodeURIComponent(caseId)}/evidence-trail`); },
  async reviewEvidenceTrail(caseId: string, criterionId: string, payload: { document_id: number | null; assessment_status: 'MATCH_POSSIBLE' | 'NOT_FOUND' | 'UNCERTAIN' | 'CONFLICTING'; review_status: 'HUMAN_CONFIRMED' | 'HUMAN_REJECTED'; supporting_excerpt: string; source_locator: string; review_notes: string }): Promise<void> { await request(`/api/cases/${encodeURIComponent(caseId)}/evidence-trail/${encodeURIComponent(criterionId)}/review`, { method: 'POST', body: JSON.stringify(payload) }); },
  async getSubmissionStatus(caseId: string): Promise<SubmissionStatus> { return request<SubmissionStatus>(`/api/cases/${encodeURIComponent(caseId)}/submission/readiness`); },
  async evaluateSubmissionReadiness(caseId: string): Promise<SubmissionReadiness> { return request<SubmissionReadiness>(`/api/cases/${encodeURIComponent(caseId)}/submission/readiness`, { method: 'POST', body: '{}' }); },
  async approveSubmission(caseId: string, readinessVersion: number, reason: string): Promise<{ status: string; approved_at: string; approved_by: string }> { return request(`/api/cases/${encodeURIComponent(caseId)}/submission/approve`, { method: 'POST', body: JSON.stringify({ readiness_version: readinessVersion, reason }) }); },
  async submitDemo(caseId: string, readinessVersion: number, idempotencyKey: string): Promise<{ status: string; adapter_type: string; reference_id: string | null; response_summary: string; attempted_at: string }> { return request(`/api/cases/${encodeURIComponent(caseId)}/submission/submit-demo`, { method: 'POST', body: JSON.stringify({ readiness_version: readinessVersion, idempotency_key: idempotencyKey }) }); },
  async getAgentStatus(): Promise<AgentSystemStatus> { return request<AgentSystemStatus>('/api/agents/status'); },
  async getCases(): Promise<PaCase[]> { return (await request<ApiCase[]>('/api/cases')).map(mapCase); },
  async getPolicies(): Promise<PolicyDocument[]> { return (await request<ApiPolicyDocument[]>('/api/policies')).map(mapPolicy); },
  async getRoleAssignmentHistory(): Promise<RoleAssignmentHistoryItem[]> { return (await request<ApiRoleAssignmentHistoryItem[]>('/api/role-assignments/history')).map(mapRoleAssignmentHistory); },
  async getPayerRequirements(): Promise<PayerRequirement[]> { return (await request<ApiPayerRequirement[]>('/api/payer-requirements')).map(mapPayerRequirement); },
  async createPayerRequirement(payload: PayerRequirementPayload): Promise<PayerRequirement> { return mapPayerRequirement(await request<ApiPayerRequirement>('/api/payer-requirements', { method: 'POST', body: JSON.stringify({ requirement_id: payload.requirementId, document_type: payload.documentType, title: payload.title, payer: payload.payer, service_type: payload.serviceType, required_documents: payload.requiredDocuments, content: payload.content, source_reference: payload.sourceReference, effective_date: payload.effectiveDate, expires_at: payload.expiresAt, version: payload.version }) })); },
  async updatePayerRequirement(originalId: string, originalVersion: string, payload: PayerRequirementPayload): Promise<PayerRequirement> { return mapPayerRequirement(await request<ApiPayerRequirement>(`/api/payer-requirements/${encodeURIComponent(originalId)}/${encodeURIComponent(originalVersion)}`, { method: 'PUT', body: JSON.stringify({ document_type: payload.documentType, title: payload.title, payer: payload.payer, service_type: payload.serviceType, required_documents: payload.requiredDocuments, content: payload.content, source_reference: payload.sourceReference, effective_date: payload.effectiveDate, expires_at: payload.expiresAt, version: payload.version }) })); },
  async getRoleAssignments(): Promise<RoleAssignment[]> { return (await request<ApiRoleAssignment[]>('/api/role-assignments')).map(mapRoleAssignment); },
  async createRoleAssignment(payload: RoleAssignmentPayload): Promise<RoleAssignment> { return mapRoleAssignment(await request<ApiRoleAssignment>('/api/role-assignments', { method: 'POST', body: JSON.stringify({ escalation_level: payload.escalationLevel, responsible_role: payload.responsibleRole, department: payload.department, effective_date: payload.effectiveDate, expires_at: payload.expiresAt }) })); },
  async updateRoleAssignment(id: number, payload: RoleAssignmentPayload): Promise<RoleAssignment> { return mapRoleAssignment(await request<ApiRoleAssignment>(`/api/role-assignments/${id}`, { method: 'PUT', body: JSON.stringify({ escalation_level: payload.escalationLevel, responsible_role: payload.responsibleRole, department: payload.department, effective_date: payload.effectiveDate, expires_at: payload.expiresAt }) })); },
  async retireRoleAssignment(id: number): Promise<RoleAssignment> { return mapRoleAssignment(await request<ApiRoleAssignment>(`/api/role-assignments/${id}/retire`, { method: 'POST' })); },
  async setPayerRequirementApproval(requirementId: string, version: string, approved: boolean): Promise<void> { await request(`/api/payer-requirements/${encodeURIComponent(requirementId)}/${encodeURIComponent(version)}/approval`, { method: 'POST', body: JSON.stringify({ actor: 'Current user', approved }) }); },
  async getEscalationPolicies(): Promise<EscalationPolicy[]> { return (await request<ApiEscalationPolicy[]>('/api/escalation-policies')).map(mapEscalationPolicy); },
  async createEscalationPolicy(payload: EscalationPolicyPayload): Promise<EscalationPolicy> { return mapEscalationPolicy(await request<ApiEscalationPolicy>('/api/escalation-policies', { method: 'POST', body: JSON.stringify({ policy_id: payload.policyId, document_type: payload.documentType, title: payload.title, content: payload.content, department: payload.department, source_reference: payload.sourceReference, effective_date: payload.effectiveDate, expires_at: payload.expiresAt, version: payload.version }) })); },
  async updateEscalationPolicy(originalId: string, originalVersion: string, payload: EscalationPolicyPayload): Promise<EscalationPolicy> { return mapEscalationPolicy(await request<ApiEscalationPolicy>(`/api/escalation-policies/${encodeURIComponent(originalId)}/${encodeURIComponent(originalVersion)}`, { method: 'PUT', body: JSON.stringify({ document_type: payload.documentType, title: payload.title, content: payload.content, department: payload.department, source_reference: payload.sourceReference, effective_date: payload.effectiveDate, expires_at: payload.expiresAt, version: payload.version }) })); },
  async setEscalationPolicyApproval(policyId: string, version: string, approved: boolean): Promise<void> { await request(`/api/escalation-policies/${encodeURIComponent(policyId)}/${encodeURIComponent(version)}/approval`, { method: 'POST', body: JSON.stringify({ actor: 'Current user', approved }) }); },
  async createPolicy(payload: PolicyPayload): Promise<PolicyDocument> { return mapPolicy(await request<ApiPolicyDocument>('/api/policies', { method: 'POST', body: JSON.stringify({ policy_id: payload.policyId, document_type: payload.documentType, title: payload.title, content: payload.content, effective_date: payload.effectiveDate, expires_at: payload.expiresAt, version: payload.version }) })); },
  async updatePolicy(originalPolicyId: string, originalVersion: string, payload: PolicyPayload): Promise<PolicyDocument> { return mapPolicy(await request<ApiPolicyDocument>(`/api/policies/${encodeURIComponent(originalPolicyId)}/${encodeURIComponent(originalVersion)}`, { method: 'PUT', body: JSON.stringify({ document_type: payload.documentType, title: payload.title, content: payload.content, effective_date: payload.effectiveDate, expires_at: payload.expiresAt, version: payload.version }) })); },
  async setPolicyApproval(policyId: string, version: string, approved: boolean): Promise<void> { await request(`/api/policies/${encodeURIComponent(policyId)}/${encodeURIComponent(version)}/approval`, { method: 'POST', body: JSON.stringify({ actor: 'Current user', approved }) }); },
  async reindexPolicies(): Promise<{ indexed: number; embedding_engine: string }> { return request('/api/agents/sla/index', { method: 'POST', body: JSON.stringify({ actor: 'Current user' }) }); },
  async getSettingsHistory(): Promise<SettingsHistoryItem[]> { return (await request<ApiSettingsHistoryItem[]>('/api/settings/history')).map(mapSettingsHistory); },
  async getWorkflowFailures(): Promise<WorkflowFailure[]> { return (await request<ApiWorkflowFailure[]>('/api/workflow-failures')).map(mapWorkflowFailure); },
  async runRecoveryAgent(caseId: string, errorCode?: string, approveRecovery = false, previewOnly = false): Promise<RecoveryDecision> { const result = await request<{ recovery: ApiRecoveryDecision }>(`/api/agents/recovery/cases/${encodeURIComponent(caseId)}/recover`, { method: 'POST', body: JSON.stringify({ actor: 'Current user', error_code: errorCode ?? null, approve_recovery: approveRecovery, preview_only: previewOnly }) }); return mapRecoveryDecision(result.recovery); },
  async predictEvidenceGaps(caseId: string): Promise<EvidencePredictionResult> { const result = await request<{ finding: ApiEvidenceFinding; prediction: ApiEvidencePrediction }>(`/api/agents/evidence/cases/${encodeURIComponent(caseId)}/predict`, { method: 'POST', body: JSON.stringify({ actor: 'Current user' }) }); return { finding: mapEvidenceFinding(result.finding), prediction: mapEvidencePrediction(result.prediction) }; },
  async getDocuments(): Promise<Document[]> { return (await request<ApiDocument[]>('/api/documents')).map(mapDocument); },
  async uploadDocument(caseId: string, file: File, category: string, submissionId?: string): Promise<Document> { const formData = new FormData(); formData.append('file', file); formData.append('category', category); if (submissionId) formData.append('submission_id', submissionId); const result = await request<ApiDocument>(`/api/cases/${encodeURIComponent(caseId)}/documents`, { method: 'POST', body: formData }); return mapDocument(result); },
  async uploadDocuments(caseId: string, files: { file: File; category: string; submissionId: string }[]): Promise<{ documents: Document[]; savedSubmissionIds: string[]; failures: ApiBatchUpload['failures'] }> {
    const formData = new FormData();
    for (const entry of files) {
      formData.append('files', entry.file);
      formData.append('categories', entry.category);
      formData.append('submission_ids', entry.submissionId);
    }
    const result = await request<ApiBatchUpload>(`/api/cases/${encodeURIComponent(caseId)}/documents/batch`, { method: 'POST', body: formData });
    return { documents: result.documents.map(mapDocument), savedSubmissionIds: result.saved_submission_ids, failures: result.failures };
  },
  async retryDocumentProcessing(documentId: number): Promise<Document> { const result = await request<ApiDocument>(`/api/documents/${encodeURIComponent(String(documentId))}/process`, { method: 'POST' }); const document = mapDocument(result); if (document.ocrStatus === 'FAILED') throw new Error(`OCR retry failed: ${document.processingError ?? 'No readable text was extracted.'}`); return document; },
  async getAudit(): Promise<AuditEvent[]> { return (await request<ApiAudit[]>('/api/audit')).map(mapAudit); },
  async runEvidenceAgent(caseId: string): Promise<EvidenceFinding> { const result = await request<{ finding: ApiEvidenceFinding }>(`/api/agents/evidence/cases/${encodeURIComponent(caseId)}/review`, { method: 'POST', body: JSON.stringify({ actor: 'Current user' }) }); return mapEvidenceFinding(result.finding); },
  async runCaseWorkflow(caseId: string): Promise<CaseWorkflowResult> { return mapCaseWorkflow(await request<ApiCaseWorkflow>(`/api/agents/cases/${encodeURIComponent(caseId)}/workflow`, { method: 'POST', body: JSON.stringify({ actor: 'Current user' }) })); },
  async retryFailedCaseWorkflowStages(caseId: string): Promise<CaseWorkflowResult> { return mapCaseWorkflow(await request<ApiCaseWorkflow>(`/api/agents/cases/${encodeURIComponent(caseId)}/workflow/retry`, { method: 'POST', body: JSON.stringify({ actor: 'Current user' }) })); },
  async requestCaseExplanation(caseId: string): Promise<CaseExplanationStatus> { return request(`/api/agents/cases/${encodeURIComponent(caseId)}/workflow/explanation`, { method: 'POST' }); },
  async getCaseExplanationStatus(caseId: string): Promise<CaseExplanationStatus> { return request(`/api/agents/cases/${encodeURIComponent(caseId)}/workflow/explanation`); },
  async recommendSlaCase(caseId: string): Promise<SlaRecommendation> { const result = await request<ApiSlaRecommendation>(`/api/agents/sla/cases/${encodeURIComponent(caseId)}/recommend`, { method: 'POST', body: JSON.stringify({ actor: 'Current user' }) }); return mapRecommendation(result, 0); },
  async getSettings(): Promise<SlaSettings> { return mapSettings(await request<ApiSlaSettings>('/api/settings')); },
  async createCase(payload: CaseCreatePayload): Promise<PaCase> { return mapCase(await request<ApiCase>('/api/cases', { method: 'POST', body: JSON.stringify({ patient_reference: payload.patientReference, payer: payload.payer, service: payload.service, deadline: payload.deadline, coordinator: payload.coordinator }) })); },
  async runEscalationAgent(caseId: string, approveNotification = false): Promise<EscalationDecision> { const result = await request<{ decision: ApiEscalationDecision }>(`/api/agents/escalation/cases/${encodeURIComponent(caseId)}/evaluate`, { method: 'POST', body: JSON.stringify({ actor: 'Current user', approve_notification: approveNotification }) }); return mapEscalationDecision(result.decision); },
  async runSlaAgent(): Promise<SlaRun> { const result = await request<{ run_at: string; mode: 'recommendation_only'; engine: string; recommendations: ApiSlaRecommendation[] }>('/api/agents/sla/run', { method: 'POST', body: JSON.stringify({ actor: 'Current user' }) }); return { runAt: result.run_at, mode: result.mode, engine: result.engine, recommendations: result.recommendations.map((item: ApiSlaRecommendation, index: number) => mapRecommendation(item, index)) }; },
  async predictSlaRisk(caseId: string): Promise<SlaRiskPrediction> { return mapSlaRiskPrediction(await request<ApiSlaRiskPrediction>(`/api/cases/${encodeURIComponent(caseId)}/sla-risk-prediction`)); },
  async updateCase(id: string, coordinator: string, expectedCoordinator: string): Promise<PaCase> { return mapCase(await request<ApiCase>(`/api/cases/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ coordinator, reason: 'Coordinator metadata correction', expected_values: { coordinator: expectedCoordinator } }) })); },
  async verifyDocument(id: number): Promise<void> { await request(`/api/documents/${id}/verify`, { method: 'POST', body: JSON.stringify({ actor: 'Current user', note: 'Document verified after human clinical review.' }) }); },
  async escalate(caseId: string, reason: string, explanation: string): Promise<{ to_role: string; to_staff_name: string }> { return request(`/api/cases/${encodeURIComponent(caseId)}/escalate`, { method: 'POST', body: JSON.stringify({ reason, explanation }) }); },
  async saveSettings(values: SlaSettings): Promise<SlaSettings> { return mapSettings(await request<ApiSlaSettings>('/api/settings', { method: 'PUT', body: JSON.stringify({ warning_hours: values.warningHours, urgent_hours: values.urgentHours, escalation_one_hours: values.escalationOneHours, escalation_two_hours: values.escalationTwoHours, remaining_time_weight: values.remainingTimeWeight, priority_weight: values.priorityWeight, evidence_weight: values.evidenceWeight }) })); },
};
