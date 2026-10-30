import { useState, type FormEvent } from 'react';
import { useAuth } from '@/app/providers/AuthContext';
import { t } from '@/app/i18n';
import './auth.css';

export default function LoginView() {
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
    } catch (failure) {
      setError(failure instanceof Error && failure.message ? failure.message : t('auth.failed'));
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <form className="login-card" onSubmit={submit}>
        <h1 className="login-title">{t('auth.title')}</h1>
        <p className="login-subtitle">{t('auth.subtitle')}</p>
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
    </main>
  );
}
