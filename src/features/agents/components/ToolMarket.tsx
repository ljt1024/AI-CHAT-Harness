import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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

// A remount -- a dev-server reload, an expired session re-reading /api/auth/me --
// rebuilds this component with empty arrays, and an empty array paints as "this
// deployment offers nothing" for as long as the refetch takes. Keeping the last
// answer in sessionStorage lets a remount repaint it at once and lets the refetch
// correct it silently afterwards. Keyed by account so two logins on one browser
// never see each other's installations.
const CACHE_PREFIX = 'agentforge.market.';

interface CachedMarket {
  catalog: McpCatalogEntry[];
  servers: McpServerSummary[];
}

const EMPTY_MARKET: CachedMarket = { catalog: [], servers: [] };

// sessionStorage survives a reload but is still read after the first paint, one
// commit too late: the remount renders an empty shelf and fills it in the effect.
// This holds the same answer for the life of the page, so a remount can seed its
// very first render with it.
let lastAnswer: { owner: string; data: CachedMarket } | null = null;

function readCache(owner: string): CachedMarket | null {
  try {
    const raw = sessionStorage.getItem(CACHE_PREFIX + owner);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<CachedMarket>;
    if (!Array.isArray(parsed.catalog) || !Array.isArray(parsed.servers)) return null;
    return { catalog: parsed.catalog, servers: parsed.servers };
  } catch {
    return null;
  }
}

function writeCache(owner: string, cache: CachedMarket): void {
  try {
    sessionStorage.setItem(CACHE_PREFIX + owner, JSON.stringify(cache));
  } catch {
    // A full quota costs one blank repaint after the next remount, not correctness.
  }
}

