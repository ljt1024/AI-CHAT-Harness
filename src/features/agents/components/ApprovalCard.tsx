import { useEffect, useRef, useState } from 'react';
import { useLanguage } from '@/app/providers/LanguageContext';
import { resolveApproval } from '../api';
import './ApprovalCard.css';

export interface Approval { id: string; tool: string; args: Record<string, unknown>; status: 'pending' | 'approved' | 'denied' | 'closed' }

export function ApprovalCard({ approval, onStop, pendingCount = 1, autoFocus = false }: {
  approval: Approval;
  onStop?: () => void;
  pendingCount?: number;
  autoFocus?: boolean;
}) {
  const { t } = useLanguage();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const panelRef = useRef<HTMLElement>(null);
  const pending = approval.status === 'pending';
  useEffect(() => {
    if (autoFocus && pending) panelRef.current?.focus();
  }, [autoFocus, pending, approval.id]);
  const decide = async (approved: boolean) => {
    if (busy || !pending) return;
    setBusy(true); setError('');
    try { await resolveApproval(approval.id, approved); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };
  const title = t(pending ? 'approval.title' : 'approval.record');
  return <section ref={panelRef} className="approval-card" aria-label={title} tabIndex={autoFocus ? -1 : undefined} aria-busy={busy}>
    <header className="approval-heading">
      <strong>{title}</strong>
      {pending && pendingCount > 1 && <span className="approval-count">{t('approval.pendingCount', { count: pendingCount })}</span>}
      {pending && onStop && <button className="approval-stop" type="button" onClick={onStop}>{t('input.stop')}</button>}
    </header>
    {pending ? <><code>{approval.tool}</code><pre>{JSON.stringify(approval.args, null, 2)}</pre></>
      : <details><summary><code>{approval.tool}</code></summary><pre>{JSON.stringify(approval.args, null, 2)}</pre></details>}
    {pending ? <div className="approval-actions">
      <button type="button" disabled={busy} onClick={() => void decide(false)}>{t('approval.deny')}</button>
      <button type="button" disabled={busy} onClick={() => void decide(true)}>{t('approval.allow')}</button>
    </div> : <p role="status">{t(approval.status === 'approved' ? 'approval.approved' : approval.status === 'denied' ? 'approval.denied' : 'approval.closed')}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
