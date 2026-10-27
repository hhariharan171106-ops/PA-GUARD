import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { API_BASE_URL } from '@/lib/pa-api';
import { clearAuthToken, getAuthToken, saveAuthToken } from '@/lib/auth-session';
import { queryClient } from '@/lib/query-client';
import { formatApiErrorMessage } from '@/lib/user-messages';

export type PaRole = 'SPECIALIST' | 'SUPERVISOR' | 'MANAGER' | 'ADMIN';
export type AuthUser = { id: number; username: string; display_name: string; role: PaRole };
type AuthContextValue = {
  user: AuthUser | null;
  token: string | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<AuthUser>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

async function authRequest<T>(path: string, token?: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new Error('PA Guard server could not be reached. Check that the backend is running.');
  }
  if (!response.ok) {
    const payload = await response.json().catch((): { detail?: string } => ({}));
    throw new Error(formatApiErrorMessage(response.status, payload.detail, 'sign in'));
  }
  return response.json() as Promise<T>;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [token, setToken] = useState<string | null>(() => getAuthToken());
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const clearSession = useCallback((redirect: boolean) => {
    clearAuthToken();
    queryClient.clear();
    setToken(null);
    setUser(null);
    if (redirect) navigate('/login', { replace: true, state: { expired: true } });
  }, [navigate]);

  useEffect(() => {
    let active = true;
    const storedToken = getAuthToken();
    if (!storedToken) { setLoading(false); return () => { active = false; }; }
    authRequest<AuthUser>('/auth/me', storedToken).then((identity) => {
      if (active) { setToken(storedToken); setUser(identity); }
    }).catch(() => {
      if (active) { clearAuthToken(); setToken(null); setUser(null); }
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const expired = () => clearSession(true);
    window.addEventListener('pa-auth-expired', expired);
    return () => window.removeEventListener('pa-auth-expired', expired);
  }, [clearSession]);

  const login = useCallback(async (username: string, password: string) => {
    try {
      const result = await authRequest<{ token: string }>('/auth/login', undefined, { username, password });
      queryClient.clear();
      saveAuthToken(result.token);
      const identity = await authRequest<AuthUser>('/auth/me', result.token);
      setToken(result.token);
      setUser(identity);
      return identity;
    } catch (error) {
      clearAuthToken(); setToken(null); setUser(null);
      throw error;
    }
  }, []);

  const logout = useCallback(async () => {
    const current = token;
    clearSession(false);
    if (current) {
      try { await authRequest<{ ok: boolean }>('/auth/logout', current, {}); } catch { /* Local state is cleared even when the session already expired. */ }
    }
    navigate('/login', { replace: true });
  }, [clearSession, navigate, token]);

  const value = useMemo(() => ({ user, token, loading, login, logout }), [user, token, loading, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}

