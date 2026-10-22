import i18n from '@/app/i18n';
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { useLanguage } from '@/app/providers/LanguageContext';
import type { AgentStep } from '../types';
import './AgentTrace.css';
import { ToolStepDetail, toolHeadline, toolDetails, toolLabel, type Translate } from './ToolStepDetail';

const formatSeconds = (ms: number, locale: string) => new Intl.NumberFormat(locale, {
  maximumFractionDigits: 1, minimumFractionDigits: 1,
}).format(ms / 1000);

function stepSummary(step: AgentStep, action: AgentStep | undefined, t: Translate): string {
  if (step.phase === 'thought') {
    const translation = step.outputTranslation;
    return translation && i18n.exists(translation.key)
      ? String(i18n.t(translation.key, translation.params))
      : step.output;
  }
  const detail = toolDetails(step, action, t)[0];
  if (detail) return detail;
  if (step.phase === 'observation' && step.output) return step.output;
  return toolHeadline(step, action, t);
}

/** Glyph per agent phase: 思考 / 行动 / 观察. Decorative only. */
function PhaseIcon({ phase }: { phase: AgentStep['phase'] }) {
  const shared = { width: 11, height: 11, viewBox: '0 0 12 12', 'aria-hidden': true, focusable: false } as const;
  if (phase === 'thought') return <svg {...shared} fill="currentColor"><circle cx="3.1" cy="6" r="1.15" /><circle cx="6" cy="6" r="1.15" /><circle cx="8.9" cy="6" r="1.15" /></svg>;
  if (phase === 'action') return <svg {...shared} fill="currentColor"><path d="M6.9 1.1 3 6.7h2.4l-.7 4.2 4.2-5.8H6.5z" /></svg>;
  return <svg {...shared} fill="none" stroke="currentColor" strokeWidth="1.1"><path d="M1.1 6S3.2 3 6 3s4.9 3 4.9 3-2.1 3-4.9 3S1.1 6 1.1 6z" /><circle cx="6" cy="6" r="1.3" fill="currentColor" stroke="none" /></svg>;
}

/** Width-stable "…" so streaming text never reflows the row it sits in. */
const ProgressDots = () => <span className="agent-dots" aria-hidden="true"><i /><i /><i /></span>;

/**
 * One transcript row. Fixed single-line height keeps the live region from
 * reflowing as tool payloads arrive.
 */
const StepRow = memo(function StepRow({ step, action, index }: {
  step: AgentStep; action?: AgentStep; index: number;
}) {
  const { t } = useLanguage();
  const translate = t as unknown as Translate;
  const [open, setOpen] = useState(false);
  const phases = { thought: t('agent.phase.thought'), action: t('agent.phase.action'), observation: t('agent.phase.observation') };
  const running = step.status === 'running';
  const expandable = step.phase !== 'thought' && Boolean(step.agentId);
  // The row already carries the phase and tool chips, so its text slot is reserved
  // for a concrete fact about the call — never a second copy of those chips.
  const detail = step.phase === 'thought' ? null : toolDetails(step, action, translate)[0];
  const summary = detail || (step.phase === 'thought' ? stepSummary(step, action, translate) : '');
  return <li data-step-id={step.id} data-phase={step.phase} data-status={step.status}
    aria-busy={running} style={{ animationDelay: `${Math.min(index, 6) * 40}ms` }}>
    <button type="button" className={`agent-step-row${expandable ? '' : ' agent-step-row--static'}`}
      aria-expanded={expandable ? open : undefined} disabled={!expandable}
      onClick={() => expandable && setOpen(value => !value)}>
      <span className={`agent-phase-chip is-${step.phase}`}>
        <PhaseIcon phase={step.phase} />
        {phases[step.phase]}
      </span>
      {step.agentId && <code>{toolLabel(step.agentId, translate)}</code>}
      <span className="agent-step-summary">{summary}</span>
      <small className="agent-step-mark">
        {running ? <ProgressDots /> : step.status === 'failed' ? t('agent.failed') : step.status === 'cancelled' ? t('agent.cancelled') : '✓'}
      </small>
    </button>
    {expandable && open && <ToolStepDetail step={step} action={action} />}
  </li>;
});

