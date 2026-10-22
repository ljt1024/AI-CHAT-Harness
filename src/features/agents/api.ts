import { t, languageHeaders } from '@/app/i18n';
import type { AgentDefinition, AgentEvent, AgentResult } from './types';
import { getFileDownloadUrl } from '@/shared/utils/fileDownloads';
import { apiUrl, consumeSSE, postJSON } from '@/infrastructure/api';

async function readError(response: Response): Promise<Error> {
  const payload = await response.json().catch(() => ({}));
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
}

export async function resolveApproval(id: string, approved: boolean): Promise<void> {
  const response = await postJSON(`/approvals/${encodeURIComponent(id)}`, { approved }, languageHeaders());
  if (!response.ok) throw await readError(response);
}

export async function runAgents(request: AgentRequest, signal?: AbortSignal): Promise<AgentResult> {
  const response = await postJSON('/agents/run', request, languageHeaders(), signal);
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
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
