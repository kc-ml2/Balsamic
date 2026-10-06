import { useEffect, useState } from 'react';
import { api, errorText } from './api';
import type { State } from './api';
import { useCommand } from './commands';
import { ErrorNotice, Field, Icon, Panel } from './ui';
import './llmUsage.css';

const roleLabel = (role: string) => role.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase());
type PiModel = { provider: string; id: string; name: string; thinking_levels: string[]; family: string };
type PiModels = { mode: 'dev' | 'locked'; default: { provider: string; model: string; effort: string | null } | null;
  providers: Record<string, { auth: string; billing: string }>; models: PiModel[] };
type TierModel = { provider: string; model: string; effort: string | null };
type Tier = { id: string; label: string; model: TierModel };
type TierView = { mode: 'dev' | 'locked'; revision: number; saved: boolean; tiers: Tier[]; roles: Record<string, string>; assignable: string[];
  models: PiModel[]; applied?: { updated: string[]; blocked: { agent_id: string; role: string; reason: string }[] } };

/** Workspace-wide tiers: each names one model from any family; agent roles follow a tier. */
export function ModelTiersPanel({ onSaved }: { onSaved?: () => Promise<void> | void }) {
  const [view, setView] = useState<TierView | null>(null), [tiers, setTiers] = useState<Tier[]>([]), [roles, setRoles] = useState<Record<string, string>>({});
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false);
  function adopt(next: TierView) { setView(next); setTiers(next.tiers); setRoles(next.roles); }
  // A server without tier support answers without tiers; the page then omits this panel.
  useEffect(() => { api<TierView>('/api/v1/model-tiers').then(next => { if (Array.isArray(next?.tiers)) adopt(next); }).catch(reason => setError(errorText(reason))); }, []);
  if (!view) return error ? <ErrorNotice text={error} /> : null;
  const dev = view.mode === 'dev';
  const dirty = JSON.stringify({ tiers, roles }) !== JSON.stringify({ tiers: view.tiers, roles: view.roles });
  const modelKey = (m: TierModel) => `${m.provider}/${m.model}`;
  function change(index: number, model: Partial<TierModel>, label?: string) {
    setTiers(rows => rows.map((row, i) => i === index ? { ...row, label: label ?? row.label, model: { ...row.model, ...model } } : row));
  }
  function addTier() {
    let n = tiers.length + 1; while (tiers.some(t => t.id === `tier_${n}`)) n += 1;
    setTiers([...tiers, { id: `tier_${n}`, label: `Tier ${n}`, model: { ...(tiers.at(-1)?.model || { provider: '', model: '', effort: null }) } }]);
  }
  async function save() {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api<TierView>('/api/v1/model-tiers', { tiers, roles, expected_revision: view!.revision }, 'PUT');
      adopt(result);
      const moved = result.applied?.updated.length || 0, kept = result.applied?.blocked || [];
      setNotice(`Saved. ${moved} agent${moved === 1 ? '' : 's'} switch at their next turn.` + (kept.length
        ? ` ${kept.length} keep their model because their tier is now another family: ${kept.map(row => roleLabel(row.role)).join(', ')}.` : ''));
      await onSaved?.();
    } catch (reason) { setError(errorText(reason)); } finally { setBusy(false); }
  }
  return <Panel title="Model tiers" className="model-tiers">
    <div className="model-panel-body">
      <p className="help-text">Each tier names one model and thinking level, from any family. Agent roles follow a tier, so changing a tier moves every agent on it at its next turn. An agent never switches to another family, and an agent given a model of its own keeps it.</p>
      {!dev && <div className="callout">Tiers apply when the Pi harness runs a dev profile; in locked mode campaigns keep their assigned models.</div>}
      {dev && !view.saved && <div className="callout amber">Tiers are not saved yet. Until you save them, each campaign's own default model applies.</div>}
      <div className="table-scroll"><table className="data-table"><thead><tr><th>Tier</th><th>Model</th><th>Thinking</th><th>Roles</th><th /></tr></thead><tbody>
        {tiers.map((tier, index) => { const model = view.models.find(m => `${m.provider}/${m.id}` === modelKey(tier.model)), used = Object.values(roles).filter(id => id === tier.id).length;
          return <tr key={tier.id}>
            <td><input aria-label={`Tier ${index + 1} name`} value={tier.label} disabled={!dev} onChange={e => change(index, {}, e.target.value)} maxLength={60} /></td>
            <td><select aria-label={`${tier.label} model`} value={modelKey(tier.model)} disabled={!dev} onChange={e => { const [provider, ...rest] = e.target.value.split('/'); change(index, { provider, model: rest.join('/'), effort: null }); }}>
              {!model && <option value={modelKey(tier.model)}>{modelKey(tier.model)} (not signed in)</option>}
              {view.models.map(m => <option key={`${m.provider}/${m.id}`} value={`${m.provider}/${m.id}`}>{m.provider}/{m.id} · {m.family}</option>)}</select></td>
            <td><select aria-label={`${tier.label} thinking level`} value={tier.model.effort || ''} disabled={!dev} onChange={e => change(index, { effort: e.target.value || null })}>
              <option value="">Model default</option>{(model?.thinking_levels || []).map(level => <option key={level} value={level}>{level}</option>)}</select></td>
            <td>{used}</td>
            <td>{dev && tiers.length > 1 && <button type="button" className="text-button danger" disabled={used > 0} title={used ? 'Move its roles to another tier first' : undefined}
              onClick={() => setTiers(tiers.filter((_, i) => i !== index))}>Remove</button>}</td></tr>; })}
      </tbody></table></div>
      {dev && <button type="button" className="button small secondary" onClick={addTier}><Icon name="plus" size={14} />Add tier</button>}
      <h3 className="model-tiers-roles">Agent roles</h3>
      <div className="model-tier-roles">{view.assignable.map(role => <Field key={role} label={roleLabel(role)}>
        <select value={roles[role] || ''} disabled={!dev} onChange={e => setRoles(current => { const next = { ...current }; if (e.target.value) next[role] = e.target.value; else delete next[role]; return next; })}>
          <option value="">No tier · campaign default</option>{tiers.map(tier => <option key={tier.id} value={tier.id}>{tier.label}</option>)}</select></Field>)}</div>
      <ErrorNotice text={error} />{notice && <p className="success-text" role="status">{notice}</p>}
      {dev && <div className="model-save-actions"><button type="button" className="button secondary" disabled={!dirty || busy} onClick={() => adopt(view)}>Discard changes</button>
        <button type="button" className="button primary" disabled={(!dirty && view.saved) || busy} onClick={() => void save()}>{busy ? 'Saving…' : 'Save tiers'}</button></div>}
    </div></Panel>;
}

