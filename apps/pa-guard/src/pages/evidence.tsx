import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { CheckCircle2, FileSearch, FileWarning, ShieldAlert } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { paApi, type EvidenceFinding } from '@/lib/pa-api';
import { usePa } from '@/lib/pa-context';
import { useAuth } from '@/lib/auth-context';
import { canVerifyEvidenceDocument } from '@/lib/evidence-permissions';
import type { Document, PaCase } from '@/lib/pa-data';

export default function EvidencePage() {
  const { cases, documents, verifyDocument } = usePa();
  const { user } = useAuth();
  const canVerifyDocument = canVerifyEvidenceDocument(user?.role);
  const location = useLocation();
  const [caseId, setCaseId] = useState('');
  const [finding, setFinding] = useState<EvidenceFinding | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const activeCases = cases.filter((item: PaCase) => !['APPROVED', 'DENIED'].includes(item.status));

  useEffect(() => {
    const requestedCaseId = new URLSearchParams(location.search).get('caseId');
    if (requestedCaseId && activeCases.some((item: PaCase) => item.id === requestedCaseId)) {
      setCaseId(requestedCaseId);
    }
  }, [activeCases, location.search]);

  const runReview = async () => {
    if (!caseId) return;
    setIsRunning(true);
    try {
      const result = await paApi.runEvidenceAgent(caseId);
      setFinding(result);
      toast.success('Document review is complete. Findings were saved to the audit history.');
    } catch (error: unknown) {
      toast.error(error instanceof Error ? error.message : 'Evidence review failed');
    } finally {
      setIsRunning(false);
    }
  };

  return <div className="space-y-5">
    <div className="flex flex-col justify-between gap-3 md:flex-row md:items-end">
      <div><h1 className="text-2xl font-semibold">Evidence review</h1><p className="text-sm text-muted-foreground">Compare extracted document text with approved payer requirements. A text match does not confirm clinical sufficiency; a person must verify the evidence.</p></div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Select value={caseId || 'select-case'} onValueChange={(value: string) => setCaseId(value === 'select-case' ? '' : value)}>
          <SelectTrigger className="w-full sm:w-64"><SelectValue placeholder="Select active case" /></SelectTrigger>
          <SelectContent><SelectItem value="select-case">Select active case</SelectItem>{activeCases.filter((item: PaCase) => item.id).map((item: PaCase) => <SelectItem key={item.id} value={item.id}>{item.id} · {item.payer}</SelectItem>)}</SelectContent>
        </Select>
        <Button onClick={runReview} disabled={!caseId || isRunning}><FileSearch className="size-4" />{isRunning ? 'Reviewing…' : 'Run Evidence Agent'}</Button>
      </div>
    </div>

    {finding ? <Card className="border-l-4 border-l-primary"><CardHeader><CardTitle className="flex flex-wrap items-center justify-between gap-2 text-base"><span>Agent finding · {finding.caseId}</span><Badge variant={finding.verificationStatus === 'VERIFIED' ? 'secondary' : 'destructive'}>{finding.verificationStatus}</Badge></CardTitle></CardHeader><CardContent className="space-y-4">
      <div className="rounded-md bg-secondary p-3 text-sm text-secondary-foreground"><div className="mb-1 flex flex-wrap items-center gap-2"><strong>Why this result was suggested</strong><Badge variant="outline">{finding.explanationEngine?.startsWith('ollama:') ? 'AI explanation' : 'Rule-based explanation'}</Badge></div><p>{finding.explanation}</p></div>
      <div className="grid gap-4 md:grid-cols-3"><div><p className="text-sm font-semibold">Required</p><ul className="mt-2 space-y-1 text-sm">{finding.requiredDocuments.length ? finding.requiredDocuments.map((item: string) => <li key={item}>• {item}</li>) : <li>Requirements unavailable</li>}</ul></div><div><p className="text-sm font-semibold">Not found in successfully processed documents</p><ul className="mt-2 space-y-1 text-sm">{finding.missingDocuments.length ? finding.missingDocuments.map((item: string) => <li key={item}>• {item}</li>) : <li>None identified</li>}</ul></div><div><p className="text-sm font-semibold">Recommended action</p><p className="mt-2 text-sm">{finding.recommendedAction}</p></div></div>
      {finding.requiredDocuments.length ? <div className="rounded-md border p-3"><p className="text-sm font-semibold">Case evidence summary · human review required</p><ul className="mt-2 space-y-1 text-sm">{finding.requiredDocuments.map((requiredDocument) => { const exact = finding.foundDocuments.filter((match) => match.requiredDocument === requiredDocument); const possible = finding.possibleEvidenceMatches.filter((match) => match.requiredDocument === requiredDocument); const status = exact.length ? 'Text found (not clinically verified)' : possible.length ? 'Possible match (review)' : finding.missingDocuments.includes(requiredDocument) ? 'Not found in successfully processed text' : finding.unreadableDocuments.length ? 'Not verifiable while OCR needs review' : 'Not identified'; const docsForRequirement = [...exact, ...possible].map((match) => `${match.documentName} · ${match.category || 'Uncategorized'}`); return <li key={requiredDocument}><strong>{requiredDocument}:</strong> {status}{docsForRequirement.length ? ` · ${docsForRequirement.join(', ')}` : ''}</li>; })}</ul></div> : null}
      {finding.foundDocuments.length ? <div><p className="text-sm font-semibold">Required text found (not clinically verified)</p><ul className="mt-2 space-y-1 text-sm">{finding.foundDocuments.map((match, index) => <li key={`${match.requiredDocument}-${match.documentName}-${match.pageNumber}-${index}`}>{match.requiredDocument} · {match.documentName} · {match.category || 'Uncategorized'} · Text extraction: {match.ocrStatus || 'unknown'}, page {match.pageNumber}: “{match.excerpt}”</li>)}</ul></div> : null}
      {finding.possibleEvidenceMatches.length ? <div><p className="text-sm font-semibold">Possible evidence matches requiring human verification</p><ul className="mt-2 space-y-1 text-sm">{finding.possibleEvidenceMatches.map((match, index) => <li key={`${match.requiredDocument}-${match.documentName}-${match.pageNumber}-${index}`}>{match.requiredDocument} · {match.documentName} · {match.category || 'Uncategorized'} · Text extraction: {match.ocrStatus || 'unknown'}, page {match.pageNumber}: “{match.excerpt}”</li>)}</ul></div> : null}
      {finding.unreadableDocuments.length ? <div><p className="text-sm font-semibold">Unreadable or unprocessed documents</p><ul className="mt-2 space-y-1 text-sm">{finding.unreadableDocuments.map((document) => <li key={`${document.documentName}-${document.reason}`}>{document.documentName} · {document.category || 'Uncategorized'} · OCR {document.ocrStatus || 'unknown'}: {document.reason}</li>)}</ul></div> : null}
      {finding.possibleMismatches.length ? <div><p className="flex items-center gap-2 text-sm font-semibold"><ShieldAlert className="size-4" />Human review flags</p><ul className="mt-2 space-y-1 text-sm">{finding.possibleMismatches.map((item: string) => <li key={item}>• {item}</li>)}</ul></div> : null}
      <div><p className="text-sm font-semibold">Source references</p><div className="mt-2 flex flex-wrap gap-2">{finding.sourceReferences.map((source) => <Badge key={`${source.policyId}-${source.version}`} variant="outline">{source.policyId} v{source.version} · {source.sourceReference ?? source.documentType}</Badge>)}</div></div>
    </CardContent></Card> : null}

    <div className="grid gap-4 lg:grid-cols-2">{documents.map((doc: Document) => <Card key={doc.id}><CardHeader><CardTitle className="flex items-center justify-between text-base"><span>{doc.originalFilename ?? doc.name}</span><Badge variant={doc.status === 'MISSING' ? 'destructive' : doc.status === 'VERIFIED' ? 'secondary' : 'outline'}>{doc.status}</Badge></CardTitle></CardHeader><CardContent className="flex items-center justify-between gap-3"><div><p className="text-sm font-medium">{doc.caseId}</p><p className="text-sm text-muted-foreground">{doc.category ?? 'Uncategorized'} · Text extraction: {doc.ocrStatus ?? 'not started'}</p></div>{doc.status !== 'MISSING' && doc.status !== 'VERIFIED' && canVerifyDocument ? <Button onClick={() => verifyDocument(doc.id)}><CheckCircle2 className="size-4" />Mark verified</Button> : doc.status === 'MISSING' ? <span className="flex items-center gap-2 text-sm"><FileWarning className="size-4" />Request from care team</span> : doc.status === 'VERIFIED' ? null : <span className="max-w-56 text-right text-xs text-muted-foreground">Clinical verification must be completed by a Specialist or Supervisor.</span>}</CardContent></Card>)}</div>
    <div className="rounded-md bg-secondary p-3 text-sm text-secondary-foreground"><strong>Safety:</strong> The agent matches extracted text to approved requirement labels only. Text matches are not clinical verification; unreadable OCR is not evidence of absence. It cannot approve or deny an authorization.</div>
  </div>;
}
