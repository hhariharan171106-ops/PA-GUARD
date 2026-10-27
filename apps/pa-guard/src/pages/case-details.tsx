import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Bot, CheckCircle2, Clock3, FileText, LoaderCircle, RefreshCw, UploadCloud, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { usePa } from '@/lib/pa-context';
import { useAuth } from '@/lib/auth-context';
import { ApiResponseError, isPaApiConfigured, paApi, type CaseExplanationStatus, type CaseWorkflowResult, type EvidenceFinding, type EvidenceTrail, type FastestSafePath } from '@/lib/pa-api';
import type { Document } from '@/lib/pa-data';
import { formatDiagnosticForUser, formatStatusLabel } from '@/lib/user-messages';
import { formatTaskHistoryDetail, historySourceLabel } from '@/lib/task-history-format';

const documentCategories = ['Clinical Reports', 'Imaging Reports', 'Treatment Reports', 'Medical Necessity Documents', 'Prescription'];
const supportedFileTypes = '.pdf,.png,.jpg,.jpeg,.tif,.tiff';
type QueuedDocument = { id: string; file: File; category: string; submissionId: string; error?: string };
type WorkflowStepId = 'validation' | 'upload' | 'ocr' | 'agents' | 'llm' | 'summary' | 'complete';
type WorkflowStepStatus = 'pending' | 'running' | 'completed' | 'partial' | 'failed' | 'skipped';
type WorkflowProgress = { status: WorkflowStepStatus; detail?: string };
const workflowStepLabels: { id: WorkflowStepId; label: string }[] = [
  { id: 'validation', label: 'Validate documents' },
  { id: 'upload', label: 'Upload documents' },
  { id: 'ocr', label: 'Extract text from documents (OCR)' },
  { id: 'agents', label: 'Check documents, deadlines, escalation, and recovery' },
  { id: 'llm', label: 'Prepare deterministic analysis' },
  { id: 'summary', label: 'Prepare evidence and escalation summary' },
  { id: 'complete', label: 'Submission workflow' },
];
const createInitialProgress = (): Record<WorkflowStepId, WorkflowProgress> => ({
  validation: { status: 'pending' },
  upload: { status: 'pending' },
  ocr: { status: 'pending' },
  agents: { status: 'pending' },
  llm: { status: 'pending' },
  summary: { status: 'pending' },
  complete: { status: 'pending' },
});

