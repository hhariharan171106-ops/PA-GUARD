import { Link } from 'react-router-dom';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import type { PaCase } from '@/lib/pa-data';

const badgeVariant = (value: string) => value === 'BREACHED' || value === 'HIGH' ? 'destructive' as const : value === 'AT RISK' || value === 'MEDIUM' ? 'default' as const : value === 'COMPLETE' || value === 'APPROVED' ? 'secondary' as const : 'outline' as const;
export function CaseTable({ cases }: { cases: PaCase[] }) {
  return <div className="overflow-hidden rounded-md border bg-card text-card-foreground"><Table><TableHeader><TableRow><TableHead>Case</TableHead><TableHead>Patient ref.</TableHead><TableHead>Service</TableHead><TableHead>Status</TableHead><TableHead>SLA risk</TableHead><TableHead>Priority</TableHead><TableHead>Deadline</TableHead><TableHead>Coordinator</TableHead></TableRow></TableHeader><TableBody>{cases.map((item: PaCase) => <TableRow key={item.id}><TableCell><Link className="font-semibold underline-offset-4 hover:underline" to={`/cases/${item.id}`}>{item.id}</Link></TableCell><TableCell>{item.patientReference}</TableCell><TableCell><div>{item.service}</div><div className="text-xs text-muted-foreground">{item.payer}</div></TableCell><TableCell><Badge variant={badgeVariant(item.status)}>{item.status}</Badge></TableCell><TableCell><Badge variant={badgeVariant(item.risk)}>{item.risk}</Badge></TableCell><TableCell><Badge variant={badgeVariant(item.priority)}>{item.priority}</Badge></TableCell><TableCell>{item.deadline}</TableCell><TableCell>{item.coordinator}</TableCell></TableRow>)}</TableBody></Table></div>;
}
