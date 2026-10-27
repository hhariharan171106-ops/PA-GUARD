export const AUTH_TOKEN_KEY = 'pa-guard-session-token';

export function getAuthToken(): string | null {
  return typeof window === 'undefined' ? null : window.sessionStorage.getItem(AUTH_TOKEN_KEY);
}

export function saveAuthToken(token: string): void {
  window.sessionStorage.setItem(AUTH_TOKEN_KEY, token);
}

export function clearAuthToken(): void {
  window.sessionStorage.removeItem(AUTH_TOKEN_KEY);
}

export function notifyAuthExpired(): void {
  window.dispatchEvent(new Event('pa-auth-expired'));
}
