import { useEffect, useMemo, useRef, useState } from 'react';
import DOMPurify from 'dompurify';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useLanguage } from '@/app/providers/LanguageContext';
import type { AgentPreview } from '../types';

export function HtmlPreview({ content, streaming = false }: { content: string; streaming?: boolean }) {
  const latest = useRef(content);
  const [display, setDisplay] = useState(content);
  useEffect(() => { latest.current = content; if (!streaming) setDisplay(content); }, [content, streaming]);
  useEffect(() => {
    if (!streaming) return;
    const timer = window.setInterval(() => setDisplay(latest.current), 250);
    return () => window.clearInterval(timer);
  }, [streaming]);
  const { t } = useLanguage();
  const source = useMemo(() => {
    // A fresh opaque-origin, script-free document. CSS remains usable; remote resources are blocked.
    const clean = DOMPurify.sanitize(display, {
      WHOLE_DOCUMENT: true, ADD_TAGS: ['style'],
      FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'base', 'meta', 'link', 'form'],
      FORBID_ATTR: ['href', 'action', 'formaction', 'target', 'srcset'],
    });
    // Preserve the generated root classes/styles; nesting full documents loses
    // those attributes and breaks layouts that depend on them.
    const parsed = new DOMParser().parseFromString(clean, 'text/html');
    const charset = parsed.createElement('meta');
    charset.setAttribute('charset', 'utf-8');
    const policy = parsed.createElement('meta');
    policy.httpEquiv = 'Content-Security-Policy';
    policy.content = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'";
    const viewport = parsed.createElement('meta');
    viewport.name = 'viewport';
    viewport.content = 'width=device-width,initial-scale=1';
    const defaults = parsed.createElement('style');
    defaults.textContent = 'body{margin:20px;font-family:system-ui,sans-serif;overflow-wrap:anywhere}img,svg{max-width:100%}';
    parsed.head.prepend(charset, policy, viewport, defaults);
    // A generated full-screen page can hide its overflow in a narrow preview.
    // Let the iframe own scrolling, while retaining the page's internal layout.
    for (const root of [parsed.documentElement, parsed.body]) {
      root.style.setProperty('overflow', 'auto', 'important');
      root.style.setProperty('height', 'auto', 'important');
      root.style.setProperty('max-height', 'none', 'important');
    }
    return `<!doctype html>${parsed.documentElement.outerHTML}`;
  }, [display]);
  return <iframe className="artifact-html-frame" title={t('preview.liveHtml')} sandbox="" referrerPolicy="no-referrer" srcDoc={source} />;
}

const text = (value: unknown) => typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? String(value) : '';

export interface SheetPayload {
  name?: string;
  columns?: Array<string | number | boolean | null>;
  rows?: Array<Array<string | number | boolean | null>>;
}

export interface SlidePayload { title?: string; body?: string[] }

export interface ParagraphPayload { style?: string; text?: string }

export function SheetPreview({ sheets = [] }: { sheets?: SheetPayload[] }) {
  const { t } = useLanguage();
  const [selected, setSelected] = useState(0);
  const [page, setPage] = useState(0);
  const sheet = sheets[Math.min(selected, Math.max(0, sheets.length - 1))];
  const rows = Array.isArray(sheet?.rows) ? sheet.rows : [];
  const columns = Array.isArray(sheet?.columns) ? sheet.columns : [];
  const currentPage = Math.min(page, Math.max(0, Math.ceil(rows.length / 100) - 1));
  return <div className="artifact-sheet">
    {sheets.length > 1 && <div className="artifact-sheet-tabs">{sheets.map((item, i) => <button type="button" key={i} aria-pressed={i === selected} onClick={() => { setSelected(i); setPage(0); }}>{text(item?.name) || `${t('preview.sheet')} ${i + 1}`}</button>)}</div>}
    <div className="artifact-sheet-scroll"><table>
      {/* A one-row sheet has no header row to promote, so it keeps its row. */}
      {columns.length > 0 && <thead><tr>{columns.map((column, i) => <th key={i}>{text(column)}</th>)}</tr></thead>}
      <tbody>{rows.slice(currentPage * 100, (currentPage + 1) * 100).map((row, i) => <tr key={i}>{(Array.isArray(row) ? row : []).map((cell, j) => <td key={j}>{text(cell)}</td>)}</tr>)}</tbody>
    </table></div>
    <div className="artifact-sheet-pages"><button disabled={currentPage === 0} type="button" onClick={() => setPage(currentPage - 1)}>{t('preview.previous')}</button><span>{t('preview.rowCount', { count: rows.length })} · {currentPage + 1} / {Math.max(1, Math.ceil(rows.length / 100))}</span><button disabled={(currentPage + 1) * 100 >= rows.length} type="button" onClick={() => setPage(currentPage + 1)}>{t('table.next')}</button></div>
  </div>;
}

