import { useState, type FormEvent } from 'react';
import { useAuth } from '@/app/providers/AuthContext';
import { t } from '@/app/i18n';
import {
  SmsCodeHint, cooldownLabel, looksLikePhone, smsErrorMessage, useSmsCode,
} from './sms';

type Mode = 'phone' | 'password';

/**
 * The form itself, with no opinion about where it is presented: the dialog uses
 * it, and so would a full-page gate if a deployment ever wanted one back.
 *
 * Two doors, because they answer different questions. A phone and a code is
 * what a person has; a username and a password is what the account created at
 * deployment time has, and what anyone who bound a phone still keeps.
 */
export default function LoginForm({ onSuccess }: { onSuccess?: () => void }) {
  const { signIn, signInWithCode } = useAuth();
  const [mode, setMode] = useState<Mode>('phone');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const sms = useSmsCode('login');

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const switchTo = (next: Mode) => {
    setMode(next);
    setError('');
  };

  const sendCode = async () => {
    setError('');
    try {
      await sms.request(phone.trim());
    } catch (failure) {
      setError(smsErrorMessage(failure));
    }
  };

  const submitPhone = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await signInWithCode(phone.trim(), code.trim());
      onSuccess?.();
    } catch (failure) {
      setError(smsErrorMessage(failure));
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

  const codeLabel = cooldownLabel(sms.cooldown, sms.sending);
  const phoneReady = looksLikePhone(phone);

  return (
    <div className="login-form">
      <div className="login-tabs" role="tablist" aria-label={t('auth.title')}>
        <button type="button" role="tab" aria-selected={mode === 'phone'}
          className={`login-tab${mode === 'phone' ? ' login-tab--active' : ''}`}
          onClick={() => switchTo('phone')}>{t('auth.tab.phone')}</button>
        <button type="button" role="tab" aria-selected={mode === 'password'}
          className={`login-tab${mode === 'password' ? ' login-tab--active' : ''}`}
          onClick={() => switchTo('password')}>{t('auth.tab.password')}</button>
      </div>

      {mode === 'phone' ? (
        <form className="login-fields" onSubmit={submitPhone}>
          <label className="login-field">
            <span>{t('auth.phone')}</span>
            <input value={phone} onChange={event => setPhone(event.target.value)}
              inputMode="numeric" autoComplete="tel" autoFocus required maxLength={20}
              placeholder={t('auth.phonePlaceholder')} />
          </label>
          <label className="login-field">
            <span>{t('auth.code')}</span>
            <div className="login-code-row">
              <input value={code} onChange={event => setCode(event.target.value)}
                inputMode="numeric" autoComplete="one-time-code" required maxLength={8}
                placeholder={t('auth.codePlaceholder')} />
              <button type="button" className="login-code-send" onClick={sendCode}
                disabled={sms.sending || sms.cooldown > 0 || !phoneReady}>{codeLabel}</button>
            </div>
          </label>
          <SmsCodeHint notice={sms.notice} debugCode={sms.debugCode} />
          {error && <p className="login-error" role="alert">{error}</p>}
          <button className="login-submit" type="submit"
            disabled={busy || !phoneReady || code.trim().length < 4}>
            {busy ? t('auth.phoneSubmitting') : t('auth.phoneSubmit')}
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
