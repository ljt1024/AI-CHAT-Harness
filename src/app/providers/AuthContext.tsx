import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  UNAUTHORIZED_EVENT,
  fetchSession,
  signIn as requestSignIn,
  signOut as requestSignOut,
  type SessionUser,
} from '@/features/auth/api';

export type AuthStatus = 'loading' | 'anonymous' | 'ready';

interface AuthValue {
  user: SessionUser | null;
  status: AuthStatus;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>('loading');

  useEffect(() => {
    let active = true;
    fetchSession()
      .then(session => {
        if (!active) return;
        setUser(session);
        setStatus(session ? 'ready' : 'anonymous');
      })
      .catch(() => { if (active) setStatus('anonymous') });
    return () => { active = false };
  }, []);

  // Any request that comes back 401 (an expired or revoked session) drops the
  // whole UI back to the login form instead of leaving a half-working page.
  useEffect(() => {
    const onUnauthorized = () => { setUser(null); setStatus('anonymous') };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const signIn = useCallback(async (username: string, password: string) => {
    const session = await requestSignIn(username, password);
    setUser(session);
    setStatus('ready');
  }, []);

  const signOut = useCallback(async () => {
    try {
      await requestSignOut();
    } finally {
      setUser(null);
      setStatus('anonymous');
    }
  }, []);

  const value = useMemo(() => ({ user, status, signIn, signOut }), [user, status, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