export function SlidePreview({ slides = [] }: { slides?: SlidePayload[] }) {
  const { t } = useLanguage();
  const [page, setPage] = useState(0);
  const current = Math.min(page, Math.max(0, slides.length - 1));
  const slide = slides[current];
  if (!slides.length) return <p role="status">{t('preview.emptySlides')}</p>;
  return <div className="artifact-slides">
    <article className="artifact-slide" aria-live="polite">
      <h2>{text(slide?.title) || t('preview.slideNumber', { page: current + 1 })}</h2>
      <ul>{(Array.isArray(slide?.body) ? slide.body : []).map((line, i) => <li key={i}>{text(line)}</li>)}</ul>
    </article>
    <div className="artifact-slide-pages">
      <button type="button" disabled={current === 0} onClick={() => setPage(current - 1)}>{t('preview.previous')}</button>
      <span>{t('preview.slideNumber', { page: current + 1 })} / {slides.length}</span>
      <button type="button" disabled={current >= slides.length - 1} onClick={() => setPage(current + 1)}>{t('table.next')}</button>
    </div>
  </div>;
}

export function DocumentPreview({ paragraphs = [] }: { paragraphs?: ParagraphPayload[] }) {
  const { t } = useLanguage();
  if (!paragraphs.length) return <p role="status">{t('preview.emptyDocument')}</p>;
  return <article className="artifact-document">
    {paragraphs.map((paragraph, i) => {
      const style = text(paragraph?.style);
      const heading = /^heading(\d+)$/i.exec(style);
      const level = style.toLowerCase() === 'title' ? 1
        : heading ? Math.min(Math.max(Number(heading[1]) + 1, 2), 6) : 0;
      const label = text(paragraph?.text);
      return level ? <Tag key={i} level={level}>{label}</Tag>
        : <p key={i}>{label}</p>;
    })}
  </article>;
}

function Tag({ level, children }: { level: number; children: string }) {
  const Headline = `h${Math.min(Math.max(level, 1), 6)}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6';
  return <Headline>{children}</Headline>;
}

export function DraftContent({ draft }: { draft: AgentPreview }) {
  const { t } = useLanguage();
  if (draft.format === 'png') return <article className="artifact-draft-document"><h1>{draft.title}</h1><p role="status">{draft.status === 'failed' ? t('preview.failed') : draft.status === 'cancelled' ? t('preview.cancelled') : t('preview.imageGenerating')}</p><p>{draft.content}</p></article>;
  if (draft.format === 'html') return <HtmlPreview content={draft.content || ''} streaming={draft.status === 'generating'} />;
  if (draft.format === 'xlsx') return <SheetPreview sheets={draft.sheets} />;
  if (draft.format === 'pptx') return <div className="artifact-draft-slides">{draft.slides?.map((slide, i) => <section className="artifact-draft-slide" key={i}><small>{String(i + 1).padStart(2, '0')}</small><h2>{text(slide?.title)}</h2><ul>{(Array.isArray(slide?.body) ? slide.body : []).map((line, j) => <li key={j}>{text(line)}</li>)}</ul></section>)}</div>;
  return <article className="artifact-draft-document"><h1>{draft.title}</h1><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{draft.content || ''}</ReactMarkdown></article>;
}
