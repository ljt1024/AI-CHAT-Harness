import { useState, type FormEvent } from 'react';
import { useAuth } from '@/app/providers/AuthContext';
import { t } from '@/app/i18n';

/**
 * The form itself, with no opinion about where it is presented: the dialog uses
 * it, and so would a full-page gate if a deployment ever wanted one back.
 */
export default function LoginForm({ onSuccess }: { onSuccess?: () => void }) {
  const { signIn } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
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

  return (
    <form className="login-form" onSubmit={submit}>
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
  );
}
