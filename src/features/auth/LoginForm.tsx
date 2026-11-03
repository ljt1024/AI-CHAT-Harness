import { useState, type FormEvent } from 'react';
import { useAuth } from '@/app/providers/AuthContext';
import { t } from '@/app/i18n';
import {
  CodeHint, codeErrorMessage, cooldownLabel, looksLikeEmail, useCode,
} from './verification';

type Mode = 'email' | 'password';

/**
 * The form itself, with no opinion about where it is presented: the dialog uses
 * it, and so would a full-page gate if a deployment ever wanted one back.
 *
 * Two doors, because they answer different questions. An email address and a
 * code is what a person has, and the first code creates the account; a username
 * and a password is what the account created at deployment time has, and what
 * anyone who bound an address still keeps.
 */
export default function LoginForm({ onSuccess }: { onSuccess?: () => void }) {
  const { signIn, signInWithCode } = useAuth();
  const [mode, setMode] = useState<Mode>('email');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const verification = useCode('login');

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const switchTo = (next: Mode) => {
    setMode(next);
    setError('');
  };

  const sendCode = async () => {
    setError('');
    try {
      await verification.request(email.trim());
    } catch (failure) {
      setError(codeErrorMessage(failure));
    }
  };

  const submitCode = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await signInWithCode(email.trim(), code.trim());
      onSuccess?.();
    } catch (failure) {
      setError(codeErrorMessage(failure));
      setBusy(false);
    }
  };

  const submitPassword = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await signIn(username.trim(), password);
      onSuccess?.();
    } catch (failure) {
      setError(failure instanceof Error && failure.message ? failure.message : t('auth.failed'));
      setBusy(false);
    }
  };

  const codeLabel = cooldownLabel(verification.cooldown, verification.sending);
  const emailReady = looksLikeEmail(email);

  return (
    <div className="login-form">
      <div className="login-tabs" role="tablist" aria-label={t('auth.title')}>
        <button type="button" role="tab" aria-selected={mode === 'email'}
          className={`login-tab${mode === 'email' ? ' login-tab--active' : ''}`}
          onClick={() => switchTo('email')}>{t('auth.tab.email')}</button>
        <button type="button" role="tab" aria-selected={mode === 'password'}
          className={`login-tab${mode === 'password' ? ' login-tab--active' : ''}`}
          onClick={() => switchTo('password')}>{t('auth.tab.password')}</button>
      </div>

      {mode === 'email' ? (
        <form className="login-fields" onSubmit={submitCode}>
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
                {codeLabel}
              </button>
            </div>
          </label>
          <CodeHint notice={verification.notice} debugCode={verification.debugCode} />
          {error && <p className="login-error" role="alert">{error}</p>}
          <button className="login-submit" type="submit"
            disabled={busy || !emailReady || code.trim().length < 4}>
            {busy ? t('auth.emailSubmitting') : t('auth.emailSubmit')}
          </button>
          <p className="login-hint">{t('auth.autoRegister')}</p>
        </form>
      ) : (
        <form className="login-fields" onSubmit={submitPassword}>
          <label className="login-field">
            <span>{t('auth.username')}</span>
            <input value={username} onChange={event => setUsername(event.target.value)}
              autoComplete="username" autoFocus required maxLength={64} />
          </label>
          <label className="login-field">
            <span>{t('auth.password')}</span>
            <input type="password" value={password} onChange={event => setPassword(event.target.value)}
              autoComplete="current-password" required maxLength={200} />
          </label>
          {error && <p className="login-error" role="alert">{error}</p>}
          <button className="login-submit" type="submit" disabled={busy}>
            {busy ? t('auth.submitting') : t('auth.submit')}
          </button>
        </form>
      )}
    </div>
  );
}
