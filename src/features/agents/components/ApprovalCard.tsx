import { useEffect, useRef, useState } from 'react';
import { useLanguage } from '@/app/providers/LanguageContext';
import { resolveApproval } from '../api';
import './ApprovalCard.css';

export interface Approval {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  status: 'pending' | 'approved' | 'denied' | 'closed';
  kind?: 'approve' | 'input';
  rememberable?: boolean;
}

function questionOf(args: Record<string, unknown>): string {
  const value = args?.question ?? args?.prompt;
  return typeof value === 'string' ? value : '';
}

function optionsOf(args: Record<string, unknown>): string[] {
  const value = args?.options;
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string');
}

export function ApprovalCard({ approval, onStop, pendingCount = 1, autoFocus = false }: {
  approval: Approval;
  onStop?: () => void;
  pendingCount?: number;
  autoFocus?: boolean;
}) {
  const { t } = useLanguage();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [answer, setAnswer] = useState('');
  const panelRef = useRef<HTMLElement>(null);
  const pending = approval.status === 'pending';
  const asking = (approval.kind ?? 'approve') === 'input';
  const question = questionOf(approval.args);
  const options = optionsOf(approval.args);
  useEffect(() => {
    if (autoFocus && pending) panelRef.current?.focus();
  }, [autoFocus, pending, approval.id]);
  const decide = async (approved: boolean, reason = '', remember = false) => {
    if (busy || !pending) return;
    setBusy(true); setError('');
    try { await resolveApproval(approval.id, approved, reason, remember); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };
  const title = t(!pending ? 'approval.record'
    : asking ? 'approval.questionTitle' : 'approval.title');
  return <section ref={panelRef} className="approval-card" aria-label={title} tabIndex={autoFocus ? -1 : undefined} aria-busy={busy}>
    <header className="approval-heading">
      <strong>{title}</strong>
      {pending && pendingCount > 1 && <span className="approval-count">{t('approval.pendingCount', { count: pendingCount })}</span>}
      {pending && onStop && <button className="approval-stop" type="button" onClick={onStop}>{t('input.stop')}</button>}
    </header>
    {pending && asking && <>
      <p className="approval-question">{question}</p>
      {options.length > 0 && <div className="approval-options">
        {options.map(option => {
          const selected = answer === option;
          return <button key={option} type="button" disabled={busy}
            className={selected ? 'selected' : ''}
            onClick={() => setAnswer(selected ? '' : option)}>{option}</button>;
        })}
      </div>}
      <textarea className="approval-answer" value={answer} disabled={busy} rows={2}
        placeholder={t('approval.placeholder')} aria-label={t('approval.placeholder')}
        onChange={event => setAnswer(event.target.value)} />
    </>}
    {pending && !asking && <><code>{approval.tool}</code><pre>{JSON.stringify(approval.args, null, 2)}</pre></>}
    {!pending && <details><summary><code>{approval.tool}</code></summary><pre>{JSON.stringify(approval.args, null, 2)}</pre></details>}
    {pending && asking && <div className="approval-actions">
      <button type="button" disabled={busy} onClick={() => void decide(false)}>{t('approval.skip')}</button>
      <button type="button" disabled={busy || !answer.trim()} onClick={() => void decide(true, answer)}>{t('approval.submit')}</button>
    </div>}
    {pending && !asking && <div className="approval-actions">
      <button type="button" disabled={busy} onClick={() => void decide(false)}>{t('approval.deny')}</button>
      {approval.rememberable && <button type="button" disabled={busy} title={t('approval.allowSessionHint')}
        onClick={() => void decide(true, '', true)}>{t('approval.allowSession')}</button>}
      <button type="button" disabled={busy} onClick={() => void decide(true)}>{t('approval.allow')}</button>
    </div>}
    {pending && !asking && approval.rememberable &&
      <p className="approval-note">{t('approval.allowSessionHint')}</p>}
    {!pending && <p role="status">{t(asking
      ? (approval.status === 'approved' ? 'approval.answered' : 'approval.skipped')
      : approval.status === 'approved' ? 'approval.approved'
        : approval.status === 'denied' ? 'approval.denied' : 'approval.closed')}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
