import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { DocumentPreview, DraftContent, HtmlPreview, SheetPreview, SlidePreview,
  type ParagraphPayload, type SheetPayload, type SlidePayload } from './LiveArtifactContent';
import { downloadAgentArtifact } from '../api';
import { formatLabel, previewKind } from '../preview';
import type { AgentArtifact } from '../types';
import { PreviewTabBar } from './PreviewTabBar';
import { getFileDownloadUrl } from '@/shared/utils/fileDownloads';
import { useLanguage } from '@/app/providers/LanguageContext';
import './ArtifactPreviewPanel.css';

// pdf.js is a megabyte of parser the panel never needs unless a PDF is open, so
// it loads as its own chunk the first time one is.
const PdfPreview = lazy(() => import('./PdfPreview'));

export function ArtifactPreviewPanel({ artifact, tabs, available, onSelect, onCloseTab,
  onOpenFile, onClose }: {
  artifact: AgentArtifact;
  tabs: AgentArtifact[];
  available: AgentArtifact[];
  onSelect: (fileId: string) => void;
  onCloseTab: (fileId: string) => void;
  onOpenFile: (artifact: AgentArtifact) => void;
  onClose: () => void;
}) {
  const { t } = useLanguage();
  const [retry, setRetry] = useState(0);
  const [fileContent, setFileContent] = useState<{ html?: string; sheets?: SheetPayload[]; slides?: SlidePayload[]; paragraphs?: ParagraphPayload[] } | null>(null);
  const [fileError, setFileError] = useState('');
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [fullscreenFallback, setFullscreenFallback] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const draft = artifact.draft;
  const kind = previewKind(artifact);
  // A delivered file either renders from its own bytes (PDF, image, HTML) or from
  // the reading the server made of it (sheets, slides, paragraphs).
  const renders = kind === 'html' || kind === 'sheets' || kind === 'slides' || kind === 'document';
  const sourceId = !draft && renders
    ? (kind === 'html' ? artifact.fileId : artifact.previewFileId) : undefined;
  // A PDF is parsed and drawn here by pdf.js rather than handed to the browser's
  // viewer: see /api/files/{id}/view for why the file must be served inline.
  const viewId = !draft && kind === 'pdf' ? artifact.fileId : undefined;

  useEffect(() => {
    setFileContent(null); setFileError('');
    if (!sourceId) return;
    const controller = new AbortController();
    void downloadAgentArtifact(sourceId, controller.signal).then(blob => blob.text()).then(content => {
      if (controller.signal.aborted) return;
      if (kind === 'html') { setFileContent({ html: content }); return; }
      const data = JSON.parse(content);
      if (kind === 'sheets') {
        if (!Array.isArray(data.sheets)) throw new Error(t('preview.invalidSheet'));
        setFileContent({ sheets: data.sheets });
      } else if (kind === 'slides') {
        if (!Array.isArray(data.slides)) throw new Error(t('preview.invalidSlides'));
        setFileContent({ slides: data.slides });
      } else {
        if (!Array.isArray(data.paragraphs)) throw new Error(t('preview.invalidDocument'));
        setFileContent({ paragraphs: data.paragraphs });
      }
    }).catch(cause => { if (!controller.signal.aborted) setFileError(String(cause.message || cause)); });
    return () => controller.abort();
  }, [sourceId, kind, retry, t]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (globalThis.document.fullscreenElement) {
        void globalThis.document.exitFullscreen();
        return;
      }
      if (fullscreenFallback) {
        setFullscreenFallback(false);
        return;
      }
      onClose();
    };
    const onFullscreenChange = () => setIsFullscreen(globalThis.document.fullscreenElement === panelRef.current);
    window.addEventListener('keydown', onKey);
    globalThis.document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => {
      window.removeEventListener('keydown', onKey);
      globalThis.document.removeEventListener('fullscreenchange', onFullscreenChange);
    };
  }, [fullscreenFallback, onClose]);

  const toggleFullscreen = async () => {
    const panel = panelRef.current;
    if (!panel) return;
    if (globalThis.document.fullscreenElement) {
      await globalThis.document.exitFullscreen();
      return;
    }
    if (fullscreenFallback) {
      setFullscreenFallback(false);
      return;
    }
    if (typeof panel.requestFullscreen === 'function') {
      try {
        await panel.requestFullscreen();
        return;
      } catch {
        // Some embedded browsers deny the native API; keep a CSS fallback.
      }
    }
    setFullscreenFallback(true);
  };

  return <aside ref={panelRef} className={`artifact-preview${isFullscreen || fullscreenFallback ? ' is-fullscreen' : ''}`} aria-label={t('preview.title')}>
    {/* The strip lives inside the pane above the split threshold and is lifted
        to the top of the workspace below it; `ArtifactPreviewPanel.css` decides
        which of the two copies is on screen. */}
    <PreviewTabBar className="artifact-preview-tabbar" tabs={tabs} activeId={artifact.fileId}
      available={available} onSelect={onSelect} onCloseTab={onCloseTab} onOpenFile={onOpenFile} />
    <header className="artifact-preview-head">
      <div><span className="artifact-preview-label">{t('preview.title')} · {formatLabel(artifact)}</span><strong>{artifact.fileName}</strong></div>
      <div className="artifact-preview-head-actions">
        {!draft && <a className="artifact-preview-download" href={getFileDownloadUrl(artifact.fileId)} download={artifact.fileName} title={t('preview.download')}>{t('preview.download')}</a>}
        <button type="button" onClick={() => void toggleFullscreen()} aria-label={isFullscreen || fullscreenFallback ? t('preview.exitFullscreen') : t('preview.openFullscreen')} title={isFullscreen || fullscreenFallback ? t('preview.exitFullscreen') : t('preview.openFullscreen')}>
          {isFullscreen || fullscreenFallback ? '↙' : '⛶'}
        </button>
        <button type="button" onClick={onClose} aria-label={t('preview.close')}>×</button>
      </div>
    </header>
    {draft && <div className={`artifact-preview-status artifact-preview-status--${draft.status}`} role="status">
      <span>{draft.status === 'generating' ? t('preview.generating') : draft.status === 'saving' ? t('preview.saving') : draft.status === 'cancelled' ? t('preview.cancelled') : t('preview.failed')}</span>
      <small>{t('preview.unsaved')}</small>
    </div>}
    <div className={`artifact-preview-body${kind === 'html' ? ' artifact-preview-body--html' : ''}`} ref={hostRef}
      aria-busy={Boolean(sourceId && !fileContent && !fileError)}>
      {draft ? <DraftContent draft={draft} /> : kind === 'image' ? <ImagePreview key={`${artifact.fileId}:${retry}`} artifact={artifact} /> : renders ? (
        fileError ? <div className="artifact-preview-error" role="alert"><p>{fileError}</p><button type="button" onClick={() => setRetry(value => value + 1)}>{t('preview.retry')}</button></div>
        : !fileContent ? <p role="status">{t('preview.loading')}</p>
        : kind === 'html' ? <HtmlPreview content={fileContent.html || ''} />
        : kind === 'sheets' ? <SheetPreview sheets={fileContent.sheets || []} />
        : kind === 'slides' ? <SlidePreview slides={fileContent.slides || []} />
        : <DocumentPreview paragraphs={fileContent.paragraphs || []} />
      ) : viewId ? (
        <Suspense fallback={<p className="artifact-pdf-note" role="status">{t('preview.loading')}</p>}>
          <PdfPreview fileId={viewId} fileName={artifact.fileName} />
        </Suspense>
      ) : <div className="artifact-preview-error"><p>{t('preview.unsupported')}</p></div>}
    </div>
    {kind === 'html' && <div className="artifact-preview-html-note">{t('preview.htmlNote')}</div>}
  </aside>;
}

function ImagePreview({ artifact }: { artifact: AgentArtifact }) {
  const { t } = useLanguage();
  const [url, setUrl] = useState('');
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = '';
    setUrl(''); setError(false);
    void downloadAgentArtifact(artifact.fileId, controller.signal).then(blob => {
      if (controller.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [artifact.fileId, retry]);
  if (error) return <div role="alert"><p>{t('preview.loadError')}</p><button type="button" onClick={() => setRetry(value => value + 1)}>{t('preview.retry')}</button></div>;
  return url ? <img className="artifact-generated-image" src={url} alt={artifact.fileName} onError={() => setError(true)} /> : <p role="status">{t('preview.loading')}</p>;
}
