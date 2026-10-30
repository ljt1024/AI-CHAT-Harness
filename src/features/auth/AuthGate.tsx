import type { ReactNode } from 'react';
import { useAuth } from '@/app/providers/AuthContext';
import { t } from '@/app/i18n';
import LoginView from './LoginView';
import './auth.css';

/**
 * Holds every page behind a session. The gate is deliberately the only place
 * that decides whether a page may render: showing a page and hiding its data
 * with a 401 banner would leave the interface half alive under an expired cookie.
 */
export default function AuthGate({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  if (status === 'loading') {
    return <div className="login-page"><p className="login-loading">{t('auth.checking')}</p></div>;
  }
  if (status === 'anonymous') return <LoginView />;
  return <>{children}</>;
}
