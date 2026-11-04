import { useCallback, useEffect, useRef, useState } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import 'react-pdf/dist/Page/TextLayer.css';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import { useLanguage } from '@/app/providers/LanguageContext';
import { getFileViewUrl } from '@/shared/utils/fileDownloads';

// pdf.js parses on a worker thread; the URL has to stay a real asset URL, so the
// import above only hands us the hashed path Vite emitted for it.
pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;

// A CJK face the document never embedded (Han reports are usually written with
// STSong-Light) is a CID font: without the Adobe CMap tables pdf.js maps every
// glyph to nothing and paints a blank page without reporting an error. The
// standard-font and wasm entries cover the other two silent-blank failures, a
// missing base-14 substitute and a JPEG2000 image. vite.config.ts copies all
// four directories to public/pdfjs.
const PDF_OPTIONS = {
  cMapUrl: '/pdfjs/cmaps/',
  cMapPacked: true,
  standardFontDataUrl: '/pdfjs/standard_fonts/',
  wasmUrl: '/pdfjs/wasm/',
  withCredentials: true,
};

const MIN_SCALE = 0.5;
const MAX_SCALE = 3;
const ZOOM_STEP = 0.1;
const PAGE_GUTTER = 28;

function clamp(value: number) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number(value.toFixed(2))));
}

// Rendering the file ourselves instead of handing it to the browser's viewer:
// that one paints its own grey chrome and ignores every style the panel has, so
// a 680px side panel ended up looking like a different application.
export default function PdfPreview({ fileId, fileName }: { fileId: string; fileName: string }) {
  const { t } = useLanguage();
  const [numPages, setNumPages] = useState(0);
  const [scale, setScale] = useState(1);
  const [currentPage, setCurrentPage] = useState(1);
  const [boxWidth, setBoxWidth] = useState(0);
  const [failure, setFailure] = useState('');
  const [attempt, setAttempt] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef<(HTMLDivElement | null)[]>([]);
  const frame = useRef(0);
  const viewUrl = getFileViewUrl(fileId);

  useEffect(() => {
    setNumPages(0); setScale(1); setCurrentPage(1); setFailure('');
    pageRefs.current = [];
  }, [fileId]);

  useEffect(() => {
    const box = scrollRef.current;
    if (!box) return;
    const sync = () => setBoxWidth(box.clientWidth);
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(box);
    return () => observer.disconnect();
  }, []);

  // The page indicator follows the scroll position, so a 20-page report reads as
  // one column and still says where it is.
  const onScroll = useCallback(() => {
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      const box = scrollRef.current;
      if (!box) return;
      const probe = box.getBoundingClientRect().top + box.clientHeight * 0.35;
      let page = 1;
      pageRefs.current.forEach((node, index) => {
        if (node && node.getBoundingClientRect().top <= probe) page = index + 1;
      });
      setCurrentPage(page);
    });
  }, []);

  useEffect(() => () => { if (frame.current) cancelAnimationFrame(frame.current); }, []);

  const goToPage = useCallback((page: number) => {
    const box = scrollRef.current;
    const node = pageRefs.current[Math.max(0, page - 1)];
    if (!box || !node) return;
    const offset = node.getBoundingClientRect().top - box.getBoundingClientRect().top;
    box.scrollTo({ top: box.scrollTop + offset - 6, behavior: 'smooth' });
    setCurrentPage(page);
  }, []);

  const pageWidth = boxWidth > 0 ? Math.max(160, Math.round((boxWidth - PAGE_GUTTER) * scale)) : 0;

  return <div className="artifact-pdf">
    <div className="artifact-pdf-bar">
      <div className="artifact-pdf-group">
        <button type="button" onClick={() => goToPage(currentPage - 1)} disabled={currentPage <= 1}
          aria-label={t('preview.previous')} title={t('preview.previous')}>‹</button>
        <span className="artifact-pdf-count">{t('preview.pdfPageOf', { current: currentPage, total: numPages || 1 })}</span>
        <button type="button" onClick={() => goToPage(currentPage + 1)} disabled={!numPages || currentPage >= numPages}
          aria-label={t('preview.next')} title={t('preview.next')}>›</button>
      </div>
      <div className="artifact-pdf-group">
        <button type="button" onClick={() => setScale(value => clamp(value - ZOOM_STEP))} disabled={scale <= MIN_SCALE}
          aria-label={t('preview.zoomOut')} title={t('preview.zoomOut')}>−</button>
        <button type="button" className="artifact-pdf-zoom" onClick={() => setScale(1)}
          title={t('preview.fitWidth')} aria-label={t('preview.fitWidth')}>{Math.round(scale * 100)}%</button>
        <button type="button" onClick={() => setScale(value => clamp(value + ZOOM_STEP))} disabled={scale >= MAX_SCALE}
          aria-label={t('preview.zoomIn')} title={t('preview.zoomIn')}>+</button>
      </div>
    </div>
    <div className="artifact-pdf-scroll" ref={scrollRef} onScroll={onScroll} role="region" aria-label={fileName}>
      {failure ? <div className="artifact-pdf-failure" role="alert">
        <p>{t('preview.pdfLoadFailed')}</p>
        <small>{failure}</small>
        <div className="artifact-pdf-failure-actions">
          <button type="button" onClick={() => { setFailure(''); setAttempt(value => value + 1); }}>{t('preview.retry')}</button>
          <a href={viewUrl} target="_blank" rel="noreferrer">{t('preview.openInNewTab')}</a>
        </div>
      </div> : <Document key={`${fileId}:${attempt}`} file={viewUrl} options={PDF_OPTIONS} suspense={false}
        onLoadSuccess={pdf => setNumPages(pdf.numPages)}
        onLoadError={cause => setFailure(cause.message)}
        onSourceError={cause => setFailure(cause.message)}
        loading={<p className="artifact-pdf-note" role="status">{t('preview.loading')}</p>}
        error={<span />}>
        {pageWidth > 0 && Array.from({ length: numPages }, (_, index) => (
          <div key={index} className="artifact-pdf-page" ref={node => { pageRefs.current[index] = node; }}>
            <Page pageNumber={index + 1} width={pageWidth} canvasBackground="#ffffff"
              onRenderError={cause => setFailure(cause.message)} />
          </div>
        ))}
      </Document>}
    </div>
  </div>;
}
