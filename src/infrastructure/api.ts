export function apiUrl(path: string): string {
  const base = import.meta.env.VITE_CHAT_BASE_URL || '/api/chat/completions';
  return new URL(`/api${path}`, new URL(base, window.location.origin)).toString();
}

export async function consumeSSE<T>(
  response: Response,
  onEvent: (event: T) => void,
  isDone: (event: T) => boolean,
  incompleteMessage: string,
): Promise<void> {
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error(incompleteMessage);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed = false;
  const parse = (block: string) => {
    const data = block.split('\n').filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart()).join('\n');
    if (!data) return;
    const event = JSON.parse(data) as T;
    completed ||= isDone(event);
    onEvent(event);
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      // Normalize only complete frames: CRLF may straddle network chunks.
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer)) !== null) {
        parse(buffer.slice(0, match.index).replace(/\r\n/g, '\n'));
        buffer = buffer.slice(match.index + match[0].length);
      }
      if (done) break;
    }
    if (buffer.trim()) parse(buffer);
    if (!completed) throw new Error(incompleteMessage);
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function postJSON(path: string, body: unknown, headers: HeadersInit, signal?: AbortSignal) {
  return fetch(apiUrl(path), {
    method: 'POST', signal, headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
