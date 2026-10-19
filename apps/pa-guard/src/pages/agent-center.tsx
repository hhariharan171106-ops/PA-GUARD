import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, Bot, FileCheck2, MessageSquareText, Play, ShieldCheck, UserRound } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { paApi, type AgentSystemStatus, type RecoveryDecision, type SlaRecommendation } from '@/lib/pa-api';
import { usePa } from '@/lib/pa-context';
import type { PaCase } from '@/lib/pa-data';

type AgentMessage = {
  id: string;
  speaker: string;
  body: string;
  notes: string[];
  status: 'advisory' | 'review';
  followUp?: { label: string; to: string };
};

type ChatMessage =
  | { id: string; role: 'human'; body: string }
  | ({ id: string; role: 'agent' } & AgentMessage);

const nowLabel = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

function explanationSource(engine?: string) {
  if (engine === 'not_run') return 'A language-model explanation was skipped because policy coverage needs human review.';
  if (engine?.startsWith('ollama:')) return `Plain-language explanation from local ${engine.slice('ollama:'.length)}.`;
  return 'The local language model was unavailable; this wording uses a deterministic fallback.';
}

function recommendationMessage(caseId: string, item: SlaRecommendation): AgentMessage {
  return {
    id: `sla-${caseId}-${Date.now()}`,
    speaker: 'SLA Operations Agent',
    body: `${item.explanation} ${item.recommendedAction}`,
    notes: [
      `Rule-based SLA status: ${item.riskLevel.replaceAll('_', ' ')} · ${
        item.riskLevel === 'BREACHED'
          ? `deadline passed ${item.remainingTime.replace(/^-/, '')} ago`
          : `${item.remainingTime} remaining`
      }`,
      `Suggested owner: ${item.assignedRole}`,
      `Priority: ${item.priority.replaceAll('_', ' ')}`,
      explanationSource(item.explanationEngine),
      ...item.sourceReferences.map((source) => `Policy source: ${source.policyId}, version ${source.version}`),
    ],
    status: 'review',
    followUp: { label: 'Open case', to: `/cases/${encodeURIComponent(caseId)}` },
  };
}

