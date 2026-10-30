import { UNAUTHORIZED_EVENT, apiUrl, notifyUnauthorized } from '@/infrastructure/api';

export interface SessionUser {
  id: string;
  username: string;
}

/** Thrown when the server says the session is gone, so callers can re-gate. */
export class UnauthorizedError extends Error {
  constructor() {
    super('Not authenticated');
    this.name = 'UnauthorizedError';
  }
}

// Re-exported so callers of this module need not know about the transport layer.
export { UNAUTHORIZED_EVENT, notifyUnauthorized };

async function readJson<T>(response: Response): Promise<T | null> {
  if (response.status === 401) return null;
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  return (body?.data ?? null) as T | null;
}

export async function fetchSession(): Promise<SessionUser | null> {
  const response = await fetch(apiUrl('/auth/me'), { credentials: 'same-origin' });
  return readJson<SessionUser>(response);
}

export async function signIn(username: string, password: string): Promise<SessionUser> {
  const response = await fetch(apiUrl('/auth/login'), {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  if (response.status === 401) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body?.detail || 'Wrong username or password.');
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  return body.data as SessionUser;
}

export async function signOut(): Promise<void> {
  await fetch(apiUrl('/auth/logout'), { method: 'POST', credentials: 'same-origin' });
}
