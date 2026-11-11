import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAuth } from '@/app/providers/AuthContext';
import { useLanguage } from '@/app/providers/LanguageContext';
import Dialog from '@/shared/components/Dialog';
import type { TranslationKey } from '@/app/i18n';
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

// Categories are operator data, so an unknown one shows as written rather than
// disappearing from the filter bar.
const CATEGORY_LABELS: Record<string, TranslationKey> = {
  docs: 'market.category.docs',
  search: 'market.category.search',
  maps: 'market.category.maps',
  data: 'market.category.data',
  development: 'market.category.development',
};

const ALL = 'all';

// Reading order for the page: what a model needs to look something up comes
// before what it needs to move through the world.
const CATEGORY_ORDER = ['docs', 'search', 'maps', 'data', 'development'];

const orderOf = (category: string) => {
  const index = CATEGORY_ORDER.indexOf(category);
  return index === -1 ? CATEGORY_ORDER.length : index;
};

/**
 * The catalogue, plus whatever this account already installed.
 *
 * A card never asks for a URL: the address belongs to the deployment, and the
 * user brings only the credential. Manual registration is still here, one level
 * down, for a service the operator has not listed — and it is the only place a
 * URL is typed, which is why it sits behind its own disclosure.
 */
export function ToolMarket() {
  const { t, language } = useLanguage();
  const { user, askToSignIn } = useAuth();
  const [catalog, setCatalog] = useState<McpCatalogEntry[]>([]);
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState(ALL);
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

  const nameOf = useCallback((item: McpCatalogEntry) =>
    (language === 'en' && item.nameEn) || item.name, [language]);
  const summaryOf = useCallback((item: McpCatalogEntry) =>
    (language === 'en' && item.summaryEn) || item.summary, [language]);

  const names = useMemo(() => new Map(catalog.map(item => [item.id, item])), [catalog]);

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    catalog.forEach(item => counts.set(item.category, (counts.get(item.category) ?? 0) + 1));
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [catalog]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return catalog.filter(item => {
      if (category !== ALL && item.category !== category) return false;
      if (!needle) return true;
      return [item.name, item.nameEn, item.summary, item.summaryEn, item.id]
        .join(' ').toLowerCase().includes(needle);
    }).sort((a, b) => orderOf(a.category) - orderOf(b.category)
      || nameOf(a).localeCompare(nameOf(b), language === 'zh' ? 'zh-CN' : 'en'));
  }, [catalog, category, query, language, nameOf]);

  const installedTools = servers.reduce((total, server) => total + server.tools.length, 0);

  const add = (item: McpCatalogEntry) => {
    if (!user) { askToSignIn('sign_in'); return; }
    // A credential the user has to look up deserves a step of its own; anything
    // else installs on the click that expressed the intent.
    if (item.auth !== 'none') { setToken(''); setTarget(item); return; }
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

      {!loading && !!catalog.length && <div className="market-browse">
        <input type="search" className="market-search" value={query}
          placeholder={t('market.searchPlaceholder')} aria-label={t('market.searchPlaceholder')}
          onChange={event => setQuery(event.target.value)} />
        <div className="market-filters" role="group" aria-label={t('market.allCategories')}>
          <button type="button" className={`market-chip${category === ALL ? ' market-chip--on' : ''}`}
            aria-pressed={category === ALL} onClick={() => setCategory(ALL)}>
            {t('market.allCategories')} <span>{catalog.length}</span>
          </button>
          {categories.map(([name, count]) => <button type="button" key={name}
            className={`market-chip${category === name ? ' market-chip--on' : ''}`}
            aria-pressed={category === name} onClick={() => setCategory(name)}>
            {CATEGORY_LABELS[name] ? t(CATEGORY_LABELS[name]) : name} <span>{count}</span>
          </button>)}
        </div>
      </div>}

      {loading && <p className="market-hint">{t('market.loading')}</p>}
      {!loading && !catalog.length && <p className="market-hint">{t('market.emptyCatalog')}</p>}
      {!loading && !!catalog.length && !visible.length &&
        <p className="market-hint">{t('market.noMatch')}</p>}
      <div className="market-grid">
        {visible.map(item => <article className="market-card" key={item.id}>
          <header>
            <h3>{nameOf(item)}</h3>
            {!item.verified && <span className="market-tag market-tag--warn">{t('market.unverified')}</span>}
            {item.private && <span className="market-tag market-tag--warn">{t('market.private')}</span>}
            {!item.enabled && <span className="market-tag">{t('market.switchedOff')}</span>}
          </header>
          <p>{summaryOf(item) || t('market.noSummary')}</p>
          {item.docs && <a className="market-docs" href={item.docs} target="_blank" rel="noreferrer">
            {item.auth === 'none' ? t('market.serviceDocs') : t('market.getToken')}</a>}
          <footer>
            <span className="market-meta">
              {item.auth === 'none' ? t('market.noAuth')
                : item.auth === 'optional' ? t('market.optionalAuth') : t('market.needsToken')}
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
        {installedTools > 0 && <p className="market-hint">
          {t('market.toolsTotal', { count: installedTools })} · {t('market.budgetHint')}</p>}
      </header>
      {!loading && !servers.length && <p className="market-hint">{t('market.emptyMine')}</p>}
      <ul className="market-list">
        {servers.map(server => <li className="market-row" key={server.id}>
          <div className="market-row-main">
            {/* A row installed from the catalogue has a name worth showing; one
                registered by hand only has the id the user typed. */}
            <strong>{names.get(server.id) ? nameOf(names.get(server.id)!) : server.id}</strong>
            <small>{server.id} · {server.url}</small>
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
      isDisabledConfirm={target?.auth === 'token' && !token.trim()}
      onConfirm={() => void confirmInstall()}>
      <p>{target?.auth === 'optional'
        ? t('market.optionalInstallBody', { name: target ? nameOf(target) : '' })
        : t('market.installBody', { name: target ? nameOf(target) : '' })}</p>
      <label className="market-token">{t('cap.token')}
        <input type="password" autoComplete="off" value={token}
          onChange={event => setToken(event.target.value)} />
      </label>
      {target?.tokenIn === 'query' && <p className="market-hint">{t('market.tokenInQuery')}</p>}
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
