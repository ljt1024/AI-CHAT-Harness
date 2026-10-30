import { useState } from 'react';
import { useLanguage } from '@/app/providers/LanguageContext';
import StreamingMarkdown from '../MarkDownContent/StreamingMarkdown';

export function ReasoningPanel({ content, loading, durationMs, interrupted }: {
  content: string; loading: boolean; durationMs?: number; interrupted?: boolean;
}) {
  const { t, dateLocale } = useLanguage();
  const [open, setOpen] = useState(true);
  const seconds = durationMs === undefined ? null : new Intl.NumberFormat(dateLocale, { maximumFractionDigits: 1, minimumFractionDigits: 1 }).format(durationMs / 1000);
  return <details className="reasoning-panel" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span className="reasoning-summary-label">{loading && <span className="agent-spinner" aria-hidden="true" />}{loading ? t('reasoning.generating') : interrupted ? t('reasoning.interrupted') : t('reasoning.complete')}</span>{seconds !== null && !loading && <small>{t('reasoning.duration', { seconds })}</small>}</summary>
    <div className="reasoning-panel-content"><StreamingMarkdown content={content} streaming={loading} /></div>
  </details>;
}
