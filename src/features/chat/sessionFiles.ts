import { apiUrl, isGuestLimit, notifyGuestLimit, watchAuth } from '@/infrastructure/api';
import { languageHeaders, t } from '@/app/i18n';

/**
 * The files a conversation holds, not the files a message carries.
 *
 * They live in the conversation's workspace, so every turn of that conversation
 * can read them; the browser keeps only the descriptor. Uploading needs an
 * account (the server refuses a guest), which is why the 401 is surfaced through
 * the shared watcher instead of a toast.
 */
export interface SessionFile {
  fileId: string;
  name: string;
  size: number;
  mimeType: string;
  createdAt: string;
  downloadPath: string;
}

async function failure(response: Response): Promise<Error> {
  const payload = await response.json().catch(() => ({}));
  const detail = payload.detail;
  if (isGuestLimit(detail)) {
    notifyGuestLimit();
    return new Error(t('auth.turnLimitBody'));
  }
  const message = typeof detail === 'string' ? detail
    : (detail && typeof detail === 'object' && 'reason' in detail ? String(detail.reason) : '')
      || payload.msg || t('error.status', { status: response.status });
  return new Error(message);
}

export async function fetchSessionFiles(sessionId: string, signal?: AbortSignal): Promise<SessionFile[]> {
  const response = watchAuth(await fetch(apiUrl(`/sessions/${encodeURIComponent(sessionId)}/files`), {
    signal, headers: languageHeaders(),
  }));
  if (!response.ok) throw await failure(response);
  return (await response.json()).data as SessionFile[];
}

export async function uploadSessionFile(
  sessionId: string, file: File, signal?: AbortSignal,
): Promise<SessionFile> {
  const body = new FormData();
  // The field name is what the endpoint declares; nothing else travels with it.
  body.append('file', file);
  const response = watchAuth(await fetch(
    apiUrl(`/sessions/${encodeURIComponent(sessionId)}/files`),
    { method: 'POST', body, signal, headers: languageHeaders() }));
  if (!response.ok) throw await failure(response);
  return (await response.json()).data as SessionFile;
}

export async function deleteSessionFile(sessionId: string, fileId: string): Promise<void> {
  const response = watchAuth(await fetch(
    apiUrl(`/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileId)}`),
    { method: 'DELETE', headers: languageHeaders() }));
  if (!response.ok) throw await failure(response);
}
