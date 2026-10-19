import { useRef, useState, type FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { AlertTriangle, Bot, Clock3, FileSearch, FileText, LoaderCircle, RefreshCw, UploadCloud } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { usePa } from '@/lib/pa-context';
import { isPaApiConfigured, paApi, type EvidenceFinding, type EvidencePrediction, type SlaRiskPrediction } from '@/lib/pa-api';
import type { CaseStatus, Document } from '@/lib/pa-data';

const statuses: CaseStatus[] = ['NEW', 'INTAKE REVIEW', 'EVIDENCE REVIEW', 'READY TO SUBMIT', 'SUBMITTED', 'PAYER PENDING', 'APPROVED', 'DENIED', 'MORE INFORMATION REQUIRED'];
const documentCategories = ['Clinical Notes', 'Imaging Report', 'Treatment History', 'Laboratory Reports', 'Medical Necessity Documentation', 'Prescription or Medication Records', 'Other Supporting Evidence'];

export default function CaseDetailsPage() {
  const { id } = useParams();
  const { cases, documents, audit, isLoading, updateCase, escalate, uploadDocument, retryDocumentProcessing } = usePa();
  const item = cases.find((entry) => entry.id === id);
  const [status, setStatus] = useState<CaseStatus>(item?.status ?? 'NEW');
  const [coordinator, setCoordinator] = useState(item?.coordinator ?? 'Maya Chen');
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [category, setCategory] = useState('Clinical Notes');
  const [isUploading, setIsUploading] = useState(false);
  const [isReviewing, setIsReviewing] = useState(false);
  const [finding, setFinding] = useState<EvidenceFinding | null>(null);
  const [isPredictingEvidence, setIsPredictingEvidence] = useState(false);
  const [evidencePrediction, setEvidencePrediction] = useState<EvidencePrediction | null>(null);
  const [slaRisk, setSlaRisk] = useState<SlaRiskPrediction | null>(null);
  const [isAssessingRisk, setIsAssessingRisk] = useState(false);
  const [slaRiskError, setSlaRiskError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  if (!item && isLoading) return <p role="status">Loading case details…</p>;
  if (!item) return <p>Case not found.</p>;

  const docs = documents.filter((doc: Document) => doc.caseId === item.id);
  const supportedFileTypes = '.pdf,.png,.jpg,.jpeg';

  const handleUpload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedFiles.length) {
      setFeedback('Select at least one PDF, PNG, or JPEG file before uploading.');
      return;
    }
    try {
      setIsUploading(true);
      setFeedback(null);
      for (const file of selectedFiles) {
        await uploadDocument(item.id, file, category);
      }
      setFeedback(`Uploaded ${selectedFiles.length} document(s) for OCR review.`);
      setFinding(null);
      setEvidencePrediction(null);
      setSelectedFiles([]);
      const form = event.currentTarget as HTMLFormElement & { reset?: () => void };
      form.reset?.();
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : 'Document upload failed.');
    } finally {
      setIsUploading(false);
    }
  };

  const handleEvidenceReview = async () => {
    try {
      setIsReviewing(true);
      setFeedback(null);
      setFinding(await paApi.runEvidenceAgent(item.id));
      setEvidencePrediction(null);
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : 'Evidence review failed.');
    } finally {
      setIsReviewing(false);
    }
  };

  const handleEvidencePrediction = async () => {
    try {
      setIsPredictingEvidence(true);
      setFeedback(null);
      const result = await paApi.predictEvidenceGaps(item.id);
      setFinding(result.finding);
      setEvidencePrediction(result.prediction);
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : 'Evidence prediction failed.');
    } finally {
      setIsPredictingEvidence(false);
    }
  };

  const handleSlaRiskPrediction = async () => {
    if (!isPaApiConfigured) {
      setSlaRisk(null);
      setSlaRiskError('ML SLA risk prediction is unavailable in offline demo mode; connect the FastAPI backend to assess this case.');
      return;
    }
    try {
      setIsAssessingRisk(true);
      setSlaRiskError(null);
      setSlaRisk(await paApi.predictSlaRisk(item.id));
    } catch (error) {
      setSlaRisk(null);
      setSlaRiskError(error instanceof Error ? error.message : 'ML SLA risk prediction is unavailable.');
    } finally {
      setIsAssessingRisk(false);
    }
  };

  const handleRetry = async (documentId: number) => {
    try {
      setFeedback(null);
      await retryDocumentProcessing(documentId);
      setFinding(null);
      setEvidencePrediction(null);
      setFeedback('OCR processing retried and evidence review updated.');
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : 'Retry failed.');
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
          <Button variant="outline" onClick={() => escalate(item.id, 'Manual escalation requested from case details.')}>Escalate</Button>
          <Button onClick={() => updateCase(item.id, status, coordinator)}>Save update</Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><UploadCloud className="size-4" />Upload Document</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleUpload} className="space-y-3">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={supportedFileTypes}
              onChange={(event) => setSelectedFiles(Array.from(event.target.files ?? []))}
              className="sr-only"
              tabIndex={-1}
              aria-hidden="true"
            />
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" onClick={() => fileInputRef.current?.click()} disabled={isUploading}>
                <UploadCloud className="mr-2 size-4" />Choose PDF or Image
              </Button>
              <Select value={category} onValueChange={setCategory}>
                <SelectTrigger className="w-full sm:w-64" aria-label="Document category">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {documentCategories.map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}
                </SelectContent>
              </Select>
              {selectedFiles.length > 0 && (
                <span className="text-sm text-muted-foreground">
                  {selectedFiles.map((file) => file.name).join(', ')}
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {selectedFiles.length > 0 && (
                <Button type="submit" disabled={isUploading}>
                  {isUploading ? <LoaderCircle className="mr-2 size-4 animate-spin" /> : <UploadCloud className="mr-2 size-4" />}
                  {isUploading ? 'Uploading and processing…' : 'Upload Selected File'}
                </Button>
              )}
              {feedback && <p role="status" aria-live="polite" className="text-sm text-muted-foreground">{feedback}</p>}
            </div>
            <p className="text-xs text-muted-foreground">Supported: PDF, PNG, JPG/JPEG. Maximum file size: 10 MB per document.</p>
          </form>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader><CardTitle>Case information</CardTitle></CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <div>
              <p className="text-sm text-muted-foreground">Workflow status</p>
              <Select value={status} onValueChange={(value: string) => setStatus(value as CaseStatus)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>{statuses.filter((value: CaseStatus) => Boolean(value)).map((value: CaseStatus) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Assigned coordinator</p>
              <Select value={coordinator} onValueChange={setCoordinator}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>{['Maya Chen', 'Jordan Lee', 'Avery Patel'].map((value: string) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <Info label="Submission date" value={item.submissionDate} />
            <Info label="Deadline" value={item.deadline} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Bot className="size-4" />Demo agent insight</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Badge variant="outline">LANGGRAPH NOT CONNECTED</Badge>
            <p className="text-sm">Priority reflects time remaining, urgency, and evidence dependencies.</p>
            <p className="text-sm font-medium">Recommended: resolve evidence gaps, then contact payer.</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <CardTitle>ML SLA breach-risk estimate</CardTitle>
          <Button variant="outline" onClick={handleSlaRiskPrediction} disabled={isAssessingRisk}>
            {isAssessingRisk && <LoaderCircle className="mr-2 size-4 animate-spin" />}
            {isAssessingRisk ? 'Assessing…' : 'Assess SLA risk (ML)'}
          </Button>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            A separate development estimate only; it does not change deterministic SLA rules, trigger escalations, or make authorization decisions.
          </p>
          {!isPaApiConfigured && <p className="text-sm text-muted-foreground">Prediction unavailable in offline demo mode.</p>}
          {slaRiskError && <p role="status" className="text-sm text-destructive">{slaRiskError}</p>}
          {slaRisk && (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={slaRisk.riskLevel === 'HIGH' ? 'destructive' : 'outline'}>{slaRisk.riskLevel} risk</Badge>
                <span className="text-sm">{slaRisk.riskScore}% estimated risk score</span>
                <Badge variant="outline">{slaRisk.reliable ? 'Validated' : 'Unvalidated'}</Badge>
              </div>
              <p className="text-xs text-muted-foreground">{slaRisk.scoreMeaning} {slaRisk.reliabilityNote}</p>
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {slaRisk.factors.map((factor) => <li key={factor.factor}>{factor.description}</li>)}
              </ul>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-3">
          <CardTitle>Required evidence</CardTitle>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={handleEvidenceReview} disabled={isReviewing || isPredictingEvidence}>
              <FileSearch className="mr-2 size-4" />{isReviewing ? 'Reviewing…' : 'Run Evidence Agent'}
            </Button>
            <Button variant="outline" onClick={handleEvidencePrediction} disabled={isPredictingEvidence || isReviewing || !isPaApiConfigured}>
              {isPredictingEvidence && <LoaderCircle className="mr-2 size-4 animate-spin" />}
              {isPredictingEvidence ? 'Predicting…' : 'Predict evidence gaps (experimental)'}
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {!isPaApiConfigured && (
            <p className="text-sm text-muted-foreground">Experimental evidence prediction requires the FastAPI backend; it is unavailable in offline demo mode.</p>
          )}
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
                  <Badge variant={doc.status === 'MISSING' || doc.status === 'REVIEW_REQUIRED' || doc.status === 'OCR_FAILED' ? 'destructive' : doc.status === 'VERIFIED' || doc.status === 'OCR_COMPLETED' ? 'secondary' : 'outline'}>{doc.processingStatus ?? doc.status}</Badge>
                  {doc.ocrStatus && <Badge variant="outline">OCR: {doc.ocrStatus}</Badge>}
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
              <p className="text-sm">{finding.explanation}</p>
              <p className="text-sm"><strong>Recommendation:</strong> {finding.recommendedAction}</p>
              {finding.foundDocuments.length > 0 && (
                <div>
                  <p className="text-sm font-medium">Required text found (not clinically verified)</p>
                  {finding.foundDocuments.map((match, index) => (
                    <p key={`${match.requiredDocument}-${match.documentName}-${match.pageNumber}-${index}`} className="text-xs text-muted-foreground">
                      {match.requiredDocument} · {match.documentName}, page {match.pageNumber}: “{match.excerpt}”
                    </p>
                  ))}
                </div>
              )}
              {finding.possibleEvidenceMatches.length > 0 && (
                <div>
                  <p className="text-sm font-medium">Possible matches requiring human verification</p>
                  {finding.possibleEvidenceMatches.map((match, index) => (
                    <p key={`${match.requiredDocument}-${match.documentName}-${match.pageNumber}-${index}`} className="text-xs text-muted-foreground">
                      {match.requiredDocument} · {match.documentName}, page {match.pageNumber}: “{match.excerpt}”
                    </p>
                  ))}
                </div>
              )}
              {finding.missingDocuments.length > 0 && (
                <p className="text-sm"><strong>Not found in successfully processed documents:</strong> {finding.missingDocuments.join(', ')}</p>
              )}
              {finding.unreadableDocuments.map((document) => (
                <p key={`${document.documentName}-${document.reason}`} className="text-sm text-destructive">
                  OCR needs review for {document.documentName}: {document.reason}
                </p>
              ))}
              {finding.possibleMismatches.map((mismatch) => (
                <p key={mismatch} className="text-sm text-destructive">{mismatch}</p>
              ))}
            </div>
          )}
          {evidencePrediction && (
            <div className="space-y-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <strong className="text-sm">Experimental evidence-gap prediction</strong>
                <Badge variant="outline">Not validated</Badge>
              </div>
              <p className="text-sm text-muted-foreground">
                {evidencePrediction.message} Training data: {evidencePrediction.trainingDataSource}
              </p>
              {!evidencePrediction.requirementsAvailable && (
                <p className="text-sm">Predictions unavailable because no applicable approved requirements were found.</p>
              )}
              {evidencePrediction.requirementsAvailable && !evidencePrediction.available && (
                <p className="text-sm">Predictions unavailable because the model or readable document text is unavailable.</p>
              )}
              {evidencePrediction.predictions.map((prediction) => (
                <div key={prediction.requiredDocument} className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium">{prediction.requiredDocument}</span>
                  <span>ML: {prediction.mlPrediction.replaceAll('_', ' ')}</span>
                  <span className="text-muted-foreground">Evidence Agent: {prediction.verificationStatus.replaceAll('_', ' ')}</span>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Task and decision history</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {audit.filter((event) => event.caseId === item.id).map((event) => (
            <div key={event.id} className="flex gap-3 border-l-2 border-l-primary pl-3">
              <Clock3 className="mt-1 size-4" />
              <div>
                <p className="font-medium">{event.action}</p>
                <p className="text-sm text-muted-foreground">{event.detail} · {event.actor} · {event.timestamp}</p>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="flex items-start gap-2 rounded-md bg-secondary p-3 text-secondary-foreground">
        <AlertTriangle className="mt-0.5 size-4" />
        <p className="text-sm"><strong>Human decision required.</strong> Clinical evidence verification and all approval or denial actions must be completed by authorized staff.</p>
      </div>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return <div><p className="text-sm text-muted-foreground">{label}</p><p className="font-medium">{value}</p></div>;
}
