import { useEffect, useState, type ChangeEvent } from 'react';
import { Pencil, Plus, UserRoundCog } from 'lucide-react';
import { toast } from 'sonner';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { paApi, type RoleAssignment, type RoleAssignmentPayload } from '@/lib/pa-api';

const emptyAssignment: RoleAssignmentPayload = { escalationLevel: 'LEVEL_1', responsibleRole: '', department: 'Prior Authorization', effectiveDate: '', expiresAt: null };

const getStatus = (assignment: RoleAssignment): 'Scheduled' | 'Active' | 'Retired' => {
  const today = new Date().toISOString().slice(0, 10);
  if (assignment.effectiveDate > today) return 'Scheduled';
  if (assignment.expiresAt && assignment.expiresAt <= today) return 'Retired';
  return 'Active';
};

export function RoleAssignmentManager() {
  const [assignments, setAssignments] = useState<RoleAssignment[]>([]);
  const [draft, setDraft] = useState<RoleAssignmentPayload>(emptyAssignment);
  const [editing, setEditing] = useState<RoleAssignment | null>(null);
  const [retiring, setRetiring] = useState<RoleAssignment | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try { setAssignments(await paApi.getRoleAssignments()); }
    catch (error: unknown) { toast.error(error instanceof Error ? error.message : 'Unable to load role assignments'); }
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

  return <>
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div><CardTitle>Role assignments</CardTitle><CardDescription>Manage effective-dated escalation ownership. Overlapping assignments for the same level and department are blocked.</CardDescription></div>
        <Button onClick={openCreate}><Plus className="size-4" />New assignment</Button>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="border-b text-left"><th className="pb-3">Level</th><th className="pb-3">Responsible role</th><th className="pb-3">Department</th><th className="pb-3">Effective period</th><th className="pb-3">Status</th><th className="pb-3 text-right">Actions</th></tr></thead><tbody>
          {assignments.map((assignment: RoleAssignment) => { const status = getStatus(assignment); return <tr key={assignment.id} className="border-b last:border-0"><td className="py-3 pr-4"><Badge variant="outline">{assignment.escalationLevel.replace('_', ' ')}</Badge></td><td className="py-3 pr-4 font-medium">{assignment.responsibleRole}</td><td className="py-3 pr-4">{assignment.department}</td><td className="py-3 pr-4">{assignment.effectiveDate} – {assignment.expiresAt ?? 'Ongoing'}</td><td className="py-3 pr-4"><Badge variant={status === 'Active' ? 'default' : 'secondary'}>{status}</Badge></td><td className="py-3"><div className="flex justify-end gap-2"><Button size="sm" variant="outline" onClick={() => openEdit(assignment)}><Pencil className="size-4" />Edit</Button>{status === 'Active' && <Button size="sm" variant="destructive" onClick={() => setRetiring(assignment)}>Retire</Button>}</div></td></tr>; })}
        </tbody></table>{assignments.length === 0 && <div className="bg-muted p-8 text-center text-muted-foreground"><UserRoundCog className="mx-auto mb-2 size-6" />No role assignments configured.</div>}</div>
      </CardContent>
    </Card>
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}><DialogContent><DialogHeader><DialogTitle>{editing ? 'Edit role assignment' : 'Create role assignment'}</DialogTitle><DialogDescription>Assignment dates control which role the Escalation Agent recommends for a department and level.</DialogDescription></DialogHeader><div className="grid gap-4 py-2"><div className="grid gap-2"><Label>Escalation level</Label><Select value={draft.escalationLevel} onValueChange={(value: 'LEVEL_1' | 'LEVEL_2') => setDraft((current: RoleAssignmentPayload) => ({ ...current, escalationLevel: value }))}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="LEVEL_1">Level 1</SelectItem><SelectItem value="LEVEL_2">Level 2</SelectItem></SelectContent></Select></div><div className="grid gap-2"><Label htmlFor="responsible-role">Responsible role</Label><Input id="responsible-role" value={draft.responsibleRole} onChange={(event: ChangeEvent<HTMLInputElement>) => setDraft((current: RoleAssignmentPayload) => ({ ...current, responsibleRole: event.target.value }))} /></div><div className="grid gap-2"><Label htmlFor="assignment-department">Department</Label><Input id="assignment-department" value={draft.department} onChange={(event: ChangeEvent<HTMLInputElement>) => setDraft((current: RoleAssignmentPayload) => ({ ...current, department: event.target.value }))} /></div><div className="grid gap-4 sm:grid-cols-2"><div className="grid gap-2"><Label htmlFor="assignment-effective">Effective date</Label><Input id="assignment-effective" placeholder="YYYY-MM-DD" value={draft.effectiveDate} onChange={(event: ChangeEvent<HTMLInputElement>) => setDraft((current: RoleAssignmentPayload) => ({ ...current, effectiveDate: event.target.value }))} /></div><div className="grid gap-2"><Label htmlFor="assignment-expires">Retirement date</Label><Input id="assignment-expires" placeholder="Optional YYYY-MM-DD" value={draft.expiresAt ?? ''} onChange={(event: ChangeEvent<HTMLInputElement>) => setDraft((current: RoleAssignmentPayload) => ({ ...current, expiresAt: event.target.value || null }))} /></div></div></div><DialogFooter><Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button><Button onClick={() => void save()} disabled={busy}>{busy ? 'Saving…' : 'Save assignment'}</Button></DialogFooter></DialogContent></Dialog>
    <AlertDialog open={Boolean(retiring)} onOpenChange={(open: boolean) => { if (!open && !busy) setRetiring(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Retire role assignment?</AlertDialogTitle><AlertDialogDescription>{retiring?.responsibleRole} will stop being the active {retiring?.escalationLevel.replace('_', ' ')} recipient for {retiring?.department} today. Historical decisions remain unchanged.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel><AlertDialogAction disabled={busy} onClick={(event: React.MouseEvent<HTMLButtonElement>) => { event.preventDefault(); void retire(); }}>{busy ? 'Retiring…' : 'Retire assignment'}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </>;
}