export default function AgentCenterPage() {
  const { cases, isOfflineDemo, isLoading } = usePa();
  const activeCases = cases.filter((item: PaCase) => !['APPROVED', 'DENIED'].includes(item.status));
  const [caseId, setCaseId] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [agentStatus, setAgentStatus] = useState<AgentSystemStatus | null>(null);
  const [agentStatusError, setAgentStatusError] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;
    void paApi.getAgentStatus().then((status) => {
      if (isMounted) {
        setAgentStatus(status);
        setAgentStatusError(null);
      }
    }).catch((error: unknown) => {
      if (isMounted) {
        setAgentStatus(null);
        setAgentStatusError(error instanceof Error ? error.message : 'Could not check FastAPI and agent availability.');
      }
    });
    return () => { isMounted = false; };
  }, []);

  useEffect(() => {
    if (!caseId && activeCases[0]) setCaseId(activeCases[0].id);
  }, [activeCases, caseId]);

  const addAgentMessage = (message: AgentMessage) => {
    setMessages((current) => [...current, { ...message, role: 'agent' }]);
  };

  const runBriefing = async () => {
    if (!caseId || isRunning || isOfflineDemo || agentStatus?.api_status !== 'ready') return;

    const selectedCase = activeCases.find((item: PaCase) => item.id === caseId);
    if (!selectedCase) {
      setRunError('Select an active case before requesting an agent briefing.');
      return;
    }

    setMessages((current) => [
      ...current,
      { id: `human-${Date.now()}`, role: 'human', body: `Please prepare an agent briefing for ${caseId}.` },
    ]);
    setRunError(null);
    setIsRunning(true);

    const runAgent = async (speaker: string, run: () => Promise<void>) => {
      try {
        await run();
      } catch (error: unknown) {
        addAgentMessage({
          id: `${speaker}-${caseId}-${Date.now()}`,
          speaker,
          body: error instanceof Error ? error.message : `${speaker} could not complete this review.`,
          notes: [],
          status: 'review',
          followUp: { label: 'Open case', to: `/cases/${encodeURIComponent(caseId)}` },
        });
      }
    };

    try {
      await runAgent('SLA Operations Agent', async () => {
        const item = await paApi.recommendSlaCase(caseId);
        addAgentMessage(recommendationMessage(caseId, item));
      });

      await runAgent('Evidence Review Agent', async () => {
        const finding = await paApi.runEvidenceAgent(caseId);
        const notes = [
          `Approved requirements checked: ${finding.requiredDocuments.length}`,
          explanationSource(finding.explanationEngine),
          finding.missingDocuments.length
            ? `Not found in successfully processed documents: ${finding.missingDocuments.join(', ')}`
            : 'No required items were reported as not found in successfully processed documents.',
          ...finding.unreadableDocuments.map((item) => `Needs human review because it could not be read: ${item.documentName}`),
          ...finding.possibleMismatches.map((item) => `Check this finding: ${item}`),
          ...finding.sourceReferences.map((source) => `Policy source: ${source.policyId}, version ${source.version}`),
        ];
        addAgentMessage({
          id: `evidence-${caseId}-${Date.now()}`,
          speaker: 'Evidence Review Agent',
          body: `${finding.explanation} This is a document-matching report, not clinical verification. An unreadable or unprocessed document is not being treated as missing.`,
          notes,
          status: 'review',
          followUp: { label: 'Review evidence', to: `/evidence?caseId=${encodeURIComponent(caseId)}` },
        });
      });

      await runAgent('Escalation Agent', async () => {
        const decision = await paApi.runEscalationAgent(caseId);
        addAgentMessage({
          id: `escalation-${caseId}-${Date.now()}`,
          speaker: 'Escalation Agent',
          body: `${decision.reason} ${decision.recommendedAction}`,
          notes: [
            explanationSource(decision.explanationEngine),
            `Escalation level: ${decision.escalationLevel.replaceAll('_', ' ')}`,
            `Responsible role: ${decision.responsibleRole}`,
            `Notification status: ${decision.notificationStatus.replaceAll('_', ' ')}`,
            ...decision.sourceReferences.map((source) => `Policy source: ${source.policyId}, version ${source.version}`),
          ],
          status: 'review',
          followUp: { label: 'Review escalation', to: `/escalations?caseId=${encodeURIComponent(caseId)}` },
        });
      });

      await runAgent('SLA Risk Model', async () => {
        if (new Date(selectedCase.deadline).getTime() <= Date.now()) {
          addAgentMessage({
            id: `risk-${caseId}-${Date.now()}`,
            speaker: 'SLA Risk Model',
            body: 'This deadline has passed, so a prospective breach prediction does not apply. Please use the deterministic SLA status and human review for this case.',
            notes: ['No machine-learning prediction was requested for a case past its deadline.'],
            status: 'advisory',
            followUp: { label: 'Open case', to: `/cases/${encodeURIComponent(caseId)}` },
          });
          return;
        }
        const prediction = await paApi.predictSlaRisk(caseId);
        addAgentMessage({
          id: `risk-${caseId}-${Date.now()}`,
          speaker: 'SLA Risk Model',
          body: prediction.available
            ? `The model estimates ${prediction.riskLevel.toLowerCase()} operational risk (${prediction.riskScore}/100). ${prediction.scoreMeaning}`
            : 'The SLA risk model is currently unavailable for this case.',
          notes: [
            prediction.reliabilityNote,
            `Training data: ${prediction.trainingDataSource}`,
            ...prediction.factors.slice(0, 3).map((factor) => `${factor.factor}: ${factor.description}`),
          ],
          status: 'advisory',
          followUp: { label: 'Open case', to: `/cases/${encodeURIComponent(caseId)}` },
        });
      });

      await runAgent('Recovery Agent', async () => {
        const failures = await paApi.getWorkflowFailures();
        const failure = failures.find((item) => item.caseId === caseId);
        if (!failure) {
          addAgentMessage({
            id: `recovery-${caseId}-${Date.now()}`,
            speaker: 'Recovery Agent',
            body: 'No recorded workflow failure was found for this case, so there is no recovery proposal to review.',
            notes: [],
            status: 'advisory',
          });
          return;
        }
        const decision: RecoveryDecision = await paApi.runRecoveryAgent(caseId, failure.errorCode, false, true);
        addAgentMessage({
          id: `recovery-${caseId}-${Date.now()}`,
          speaker: 'Recovery Agent',
          body: decision.narrative ?? `The recorded issue is ${decision.failureType.replaceAll('_', ' ').toLowerCase()}. ${decision.likelyCause} Suggested next step: ${decision.recommendedRecovery} No recovery action was taken.`,
          notes: [
            `Recovery status: ${decision.recoveryStatus.replaceAll('_', ' ')}`,
            explanationSource(decision.explanationEngine),
            decision.humanApprovalRequired ? 'Human approval is required before any eligible recovery action.' : 'This report does not itself execute a recovery action.',
            ...decision.sourceReferences.map((source) => `Policy source: ${source.policyId}, version ${source.version}`),
          ],
          status: 'review',
          followUp: { label: 'Open case', to: `/cases/${encodeURIComponent(caseId)}` },
        });
      });
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><MessageSquareText className="size-6" />Agent conversation</h1>
          <p className="text-sm text-muted-foreground">Readable, case-by-case briefings from PA Guard agents with human follow-up links.</p>
        </div>
        <Badge variant="outline"><Bot className="mr-1 size-3" />{agentStatus?.ollama.status === 'ready' ? agentStatus.ollama.model : 'Local LLM status'}</Badge>
      </div>

      <div className="rounded-md border border-amber-500/40 bg-amber-50 p-3 text-sm text-amber-950 dark:bg-amber-950/20 dark:text-amber-100">
        <strong>Human decision required.</strong> Agent messages are recommendations, not clinical verification or authorization decisions. The risk model is experimental and trained on synthetic data. No approval, denial, submission, or recovery action is triggered by this briefing.
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Start a case briefing</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 sm:flex-row">
          <Select value={caseId} onValueChange={setCaseId}>
            <SelectTrigger className="sm:flex-1"><SelectValue placeholder="Select a case" /></SelectTrigger>
            <SelectContent>
              {activeCases.map((item: PaCase) => <SelectItem key={item.id} value={item.id}>{item.id} · {item.service} · {item.payer}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button onClick={() => void runBriefing()} disabled={!caseId || isRunning || isOfflineDemo || agentStatus?.api_status !== 'ready'}>
            <Play className={isRunning ? 'size-4 animate-pulse' : 'size-4'} />
            {isRunning ? 'Agents are preparing reports…' : 'Ask agents for a briefing'}
          </Button>
        </CardContent>
        {(isOfflineDemo || agentStatus?.api_status !== 'ready') && <p className="px-6 pb-4 text-sm text-muted-foreground">FastAPI must be connected before live agents can review a case. No sample JSON is shown in chat.</p>}
        {isLoading && <p className="px-6 pb-4 text-sm text-muted-foreground">Loading cases from PA Guard…</p>}
        {!isLoading && !activeCases.length && !isOfflineDemo && <p className="px-6 pb-4 text-sm text-muted-foreground">There are no active cases to review.</p>}
      </Card>

      <Card>
        <CardContent className="space-y-1 p-4 text-sm">
          <p>
            <strong>FastAPI:</strong> {agentStatus?.api_status === 'ready' ? 'connected' : agentStatusError ? 'unavailable' : 'checking…'}
            {' · '}<strong>LangGraph:</strong> {agentStatus?.agents
              ? `SLA, evidence, escalation, and recovery agents are ${Object.values(agentStatus.agents).every((status) => status === 'ready') ? 'ready' : 'partially unavailable'}.`
              : 'checking agent routes…'}
          </p>
          <p className={agentStatus?.ollama.status === 'ready' ? 'text-muted-foreground' : 'text-amber-700 dark:text-amber-300'}>
            <strong>Local LLM:</strong> {agentStatus?.ollama.message ?? agentStatusError ?? 'Checking Ollama and the configured model…'}
            {agentStatus?.ollama.status === 'ready' && ` (${agentStatus.ollama.model})`}
          </p>
          {agentStatusError && <p role="alert" className="text-destructive">{agentStatusError}</p>}
        </CardContent>
      </Card>

      {runError && <p role="alert" className="text-sm text-destructive">{runError}</p>}

      <Card className="overflow-hidden">
        <CardHeader className="border-b bg-muted/30">
          <CardTitle className="flex items-center gap-2 text-base"><Bot className="size-4" />Case review conversation{caseId ? ` · ${caseId}` : ''}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5 p-4 md:p-6">
          {!messages.length ? (
            <div className="flex min-h-56 flex-col items-center justify-center gap-3 text-center text-muted-foreground">
              <MessageSquareText className="size-8" />
              <p className="font-medium">Your agent briefing will appear here in plain language.</p>
              <p className="max-w-md text-sm">Choose a case and ask for a briefing. The agents report their findings one at a time, including items that need your review.</p>
            </div>
          ) : messages.map((message) => message.role === 'human' ? (
            <div key={message.id} className="ml-auto flex max-w-[85%] items-start justify-end gap-3">
              <div className="rounded-2xl rounded-tr-sm bg-primary px-4 py-3 text-sm text-primary-foreground">{message.body}</div>
              <span className="grid size-8 shrink-0 place-items-center rounded-full bg-muted"><UserRound className="size-4" /></span>
            </div>
          ) : (
            <article key={message.id} className="flex max-w-[95%] items-start gap-3">
              <span className="grid size-8 shrink-0 place-items-center rounded-full bg-secondary text-secondary-foreground"><Bot className="size-4" /></span>
              <div className="min-w-0 flex-1 space-y-3 rounded-2xl rounded-tl-sm border bg-card p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h2 className="font-semibold">{message.speaker}</h2>
                  <Badge variant={message.status === 'review' ? 'destructive' : 'secondary'}>
                    {message.status === 'review' ? <><AlertTriangle className="mr-1 size-3" />Human review requested</> : 'Advisory only'}
                  </Badge>
                </div>
                <p className="whitespace-pre-wrap text-sm leading-6">{message.body}</p>
                {message.notes.length > 0 && <ul className="space-y-1 border-l-2 pl-3 text-sm text-muted-foreground">{message.notes.map((note, index) => <li key={`${message.id}-note-${index}`}>{note}</li>)}</ul>}
                <div className="flex flex-wrap items-center gap-2 border-t pt-3">
                  {message.followUp && <Button asChild size="sm" variant="outline"><Link to={message.followUp.to}>{message.speaker === 'Evidence Review Agent' ? <FileCheck2 className="size-4" /> : <ShieldCheck className="size-4" />}{message.followUp.label}</Link></Button>}
                  <span className="text-xs text-muted-foreground">Review the underlying case and policy before acting.</span>
                </div>
              </div>
            </article>
          ))}
          {isRunning && <div role="status" className="flex items-center gap-3 text-sm text-muted-foreground"><Bot className="size-4 animate-pulse" />Waiting for the next agent report…</div>}
        </CardContent>
      </Card>
    </div>
  );
}