export default function CaseDetailsPage() {
  const queryClient = useQueryClient();
  const { id } = useParams();
  const { cases, documents, audit, isLoading, updateCase, escalate, uploadDocuments, retryDocumentProcessing } = usePa();
  const { user } = useAuth();
  const item = cases.find((entry) => entry.id === id);
  const [coordinator, setCoordinator] = useState(item?.coordinator ?? '');
  const [pendingDocuments, setPendingDocuments] = useState<QueuedDocument[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [category, setCategory] = useState('Clinical Reports');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [finding, setFinding] = useState<EvidenceFinding | null>(null);
  const [workflowResult, setWorkflowResult] = useState<CaseWorkflowResult | null>(null);
  const [aiEnhancement, setAiEnhancement] = useState<CaseExplanationStatus>({ status: 'NOT_REQUESTED' });
  const [aiBusy, setAiBusy] = useState(false);
  const [workflowProgress, setWorkflowProgress] = useState(createInitialProgress);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [escalationOpen, setEscalationOpen] = useState(false);
  const [escalationReason, setEscalationReason] = useState('SLA deadline approaching');
  const [escalationExplanation, setEscalationExplanation] = useState('');
  const [escalationBusy, setEscalationBusy] = useState(false);
  const [escalationError, setEscalationError] = useState('');
  const [evidenceTrail, setEvidenceTrail] = useState<EvidenceTrail | null>(null);
  const [evidenceTrailError, setEvidenceTrailError] = useState('');
  const [reviewCriterion, setReviewCriterion] = useState('');
  const [reviewDocumentId, setReviewDocumentId] = useState('');
  const [reviewExcerpt, setReviewExcerpt] = useState('');
  const [reviewLocator, setReviewLocator] = useState('');
  const [reviewNotes, setReviewNotes] = useState('');
  const [reviewStatus, setReviewStatus] = useState<'HUMAN_CONFIRMED' | 'HUMAN_REJECTED'>('HUMAN_CONFIRMED');
  const [reviewAssessment, setReviewAssessment] = useState<'MATCH_POSSIBLE' | 'NOT_FOUND' | 'UNCERTAIN' | 'CONFLICTING'>('UNCERTAIN');
  const [reviewBusy, setReviewBusy] = useState(false);
  const [safePath, setSafePath] = useState<FastestSafePath | null>(null);
  const [safePathError, setSafePathError] = useState('');
  const [safePathLoading, setSafePathLoading] = useState(false);
  const refreshSafePath = async () => {
    if (!id) return;
    setSafePathLoading(true); setSafePath(null); setSafePathError('');
    try { setSafePath(await paApi.getFastestSafePath(id)); void queryClient.invalidateQueries({ queryKey: ['pa-audit'] }); }
    catch (error) { setSafePathError(error instanceof ApiResponseError && error.status === 401 ? 'Your session has expired. Sign in again to load this plan.' : error instanceof ApiResponseError && error.status === 403 ? 'You do not have access to this case plan.' : error instanceof ApiResponseError && error.status === 404 ? 'Case not found.' : error instanceof Error ? error.message : 'Fastest Safe Path unavailable.'); }
    finally { setSafePathLoading(false); }
  };
  useEffect(() => { if (id && isPaApiConfigured) void refreshSafePath(); }, [id]);
  useEffect(() => {
    if (!id || !isPaApiConfigured) return;
    let cancelled = false;
    void paApi.getEvidenceTrail(id).then((trail) => { if (!cancelled) { setEvidenceTrail(trail); setEvidenceTrailError(''); } })
      .catch((error) => { if (!cancelled) setEvidenceTrailError(error instanceof Error ? error.message : 'Evidence trail unavailable.'); });
    return () => { cancelled = true; };
  }, [id]);
  useEffect(() => {
    if (!id || aiEnhancement.status !== 'GENERATING') return;
    let cancelled = false;
    const poll = async () => {
      try {
        const result = await paApi.getCaseExplanationStatus(id);
        if (!cancelled) setAiEnhancement(result);
      } catch (error) {
        if (!cancelled) setAiEnhancement({ status: 'UNAVAILABLE', detail: error instanceof Error ? error.message : 'Enhancement status unavailable.' });
      }
    };
    const timer = window.setInterval(() => { void poll(); }, 1500);
    void poll();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [id, aiEnhancement.status]);
  const requestAiEnhancement = async () => {
    if (!item) return;
    setAiBusy(true);
    try { setAiEnhancement(await paApi.requestCaseExplanation(item.id)); }
    catch (error) { setAiEnhancement({ status: 'UNAVAILABLE', detail: error instanceof Error ? error.message : 'AI enhancement unavailable.' }); }
    finally { setAiBusy(false); }
  };
  const submitEvidenceReview = async () => {
    if (!id || !reviewCriterion || !reviewNotes.trim()) return;
    setReviewBusy(true); setEvidenceTrailError('');
    try {
      await paApi.reviewEvidenceTrail(id, reviewCriterion, { document_id: reviewDocumentId ? Number(reviewDocumentId) : null, assessment_status: reviewAssessment, review_status: reviewStatus, supporting_excerpt: reviewExcerpt, source_locator: reviewLocator, review_notes: reviewNotes });
      setEvidenceTrail(await paApi.getEvidenceTrail(id)); setReviewCriterion(''); setReviewExcerpt(''); setReviewLocator(''); setReviewNotes('');
      await refreshSafePath();
    } catch (error) { setEvidenceTrailError(error instanceof Error ? error.message : 'Unable to save human evidence review.'); }
    finally { setReviewBusy(false); }
  };
  const submitEscalation = async () => {
    if (!item || !escalationExplanation.trim()) return;
    setEscalationBusy(true); setEscalationError('');
    try { await escalate(item.id, escalationReason, escalationExplanation); setEscalationOpen(false); setEscalationExplanation(''); }
    catch (error) { setEscalationError(error instanceof Error ? error.message : 'Unable to route escalation'); }
    finally { setEscalationBusy(false); }
  };

  if (!item && isLoading) return <p role="status">Loading case details…</p>;
  if (!item) return <p>Case not found.</p>;

  const docs = documents.filter((doc: Document) => doc.caseId === item.id);
  const canRetryAiWorkflow = workflowResult
    ? [
      workflowResult.stages.evidenceAgent,
      workflowResult.stages.evidencePrediction,
      workflowResult.stages.slaAgent,
      workflowResult.stages.slaRiskPrediction,
      workflowResult.stages.escalationAgent,
      workflowResult.stages.recoveryAgent,
      workflowResult.stages.llmExplanation,
    ].some((stage) => stage.status === 'failed' || (stage.status === 'skipped' && stage.retryable))
    : false;
  const workflowStageCards = workflowResult ? [
    { label: 'Evidence Agent', ...workflowResult.stages.evidenceAgent },
    { label: 'Evidence prediction (experimental)', ...workflowResult.stages.evidencePrediction },
    { label: 'SLA Agent', ...workflowResult.stages.slaAgent },
    { label: 'SLA risk model', ...workflowResult.stages.slaRiskPrediction },
    { label: 'Escalation Agent', ...workflowResult.stages.escalationAgent },
    { label: 'Recovery Agent', ...workflowResult.stages.recoveryAgent },
    { label: 'Case summary', ...workflowResult.stages.llmExplanation },
    { label: 'Case summary', ...workflowResult.stages.summary },
  ] : [];
  const updateProgress = (step: WorkflowStepId, progress: WorkflowProgress) => {
    setWorkflowProgress((current) => ({ ...current, [step]: progress }));
  };

  const handleQueueFile = (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = '';
    if (!selectedFiles.length) return;
    const validExtensions = ['.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff'];
    const validMimeTypes = ['application/pdf', 'image/png', 'image/jpeg', 'image/tiff'];
    const validFiles: File[] = [];
    const errors: string[] = [];
    for (const file of selectedFiles) {
      const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
      if (!validExtensions.includes(extension) || (file.type && !validMimeTypes.includes(file.type))) {
        errors.push(`${file.name}: select a PDF, PNG, JPEG, or TIFF document.`);
      } else if (file.size === 0 || file.size > 10 * 1024 * 1024) {
        errors.push(`${file.name}: documents must be larger than 0 bytes and no larger than 10 MB.`);
      } else {
        validFiles.push(file);
      }
    }
    if (validFiles.length) {
      setPendingDocuments((current) => {
        const newFiles = validFiles.filter((file) => !current.some((pending) => pending.file.name === file.name && pending.file.size === file.size && pending.file.lastModified === file.lastModified));
        return [...current, ...newFiles.map((file) => ({ id: crypto.randomUUID(), file, category, submissionId: crypto.randomUUID() }))];
      });
    }
    setFeedback(errors.length ? errors.join(' ') : null);
  };

  const handleSubmitDocuments = async () => {
    if (!pendingDocuments.length) return;
    if (!isPaApiConfigured) {
      setFeedback('Connect to PA Guard to upload documents and review this case.');
      return;
    }
    setIsSubmitting(true);
    setFeedback(null);
    setWorkflowResult(null);
    setAiEnhancement({ status: 'NOT_REQUESTED' });
    setFinding(null);
    setWorkflowProgress(createInitialProgress());
    updateProgress('validation', { status: 'completed', detail: `${pendingDocuments.length} queued document(s) validated.` });
    updateProgress('upload', { status: 'running', detail: `Uploading ${pendingDocuments.length} file${pendingDocuments.length === 1 ? '' : 's'}…` });
    updateProgress('ocr', { status: 'running', detail: 'PA Guard is reading the uploaded documents and saving the extracted text.' });
    let savedCount = 0;
    let uploadFailures = 0;
    let ocrFailures = 0;
    try {
      const result = await uploadDocuments(item.id, pendingDocuments.map(({ file, category: queuedCategory, submissionId }) => ({
        file,
        category: queuedCategory,
        submissionId,
      })));
      savedCount = result.documents.length;
      ocrFailures = result.documents.filter((document) => document.ocrStatus === 'FAILED').length;
      uploadFailures = result.failures.length;
      const savedIds = new Set(result.savedSubmissionIds);
      const failuresById = new Map(result.failures.map((failure) => [failure.submission_id, formatDiagnosticForUser(failure.message, 'Upload failed. Check that the file is supported and try again.')]));
      setPendingDocuments((current) => current
        .filter((entry) => !savedIds.has(entry.submissionId))
        .map((entry) => ({ ...entry, error: failuresById.get(entry.submissionId) })));
      setFeedback(result.failures.length
        ? result.failures.map((failure) => `${failure.filename}: ${formatDiagnosticForUser(failure.message, 'Upload failed. Check that the file is supported and try again.')}`).join(' ')
        : null);
    } catch (error) {
      uploadFailures = pendingDocuments.length;
      const message = error instanceof ApiResponseError
        ? error.message
        : error instanceof Error ? error.message : 'Batch upload failed.';
      setPendingDocuments((current) => current.map((entry) => ({ ...entry, error: message })));
      setFeedback(message);
    }
    updateProgress('upload', {
      status: uploadFailures ? 'failed' : 'completed',
      detail: `${savedCount} file${savedCount === 1 ? '' : 's'} uploaded. ${uploadFailures} could not be uploaded; you can retry them.`,
    });
    updateProgress('ocr', {
      status: savedCount === 0 ? 'skipped' : ocrFailures ? 'failed' : 'completed',
      detail: savedCount === 0
        ? 'No files were saved, so text extraction did not run.'
        : `${savedCount - ocrFailures} of ${savedCount} saved files had text extracted. ${ocrFailures} could not be read. ${uploadFailures} failed uploads were not processed.`,
    });
    if (savedCount === 0) {
      updateProgress('agents', { status: 'skipped', detail: 'No document was saved, so the AI workflow was not started.' });
      updateProgress('llm', { status: 'skipped' });
      updateProgress('summary', { status: 'skipped' });
      updateProgress('complete', { status: 'failed', detail: 'No documents were submitted successfully. Retained items can be retried.' });
      setFeedback('No documents were saved. The queued items are still available to retry.');
      setIsSubmitting(false);
      return;
    }
    updateProgress('agents', { status: 'running', detail: 'Checking documents, deadlines, escalation, and recovery.' });
      updateProgress('llm', { status: 'running', detail: 'Preparing the case summary.' });
    updateProgress('summary', { status: 'running' });
    try {
      const result = await paApi.runCaseWorkflow(item.id);
      setWorkflowResult(result);
      setAiEnhancement({ status: 'NOT_REQUESTED' });
      setFinding(result.stages.evidenceAgent.result ?? null);
      const agentStages = [
        result.stages.evidenceAgent,
        result.stages.evidencePrediction,
        result.stages.slaAgent,
        result.stages.slaRiskPrediction,
        result.stages.escalationAgent,
        result.stages.recoveryAgent,
      ];
      const failedAgentStages = agentStages.filter((stage) => stage.status === 'failed').length;
      const skippedAgentStages = agentStages.filter((stage) => stage.status === 'skipped').length;
      const runningAgentStatus: WorkflowStepStatus = failedAgentStages
        ? 'failed'
        : skippedAgentStages ? 'partial' : 'completed';
      updateProgress('agents', {
        status: runningAgentStatus,
        detail: failedAgentStages
          ? `${failedAgentStages} agent stage(s) failed; see individual status and error below.`
          : skippedAgentStages
            ? `${skippedAgentStages} agent stage(s) were skipped; review their reasons and correct required case data.`
            : 'Agent stages completed.',
      });
      updateProgress('llm', {
        status: result.stages.llmExplanation.status,
        detail: result.stages.llmExplanation.status === 'failed' ? 'The AI explanation could not be generated. The recorded case summary is still available.' : 'A case summary is ready.',
      });
      updateProgress('summary', { status: result.stages.summary.status, detail: 'Case summary returned from the backend.' });
      updateProgress('complete', {
        status: result.workflowStatus === 'completed' ? 'completed' : 'partial',
        detail: result.workflowStatus === 'completed' ? 'Submission and AI workflow finished.' : 'Workflow finished with items requiring review.',
      });
      setFeedback(`${savedCount} document${savedCount === 1 ? '' : 's'} saved. ${ocrFailures} could not be read and ${uploadFailures} could not be uploaded.${ocrFailures || uploadFailures ? ' Review the affected files and try again.' : ' All saved documents are ready for review.'}`);
    } catch (error) {
      const message = formatDiagnosticForUser(error instanceof Error ? error.message : undefined, 'The case review could not finish. Check the connection and try again.');
      updateProgress('agents', { status: 'failed', detail: message });
      updateProgress('llm', { status: 'skipped', detail: 'The workflow endpoint returned no LLM result.' });
      updateProgress('summary', { status: 'skipped', detail: 'The workflow endpoint returned no final summary.' });
      updateProgress('complete', { status: 'failed', detail: 'Saved documents were not uploaded again; retry the AI workflow when ready.' });
      setFeedback(`Documents were saved, but the case review could not finish. ${message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleRetryAiWorkflow = async () => {
    setIsSubmitting(true);
    setFeedback(null);
    try {
      const result = await paApi.retryFailedCaseWorkflowStages(item.id);
      setWorkflowResult(result);
      setAiEnhancement({ status: 'NOT_REQUESTED' });
      setFinding(result.stages.evidenceAgent.result ?? null);
      const agentStages = [
        result.stages.evidenceAgent,
        result.stages.evidencePrediction,
        result.stages.slaAgent,
        result.stages.slaRiskPrediction,
        result.stages.escalationAgent,
        result.stages.recoveryAgent,
      ];
      const failedAgentStages = agentStages.filter((stage) => stage.status === 'failed').length;
      setWorkflowProgress((current) => ({
        ...current,
        agents: { status: failedAgentStages ? 'failed' : 'completed', detail: `${failedAgentStages} agent stage(s) failed.` },
        llm: { status: result.stages.llmExplanation.status, detail: result.stages.llmExplanation.status === 'failed' ? 'The AI explanation could not be generated. The case summary remains available.' : 'Case summary is ready.' },
        summary: { status: result.stages.summary.status, detail: 'Case summary returned from the backend.' },
        complete: { status: result.workflowStatus === 'completed' ? 'completed' : 'partial', detail: result.workflowStatus === 'completed' ? 'Workflow finished.' : 'Workflow finished with review items.' },
      }));
      setFeedback(failedAgentStages ? `${failedAgentStages} review step${failedAgentStages === 1 ? '' : 's'} still need attention. Check the results below and try again.` : 'The case review was updated using the documents already saved.');
    } catch (error) {
      setFeedback(formatDiagnosticForUser(error instanceof Error ? error.message : undefined, 'The case review could not be retried. Check the connection and try again.'));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleRetry = async (documentId: number) => {
    try {
      setFeedback(null);
      await retryDocumentProcessing(documentId);
      setFinding(null);
      setFeedback('Document text extraction was retried. Run the case review again to update the document findings.');
    } catch (error) {
      setFeedback(formatDiagnosticForUser(error instanceof Error ? error.message : undefined, 'Text extraction could not be retried. Check the connection and try again.'));
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold">{item.id}</h1>
            <Badge>{item.risk}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">{item.patientReference} · {item.payer} · {item.service}</p>
        </div>
        <div className="flex gap-2">
          <Button asChild variant="outline"><Link to={`/cases/${encodeURIComponent(item.id)}/submission-review`}><FileText className="mr-2 size-4" />Review Submission</Link></Button>
          {(user?.role === 'SPECIALIST' || user?.role === 'SUPERVISOR') && <Button variant="outline" onClick={() => setEscalationOpen(true)}>Escalate</Button>}
          {user?.role === 'MANAGER' && <Button onClick={() => updateCase(item.id, coordinator, item.coordinator)}>Save coordinator</Button>}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><UploadCloud className="size-4" />Stage documents for {item.id}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            <input ref={fileInputRef} type="file" accept={supportedFileTypes} multiple onChange={handleQueueFile} className="sr-only" tabIndex={-1} aria-hidden="true" />
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" onClick={() => fileInputRef.current?.click()} disabled={isSubmitting}>
                <UploadCloud className="mr-2 size-4" />Add documents
              </Button>
              <Select value={category} onValueChange={setCategory}>
                <SelectTrigger className="w-full sm:w-64" aria-label="Document category">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {documentCategories.map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}
                </SelectContent>
              </Select>
              <span className="text-sm text-muted-foreground">{pendingDocuments.length} queued · Case {item.id}</span>
            </div>
            {pendingDocuments.length > 0 ? (
              <div className="space-y-2">
                {pendingDocuments.map((queued) => (
                  <div key={queued.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{queued.file.name}</p>
                      <p className="text-xs text-muted-foreground">{queued.category} · {queued.file.type || 'Unknown file type'} · {(queued.file.size / (1024 * 1024)).toFixed(2)} MB</p>
                      {queued.error && <p className="mt-1 text-xs text-destructive">Upload failed; this file will be retried: {queued.error}</p>}
                    </div>
                    <Button variant="ghost" size="icon" aria-label={`Remove ${queued.file.name}`} onClick={() => setPendingDocuments((current) => current.filter((entry) => entry.id !== queued.id))} disabled={isSubmitting}>
                      <X className="size-4" />
                    </Button>
                  </div>
                ))}
                <p className="text-xs text-muted-foreground">
                  Categories queued: {[...new Set(pendingDocuments.map((queued) => queued.category))].join(', ')}.
                </p>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No documents queued. Adding files only stages them; nothing is uploaded or analyzed until final submission.</p>
            )}
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={handleSubmitDocuments} disabled={!pendingDocuments.length || isSubmitting || !isPaApiConfigured}>
                {isSubmitting && <LoaderCircle className="mr-2 size-4 animate-spin" />}
                {isSubmitting ? 'Submitting and running workflow…' : 'Submit queued documents & run workflow'}
              </Button>
              {!isPaApiConfigured && <p className="text-sm text-muted-foreground">Connect to PA Guard to upload documents.</p>}
              {canRetryAiWorkflow && !isSubmitting && (
                <Button variant="outline" onClick={handleRetryAiWorkflow} disabled={Boolean(pendingDocuments.length)}>
                  <RefreshCw className="mr-2 size-4" />Retry failed AI stages
                </Button>
              )}
            </div>
            <div className="space-y-2 rounded-md border bg-muted/20 p-3" aria-live="polite">
              <p className="text-sm font-medium">Submission progress · {pendingDocuments.length ? `${pendingDocuments.length} document(s) waiting` : 'No queued documents'}</p>
              {workflowStepLabels.map((step) => {
                const progress = workflowProgress[step.id];
                return (
                  <div key={step.id} className="flex items-start gap-2 text-sm">
                    {progress.status === 'running'
                      ? <LoaderCircle className="mt-0.5 size-4 animate-spin text-primary" />
                      : progress.status === 'completed'
                        ? <CheckCircle2 className="mt-0.5 size-4 text-green-600" />
                        : progress.status === 'partial'
                          ? <AlertTriangle className="mt-0.5 size-4 text-amber-600" />
                        : <span className="mt-0.5 size-4 rounded-full border" aria-hidden="true" />}
                    <div>
                      <span className="font-medium">{step.label}</span>
                      <Badge className="ml-2" variant={progress.status === 'failed' ? 'destructive' : progress.status === 'completed' ? 'secondary' : 'outline'}>{progress.status === 'pending' && pendingDocuments.length ? 'waiting' : progress.status}</Badge>
                      {progress.detail && <p className="text-xs text-muted-foreground">{progress.detail}</p>}
                    </div>
                  </div>
                );
              })}
            </div>
            {feedback && <p role="status" aria-live="polite" className="text-sm text-muted-foreground">{feedback}</p>}
            <p className="text-xs text-muted-foreground">Supported: PDF, PNG, JPG/JPEG, TIFF. Maximum file size: 10 MB per document. Each file retains its category and retry identifier until saved.</p>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader><CardTitle>Case information</CardTitle></CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <div>
              <p className="text-sm text-muted-foreground">Workflow status</p>
              <p className="rounded-md border bg-muted/30 px-3 py-2 text-sm">{item.status} · change status through the Approval Review workflow</p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Case coordinator metadata</p>
              <Select value={coordinator} onValueChange={setCoordinator}>
                <SelectTrigger aria-label="Case coordinator metadata"><SelectValue /></SelectTrigger>
                <SelectContent>{[...new Set([item.coordinator, 'Maya Chen', 'Jordan Lee', 'Avery Patel'].filter(Boolean))].map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <Info label="Submission date" value={item.submissionDate} />
            <Info label="Deadline" value={item.deadline} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Bot className="size-4" />Case AI workflow</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Badge variant="outline">Runs after final document submission</Badge>
            <p className="text-sm">The case review checks saved documents, deadlines, escalation, and recovery. Results are recommendations for authorized staff.</p>
            <p className="text-sm font-medium">AI results are recommendations only. Authorized staff retain all clinical and approval decisions.</p>
          </CardContent>
        </Card>
      </div>

      {workflowResult && (
        <Card>
          <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
            <CardTitle>AI workflow results · {workflowResult.caseId}</CardTitle>
            <Badge variant={workflowResult.workflowStatus === 'completed' ? 'secondary' : 'destructive'}>{workflowResult.workflowStatus === 'completed' ? 'Completed' : 'Partial · human review required'}</Badge>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              <section aria-labelledby="sla-assessment-title" className="rounded-lg border p-4">
                <div className="flex flex-wrap items-center gap-2"><h3 id="sla-assessment-title" className="font-semibold">SLA risk · experimental ML</h3><Badge variant="outline">Synthetic model</Badge></div>
                <p className="mt-1 text-sm">{workflowResult.stages.slaRiskPrediction.result?.available
                  ? `${workflowResult.stages.slaRiskPrediction.result.riskLevel} · ${workflowResult.stages.slaRiskPrediction.result.riskScore ?? '—'}% estimated breach probability`
                  : 'Prediction unavailable; use the operational deadline assessment.'}</p>
                <p className="mt-2 text-xs text-muted-foreground">{workflowResult.stages.slaRiskPrediction.result?.decisionScope} {workflowResult.stages.slaRiskPrediction.result?.reliabilityNote}</p>
                <p className="mt-1 text-xs text-muted-foreground">Model {workflowResult.stages.slaRiskPrediction.result?.modelVersion ?? 'unavailable'} · prediction time {workflowResult.stages.slaRiskPrediction.result?.predictionTimestamp ?? 'unavailable'}</p>
              </section>
              <section aria-labelledby="operational-assessment-title" className="rounded-lg border p-4">
                <h3 id="operational-assessment-title" className="font-semibold">Operational risk · configured rules</h3>
                <p className="mt-1 text-sm">{workflowResult.stages.slaRiskPrediction.result?.operationalRisk?.breached ? 'Confirmed SLA breach' : workflowResult.stages.slaRiskPrediction.result?.operationalRisk?.category ?? 'Unavailable'} · {workflowResult.stages.slaRiskPrediction.result?.operationalRisk?.remainingHours ?? '—'} hours to deadline</p>
                <p className="mt-2 text-xs text-muted-foreground">{workflowResult.stages.slaRiskPrediction.result?.operationalRisk?.reasons.join(' ') ?? 'Deterministic rule result unavailable.'}</p>
              </section>
              <section aria-labelledby="verification-state-title" className="rounded-lg border p-4">
                <h3 id="verification-state-title" className="font-semibold">Evidence verification</h3>
                <p className="mt-1 text-sm">{workflowResult.stages.evidenceAgent.result?.verificationStatus?.replaceAll('_', ' ') ?? 'NEEDS REVIEW'}</p>
                <p className="mt-2 text-xs text-muted-foreground">ML evidence prediction and OCR text matching do not constitute human verification.</p>
              </section>
              <section aria-labelledby="escalation-recommendation-title" className="rounded-lg border p-4">
                <h3 id="escalation-recommendation-title" className="font-semibold">Formal escalation</h3>
                <p className="mt-1 text-sm">{workflowResult.summary.escalation?.escalationRequired ? `Threshold reached · ${workflowResult.summary.escalation.escalationLevel.replaceAll('_', ' ')}` : 'No configured threshold reached'}</p>
                <p className="mt-2 text-xs text-muted-foreground">Elevated ML risk may recommend priority human review; it does not trigger escalation or authorize action.</p>
              </section>
              <section aria-labelledby="responsible-staff-title" className="rounded-lg border p-4">
                <h3 id="responsible-staff-title" className="font-semibold">Responsible staff</h3>
                {workflowResult.summary.activeEscalation ? <p className="mt-1 text-sm">{workflowResult.summary.activeEscalation.status === 'ACCEPTED' ? 'Accepted by' : 'Pending acceptance by'} {workflowResult.summary.activeEscalation.assignee?.name ?? 'Receiving assignee unavailable'} · {workflowResult.summary.activeEscalation.assignee?.role ?? 'Role unavailable'}</p>
                  : workflowResult.summary.responsibleStaff ? <p className="mt-1 text-sm">{workflowResult.summary.responsibleStaff.name} · {workflowResult.summary.responsibleStaff.role}</p>
                    : <p className="mt-1 text-sm">Unassigned — Manager allocation required</p>}
                <p className="mt-2 text-xs text-muted-foreground">Resolved from active assignment and escalation records. Case coordinator metadata is not used as an assignee.</p>
              </section>
              <section aria-labelledby="next-action-title" className="rounded-lg border bg-muted/30 p-4">
                <h3 id="next-action-title" className="font-semibold">Next recommended action</h3>
                <p className="mt-1 text-sm">{workflowResult.summary.recommendedNextAction}</p>
                <Badge className="mt-2" variant="outline">Recommendation only · not completed</Badge>
              </section>
            </div>
            <section aria-labelledby="unverified-documents-title" className="rounded-lg border p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 id="unverified-documents-title" className="font-semibold">Document review queue</h3>
                <Badge variant={workflowResult.summary.unverifiedDocumentCount ? 'outline' : 'secondary'}>{workflowResult.summary.unverifiedDocumentCount} unverified</Badge>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">This count uses the saved document review statuses. {workflowResult.summary.evidenceReferenceCount} distinct document/page source(s) appear in the extracted evidence summary. A source may be cited for multiple requirements, and not every upload produced a match. Text extraction results do not verify evidence.</p>
              {workflowResult.summary.unverifiedDocuments.length > 0 ? <ul className="mt-3 grid gap-2 sm:grid-cols-2">{workflowResult.summary.unverifiedDocuments.map((document) => <li key={document.documentId} className="rounded-md bg-muted/40 p-3 text-sm"><span className="font-medium">{document.filename}</span><p className="text-xs text-muted-foreground">{document.category || 'Uncategorized'} · Review status: {formatStatusLabel(document.verificationStatus)} · Text extraction: {formatStatusLabel(document.ocrStatus)}{document.reviewer ? ` · Reviewed by ${document.reviewer}` : ' · No reviewer recorded'}</p></li>)}</ul> : <p className="mt-2 text-sm text-muted-foreground">No unverified documents are recorded.</p>}
            </section>
            <div className="grid gap-2 sm:grid-cols-2">
              {workflowStageCards.map((stage) => {
                return (
                  <div key={stage.label} className="rounded-md border p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-sm font-medium">{stage.label}</span>
                      <Badge variant={stage.status === 'failed' ? 'destructive' : stage.status === 'completed' ? 'secondary' : 'outline'}>{stage.status}</Badge>
                    </div>
                    {(stage.error || stage.reason) && <p className="mt-1 text-xs text-muted-foreground">{formatDiagnosticForUser(stage.error ?? stage.reason, 'This review step could not finish. Check the case information and try again.')}</p>}
                  </div>
                );
              })}
            </div>
            {workflowResult.llmExplanation && (
              <div className="rounded-md border border-primary/30 bg-primary/5 p-3">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Case summary from recorded information</p>
                  <Badge variant="outline">Available immediately</Badge>
                </div>
                <p className="whitespace-pre-wrap text-sm">{workflowResult.llmExplanation.explanation}</p>
                <p className="mt-2 text-xs text-muted-foreground">AI can help explain these results in plain language. It cannot verify clinical evidence or change case decisions.</p>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="outline" onClick={() => void requestAiEnhancement()} disabled={aiBusy || aiEnhancement.status === 'GENERATING'}>
                    {(aiBusy || aiEnhancement.status === 'GENERATING') && <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />}
                    {aiEnhancement.status === 'UNAVAILABLE' ? 'Retry AI enhancement' : 'Generate AI-enhanced explanation'}
                  </Button>
                  <Badge variant={aiEnhancement.status === 'READY' ? 'secondary' : aiEnhancement.status === 'UNAVAILABLE' || aiEnhancement.status === 'STALE' ? 'destructive' : 'outline'}>
                    {aiEnhancement.status === 'NOT_REQUESTED' ? 'AI explanation not requested' : aiEnhancement.status === 'READY' ? 'AI explanation ready' : aiEnhancement.status === 'GENERATING' ? 'Writing explanation…' : aiEnhancement.status === 'STALE' ? 'Case information has changed' : 'AI explanation unavailable'}
                  </Badge>
                </div>
                {aiEnhancement.status === 'UNAVAILABLE' && <p role="status" className="mt-2 text-xs text-muted-foreground">The AI explanation could not be generated. The case summary is still available. Check that the local AI service is running, then try again.</p>}
                {aiEnhancement.status === 'STALE' && <p role="status" className="mt-2 text-xs text-muted-foreground">This explanation is based on older case information. Run the case review again before requesting a new explanation.</p>}
                {aiEnhancement.status === 'READY' && <p className="mt-3 whitespace-pre-wrap rounded-md border bg-background p-3 text-sm">{aiEnhancement.explanation}</p>}
              </div>
            )}
            <div className="space-y-2 rounded-md border p-3">
              <h3 className="text-sm font-semibold">Documents and extraction</h3>
              {workflowResult.summary.documents.length === 0 && <p className="text-sm text-muted-foreground">No persisted documents are available for this case.</p>}
              {workflowResult.summary.documents.map((document) => (
                <div key={document.documentId} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                  <span>{document.filename} · {document.category || 'Uncategorized'}</span>
                  <span className="text-muted-foreground">OCR: {document.ocrStatus} · extracted text {document.extractedTextAvailable ? 'persisted' : 'unavailable'}</span>
                </div>
              ))}
            </div>
            {workflowResult.stages.slaRiskPrediction.result && (
              <div className="space-y-2 rounded-md border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold">Existing SLA risk estimate</h3>
                  <Badge variant={workflowResult.stages.slaRiskPrediction.result.riskLevel === 'HIGH' || workflowResult.stages.slaRiskPrediction.result.riskLevel === 'NEEDS_REVIEW' ? 'destructive' : 'outline'}>
                    {workflowResult.stages.slaRiskPrediction.result.available ? `${workflowResult.stages.slaRiskPrediction.result.riskLevel} · ${workflowResult.stages.slaRiskPrediction.result.riskScore}% estimated` : 'Prospective ML unavailable'}
                  </Badge>
                  <Badge variant="outline">{workflowResult.stages.slaRiskPrediction.result.reliable ? 'Validated' : 'Unvalidated'}</Badge>
                  <Badge variant="outline">Synthetic model</Badge>
                </div>
                <p className="text-xs text-muted-foreground">{workflowResult.stages.slaRiskPrediction.result.scoreMeaning} {workflowResult.stages.slaRiskPrediction.result.reliabilityNote}</p>
                <ul className="list-disc space-y-1 pl-5 text-sm">
                  {workflowResult.stages.slaRiskPrediction.result.factors.map((factor) => <li key={factor.factor}>{factor.description}</li>)}
                </ul>
                {workflowResult.stages.slaRiskPrediction.result.operationalRisk && (
                  <div className="space-y-1 border-t pt-2 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">Current operational status</span>
                      <Badge variant={workflowResult.stages.slaRiskPrediction.result.operationalRisk.breached || workflowResult.stages.slaRiskPrediction.result.operationalRisk.escalationRequired ? 'destructive' : 'outline'}>
                        {workflowResult.stages.slaRiskPrediction.result.operationalRisk.category}
                      </Badge>
                      <span className="text-muted-foreground">{workflowResult.stages.slaRiskPrediction.result.operationalRisk.remainingHours}h to deadline</span>
                      {workflowResult.stages.slaRiskPrediction.result.operationalRisk.escalationRequired && <span className="font-medium">Escalation required</span>}
                    </div>
                    <p>{workflowResult.stages.slaRiskPrediction.result.operationalRisk.recommendedAction}</p>
                    <p className="text-xs text-muted-foreground">Rule {workflowResult.stages.slaRiskPrediction.result.operationalRisk.ruleVersion}; uses actual deadline and case workflow state.</p>
                    <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
                      {workflowResult.stages.slaRiskPrediction.result.operationalRisk.reasons.map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)}
                    </ul>
                  </div>
                )}
              </div>
            )}
            {workflowResult.stages.evidencePrediction.result && (
              <div className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold">Experimental evidence-gap prediction</h3>
                  <Badge variant="outline">Experimental — not used for decisions</Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  {workflowResult.stages.evidencePrediction.result.message} Training data: {workflowResult.stages.evidencePrediction.result.trainingDataSource}. Prediction does not establish clinical presence or absence.
                </p>
                {workflowResult.stages.evidencePrediction.result.predictions.map((prediction) => (
                  <div key={prediction.requiredDocument} className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">{prediction.requiredDocument}</span>
                    <span>Model: {prediction.mlPrediction.replaceAll('_', ' ')}</span>
                    <span className="text-muted-foreground">Evidence review: {prediction.verificationStatus.replaceAll('_', ' ')}</span>
                  </div>
                ))}
              </div>
            )}
            {workflowResult.summary.evidenceItems.length > 0 && (
              <div className="space-y-2 rounded-md border p-3">
                <h3 className="text-sm font-semibold">Evidence summary · human verification required</h3>
                {workflowResult.summary.evidenceItems.map((evidence) => (
                  <div key={evidence.requirement} className="text-sm">
                    <p><strong>{evidence.requirement}:</strong> {evidence.status.replaceAll('_', ' ')}{evidence.humanReviewRequired ? ' · review required' : ''}</p>
                    <p className="text-xs text-muted-foreground">{evidence.explanation}</p>
                    {evidence.supportingDocuments.map((source, index) => (
                      <p key={`${source.filename}-${source.pageNumber}-${index}`} className="text-xs text-muted-foreground">
                        {source.filename} · {source.category || 'Uncategorized'}{source.pageNumber ? ` · page ${source.pageNumber}` : ''}{source.excerpt ? ` · “${source.excerpt}”` : ''}
                      </p>
                    ))}
                  </div>
                ))}
              </div>
            )}
            {workflowResult.summary.escalation && (
              <div className="space-y-1 rounded-md border p-3">
                <h3 className="text-sm font-semibold">Escalation recommendation · {workflowResult.summary.escalation.escalationLevel.replaceAll('_', ' ')}</h3>
                <p className="text-sm">{workflowResult.summary.escalation.reason}</p>
                <p className="text-sm"><strong>Receiving role:</strong> {workflowResult.summary.escalation.escalationLevel === 'NONE' ? 'None — no formal threshold reached' : workflowResult.summary.escalation.responsibleRole || 'Human policy review required'}</p>
                <p className="text-sm"><strong>Recommended action:</strong> {workflowResult.summary.escalation.recommendedAction}</p>
                <p className="text-xs text-muted-foreground">Notification: {workflowResult.summary.escalation.notificationStatus.replaceAll('_', ' ')} · approval required: {workflowResult.summary.escalation.approvalRequired ? 'yes' : 'no'}</p>
              </div>
            )}
            {workflowResult.stages.recoveryAgent.result && (
              <div className="space-y-1 rounded-md border p-3">
                <h3 className="text-sm font-semibold">Recovery recommendation · preview only</h3>
                <p className="text-sm">{workflowResult.stages.recoveryAgent.result.narrative ?? workflowResult.stages.recoveryAgent.result.recommendedRecovery}</p>
                <p className="text-xs text-muted-foreground">{workflowResult.stages.recoveryAgent.result.likelyCause} · human approval required: {workflowResult.stages.recoveryAgent.result.humanApprovalRequired ? 'yes' : 'no'}</p>
              </div>
            )}
            <div className="rounded-md bg-muted/40 p-3">
              <p className="text-sm"><strong>Recommended next action:</strong> {workflowResult.summary.recommendedNextAction}</p>
              {workflowResult.summary.humanReviewItems.length > 0 && (
                <p className="mt-1 text-xs text-muted-foreground"><strong>Human review queue:</strong> {workflowResult.summary.humanReviewItems.join('; ')}</p>
              )}
            </div>
            <p className="text-xs text-muted-foreground">SLA estimates, evidence matches, and escalation or recovery recommendations do not change authorization status or send notifications. Authorized staff must review and approve any action.</p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader><CardTitle>Previously submitted documents</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {docs.length === 0 && <p className="text-sm text-muted-foreground">No uploaded evidence yet.</p>}
          {docs.map((doc: Document) => (
            <div key={doc.id} className="rounded-md border p-3 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2">
                    <FileText className="size-4 text-muted-foreground" />
                    <span className="font-medium">{doc.originalFilename ?? doc.name}</span>
                  </div>
                  <p className="text-xs text-muted-foreground">{doc.category ?? doc.name} · {doc.uploadedAt ? new Date(doc.uploadedAt).toLocaleString() : 'No upload date'}</p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={doc.status === 'MISSING' || doc.status === 'REVIEW_REQUIRED' || doc.status === 'OCR_FAILED' ? 'destructive' : doc.status === 'VERIFIED' || doc.status === 'OCR_COMPLETED' ? 'secondary' : 'outline'}>{formatStatusLabel(doc.processingStatus ?? doc.status)}</Badge>
                  {doc.ocrStatus && <Badge variant="outline">Text extraction: {formatStatusLabel(doc.ocrStatus)}</Badge>}
                </div>
              </div>

              {doc.evidenceSummary && (
                <p className="text-sm text-muted-foreground">{doc.evidenceSummary}</p>
              )}

              {doc.processingError && (
                <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
                  {doc.processingError}
                </div>
              )}

              {doc.extractedText && (
                <details className="rounded-md border bg-muted/30 p-2 text-sm">
                  <summary className="cursor-pointer font-medium">View extracted text</summary>
                  <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap text-xs">{doc.extractedText}</pre>
                </details>
              )}

              {(doc.processingStatus === 'OCR_FAILED' || doc.ocrStatus === 'FAILED') && (
                <Button variant="outline" size="sm" onClick={() => handleRetry(doc.id)}>
                  <RefreshCw className="mr-2 size-4" />Retry processing
                </Button>
              )}
            </div>
          ))}
          {finding && (
            <div className="space-y-3 rounded-md border border-primary/30 bg-primary/5 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <strong className="text-sm">Evidence Agent finding</strong>
                <Badge variant="destructive">{finding.verificationStatus}</Badge>
                <Badge variant="outline">Human review required</Badge>
              </div>
              <div className="rounded-md bg-muted/50 p-3">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Agent rationale</p>
                  <Badge variant="outline">{finding.explanationEngine?.startsWith('ollama:') ? 'AI explanation' : 'Rule-based explanation'}</Badge>
                </div>
                <p className="text-sm">{finding.explanation}</p>
              </div>
              <p className="text-sm"><strong>Recommendation:</strong> {finding.recommendedAction}</p>
              <div className="rounded-md border p-3">
                <p className="text-sm font-medium">Case evidence summary · {finding.verificationStatus.replaceAll('_', ' ')}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {finding.policyMode === 'UNAVAILABLE' || finding.sourceReferences.length === 0
                    ? 'No applicable approved policy is available; requirements are unavailable and must not be inferred.'
                    : finding.policyMode === 'DEMO_ONLY'
                      ? 'Demo policy references only; they are not authoritative payer guidance. Every match requires human review.'
                      : `Based on ${finding.sourceReferences.length} verified public policy reference(s) and OCR text. Every match remains subject to human review.`}
                </p>
                {finding.sourceReferences.length > 0 && <div className="space-y-1 text-xs text-muted-foreground">{finding.sourceReferences.map((source) => <p key={`${source.policyId}-${source.version}`}>{source.policyId} v{source.version} · {source.policyUse === 'AUTHORITATIVE' ? 'verified public guidance' : 'demo only'} · {source.verificationStatus ?? 'MOCK_DEMO'} · {source.sourceVerifiedBy ? `source review by ${source.sourceVerifiedBy}` : 'source review not recorded'}{source.sourceUrl ? ` · ${source.sourceUrl}` : ''}</p>)}</div>}
                {finding.evidenceValidations.length > 0 && <div className="space-y-2 rounded-md border p-3"><p className="text-sm font-medium">Requirement validation (human review remains authoritative)</p>{finding.evidenceValidations.map((validation) => <div key={validation.requirement} className="flex flex-wrap items-start gap-2 border-b pb-2 last:border-0 last:pb-0"><Badge variant={validation.validationStatus === 'MISSING' || validation.validationStatus === 'CONFLICTING' ? 'destructive' : validation.validationStatus === 'SATISFIED' ? 'secondary' : 'outline'}>{validation.validationStatus}</Badge><div className="min-w-0 flex-1"><p className="text-sm font-medium">{validation.requirement}</p>{validation.policyRequiredCategories.length > 0 && <p className="text-xs text-muted-foreground">Policy categories: {validation.policyRequiredCategories.join(', ')}</p>}<p className="text-xs text-muted-foreground">{validation.reason}</p></div></div>)}</div>}
                {finding.requiredDocuments.length > 0 ? (
                  <ul className="mt-2 space-y-1 text-sm">
                    {finding.requiredDocuments.map((requiredDocument) => {
                      const exactMatches = finding.foundDocuments.filter((match) => match.requiredDocument === requiredDocument);
                      const possibleMatches = finding.possibleEvidenceMatches.filter((match) => match.requiredDocument === requiredDocument);
                      const statusLabel = exactMatches.length
                        ? 'Text found · human review required'
                        : possibleMatches.length
                          ? 'Possible text match · human review required'
                          : finding.missingDocuments.includes(requiredDocument)
                            ? 'Not found in successfully processed documents'
                            : finding.unreadableDocuments.length
                              ? 'Not verifiable while documents need OCR review'
                              : 'Not identified';
                      const supportingFiles = [...exactMatches, ...possibleMatches]
                        .map((match) => `${match.documentName} (${match.category || 'Uncategorized'})`);
                      return <li key={requiredDocument}><strong>{requiredDocument}:</strong> {statusLabel}{supportingFiles.length ? ` · ${supportingFiles.join(', ')}` : ''}</li>;
                    })}
                  </ul>
                ) : (
                  <p className="mt-2 text-sm">No applicable approved requirements are available; the case needs human policy review.</p>
                )}
                {finding.unreadableDocuments.length > 0 && (
                  <p className="mt-2 text-sm text-destructive">
                    Documents waiting for text extraction or that could not be read: {finding.unreadableDocuments.map((document) => `${document.documentName} (${document.category || 'Uncategorized'}, ${document.ocrStatus || 'status unknown'})`).join(', ')}.
                  </p>
                )}
              </div>
              {finding.foundDocuments.length > 0 && (
                <div>
                  <p className="text-sm font-medium">Required text found (not clinically verified)</p>
                  {finding.foundDocuments.map((match, index) => (
                    <p key={`${match.requiredDocument}-${match.documentName}-${match.pageNumber}-${index}`} className="text-xs text-muted-foreground">
                      {match.requiredDocument} · {match.documentName} · {match.category || 'Uncategorized'} · Text extraction: {formatStatusLabel(match.ocrStatus || 'unknown')}, page {match.pageNumber}: “{match.excerpt}”
                    </p>
                  ))}
                </div>
              )}
              {finding.possibleEvidenceMatches.length > 0 && (
                <div>
                  <p className="text-sm font-medium">Possible matches requiring human verification</p>
                  {finding.possibleEvidenceMatches.map((match, index) => (
                    <p key={`${match.requiredDocument}-${match.documentName}-${match.pageNumber}-${index}`} className="text-xs text-muted-foreground">
                      {match.requiredDocument} · {match.documentName} · {match.category || 'Uncategorized'} · Text extraction: {formatStatusLabel(match.ocrStatus || 'unknown')}, page {match.pageNumber}: “{match.excerpt}”
                    </p>
                  ))}
                </div>
              )}
              {finding.missingDocuments.length > 0 && (
                <p className="text-sm"><strong>Not found in successfully processed documents:</strong> {finding.missingDocuments.join(', ')}</p>
              )}
              {finding.unreadableDocuments.map((document) => (
                <p key={`${document.documentName}-${document.reason}`} className="text-sm text-destructive">
                  Text extraction needs review for {document.documentName}: {document.reason}
                </p>
              ))}
              {finding.possibleMismatches.map((mismatch) => (
                <p key={mismatch} className="text-sm text-destructive">{mismatch}</p>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Policy evidence trail</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {evidenceTrailError ? <p role="status" className="text-sm text-muted-foreground">Evidence trail unavailable: {evidenceTrailError}</p>
            : !evidenceTrail ? <p role="status" className="text-sm text-muted-foreground">Loading policy status…</p>
              : !evidenceTrail.available ? <p className="rounded-md border border-amber-500/40 bg-amber-50 p-3 text-sm text-amber-950">{evidenceTrail.message}</p>
                : <><p className="text-sm">{evidenceTrail.policy?.title} · version {evidenceTrail.policy?.version} · {evidenceTrail.policy?.verification_status}</p>
                  <p className="text-xs text-muted-foreground">Text extracted from documents is machine-generated. It does not confirm clinical evidence. A person must review it.</p>
                  <ul className="space-y-2">{evidenceTrail.criteria.map((criterion) => <li key={criterion.criterion_id} className="rounded border p-3 text-sm">
                    <p className="font-medium">{criterion.criterion_text}</p><p className="text-xs text-muted-foreground">Source status: {criterion.verification_status} · Evidence assessment: {criterion.assessment_status ?? 'NOT_ASSESSED'} · Human review: {criterion.review_status ?? 'PENDING'}</p>
                    {criterion.reviewer_id && <p className="text-xs text-muted-foreground">Reviewed by {criterion.reviewer_id} at {criterion.reviewed_at}: {criterion.review_notes}</p>}
                    {(user?.role === 'SUPERVISOR' || user?.role === 'MANAGER') && <Button type="button" variant="outline" size="sm" className="mt-2" onClick={() => setReviewCriterion(criterion.criterion_id)}>Review this criterion</Button>}
                  </li>)}</ul></>}
          {reviewCriterion && (user?.role === 'SUPERVISOR' || user?.role === 'MANAGER') && <form className="space-y-3 rounded-md border p-3" onSubmit={(event) => { event.preventDefault(); void submitEvidenceReview(); }}>
            <p className="text-sm font-medium">Human review for {reviewCriterion}</p>
            <label className="grid gap-1 text-sm">Document<select className="min-h-9 rounded border bg-background px-2" value={reviewDocumentId} onChange={(event) => setReviewDocumentId(event.target.value)}><option value="">No document (for missing evidence)</option>{(evidenceTrail?.documents ?? []).map((document) => <option key={document.id} value={document.id}>{document.name} · Text extraction: {formatStatusLabel(document.ocr_status ?? 'unknown')}</option>)}</select></label>
            <label className="grid gap-1 text-sm">Assessment<select className="min-h-9 rounded border bg-background px-2" value={reviewAssessment} onChange={(event) => setReviewAssessment(event.target.value as typeof reviewAssessment)}>{['MATCH_POSSIBLE', 'NOT_FOUND', 'UNCERTAIN', 'CONFLICTING'].map((value) => <option key={value}>{value}</option>)}</select></label>
            <label className="grid gap-1 text-sm">Human decision<select className="min-h-9 rounded border bg-background px-2" value={reviewStatus} onChange={(event) => setReviewStatus(event.target.value as typeof reviewStatus)}><option value="HUMAN_CONFIRMED">Confirm assessment</option><option value="HUMAN_REJECTED">Reject assessment</option></select></label>
            <label className="grid gap-1 text-sm">Document excerpt<textarea className="min-h-16 rounded border bg-background p-2" value={reviewExcerpt} onChange={(event) => setReviewExcerpt(event.target.value)} placeholder="Must match text extracted from the selected document." /></label>
            <label className="grid gap-1 text-sm">Page or location<input className="min-h-9 rounded border bg-background px-2" value={reviewLocator} onChange={(event) => setReviewLocator(event.target.value)} /></label>
            <label className="grid gap-1 text-sm">Review notes<textarea required minLength={1} className="min-h-16 rounded border bg-background p-2" value={reviewNotes} onChange={(event) => setReviewNotes(event.target.value)} /></label>
            <div className="flex gap-2"><Button type="submit" disabled={reviewBusy || !reviewNotes.trim()}>{reviewBusy ? 'Saving…' : 'Save human review'}</Button><Button type="button" variant="outline" onClick={() => setReviewCriterion('')}>Cancel</Button></div>
          </form>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Fastest Safe Path</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {safePathLoading ? <p role="status">Loading plan…</p> : safePathError ? <div role="alert" className="space-y-2"><p>Plan unavailable: {safePathError}</p><Button type="button" variant="outline" onClick={() => void refreshSafePath()}>Retry</Button></div> : safePath ? <>
            <div className="flex items-center justify-between gap-2"><Badge>{safePath.verdict.replaceAll('_', ' ')}</Badge><Button type="button" variant="outline" size="sm" onClick={() => void refreshSafePath()}><RefreshCw className="mr-2 size-4" />Refresh</Button></div>
            <p className="text-sm">{safePath.verdict_explanation}</p>
            <p className="text-sm">{safePath.time_remaining_minutes === null ? 'Deadline unknown' : safePath.time_remaining_minutes < 0 ? `Overdue by ${Math.abs(safePath.time_remaining_minutes)} minutes` : `${safePath.time_remaining_minutes} minutes remaining`} · Critical path: {safePath.critical_path_minutes === null ? 'Unknown' : `${safePath.critical_path_minutes} minutes (estimate)`}</p>
            {safePath.external_wait_unknown && <p className="text-sm text-amber-700">External waiting time is unknown and is excluded from this estimate.</p>}
            {safePath.steps.length === 0 ? <p className="text-sm">No recommended steps remain.</p> : <ol className="list-decimal space-y-2 pl-5">{safePath.steps.map((step) => <li key={`${step.order}-${step.action_type}-${step.criterion_id ?? ''}`} className="text-sm"><p>{step.description}</p><p className="text-xs text-muted-foreground">Requires: {step.required_role?.replaceAll('_', ' ') ?? 'Unspecified'} · {step.estimated_minutes === null ? 'Duration unknown' : `${step.estimated_minutes} minute${step.estimated_minutes === 1 ? '' : 's'} estimate`} · {step.can_current_user_perform ? 'Available to you' : `Requires: ${step.required_role?.replaceAll('_', ' ') ?? 'authorized role'}`}{step.external_wait_unknown ? ' · external wait unknown' : ''}</p></li>)}</ol>}
            {safePath.unresolved_gaps.length > 0 && <div><p className="font-medium">Unresolved gaps</p><ul className="list-disc pl-5 text-sm">{safePath.unresolved_gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul></div>}
            {safePath.assumptions.length > 0 && <div><p className="font-medium">Assumptions</p><ul className="list-disc pl-5 text-xs text-muted-foreground">{safePath.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}</ul></div>}
            <p className="border-t pt-2 text-xs text-muted-foreground">{safePath.disclaimer}</p>
          </> : <p role="status">Plan has not been loaded.</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Task and decision history</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {audit.filter((event) => event.caseId === item.id).map((event) => {
            const detail = formatTaskHistoryDetail(event.detail);
            const decisionType = formatStatusLabel(event.decisionType);
            return (
              <div key={event.id} className="flex gap-3 border-l-2 border-l-primary pl-3">
                <Clock3 className="mt-1 size-4 shrink-0" />
                <div className="min-w-0 flex-1 space-y-2">
                  <p className="font-medium">{event.action}</p>
                  <p className="text-xs text-muted-foreground">{historySourceLabel(event.decisionType, event.action)} · {event.actor} · {event.timestamp} · {decisionType}</p>
                  {detail.kind === 'conversation' ? (
                    <div className="space-y-2">
                      {detail.messages.map((message, index) => (
                        <div key={`${event.id}-message-${index}`} className="rounded-md bg-muted/40 p-2 text-sm">
                          <p className="text-xs font-medium text-muted-foreground">
                            {message.speaker ?? 'Message'}{message.timestamp ? ` · ${message.timestamp}` : ''}
                          </p>
                          <HistoryText text={message.message} />
                          {message.additionalDetails && <details className="mt-2 text-xs"><summary className="cursor-pointer">More message details</summary><p className="mt-1 whitespace-pre-wrap break-words">{message.additionalDetails}</p></details>}
                        </div>
                      ))}
                      {detail.additionalDetails && <details className="text-xs"><summary className="cursor-pointer">Additional decision details</summary><p className="mt-1 whitespace-pre-wrap break-words">{detail.additionalDetails}</p></details>}
                    </div>
                  ) : <HistoryText text={detail.text} />}
                </div>
              </div>
            );
          })}
        </CardContent>
      </Card>

      <div className="flex items-start gap-2 rounded-md bg-secondary p-3 text-secondary-foreground">
        <AlertTriangle className="mt-0.5 size-4" />
        <p className="text-sm"><strong>Human decision required.</strong> Clinical evidence verification and all approval or denial actions must be completed by authorized staff.</p>
      </div>
      {escalationOpen && <div role="dialog" aria-modal="true" aria-labelledby="escalation-title" className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3 sm:p-5"><form className="max-h-[95dvh] w-full max-w-xl space-y-4 overflow-y-auto rounded-xl border bg-background p-4 shadow-xl sm:p-6" onSubmit={event=>{event.preventDefault();void submitEscalation()}}><div><h2 id="escalation-title" className="text-xl font-semibold">Send {item.id} for escalation review</h2><p className="mt-1 text-sm text-muted-foreground">Next role: {user?.role === 'SPECIALIST' ? 'Supervisor' : 'Manager'}. This records a workflow handoff. It does not make a clinical or authorization decision.</p></div><div className="grid gap-3 rounded-lg border bg-muted/30 p-3 text-sm sm:grid-cols-2"><div><b>Payer:</b> {item.payer}</div><div><b>Service:</b> {item.service}</div><div><b>Status:</b> {item.status}</div><div><b>SLA risk:</b> {item.risk}</div><div className="sm:col-span-2"><b>Deadline:</b> {new Date(item.deadline).toLocaleString()} ({Math.round((new Date(item.deadline).getTime()-Date.now())/3600000)} hours remaining)</div></div><label className="grid gap-1 text-sm">Reason<select required className="min-h-10 rounded border bg-background px-3" value={escalationReason} onChange={event=>setEscalationReason(event.target.value)}>{['SLA deadline approaching','SLA deadline breached','Missing or conflicting clinical evidence','Payer policy clarification required','Case blocked or unresolved','Technical or workflow failure','Other'].map(value=><option key={value}>{value}</option>)}</select></label><label className="grid gap-1 text-sm">Explanation<textarea required minLength={5} maxLength={2000} className="min-h-24 rounded border bg-background p-3" value={escalationExplanation} onChange={event=>setEscalationExplanation(event.target.value)} placeholder="Describe why this case needs the next workflow role."/></label>{escalationError&&<p role="alert" className="rounded border border-destructive/40 p-3 text-sm text-destructive">{escalationError}</p>}<div className="flex flex-wrap justify-end gap-2"><Button type="button" variant="outline" disabled={escalationBusy} onClick={()=>setEscalationOpen(false)}>Cancel</Button><Button type="submit" disabled={escalationBusy||!escalationExplanation.trim()}>{escalationBusy?'Routing…':'Confirm escalation'}</Button></div></form></div>}
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return <div><p className="text-sm text-muted-foreground">{label}</p><p className="font-medium">{value}</p></div>;
}

function HistoryText({ text }: { text: string }) {
  const previewLength = 320;
  if (text.length <= previewLength) return <p className="whitespace-pre-wrap break-words text-sm">{text}</p>;
  return <details className="text-sm">
    <summary className="cursor-pointer whitespace-pre-wrap break-words">{text.slice(0, previewLength)}… <span className="text-xs text-muted-foreground">Show full details</span></summary>
    <p className="mt-2 whitespace-pre-wrap break-words">{text}</p>
  </details>;
}