/** The answer this account last received, read before the first render. */
function initialMarket(owner: string): CachedMarket {
  if (lastAnswer && lastAnswer.owner === owner) return lastAnswer.data;
  return readCache(owner) ?? EMPTY_MARKET;
}

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
  // The account, not the `user` object: a session re-read hands back a fresh
  // object with the same id, and re-running the fetch below on that would be a
  // request nobody asked for.
  const ownerId = user?.id ?? '';
  const [seed] = useState(() => initialMarket(ownerId));
  const [catalog, setCatalog] = useState<McpCatalogEntry[]>(seed.catalog);
  const [servers, setServers] = useState<McpServerSummary[]>(seed.servers);
  // "loaded" rather than "loading": what the page must never do is paint an empty
  // catalogue as if the deployment had none. Until a request has actually
  // succeeded the panel says it is fetching, and a refresh keeps the rows that
  // are already on screen instead of blinking them away. Seeded from the last
  // answer so a remount starts there rather than here.
  const [loaded, setLoaded] = useState(seed.catalog.length > 0 || seed.servers.length > 0);
  const [error, setError] = useState('');
  // A refresh that answers "nothing" while rows are on screen: the rows stay and
  // this says why they are older than the timestamp.
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState('');
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState(ALL);
  const [target, setTarget] = useState<McpCatalogEntry | null>(null);
  const [token, setToken] = useState('');
  const [removing, setRemoving] = useState<McpServerSummary | null>(null);
  const [form, setForm] = useState({ id: '', url: '', token: '' });
  const [paste, setPaste] = useState('');
  const [retrying, setRetrying] = useState(false);

  // How many cards are actually on screen, for the "this refresh came back
  // empty" comparison in `load`. A ref because `load` must not be rebuilt every
  // time the catalogue changes.
  const shown = useRef(seed.catalog.length);
  useEffect(() => { shown.current = catalog.length; }, [catalog]);

  const load = useCallback(async (signal?: AbortSignal) => {
    // The two halves are asked for together and settled apart. "My services" runs
    // a live discovery per row, so it can take tens of seconds, and one
    // unreachable server makes it fail outright; awaited as a pair, that single
    // row used to hold back -- or throw away -- a catalogue that had answered in
    // milliseconds. That is exactly the shape of "the list was here, then it was
    // not".
    const [catalogResult, serversResult] = await Promise.allSettled([
      fetchMcpCatalog(signal), fetchMcpServers(signal)]);
    if (signal?.aborted) return;

    if (catalogResult.status === 'fulfilled') {
      // A 200 that is not an array -- a proxy's error page, a half-deployed
      // server, a body someone rewrote -- used to throw here, outside any catch,
      // and the panel then showed whatever it had before. Reading it as "no
      // rows" at least keeps the shelf and says what happened.
      const answer = Array.isArray(catalogResult.value) ? catalogResult.value : [];
      // An answer of "no services at all" while the shelf is full is a
      // half-started server, not a catalogue that vanished: keep the rows and let
      // the next refresh correct them, rather than telling the reader the
      // deployment offers nothing.
      const emptied = !answer.length && shown.current > 0;
      setCatalog(previous => (answer.length || !previous.length ? answer : previous));
      setStale(emptied);
    }
    if (serversResult.status === 'fulfilled') {
      setServers(Array.isArray(serversResult.value) ? serversResult.value : []);
    }

    const failure = catalogResult.status === 'rejected' ? catalogResult.reason
      : serversResult.status === 'rejected' ? serversResult.reason : null;
    if (failure) setError(failure instanceof Error ? failure.message : String(failure));
    else setError('');
    // Only an answer counts as "loaded". Two failures leave the panel saying it
    // is fetching, next to the reason it failed, instead of telling the reader
    // the deployment offers no services.
    if (catalogResult.status === 'fulfilled' || serversResult.status === 'fulfilled') setLoaded(true);
  }, []);

  const owner = useRef(ownerId);

  useEffect(() => {
    // The catalogue needs an account; the guest path is the sign-in prompt, not
    // a request that would come back 401 and look like a broken page.
    if (!ownerId) { owner.current = ''; return; }
    // Only a change of account clears the rows; this render was already seeded
    // from that account's cache, so there is nothing to clear on the first pass.
    if (owner.current !== ownerId) {
      owner.current = ownerId;
      // sessionStorage is a second-best copy: private mode, a full quota and a
      // cleared profile all make it unreadable while the page itself still holds
      // the answer. Reading only the copy meant a session that flickered to
      // signed-out and back -- any single 401 does that -- wiped a shelf that was
      // on screen a moment earlier. The in-memory answer is the first choice.
      const cached = readCache(ownerId)
        ?? (lastAnswer && lastAnswer.owner === ownerId ? lastAnswer.data : null);
      setCatalog(cached?.catalog ?? []);
      setServers(cached?.servers ?? []);
      setLoaded(!!cached);
      setStale(false);
    }
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, ownerId]);

  useEffect(() => {
    if (!ownerId || !loaded) return;
    // "Nothing" is never worth remembering as this account's answer: an empty
    // catalogue in a deployment that had rows a moment ago is a half-started or
    // failing server, and storing it would make the next remount paint the one
    // screen the panel exists to avoid.
    if (!catalog.length && lastAnswer && lastAnswer.owner === ownerId
        && lastAnswer.data.catalog.length) return;
    const answer = { catalog, servers };
    lastAnswer = { owner: ownerId, data: answer };
    writeCache(ownerId, answer);
  }, [ownerId, loaded, catalog, servers]);

  // A last line of defence, and a blunt one: if this page has seen a catalogue
  // and the state is now empty, put the answer back. Every path that empties it
  // -- an account change, a failed read, an answer that arrived as zero -- ends
  // on the same screen, and one of them is always a bug. The reader does not
  // care which; they care that the shelf they were reading is still there.
  // `lastAnswer` is per page load, so a genuine removal still lands on reload.
  useEffect(() => {
    if (!ownerId || catalog.length) return;
    if (!lastAnswer || lastAnswer.owner !== ownerId || !lastAnswer.data.catalog.length) return;
    setCatalog(lastAnswer.data.catalog);
    setServers(lastAnswer.data.servers);
    setLoaded(true);
  }, [ownerId, catalog]);

  // Development-only readout. "The list disappeared" has three different causes
  // -- no rows in state, rows in state that were never painted, or rows painted
  // and covered -- and they are indistinguishable on screen. This reports which
  // one it is from the live DOM, so the next report is a measurement rather than
  // another guess. It writes through a ref instead of state: an observer that
  // re-renders the thing it observes would be a suspect in its own case.
  const probe = useRef<HTMLSpanElement>(null);

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

  // The only recovery a failed read has: ask again, on purpose, with the reason
  // still on screen. A refresh that kept the rows already loaded is `load`
  // itself -- this one is for the case where there is nothing to keep.
  const reload = async () => {
    setRetrying(true);
    try {
      await load();
    } finally {
      setRetrying(false);
    }
  };

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

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const size = (node: Element | null) => {
      if (!node) return '—';
      const box = node.getBoundingClientRect();
      return `${Math.round(box.width)}×${Math.round(box.height)}`;
    };
    const draw = () => {
      const target = probe.current;
      if (!target) return;
      const market = document.querySelector('.market');
      const style = market ? getComputedStyle(market) : null;
      target.textContent = [
        `目录 ${catalog.length}`, `显示 ${visible.length}`,
        `DOM ${document.querySelectorAll('.market-card').length}`,
        `网格 ${size(document.querySelector('.market-grid'))}`,
        `首卡 ${size(document.querySelector('.market-card'))}`,
        style ? `块 ${style.display}/${style.visibility}` : '块 —',
        // A page that scrolled away looks exactly like a page that emptied, and
        // the two need different fixes.
        `滚 ${Math.round(window.scrollY)}`,
        user ? '已登录' : '未登录',
        error ? '有错误' : '',
      ].filter(Boolean).join(' · ');
    };
    draw();
    const timer = window.setInterval(draw, 500);
    document.addEventListener('click', draw, true);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('click', draw, true);
    };
  }, [catalog, visible, user, error]);

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
    {/* First thing on the page in development: when the shelf goes blank this
        line is what the reader sees in its place, and it says whether the rows
        are missing from state, from the DOM, or only from the screen. */}
    {import.meta.env.DEV && <p className="market-hint market-probe">
      <span ref={probe} />
    </p>}

    {/* A failure is reported next to the reason, not as an empty catalogue: the
        two look identical to a reader and only one of them means "there is
        nothing here". */}
    {error && <div className="market-error" role="alert">
      <span>{error}</span>
      <button type="button" className="agent-button" onClick={() => void reload()}
        disabled={retrying}>{retrying ? t('market.loading') : t('market.retry')}</button>
    </div>}
    {!user && <p className="market-hint">{t('market.signIn')}</p>}

    {/* A dropped session is a banner on top of the shelf, not a reason to take
        the shelf away. `user` goes null on any single 401 -- an expired cookie,
        one background read -- and gating the whole body on it meant one failed
        request erased a catalogue the reader was looking at. What is already on
        screen stays; only an account that never had anything sees just the hint. */}
    {(user || !!catalog.length || !!servers.length) && <>
      <header className="market-head">
        <h2>{t('market.catalogTitle')}</h2>
        <p>{t('market.catalogHint')}</p>
      </header>

      {loaded && !!catalog.length && <div className="market-browse">
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
        {/* The filter bar counts the catalogue; this counts what is below it. They
            differ only while a search or a category is narrowing the list, and
            saying so out loud is what stops "14 above, nothing below" from
            looking like a bug in the data. */}
        <p className="market-count">
          {t('market.catalogCount', { total: catalog.length, shown: visible.length })}
        </p>
      </div>}

      {!loaded && !error && <p className="market-hint">{t('market.loading')}</p>}
      {/* Rows that outlived the answer they came from are said so out loud: a
          full shelf under a refresh that returned nothing is either a slow
          server or a real removal, and the reader is the one who can tell. */}
      {stale && <p className="market-hint">{t('market.stale')}</p>}
      {loaded && !catalog.length && <p className="market-hint">{t('market.emptyCatalog')}</p>}
      {loaded && !!catalog.length && !visible.length &&
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
      {loaded && !servers.length && <p className="market-hint">{t('market.emptyMine')}</p>}
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

      {user && <details className="market-manual">
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
      </details>}
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
