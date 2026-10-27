import { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, RefreshCw, ShieldCheck, Wrench } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { formatDiagnosticForUser, formatStatusLabel } from '@/lib/user-messages';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { paApi, type RecoveryDecision, type WorkflowFailure } from '@/lib/pa-api';
import type { PaRole } from '@/lib/auth-context';

export function RecoveryAgentPanel({ offline, role }: { offline: boolean; role: PaRole }) {
  const [failures, setFailures] = useState<WorkflowFailure[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [decision, setDecision] = useState<RecoveryDecision | null>(null);
  const [isRunning, setIsRunning] = useState(false);

  useEffect(() => {
    if (offline) return;
    void paApi.getWorkflowFailures().then((items: WorkflowFailure[]) => {
      setFailures(items);
      if (items[0]) setSelectedId(String(items[0].id));
    }).catch((error: unknown) => toast.error(error instanceof Error ? error.message : 'Could not load workflow failures'));
  }, [offline]);

  const selected = failures.find((item: WorkflowFailure) => String(item.id) === selectedId);
  const runRecovery = async (approveRecovery: boolean) => {
    if (!selected) return;
    setIsRunning(true);
    try {
      const result = await paApi.runRecoveryAgent(
        selected.caseId,
        selected.errorCode,
        approveRecovery,
        role === 'SPECIALIST',
      );
      setDecision(result);
      toast.success(approveRecovery ? 'Recovery completed and recorded.' : 'Recovery plan is ready for review.');
    } catch (error: unknown) {
      toast.error(error instanceof Error ? error.message : 'Recovery Agent failed');
    } finally {
      setIsRunning(false);
    }
  };

  return <Card className="border-l-4 border-l-primary">
    <CardHeader className="flex flex-row items-start justify-between gap-4">
      <div><CardTitle className="flex items-center gap-2"><Wrench className="size-5" /> Recovery Agent</CardTitle><p className="mt-1 text-sm text-muted-foreground">Diagnose failed actions and apply only approved, bounded recovery procedures.</p></div>
      <Badge variant="secondary">Workflow recovery</Badge>
    </CardHeader>
    <CardContent className="space-y-4">
      {offline ? <div className="rounded-md bg-secondary p-4 text-secondary-foreground"><p className="font-medium">Backend required</p><p className="text-sm">Connect to PA Guard to review failed steps and prepare a safe recovery plan.</p></div> : <>
        <div className="flex flex-col gap-3 md:flex-row">
          <Select value={selectedId || undefined} onValueChange={(value: string) => { setSelectedId(value); setDecision(null); }}>
            <SelectTrigger className="md:flex-1"><SelectValue placeholder="Select a failed workflow event" /></SelectTrigger>
            <SelectContent>{failures.filter((item: WorkflowFailure) => item.id).map((item: WorkflowFailure) => <SelectItem key={item.id} value={String(item.id)}>{item.caseId} · {formatStatusLabel(item.errorCode)}</SelectItem>)}</SelectContent>
          </Select>
          <Button onClick={() => void runRecovery(false)} disabled={!selected || isRunning}><RefreshCw className={isRunning ? 'size-4 animate-spin' : 'size-4'} /> Analyze failure</Button>
        </div>
        {selected ? <div className="grid gap-3 rounded-md bg-muted p-4 text-muted-foreground md:grid-cols-3"><div><p className="text-xs font-semibold">Case</p><p className="text-sm">{selected.caseId}</p></div><div><p className="text-xs font-semibold">Review step</p><p className="text-sm">{formatStatusLabel(selected.workflowState)}</p></div><div><p className="text-xs font-semibold">What happened</p><p className="text-sm">{formatDiagnosticForUser(selected.errorDetails, 'This step could not be completed. Review the case and try again.')}</p></div></div> : null}
        {decision ? <div className="space-y-4 border-t pt-4">
          <div className="flex flex-wrap items-center gap-2"><Badge variant={decision.recoveryStatus === 'SUCCESS' ? 'default' : 'outline'}>{formatStatusLabel(decision.recoveryStatus)}</Badge><Badge variant="secondary">{formatStatusLabel(decision.failureType)}</Badge>{decision.retryAllowed ? <Badge variant="outline">Retry allowed</Badge> : <Badge variant="destructive">Retry not available</Badge>}</div>
          {decision.narrative && <div className="rounded-md bg-muted/50 p-3"><div className="mb-1 flex flex-wrap items-center gap-2"><p className="text-xs font-semibold uppercase tracking-wide">Why this plan was suggested</p><Badge variant="outline">{decision.explanationEngine?.startsWith('ollama:') ? 'AI explanation' : 'Rule-based explanation'}</Badge></div><p className="text-sm text-muted-foreground">{decision.narrative}</p></div>}
          <div className="grid gap-4 md:grid-cols-2"><div><p className="text-sm font-semibold">Likely cause</p><p className="text-sm text-muted-foreground">{decision.likelyCause}</p></div><div><p className="text-sm font-semibold">Recommended recovery</p><p className="text-sm text-muted-foreground">{decision.recommendedRecovery}</p></div></div>
          <div><p className="text-sm font-semibold">Approved sources</p><div className="mt-2 flex flex-wrap gap-2">{decision.sourceReferences.map((source) => <Badge key={`${source.policyId}-${source.version}`} variant="outline">{source.policyId} v{source.version}</Badge>)}</div></div>
          {role === 'SPECIALIST' ? <p className="text-sm text-muted-foreground">Specialist access is limited to assigned case failures and recommendation-only previews.</p> : decision.recoveryStatus === 'PENDING_APPROVAL' ? <Button onClick={() => void runRecovery(true)} disabled={isRunning}><ShieldCheck className="size-4" /> Approve recovery</Button> : decision.recoveryStatus === 'SUCCESS' ? <div className="flex items-center gap-2 rounded-md bg-primary p-3 text-sm text-primary-foreground"><CheckCircle2 className="size-4" /> Recovery confirmed and recorded.</div> : decision.recoveryStatus === 'MANUAL_FOLLOW_UP_CREATED' ? <div className="flex items-center gap-2 rounded-md bg-secondary p-3 text-sm text-secondary-foreground"><AlertTriangle className="size-4" /> Automated recovery stopped; manual follow-up recorded.</div> : null}
        </div> : null}
      </>}
    </CardContent>
  </Card>;
}
