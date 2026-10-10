import { useEffect, useState, type ChangeEvent } from 'react';
import { BookOpenCheck, CheckCircle2, Download, Pencil, Plus, RefreshCw, ShieldAlert, X } from 'lucide-react';
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
import { paApi, type PolicyDocument, type PolicyPayload } from '@/lib/pa-api';

const emptyPolicy: PolicyPayload = { policyId: '', documentType: 'SLA_POLICY', title: '', content: '', effectiveDate: '', expiresAt: null, version: '1.0' };
const policyTypes = ['SLA_POLICY', 'WORKFLOW_GUIDELINE', 'ESCALATION_RULE', 'TASK_PRIORITY'];

export default function PoliciesPage() {
  const [policies, setPolicies] = useState<PolicyDocument[]>([]);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('all');
  const [documentType, setDocumentType] = useState('all');
  const [dialogOpen, setDialogOpen] = useState(false);
  const [draft, setDraft] = useState<PolicyPayload>(emptyPolicy);
  const [editing, setEditing] = useState<PolicyDocument | null>(null);
  const [approvalCandidate, setApprovalCandidate] = useState<PolicyDocument | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => { try { setPolicies(await paApi.getPolicies()); } catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to load policies'); } };
  useEffect(() => { void load(); }, []);
  const filtered = policies.filter((policy: PolicyDocument) => {
    const matchesStatus = status === 'all' || (status === 'approved' ? policy.approved : !policy.approved);
    const matchesDocumentType = documentType === 'all' || policy.documentType === documentType;
    const text = `${policy.policyId} ${policy.title} ${policy.documentType} ${policy.version}`.toLowerCase();
    return matchesStatus && matchesDocumentType && text.includes(query.trim().toLowerCase());
  });
  const exportInventory = () => {
    if (filtered.length === 0) { toast.warning('No filtered policy versions are available to export.'); return; }
    const escapeCell = (value: string | number | boolean | null) => `"${String(value ?? '').replaceAll('"', '""')}"`;
    const headers = ['Policy ID', 'Title', 'Document type', 'Version', 'Effective date', 'Expiration date', 'Approval status', 'Index engine'];
    const rows = filtered.map((policy: PolicyDocument) => [policy.policyId, policy.title, policy.documentType, policy.version, policy.effectiveDate, policy.expiresAt, policy.approved ? 'Approved' : 'Draft / retired', policy.embeddingEngine ?? 'Not indexed']);
    const csv = [headers, ...rows].map((row: (string | number | boolean | null)[]) => row.map(escapeCell).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `pa-guard-filtered-policy-inventory-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast.success(`Exported ${filtered.length} filtered policy versions.`);
  };
  const openCreate = () => { setEditing(null); setDraft(emptyPolicy); setDialogOpen(true); };
  const openEdit = (policy: PolicyDocument) => { setEditing(policy); setDraft({ policyId: policy.policyId, documentType: policy.documentType, title: policy.title, content: policy.content, effectiveDate: policy.effectiveDate, expiresAt: policy.expiresAt, version: policy.version }); setDialogOpen(true); };
  const setField = (field: keyof PolicyPayload, value: string) => setDraft((current: PolicyPayload) => ({ ...current, [field]: field === 'expiresAt' ? value || null : value }));
  const save = async () => {
    if (!draft.policyId.trim() || !draft.title.trim() || !draft.content.trim() || !draft.effectiveDate || !draft.version.trim()) { toast.error('Complete all required policy fields.'); return; }
    setBusy(true);
    try {
      if (editing) await paApi.updatePolicy(editing.policyId, editing.version, draft); else await paApi.createPolicy(draft);
      toast.success(editing ? 'Draft policy updated.' : 'Draft policy created.');
      setDialogOpen(false);
      await load();
    } catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to save policy'); } finally { setBusy(false); }
  };
  const changeApproval = async (policy: PolicyDocument) => {
    setBusy(true);
    try {
      await paApi.setPolicyApproval(policy.policyId, policy.version, !policy.approved);
      toast.success(policy.approved ? 'Policy retired from retrieval.' : 'Policy approved and indexed.');
      setApprovalCandidate(null);
      await load();
    } catch (error: unknown) {
      toast.error(error instanceof Error ? error.message : 'Unable to change approval');
    } finally {
      setBusy(false);
    }
  };
  const reindex = async () => {

    setBusy(true);
    try { const result = await paApi.reindexPolicies(); toast.success(`Indexed ${result.indexed} approved policies with ${result.embedding_engine}.`); await load(); }
    catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to rebuild policy index'); } finally { setBusy(false); }
  };

  return <div className="space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-4"><div><h1 className="text-2xl font-semibold">Policy management</h1><p className="text-sm text-muted-foreground">Manage approved evidence used by SLA retrieval and recommendations.</p></div><div className="flex flex-wrap gap-2"><Button variant="outline" onClick={exportInventory} disabled={filtered.length === 0}><Download className="size-4" />Export filtered CSV</Button><Button variant="outline" onClick={reindex} disabled={busy}><RefreshCw className="size-4" />Rebuild index</Button><Button onClick={openCreate}><Plus className="size-4" />New policy</Button></div></div>
    <div className="grid gap-4 sm:grid-cols-3"><Card><CardHeader className="pb-2"><CardDescription>Total versions</CardDescription><CardTitle>{policies.length}</CardTitle></CardHeader></Card><Card><CardHeader className="pb-2"><CardDescription>Approved for retrieval</CardDescription><CardTitle>{policies.filter((policy: PolicyDocument) => policy.approved).length}</CardTitle></CardHeader></Card><Card><CardHeader className="pb-2"><CardDescription>Draft or retired</CardDescription><CardTitle>{policies.filter((policy: PolicyDocument) => !policy.approved).length}</CardTitle></CardHeader></Card></div>
    <Card><CardHeader><CardTitle>Knowledge base</CardTitle><CardDescription>Only explicitly approved, effective policy versions are eligible for RAG retrieval.</CardDescription></CardHeader><CardContent className="space-y-4"><div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-[minmax(240px,1fr)_220px_220px_auto]"><Input value={query} onChange={(event: ChangeEvent<HTMLInputElement>) => setQuery(event.target.value)} placeholder="Search policy ID, title, type, or version" /><Select value={status} onValueChange={setStatus}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All approval states</SelectItem><SelectItem value="approved">Approved</SelectItem><SelectItem value="draft">Draft or retired</SelectItem></SelectContent></Select><Select value={documentType} onValueChange={setDocumentType}><SelectTrigger><SelectValue placeholder="All document types" /></SelectTrigger><SelectContent><SelectItem value="all">All document types</SelectItem>{policyTypes.filter((type: string) => type).map((type: string) => <SelectItem key={type} value={type}>{type.replaceAll('_', ' ')}</SelectItem>)}</SelectContent></Select><Button variant="outline" onClick={() => { setQuery(''); setStatus('all'); setDocumentType('all'); }} disabled={!query.trim() && status === 'all' && documentType === 'all'}><X className="size-4" />Clear filters</Button></div><div className="flex items-center justify-between gap-3 text-sm" aria-live="polite"><p className="font-medium">Showing {filtered.length} of {policies.length} policy versions</p>{(query.trim() || status !== 'all' || documentType !== 'all') && <Badge variant="secondary">Filtered</Badge>}</div><div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="border-b text-left"><th className="pb-3">Policy</th><th className="pb-3">Type</th><th className="pb-3">Effective</th><th className="pb-3">Version</th><th className="pb-3">Index</th><th className="pb-3">Status</th><th className="pb-3 text-right">Actions</th></tr></thead><tbody>{filtered.map((policy: PolicyDocument) => <tr key={`${policy.policyId}-${policy.version}`} className="border-b last:border-0"><td className="py-3 pr-4"><p className="font-medium">{policy.title}</p><p className="text-xs text-muted-foreground">{policy.policyId}</p></td><td className="py-3 pr-4">{policy.documentType.replaceAll('_', ' ')}</td><td className="py-3 pr-4">{policy.effectiveDate}{policy.expiresAt ? ` – ${policy.expiresAt}` : ''}</td><td className="py-3 pr-4">v{policy.version}</td><td className="py-3 pr-4">{policy.embeddingEngine ?? 'Not indexed'}</td><td className="py-3 pr-4"><Badge variant={policy.approved ? 'default' : 'secondary'}>{policy.approved ? 'Approved' : 'Draft / retired'}</Badge></td><td className="py-3"><div className="flex justify-end gap-2">{!policy.approved && <Button size="sm" variant="outline" onClick={() => openEdit(policy)}><Pencil className="size-4" />Edit</Button>}<Button size="sm" variant={policy.approved ? 'destructive' : 'default'} onClick={() => setApprovalCandidate(policy)} disabled={busy}>{policy.approved ? <ShieldAlert className="size-4" /> : <CheckCircle2 className="size-4" />}{policy.approved ? 'Retire' : 'Approve'}</Button></div></td></tr>)}</tbody></table>{filtered.length === 0 && <div className="bg-muted p-8 text-center text-muted-foreground">No policies match the current filters.</div>}</div></CardContent></Card>
    <div className="rounded-md bg-secondary p-3 text-sm text-secondary-foreground"><BookOpenCheck className="mr-2 inline size-4" />Policy approval and retirement are human decisions and are recorded in audit history. Approved versions are immutable.</div>
    <AlertDialog open={Boolean(approvalCandidate)} onOpenChange={(open: boolean) => { if (!open && !busy) setApprovalCandidate(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{approvalCandidate?.approved ? 'Retire this policy version?' : 'Approve this policy version?'}</AlertDialogTitle>
          <AlertDialogDescription>
            {approvalCandidate?.approved
              ? `${approvalCandidate.title} (v${approvalCandidate.version}) will be removed from SLA retrieval and will no longer support new recommendations. Existing audit references remain unchanged, and this human decision will be recorded in audit history.`
              : approvalCandidate
                ? `${approvalCandidate.title} (v${approvalCandidate.version}) will become immutable and eligible for SLA retrieval. This human approval will be recorded in audit history.`
                : 'Confirm this policy lifecycle change.'}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction disabled={busy} onClick={(event: React.MouseEvent<HTMLButtonElement>) => { event.preventDefault(); if (approvalCandidate) void changeApproval(approvalCandidate); }}>
            {busy ? (approvalCandidate?.approved ? 'Retiring…' : 'Approving…') : (approvalCandidate?.approved ? 'Retire policy' : 'Approve and index')}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}><DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl"><DialogHeader><DialogTitle>{editing ? 'Edit draft policy' : 'Create policy draft'}</DialogTitle><DialogDescription>Document text is retrieval evidence only and cannot override deterministic system rules.</DialogDescription></DialogHeader><div className="grid gap-4 py-2"><div className="grid gap-4 sm:grid-cols-2"><div className="grid gap-2"><Label htmlFor="policy-id">Policy ID</Label><Input id="policy-id" value={draft.policyId} disabled={Boolean(editing)} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('policyId', event.target.value)} /></div><div className="grid gap-2"><Label>Document type</Label><Select value={draft.documentType} onValueChange={(value: string) => setField('documentType', value)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{policyTypes.filter((type: string) => type).map((type: string) => <SelectItem key={type} value={type}>{type.replaceAll('_', ' ')}</SelectItem>)}</SelectContent></Select></div></div><div className="grid gap-2"><Label htmlFor="title">Title</Label><Input id="title" value={draft.title} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('title', event.target.value)} /></div><div className="grid gap-2"><Label htmlFor="content">Policy content</Label><Textarea id="content" className="min-h-36" value={draft.content} onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setField('content', event.target.value)} /></div><div className="grid gap-4 sm:grid-cols-3"><div className="grid gap-2"><Label htmlFor="effective">Effective date</Label><Input id="effective" placeholder="YYYY-MM-DD" value={draft.effectiveDate} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('effectiveDate', event.target.value)} /></div><div className="grid gap-2"><Label htmlFor="expires">Expiration date</Label><Input id="expires" placeholder="Optional YYYY-MM-DD" value={draft.expiresAt ?? ''} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('expiresAt', event.target.value)} /></div><div className="grid gap-2"><Label htmlFor="version">Version</Label><Input id="version" value={draft.version} onChange={(event: ChangeEvent<HTMLInputElement>) => setField('version', event.target.value)} /></div></div></div><DialogFooter><Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button><Button onClick={() => void save()} disabled={busy}>{busy ? 'Saving…' : 'Save draft'}</Button></DialogFooter></DialogContent></Dialog>
  </div>;
}
