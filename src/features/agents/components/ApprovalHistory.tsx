import { useLanguage } from '@/app/providers/LanguageContext';
import type { MessageApproval } from '@/shared/utils/localMessages';
import './ApprovalCard.css';

const STATUS_KEY = {
  approved: 'approval.approved',
  denied: 'approval.denied',
  closed: 'approval.closed',
} as const;

type ResolvedApproval = MessageApproval & { status: keyof typeof STATUS_KEY };

const MARK = { approved: '✓', denied: '✕', closed: '–' } as const;

// A question is not approved or denied; it was answered or skipped.
function statusKey(item: MessageApproval) {
  if (item.kind !== 'input') return STATUS_KEY[item.status as keyof typeof STATUS_KEY];
  return item.status === 'denied' ? 'approval.skipped' : 'approval.answered';
}

/**
 * Compact, per-turn audit trail of the human approvals that were requested while the
 * agent worked. Full arguments stay one click away so the transcript still explains
 * why a command ran or was refused, without repeating a full card for every call.
 */
export function ApprovalHistory({ approvals }: { approvals: MessageApproval[] }) {
  const { t } = useLanguage();
  // A question was answered, not approved: counting it as an approval would make
  // the audit trail say the human permitted something they merely answered.
  const resolved = approvals.filter((item): item is ResolvedApproval => item.status !== 'pending');
  if (!resolved.length) return null;
  const gates = resolved.filter(item => item.kind !== 'input');
  const questions = resolved.filter(item => item.kind === 'input');
  const approved = gates.filter(item => item.status === 'approved').length;
  const denied = gates.filter(item => item.status === 'denied').length;
  const answered = questions.filter(item => item.status === 'approved').length;
  const skipped = questions.filter(item => item.status === 'denied').length;
  return <details className="approval-record">
    <summary>
      <span className="approval-record-title">{t('approval.record')}</span>
      <span className="approval-record-count">
        <span>{t('approval.recordCount', { count: resolved.length })}</span>
        {approved > 0 && <em className="approved">{t('approval.approved')} {approved}</em>}
        {denied > 0 && <em className="denied">{t('approval.denied')} {denied}</em>}
        {answered > 0 && <em className="approved">{t('approval.answered')} {answered}</em>}
        {skipped > 0 && <em className="denied">{t('approval.skipped')} {skipped}</em>}
      </span>
    </summary>
    <ul className="approval-record-list">
      {resolved.map(item => <li key={item.id}>
        <details>
          <summary>
            <span className={`approval-mark ${item.status}`} aria-hidden>
              {MARK[item.status]}
            </span>
            <code>{item.tool}</code>
            <span className="approval-record-status">{t(statusKey(item))}</span>
          </summary>
          <pre>{JSON.stringify(item.args, null, 2)}</pre>
        </details>
      </li>)}
    </ul>
  </details>;
}
