import Markdown from 'react-markdown';
import type { Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import 'katex/dist/katex.min.css';

// An agent links a notebook cell as #formulation/<cell>. Written without the #, the link would leave the dashboard.
const cellLink = /^\/?formulation\/[a-z][a-z0-9_]*$/;
function destination(href?: string) { return href && cellLink.test(href) ? `#${href.replace(/^\//, '')}` : href; }
const components: Components = {
  a: ({ href, children }) => { const to = destination(href); return to ? <a href={to} target={/^https?:\/\//.test(to) ? '_blank' : undefined} rel="noreferrer">{children}</a> : <span>{children}</span>; },
  table: ({ children }) => <div className="markdown-table"><table>{children}</table></div>,
  // Messages support text markup without loading remote images from model output.
  img: ({ alt }) => <span>{alt}</span>,
};
// Equations are typeset from model output: no commands that reach outside the equation, and bounded work.
const katexOptions = { trust: false, strict: 'ignore', maxExpand: 500, maxSize: 20 } as const;

type MarkdownNode = { type: string; children?: MarkdownNode[]; position?: { start: { offset?: number } }; data?: Record<string, unknown> };
// remark-math sets $$…$$ on its own lines as a displayed equation but reads it inside a line as part of the sentence.
// Whoever writes two dollar signs means a displayed equation, so both are displayed.
function displayDoubleDollars() {
  return (tree: MarkdownNode, file: unknown) => {
    const source = String(file);
    const visit = (node: MarkdownNode) => {
      const offset = node.position?.start.offset;
      if (node.type === 'inlineMath' && offset !== undefined && source.startsWith('$$', offset)) node.data = { ...node.data, hProperties: { className: ['language-math', 'math-display'] } };
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}

// `math` reads $…$ and $$…$$ as equations. It is off for conversation, where "$1.02" is an amount.
export function TextContent({ text, math = false }: { text: unknown; math?: boolean }) {
  if (text != null && typeof text !== 'string') {
    return <div className="text-content markdown-content"><pre><code>{JSON.stringify(text, null, 2)}</code></pre></div>;
  }
  return <div className="text-content markdown-content">
    <Markdown remarkPlugins={math ? [remarkGfm, remarkMath, displayDoubleDollars] : [remarkGfm]} rehypePlugins={math ? [[rehypeKatex, katexOptions]] : []}
      components={components} skipHtml>{text || ''}</Markdown>
  </div>;
}