/** Model and thinking-level selector over the given signed-in models. */
function ModelPicker({ models, value, disabled, onApply, label }: { models: PiModel[]; value: { provider?: string; model?: string; effort?: string | null };
    disabled?: boolean; onApply: (choice: { provider: string; model: string; effort: string | null }) => Promise<void>; label: string }) {
  const current = `${value.provider || ''}/${value.model || ''}`;
  const [choice, setChoice] = useState(current), [effort, setEffort] = useState(value.effort || ''), [busy, setBusy] = useState(false);
  useEffect(() => { setChoice(current); setEffort(value.effort || ''); }, [current, value.effort]);
  const selected = models.find(m => `${m.provider}/${m.id}` === choice);
  const levels = selected?.thinking_levels || [];
  const changed = choice !== current || effort !== (value.effort || '');
  const known = models.some(m => `${m.provider}/${m.id}` === current);
  return <div className="model-picker">
    <select aria-label={`${label} model`} value={choice} disabled={disabled || busy} onChange={event => setChoice(event.target.value)}>
      {!known && <option value={current}>{current} (not signed in)</option>}
      {models.map(m => <option key={`${m.provider}/${m.id}`} value={`${m.provider}/${m.id}`}>{m.provider}/{m.id}</option>)}
    </select>
    <select aria-label={`${label} thinking level`} value={effort} disabled={disabled || busy} onChange={event => setEffort(event.target.value)}>
      <option value="">Model default</option>
      {levels.map(level => <option key={level} value={level}>{level}</option>)}
    </select>
    <button className="button small primary" disabled={disabled || busy || !changed || !selected || Boolean(effort && !levels.includes(effort))}
      onClick={async () => { setBusy(true); try { await onApply({ provider: selected!.provider, model: selected!.id, effort: effort || null }); } finally { setBusy(false); } }}>
      {busy ? 'Saving…' : 'Apply'}</button>
  </div>;
}

