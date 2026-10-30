import { UNAUTHORIZED_EVENT, apiUrl, notifyUnauthorized } from '@/infrastructure/api';

export interface SessionUser {
  id: string;
  username: string;
}

/** How much anonymous trial is left. `allowed: false` means the trial is off. */
export interface GuestBudget {
  allowed: boolean;
  turnsLeft: number;
  turnsTotal: number;
}

export interface SessionState {
  user: SessionUser | null;
  guest: GuestBudget;
}

const NO_TRIAL: GuestBudget = { allowed: false, turnsLeft: 0, turnsTotal: 0 };

/** Thrown when the server says the session is gone, so callers can re-gate. */
export class UnauthorizedError extends Error {
  constructor() {
    super('Not authenticated');
    this.name = 'UnauthorizedError';
  }
}

// Re-exported so callers of this module need not know about the transport layer.
export { UNAUTHORIZED_EVENT, notifyUnauthorized };

function readBudget(raw: unknown): GuestBudget {
  if (!raw || typeof raw !== 'object') return NO_TRIAL;
  const value = raw as Partial<GuestBudget>;
  return {
    allowed: Boolean(value.allowed),
    turnsLeft: typeof value.turnsLeft === 'number' ? value.turnsLeft : 0,
    turnsTotal: typeof value.turnsTotal === 'number' ? value.turnsTotal : 0,
  };
}

/**
 * One request answers both questions the interface asks at startup: is there an
 * account, and how much of the anonymous trial is left. Splitting them would
 * cost a second round trip and let the two answers disagree.
 */
export async function fetchSession(): Promise<SessionState> {
  const response = await fetch(apiUrl('/auth/me'), { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  return {
    user: (body?.data ?? null) as SessionUser | null,
    guest: readBudget(body?.guest),
  };
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
