import { useEffect, useRef } from 'react';
import { useAuth, type SignInReason } from '@/app/providers/AuthContext';
import { t } from '@/app/i18n';
import LoginForm from './LoginForm';
import './auth.css';

/** One sentence per reason, so the dialog never opens without saying why. */
const COPY = {
  turn_limit: { title: 'auth.turnLimitTitle', body: 'auth.turnLimitBody' },
  daily_limit: { title: 'auth.dailyLimitTitle', body: 'auth.dailyLimitBody' },
  required: { title: 'auth.requiredTitle', body: 'auth.requiredBody' },
  expired: { title: 'auth.expiredTitle', body: 'auth.expiredBody' },
  sign_in: { title: 'auth.title', body: 'auth.subtitle' },
} as const satisfies Record<SignInReason, { title: string; body: string }>;

/**
 * The sign-in step, shown over the page instead of in place of it. Whatever the
 * visitor was doing stays on screen behind the dialog -- a draft message, a
 * half-configured agent -- so signing in resumes rather than restarts.
 */
export default function LoginDialog() {
  const { signInReason, dismissSignIn, guest } = useAuth();
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!signInReason) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dismissSignIn();
    };
    window.addEventListener('keydown', onKey);
    cardRef.current?.focus();
    return () => window.removeEventListener('keydown', onKey);
  }, [signInReason, dismissSignIn]);

  if (!signInReason) return null;
  const copy = COPY[signInReason];
  const canKeepBrowsing = guest.allowed && guest.turnsLeft > 0;

  return (
    <div className="login-backdrop" role="presentation"
      onMouseDown={event => { if (event.target === event.currentTarget) dismissSignIn(); }}>
      <div className="login-card login-dialog" role="dialog" aria-modal="true"
        aria-labelledby="login-dialog-title" tabIndex={-1} ref={cardRef}>
        <button type="button" className="login-close" aria-label={t('auth.close')}
          onClick={dismissSignIn}>×</button>
        <h2 className="login-title" id="login-dialog-title">{t(copy.title)}</h2>
        <p className="login-subtitle">{t(copy.body)}</p>
        <LoginForm onSuccess={dismissSignIn} />
        {canKeepBrowsing && (
          <button type="button" className="login-secondary" onClick={dismissSignIn}>
            {t('auth.continueAsGuest', { count: guest.turnsLeft })}
          </button>
        )}
      </div>
    </div>
  );
}
