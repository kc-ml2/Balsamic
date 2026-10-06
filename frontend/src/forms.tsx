import { useState } from 'react';
import type { FormEvent } from 'react';
import { errorText } from './api';
import type { Campaign, Hypothesis, Json, State, Trial } from './api';
import { ErrorNotice, Field, Icon, Modal } from './ui';
import { useCommand } from './commands';

const lines = (value: string) => value.split('\n').map(s => s.trim()).filter(Boolean);
const parseObject = (value: string): Json => { const parsed = JSON.parse(value || '{}'); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Configuration must be a JSON object.'); return parsed; };
type Done = (result?: Json) => void;

export { CampaignForm } from './campaignSetup';

export function HypothesisForm({ state, parent, onClose, onDone }: { state: State; parent?: Hypothesis; onClose: () => void; onDone: Done }) {
  const [campaign] = useState(state.campaign);
  const command = useCommand(campaign);
  const [title, setTitle] = useState(parent ? `${parent.title} · revision` : '');
  const [mechanism, setMechanism] = useState(parent?.mechanism || '');
  const [rationale, setRationale] = useState(parent?.rationale || '');
  const [assumptions, setAssumptions] = useState((parent?.assumptions || []).map(a => typeof a === 'string' ? a : JSON.stringify(a)).join('\n'));
  const [risks, setRisks] = useState((parent?.risks || []).join('\n'));
  const [sources, setSources] = useState((parent?.sources || []).map(s => typeof s === 'string' ? s : s.url).join('\n'));
  const [algorithm, setAlgorithm] = useState(parent?.algorithm || '');
  const [source, setSource] = useState(parent?.source || '');
  const [config, setConfig] = useState(JSON.stringify(parent?.algorithm_config || {}, null, 2));
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  async function save(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('');
    try { const result = await command('hypothesis.create', { campaign_id: campaign!.id, title, mechanism, rationale, assumptions: lines(assumptions), risks: lines(risks), sources: lines(sources), algorithm, algorithm_config: parseObject(config), parent_ids: parent ? [parent.id] : [], status: 'proposed', source: algorithm === 'custom' ? source : undefined }); onDone(result.hypothesis); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  return <Modal wide title={parent ? 'Evolve this strategy' : 'Contribute a hypothesis'} description={parent ? 'Create a new identity linked to its parent. The existing strategy and its evidence are preserved.' : 'Make the mechanism and rationale explicit. An idea can be promising before it has a measured result.'} onClose={onClose}><form onSubmit={save}><div className="form-grid"><Field label="Strategy title" wide><input value={title} onChange={e => setTitle(e.target.value)} required placeholder="e.g. Boundary-aware block search" /></Field><Field label="Proposed mechanism" wide><textarea value={mechanism} onChange={e => setMechanism(e.target.value)} required rows={3} placeholder="What changes in the search, and why could that improve it?" /></Field><Field label="Rationale and cheapest useful test" wide><textarea value={rationale} onChange={e => setRationale(e.target.value)} required rows={4} placeholder="Connect the mechanism to this physical problem. What observation would change your mind?" /></Field><Field label="Assumptions" hint="One per line."><textarea value={assumptions} onChange={e => setAssumptions(e.target.value)} rows={3} /></Field><Field label="Risks and counterarguments" hint="One per line."><textarea value={risks} onChange={e => setRisks(e.target.value)} rows={3} /></Field><Field label="Sources" wide hint="One paper or URL per line. Sources support rationale, not a measured performance claim."><textarea value={sources} onChange={e => setSources(e.target.value)} rows={2} /></Field><Field label="Runnable implementation"><select value={algorithm} onChange={e => setAlgorithm(e.target.value)}><option value="">Proposal only · implementation needed</option><option value="custom">Custom Python strategy</option>{state.algorithms.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></Field><Field label="Algorithm parameters (JSON)"><textarea className="code-input" value={config} onChange={e => setConfig(e.target.value)} rows={3} spellCheck={false} /></Field>{algorithm === 'custom' && <Field wide label="Optimizer source (Python)" hint="Standard library only. Each call starts a fresh process, so return all persistent state explicitly as JSON. Save this legacy source, then import it through the independent implementation service before launching."><textarea className="code-input" value={source} onChange={e => setSource(e.target.value)} rows={12} required spellCheck={false} placeholder={'def initialize(n_cells, seed, config):\n    return {"n_cells": n_cells, "seed": seed}\n\ndef propose(state):\n    # Return {"design": [0, 1, ...], "state": state}\n    ...\n\ndef observe(state, design, efficiency):\n    return state'} /></Field>}</div><ErrorNotice text={error} /><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{busy ? 'Saving…' : 'Save hypothesis'}<Icon name="branch" size={16} /></button></div></form></Modal>;
}

export { TrialForm } from './experimentForm';

export function TrialActionForm({ trial, campaign, action, onClose, onDone }: { trial: Trial; campaign: Campaign | null; action: 'extend' | 'validate' | 'prioritize'; onClose: () => void; onDone: Done }) {
  const [charter] = useState(campaign);
  const command = useCommand(charter);
  const [steps, setSteps] = useState(String(trial.max_steps * 2));
  const [wall, setWall] = useState(String(action === 'validate' ? 60 : trial.wall_seconds * 2));
  const [orders, setOrders] = useState('25, 40, 60'), [priority, setPriority] = useState(String((trial.priority || 0) + 1));
  const [designs, setDesigns] = useState('3');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  async function save(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('');
    try {
      const body = action === 'validate' ? { orders: orders.split(',').map(n => Number(n.trim())), max_designs: Number(designs), wall_seconds: Number(wall) } : action === 'extend' ? { action, max_steps: Number(steps), wall_seconds: Number(wall) } : { action, priority: Number(priority) };
      if (action === 'validate' && body.orders?.some(n => !Number.isInteger(n) || n < 1)) throw new Error('Fourier orders must be positive integers separated by commas.');
      const result = await command(action === 'validate' ? 'trial.validate' : 'trial.control', {
        ...body, trial_id: trial.id, ...(action === 'validate' ? {} : { expected_control_revision: trial.control_revision ?? 0 }),
      });
      onDone(result.trial);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  const title = action === 'validate' ? 'Check physical convergence' : action === 'extend' ? 'Extend the evidence window' : 'Reprioritize this trial';
  return <Modal title={title} description={action === 'validate' ? 'Reevaluate archived designs at higher Fourier orders. Validation uses the campaign compute budget.' : action === 'extend' ? 'Set larger total limits. The existing schedule and checkpoint remain part of this trial.' : 'Higher values move queued work earlier. A running trial is not interrupted.'} onClose={onClose}><form onSubmit={save}><div className="form-grid">{action === 'extend' && <Field label="New total evaluation limit"><input type="number" min={trial.max_steps} value={steps} onChange={e => setSteps(e.target.value)} required /></Field>}{action !== 'prioritize' && <Field label={action === 'extend' ? 'New total time cap (seconds)' : 'Validation time cap (seconds)'}><input type="number" min={action === 'extend' ? trial.wall_seconds : 1} value={wall} onChange={e => setWall(e.target.value)} required /></Field>}{action === 'validate' && <><Field label="Fourier orders" hint="Comma-separated; use increasing orders."><input value={orders} onChange={e => setOrders(e.target.value)} required /></Field><Field label="Number of archived designs"><input type="number" min="1" max="10" value={designs} onChange={e => setDesigns(e.target.value)} required /></Field></>}{action === 'prioritize' && <Field label="Queue priority"><input type="number" value={priority} onChange={e => setPriority(e.target.value)} step="1" required /></Field>}</div><ErrorNotice text={error} /><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{busy ? 'Saving…' : action === 'validate' ? 'Run validation' : 'Update trial'}</button></div></form></Modal>;
}

export function campaignUsage(campaign: Campaign | null, trials: Trial[]) {
  const compute = campaign?.compute_used_seconds ?? campaign?.usage?.compute_seconds ?? campaign?.compute_spent_seconds ?? trials.reduce((sum, t) => sum + (t.progress?.elapsed_seconds || 0), 0);
  const llm = campaign?.llm_used_usd ?? campaign?.usage?.llm_usd ?? campaign?.llm_spent_usd ?? 0;
  return { compute, llm };
}
