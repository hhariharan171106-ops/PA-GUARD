import { useState, type FormEvent } from 'react';
import { Eye, EyeOff, LoaderCircle, ShieldCheck } from 'lucide-react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth, type PaRole } from '@/lib/auth-context';

const roleHome: Record<PaRole, string> = {
  SPECIALIST: '/approvals', SUPERVISOR: '/approvals', MANAGER: '/approvals', ADMIN: '/staff-management',
};

export default function LoginPage() {
  const { user, loading: sessionLoading, login } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const expired = Boolean((location.state as { expired?: boolean } | null)?.expired);

  if (sessionLoading) return <main className="grid min-h-screen place-items-center" aria-label="Checking session"><LoaderCircle className="size-7 animate-spin text-primary" /></main>;
  if (user) return <Navigate to={roleHome[user.role] || '/'} replace />;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const identity = await login(username.trim(), password);
      const destination = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
      navigate(destination || roleHome[identity.role], { replace: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to sign in. Check your connection and try again.');
    } finally { setBusy(false); }
  }

  return <main className="flex min-h-screen items-center justify-center bg-background px-4 py-10">
    <section className="w-full max-w-md rounded-2xl border bg-card p-7 shadow-sm sm:p-9">
      <div className="mb-7 flex items-center gap-3"><span className="grid size-12 place-items-center rounded-xl bg-primary text-primary-foreground"><ShieldCheck className="size-7" aria-hidden="true" /></span><div><p className="text-xl font-bold">PA Guard</p><p className="text-sm text-muted-foreground">Authorization operations</p></div></div>
      <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
      <p className="mt-2 text-sm text-muted-foreground">Use your staff account to access your role-specific work queue.</p>
      {expired && <p role="status" className="mt-4 rounded-md border bg-muted/50 p-3 text-sm">Your session expired. Sign in again to continue.</p>}
      <form onSubmit={submit} className="mt-6 space-y-4">
        <div><label htmlFor="username" className="mb-1.5 block text-sm font-medium">Username</label><input id="username" name="username" autoComplete="username" required maxLength={80} value={username} onChange={event=>setUsername(event.target.value)} className="h-11 w-full rounded-md border bg-background px-3 outline-none focus-visible:ring-2 focus-visible:ring-ring" /></div>
        <div><label htmlFor="password" className="mb-1.5 block text-sm font-medium">Password</label><div className="relative"><input id="password" name="password" type={showPassword?'text':'password'} autoComplete="current-password" required maxLength={200} value={password} onChange={event=>setPassword(event.target.value)} className="h-11 w-full rounded-md border bg-background px-3 pr-12 outline-none focus-visible:ring-2 focus-visible:ring-ring"/><button type="button" onClick={()=>setShowPassword(value=>!value)} aria-label={showPassword?'Hide password':'Show password'} aria-pressed={showPassword} className="absolute inset-y-0 right-0 grid w-11 place-items-center rounded-r-md text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{showPassword?<EyeOff className="size-4" aria-hidden="true"/>:<Eye className="size-4" aria-hidden="true"/>}</button></div></div>
        {error&&<p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">{error}</p>}
        <button type="submit" disabled={busy||!username.trim()||!password} className="flex h-11 w-full items-center justify-center gap-2 rounded-md bg-primary px-4 font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-60">{busy&&<LoaderCircle className="size-4 animate-spin" aria-hidden="true"/>}{busy?'Signing in…':'Sign in'}</button>
      </form>
      <p className="mt-6 text-center text-xs text-muted-foreground">Local synthetic-data prototype · Not for real patient information</p>
    </section>
  </main>;
}
