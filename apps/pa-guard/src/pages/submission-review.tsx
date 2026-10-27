import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { AlertTriangle, ArrowLeft, CheckCircle2, LoaderCircle, Send, ShieldCheck } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { useAuth } from '@/lib/auth-context';
import { isPaApiConfigured, paApi, type SubmissionStatus } from '@/lib/pa-api';
import { formatStatusLabel } from '@/lib/user-messages';

export default function SubmissionReviewPage() {
  const { id = '' } = useParams();
  const { user } = useAuth();
  const [data, setData] = useState<SubmissionStatus | null>(null);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    if (!isPaApiConfigured) throw new Error('Connect to PA Guard to review this submission.');
    const result = await paApi.getSubmissionStatus(id);
    if (!result.readiness) {
      await paApi.evaluateSubmissionReadiness(id);
      setData(await paApi.getSubmissionStatus(id));
    } else {
      setData(result);
    }
  }, [id]);

  useEffect(() => {
    let active = true;
    void refresh().catch((reasonValue: unknown) => {
      if (active) setError(reasonValue instanceof Error ? reasonValue.message : 'Unable to load submission readiness.');
    });
    return () => { active = false; };
  }, [refresh]);

  async function reevaluate() {
    setBusy(true); setError(''); setNotice('');
    try { await paApi.evaluateSubmissionReadiness(id); await refresh(); setReason(''); setNotice('Readiness was recalculated from current case data, approved requirements, and extracted document text.'); }
    catch (reasonValue) { setError(reasonValue instanceof Error ? reasonValue.message : 'Unable to evaluate readiness.'); }
    finally { setBusy(false); }
  }

  async function approve() {
    if (!data?.readiness?.version || !reason.trim()) return;
    setBusy(true); setError(''); setNotice('');
    try { await paApi.approveSubmission(id, data.readiness.version, reason.trim()); setReason(''); await refresh(); setNotice('Manager approval recorded for this readiness version.'); }
    catch (reasonValue) { setError(reasonValue instanceof Error ? reasonValue.message : 'Approval was not recorded.'); }
    finally { setBusy(false); }
  }

  async function submitDemo() {
    const version = data?.readiness?.version;
    if (!version) return;
    setBusy(true); setError(''); setNotice('');
    const storageKey = `pa-guard-demo-submit:${id}:${version}`;
    const idempotencyKey = sessionStorage.getItem(storageKey) || crypto.randomUUID();
    sessionStorage.setItem(storageKey, idempotencyKey);
    try {
      const attempt = await paApi.submitDemo(id, version, idempotencyKey);
      sessionStorage.removeItem(storageKey);
      await refresh();
      setNotice(`${attempt.status}: ${attempt.response_summary}${attempt.reference_id ? ` Reference: ${attempt.reference_id}.` : ''}`);
    } catch (reasonValue) { setError(reasonValue instanceof Error ? reasonValue.message : 'Demo submission failed. Retrying will use the same idempotency key.'); }
    finally { setBusy(false); }
  }

  if (!data && !error) return <div role="status" className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin" />Checking submission readiness…</div>;
  const readiness = data?.readiness;
  const manager = user?.role === 'MANAGER';
  const approved = data?.status === 'APPROVED_FOR_SUBMISSION';
  const statusVariant = data?.status === 'READY_FOR_REVIEW' || approved ? 'default' : 'secondary';

  return <div className="mx-auto max-w-5xl space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><Link className="mb-2 inline-flex items-center gap-2 text-sm underline" to={`/cases/${encodeURIComponent(id)}`}><ArrowLeft className="size-4" />Back to case</Link><h1 className="text-2xl font-semibold">Submission readiness review</h1><p className="text-sm text-muted-foreground">A case readiness checklist for human review. This demo is not connected to a payer.</p></div>
      <Button variant="outline" onClick={() => void reevaluate()} disabled={busy}><CheckCircle2 className="mr-2 size-4" />Recheck readiness</Button>
    </div>
    {error && <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">{error}</div>}
    {notice && <div role="status" className="rounded-md border bg-muted p-3 text-sm">{notice}</div>}
    {readiness && <>
      <Card><CardHeader><div className="flex flex-wrap items-center justify-between gap-2"><div><CardTitle>{readiness.case.id} · {readiness.case.service}</CardTitle><CardDescription>{readiness.case.payer} · Case status {formatStatusLabel(readiness.case.status)} · Deadline {readiness.case.deadline}</CardDescription></div><Badge variant={statusVariant}>{data?.status?.replaceAll('_', ' ')}</Badge></div></CardHeader><CardContent className="grid gap-3 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">SLA status</p><p className="font-medium">{readiness.sla_status}{readiness.hours_remaining == null ? '' : ` · ${readiness.hours_remaining.toFixed(1)} hours remaining`}</p></div><div><p className="text-xs text-muted-foreground">Trained SLA risk model</p><p className="font-medium">{readiness.sla_risk_prediction.available ? `${readiness.sla_risk_prediction.risk_level ?? 'Available'}${readiness.sla_risk_prediction.risk_score == null ? '' : ` · ${Math.round(readiness.sla_risk_prediction.risk_score * 100)}%`}` : `Unavailable · ${readiness.sla_risk_prediction.reason ?? 'No prediction'}`}</p></div><div><p className="text-xs text-muted-foreground">Readiness version</p><p className="font-medium">{readiness.version ?? 'Not evaluated'}{readiness.evaluated_at ? ` · ${new Date(readiness.evaluated_at).toLocaleString()}` : ''}</p></div></CardContent></Card>
      <Card><CardHeader><CardTitle>Required evidence</CardTitle><CardDescription>Text matches establish provenance only. They do not establish clinical sufficiency.</CardDescription></CardHeader><CardContent className="space-y-3">{readiness.required_evidence.length ? readiness.required_evidence.map((entry) => <div key={entry.requirement} className="rounded-md border p-3"><div className="flex flex-wrap justify-between gap-2"><strong>{entry.requirement}</strong><Badge variant={entry.status === 'FOUND_IN_TEXT' ? 'default' : 'secondary'}>{entry.status.replaceAll('_', ' ')}</Badge></div>{entry.provenance.map((source) => <p key={`${source.document_id}-${source.page_number}`} className="mt-2 text-sm text-muted-foreground">{source.filename} · {source.category} · page {source.page_number}{source.excerpt ? ` · “${source.excerpt}”` : ''}</p>)}{!entry.provenance.length && <p className="mt-2 text-sm text-muted-foreground">No supporting text located.</p>}</div>) : <p className="text-sm">No approved active payer checklist applies. A manager must configure and approve requirements before the case can be ready.</p>}</CardContent></Card>
      {(readiness.missing_items.length > 0 || readiness.conflicts.length > 0 || readiness.possible_matches.length > 0 || readiness.unreadable_documents.length > 0) && <Card className="border-amber-500/40"><CardHeader><CardTitle className="flex items-center gap-2"><AlertTriangle className="size-4" />Items requiring attention</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">{readiness.missing_items.map((value) => <p key={`missing-${value}`}>Missing: {value}</p>)}{readiness.possible_matches.map((value) => <p key={`possible-${value}`}>Possible text match needs human review: {value}</p>)}{readiness.conflicts.map((value) => <p key={`conflict-${value}`}>Conflict/configuration issue: {value}</p>)}{readiness.unreadable_documents.map((value) => <p key={`unreadable-${value.document_id}`}>Could not read or finish processing: {value.filename} · Text extraction: {formatStatusLabel(value.ocr_status)}</p>)}</CardContent></Card>}
      <Card><CardHeader><CardTitle>Authorization and submission</CardTitle><CardDescription>Only a manager can record the final internal approval and use the local demo adapter. This does not represent insurer authorization.</CardDescription></CardHeader><CardContent className="space-y-4">
        {data?.approval && <p className="rounded border p-3 text-sm"><ShieldCheck className="mr-2 inline size-4" />Approved by {data.approval.display_name} on {new Date(data.approval.approved_at).toLocaleString()}: {data.approval.reason}</p>}
        {manager && readiness.status === 'READY_FOR_REVIEW' && !approved && <><label className="grid gap-2 text-sm">Manager approval reason<Textarea value={reason} onChange={(event) => setReason(event.target.value)} minLength={10} maxLength={1000} placeholder="Document why this submission is approved for the demo adapter." /></label><Button onClick={() => void approve()} disabled={busy || reason.trim().length < 10 || !readiness.version}><ShieldCheck className="mr-2 size-4" />{busy ? 'Recording…' : 'Approve for demo submission'}</Button></>}
        {manager && approved && <Button onClick={() => void submitDemo()} disabled={busy}><Send className="mr-2 size-4" />{busy ? 'Recording…' : 'Submit PA (DEMO only)'}</Button>}
        {!manager && <p className="text-sm text-muted-foreground">A manager must approve a ready checklist before the demo submission action is available.</p>}
        <p className="rounded-md bg-muted p-3 text-sm"><strong>DEMO adapter:</strong> records a local attempt and a DEMO reference ID. It does not contact a payer, transmit documents, return a payer response, or make an approval decision.</p>
        {data?.attempts.map((attempt, index) => <div key={`${attempt.idempotency_key}-${index}`} className="rounded border p-3 text-sm"><b>{attempt.status}</b> · {attempt.adapter_type} · {attempt.reference_id ?? 'No reference'} · {new Date(attempt.attempted_at).toLocaleString()}<p className="mt-1 text-muted-foreground">{attempt.response_summary}</p></div>)}
      </CardContent></Card>
    </>}
  </div>;
}
