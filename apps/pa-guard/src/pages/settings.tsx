import { useEffect, useState, type ChangeEvent } from 'react';
import { endOfDay, format, isValid, parseISO, startOfDay } from 'date-fns';
import { CalendarDays, Download, X } from 'lucide-react';
import type { DateRange } from 'react-day-picker';
import { Button } from '@/components/ui/button';
import { Calendar } from '@/components/ui/calendar';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { toast } from 'sonner';
import { usePa } from '@/lib/pa-context';
import type { SettingsHistoryItem, SlaSettings } from '@/lib/pa-api';

const fields: Array<{ key: keyof SlaSettings; label: string; description: string }> = [
  { key: 'warningHours', label: 'Warning window (hours)', description: 'Cases inside this window are flagged for closer monitoring.' },
  { key: 'urgentHours', label: 'Urgent window (hours)', description: 'Cases inside this window receive high SLA risk.' },
  { key: 'escalationOneHours', label: 'Level 1 escalation (hours)', description: 'Initial operational escalation threshold.' },
  { key: 'escalationTwoHours', label: 'Level 2 escalation (hours)', description: 'Final escalation threshold before breach.' },
];
const weights: Array<{ key: keyof SlaSettings; label: string; description: string }> = [
  { key: 'remainingTimeWeight', label: 'Remaining time', description: 'Emphasizes proximity to or passage of the SLA deadline.' },
  { key: 'priorityWeight', label: 'Case priority', description: 'Emphasizes the coordinator-assigned case priority.' },
  { key: 'evidenceWeight', label: 'Evidence dependencies', description: 'Emphasizes missing or unverified required documents.' },
];

