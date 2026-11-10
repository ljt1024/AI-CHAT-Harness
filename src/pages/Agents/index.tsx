import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useLanguage } from '@/app/providers/LanguageContext';
import { useAuth } from '@/app/providers/AuthContext';
import Dialog from '@/shared/components/Dialog';
import { AgentWizard } from '@/features/agents/components/AgentWizard';
import { ToolMarket } from '@/features/agents/components/ToolMarket';
import { deleteAgent, fetchAgents } from '@/features/agents/api';
import type { AgentDefinition } from '@/features/agents/types';
import './index.css';

/** The built-in row is shared (owner_id empty); every account may run it, none may edit it. */
const isShared = (agent: AgentDefinition) => agent.id === 'default' || agent.owner_id === '';

export default function Agents() {
  const { t } = useLanguage();
  const { user, askToSignIn } = useAuth();
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // `null` means the list; a wizard state replaces it, so the page never has to
  // juggle a modal on top of a scrollable grid.
  const [editor, setEditor] = useState<{ mode: 'create' | 'edit'; initial: AgentDefinition | null } | null>(null);
  const [deleting, setDeleting] = useState<AgentDefinition | null>(null);
  const [deleteError, setDeleteError] = useState('');
  // Two views, one page: the catalogue of agents people build, and the catalogue
  // of services those agents may call. A tab beats a second route here because
  // "which tools does this deployment offer" is answered in the same breath as
  // "what can I build with them".
  const [view, setView] = useState<'agents' | 'market'>('agents');

  // A visitor may read the catalogue and run the shared agent, but every write
  // answers 401. Asking for an account up front beats a wizard whose catalogue
  // cannot load and whose save fails at the last step.
  const requireAccount = (action: () => void) => () => {
    if (!user) { askToSignIn('sign_in'); return; }
    action();
  };

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setAgents(await fetchAgents(signal));
      setError('');
    } catch (cause) {
      if (!signal?.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const confirmDelete = async () => {
    if (!deleting) return;
    try {
      await deleteAgent(deleting.id);
      setDeleting(null);
      setDeleteError('');
      await load();
    } catch (cause) {
      setDeleteError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  if (editor) {
    return (
      <div className="agents-page">
        <AgentWizard
          key={`${editor.mode}-${editor.initial?.id || 'new'}`}
          mode={editor.mode}
          initial={editor.initial}
          onCancel={() => setEditor(null)}
          onSaved={() => { setEditor(null); void load(); }}
        />
      </div>
    );
  }

  return (
    <div className="agents-page">
      <header className="agents-head">
        <div>
          <Link className="agents-back" to="/ai">← {t('agents.back')}</Link>
          <h1>{t('agents.title')}</h1>
          <p>{t('agents.subtitle')}</p>
        </div>
        <button type="button" className="agent-button agent-button--primary"
          onClick={requireAccount(() => setEditor({ mode: 'create', initial: null }))}>{t('agents.new')}</button>
      </header>

      <nav className="agents-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={view === 'agents'}
          className={view === 'agents' ? 'agents-tab is-active' : 'agents-tab'}
          onClick={() => setView('agents')}>{t('agents.tabAgents')}</button>
        <button type="button" role="tab" aria-selected={view === 'market'}
          className={view === 'market' ? 'agents-tab is-active' : 'agents-tab'}
          onClick={() => setView('market')}>{t('agents.tabTools')}</button>
      </nav>

      {view === 'market' && <ToolMarket />}

      {view === 'agents' && <>
      {error && <p className="agent-wizard-error" role="alert">{t('agents.loadFailed')} {error}</p>}
      {!user && <p className="agents-hint agents-hint--guest">{t('agents.guestHint')}</p>}
      {loading && <p className="agents-hint">{t('agents.loading')}</p>}

      {!loading && !agents.length && (
        <div className="agents-empty">
          <p>{t('agents.empty')}</p>
          <small>{t('agents.emptyHint')}</small>
        </div>
      )}

      <div className="agents-grid">
        {agents.map(agent => {
          const shared = isShared(agent);
          return <article className="agent-card" key={agent.id}>
            <header>
              <h2>{agent.name}</h2>
              {shared && <span className="agents-tag">{t('agents.shared')}</span>}
            </header>
            <p className="agent-card-desc">{agent.description || t('agents.noDescription')}</p>
            <ul className="agent-card-meta">
              <li>{agent.modelId}</li>
              <li>{t('agents.toolCount', { count: agent.tools?.length || 0 })}</li>
              {Boolean(agent.skills?.length) && <li>{t('agents.skillCount', { count: agent.skills!.length })}</li>}
              {Boolean(agent.mcp_servers?.length) && <li>{t('agents.mcpCount', { count: agent.mcp_servers!.length })}</li>}
              {agent.sandbox_policy && <li>{t('agents.sandboxed')}</li>}
            </ul>
            <footer>
              <button type="button" className="agent-button" onClick={() => navigate(`/ai?agent=${encodeURIComponent(agent.id)}`)}>
                {t('agents.use')}
              </button>
              <button type="button" className="agent-button"
                onClick={requireAccount(() => setEditor({ mode: 'create', initial: { ...agent, id: '' } }))}>{t('agents.duplicate')}</button>
              {!shared && <>
                <button type="button" className="agent-button"
                  onClick={requireAccount(() => setEditor({ mode: 'edit', initial: agent }))}>{t('agents.edit')}</button>
                <button type="button" className="agent-button agent-button--danger"
                  onClick={requireAccount(() => { setDeleting(agent); setDeleteError(''); })}>{t('agents.delete')}</button>
              </>}
            </footer>
          </article>;
        })}
      </div>

      <p className="agents-hint">{t('agents.sharedHint')}</p>
      </>}

      <Dialog isOpen={deleting !== null} onClose={() => setDeleting(null)} title={t('agents.deleteTitle')}
        type="confirm" confirmText={t('agents.deleteConfirm')} onConfirm={() => void confirmDelete()}>
        <p>{t('agents.deleteBody', { name: deleting?.name || '' })}</p>
        {deleteError && <p className="agent-wizard-error" role="alert">{deleteError}</p>}
      </Dialog>
    </div>
  );
}
