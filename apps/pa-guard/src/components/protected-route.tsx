import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useAuth, type PaRole } from '@/lib/auth-context';

const rolePaths: Record<PaRole, string[]> = {
  SPECIALIST: ['/', '/cases', '/agent-center', '/evidence', '/escalations', '/approvals'],
  SUPERVISOR: ['/', '/cases', '/agent-center', '/evidence', '/escalations', '/escalation-policies', '/approvals'],
  MANAGER: ['*'],
  ADMIN: ['/staff-management', '/audit'],
};

function roleCanOpen(role: PaRole, path: string): boolean {
  const allowed = rolePaths[role] || [];
  return allowed.includes('*') || allowed.some((base) => base === '/' ? path === '/' : path === base || path.startsWith(`${base}/`));
}

export default function ProtectedRoute() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <main className="grid min-h-screen place-items-center" aria-label="Checking sign-in"><div className="size-7 animate-spin rounded-full border-2 border-primary border-t-transparent"/></main>;
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />;
  if (!roleCanOpen(user.role, location.pathname)) return <Navigate to={user.role === 'ADMIN' ? '/staff-management' : '/approvals'} replace />;
  return <Outlet />;
}
