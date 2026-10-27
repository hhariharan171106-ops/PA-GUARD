import { AlertTriangle, Bot, CheckCircle2, Clock3, FileStack, ShieldAlert } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { CaseTable } from '@/components/case-table';
import { usePa } from '@/lib/pa-context';
import type { PaCase } from '@/lib/pa-data';
import { RecoveryAgentPanel } from '@/components/recovery-agent-panel';
import { useAuth } from '@/lib/auth-context';

export default function DashboardPage() {
  const { cases, casesError, runSlaAgent, isRunningSla, isOfflineDemo } = usePa();
  const { user } = useAuth();
  const stats = [
    { label: 'Active cases', value: cases.length, icon: FileStack },
    { label: 'Pending', value: cases.filter((item: PaCase) => !['APPROVED','DENIED'].includes(item.status)).length, icon: Clock3 },
    { label: 'Urgent', value: cases.filter((item: PaCase) => item.priority === 'HIGH').length, icon: AlertTriangle },
    { label: 'SLA breaches', value: cases.filter((item: PaCase) => item.risk === 'BREACHED').length, icon: ShieldAlert },
    { label: 'At SLA risk', value: cases.filter((item: PaCase) => item.risk === 'AT RISK').length, icon: CheckCircle2 },
  ];
  return <div className="space-y-6"><div className="flex flex-wrap items-end justify-between gap-3"><div><h1 className="text-2xl font-semibold">Operations dashboard</h1><p className="text-sm text-muted-foreground">Prioritize deadlines, evidence gaps, and safe workflow recovery.</p></div><Button onClick={runSlaAgent} disabled={isRunningSla || isOfflineDemo} title={isOfflineDemo ? 'Connect to PA Guard to review case deadlines' : undefined}><Bot className="size-4" />{isRunningSla ? 'Checking case deadlines…' : isOfflineDemo ? 'Deadline review unavailable while offline' : 'Run SLA agent'}</Button></div>{casesError ? <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">Cases could not be loaded: {casesError}</p> : null}<div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">{stats.map(({ label, value, icon: Icon }) => <Card key={label}><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">{label}</CardTitle><Icon className="size-4" /></CardHeader><CardContent><div className="text-3xl font-semibold">{value}</div></CardContent></Card>)}</div>{user ? <RecoveryAgentPanel offline={isOfflineDemo} role={user.role} /> : null}<section className="space-y-3"><div><h2 className="text-lg font-semibold">Active case queue</h2><p className="text-sm text-muted-foreground">Case status and SLA risk are tracked independently. Automated recommendations do not change case records.</p></div><CaseTable cases={cases} /></section></div>;
}
