import { UNAUTHORIZED_EVENT, apiUrl, notifyUnauthorized } from '@/infrastructure/api';

export interface SessionUser {
  id: string;
  username: string;
  /** What the sidebar shows: a chosen name, or the masked address. */
  displayName?: string;
  /** Masked server-side (a***@example.com). Empty until an address is bound. */
  email?: string;
  /** Masked server-side (138****8000). Only accounts from the SMS door have one. */
  phone?: string;
}

/** What a code request answers: how long it is good for, and when to retry. */
export interface CodeChallenge {
  expiresIn: number;
  cooldown: number;
  /** Present only on a development deployment (console channel with echo on). */
  debugCode?: string;
}

export type CodePurpose = 'login' | 'bind';

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

/**
 * A refusal that carries a reason rather than a sentence: the sender does not
 * know which language the reader uses, and "too many attempts" is a different
 * sentence from "that code is wrong".
 */
export class AuthRequestError extends Error {
  readonly code: string;
  readonly retryAfter: number;

  constructor(code: string, message = '', retryAfter = 0) {
    super(message || code);
    this.name = 'AuthRequestError';
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

function readFailure(status: number, body: unknown, invalidField: string): AuthRequestError {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === 'object') {
    const value = detail as { code?: string; retryAfter?: number };
    return new AuthRequestError(value.code ?? 'unknown', '', Number(value.retryAfter) || 0);
  }
  // A 422 arrives as a plain sentence from the validator: the identifier is the
  // only field there is to get wrong.
  return new AuthRequestError(status === 422 ? invalidField : 'unknown',
    typeof detail === 'string' ? detail : '');
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

async function postAuth(path: string, body: unknown,
                        invalidField = 'invalid_email'): Promise<unknown> {
  const response = await fetch(apiUrl(path), {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw readFailure(response.status, payload, invalidField);
  return (payload as { data?: unknown } | null)?.data;
}

/**
 * Send a code. Registration and sign-in are the same call from the browser's
 * side: whether this address already has an account is the server's business,
 * and asking it here would turn the endpoint into a way to test for accounts.
 */
export async function requestEmailCode(email: string,
                                       purpose: CodePurpose = 'login'): Promise<CodeChallenge> {
  return await postAuth('/auth/email/code', { email, purpose }) as CodeChallenge;
}

/** Exchange a code for a session. An unknown address becomes a new account. */
export async function signInWithEmail(email: string, code: string): Promise<SessionUser> {
  return await postAuth('/auth/email/verify', { email, code }) as SessionUser;
}

/** Attach an address to the account that is already signed in. */
export async function bindEmail(email: string, code: string): Promise<SessionUser> {
  return await postAuth('/auth/email', { email, code }) as SessionUser;
}
