import { useLanguage } from '@/app/providers/LanguageContext';
import type { AgentStep } from '../types';

/** i18next's translate function, narrowed to the pure string lookup we need here. */
export type Translate = (key: string, options?: Record<string, unknown>) => string;

function parseRecord(value: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch { return null; }
}

function objectFrom(value: string): Record<string, unknown> | null {
  const direct = parseRecord(value);
  if (direct) return direct;
  // Recorded tool calls read "<tool name> {json}", so fall back to the JSON tail.
  const start = value.indexOf('{');
  const end = value.lastIndexOf('}');
  return start >= 0 && end > start ? parseRecord(value.slice(start, end + 1)) : null;
}

export function toolLabel(tool: string, t: Translate): string {
  const names: Record<string, string> = {
    calculate: t('trace.calculate'), current_time: t('trace.time'), get_weather: t('trace.weather'),
    generate_image: t('trace.image'), export_html: t('trace.html'), export_pdf: t('trace.pdf'),
    export_excel: t('trace.excel'), export_pptx: t('trace.ppt'),
    delegate_planner: t('trace.planner'), delegate_researcher: t('trace.researcher'), delegate_writer: t('trace.writer'),
    run_command: t('trace.shell'), read_file: t('trace.readFile'), write_file: t('trace.writeFile'),
    grep: t('trace.searchText'), glob: t('trace.findFiles'),
    list_skills: t('trace.listSkills'), load_skill: t('trace.loadSkill'),
    install_skill: t('trace.installSkill'), delete_skill: t('trace.deleteSkill'),
  };
  return names[tool] || tool;
}

/**
 * One short sentence describing what this step is doing right now. Each phase
 * gets its own verb so a run reads like a narration: thinking / acting / observing.
 */
export function toolHeadline(step: AgentStep, action: AgentStep | undefined, t: Translate): string {
  const name = toolLabel(step.agentId || action?.agentId || '', t);
  if (step.status === 'failed') return t('trace.failed', { name });
  if (step.status === 'cancelled') return t('trace.cancelled', { name });
  if (step.phase === 'thought') return step.status === 'running' ? t('agent.thinking') : step.output;
  if (step.phase === 'action') {
    if (step.status !== 'running') return t('trace.called', { name });
    return step.stage === 'preparing' ? t('trace.preparing', { name }) : t('trace.calling', { name });
  }
  return step.status === 'running' ? t('trace.receiving', { name }) : t('trace.received', { name });
}

/** Structured facts extracted from the recorded tool call, never inferred reasoning. */
export function toolDetails(step: AgentStep, action: AgentStep | undefined, t: Translate): string[] {
  const args = objectFrom(step.phase === 'action' ? step.output : action?.output || '');
  const result = step.phase === 'observation' ? objectFrom(step.output) : null;
  const details: string[] = [];
  if (args) {
    if (typeof args.city === 'string') details.push(t('trace.city', { city: args.city, kind: args.forecast ? t('trace.forecast') : t('trace.current') }));
    if (typeof args.title === 'string') details.push(t('trace.title', { title: args.title }));
    if (typeof args.operation === 'string' && Array.isArray(args.values)) details.push(t('trace.expression', { value: `${args.operation} (${args.values.join(', ')})` }));
    if (typeof args.task === 'string') details.push(t('trace.task', { task: args.task.slice(0, 240) }));
    if (Array.isArray(args.slides)) details.push(t('trace.slides', { count: args.slides.length }));
    if (Array.isArray(args.sheets)) details.push(t('trace.sheets', { count: args.sheets.length }));
  }
  if (step.status === 'completed' && result) {
    if (typeof result.fileId === 'string' && typeof result.fileName === 'string' && typeof result.downloadPath === 'string') {
      details.push(t('trace.written', { file: result.fileName }));
      if (typeof result.size === 'number') details.push(t('trace.bytes', { count: result.size }));
      if (typeof result.previewFileId === 'string') details.push(t('trace.previewSaved'));
    }
    if (typeof result.result === 'number') details.push(t('trace.result', { value: result.result }));
    if (typeof result.source === 'string') details.push(t('trace.source', { source: result.source }));
    if (typeof result.reportTime === 'string' && result.reportTime) details.push(t('trace.reportTime', { time: result.reportTime }));
  }
  return details;
}

/**
 * Detail body for one tool step. The raw payload stays collapsed by default: an
 * expanding transcript is what makes a live run feel like it is jumping around.
 */
export function ToolStepDetail({ step, action }: { step: AgentStep; action?: AgentStep }) {
  const { t } = useLanguage();
  const translate = t as unknown as Translate;
  const isAction = step.phase === 'action';
  const details = toolDetails(step, action, translate);
  return <section className="agent-tool-detail">
    <p className="agent-tool-headline">{toolHeadline(step, action, translate)}</p>
    {details.length > 0 && <ul>{details.map((text, index) => <li key={index}>{text}</li>)}</ul>}
    <details className="agent-tool-raw">
      <summary>{isAction ? t('trace.parameters') : t('trace.rawResult')}</summary>
      <pre>{step.output || t('trace.waiting')}</pre>
    </details>
  </section>;
}
