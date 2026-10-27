import { NavLink, Outlet } from 'react-router-dom';
import { Activity, Archive, BookOpenCheck, Bot, ClipboardList, CloudOff, FileCheck2, Files, Gauge, Settings, ShieldCheck, ShieldEllipsis, Siren, UserCheck, Users } from 'lucide-react';
import { usePa } from '@/lib/pa-context';
import { useAuth } from '@/lib/auth-context';

const links = [
  { to: '/', label: 'Dashboard', icon: Gauge }, { to: '/cases', label: 'Case management', icon: ClipboardList },
  { to: '/agent-center', label: 'Agent conversation', icon: Bot }, { to: '/evidence', label: 'Evidence review', icon: FileCheck2 },
  { to: '/escalations', label: 'Escalation center', icon: Siren }, { to: '/escalation-policies', label: 'Escalation policies', icon: ShieldEllipsis },
  { to: '/policies', label: 'Policy management', icon: BookOpenCheck }, { to: '/payer-requirements', label: 'Payer requirements', icon: Files }, { to: '/settings', label: 'SLA settings', icon: Settings },
  { to: '/audit', label: 'Audit history', icon: Archive },
  { to: '/approvals', label: 'Approval review', icon: UserCheck },
  { to: '/case-allocation', label: 'Case Allocation', icon: ClipboardList },
  { to: '/staff-management', label: 'Staff management', icon: Users },
];
export default function Layout() {
  const { isLoading, isOfflineDemo } = usePa();
  const { user, logout } = useAuth();
  const roleLinks: Record<string, string[]> = {
    SPECIALIST: ['/', '/cases', '/agent-center', '/evidence', '/escalations', '/approvals'],
    SUPERVISOR: ['/', '/cases', '/agent-center', '/evidence', '/escalations', '/escalation-policies', '/approvals'],
    MANAGER: links.map(link => link.to),
    ADMIN: ['/staff-management', '/audit'],
  };
  const visibleLinks = links.filter(link => (roleLinks[user?.role ?? ''] ?? []).includes(link.to));
  return <div className="min-h-screen bg-background text-foreground lg:grid lg:grid-cols-[248px_1fr]">
    <aside className="bg-sidebar text-sidebar-foreground lg:sticky lg:top-0 lg:h-screen">
      <div className="flex h-16 items-center gap-3 border-b border-sidebar-border px-5"><span className="grid size-9 place-items-center rounded-md bg-sidebar-primary text-sidebar-primary-foreground"><ShieldCheck className="size-5" /></span><div><div className="font-semibold">PA Guard</div><div className="text-xs text-sidebar-foreground">Authorization operations</div></div></div>
      <nav className="flex gap-1 overflow-x-auto p-3 lg:block lg:space-y-1">{visibleLinks.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'} className={({ isActive }: { isActive: boolean }) => `flex shrink-0 items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors ${isActive ? 'bg-sidebar-primary text-sidebar-primary-foreground' : 'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground'}`}><Icon className="size-4" /><span>{label}</span></NavLink>)}</nav>
      <div className="m-3 hidden rounded-md border border-sidebar-border bg-sidebar-accent p-3 text-sidebar-accent-foreground lg:block"><div className="flex items-center gap-2 text-sm font-medium"><Activity className="size-4" /> Review tools</div><p className="mt-1 text-xs">Automated tools help staff review deadlines, documents, escalation, and recovery. Human decisions are required. Notifications in this demo are simulated.</p></div>
    </aside>
    <main><header className="flex h-16 items-center justify-between border-b bg-card px-4 text-card-foreground md:px-6"><div><p className="text-sm font-semibold">Prior authorization command center</p><p className="text-xs text-muted-foreground">Fictional patient data only</p></div><div className="flex items-center gap-4"><div className="text-right"><p className="text-sm font-medium">{user?.display_name}</p><p className="text-xs text-muted-foreground">{user?.role}</p></div><button onClick={()=>void logout()} className="rounded-md border px-3 py-2 text-sm hover:bg-muted">Log out</button></div></header>{isOfflineDemo ? <div className="m-4 flex items-start gap-3 rounded-md bg-secondary p-3 text-sm text-secondary-foreground md:m-6"><CloudOff className="mt-0.5 size-4 shrink-0" /><div><p className="font-semibold">Offline demo mode</p><p>The PA Guard service is unavailable, so this demo is showing fictional cases and documents. Changes and automated reviews are unavailable while offline.</p></div></div> : null}<div className="p-4 md:p-6"><Outlet /></div></main>
  </div>;
}
