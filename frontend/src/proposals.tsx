import { useState } from 'react';
import type { Hypothesis, State } from './api';
import type { WorkspaceActions } from './views';
import { Field, Modal } from './ui';

export type ProposalOperation = 'expand' | 'diversify' | 'hybrid';

export function ProposalComposer({ state, actions, operation, parent, onClose }: {
  state: State; actions: WorkspaceActions; operation: ProposalOperation; parent?: Hypothesis; onClose: () => void;
}) {
  const parents = state.hypotheses.filter(h => h.status !== 'archived');
  const [first, setFirst] = useState(parent?.id || parents[0]?.id || '');
  const [second, setSecond] = useState(parents.find(h => h.id !== first)?.id || '');
  const [count, setCount] = useState(operation === 'hybrid' ? 2 : 3);
  const [direction, setDirection] = useState('');
  const [busy, setBusy] = useState(false);
  const session = state.research_progress?.session;
  const piOwned = !!state.agent_runtime?.configuration?.enabled;
  const hasDiscovery = piOwned || Boolean(session && !['completed', 'stopped', 'exhausted'].includes(session.status));
  const title = { expand: 'Generate more proposals', diversify: 'Diversify a proposal', hybrid: 'Combine two proposals' }[operation];
  const valid = operation === 'expand' || !!first && (operation !== 'hybrid' || !!second && second !== first);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!hasDiscovery || !valid || busy) return;
    setBusy(true);
    const ids = operation === 'expand' ? [] : operation === 'diversify' ? [first] : [first, second];
    const names = ids.map(id => parents.find(h => h.id === id)!.title);
    const purpose = operation === 'expand'
      ? 'Explore additional mechanisms beyond the current proposals. Compare against existing ideas and explain the new coverage.'
      : operation === 'diversify'
        ? `Develop substantive alternative mechanisms from "${names[0]}". Explain the differences; parameter tuning alone is not diversification.`
        : `Creatively combine "${names[0]}" and "${names[1]}". Explain each parent's contribution, how they interact, and possible conflicts. A hybrid must be a coherent algorithm.`;
    const accepted = await actions.research(`Request ${count} proposals. ${purpose}\n${direction.trim() ? `Researcher direction: ${direction.trim()}\n` : ''}Use the problem analysis and literature evidence. Preserve the originals and exact parent links. Have a separate reviewer judge conceptual plausibility in writing. Only after a test verdict and implementation validation, design bounded quick tests with rough tuning, multiple seeds, and parent/baseline comparisons. Evaluate the evidence before further resources.`,
      'generate', undefined, undefined, { proposal_operation: operation, parent_hypothesis_ids: ids, proposal_count: count });
    setBusy(false);
    if (accepted) onClose();
  }
  return <Modal title={title} description="The campaign manager coordinates generation, independent review, and empirical follow-up." onClose={onClose}>
    <form onSubmit={submit}>
      {!hasDiscovery && <div className="callout" role="status"><strong>Start discovery to generate proposals</strong><p>Open Research notebook → Discovery, choose the problem and session limits, then select Start discovery. Return here to request proposals.</p><a className="button secondary" href="#notebook/discovery" onClick={onClose}>Set up discovery</a></div>}
      {session?.status === 'paused' && <div className="callout proposal-paused-notice" role="status"><strong>Discovery is paused</strong><p>Your request will be saved in the queue. Select Resume discovery in the progress panel when you want the agents to begin.</p></div>}
      {operation !== 'expand' && <Field label={operation === 'hybrid' ? 'First parent proposal' : 'Parent proposal'}>
        <select aria-label="First parent proposal" value={first} onChange={e => setFirst(e.target.value)}>
          {parents.map(h => <option key={h.id} value={h.id}>{h.title}</option>)}
        </select>
      </Field>}
      {operation === 'hybrid' && <Field label="Second parent proposal">
        <select aria-label="Second parent proposal" value={second} onChange={e => setSecond(e.target.value)}>
          <option value="">Select another proposal</option>
          {parents.filter(h => h.id !== first).map(h => <option key={h.id} value={h.id}>{h.title}</option>)}
        </select>
      </Field>}
      <Field label="Requested proposal count"><input aria-label="Requested proposal count" type="number" min={1} max={6} value={count} onChange={e => setCount(Number(e.target.value))} required /></Field>
      <Field label="Direction or constraints (optional)"><textarea aria-label="Direction or constraints" rows={4} value={direction} onChange={e => setDirection(e.target.value)} placeholder="For example: combine tabu memory with surrogate ranking, while keeping startup costs low." /></Field>
      <p className="help-text">{piOwned ? 'The lead agent delegates generation, independent review and implementation within this campaign’s allocations.' : 'Requires an optimizer discovery session. A paused session keeps this request queued until you resume it. Missing implementations are handled by the separate implementation service. Existing resource limits apply.'}</p>
      <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy || !valid || !hasDiscovery}>{busy ? 'Sending…' : 'Send to campaign manager'}</button></div>
    </form>
  </Modal>;
}
