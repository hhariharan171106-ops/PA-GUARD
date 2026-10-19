import { useState } from 'react';
import { useLocation } from 'react-router-dom';
import { AlertTriangle, BellRing, Bot, CheckCircle2, FileSearch } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { usePa } from '@/lib/pa-context';
import { paApi, type EscalationDecision } from '@/lib/pa-api';
import type { PaCase } from '@/lib/pa-data';

export default function EscalationsPage() {
  const { cases } = usePa();
  const location = useLocation();
  const [decisions, setDecisions] = useState<Record<string, EscalationDecision>>({});
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
                    <p className="text-xs text-muted-foreground">Grounded rationale</p>
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
