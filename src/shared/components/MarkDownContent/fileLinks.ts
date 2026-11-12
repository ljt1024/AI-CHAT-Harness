export interface DownloadTarget {
  name: string;
  fileId: string;
}

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
}

const escapePattern = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function linkNode(target: DownloadTarget, children: MarkdownNode[]): MarkdownNode {
  return {
    type: 'link',
    url: `/api/files/${encodeURIComponent(target.fileId)}/download`,
    children,
  };
}

/**
 * Turns a filename the model wrote into a download link.
 *
 * The match is limited to names this turn actually produced, so the text is not
 * searched for "anything that looks like a file" -- a stricter rule would link
 * `package.json` in an explanation to nothing, or worse, guess an id. Plain text
 * and inline code are both covered because models write a filename either way,
 * and code blocks are left alone: there the name is the content being shown.
 */
export function remarkFileLinks(options: { targets?: DownloadTarget[] } = {}) {
  const targets = (options.targets || []).filter(target => target.name && target.fileId);

  return (tree: MarkdownNode) => {
    if (!targets.length) return;
    const byName = new Map(targets.map(target => [target.name.toLowerCase(), target]));
    const pattern = new RegExp(`(${[...byName.keys()].map(escapePattern).join('|')})`, 'gi');

    const splitText = (value: string): MarkdownNode[] => {
      const parts: MarkdownNode[] = [];
      let last = 0;
      for (const match of value.matchAll(pattern)) {
        const written = match[1];
        const target = byName.get(written.toLowerCase());
        const index = match.index ?? 0;
        if (!target) continue;
        if (index > last) parts.push({ type: 'text', value: value.slice(last, index) });
        parts.push(linkNode(target, [{ type: 'text', value: written }]));
        last = index + written.length;
      }
      if (!parts.length) return [{ type: 'text', value }];
      if (last < value.length) parts.push({ type: 'text', value: value.slice(last) });
      return parts;
    };

    const walk = (node: MarkdownNode) => {
      if (!node.children) return;
      const next: MarkdownNode[] = [];
      for (const child of node.children) {
        if (child.type === 'text' && child.value) {
          next.push(...splitText(child.value));
          continue;
        }
        if (child.type === 'inlineCode' && child.value) {
          const written = child.value.trim();
          const target = byName.get(written.toLowerCase());
          if (target) {
            // Keep the code styling and wrap it, so a linked filename still looks
            // like the filename the model wrote rather than like prose.
            next.push(linkNode(target, [{ type: 'inlineCode', value: written }]));
            continue;
          }
        }
        // A link inside a link is invalid nesting, and a code block is literal.
        if (child.type !== 'link' && child.type !== 'code') walk(child);
        next.push(child);
      }
      node.children = next;
    };

    walk(tree);
  };
}