export default function SettingsPage() {
  const { settings, settingsHistory, setSettings } = usePa();
  const [draft, setDraft] = useState<SlaSettings>(settings);
  const [historyQuery, setHistoryQuery] = useState('');
  const [historyField, setHistoryField] = useState('all');
  const [historyDateRange, setHistoryDateRange] = useState<DateRange | undefined>();
  useEffect(() => setDraft(settings), [settings]);
  const totalWeight = draft.remainingTimeWeight + draft.priorityWeight + draft.evidenceWeight;
  const normalizedQuery = historyQuery.trim().toLowerCase();
  const historyFields = Array.from(new Set(settingsHistory.map((item: SettingsHistoryItem) => item.fieldName))).filter((fieldName: string) => fieldName);
  const filteredHistory = settingsHistory.filter((item: SettingsHistoryItem) => {
    const matchesField = historyField === 'all' || item.fieldName === historyField;
    const changedDate = parseISO(item.changedAt);
    const matchesStartDate = !historyDateRange?.from || (isValid(changedDate) && changedDate >= startOfDay(historyDateRange.from));
    const matchesEndDate = !historyDateRange?.to || (isValid(changedDate) && changedDate <= endOfDay(historyDateRange.to));
    const searchableText = `${item.actor} ${item.fieldName.replaceAll('_', ' ')} ${item.previousValue} ${item.newValue} ${item.changedAt}`.toLowerCase();
    return matchesField && matchesStartDate && matchesEndDate && (!normalizedQuery || searchableText.includes(normalizedQuery));
  });
  const hasHistoryFilters = historyField !== 'all' || historyQuery.length > 0 || Boolean(historyDateRange?.from);
  const dateRangeLabel = historyDateRange?.from
    ? historyDateRange.to
      ? `${format(historyDateRange.from, 'MMM d, yyyy')} – ${format(historyDateRange.to, 'MMM d, yyyy')}`
      : `From ${format(historyDateRange.from, 'MMM d, yyyy')}`
    : 'All dates';
  const update = (key: keyof SlaSettings, event: ChangeEvent<HTMLInputElement>) => setDraft((current: SlaSettings) => ({ ...current, [key]: Number(event.target.value) }));
  const save = async () => { await setSettings(draft); };
  const clearHistoryFilters = () => { setHistoryQuery(''); setHistoryField('all'); setHistoryDateRange(undefined); };
  const exportFilteredHistory = () => {
    if (filteredHistory.length === 0) {
      toast.info('No settings history matches the current filters.');
      return;
    }
    const escapeCsvCell = (value: string | number) => {
      const text = String(value);
      const safeText = /^[=+\-@]/.test(text) ? `'${text}` : text;
      return `"${safeText.replaceAll('"', '""')}"`;
    };
    const header = ['Changed', 'Setting', 'Previous value', 'New value', 'Actor'];
    const rows = filteredHistory.map((item: SettingsHistoryItem) => [item.changedAt, item.fieldName.replaceAll('_', ' '), item.previousValue, item.newValue, item.actor]);
    const csv = [header, ...rows].map((row: Array<string | number>) => row.map(escapeCsvCell).join(',')).join('\r\n');
    const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `pa-guard-settings-history-${format(new Date(), 'yyyy-MM-dd')}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast.success(`Exported ${filteredHistory.length} settings history ${filteredHistory.length === 1 ? 'record' : 'records'}.`);
  };
  return <div className="max-w-4xl space-y-5">
    <div><h1 className="text-2xl font-semibold">SLA settings</h1><p className="text-sm text-muted-foreground">Configure operational thresholds and transparent LangGraph scoring.</p></div>
    <Card><CardHeader><CardTitle>Operational thresholds</CardTitle><CardDescription>Define when cases move into warning, urgent, and escalation windows.</CardDescription></CardHeader><CardContent className="grid gap-5 sm:grid-cols-2">{fields.map((field: { key: keyof SlaSettings; label: string; description: string }) => <div className="grid gap-2" key={field.key}><Label htmlFor={field.key}>{field.label}</Label><Input id={field.key} type="number" min="0" value={draft[field.key]} onChange={(event: ChangeEvent<HTMLInputElement>) => update(field.key, event)} /><p className="text-xs text-muted-foreground">{field.description}</p></div>)}</CardContent></Card>
    <Card><CardHeader><CardTitle>SLA scoring weights</CardTitle><CardDescription>Set the relative influence of each factor. Weights must total 100%.</CardDescription></CardHeader><CardContent className="space-y-5"><div className="grid gap-5 sm:grid-cols-3">{weights.map((field: { key: keyof SlaSettings; label: string; description: string }) => <div className="grid gap-2" key={field.key}><Label htmlFor={field.key}>{field.label}</Label><div className="relative"><Input id={field.key} className="pr-9" type="number" min="0" max="100" value={draft[field.key]} onChange={(event: ChangeEvent<HTMLInputElement>) => update(field.key, event)} /><span className="absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">%</span></div><p className="text-xs text-muted-foreground">{field.description}</p></div>)}</div><div className={totalWeight === 100 ? 'rounded-md bg-secondary p-3 text-sm text-secondary-foreground' : 'rounded-md bg-destructive p-3 text-sm text-destructive-foreground'}>Total weight: <strong>{totalWeight}%</strong>{totalWeight === 100 ? ' — ready to save.' : ' — adjust the factors to equal 100%.'}</div></CardContent></Card>
    <Card>
      <CardHeader><CardTitle>Settings change history</CardTitle><CardDescription>Immutable before-and-after values recorded for operational traceability.</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_220px_260px_auto]">
          <div className="grid gap-2"><Label htmlFor="history-search">Search history</Label><Input id="history-search" value={historyQuery} onChange={(event: ChangeEvent<HTMLInputElement>) => setHistoryQuery(event.target.value)} placeholder="Actor, setting, or value" /></div>
          <div className="grid gap-2"><Label htmlFor="history-field">Setting</Label><Select value={historyField} onValueChange={setHistoryField}><SelectTrigger id="history-field"><SelectValue placeholder="All settings" /></SelectTrigger><SelectContent><SelectItem value="all">All settings</SelectItem>{historyFields.filter((fieldName: string) => fieldName).map((fieldName: string) => <SelectItem key={fieldName} value={fieldName}>{fieldName.replaceAll('_', ' ')}</SelectItem>)}</SelectContent></Select></div>
          <div className="grid gap-2">
            <Label>Date range</Label>
            <Popover>
              <PopoverTrigger asChild><Button type="button" variant="outline" className="w-full justify-start font-normal"><CalendarDays className="h-4 w-4" />{dateRangeLabel}</Button></PopoverTrigger>
              <PopoverContent className="w-auto p-0" align="start"><Calendar mode="range" selected={historyDateRange} onSelect={setHistoryDateRange} numberOfMonths={2} initialFocus /></PopoverContent>
            </Popover>
          </div>
          <div className="flex items-end"><Button className="w-full lg:w-auto" type="button" variant="outline" onClick={clearHistoryFilters} disabled={!hasHistoryFilters}><X className="h-4 w-4" />Clear</Button></div>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4"><div className="flex items-center gap-3"><p className="text-sm text-muted-foreground">Showing {filteredHistory.length} of {settingsHistory.length} changes</p>{hasHistoryFilters && <p className="text-sm font-medium">Filters applied</p>}</div><Button type="button" variant="outline" onClick={exportFilteredHistory} disabled={filteredHistory.length === 0}><Download className="h-4 w-4" />Export CSV</Button></div>
        {settingsHistory.length === 0 ? <p className="text-sm text-muted-foreground">No settings changes recorded yet.</p> : filteredHistory.length === 0 ? <div className="rounded-md bg-muted p-6 text-center text-muted-foreground"><p className="font-medium text-foreground">No matching changes</p><p className="mt-1 text-sm">Adjust or clear the history filters.</p></div> : <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="border-b text-left"><th className="pb-3 font-medium">Changed</th><th className="pb-3 font-medium">Setting</th><th className="pb-3 font-medium">Previous</th><th className="pb-3 font-medium">New</th><th className="pb-3 font-medium">Actor</th></tr></thead><tbody>{filteredHistory.map((item: SettingsHistoryItem) => <tr key={item.id} className="border-b last:border-0"><td className="py-3 pr-4">{item.changedAt}</td><td className="py-3 pr-4 font-medium">{item.fieldName.replaceAll('_', ' ')}</td><td className="py-3 pr-4">{item.previousValue}</td><td className="py-3 pr-4">{item.newValue}</td><td className="py-3">{item.actor}</td></tr>)}</tbody></table></div>}
      </CardContent>
    </Card>
    <div className="flex items-center justify-between gap-4"><p className="text-sm text-muted-foreground">Changes affect future recommendation runs only and never modify case outcomes.</p><Button onClick={save} disabled={totalWeight !== 100}>Save SLA settings</Button></div>
    <div className="rounded-md bg-secondary p-3 text-sm text-secondary-foreground">Settings are persisted by FastAPI and applied by the connected deterministic LangGraph SLA agent. Recommendations remain subject to human review.</div>
  </div>;
}
