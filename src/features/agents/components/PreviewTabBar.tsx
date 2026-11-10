import { useEffect, useRef, useState } from 'react';
import { useLanguage } from '@/app/providers/LanguageContext';
import type { AgentArtifact } from '../types';
import './PreviewTabBar.css';

/**
 * The strip above the preview body. One tab per opened file, so switching between
 * a report and its stylesheet is a click instead of a trip back into the chat.
 *
 * Closing a tab closes that file only; the last one closes the panel, which is
 * what the header's × does for the whole set.
 */
export function PreviewTabBar({ tabs, activeId, available, className = '',
  showChat = false, chatActive = false, onSelect, onCloseTab, onShowChat, onOpenFile }: {
  tabs: AgentArtifact[];
  activeId: string;
  available: AgentArtifact[];
  className?: string;
  // Below the split threshold the strip also carries the way back to the chat,
  // because there the preview is a view rather than a pane beside it.
  showChat?: boolean;
  chatActive?: boolean;
  onSelect: (fileId: string) => void;
  onCloseTab: (fileId: string) => void;
  onShowChat?: () => void;
  onOpenFile: (artifact: AgentArtifact) => void;
}) {
  const { t } = useLanguage();
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isMenuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setIsMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setIsMenuOpen(false); };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [isMenuOpen]);

  // Files the run produced but that have no tab yet: the panel is a workspace, not
  // a viewer, so a file you closed must stay reachable without digging in the chat.
  const unopened = available.filter(file => !tabs.some(tab => tab.fileId === file.fileId));
  const showsTabs = showChat || tabs.length > 0;

  return <div className={`preview-tabbar ${className}`.trim()} role="tablist" aria-label={t('preview.tabs')}>
    {showChat && <button type="button" role="tab" aria-selected={chatActive}
      className={`preview-tab preview-tab--chat${chatActive ? ' is-active' : ''}`}
      onClick={onShowChat}>{t('preview.chatTab')}</button>}
    <div className="preview-tab-scroll">
      {tabs.map(file => {
        const isActive = file.fileId === activeId && !chatActive;
        return <div key={file.fileId} className={`preview-tab${isActive ? ' is-active' : ''}`}>
          <button type="button" role="tab" aria-selected={isActive} className="preview-tab-name"
            title={file.fileName} onClick={() => onSelect(file.fileId)}>{file.fileName}</button>
          <button type="button" className="preview-tab-close" title={t('preview.closeTab')}
            aria-label={`${t('preview.closeTab')}: ${file.fileName}`}
            onClick={() => onCloseTab(file.fileId)}>×</button>
        </div>;
      })}
    </div>
    {unopened.length > 0 && <div className="preview-tab-more" ref={menuRef}>
      <button type="button" className="preview-tab-add" title={t('preview.moreFiles')}
        aria-label={t('preview.moreFiles')} aria-expanded={isMenuOpen}
        onClick={() => setIsMenuOpen(value => !value)}>+</button>
      {isMenuOpen && <div className="preview-tab-more-list" role="menu">
        {unopened.map(file => <button key={file.fileId} type="button" role="menuitem"
          title={file.fileName} onClick={() => { onOpenFile(file); setIsMenuOpen(false); }}>
          {file.fileName}</button>)}
      </div>}
    </div>}
    {!showsTabs && <span className="preview-tabbar-empty" />}
  </div>;
}
