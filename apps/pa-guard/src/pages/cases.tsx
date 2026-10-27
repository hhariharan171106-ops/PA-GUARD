import { useState, type ChangeEvent } from 'react';
import { Plus, Search } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { CaseTable } from '@/components/case-table';
import { usePa } from '@/lib/pa-context';
import type { PaCase } from '@/lib/pa-data';
import { useAuth } from '@/lib/auth-context';

export default function CasesPage() {
  const { cases, casesError, addCase } = usePa(); const { user } = useAuth(); const [query, setQuery] = useState(''); const [risk, setRisk] = useState('all'); const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ patientReference: '', payer: '', service: '', deadline: '', coordinator: 'Maya Chen' });
  const filtered = cases.filter((item: PaCase) => `${item.id} ${item.patientReference} ${item.payer} ${item.service}`.toLowerCase().includes(query.toLowerCase()) && (risk === 'all' || item.risk === risk));
  const create = async () => { if (!form.patientReference || !form.payer || !form.service || !form.deadline) return; await addCase(form); setOpen(false); };
  const noCasesMessage = user?.role === 'SPECIALIST'
    ? 'No cases are assigned to you. Ask your supervisor or manager to run the existing work allocation workflow.'
    : user?.role === 'SUPERVISOR'
      ? 'No cases are assigned to your supervisor queue. Ask a manager to review case allocation.'
      : 'No cases are available.';
  return <div className="space-y-5"><div className="flex flex-wrap items-end justify-between gap-3"><div><h1 className="text-2xl font-semibold">Case management</h1><p className="text-sm text-muted-foreground">Create, search, filter, and update authorization cases.</p></div><Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><Plus className="size-4" />New case</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>Create authorization case</DialogTitle></DialogHeader><div className="grid gap-4">{(['patientReference','payer','service','deadline'] as const).map((field) => <div key={field} className="grid gap-2"><Label htmlFor={field}>{field === 'patientReference' ? 'Patient reference' : field.charAt(0).toUpperCase()+field.slice(1)}</Label><Input id={field} value={form[field]} onChange={(event: ChangeEvent<HTMLInputElement>) => setForm({ ...form, [field]: event.target.value })} placeholder={field === 'deadline' ? 'Oct 14, 4:00 PM' : ''} /></div>)}<Button onClick={create}>Create case</Button></div></DialogContent></Dialog></div>{casesError ? <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">Cases could not be loaded: {casesError}</p> : cases.length === 0 ? <p className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">{noCasesMessage}</p> : null}<div className="flex flex-wrap gap-3"><div className="relative min-w-64 flex-1"><Search className="absolute left-3 top-2.5 size-4" /><Input className="pl-9" placeholder="Search case, patient reference, payer..." value={query} onChange={(event: ChangeEvent<HTMLInputElement>) => setQuery(event.target.value)} /></div><Select value={risk} onValueChange={setRisk}><SelectTrigger className="w-48"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All SLA risks</SelectItem>{['ON TRACK','AT RISK','BREACHED','COMPLETE'].map((item: string) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></div><CaseTable cases={filtered} /></div>;
}
