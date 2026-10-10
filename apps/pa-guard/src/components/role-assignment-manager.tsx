import { useEffect, useMemo, useState, type ChangeEvent } from 'react';
import { format } from 'date-fns';
import { CalendarDays, History, Pencil, Plus, Search, UserRoundCog, X } from 'lucide-react';
import type { DateRange } from 'react-day-picker';
import { toast } from 'sonner';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Calendar } from '@/components/ui/calendar';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { paApi, type RoleAssignment, type RoleAssignmentHistoryItem, type RoleAssignmentPayload } from '@/lib/pa-api';

const emptyAssignment: RoleAssignmentPayload = { escalationLevel: 'LEVEL_1', responsibleRole: '', department: 'Prior Authorization', effectiveDate: '', expiresAt: null };

const getStatus = (assignment: RoleAssignment): 'Scheduled' | 'Active' | 'Retired' => {
  const today = new Date().toISOString().slice(0, 10);
  if (assignment.effectiveDate > today) return 'Scheduled';
  if (assignment.expiresAt && assignment.expiresAt <= today) return 'Retired';
  return 'Active';
};

export function RoleAssignmentManager() {
  const [assignments, setAssignments] = useState<RoleAssignment[]>([]);
  const [history, setHistory] = useState<RoleAssignmentHistoryItem[]>([]);
  const [draft, setDraft] = useState<RoleAssignmentPayload>(emptyAssignment);
  const [editing, setEditing] = useState<RoleAssignment | null>(null);
  const [retiring, setRetiring] = useState<RoleAssignment | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [historyDateRange, setHistoryDateRange] = useState<DateRange | undefined>();
  const [busy, setBusy] = useState(false);
  const [historySearch, setHistorySearch] = useState('');
  const [historyAction, setHistoryAction] = useState<'all' | RoleAssignmentHistoryItem['action']>('all');
  const [historyLevel, setHistoryLevel] = useState<'all' | RoleAssignment['escalationLevel']>('all');

  const load = async () => {
    try {
      const [assignmentItems, historyItems] = await Promise.all([paApi.getRoleAssignments(), paApi.getRoleAssignmentHistory()]);
      setAssignments(assignmentItems);
      setHistory(historyItems);
    } catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to load role assignments'); }
  };
  useEffect(() => { void load(); }, []);

  const openCreate = () => { setEditing(null); setDraft(emptyAssignment); setDialogOpen(true); };
  const openEdit = (assignment: RoleAssignment) => { setEditing(assignment); setDraft({ escalationLevel: assignment.escalationLevel, responsibleRole: assignment.responsibleRole, department: assignment.department, effectiveDate: assignment.effectiveDate, expiresAt: assignment.expiresAt }); setDialogOpen(true); };
  const save = async () => {
    if (!draft.responsibleRole.trim() || !draft.department.trim() || !draft.effectiveDate) { toast.error('Complete all required assignment fields.'); return; }
    setBusy(true);
    try {
      if (editing) await paApi.updateRoleAssignment(editing.id, draft); else await paApi.createRoleAssignment(draft);
      toast.success(editing ? 'Role assignment updated.' : 'Role assignment created.');
      setDialogOpen(false);
      await load();
    } catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to save role assignment'); }
    finally { setBusy(false); }
  };
  const retire = async () => {
    if (!retiring) return;
    setBusy(true);
    try { await paApi.retireRoleAssignment(retiring.id); toast.success('Role assignment retired.'); setRetiring(null); await load(); }
    catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to retire role assignment'); }
    finally { setBusy(false); }
  };
  const filteredHistory = useMemo(() => {
    const query = historySearch.trim().toLocaleLowerCase();
    return history.filter((item: RoleAssignmentHistoryItem) => {
      const changedAt = new Date(item.changedAt);
      const matchesSearch = !query || [item.actor, item.newValues.responsibleRole, item.newValues.department].some((value: string) => value.toLocaleLowerCase().includes(query));
      const matchesAction = historyAction === 'all' || item.action === historyAction;
      const matchesLevel = historyLevel === 'all' || item.newValues.escalationLevel === historyLevel;
      const matchesStart = !historyDateRange?.from || changedAt >= new Date(historyDateRange.from.getFullYear(), historyDateRange.from.getMonth(), historyDateRange.from.getDate());
      const matchesEnd = !historyDateRange?.to || changedAt <= new Date(historyDateRange.to.getFullYear(), historyDateRange.to.getMonth(), historyDateRange.to.getDate(), 23, 59, 59, 999);
      return matchesSearch && matchesAction && matchesLevel && matchesStart && matchesEnd;
    });
  }, [history, historyAction, historyDateRange, historyLevel, historySearch]);
  const hasHistoryFilters = Boolean(historySearch) || historyAction !== 'all' || historyLevel !== 'all' || Boolean(historyDateRange?.from);
  const clearHistoryFilters = () => { setHistorySearch(''); setHistoryAction('all'); setHistoryLevel('all'); setHistoryDateRange(undefined); };

  return <>
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div><CardTitle>Role assignments</CardTitle><CardDescription>Manage effective-dated escalation ownership. Overlapping assignments for the same level and department are blocked.</CardDescription></div>
        <Button onClick={openCreate}><Plus className="size-4" />New assignment</Button>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="border-b text-left"><th className="pb-3">Level</th><th className="pb-3">Responsible role</th><th className="pb-3">Department</th><th className="pb-3">Effective period</th><th className="pb-3">Status</th><th className="pb-3 text-right">Actions</th></tr></thead><tbody>
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><History className="size-5" />Assignment change history</CardTitle>
        <CardDescription>Review who changed routing ownership, when it changed, and the values before and after each action.</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="mb-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_180px_180px_240px_auto]">
          <div className="relative"><Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input aria-label="Search assignment history" className="pl-9" placeholder="Search role, department, or actor" value={historySearch} onChange={(event: ChangeEvent<HTMLInputElement>) => setHistorySearch(event.target.value)} /></div>
          <Select value={historyAction} onValueChange={(value: 'all' | RoleAssignmentHistoryItem['action']) => setHistoryAction(value)}><SelectTrigger aria-label="Filter by action"><SelectValue placeholder="All actions" /></SelectTrigger><SelectContent><SelectItem value="all">All actions</SelectItem><SelectItem value="CREATED">Created</SelectItem><SelectItem value="UPDATED">Updated</SelectItem><SelectItem value="RETIRED">Retired</SelectItem></SelectContent></Select>
          <Select value={historyLevel} onValueChange={(value: 'all' | RoleAssignment['escalationLevel']) => setHistoryLevel(value)}><SelectTrigger aria-label="Filter by escalation level"><SelectValue placeholder="All levels" /></SelectTrigger><SelectContent><SelectItem value="all">All levels</SelectItem><SelectItem value="LEVEL_1">Level 1</SelectItem><SelectItem value="LEVEL_2">Level 2</SelectItem></SelectContent></Select>
          <Popover><PopoverTrigger asChild><Button variant="outline" className="justify-start font-normal" aria-label="Filter history by date range"><CalendarDays className="size-4" />{historyDateRange?.from ? (historyDateRange.to ? `${format(historyDateRange.from, 'MMM d, yyyy')} – ${format(historyDateRange.to, 'MMM d, yyyy')}` : `From ${format(historyDateRange.from, 'MMM d, yyyy')}`) : 'All dates'}</Button></PopoverTrigger><PopoverContent className="w-auto p-0" align="end"><Calendar mode="range" selected={historyDateRange} onSelect={setHistoryDateRange} numberOfMonths={2} initialFocus /></PopoverContent></Popover>
          <Button variant="outline" onClick={clearHistoryFilters} disabled={!hasHistoryFilters}><X className="size-4" />Clear</Button>
        </div>
        <div className="mb-3 text-sm text-muted-foreground">Showing {filteredHistory.length} of {history.length} changes</div>
        <div className="space-y-3">
          {filteredHistory.map((item: RoleAssignmentHistoryItem) => {
            const changedFields = item.previousValues ? (['responsibleRole', 'department', 'escalationLevel', 'effectiveDate', 'expiresAt'] as const).filter((field: keyof RoleAssignment) => item.previousValues?.[field] !== item.newValues[field]) : [];
            return <div key={item.id} className="rounded-lg border bg-card p-4 text-card-foreground"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2"><Badge variant={item.action === 'RETIRED' ? 'secondary' : 'outline'}>{item.action}</Badge><span className="font-medium">{item.newValues.responsibleRole}</span></div><span className="text-sm text-muted-foreground">{item.changedAt} · {item.actor}</span></div><p className="mt-2 text-sm">{item.newValues.escalationLevel.replace('_', ' ')} · {item.newValues.department} · {item.newValues.effectiveDate} – {item.newValues.expiresAt ?? 'Ongoing'}</p>{changedFields.length > 0 && <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">{changedFields.map((field: keyof RoleAssignment) => <div key={field} className="rounded-md bg-muted p-2 text-muted-foreground"><span className="font-medium text-foreground">{field.replace(/([A-Z])/g, ' $1')}:</span> {String(item.previousValues?.[field] ?? 'None')} → {String(item.newValues[field] ?? 'None')}</div>)}</div>}</div>;
          })}
          {history.length === 0 && <div className="bg-muted p-8 text-center text-muted-foreground"><History className="mx-auto mb-2 size-6" />No assignment changes recorded yet.</div>}
          {history.length > 0 && filteredHistory.length === 0 && <div className="bg-muted p-8 text-center text-muted-foreground"><Search className="mx-auto mb-2 size-6" /><p>No assignment changes match these filters.</p><Button className="mt-3" variant="outline" onClick={clearHistoryFilters}>Clear filters</Button></div>}
        </div>
      </CardContent>
    </Card>
          {assignments.map((assignment: RoleAssignment) => { const status = getStatus(assignment); return <tr key={assignment.id} className="border-b last:border-0"><td className="py-3 pr-4"><Badge variant="outline">{assignment.escalationLevel.replace('_', ' ')}</Badge></td><td className="py-3 pr-4 font-medium">{assignment.responsibleRole}</td><td className="py-3 pr-4">{assignment.department}</td><td className="py-3 pr-4">{assignment.effectiveDate} – {assignment.expiresAt ?? 'Ongoing'}</td><td className="py-3 pr-4"><Badge variant={status === 'Active' ? 'default' : 'secondary'}>{status}</Badge></td><td className="py-3"><div className="flex justify-end gap-2"><Button size="sm" variant="outline" onClick={() => openEdit(assignment)}><Pencil className="size-4" />Edit</Button>{status === 'Active' && <Button size="sm" variant="destructive" onClick={() => setRetiring(assignment)}>Retire</Button>}</div></td></tr>; })}
        </tbody></table>{assignments.length === 0 && <div className="bg-muted p-8 text-center text-muted-foreground"><UserRoundCog className="mx-auto mb-2 size-6" />No role assignments configured.</div>}</div>
      </CardContent>
    </Card>
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}><DialogContent><DialogHeader><DialogTitle>{editing ? 'Edit role assignment' : 'Create role assignment'}</DialogTitle><DialogDescription>Assignment dates control which role the Escalation Agent recommends for a department and level.</DialogDescription></DialogHeader><div className="grid gap-4 py-2"><div className="grid gap-2"><Label>Escalation level</Label><Select value={draft.escalationLevel} onValueChange={(value: 'LEVEL_1' | 'LEVEL_2') => setDraft((current: RoleAssignmentPayload) => ({ ...current, escalationLevel: value }))}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="LEVEL_1">Level 1</SelectItem><SelectItem value="LEVEL_2">Level 2</SelectItem></SelectContent></Select></div><div className="grid gap-2"><Label htmlFor="responsible-role">Responsible role</Label><Input id="responsible-role" value={draft.responsibleRole} onChange={(event: ChangeEvent<HTMLInputElement>) => setDraft((current: RoleAssignmentPayload) => ({ ...current, responsibleRole: event.target.value }))} /></div><div className="grid gap-2"><Label htmlFor="assignment-department">Department</Label><Input id="assignment-department" value={draft.department} onChange={(event: ChangeEvent<HTMLInputElement>) => setDraft((current: RoleAssignmentPayload) => ({ ...current, department: event.target.value }))} /></div><div className="grid gap-4 sm:grid-cols-2"><div className="grid gap-2"><Label htmlFor="assignment-effective">Effective date</Label><Input id="assignment-effective" placeholder="YYYY-MM-DD" value={draft.effectiveDate} onChange={(event: ChangeEvent<HTMLInputElement>) => setDraft((current: RoleAssignmentPayload) => ({ ...current, effectiveDate: event.target.value }))} /></div><div className="grid gap-2"><Label htmlFor="assignment-expires">Retirement date</Label><Input id="assignment-expires" placeholder="Optional YYYY-MM-DD" value={draft.expiresAt ?? ''} onChange={(event: ChangeEvent<HTMLInputElement>) => setDraft((current: RoleAssignmentPayload) => ({ ...current, expiresAt: event.target.value || null }))} /></div></div></div><DialogFooter><Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button><Button onClick={() => void save()} disabled={busy}>{busy ? 'Saving…' : 'Save assignment'}</Button></DialogFooter></DialogContent></Dialog>
    <AlertDialog open={Boolean(retiring)} onOpenChange={(open: boolean) => { if (!open && !busy) setRetiring(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Retire role assignment?</AlertDialogTitle><AlertDialogDescription>{retiring?.responsibleRole} will stop being the active {retiring?.escalationLevel.replace('_', ' ')} recipient for {retiring?.department} today. Historical decisions remain unchanged.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel><AlertDialogAction disabled={busy} onClick={(event: React.MouseEvent<HTMLButtonElement>) => { event.preventDefault(); void retire(); }}>{busy ? 'Retiring…' : 'Retire assignment'}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </>;
}
