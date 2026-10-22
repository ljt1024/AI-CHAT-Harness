export const MODEL_STORAGE_KEY = 'selectedModelId'
export const DEFAULT_MODEL_ID = 'deepseek-reasoner'
export const CONTEXT_HISTORY_LIMIT = 20
export const CONTEXT_MESSAGE_CHAR_LIMIT = 8000
// The Docker sandbox checkbox and the Skills & MCP panel are hidden in the product UI.
// Behaviour is unchanged: agent runs always use the Docker sandbox, and the agent still
// discovers/loads installed skills on its own. Set VITE_SHOW_SANDBOX_UI=true to expose
// both controls again (scripts/verify_*.py drive them).
export const SHOW_SANDBOX_AND_SKILLS_UI =
  (import.meta as any).env?.VITE_SHOW_SANDBOX_UI === 'true'
