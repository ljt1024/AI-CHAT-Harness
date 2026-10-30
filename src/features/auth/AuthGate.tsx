import type { ReactNode } from 'react';
import { useAuth } from '@/app/providers/AuthContext';
import { t } from '@/app/i18n';
import './auth.css';

/**
 * The page is no longer held hostage to the session.
 *
 * A visitor browses and tries a few real turns; the sign-in dialog appears at the
 * moment an action actually needs an account. This gate now only covers the one
 * instant where we do not yet know who is calling -- and even that is a
 * placeholder, not a login form.
 */
export default function AuthGate({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  if (status === 'loading') {
    return <div className="login-page"><p className="login-loading">{t('auth.checking')}</p></div>;
  }
  return <>{children}</>;
}
