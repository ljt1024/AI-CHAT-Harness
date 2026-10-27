export const MODEL_STORAGE_KEY = 'selectedModelId'
// Must match the model id the runtime serves (/api/models), otherwise the cached
// selection and the offline fallback both point at a model the backend rejects.
export const DEFAULT_MODEL_ID = 'deepseek-chat'
export const CONTEXT_HISTORY_LIMIT = 20
export const CONTEXT_MESSAGE_CHAR_LIMIT = 8000
// The Docker sandbox checkbox and the Skills & MCP panel are hidden in the product UI.
// Behaviour is unchanged: agent runs always use the Docker sandbox, the agent discovers/loads
// installed skills on its own, and every registered MCP server is attached to the run
// automatically (MCP_AUTO_ATTACH). These controls are an administration/debug surface, so
// VITE_SHOW_SANDBOX_UI=true is what scripts/verify_*.py set to drive them.
export const SHOW_SANDBOX_AND_SKILLS_UI =
  (import.meta as any).env?.VITE_SHOW_SANDBOX_UI === 'true'
