import { useEffect, useRef, useState, type FormEvent } from 'react';
import { t } from '@/app/i18n';
import { useAuth } from '@/app/providers/AuthContext';
import { bindEmail } from './api';
import {
  CodeHint, codeErrorMessage, cooldownLabel, looksLikeEmail, useCode,
} from './verification';
import './auth.css';

/**
 * Binding an address to an account that already exists.
 *
 * This is the one flow the email door does not cover: the account `bootstrap()`
 * created has a password and no address, and its owner should not have to create
 * a second account to get one.
 */
export default function EmailBindDialog({ onClose }: { onClose: () => void }) {
  const { user, refresh } = useAuth();
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const verification = useCode('bind');
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    cardRef.current?.focus();
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const sendCode = async () => {
    setError('');
    try {
      await verification.request(email.trim());
    } catch (failure) {
      setError(codeErrorMessage(failure));
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await bindEmail(email.trim(), code.trim());
      // The masked address lives on the session record, so the sidebar only
      // changes once that record is read again.
      await refresh();
      onClose();
    } catch (failure) {
      setError(codeErrorMessage(failure));
      setBusy(false);
    }
  };

  const emailReady = looksLikeEmail(email);

  return (
    <div className="login-backdrop" role="presentation"
      onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="login-card login-dialog" role="dialog" aria-modal="true"
        aria-labelledby="email-bind-title" tabIndex={-1} ref={cardRef}>
        <button type="button" className="login-close" aria-label={t('auth.close')}
          onClick={onClose}>×</button>
        <h2 className="login-title" id="email-bind-title">{t('auth.bindTitle')}</h2>
        <p className="login-subtitle">
          {user?.email ? t('auth.bound', { email: user.email }) : t('auth.bindBody')}
        </p>
        <form className="login-fields" onSubmit={submit}>
          <label className="login-field">
            <span>{t('auth.email')}</span>
            <input value={email} onChange={event => setEmail(event.target.value)}
              type="email" inputMode="email" autoComplete="email" autoFocus required
              maxLength={254} placeholder={t('auth.emailPlaceholder')} />
          </label>
          <label className="login-field">
            <span>{t('auth.code')}</span>
            <div className="login-code-row">
              <input value={code} onChange={event => setCode(event.target.value)}
                inputMode="numeric" autoComplete="one-time-code" required maxLength={8}
                placeholder={t('auth.codePlaceholder')} />
              <button type="button" className="login-code-send" onClick={sendCode}
                disabled={verification.sending || verification.cooldown > 0 || !emailReady}>
                {cooldownLabel(verification.cooldown, verification.sending)}
              </button>
            </div>
          </label>
          <CodeHint notice={verification.notice} debugCode={verification.debugCode} />
          {error && <p className="login-error" role="alert">{error}</p>}
          <button className="login-submit" type="submit"
            disabled={busy || !emailReady || code.trim().length < 4}>
            {busy ? t('auth.binding') : t('auth.bindSubmit')}
          </button>
          <button type="button" className="login-secondary" onClick={onClose}>
            {t('auth.close')}
          </button>
        </form>
      </div>
    </div>
  );
}
