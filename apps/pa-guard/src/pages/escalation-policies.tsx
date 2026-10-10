import { useEffect, useState, type ChangeEvent } from 'react';
import { CheckCircle2, Pencil, Plus, ShieldAlert, X } from 'lucide-react';
import { toast } from 'sonner';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { paApi, type EscalationPolicy, type EscalationPolicyPayload } from '@/lib/pa-api';

const policyTypes: EscalationPolicy['documentType'][] = ['ESCALATION_RULE', 'ROLE_RESPONSIBILITY', 'NOTIFICATION_PROCEDURE'];
const emptyPolicy: EscalationPolicyPayload = { policyId: '', documentType: 'ESCALATION_RULE', title: '', content: '', department: 'Prior Authorization', sourceReference: '', effectiveDate: '', expiresAt: null, version: '1.0' };

export default function EscalationPoliciesPage() {
  const [policies, setPolicies] = useState<EscalationPolicy[]>([]);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('all');
  const [type, setType] = useState('all');
  const [dialogOpen, setDialogOpen] = useState(false);
  const [draft, setDraft] = useState<EscalationPolicyPayload>(emptyPolicy);
  const [editing, setEditing] = useState<EscalationPolicy | null>(null);
  const [candidate, setCandidate] = useState<EscalationPolicy | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => { try { setPolicies(await paApi.getEscalationPolicies()); } catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to load escalation policies'); } };
  useEffect(() => { void load(); }, []);
  const filtered = policies.filter((policy: EscalationPolicy) => {
    const matchesStatus = status === 'all' || (status === 'approved' ? policy.approved : !policy.approved);
    const matchesType = type === 'all' || policy.documentType === type;
    return matchesStatus && matchesType && `${policy.policyId} ${policy.title} ${policy.department} ${policy.version}`.toLowerCase().includes(query.trim().toLowerCase());
  });
  const setField = (field: keyof EscalationPolicyPayload, value: string) => setDraft((current: EscalationPolicyPayload) => ({ ...current, [field]: field === 'expiresAt' ? value || null : value }));
  const openCreate = () => { setEditing(null); setDraft(emptyPolicy); setDialogOpen(true); };
  const openEdit = (policy: EscalationPolicy) => { setEditing(policy); setDraft({ policyId: policy.policyId, documentType: policy.documentType, title: policy.title, content: policy.content, department: policy.department, sourceReference: policy.sourceReference, effectiveDate: policy.effectiveDate, expiresAt: policy.expiresAt, version: policy.version }); setDialogOpen(true); };
  const save = async () => {
    if (!draft.policyId.trim() || !draft.title.trim() || !draft.content.trim() || !draft.department.trim() || !draft.sourceReference.trim() || !draft.effectiveDate || !draft.version.trim()) { toast.error('Complete all required policy fields.'); return; }
    setBusy(true);
    try {
      if (editing) await paApi.updateEscalationPolicy(editing.policyId, editing.version, draft); else await paApi.createEscalationPolicy(draft);
      toast.success(editing ? 'Escalation policy draft updated.' : 'Escalation policy draft created.');
      setDialogOpen(false);
      await load();
    } catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to save policy'); } finally { setBusy(false); }
  };
  const changeApproval = async (policy: EscalationPolicy) => {
    setBusy(true);
    try { await paApi.setEscalationPolicyApproval(policy.policyId, policy.version, !policy.approved); toast.success(policy.approved ? 'Policy retired from retrieval.' : 'Policy approved and indexed.'); setCandidate(null); await load(); }
    catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to change policy lifecycle'); }
    finally { setBusy(false); }
  };

  return <div className="space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-4"><div><h1 className="text-2xl font-semibold">Escalation policy management</h1><p className="text-sm text-muted-foreground">Control approved escalation rules, role ownership, and notification procedures.</p></div><Button onClick={openCreate}><Plus className="size-4" />New policy</Button></div>
    <div className="grid gap-4 sm:grid-cols-3"><Card><CardHeader className="pb-2"><CardDescription>Total versions</CardDescription><CardTitle>{policies.length}</CardTitle></CardHeader></Card><Card><CardHeader className="pb-2"><CardDescription>Approved for retrieval</CardDescription><CardTitle>{policies.filter((policy: EscalationPolicy) => policy.approved).length}</CardTitle></CardHeader></Card><Card><CardHeader className="pb-2"><CardDescription>Departments covered</CardDescription><CardTitle>{new Set(policies.map((policy: EscalationPolicy) => policy.department)).size}</CardTitle></CardHeader></Card></div>
    <Card><CardHeader><CardTitle>Escalation knowledge base</CardTitle><CardDescription>Only current, approved versions can support agent recommendations. Threshold values remain controlled by SLA settings.</CardDescription></CardHeader><CardContent className="space-y-4">
      <div className="grid gap-3 lg:grid-cols-[minmax(220px,1fr)_220px_220px_auto]"><Input value={query} onChange={(event: ChangeEvent<HTMLInputElement>) => setQuery(event.target.value)} placeholder="Search ID, title, department, or version" /><Select value={status} onValueChange={setStatus}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All lifecycle states</SelectItem><SelectItem value="approved">Approved</SelectItem><SelectItem value="draft">Draft or retired</SelectItem></SelectContent></Select><Select value={type} onValueChange={setType}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All policy types</SelectItem>{policyTypes.filter((item: string) => item).map((item: EscalationPolicy['documentType']) => <SelectItem key={item} value={item}>{item.replaceAll('_', ' ')}</SelectItem>)}</SelectContent></Select><Button variant="outline" onClick={() => { setQuery(''); setStatus('all'); setType('all'); }} disabled={!query.trim() && status === 'all' && type === 'all'}><X className="size-4" />Clear filters</Button></div>
      <p className="text-sm font-medium" aria-live="polite">Showing {filtered.length} of {policies.length} policy versions</p>
      <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="border-b text-left"><th className="pb-3">Policy</th><th className="pb-3">Type</th><th className="pb-3">Department</th><th className="pb-3">Effective</th><th className="pb-3">Status</th><th className="pb-3 text-right">Actions</th></tr></thead><tbody>{filtered.map((policy: EscalationPolicy) => <tr key={`${policy.policyId}-${policy.version}`} className="border-b last:border-0"><td className="py-3 pr-4"><p className="font-medium">{policy.title}</p><p className="text-xs text-muted-foreground">{policy.policyId} · v{policy.version}</p></td><td className="py-3 pr-4">{policy.documentType.replaceAll('_', ' ')}</td><td className="py-3 pr-4">{policy.department}</td><td className="py-3 pr-4">{policy.effectiveDate}{policy.expiresAt ? ` – ${policy.expiresAt}` : ''}</td><td className="py-3 pr-4"><Badge variant={policy.approved ? 'default' : 'secondary'}>{policy.approved ? 'Approved' : 'Draft / retired'}</Badge></td><td className="py-3"><div className="flex justify-end gap-2">{!policy.approved && <Button size="sm" variant="outline" onClick={() => openEdit(policy)}><Pencil className="size-4" />Edit</Button>}<Button size="sm" variant={policy.approved ? 'destructive' : 'default'} onClick={() => setCandidate(policy)}>{policy.approved ? <ShieldAlert className="size-4" /> : <CheckCircle2 className="size-4" />}{policy.approved ? 'Retire' : 'Approve'}</Button></div></td></tr>)}</tbody></table>{filtered.length === 0 && <div className="bg-muted p-8 text-center text-muted-foreground">No escalation policies match the current filters.</div>}</div>
    </CardContent></Card>
    <div className="rounded-md bg-secondary p-3 text-sm text-secondary-foreground">Approval and retirement require human confirmation and are recorded in immutable audit history. Approved versions cannot be edited.</div>
    <AlertDialog open={Boolean(candidate)} onOpenChange={(open: boolean) => { if (!open && !busy) setCandidate(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{candidate?.approved ? 'Retire escalation policy?' : 'Approve escalation policy?'}</AlertDialogTitle><AlertDialogDescription>{candidate?.approved ? `${candidate.title} v${candidate.version} will be removed from new Escalation Agent retrieval. Existing audit citations remain available.` : `${candidate?.title ?? 'This policy'} v${candidate?.version ?? ''} will become immutable, indexed, and eligible for Escalation Agent retrieval.`}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel><AlertDialogAction disabled={busy} onClick={(event: React.MouseEvent<HTMLButtonElement>) => { event.preventDefault(); if (candidate) void changeApproval(candidate); }}>{busy ? 'Saving…' : candidate?.approved ? 'Retire policy' : 'Approve and index'}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}><DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl"><DialogHeader><DialogTitle>{editing ? 'Edit escalation policy draft' : 'Create escalation policy draft'}</DialogTitle><DialogDescription>Policy text is retrieval evidence only. It cannot override deterministic thresholds or send notifications.</DialogDescription></DialogHeader><div className="grid gap-4 py-2"><div className="grid gap-4 sm:grid-cols-2"><div className="grid gap-2"><Label htmlFor="escalation-policy-id">Policy ID</Label><Input id="escalation-policy-id" value={draft.policyId} disabled={Boolean(editing)} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('policyId', event.target.value)} /></div><div className="grid gap-2"><Label>Policy type</Label><Select value={draft.documentType} onValueChange={(value: string) => setField('documentType', value)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{policyTypes.filter((item: string) => item).map((item: EscalationPolicy['documentType']) => <SelectItem key={item} value={item}>{item.replaceAll('_', ' ')}</SelectItem>)}</SelectContent></Select></div></div><div className="grid gap-2"><Label htmlFor="escalation-title">Title</Label><Input id="escalation-title" value={draft.title} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('title', event.target.value)} /></div><div className="grid gap-2"><Label htmlFor="escalation-content">Policy content</Label><Textarea id="escalation-content" className="min-h-32" value={draft.content} onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setField('content', event.target.value)} /></div><div className="grid gap-4 sm:grid-cols-2"><div className="grid gap-2"><Label htmlFor="department">Applicable department</Label><Input id="department" value={draft.department} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('department', event.target.value)} /></div><div className="grid gap-2"><Label htmlFor="source-reference">Source reference</Label><Input id="source-reference" value={draft.sourceReference} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('sourceReference', event.target.value)} /></div></div><div className="grid gap-4 sm:grid-cols-3"><div className="grid gap-2"><Label htmlFor="escalation-effective">Effective date</Label><Input id="escalation-effective" placeholder="YYYY-MM-DD" value={draft.effectiveDate} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('effectiveDate', event.target.value)} /></div><div className="grid gap-2"><Label htmlFor="escalation-expires">Expiration date</Label><Input id="escalation-expires" placeholder="Optional YYYY-MM-DD" value={draft.expiresAt ?? ''} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('expiresAt', event.target.value)} /></div><div className="grid gap-2"><Label htmlFor="escalation-version">Version</Label><Input id="escalation-version" value={draft.version} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('version', event.target.value)} /></div></div></div><DialogFooter><Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button><Button onClick={() => void save()} disabled={busy}>{busy ? 'Saving…' : 'Save draft'}</Button></DialogFooter></DialogContent></Dialog>
  </div>;
}
