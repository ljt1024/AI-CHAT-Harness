import { useEffect, useRef, useState, type FormEvent } from 'react';
import { t } from '@/app/i18n';
import { useAuth } from '@/app/providers/AuthContext';
import { bindPhone } from './api';
import {
  SmsCodeHint, cooldownLabel, looksLikePhone, smsErrorMessage, useSmsCode,
} from './sms';
import './auth.css';

/**
 * Binding a number to an account that already exists.
 *
 * This is the one flow the phone door does not cover: the account `bootstrap()`
 * created has a password and no number, and its owner should not have to create
 * a second account to get one.
 */
export default function PhoneBindDialog({ onClose }: { onClose: () => void }) {
  const { user, refresh } = useAuth();
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const sms = useSmsCode('bind');
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
      await sms.request(phone.trim());
    } catch (failure) {
      setError(smsErrorMessage(failure));
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await bindPhone(phone.trim(), code.trim());
      // The masked number lives on the session record, so the sidebar only
      // changes once that record is read again.
      await refresh();
      onClose();
    } catch (failure) {
      setError(smsErrorMessage(failure));
      setBusy(false);
    }
  };

  const phoneReady = looksLikePhone(phone);

  return (
    <div className="login-backdrop" role="presentation"
      onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="login-card login-dialog" role="dialog" aria-modal="true"
        aria-labelledby="phone-bind-title" tabIndex={-1} ref={cardRef}>
        <button type="button" className="login-close" aria-label={t('auth.close')}
          onClick={onClose}>×</button>
        <h2 className="login-title" id="phone-bind-title">{t('auth.bindTitle')}</h2>
        <p className="login-subtitle">
          {user?.phone ? t('auth.bound', { phone: user.phone }) : t('auth.bindBody')}
        </p>
        <form className="login-fields" onSubmit={submit}>
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
                disabled={sms.sending || sms.cooldown > 0 || !phoneReady}>
                {cooldownLabel(sms.cooldown, sms.sending)}
              </button>
            </div>
          </label>
          <SmsCodeHint notice={sms.notice} debugCode={sms.debugCode} />
          {error && <p className="login-error" role="alert">{error}</p>}
          <button className="login-submit" type="submit"
            disabled={busy || !phoneReady || code.trim().length < 4}>
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
