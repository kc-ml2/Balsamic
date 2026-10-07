import { useState } from 'react';
import type { ReactNode } from 'react';

// Long decimals and commit hashes are shortened for reading; hovering shows the exact text.
const TOKEN = /(?<![\w.])-?\d+\.\d+(?![\w]|\.\d)|(?<![\w])[0-9a-f]{12,64}(?![\w])/g;
const SIGNIFICANT = 6;

function shorten(raw: string): string | null {
  if (/^[0-9a-f]+$/.test(raw)) return /\d/.test(raw) && /[a-f]/.test(raw) ? raw.slice(0, 7) : null;
  const digits = raw.replace(/[-.]/g, '').replace(/^0+/, '');
  if (digits.length <= SIGNIFICANT) return null;
  return String(Number(Number(raw).toPrecision(SIGNIFICANT)));
}

function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(TOKEN)) {
    const short = shorten(match[0]);
    if (short === null) continue;
    parts.push(text.slice(last, match.index));
    parts.push(<span key={match.index} className="readable-exact" title={match[0]}>{short}</span>);
    last = match.index! + match[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}

/** Paragraphs as written; one long paragraph is split into groups of sentences. */
function paragraphs(text: string): string[][] {
  return text.split(/\n\s*\n/).map(block => block.replace(/\s+/g, ' ').trim()).filter(Boolean).map(block => {
    const sentences = block.split(/(?<=[.!?])\s+(?=[A-Z(\[])/);
    const groups: string[][] = [];
    for (const sentence of sentences) {
      const current = groups.at(-1);
      if (current && current.join(' ').length + sentence.length < 320) current.push(sentence);
      else groups.push([sentence]);
    }
    return groups;
  }).flat();
}

export function ReadableText({ text, preview = 0, className = '' }: { text?: string | null; preview?: number; className?: string }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  const groups = paragraphs(text);
  const sentences = groups.flat();
  if (preview && !open && sentences.length > preview) {
    return <div className={`readable-text ${className}`}><p>{inline(sentences.slice(0, preview).join(' '))}{' '}
      <button type="button" className="text-button readable-more" onClick={() => setOpen(true)}>Show full charter</button></p></div>;
  }
  return <div className={`readable-text ${className}`}>{groups.map((group, index) => <p key={index}>{inline(group.join(' '))}</p>)}
    {preview > 0 && sentences.length > preview && <button type="button" className="text-button readable-more" onClick={() => setOpen(false)}>Show less</button>}</div>;
}
