import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, ApiError, errorText } from './api';
import type { ProviderStatus, State } from './api';
import { useCommand } from './commands';
import { Badge, Empty, ErrorNotice, Field, Icon, Panel } from './ui';
import './llmUsage.css';

type Effort = 'low' | 'medium' | 'high' | 'xhigh';
type Binding = {
  model: string; reasoning_effort: Effort;
  input_usd_per_million?: number | null; output_usd_per_million?: number | null;
};
type Policy = { default: Binding; roles: Record<string, Binding> };
type ModelView = {
  revision: number; configured: boolean; provider: ProviderStatus; saved: boolean; policy: Policy;
  roles: { role: string; label: string; model: string; reasoning_effort: Effort; source: string; matched_role?: string }[];
};
const efforts: [Effort, string][] = [['low', 'Low'], ['medium', 'Medium'], ['high', 'High'], ['xhigh', 'Extra high']];
const effortLabel = (effort: string) => efforts.find(([value]) => value === effort)?.[1] || effort;
const clonePolicy = (policy: Policy): Policy => JSON.parse(JSON.stringify(policy));
const roleLabel = (role: string) => role.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase());
function effectiveBinding(policy: Policy, role: string): { binding: Binding; assignment: string } {
  if (policy.roles[role]) return { binding: policy.roles[role], assignment: 'Override' };
  if (role === 'research_synthesizer' && policy.roles.campaign_manager) {
    return { binding: policy.roles.campaign_manager, assignment: 'Campaign manager' };
  }
  for (const family of ['literature_investigator', 'methodology_specialist']) {
    if (policy.roles[family] && new RegExp(`(?:^|_)${family}(?:_|$)`).test(role)) {
      return { binding: policy.roles[family], assignment: `${roleLabel(family)} family` };
    }
  }
  return { binding: policy.default, assignment: 'Default' };
}
const preset = (): Policy => ({
  default: { model: 'gpt-6-sol', reasoning_effort: 'xhigh' },
  roles: {
    campaign_manager: { model: 'gpt-6-astra', reasoning_effort: 'xhigh' },
    literature_investigator: { model: 'gpt-6-luna', reasoning_effort: 'xhigh' },
    methodology_specialist: { model: 'gpt-6-luna', reasoning_effort: 'xhigh' },
  },
});

function BindingFields({ label, value, onChange, priced }: {
  label: string; value: Binding; onChange: (value: Binding) => void; priced: boolean;
}) {
  return <div className="form-grid model-binding-fields">
    <Field label={`${label} model`}><input required maxLength={200} list="model-identifiers" value={value.model}
      onChange={event => onChange({ ...value, model: event.target.value,
        ...(priced ? { input_usd_per_million: null, output_usd_per_million: null } : {}) })} /></Field>
    <Field label={`${label} reasoning effort`}><select value={value.reasoning_effort}
      onChange={event => onChange({ ...value, reasoning_effort: event.target.value as Effort })}>
      {efforts.map(([key, title]) => <option key={key} value={key}>{title}</option>)}
    </select></Field>
    {priced && <>
      <Field label={`${label} input USD / million tokens`}><input type="number" min="0" step="any"
        value={value.input_usd_per_million ?? ''} placeholder="Required before calls"
        onChange={event => onChange({ ...value, input_usd_per_million: event.target.value === '' ? null : Number(event.target.value) })} /></Field>
      <Field label={`${label} output USD / million tokens`}><input type="number" min="0" step="any"
        value={value.output_usd_per_million ?? ''} placeholder="Required before calls"
        onChange={event => onChange({ ...value, output_usd_per_million: event.target.value === '' ? null : Number(event.target.value) })} /></Field>
    </>}
  </div>;
}

type PiModel = { provider: string; id: string; name: string; thinking_levels: string[]; family: string };
type PiModels = { mode: 'dev' | 'locked'; family: string | null; default: { provider: string; model: string; effort: string | null } | null;
  providers: Record<string, { auth: string; billing: string }>; models: PiModel[] };

