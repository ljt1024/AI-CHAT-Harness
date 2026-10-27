export type AgentId = string;

export interface AgentDefinition {
  id: AgentId;
  name: string;
  description?: string;
  modelId: string;
  systemPrompt?: string;
  tools?: string[];
}

export interface AgentTask {
  id: string;
  input: string;
  agentId: AgentId;
  status: 'pending' | 'running' | 'completed' | 'failed';
}

export interface AgentRun {
  id: string;
  taskId: string;
  steps: Array<{ agentId: AgentId; output?: string; status: AgentTask['status'] }>;
}

export type AgentExecutor = (agent: AgentDefinition, input: string, signal?: AbortSignal) => Promise<string>;

export type OrchestrationStrategy = 'sequential' | 'parallel';

export interface AgentStep {
  outputTranslation?: { key: string; params?: Record<string, string | number> };
  stage?: 'preparing' | 'executing';
  id: string;
  phase: 'thought' | 'action' | 'observation';
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  agentId?: string;
  toolCallId?: string;
  output: string;
}

export interface AgentResult {
  artifacts?: AgentArtifact[];
  id: string;
  sessionId: string;
  input: string;
  output: string;
  steps: AgentStep[];
  memoryMessages: number;
}

export interface AgentArtifact {
  toolCallId?: string;
  draft?: AgentPreview;
  fileId: string;
  fileName: string;
  format: 'pdf' | 'xlsx' | 'pptx' | 'html' | 'png';
  previewFileId?: string;
  pageCount?: number;
  mimeType: string;
  size: number;
  downloadPath: string;
  createdAt: string;
}

export type AgentEvent =
  | { type: 'skill_installed'; sessionId: string; skill_id: string; name: string; sha256: string }
  | { type: 'skill_loaded'; sessionId: string; skill_id: string; name: string }
  | { type: 'skill_deleted'; sessionId: string; skill_id: string; name: string }
  | { type: 'await_approval'; call_id: string; tool: string; arguments: Record<string, unknown>; rememberable?: boolean }
  | { type: 'await_input'; call_id: string; tool: string; question: string; arguments: Record<string, unknown> }
  | { type: 'approval_resolved'; call_id: string; approved: boolean; reason: string }
  | { type: 'start'; sessionId: string; memoryMessages: number }
  | { type: 'memory'; summarizedMessages: number; recentMessages: number }
  | { type: 'context'; contextTokens: number; contextWindow: number }
  | { type: 'artifact'; artifact: AgentArtifact }
  | { type: 'step'; step: AgentStep }
  | { type: 'delta'; text: string }
  | { type: 'reflection'; consecutive: number }
  | { type: 'middleware_rejected'; message: string }
  | { type: 'done'; result: AgentResult }
  | { type: 'error'; message: string; requestId?: string };

export interface AgentPreview {
  id: string;
  toolCallId?: string;
  format: 'html' | 'pdf' | 'xlsx' | 'pptx' | 'png';
  status: 'generating' | 'saving' | 'failed' | 'cancelled';
  title?: string;
  content?: string;
  slides?: Array<{ title: string; body: string[] }>;
  sheets?: Array<{ name?: string; columns?: string[]; rows?: Array<Array<string | number | boolean | null>> }>;
}