interface AgentTraceProps {
  steps: AgentStep[];
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  live?: boolean;
  answering?: boolean;
  memoryMessages?: number;
  summarizedMessages?: number;
}

export const AgentTrace = memo(function AgentTrace({ steps, status, live = false, answering = false, memoryMessages = 0, summarizedMessages = 0 }: AgentTraceProps) {
  const { t, dateLocale } = useLanguage();
  const translate = t as unknown as Translate;
  /** null means "follow the run state": open while working, folded once done. */
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  // Mounting an open <details> also fires a toggle event; only clicks count as intent.
  const userToggledRef = useRef(false);
  const open = userOpen ?? live;
  const listRef = useRef<HTMLOListElement>(null);
  const startRef = useRef(performance.now());
  const endRef = useRef<number | null>(null);
  const wasLiveRef = useRef(live);
  if (live) wasLiveRef.current = true;
  if (!live && endRef.current === null) endRef.current = performance.now();

  const [, forceTick] = useState(0);
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => forceTick((value) => value + 1), 500);
    return () => window.clearInterval(id);
  }, [live]);

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [steps.length, open]);

  const statuses = { running: t('agent.status.running'), completed: t('agent.status.completed'), failed: t('agent.status.failed'), cancelled: t('agent.status.cancelled') };
  const activeStep = [...steps].reverse().find((step) => step.status === 'running') || steps[steps.length - 1];
  const activeAction = activeStep
    ? steps.find((item) => item.phase === 'action' && Boolean(activeStep.toolCallId) && item.toolCallId === activeStep.toolCallId)
    : undefined;
  /**
   * Narrated headline: while a tool is actually running we name it; between
   * tools (and while the answer streams) we describe what the model is doing.
   */
  const activity = !live ? statuses[status]
    : answering ? t('agent.answering')
      : activeStep?.status === 'running' ? toolHeadline(activeStep, activeAction, translate)
        : t('agent.thinking');
  const elapsed = wasLiveRef.current ? formatSeconds((endRef.current ?? performance.now()) - startRef.current, dateLocale) : null;
  /**
   * The backend only emits action/observation steps, but the model spends real
   * time deliberating between them. Surface that as a live 思考 row so the run
   * reads as thinking → acting → observing instead of looking stalled.
   */
  const deliberating = live && !answering && !steps.some((step) => step.status === 'running');
  const thinkingRow = useMemo<AgentStep | null>(() => deliberating
    ? { id: `thinking-${steps.length}`, phase: 'thought', status: 'running', output: t('agent.thinking') }
    : null, [deliberating, steps.length, t]);

  return <details className={`agent-trace agent-trace--${status}${live ? ' is-live' : ''}`}
    open={open} onToggle={(event) => { if (userToggledRef.current) setUserOpen(event.currentTarget.open); }}>
    <summary onClick={() => { userToggledRef.current = true; }}>
      <span className="agent-status-dot" />
      <strong>{t('agent.label')}</strong>
      <span className="agent-trace-activity">{activity}{live && <ProgressDots />}</span>
      <small className="agent-trace-meta">
        {t('agent.steps', { count: steps.length })}
        {elapsed !== null && ` · ${t('agent.duration', { seconds: elapsed })}`}
      </small>
      <span className="agent-trace-chevron" aria-hidden="true" />
    </summary>
    {memoryMessages > 0 && <p className="agent-trace-note">{t('agent.memory', { count: memoryMessages })}</p>}
    {summarizedMessages > 0 && <p className="agent-trace-note">{t('agent.summary', { count: summarizedMessages, recent: memoryMessages - summarizedMessages })}</p>}
    <ol ref={listRef}>{steps.map((step, index) => <StepRow key={`${step.id}-${step.phase}`} step={step} index={index}
      action={steps.find((item) => item.phase === 'action' && Boolean(step.toolCallId) && item.toolCallId === step.toolCallId)} />)}
      {thinkingRow && <StepRow step={thinkingRow} index={steps.length} />}</ol>
  </details>;
});
