import { useEffect, useMemo, useState } from 'react';
import { useLanguage } from '@/app/providers/LanguageContext';
import { apiUrl } from '@/infrastructure/api';
import { languageHeaders } from '@/app/i18n';
import type { TranslationKey } from '@/app/i18n';
import { fetchToolCatalog, saveAgent, type AgentPayload } from '../api';
import type { AgentDefinition, ToolInfo, ToolPermission } from '../types';
import './AgentWizard.css';

const MAX_SKILLS = 8;
const MAX_SERVERS = 8;
// Both images run a CPython interpreter, which the file tools need; node:22-slim
// is offered for command-only work and says so in the hint.
const IMAGES = ['agentforge/python-docs:3.12', 'python:3.12-slim', 'node:22-slim'] as const;

interface Skill { id: string; name: string }
interface Server { id: string; url: string; enabled?: boolean; tools: { name: string }[] }
interface Model { id: string; name: string; provider: string; enabled?: boolean }

interface Draft {
  name: string;
  description: string;
  systemPrompt: string;
  provider: string;
  modelId: string;
  tools: string[];
  skills: string[];
  servers: string[];
  sandbox: boolean;
  image: string;
  permissions: Record<string, ToolPermission>;
  maxTurns: number;
  budget: number;
  idleLimit: number;
}

// Rows are per-owner, so a catalogue can legitimately hold two entries with the
// same id. React keys and the selection list both need one.
function uniqueById<T extends { id: string }>(rows: T[]): T[] {
  const seen = new Set<string>();
  return rows.filter(row => (seen.has(row.id) ? false : (seen.add(row.id), true)));
}

const TEMPLATES: { id: string; nameKey: TranslationKey; promptKey: TranslationKey }[] = [
  { id: 'writer', nameKey: 'agentwiz.template.writer', promptKey: 'agentwiz.template.writerPrompt' },
  { id: 'researcher', nameKey: 'agentwiz.template.researcher', promptKey: 'agentwiz.template.researcherPrompt' },
  { id: 'analyst', nameKey: 'agentwiz.template.analyst', promptKey: 'agentwiz.template.analystPrompt' },
];

function toDraft(agent: AgentDefinition | null, tools: ToolInfo[]): Draft {
  const defaults = Object.fromEntries(tools.map(tool => [tool.name, tool.permission]));
  return {
    name: agent?.name || '',
    description: agent?.description || '',
    systemPrompt: agent?.systemPrompt || '',
    provider: agent?.model?.provider || 'deepseek',
    modelId: agent?.modelId || 'deepseek-chat',
    // Web access is on by default: an assistant that cannot look anything up
    // answers "I cannot reach the internet", which is the wrong first impression.
    tools: agent?.tools || ['read_file', 'write_file', 'grep', 'glob', 'run_command', 'web_search', 'web_fetch', 'search_products', 'get_product'],
    skills: agent?.skills || [],
    servers: agent?.mcp_servers || [],
    sandbox: Boolean(agent?.sandbox_policy),
    image: agent?.sandbox_policy?.image || IMAGES[0],
    permissions: { ...defaults, ...(agent?.permissions || {}) },
    maxTurns: agent?.stop_conditions?.max_turns ?? 25,
    budget: agent?.stop_conditions?.budget ?? 0.5,
    idleLimit: agent?.stop_conditions?.idle_limit ?? 3,
  };
}

/**
 * Create or edit one agent definition.
 *
 * Three steps rather than one long form because the fields answer different
 * questions -- what it is, what it may touch, and when it must stop -- and a
 * single page of thirty inputs makes the last two look optional.
 */
