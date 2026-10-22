/** Slash commands typed in the composer: each becomes a command on the bus or a request to the lead. */
export interface Slash { name: string; usage: string; help: string }
/** What /skip and the note page's button ask: the lead makes the case for each skip, and you agree. */
export const SKIP_REQUEST = 'Which of the empty sections of the problem note can be left unwritten? Propose a skip only where the confirmed charter already states the section or it does not apply here, each with your reason and what I confirm by agreeing; ask me about anything the records do not settle, and write the rest with me.';
export const SLASH: Slash[] = [
  { name: 'discuss', usage: '/discuss <topic>', help: 'Fork a thread to discuss a topic with the lead' },
  { name: 'generate', usage: '/generate N [hybrid|diversify|revise of H01 H02] [note]', help: 'Ask for N new strategies (research.generate)' },
  { name: 'review', usage: '/review H01 H02', help: 'Ask the discussion moderator to review proposals' },
  { name: 'compare', usage: '/compare [trial ids]', help: 'Compare runs at matched cost (comparison.report)' },
  { name: 'evolve', usage: '/evolve H01 [note]', help: 'Ask for a revision of one strategy' },
  { name: 'probe', usage: '/probe', help: 'Measure evaluation speed (runtime.profile)' },
  { name: 'plan', usage: '/plan', help: 'Ask the resource planner for a plan' },
  { name: 'note', usage: '/note [what to write]', help: 'Ask the lead to draft or revise the problem note' },
  { name: 'skip', usage: '/skip', help: 'Ask the lead which empty sections of the note can be left unwritten, and why' },
  { name: 'fork', usage: '/fork [title]', help: 'Start a branch from the last message' },
  { name: 'merge', usage: '/merge into <thread> <note>', help: 'Merge this branch into another with a note' },
  { name: 'park', usage: '/park', help: 'Park this branch with a summary' },
];

export type Parsed =
  | { kind: 'generate'; count: number; operation: 'diversify' | 'hybrid' | 'revise'; parents: string[]; note: string }
  | { kind: 'message'; text: string; page_agent?: string }
  | { kind: 'fork'; title: string } | { kind: 'park' } | { kind: 'merge'; into: string; note: string } | { kind: 'probe' } | { kind: 'compare'; ids: string[] }
  | { kind: 'help' } | { kind: 'unknown'; name: string };

export function parseSlash(text: string, aliases: Record<string, string> = {}): Parsed | null {
  if (!text.startsWith('/')) return null;
  const [head, ...rest] = text.slice(1).trim().split(/\s+/);
  const name = head.toLowerCase();
  const args = rest.join(' ').trim();
  const ids = (s: string) => s.split(/[\s,]+/).filter(Boolean).map(label => aliases[label] || label);
  switch (name) {
    case 'generate': {
      const tokens = args.split(/\s+/).filter(Boolean);
      let count = 3; let operation: 'diversify' | 'hybrid' | 'revise' = 'diversify'; const parents: string[] = []; const note: string[] = [];
      let i = 0;
      if (/^\d+$/.test(tokens[i] || '')) count = Math.max(1, Math.min(8, Number(tokens[i++])));
      if (['hybrid', 'diversify', 'revise'].includes(tokens[i] || '')) operation = tokens[i++] as typeof operation;
      if ((tokens[i] || '').toLowerCase() === 'of') { i++; while (i < tokens.length && /^(H\d{2}|hypothesis_[A-Za-z0-9_-]+|ph_[A-Za-z0-9_]+)$/.test(tokens[i])) parents.push(...ids(tokens[i++])); }
      note.push(...tokens.slice(i));
      return { kind: 'generate', count, operation, parents, note: note.join(' ') };
    }
    case 'evolve': { const [first, ...note] = args.split(/\s+/); return { kind: 'generate', count: 1, operation: 'revise', parents: ids(first || ''), note: note.join(' ') }; }
    case 'review': return { kind: 'message', text: `Ask the discussion moderator to review ${ids(args).join(', ') || 'the open proposals'} in a round and report the verdicts to me.` };
    case 'compare': return { kind: 'compare', ids: ids(args) };
    case 'probe': return { kind: 'probe' };
    case 'plan': return { kind: 'message', text: 'Plan how this campaign uses the machine within the resource level, and report the plan.', page_agent: 'resource_planner' };
    case 'note': return { kind: 'message', text: args ? `Revise the problem note: ${args}` : 'Draft the problem note from our conversation so far: objective, knobs, observables, noise, constraints, confounders, open questions.' };
    case 'skip': return { kind: 'message', text: SKIP_REQUEST };
    case 'discuss': return { kind: 'fork', title: args || 'Discussion' };
    case 'fork': return { kind: 'fork', title: args };
    case 'park': return { kind: 'park' };
    case 'merge': { const m = args.match(/^into\s+(\S+)\s*(.*)$/s); return m ? { kind: 'merge', into: m[1], note: m[2] || '' } : { kind: 'help' }; }
    case 'help': case '?': return { kind: 'help' };
    default: return { kind: 'unknown', name };
  }
}
