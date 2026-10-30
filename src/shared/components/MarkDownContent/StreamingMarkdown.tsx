import React, { useMemo } from 'react';
import MarkdownContent from './index';
import './StreamingMarkdown.css';

/**
 * Splits streamed markdown into settled blocks and one in-flight tail.
 *
 * Reparsing the whole document on every token makes half-finished syntax flicker
 * (`**bold` renders as bold, then star, then bold again) and forces a full layout
 * pass each frame. Settled blocks never change again, so they can be memoised and
 * the tail is rendered as plain text until it closes.
 */
export interface StreamingSplit {
  blocks: string[];
  tail: string;
  /** Set when the tail started inside an unterminated fenced code block. */
  tailLanguage: string | null;
}

const FENCE_PATTERN = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)/;

export function splitStreamingMarkdown(source: string): StreamingSplit {
  if (!source) return { blocks: [], tail: '', tailLanguage: null };

  const blocks: string[] = [];
  let buffer: string[] = [];
  let fence: string | null = null;
  let fenceLanguage: string | null = null;
  let fenceLine = -1;

  const flush = () => {
    const text = buffer.join('\n').replace(/\s+$/, '');
    buffer = [];
    if (text.trim()) blocks.push(text);
  };

  const lines = source.split('\n');
  for (const line of lines) {
    const fenceMatch = FENCE_PATTERN.exec(line);
    if (fenceMatch) {
      if (fence === null) {
        fence = fenceMatch[1][0];
        fenceLanguage = fenceMatch[2] || null;
        // Index of the opening marker inside `buffer` once it is pushed below.
        fenceLine = buffer.length;
      } else if (fenceMatch[1][0] === fence) {
        fence = null;
        fenceLanguage = null;
        fenceLine = -1;
      }
    }
    buffer.push(line);
    if (fence === null && fenceMatch === null && line.trim() === '' && buffer.length > 1) {
      flush();
    }
  }

  if (fence !== null && fenceLine >= 0) {
    // Keep whatever preceded the fence as its own block, and render the code body
    // without the opening marker on its own code surface.
    const head = buffer.slice(0, fenceLine).join('\n').replace(/\s+$/, '');
    if (head.trim()) blocks.push(head);
    const code = buffer.slice(fenceLine + 1).join('\n').replace(/\s+$/, '');
    return { blocks, tail: code, tailLanguage: fenceLanguage || 'text' };
  }

  return { blocks, tail: buffer.join('\n').replace(/\s+$/, ''), tailLanguage: null };
}

const StreamingBlock = React.memo(function StreamingBlock({ text }: { text: string }) {
  return <MarkdownContent msg={text} />;
});

interface StreamingMarkdownProps {
  content: string;
  /** True while tokens are still arriving. */
  streaming?: boolean;
}

const StreamingMarkdown = React.memo(({ content, streaming = false }: StreamingMarkdownProps) => {
  const split = useMemo(
    () => (streaming ? splitStreamingMarkdown(content) : null),
    [content, streaming]
  );

  if (!streaming) return <MarkdownContent msg={content} />;
  if (!split) return null;

  const { blocks, tail, tailLanguage } = split;
  const hasBody = blocks.length > 0 || tail.length > 0;

  return (
    <div className="stream-markdown">
      {blocks.map((text, index) => <StreamingBlock key={index} text={text} />)}
      {tailLanguage !== null
        ? <pre className="stream-pending-code"><code>{tail}<span className="stream-caret" aria-hidden="true" /></code></pre>
        : tail.length > 0
          ? <div className="stream-pending-text">{tail}<span className="stream-caret" aria-hidden="true" /></div>
          : hasBody && <div className="stream-pending-text"><span className="stream-caret" aria-hidden="true" /></div>}
    </div>
  );
});

export default StreamingMarkdown;