/** Model and thinking-level selector; the list holds only the campaign family's signed-in models. */
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
      setNotice(agentId ? 'Saved. The agent switches at its next turn.' : 'Saved. New agents in this campaign will use this model.');
      await refresh();
      if (campaignId) setView(await api<PiModels>(`/api/campaigns/${campaignId}/agents/models`));
    } catch (reason) { setError(errorText(reason)); }
  }
  return <div className="model-control-panel">
    <h1>Agent models</h1>
    <p>{dev ? 'Dev mode: choose the model and thinking level per agent, or the campaign default for new agents. Changes take effect at the next turn.'
      : 'Each persistent session keeps its assigned model and thinking level. Model changes are available only when the Pi harness runs a dev profile.'}</p>
    {view?.family && <p>This campaign uses the <b>{view.family}</b> model family. Continuing a campaign with another family is not allowed; start a new campaign instead.</p>}
    <ErrorNotice text={error} />{notice && <p className="success-text" role="status">{notice}</p>}
    {dev && view && <><h2>Campaign default</h2>
      <ModelPicker label="Campaign default" models={view.models} value={{ provider: view.default?.provider, model: view.default?.model, effort: view.default?.effort }}
        onApply={choice => apply(null, choice)} /></>}
    <h2>Agents</h2>
    <table><thead><tr><th>Agent</th><th>Status</th><th>{dev ? 'Model and thinking' : 'Model'}</th>{!dev && <th>Thinking</th>}</tr></thead><tbody>
      {agents.map((agent: any) => <tr key={agent.id}><td>{roleLabel(agent.role)}</td><td>{agent.status}</td>
        {dev && view && !agent.grant_id
          ? <td><ModelPicker label={roleLabel(agent.role)} models={view.models} value={{ provider: agent.provider, model: agent.model, effort: agent.reasoning_effort }}
              onApply={choice => apply(agent.id, choice)} /></td>
          : <><td>{agent.provider ? `${agent.provider}/` : ''}{agent.model}{agent.grant_id ? ' (frozen with its grant)' : ''}</td>{!dev && <td>{agent.reasoning_effort ? effortLabel(agent.reasoning_effort) : 'Model default'}</td>}</>}
      </tr>)}
    </tbody></table>
    <p>Usage and cost per agent and model are on the <a href="#llm-usage">LLM usage</a> page.</p>
    <a className="button secondary" href="#notebook">Open the lead agent conversation</a>
  </div>;
}

export function ModelControlPanel({ state, refresh }: { state: State; refresh: () => Promise<void> }) {
  if (state.agent_runtime?.configuration?.enabled) return <AgentModelPanel state={state} refresh={refresh} />;
  return <LegacyModelControlPanel state={state} refresh={refresh} />;
}

