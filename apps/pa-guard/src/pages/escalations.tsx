import { useState } from 'react';
import { useLocation } from 'react-router-dom';
import { AlertTriangle, BellRing, Bot, CheckCircle2, FileSearch } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { usePa } from '@/lib/pa-context';
import { paApi, type EscalationDecision, type EvidenceFinding } from '@/lib/pa-api';
import type { PaCase } from '@/lib/pa-data';

export default function EscalationsPage() {
  const { cases, documents } = usePa();
  const location = useLocation();
  const [decisions, setDecisions] = useState<Record<string, EscalationDecision>>({});
  const [evidenceFindings, setEvidenceFindings] = useState<Record<string, EvidenceFinding>>({});
  const [runningId, setRunningId] = useState<string | null>(null);
  const [approvalCase, setApprovalCase] = useState<PaCase | null>(null);
  const requestedCaseId = new URLSearchParams(location.search).get('caseId');
  const items = cases.filter((item: PaCase) =>
    (!requestedCaseId || item.id === requestedCaseId) &&
    (item.risk === 'AT RISK' || item.risk === 'BREACHED'),
  );

  const evaluate = async (item: PaCase, approveNotification = false): Promise<void> => {
    setRunningId(item.id);
    try {
      const decision = await paApi.runEscalationAgent(item.id, approveNotification);
      setDecisions((current: Record<string, EscalationDecision>) => ({ ...current, [item.id]: decision }));
      try {
        const finding = await paApi.runEvidenceAgent(item.id);
        setEvidenceFindings((current: Record<string, EvidenceFinding>) => ({ ...current, [item.id]: finding }));
      } catch (error: unknown) {
        toast.error(`Escalation recommendation is available, but evidence summary failed: ${error instanceof Error ? error.message : 'Evidence review failed.'}`);
      }

      if (decision.notificationStatus === 'SIMULATED_DELIVERED') toast.success('Simulated notification confirmed and audited.');
      else toast.success('Escalation recommendation created.');
    } catch (error: unknown) {
      toast.error(error instanceof Error ? error.message : 'Escalation Agent failed.');
    } finally {
      setRunningId(null);
      setApprovalCase(null);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Escalation center</h1>
          <p className="text-sm text-muted-foreground">Deterministic LangGraph recommendations with approved policy citations and human-controlled simulated notifications.</p>
        </div>
        <Badge variant="outline"><Bot className="mr-1 size-3" />RAG connected · recommendation only</Badge>
      </div>

      <div className="space-y-3">
        {items.map((item: PaCase) => {
          const decision = decisions[item.id];
          const evidence = evidenceFindings[item.id];
          const caseDocuments = documents.filter((document) => document.caseId === item.id);
          return (
            <Card key={item.id} className={item.risk === 'BREACHED' ? 'border-l-4 border-l-destructive' : 'border-l-4 border-l-primary'}>
              <CardHeader className="pb-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <CardTitle className="text-base">{item.id} · {item.service}</CardTitle>
                    <p className="text-sm text-muted-foreground">{item.patientReference} · {item.payer} · Owner: {item.coordinator}</p>
                  </div>
                  <div className="flex gap-2">
                    <Badge variant={item.risk === 'BREACHED' ? 'destructive' : 'default'}>{item.risk}</Badge>
                    <Button variant="outline" size="sm" disabled={runningId === item.id} onClick={() => void evaluate(item)}>
                      <FileSearch className="mr-1 size-4" />{runningId === item.id ? 'Evaluating…' : 'Evaluate'}
                    </Button>
                  </div>
                </div>
              </CardHeader>
              {decision && (
                <CardContent className="grid gap-4 border-t pt-4 lg:grid-cols-[.7fr_1fr_1.4fr_auto]">
                  <div>
                    <p className="text-xs text-muted-foreground">Decision</p>
                    <p className="font-medium">{decision.escalationLevel.replace('_', ' ')}</p>
                    <p className="text-sm">{decision.escalationRequired ? 'Escalation required' : 'No escalation required'}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Responsible role</p>
                    <p className="font-medium">{decision.responsibleRole}</p>
                    <Badge className="mt-2" variant="secondary">{decision.notificationStatus.replaceAll('_', ' ')}</Badge>
                  </div>
                  <div>
                    <div className="mb-1 flex flex-wrap items-center gap-2">
                      <p className="text-xs text-muted-foreground">Agent rationale</p>
                      <Badge variant="outline">{decision.explanationEngine?.startsWith('ollama:') ? `Local LLM · ${decision.explanationEngine.slice('ollama:'.length)}` : 'Deterministic fallback'}</Badge>
                    </div>
                    <p className="text-sm">{decision.reason}</p>
                    <p className="mt-2 text-sm font-medium">{decision.recommendedAction}</p>
                    <div className="mt-2 flex flex-wrap gap-1">
                      {decision.sourceReferences.map((source) => <Badge key={`${source.policyId}-${source.version}`} variant="outline">{source.policyId} v{source.version}</Badge>)}
                    </div>
                  </div>
                  <div className="flex items-center">
                    {decision.notificationStatus === 'PENDING_APPROVAL' ? (
                      <Button size="sm" onClick={() => setApprovalCase(item)}><BellRing className="mr-1 size-4" />Review notification</Button>
                    ) : decision.notificationStatus === 'SIMULATED_DELIVERED' ? (
                      <div className="flex items-center gap-2 text-sm"><CheckCircle2 className="size-4" />Simulation confirmed</div>
                    ) : decision.notificationStatus === 'DUPLICATE_SUPPRESSED' ? (
                      <div className="flex items-center gap-2 text-sm"><AlertTriangle className="size-4" />Duplicate suppressed</div>
                    ) : null}
                  </div>
                  <div className="col-span-full rounded-md border p-3">
                    <p className="text-sm font-semibold">Case evidence and SLA summary · human review required</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Case status: {item.status} · SLA risk: {item.risk} · Deadline: {item.deadline}
                    </p>
                    <div className="mt-3 grid gap-4 md:grid-cols-2">
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Uploaded documents</p>
                        {caseDocuments.length ? (
                          <ul className="mt-1 space-y-1 text-sm">
                            {caseDocuments.map((document) => (
                              <li key={document.id}>
                                {document.originalFilename ?? document.name} · {document.category ?? 'Uncategorized'} · OCR {document.ocrStatus ?? 'not started'}{document.processingError ? ` · ${document.processingError}` : ''}
                              </li>
                            ))}
                          </ul>
                        ) : <p className="mt-1 text-sm">No uploaded documents.</p>}
                      </div>
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Evidence requirements and gaps</p>
                        {!evidence ? <p className="mt-1 text-sm">Evidence review unavailable; run Evaluate again or review the case manually.</p> : evidence.requiredDocuments.length ? (
                          <ul className="mt-1 space-y-1 text-sm">
                            {evidence.requiredDocuments.map((requiredDocument) => {
                              const found = evidence.foundDocuments.filter((match) => match.requiredDocument === requiredDocument);
                              const possible = evidence.possibleEvidenceMatches.filter((match) => match.requiredDocument === requiredDocument);
                              const status = found.length ? 'Text found · human verification required' : possible.length ? 'Possible match · human verification required' : evidence.missingDocuments.includes(requiredDocument) ? 'Not found in readable text' : evidence.unreadableDocuments.length ? 'Not verifiable while OCR needs review' : 'Not identified';
                              const supportingDocs = [...found, ...possible].map((match) => `${match.documentName} (${match.category || 'Uncategorized'})`);
                              return <li key={requiredDocument}><strong>{requiredDocument}:</strong> {status}{supportingDocs.length ? ` · ${supportingDocs.join(', ')}` : ''}</li>;
                            })}
                          </ul>
                        ) : <p className="mt-1 text-sm">No applicable approved evidence requirements; human policy review is needed.</p>}
                        {evidence?.unreadableDocuments.map((document) => (
                          <p key={`${document.documentName}-${document.reason}`} className="mt-1 text-sm text-destructive">
                            OCR review: {document.documentName} · {document.category || 'Uncategorized'} · {document.ocrStatus || 'status unknown'} · {document.reason}
                          </p>
                        ))}
                      </div>
                    </div>
                    {evidence && <p className="mt-3 text-sm"><strong>Evidence recommendation:</strong> {evidence.recommendedAction}</p>}
                    <p className="mt-2 text-xs text-muted-foreground">
                      Escalation recommendation: {decision.recommendedAction} The escalation policy and notification approval controls are unchanged.
                    </p>
                  </div>
                </CardContent>
              )}
            </Card>
          );
        })}
      </div>

      <Dialog open={approvalCase !== null} onOpenChange={(open: boolean) => { if (!open) setApprovalCase(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Approve simulated escalation notification</DialogTitle>
            <DialogDescription>This records a human approval and runs a notification simulation only. PA Guard will not claim delivery to a real recipient.</DialogDescription>
          </DialogHeader>
          {approvalCase && <div className="rounded-md bg-muted p-3 text-sm text-muted-foreground">{approvalCase.id} will be routed to the recommended responsible role. Duplicate alerts remain suppressed.</div>}
          <DialogFooter>
            <Button variant="outline" onClick={() => setApprovalCase(null)}>Cancel</Button>
            <Button disabled={!approvalCase || runningId !== null} onClick={() => { if (approvalCase) void evaluate(approvalCase, true); }}>Approve simulation</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
