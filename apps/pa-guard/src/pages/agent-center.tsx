import { Bot, Clock3, FileText, Play, ShieldCheck } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { usePa } from '@/lib/pa-context';
import type { PolicyReference, SlaRecommendation } from '@/lib/pa-api';

export default function AgentCenterPage() {
  const { slaRun, runSlaAgent, isRunningSla } = usePa();
  return <div className="space-y-5">
    <div className="flex flex-wrap items-end justify-between gap-3"><div><h1 className="text-2xl font-semibold">SLA RAG agent center</h1><p className="text-sm text-muted-foreground">Policy-grounded prioritization with deterministic deadline calculations and local Ollama explanations.</p></div><Button onClick={runSlaAgent} disabled={isRunningSla}><Play className="size-4" />{isRunningSla ? 'Running RAG graph…' : 'Run SLA RAG agent'}</Button></div>
    <div className="rounded-md bg-secondary p-3 text-sm text-secondary-foreground"><strong>Recommendation only:</strong> retrieved policies are evidence, not executable instructions. Every recommendation requires human review and cannot change status, assignment, approval, or denial.</div>
    {!slaRun ? <Card><CardContent className="flex flex-col items-center gap-3 p-8 text-center"><Bot className="size-8" /><div><p className="font-semibold">No RAG run in this session</p><p className="text-sm text-muted-foreground">Run on demand to retrieve approved policies, validate versions, calculate risk, and audit cited recommendations.</p></div><Button onClick={runSlaAgent} disabled={isRunningSla}>Run SLA RAG agent</Button></CardContent></Card> : <>
      <div className="flex flex-wrap items-center gap-2 text-sm"><Badge variant="secondary">LANGGRAPH RAG CONNECTED</Badge><Badge variant="outline">LOCAL OLLAMA</Badge><span className="text-muted-foreground">Run {new Date(slaRun.runAt).toLocaleString()}</span></div>
      <div className="space-y-3">{slaRun.recommendations.map((item: SlaRecommendation) => <Card key={item.caseId} className={item.riskLevel === 'BREACHED' || item.riskLevel === 'HIGH' ? 'border-l-4 border-l-destructive' : 'border-l-4 border-l-primary'}><CardContent className="space-y-4 p-4">
        <div className="grid gap-4 md:grid-cols-[.3fr_1fr_.7fr_1.3fr_1fr]"><div className="text-2xl font-semibold">#{item.rank}</div><div><p className="font-semibold">{item.caseId}</p><p className="text-sm text-muted-foreground">Confidence {Math.round(item.confidence * 100)}%</p></div><div><Badge variant={item.riskLevel === 'BREACHED' || item.riskLevel === 'HIGH' ? 'destructive' : item.riskLevel === 'MEDIUM' ? 'default' : 'outline'}>{item.riskLevel}</Badge><p className="mt-2 flex items-center gap-1 text-sm"><Clock3 className="size-3" />{item.remainingTime}</p></div><div><p className="text-xs text-muted-foreground">Grounded explanation</p><p className="text-sm">{item.explanation}</p></div><div><p className="text-xs text-muted-foreground">Responsible role</p><p className="text-sm font-medium">{item.assignedRole}</p></div></div>
        <div className="grid gap-3 border-t pt-3 md:grid-cols-2"><div><p className="text-xs text-muted-foreground">Recommended action</p><p className="text-sm font-medium">{item.recommendedAction}</p></div><div><p className="flex items-center gap-1 text-xs text-muted-foreground"><FileText className="size-3" />Source policies</p><div className="mt-1 flex flex-wrap gap-1">{item.sourceReferences.map((source: PolicyReference) => <Badge key={`${source.policyId}-${source.version}`} variant="outline">{source.policyId} v{source.version}</Badge>)}</div></div></div>
        {item.needsHumanReview && <div className="flex items-center gap-2 rounded-md bg-accent p-2 text-sm text-accent-foreground"><ShieldCheck className="size-4" />Human review required before any consequential action.</div>}
      </CardContent></Card>)}</div>
    </>}
  </div>;
}