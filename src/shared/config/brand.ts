/**
 * The product's name, in one place.
 *
 * It reaches three surfaces that have no other reason to agree: the browser tab
 * (`index.html`), the welcome screen's wordmark and the file name of an exported
 * transcript. Those three drifted apart once — the tab read AI-CHAT, the welcome
 * screen AICHAT, and an export carried `ai-chat-` — because each was a string
 * literal in a different file.
 *
 * `index.html` cannot import this: it is parsed before any module runs, so its
 * `<title>` stays a literal that has to be kept in step by hand. Everything the
 * application renders reads the constant.
 */
export const APP_NAME = 'AgentForge';

/**
 * Lower-case form for file names and other places that cannot carry capitals.
 * A slug rather than `toLowerCase()` at each call site, so a future rename with a
 * space ("Agent Forge") can be turned into one token instead of leaking a space
 * into a download name.
 */
export const APP_SLUG = 'agentforge';
