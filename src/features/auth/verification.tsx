import { useCallback, useEffect, useState } from 'react';
import { t, type TranslationKey } from '@/app/i18n';
import { AuthRequestError, requestEmailCode, type CodePurpose } from './api';

/** Every refusal the code endpoints can return, in the reader's language. */
const REASONS = {
  cooldown: 'auth.sms.cooldown',
  invalid: 'auth.sms.invalid',
  expired: 'auth.sms.expired',
  too_many: 'auth.sms.too_many',
  daily_limit: 'auth.sms.daily_limit',
  send_failed: 'auth.sms.send_failed',
  mail_unavailable: 'auth.sms.mail_unavailable',
  sms_unavailable: 'auth.sms.sms_unavailable',
  signup_closed: 'auth.sms.signup_closed',
  email_taken: 'auth.sms.email_taken',
  phone_taken: 'auth.sms.phone_taken',
  login_required: 'auth.sms.login_required',
  invalid_email: 'auth.sms.invalid_email',
  invalid_phone: 'auth.sms.invalid_phone',
} as const satisfies Record<string, TranslationKey>;

export function codeErrorMessage(failure: unknown): string {
  if (failure instanceof AuthRequestError) {
    const key = REASONS[failure.code as keyof typeof REASONS];
    if (key) return t(key, { seconds: failure.retryAfter });
  }
  if (failure instanceof Error && failure.message && failure.message !== failure.name) {
    return failure.message;
  }
  return t('auth.sms.unknown');
}

/**
 * The "send code" half of a form: the countdown, the development echo, and the
 * one place that knows how long the server said to wait. Both the sign-in form
 * and the bind dialog use it, so the two cannot disagree about the cooldown.
 */
export function useCode(purpose: CodePurpose) {
  const [cooldown, setCooldown] = useState(0);
  const [sending, setSending] = useState(false);
  const [debugCode, setDebugCode] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setTimeout(() => setCooldown(value => value - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  const request = useCallback(async (email: string) => {
    setSending(true);
    setNotice('');
    try {
      const challenge = await requestEmailCode(email, purpose);
      setCooldown(challenge.cooldown);
      setDebugCode(challenge.debugCode ?? '');
      setNotice(t('auth.codeSent', { minutes: Math.max(1, Math.round(challenge.expiresIn / 60)) }));
      return challenge;
    } finally {
      setSending(false);
    }
  }, [purpose]);

  const reset = useCallback(() => {
    setCooldown(0);
    setDebugCode('');
    setNotice('');
  }, []);

  return { cooldown, sending, debugCode, notice, request, reset };
}

export function CodeHint({ notice, debugCode }: { notice: string; debugCode: string }) {
  return <>
    {notice && <p className="login-hint" role="status">{notice}</p>}
    {debugCode && <p className="login-hint login-hint--dev" role="status">
      {t('auth.debugCode', { code: debugCode })}
    </p>}
  </>;
}

/** Turns a timer into the label the button shows while it counts down. */
export function cooldownLabel(cooldown: number, sending: boolean): string {
  if (sending) return t('auth.sendingCode');
  if (cooldown > 0) return t('auth.resendIn', { seconds: cooldown });
  return t('auth.sendCode');
}

/** Keeps the address field from being submitted with a shape the server rejects. */
export function looksLikeEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$/.test(value.trim());
}