function AgentModelPanel({ state, refresh }: { state: State; refresh: () => Promise<void> }) {
  const command = useCommand(state.campaign);
  const [view, setView] = useState<PiModels | null>(null), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const campaignId = state.campaign?.id;
  useEffect(() => {
    if (!campaignId) return;
    api<PiModels>(`/api/campaigns/${campaignId}/agents/models`).then(setView).catch(reason => setError(errorText(reason)));
  }, [campaignId]);
  const agents = state.agent_runtime?.agents || [];
  const dev = view?.mode === 'dev';
  async function apply(agentId: string | null, model: { provider: string; model: string; effort: string | null }) {
    setError(''); setNotice('');
    try {
      await command('agent.configure', { agent_id: agentId, model });
      setNotice(agentId ? 'Saved. The agent switches at its next turn.' : 'Saved. New agents of roles without a tier will use this model.');
      await refresh();
      if (campaignId) setView(await api<PiModels>(`/api/campaigns/${campaignId}/agents/models`));
    } catch (reason) { setError(errorText(reason)); }
  }
  async function follow(agentId: string) {
    setError(''); setNotice('');
    try { await command('agent.configure', { agent_id: agentId, follow_tier: true }); setNotice('Saved. The agent follows its tier from its next turn.'); await refresh(); }
    catch (reason) { setError(errorText(reason)); }
  }
  const source = (agent: any) => agent.grant_id ? 'Frozen with its grant' : agent.model_source === 'agent' || (!agent.model_source && agent.model_history?.length) ? 'Own model'
    : agent.model_source === 'tier' ? `Tier · ${agent.tier}` : 'Campaign default';
  return <Panel title="Agent models" eyebrow="This campaign" className="agent-models"><div className="model-panel-body">
    <p className="help-text">{dev ? 'Agents follow their role\'s tier. Give one agent a model of its own here, or set the fallback for roles without a tier. Changes take effect at the next turn; an agent never switches to another model family.'
      : 'Each persistent session keeps its assigned model and thinking level. Model changes are available only when the Pi harness runs a dev profile.'}</p>
    <ErrorNotice text={error} />{notice && <p className="success-text" role="status">{notice}</p>}
    {dev && view && <div className="agent-models-fallback"><h3>Fallback for roles without a tier</h3>
      <ModelPicker label="Campaign default" models={view.models} value={{ provider: view.default?.provider, model: view.default?.model, effort: view.default?.effort }}
        onApply={choice => apply(null, choice)} /></div>}
    <div className="table-scroll"><table className="data-table"><thead><tr><th>Agent</th><th>Status</th><th>Source</th><th>{dev ? 'Model and thinking' : 'Model'}</th>{!dev && <th>Thinking</th>}</tr></thead><tbody>
      {agents.map((agent: any) => <tr key={agent.id}><td>{roleLabel(agent.role)}</td><td>{agent.status}</td>
        <td className="agent-model-source">{source(agent)}{dev && source(agent) === 'Own model' && <button type="button" className="text-button" onClick={() => void follow(agent.id)}>Follow tier</button>}</td>
        {dev && view && !agent.grant_id
          ? <td><ModelPicker label={roleLabel(agent.role)} models={view.models.filter(m => !agent.family || m.family === agent.family)}
              value={{ provider: agent.provider, model: agent.model, effort: agent.reasoning_effort }} onApply={choice => apply(agent.id, choice)} /></td>
          : <><td>{agent.provider ? `${agent.provider}/` : ''}{agent.model}</td>{!dev && <td>{agent.reasoning_effort || 'Model default'}</td>}</>}
      </tr>)}
    </tbody></table></div>
    <p className="help-text">Usage and cost per agent and model are on the <a href="#llm-usage">LLM usage</a> page.</p>
    <a className="button secondary" href="#notebook">Open the lead agent conversation</a>
  </div></Panel>;
}

/** The one place to choose models: workspace tiers, then this campaign's agents. */
export function ModelControlPanel({ state, refresh }: { state: State; refresh: () => Promise<void> }) {
  return <div className="model-control-panel">
    <div className="page-heading"><div><span className="eyebrow">Models</span><h1>Choose the models behind your agents.</h1>
      <p>Tiers set the models for every agent role across campaigns. Below them, this campaign's agents show which model each one uses.</p></div></div>
    <ModelTiersPanel onSaved={refresh} />
    {state.agent_runtime?.configuration?.enabled ? <AgentModelPanel state={state} refresh={refresh} />
      : <p className="help-text">{state.campaign ? 'This campaign has no agent team yet. When it starts, its agents follow the tiers above.' : 'Create a campaign to see its agents here.'}</p>}
  </div>;
}
