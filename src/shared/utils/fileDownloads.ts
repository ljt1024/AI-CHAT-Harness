function fileUrl(fileId: string, action: 'download' | 'view'): string {
  const chatUrl = import.meta.env.VITE_CHAT_BASE_URL || '/api/chat/completions';
  const base = new URL(chatUrl, window.location.origin);
  return new URL(`/api/files/${encodeURIComponent(fileId)}/${action}`, base).toString();
}

export function getFileDownloadUrl(fileId: string): string {
  return fileUrl(fileId, 'download');
}

/** Same file, `inline`: the browser's own viewer draws it (PDFs only). */
export function getFileViewUrl(fileId: string): string {
  return fileUrl(fileId, 'view');
}

export function resolveFileDownloadUrl(href?: string): string | undefined {
  const match = href?.match(/^\/api\/files\/([a-f0-9-]{36})\/download$/i);
  return match ? getFileDownloadUrl(match[1]) : href;
}
