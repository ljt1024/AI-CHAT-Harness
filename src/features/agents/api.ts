import { t, languageHeaders } from '@/app/i18n';
import type { AgentDefinition, AgentEvent, AgentResult, ToolInfo } from './types';
import { getFileDownloadUrl } from '@/shared/utils/fileDownloads';
import { apiUrl, consumeSSE, isGuestLimit, notifyGuestLimit, postJSON, requestJSON } from '@/infrastructure/api';

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

/** The tools a definition may select, straight from the runtime's registry. */
export async function fetchToolCatalog(signal?: AbortSignal): Promise<ToolInfo[]> {
  const response = await fetch(apiUrl('/tools'), { signal, headers: languageHeaders() });
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

/**
 * What the wizard hands the server. Snake_case because it mirrors the stored
 * definition: a translated shape here would need a second mapping to stay true.
 */
export interface AgentPayload {
  id?: string;
  name: string;
  description: string;
  system_prompt: string;
  model: { provider: string; model_id: string };
  tools: string[];
  skills: string[];
  mcp_servers: string[];
  sandbox_policy: { image: string; timeout: number } | null;
  permissions: Record<string, string>;
  stop_conditions: { max_turns: number; budget: number; idle_limit: number };
}

export async function saveAgent(payload: AgentPayload, signal?: AbortSignal): Promise<AgentDefinition> {
  const response = payload.id
    ? await requestJSON('PUT', `/agents/${encodeURIComponent(payload.id)}`, payload, languageHeaders(), signal)
    : await requestJSON('POST', '/agents', payload, languageHeaders(), signal);
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

export async function deleteAgent(id: string, signal?: AbortSignal): Promise<void> {
  const response = await requestJSON('DELETE', `/agents/${encodeURIComponent(id)}`, undefined,
    languageHeaders(), signal);
  if (!response.ok) throw await readError(response);
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

/** A server this account has installed. The URL arrives masked; the token never does. */
export interface McpServerSummary {
  id: string;
  url: string;
  hasToken: boolean;
  enabled: boolean;
  private?: boolean;
  tools: { name: string }[];
}

/** An entry the deployment offers. It carries no URL: installing uses the server's. */
export interface McpCatalogEntry {
  id: string;
  name: string;
  summary: string;
  /** Operator data, not UI copy: English falls back to the Chinese text. */
  nameEn: string;
  summaryEn: string;
  category: string;
  auth: 'none' | 'token' | 'optional';
  /** Where the credential goes. Query-parameter services cannot read a bearer header. */
  tokenIn: 'header' | 'query';
  docs: string;
  verified: boolean;
  private: boolean;
  enabled: boolean;
  installed: boolean;
}

export async function fetchMcpCatalog(signal?: AbortSignal): Promise<McpCatalogEntry[]> {
  const response = await fetch(apiUrl('/mcp/catalog'), { signal, headers: languageHeaders() });
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

export async function fetchMcpServers(signal?: AbortSignal): Promise<McpServerSummary[]> {
  const response = await fetch(apiUrl('/mcp/servers'), { signal, headers: languageHeaders() });
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

/**
 * The catalogue is the only place an address comes from: this call sends a
 * credential and never a URL, so a page cannot point the host at its own network.
 */
export async function installCatalogServer(id: string, token?: string): Promise<McpServerSummary> {
  const response = await postJSON(`/mcp/catalog/${encodeURIComponent(id)}/install`,
    { token: token || undefined }, languageHeaders());
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

export async function registerMcpServer(payload: {
  id: string; url: string; token?: string;
}): Promise<McpServerSummary> {
  const response = await postJSON('/mcp/servers',
    { ...payload, token: payload.token || undefined }, languageHeaders());
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

export async function importMcpServer(config: string): Promise<McpServerSummary> {
  const response = await postJSON('/mcp/servers/import', { config }, languageHeaders());
  if (!response.ok) throw await readError(response);
  return (await response.json()).data;
}

export async function setMcpServerEnabled(id: string, enabled: boolean): Promise<void> {
  const response = await fetch(apiUrl(`/mcp/servers/${encodeURIComponent(id)}`), {
    method: 'PATCH',
    headers: { ...languageHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  if (!response.ok) throw await readError(response);
}

export async function deleteMcpServer(id: string): Promise<void> {
  const response = await requestJSON('DELETE', `/mcp/servers/${encodeURIComponent(id)}`,
    undefined, languageHeaders());
  if (!response.ok) throw await readError(response);
}
