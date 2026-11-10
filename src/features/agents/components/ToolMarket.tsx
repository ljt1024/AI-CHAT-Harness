import { useCallback, useEffect, useState } from 'react';
import { useAuth } from '@/app/providers/AuthContext';
import { useLanguage } from '@/app/providers/LanguageContext';
import Dialog from '@/shared/components/Dialog';
import {
  deleteMcpServer, fetchMcpCatalog, fetchMcpServers, importMcpServer,
  installCatalogServer, registerMcpServer, setMcpServerEnabled,
  type McpCatalogEntry, type McpServerSummary,
} from '../api';
import './ToolMarket.css';

// The block a hosted catalogue hands you to copy.
const PASTE_EXAMPLE =
  '{"mcpServers": {"fetch": {"type": "streamable_http", "url": "https://host/mcp", ' +
  '"headers": {"Authorization": "Bearer <token>"}}}}';

/**
 * The catalogue, plus whatever this account already installed.
 *
 * A card never asks for a URL: the address belongs to the deployment, and the
 * user brings only the credential. Manual registration is still here, one level
 * down, for a service the operator has not listed — and it is the only place a
 * URL is typed, which is why it sits behind its own disclosure.
 */
export function ToolMarket() {
  const { t } = useLanguage();
  const { user, askToSignIn } = useAuth();
  const [catalog, setCatalog] = useState<McpCatalogEntry[]>([]);
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [target, setTarget] = useState<McpCatalogEntry | null>(null);
  const [token, setToken] = useState('');
  const [removing, setRemoving] = useState<McpServerSummary | null>(null);
  const [form, setForm] = useState({ id: '', url: '', token: '' });
  const [paste, setPaste] = useState('');

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [nextCatalog, nextServers] = await Promise.all([
        fetchMcpCatalog(signal), fetchMcpServers(signal)]);
      setCatalog(nextCatalog);
      setServers(nextServers);
      setError('');
    } catch (cause) {
      if (!signal?.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // The catalogue needs an account; the guest path is the sign-in prompt, not
    // a request that would come back 401 and look like a broken page.
    if (!user) { setLoading(false); return; }
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, user]);

  const run = useCallback(async (key: string, action: () => Promise<unknown>) => {
    setBusy(key);
    setError('');
    try {
      await action();
      await load();
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setBusy('');
    }
  }, [load]);

  const add = (item: McpCatalogEntry) => {
    if (!user) { askToSignIn('sign_in'); return; }
    // A credential the user has to look up deserves a step of its own; anything
    // else installs on the click that expressed the intent.
    if (item.auth === 'token') { setToken(''); setTarget(item); return; }
    void run(item.id, () => installCatalogServer(item.id));
  };

  const confirmInstall = async () => {
    const item = target;
    if (!item) return;
    if (await run(item.id, () => installCatalogServer(item.id, token.trim() || undefined))) {
      setTarget(null);
      setToken('');
    }
  };

  return <section className="market">
    {error && <p className="market-error" role="alert">{error}</p>}
    {!user && <p className="market-hint">{t('market.signIn')}</p>}

    {user && <>
      <header className="market-head">
        <h2>{t('market.catalogTitle')}</h2>
        <p>{t('market.catalogHint')}</p>
      </header>
      {loading && <p className="market-hint">{t('market.loading')}</p>}
      {!loading && !catalog.length && <p className="market-hint">{t('market.emptyCatalog')}</p>}
      <div className="market-grid">
        {catalog.map(item => <article className="market-card" key={item.id}>
          <header>
            <h3>{item.name}</h3>
            {item.verified && <span className="market-tag">{t('market.verified')}</span>}
            {item.private && <span className="market-tag market-tag--warn">{t('market.private')}</span>}
            {!item.enabled && <span className="market-tag">{t('market.switchedOff')}</span>}
          </header>
          <p>{item.summary || t('market.noSummary')}</p>
          {item.docs && <a className="market-docs" href={item.docs} target="_blank" rel="noreferrer">
            {t('market.getToken')}</a>}
          <footer>
            <span className="market-meta">
              {item.auth === 'none' ? t('market.noAuth') : t('market.needsToken')}
            </span>
            <button type="button" className="agent-button agent-button--primary"
              disabled={item.installed || !item.enabled || busy === item.id}
              onClick={() => add(item)}>
              {item.installed ? t('market.installed')
                : busy === item.id ? t('market.adding') : t('market.add')}
            </button>
          </footer>
        </article>)}
      </div>

      <header className="market-head">
        <h2>{t('market.mine')}</h2>
        <p>{t('market.mineHint')}</p>
      </header>
      {!loading && !servers.length && <p className="market-hint">{t('market.emptyMine')}</p>}
      <ul className="market-list">
        {servers.map(server => <li className="market-row" key={server.id}>
          <div className="market-row-main">
            <strong>{server.id}</strong>
            <small>{server.url}</small>
            <span className="market-row-tags">
              {server.private && <span className="market-tag market-tag--warn">{t('market.private')}</span>}
              {server.hasToken && <span className="market-tag">{t('market.tokenSaved')}</span>}
              {!server.enabled && <span className="market-tag">{t('market.switchedOff')}</span>}
              <span className="market-tag">{t('market.toolsCount', { count: server.tools.length })}</span>
            </span>
          </div>
          <div className="market-row-actions">
            <button type="button" className="agent-button" disabled={busy === server.id}
              onClick={() => void run(server.id, () => setMcpServerEnabled(server.id, !server.enabled))}>
              {server.enabled ? t('market.disable') : t('market.enable')}
            </button>
            <button type="button" className="agent-button agent-button--danger"
              disabled={busy === server.id} onClick={() => setRemoving(server)}>{t('market.remove')}</button>
          </div>
        </li>)}
      </ul>

      <details className="market-manual">
        <summary>{t('market.manual')}</summary>
        <p className="market-hint">{t('market.manualHint')}</p>
        <div className="market-form">
          <label>{t('cap.serverId')}
            <input value={form.id} maxLength={20} placeholder="amap"
              onChange={event => setForm({ ...form, id: event.target.value })} /></label>
          <label>{t('cap.url')}
            <input type="url" value={form.url} placeholder="https://host/mcp"
              onChange={event => setForm({ ...form, url: event.target.value })} /></label>
          <label>{t('cap.token')}
            <input type="password" autoComplete="off" value={form.token}
              placeholder={t('cap.tokenOptional')}
              onChange={event => setForm({ ...form, token: event.target.value })} /></label>
          <button type="button" className="agent-button agent-button--primary"
            disabled={!form.id.trim() || !form.url.trim() || busy === 'connect'}
            onClick={() => void run('connect', async () => {
              await registerMcpServer({ id: form.id.trim(), url: form.url.trim(),
                token: form.token.trim() || undefined });
              setForm({ id: '', url: '', token: '' });
            })}>{t('cap.connect')}</button>
        </div>
        <label className="market-paste">{t('cap.pasteConfig')}
          <textarea rows={3} spellCheck={false} value={paste} placeholder={PASTE_EXAMPLE}
            onChange={event => setPaste(event.target.value)} />
        </label>
        <button type="button" className="agent-button" disabled={!paste.trim() || busy === 'paste'}
          onClick={() => void run('paste', async () => {
            await importMcpServer(paste);
            setPaste('');
          })}>{t('cap.import')}</button>
      </details>
    </>}

    <Dialog isOpen={target !== null} onClose={() => setTarget(null)} title={t('market.installTitle')}
      type="confirm" confirmText={t('market.add')}
      onConfirm={() => void confirmInstall()}>
      <p>{t('market.installBody', { name: target?.name ?? '' })}</p>
      <label className="market-token">{t('cap.token')}
        <input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} />
      </label>
      {target?.docs && <a className="market-docs" href={target.docs} target="_blank" rel="noreferrer">
        {t('market.getToken')}</a>}
    </Dialog>

    <Dialog isOpen={removing !== null} onClose={() => setRemoving(null)} title={t('market.removeTitle')}
      type="confirm" confirmText={t('market.remove')}
      confirmButtonStyle={{ backgroundColor: 'var(--danger-color)', outline: 'none' }}
      onConfirm={() => {
        const server = removing;
        setRemoving(null);
        if (server) void run(server.id, () => deleteMcpServer(server.id));
      }}>
      <p>{t('market.removeBody', { name: removing?.id ?? '' })}</p>
    </Dialog>
  </section>;
}