function LegacyModelControlPanel({ state, refresh }: { state: State; refresh: () => Promise<void> }) {
  const command = useCommand(state.campaign);
  const [view, setView] = useState<ModelView | null>(null), [draft, setDraft] = useState<Policy | null>(null);
  const [baseRevision, setBaseRevision] = useState(0), [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [newRole, setNewRole] = useState(''), [reason, setReason] = useState('');
  const dirtyRef = useRef(false), currentRevision = useRef(-1), mounted = useRef(true);
  const campaignId = state.campaign?.id;
  function accept(next: ModelView, discard = false) {
    if (!mounted.current || next.revision < currentRevision.current) return;
    currentRevision.current = next.revision;
    setView(next);
    if (discard || !dirtyRef.current) {
      setDraft(clonePolicy(next.policy)); setBaseRevision(next.revision);
      dirtyRef.current = false; setDirty(false);
    }
  }
  async function load(discard = false) {
    if (!campaignId) return;
    try {
      const next = await api<ModelView>(`/api/campaigns/${encodeURIComponent(campaignId)}/models`);
      accept(next, discard); if (mounted.current) setError('');
    } catch (failure) { if (mounted.current) setError(errorText(failure)); }
  }
  useEffect(() => {
    mounted.current = true; void load();
    return () => { mounted.current = false; };
    // The parent keys this panel by campaign so a switch starts with a clean form.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaignId]);
  useEffect(() => {
    if (state.settings.model_policy?.policy) accept(state.settings.model_policy as ModelView);
  }, [state.settings.model_policy]);
  function edit(policy: Policy) { setDraft(policy); dirtyRef.current = true; setDirty(true); setNotice(''); }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!draft || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const policy = clonePolicy(draft);
      for (const binding of [policy.default, ...Object.values(policy.roles)]) {
        binding.model = binding.model.trim();
        if (!binding.model) throw new Error('Every assignment needs a model identifier.');
      }
      await command('models.configure', { expected_revision: baseRevision, policy,
        reason: reason.trim() || 'Updated model assignments in the campaign model control panel.' });
      dirtyRef.current = false; setDirty(false); setReason('');
      setNotice('Model settings saved for this campaign. Future calls will use these assignments.');
      await load(true); await refresh();
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 409
        ? 'Model settings changed since you opened this form. Your edits are still here. Reload saved settings, then reapply your changes.'
        : errorText(failure));
    } finally { setBusy(false); }
  }
  if (!state.campaign) return <Empty icon="spark" title="Select a campaign">Model assignments are saved separately for each campaign.</Empty>;
  if (!draft || !view) return <><ErrorNotice text={error} /><p className="help-text">Loading model settings…</p>{error && <button className="button secondary" onClick={() => void load()}>Retry</button>}</>;
  const priced = view.provider.billing_mode === 'api';
  const roles = [...new Set([...view.roles.map(item => item.role), ...Object.keys(draft.roles)])];
  return <div className="model-control-panel">
    <div className="page-heading"><div><span className="eyebrow">Campaign model policy</span><h1>Choose the models behind your agents.</h1>
      <p>Set a default for all researchers, analysts, and new roles, then override individual roles. These settings belong to {state.campaign.name}.</p></div>
      <Badge>{dirty ? 'Unsaved changes' : view.saved ? `Saved revision ${view.revision}` : 'Inherited settings'}</Badge></div>
    <div className="callout"><strong>Provider connection · {view.provider.provider === 'codex' ? 'Codex' : view.provider.provider === 'openai_api' ? 'OpenAI API' : 'Compatible API'}</strong><p>{view.configured
      ? `${priced ? 'Paid API' : 'Codex subscription'} access is configured.`
      : `Model access is unavailable: ${view.provider.status_reason || 'Configure and enable the provider on the server.'}`}</p>
      <p>Saved assignments apply to the next discovery call. Calls already dispatched, legacy research runs already started, and implementation jobs already commissioned keep their model snapshot.</p>
      {view.provider.provider === 'compatible' && <p>The compatible API transport does not send reasoning effort. Effort settings are retained for use with a provider that supports them.</p>}
      {!view.saved && <p>These are the current inherited settings. Saving establishes this campaign’s policy, replacing earlier discovery session model overrides.</p>}</div>
    <ErrorNotice text={error} />
    {view.revision > baseRevision && dirty && <p className="callout amber">A newer model policy was saved elsewhere. Reload saved settings before submitting another change.</p>}
    {notice && <p className="callout" role="status">{notice}</p>}
    <form onSubmit={save}>
      <fieldset disabled={busy} className="model-policy-fields">
        <Panel title="Default for all roles" eyebrow="Fallback assignment" action={<button type="button" className="button small secondary"
          onClick={() => edit(preset())}>Use Astra / Sol / Luna preset</button>}>
          <div className="model-panel-body"><p className="help-text">Every role without an override inherits this model and effort, including roles created later by the campaign manager.</p>
            <BindingFields label="Default" value={draft.default} priced={priced} onChange={value => edit({ ...draft, default: value })} />
            {priced && <p className="help-text">Enter current API rates for spending estimates. Calls require both rates. Changing a model clears its previous rates.</p>}
          </div>
        </Panel>
        <Panel title="Role overrides" eyebrow="Individual assignments">
          <div className="model-panel-body"><p className="help-text">The preset uses Astra for the campaign manager, Luna for literature investigators and methodology specialists, and Sol for everything else, all at Extra high effort. Literature and methodology family overrides also cover specialized roles; an exact role override takes precedence.</p>
            {Object.entries(draft.roles).map(([role, binding]) => {
              const label = view.roles.find(item => item.role === role)?.label || roleLabel(role);
              return <article className="model-role-editor" key={role}><div className="row-between"><div><h3>{label}</h3><code>{role}</code></div>
                <button type="button" className="button small secondary" aria-label={`Use inherited assignment for ${label}`} onClick={() => {
                  const overrides = { ...draft.roles }; delete overrides[role]; edit({ ...draft, roles: overrides });
                }}>Inherit assignment</button></div>
                <BindingFields label={label} value={binding} priced={priced} onChange={value => edit({ ...draft, roles: { ...draft.roles, [role]: value } })} />
              </article>;
            })}
            {!Object.keys(draft.roles).length && <p className="help-text">All roles currently use the default.</p>}
            <div className="model-add-role"><Field label="Role to override" hint="Choose a known role or enter the exact role identifier."><input list="model-role-identifiers"
              value={newRole} maxLength={100} pattern="[a-zA-Z0-9_\-]{1,100}" placeholder="e.g. problem_analyst" onChange={event => setNewRole(event.target.value)} /></Field>
              <button type="button" className="button secondary" disabled={!newRole.trim() || !!draft.roles[newRole.trim()] || !/^[a-zA-Z0-9_-]{1,100}$/.test(newRole.trim())}
                onClick={() => { edit({ ...draft, roles: { ...draft.roles, [newRole.trim()]: { ...draft.default } } }); setNewRole(''); }}><Icon name="plus" size={14} />Add override</button>
            </div>
          </div>
        </Panel>
        <Panel title={dirty ? 'Assignments after saving' : 'Current assignments'} eyebrow="All known roles">
          <div className="table-scroll"><table className="data-table"><thead><tr><th>Role</th><th>Model</th><th>Effort</th><th>Assignment</th></tr></thead>
            <tbody>{roles.map(role => {
              const savedRole = view.roles.find(item => item.role === role);
              const { binding, assignment } = !dirty && savedRole ? {
                binding: savedRole,
                assignment: savedRole.source === 'legacy' ? 'Discovery session' : savedRole.source === 'default' ? 'Default'
                  : savedRole.matched_role && savedRole.matched_role !== role ? roleLabel(savedRole.matched_role) : 'Override',
              } : effectiveBinding(draft, role);
              return <tr key={role}><td>{view.roles.find(item => item.role === role)?.label || roleLabel(role)}<small>{role}</small></td>
                <td>{binding.model}</td><td>{effortLabel(binding.reasoning_effort)}</td><td>{assignment}</td></tr>;
            })}<tr><td>Any other or future role</td><td>{draft.default.model}</td><td>{effortLabel(draft.default.reasoning_effort)}</td><td>Default</td></tr></tbody></table></div>
        </Panel>
        <Field label="Reason for change (optional)"><input value={reason} maxLength={2000} onChange={event => setReason(event.target.value)} placeholder="Record why these assignments fit the next research stage" /></Field>
        <div className="model-save-actions"><button type="button" className="button secondary" onClick={() => { setReason(''); setNewRole(''); setNotice(''); void load(true); }}>Reload saved settings</button>
          <button className="button primary" disabled={!dirty || !state.workspace_id || view.revision > baseRevision}>{busy ? 'Saving…' : 'Save model settings'}</button></div>
      </fieldset>
      <datalist id="model-identifiers">{['gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna'].map(model => <option key={model} value={model} />)}</datalist>
      <datalist id="model-role-identifiers">{roles.filter(role => !draft.roles[role]).map(role => <option key={role} value={role}>{view.roles.find(item => item.role === role)?.label || roleLabel(role)}</option>)}</datalist>
    </form>
  </div>;
}