export function AgentWizard({ initial, mode, onCancel, onSaved }: {
  initial?: AgentDefinition | null;
  /** `create` also covers "duplicate": the fields are prefilled but the id is not sent. */
  mode: 'create' | 'edit';
  onCancel: () => void;
  onSaved: (agent: AgentDefinition) => void;
}) {
  const { t } = useLanguage();
  const [step, setStep] = useState(0);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [servers, setServers] = useState<Server[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [catalogError, setCatalogError] = useState('');
  const [draft, setDraft] = useState<Draft>(() => toDraft(initial || null, []));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    const read = async (path: string) => {
      const response = await fetch(apiUrl(path), { signal: controller.signal, headers: languageHeaders() });
      if (!response.ok) throw new Error(`${path}: ${response.status}`);
      return (await response.json()).data;
    };
    Promise.all([fetchToolCatalog(controller.signal), read('/skills'), read('/mcp/servers'), read('/models')])
      .then(([toolList, skillList, serverList, modelList]) => {
        if (controller.signal.aborted) return;
        setTools(toolList);
        setSkills(uniqueById(skillList));
        setServers(uniqueById(serverList));
        setModels(uniqueById(modelList));
        // The catalogue is what supplies each tool's default permission, so the
        // draft is seeded once it arrives rather than at first render.
        setDraft(current => {
          const merged = toDraft(initial || null, toolList);
          return {
            ...merged,
            name: current.name, description: current.description, systemPrompt: current.systemPrompt,
            provider: modelList.some((m: Model) => m.provider === merged.provider)
              ? merged.provider : (modelList[0]?.provider || 'deepseek'),
            modelId: modelList.some((m: Model) => m.id === merged.modelId)
              ? merged.modelId : (modelList[0]?.id || 'deepseek-chat'),
          };
        });
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setCatalogError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => controller.abort();
  }, [initial]);

  const update = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft(current => ({ ...current, [key]: value }));

  // Show what the server would actually do, not a hardcoded "confirm". The
  // tool catalogue carries each tool's own default; a wizard that displays a
  // stricter level than the run uses is how every call ends up looking like it
  // must be approved.
  const permissionOf = (name: string): ToolPermission =>
    draft.permissions[name] || tools.find(tool => tool.name === name)?.permission || 'confirm';

  const toggle = (key: 'tools' | 'skills' | 'servers', value: string, limit = Number.POSITIVE_INFINITY) => {
    const selected = draft[key];
    if (selected.includes(value)) {
      update(key, selected.filter(item => item !== value) as Draft[typeof key]);
      return;
    }
    if (selected.length >= limit) return;
    if (key === 'tools') {
      const fallback = tools.find(tool => tool.name === value)?.permission || 'confirm';
      setDraft(current => ({ ...current,
        tools: [...current.tools, value],
        permissions: { ...current.permissions, [value]: current.permissions[value] || fallback } }));
      return;
    }
    update(key, [...selected, value] as Draft[typeof key]);
  };

  const steps = [t('agentwiz.step.basics'), t('agentwiz.step.capabilities'), t('agentwiz.step.guardrails')];
  const nameMissing = !draft.name.trim();
  const canContinue = step > 0 || !nameMissing;
  const activeModel = models.find(model => model.id === draft.modelId);

  const payload = useMemo<AgentPayload>(() => ({
    ...(mode === 'edit' && initial?.id ? { id: initial.id } : {}),
    name: draft.name.trim(),
    description: draft.description.trim(),
    system_prompt: draft.systemPrompt.trim(),
    model: { provider: draft.provider, model_id: draft.modelId },
    tools: draft.tools,
    skills: draft.skills,
    mcp_servers: draft.servers,
    sandbox_policy: draft.sandbox ? { image: draft.image, timeout: 30 } : null,
    // Only the tools this agent actually carries: a leftover override for a
    // deselected tool would be stored noise that never applies.
    permissions: Object.fromEntries(draft.tools
      .filter(name => draft.permissions[name])
      .map(name => [name, draft.permissions[name]])),
    stop_conditions: { max_turns: draft.maxTurns, budget: draft.budget, idle_limit: draft.idleLimit },
  }), [draft, initial]);

  const submit = async () => {
    setSaving(true);
    setError('');
    try {
      onSaved(await saveAgent(payload));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="agent-wizard" aria-label={mode === 'edit' ? t('agentwiz.editTitle') : t('agentwiz.title')}>
      <header className="agent-wizard-head">
        <div>
          <h1>{mode === 'edit' ? t('agentwiz.editTitle') : t('agentwiz.title')}</h1>
          <p>{t('agentwiz.subtitle')}</p>
        </div>
        <ol className="agent-wizard-steps">
          {steps.map((label, index) => (
            <li key={label} aria-current={index === step ? 'step' : undefined}
              className={index === step ? 'current' : index < step ? 'done' : ''}>
              <span>{index + 1}</span>{label}
            </li>
          ))}
        </ol>
      </header>

      {catalogError && <p className="agent-wizard-error" role="alert">{t('agentwiz.catalogFailed')} {catalogError}</p>}

      <div className="agent-wizard-body">
        {step === 0 && <>
          <label className="agent-field">
            <span>{t('agentwiz.name')}</span>
            <input value={draft.name} maxLength={100} autoFocus placeholder={t('agentwiz.namePlaceholder')}
              onChange={event => update('name', event.target.value)} />
          </label>
          <label className="agent-field">
            <span>{t('agentwiz.description')}</span>
            <input value={draft.description} maxLength={300} placeholder={t('agentwiz.descriptionPlaceholder')}
              onChange={event => update('description', event.target.value)} />
          </label>
          <div className="agent-field">
            <span>{t('agentwiz.template')}</span>
            <div className="agent-template-row">
              {TEMPLATES.map(template => <button key={template.id} type="button" className="agent-chip"
                onClick={() => {
                  update('systemPrompt', t(template.promptKey));
                  if (!draft.name.trim()) update('name', t(template.nameKey));
                }}>{t(template.nameKey)}</button>)}
            </div>
          </div>
          <label className="agent-field">
            <span>{t('agentwiz.systemPrompt')}</span>
            <textarea rows={9} value={draft.systemPrompt}
              placeholder={t('agentwiz.systemPromptPlaceholder')}
              onChange={event => update('systemPrompt', event.target.value)} />
            <small>{t('agentwiz.systemPromptHint')}</small>
          </label>
        </>}

        {step === 1 && <>
          <label className="agent-field agent-field--inline">
            <span>{t('agentwiz.model')}</span>
            <select value={draft.modelId} onChange={event => {
              const model = models.find(item => item.id === event.target.value);
              if (!model) return;
              setDraft(current => ({ ...current, modelId: model.id, provider: model.provider }));
            }}>
              {models.map(model => <option key={model.id} value={model.id} disabled={model.enabled === false}>
                {model.name}{model.enabled === false ? ` · ${t('header.tag.disabled')}` : ''}
              </option>)}
            </select>
            {activeModel && <small>{activeModel.provider}</small>}
          </label>

          <fieldset className="agent-field agent-choices">
            <legend>{t('agentwiz.tools')}</legend>
            <small>{t('agentwiz.toolsHint')}</small>
            <div className="agent-choice-grid">
              {tools.map(tool => <label key={tool.name} className="agent-choice" title={tool.description}>
                <input type="checkbox" checked={draft.tools.includes(tool.name)}
                  onChange={() => toggle('tools', tool.name)} />
                <span className="agent-choice-name">{tool.name}</span>
                <span className={`agent-badge agent-badge--${tool.permission}`}>
                  {t(`agentwiz.permission.${tool.permission}`)}
                </span>
              </label>)}
            </div>
          </fieldset>

          <fieldset className="agent-field agent-choices">
            <legend>{t('agentwiz.skills')} <em>{draft.skills.length}/{MAX_SKILLS}</em></legend>
            <small>{t('agentwiz.skillsHint')}</small>
            {skills.length === 0 ? <p className="agent-empty">{t('agentwiz.noSkills')}</p>
              : <div className="agent-choice-grid">
                {skills.map(skill => <label key={skill.id} className="agent-choice">
                  <input type="checkbox" checked={draft.skills.includes(skill.id)}
                    onChange={() => toggle('skills', skill.id, MAX_SKILLS)} />
                  <span className="agent-choice-name">{skill.name}</span>
                </label>)}
              </div>}
          </fieldset>

          <fieldset className="agent-field agent-choices">
            <legend>{t('agentwiz.mcp')} <em>{draft.servers.length}/{MAX_SERVERS}</em></legend>
            {servers.length === 0 ? <p className="agent-empty">{t('agentwiz.noMcp')}</p>
              : <div className="agent-choice-grid">
                {servers.map(server => <label key={server.id} className="agent-choice">
                  <input type="checkbox" checked={draft.servers.includes(server.id)}
                    onChange={() => toggle('servers', server.id, MAX_SERVERS)} />
                  <span className="agent-choice-name">{server.id}</span>
                  <span className="agent-badge">{server.tools?.length || 0}</span>
                </label>)}
              </div>}
          </fieldset>

          <label className="agent-choice agent-choice--wide">
            <input type="checkbox" checked={draft.sandbox} onChange={() => update('sandbox', !draft.sandbox)} />
            <span className="agent-choice-name">{t('agentwiz.sandbox')}</span>
            <small>{t('agentwiz.sandboxHint')}</small>
          </label>
          {draft.sandbox && <label className="agent-field agent-field--inline">
            <span>{t('agentwiz.image')}</span>
            <select value={draft.image} onChange={event => update('image', event.target.value)}>
              {IMAGES.map(image => <option key={image} value={image}>{image}</option>)}
            </select>
          </label>}
        </>}

        {step === 2 && <>
          <div className="agent-field agent-numbers">
            <label>
              <span>{t('agentwiz.maxTurns')}</span>
              <input type="number" min={1} max={100} value={draft.maxTurns}
                onChange={event => update('maxTurns', Math.min(100, Math.max(1, Number(event.target.value) || 1)))} />
            </label>
            <label>
              <span>{t('agentwiz.budget')}</span>
              <input type="number" min={0.01} max={100} step={0.01} value={draft.budget}
                onChange={event => update('budget', Math.min(100, Math.max(0.01, Number(event.target.value) || 0.01)))} />
            </label>
            <label>
              <span>{t('agentwiz.idleLimit')}</span>
              <input type="number" min={1} max={10} value={draft.idleLimit}
                onChange={event => update('idleLimit', Math.min(10, Math.max(1, Number(event.target.value) || 1)))} />
            </label>
          </div>

          <fieldset className="agent-field agent-choices">
            <legend>{t('agentwiz.permissions')}</legend>
            <small>{t('agentwiz.permissionsHint')}</small>
            {draft.tools.length === 0 ? <p className="agent-empty">{t('agentwiz.noTools')}</p>
              : <div className="agent-permission-list">
                {draft.tools.map(name => <label key={name} className="agent-permission-row">
                  <span>{name}</span>
                  <select value={permissionOf(name)}
                    onChange={event => setDraft(current => ({ ...current,
                      permissions: { ...current.permissions, [name]: event.target.value as ToolPermission } }))}>
                    {(['auto', 'rule', 'confirm'] as const).map(level =>
                      <option key={level} value={level}>{t(`agentwiz.permission.${level}`)}</option>)}
                  </select>
                </label>)}
              </div>}
          </fieldset>

          <div className="agent-summary">
            <h2>{draft.name || t('agentwiz.namePlaceholder')}</h2>
            <p>{draft.description || t('agentwiz.descriptionPlaceholder')}</p>
            <ul>
              <li>{t('agentwiz.summaryModel')} {draft.modelId}</li>
              <li>{t('agentwiz.summaryTools', { count: draft.tools.length })}</li>
              <li>{t('agentwiz.summarySkills', { count: draft.skills.length })}</li>
              <li>{draft.sandbox ? t('agentwiz.summarySandbox', { image: draft.image }) : t('agentwiz.summaryHost')}</li>
            </ul>
          </div>
        </>}
      </div>

      {error && <p className="agent-wizard-error" role="alert">{error}</p>}

      <footer className="agent-wizard-foot">
        <button type="button" className="agent-button" onClick={onCancel} disabled={saving}>{t('agentwiz.cancel')}</button>
        <div>
          {step > 0 && <button type="button" className="agent-button"
            onClick={() => setStep(step - 1)} disabled={saving}>{t('agentwiz.back')}</button>}
          {step < steps.length - 1
            ? <button type="button" className="agent-button agent-button--primary" disabled={!canContinue}
              onClick={() => setStep(step + 1)}>{t('agentwiz.next')}</button>
            : <button type="button" className="agent-button agent-button--primary" disabled={saving || nameMissing}
              onClick={() => void submit()}>
              {saving ? t('agentwiz.saving') : mode === 'edit' ? t('agentwiz.save') : t('agentwiz.create')}
            </button>}
        </div>
      </footer>
    </section>
  );
}
