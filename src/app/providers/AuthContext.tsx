import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  UNAUTHORIZED_EVENT,
  fetchSession,
  signIn as requestSignIn,
  signOut as requestSignOut,
  type GuestBudget,
  type SessionUser,
} from '@/features/auth/api';
import { GUEST_LIMIT_EVENT } from '@/infrastructure/api';

export type AuthStatus = 'loading' | 'anonymous' | 'ready';

/** Why the sign-in dialog opened; the dialog turns it into one sentence. */
export type SignInReason = 'turn_limit' | 'daily_limit' | 'required' | 'expired' | 'sign_in';

interface AuthValue {
  user: SessionUser | null;
  status: AuthStatus;
  guest: GuestBudget;
  /** Non-null while the sign-in dialog is open. */
  signInReason: SignInReason | null;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  askToSignIn: (reason?: SignInReason) => void;
  dismissSignIn: () => void;
  /** Spend one trial turn locally, so the counter moves with the message. */
  spendTurn: () => void;
}

const NO_TRIAL: GuestBudget = { allowed: false, turnsLeft: 0, turnsTotal: 0 };
const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [guest, setGuest] = useState<GuestBudget>(NO_TRIAL);
  const [signInReason, setSignInReason] = useState<SignInReason | null>(null);

  const applySession = useCallback((session: { user: SessionUser | null; guest: GuestBudget }) => {
    setUser(session.user);
    setGuest(session.guest);
    setStatus(session.user ? 'ready' : 'anonymous');
    if (session.user) setSignInReason(null);
  }, []);

  const reload = useCallback(async () => {
    try {
      applySession(await fetchSession());
    } catch {
      setStatus('anonymous');
    }
  }, [applySession]);

  useEffect(() => { void reload(); }, [reload]);

  // An expired or revoked session no longer throws the page away: it reopens the
  // dialog over whatever the visitor was doing, so a half-written message is not
  // lost to a redirect.
  useEffect(() => {
    const onUnauthorized = () => {
      setUser(null);
      setGuest(current => (current.allowed ? current : NO_TRIAL));
      setStatus('anonymous');
      setSignInReason(reason => reason ?? 'expired');
    };
    const onGuestLimit = () => setSignInReason('turn_limit');
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    window.addEventListener(GUEST_LIMIT_EVENT, onGuestLimit);
    return () => {
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
      window.removeEventListener(GUEST_LIMIT_EVENT, onGuestLimit);
    };
  }, []);

  const signIn = useCallback(async (username: string, password: string) => {
    await requestSignIn(username, password);
    // Re-read rather than trusting the login response: signing in may have
    // adopted a trial transcript, and the budget has to disappear with it.
    await reload();
  }, [reload]);

  const signOut = useCallback(async () => {
    try {
      await requestSignOut();
    } finally {
      setUser(null);
      setSignInReason(null);
      await reload();
    }
  }, [reload]);

  const askToSignIn = useCallback((reason: SignInReason = 'required') => {
    // First reason wins: a limit dialog must not be relabelled by the 401 that a
    // background request happens to produce while it is open.
    setSignInReason(current => current ?? reason);
  }, []);
  const dismissSignIn = useCallback(() => setSignInReason(null), []);
  const spendTurn = useCallback(() => {
    setGuest(current => (current.allowed
      ? { ...current, turnsLeft: Math.max(0, current.turnsLeft - 1) }
      : current));
  }, []);

  const value = useMemo(() => ({
    user, status, guest, signInReason, signIn, signOut, askToSignIn, dismissSignIn, spendTurn,
  }), [user, status, guest, signInReason, signIn, signOut, askToSignIn, dismissSignIn, spendTurn]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
