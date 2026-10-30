import { t, languageHeaders } from '@/app/i18n';
import type { AgentDefinition, AgentEvent, AgentResult } from './types';
import { getFileDownloadUrl } from '@/shared/utils/fileDownloads';
import { apiUrl, consumeSSE, isGuestLimit, notifyGuestLimit, postJSON } from '@/infrastructure/api';

async function readError(response: Response): Promise<Error> {
  const payload = await response.json().catch(() => ({}));
  // A refused anonymous turn is not a failure to report in the transcript: the
  // dialog it opens explains what to do, and the message stays where it is.
  if (isGuestLimit(payload.detail)) {
    notifyGuestLimit();
    return new Error(t('auth.turnLimitBody'));
  }
  return new Error(payload.msg || payload.error?.message ||
    (typeof payload.detail === 'string' ? payload.detail : t('error.status', { status: response.status })));
}

export async function fetchAgents(signal?: AbortSignal): Promise<AgentDefinition[]> {
  const response = await fetch(apiUrl('/agents'), { signal, headers: languageHeaders() });
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

export async function downloadAgentArtifact(fileId: string, signal?: AbortSignal): Promise<Blob> {
  if (!/^[a-f0-9-]{36}$/i.test(fileId)) throw new Error(t('error.invalidFile'));
  const response = await fetch(getFileDownloadUrl(fileId), { signal, headers: languageHeaders() });
  if (!response.ok) throw await readError(response);
  return response.blob();
}

export interface AgentRequest {
  skills?: string[];
  mcp_servers?: string[];
  sandbox?: boolean;
  fileIds?: string[];
  input: string;
  sessionId: string;
  agentIds?: string[];
  model?: string;
  turnId?: string;
  /** Continue the previous run from its latest checkpoint instead of opening a new turn. */
  resume?: boolean;
}

export async function resolveApproval(id: string, approved: boolean, reason = '', remember = false): Promise<void> {
  // For a question the answer rides along as "reason"; for a gate it is the refusal note.
  const response = await postJSON(`/approvals/${encodeURIComponent(id)}`, { approved, reason, remember },
    languageHeaders());
  if (!response.ok) throw await readError(response);
}

export async function runAgents(request: AgentRequest, signal?: AbortSignal): Promise<AgentResult> {
  const response = await postJSON('/agents/run', request, languageHeaders(), signal);
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

/** Ask the runtime to cancel a running turn instead of only hanging up the SSE read. */
export async function stopSession(sessionId: string): Promise<void> {
  const response = await fetch(apiUrl(`/sessions/${encodeURIComponent(sessionId)}/stop`), {
    method: 'POST', headers: languageHeaders(),
  });
  if (!response.ok) throw await readError(response);
}

export interface SessionContext {
  tokens: number;
  window: number;
}

/**
 * The last measurement a finished run committed with the transcript. A reload has no
 * event stream to replay, so this is how the composer gauge refills.
 */
export async function fetchSessionContext(sessionId: string,
  signal?: AbortSignal): Promise<SessionContext | null> {
  const response = await fetch(apiUrl(`/sessions/${encodeURIComponent(sessionId)}/context`), {
    signal, headers: languageHeaders(),
  });
  if (!response.ok) return null;
  const data = (await response.json())?.data;
  return typeof data?.tokens === 'number' && typeof data?.window === 'number' ? data : null;
}

export async function streamAgents(request: AgentRequest, onEvent: (event: AgentEvent) => void, signal?: AbortSignal): Promise<void> {
  const response = await postJSON('/agents/run', { ...request, stream: true },
    { ...languageHeaders(), Accept: 'text/event-stream' }, signal);
  if (!response.ok) throw await readError(response);
  await consumeSSE<AgentEvent>(response, event => {
    if (event.type === 'error') throw new Error(event.message);
    onEvent(event);
  }, event => event.type === 'done', t('error.incompleteStream'));
}
