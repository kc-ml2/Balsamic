import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { api, errorText } from './api';
import type { Campaign, Json, State } from './api';
import { ErrorNotice, Field, Icon, Modal } from './ui';
import { newProblemManifest, ProblemManifestFields } from './evaluatorForms';
import { resolvedParameters, SchemaFields } from './schemaFields';
import { useCommand } from './commands';
import './campaignSetup.css';

type Done = (result?: Json) => void;
/** One problem instance. Field schemas are fixed when the instance is created, so typing never changes a field's type. */
export type Instance = { id?: string; name: string; split: string; problem_id: string; evaluator_manifest?: Json;
  configuration: Json; fidelity: Json; configurationSchema: Json; fidelitySchema: Json; json: { configuration: string; fidelity: string } | null };
/** A campaign's resource limits and autonomy, as the form edits them. */
export type Limits = { compute: string; implementationCompute: string; llm: string; reserve: string; delegated: string; autonomy: string };

const parseObject = (value: string, label: string): Json => {
  const parsed = JSON.parse(value || '{}');
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error(`${label} must be a JSON object.`);
  return parsed;
};
function fieldType(value: unknown): Json {
  if (Array.isArray(value)) return { type: 'array', items: { type: typeof value[0] === 'string' ? 'string' : 'number' } };
  if (typeof value === 'boolean') return { type: 'boolean' };
  if (typeof value === 'number') return { type: Number.isInteger(value) ? 'integer' : 'number' };
  if (value && typeof value === 'object') return { type: 'object' };
  return { type: 'string' };
}
/** The adapter's declared fields plus any other keys the setup already uses. */
function observedSchema(schema: Json | undefined, values: Json): Json {
  const properties: Json = { ...(schema?.properties || {}) };
  for (const [key, value] of Object.entries(values || {})) if (!(key in properties)) properties[key] = fieldType(value);
  return { ...(schema || {}), type: 'object', properties };
}
function schemaDefaults(schema: Json | undefined): Json {
  return Object.fromEntries(Object.entries(schema?.properties || {}).filter(([, p]) => (p as Json).default !== undefined).map(([k, p]) => [k, (p as Json).default]));
}
export function instanceFor(setup: Json, adapters: Json[]): Instance {
  const adapter = adapters.find(a => a.id === setup.problem_id);
  const configuration = setup.configuration || {}, fidelity = setup.fidelity || {};
  return { id: setup.id, name: setup.name || adapter?.name || 'Configuration', split: setup.split || 'development',
    problem_id: setup.evaluator_manifest?.id || setup.problem_id || '', evaluator_manifest: setup.evaluator_manifest || undefined,
    configuration, fidelity, json: null,
    configurationSchema: observedSchema(adapter?.configuration_schema, configuration), fidelitySchema: observedSchema(adapter?.fidelity_schema, fidelity) };
}
/** A new instance of `adapter`, or one whose problem is still to be chosen. */
export function blankInstance(adapter: Json | undefined, index: number): Instance {
  return instanceFor({ name: adapter?.name || `Configuration ${index + 1}`, problem_id: adapter?.id || '',
    configuration: schemaDefaults(adapter?.configuration_schema), fidelity: schemaDefaults(adapter?.fidelity_schema) }, adapter ? [adapter] : []);
}
/** A new instance of a problem the researcher declares, whose evaluator is built after the campaign exists. */
export function declaredInstance(index: number): Instance {
  const manifest = newProblemManifest(index);
  return { ...blankInstance(undefined, index), name: manifest.name, problem_id: manifest.id, evaluator_manifest: manifest };
}
function values(instance: Instance) {
  if (instance.json) return { configuration: parseObject(instance.json.configuration, 'Problem configuration'), fidelity: parseObject(instance.json.fidelity, 'Fidelity') };
  return { configuration: resolvedParameters(instance.configurationSchema, instance.configuration), fidelity: resolvedParameters(instance.fidelitySchema, instance.fidelity) };
}
/** The tasks of a campaign command. Throws when a configuration typed as JSON does not parse, or a new one has no problem.
 *  A legacy task without a problem id is sent without one, and keeps the server's default adapter. */
export function tasksPayload(instances: Instance[], { chosen = false } = {}) {
  return instances.map(row => {
    const { configuration, fidelity } = row.evaluator_manifest ? { configuration: row.configuration, fidelity: row.fidelity } : values(row);
    const problem = row.evaluator_manifest?.id || row.problem_id;
    if (chosen && !problem) throw new Error(`Choose a problem for ${row.name || 'each configuration'}.`);
    return { ...(row.id ? { id: row.id } : {}), name: row.name, split: row.split, ...(problem ? { problem_id: problem } : {}), configuration,
      ...(row.evaluator_manifest || Object.keys(fidelity).length ? { fidelity } : {}), ...(row.evaluator_manifest ? { evaluator_manifest: row.evaluator_manifest } : {}) };
  });
}
export function limitsOf(campaign?: Campaign | Json | null): Limits {
  return { compute: String(campaign?.compute_budget_seconds ?? 3600), implementationCompute: String(campaign?.implementation_compute_budget_seconds ?? 0),
    llm: String(campaign?.llm_budget_usd ?? 5), reserve: String(campaign?.validation_reserve_seconds ?? 120),
    delegated: String(campaign?.delegated_trial_seconds ?? 60), autonomy: campaign?.autonomy || 'guided' };
}
/** An example's suggested limits over the current ones. */
export function exampleLimits(limits: Limits, defaults: Json = {}): Limits {
  const pick = (key: string, current: string) => defaults[key] != null ? String(defaults[key]) : current;
  return { compute: pick('compute_budget_seconds', limits.compute), implementationCompute: pick('implementation_compute_budget_seconds', limits.implementationCompute),
    llm: pick('llm_budget_usd', limits.llm), reserve: pick('validation_reserve_seconds', limits.reserve),
    delegated: pick('delegated_trial_seconds', limits.delegated), autonomy: defaults.autonomy || limits.autonomy };
}
export function limitsPayload(limits: Limits) {
  return { compute_budget_seconds: Number(limits.compute), implementation_compute_budget_seconds: Number(limits.implementationCompute),
    llm_budget_usd: Number(limits.llm), validation_reserve_seconds: Math.min(Number(limits.reserve), Number(limits.compute)),
    delegated_trial_seconds: Number(limits.delegated), autonomy: limits.autonomy };
}

/** The installed problem adapters and the starting points offered for a new campaign. */
export function useProblemCatalog() {
  const [catalog, setCatalog] = useState<{ adapters: Json[]; examples: Json[]; loaded: boolean; error: string }>({ adapters: [], examples: [], loaded: false, error: '' });
  useEffect(() => {
    let live = true;
    Promise.all([api('/api/v1/problems'), api('/api/v1/problem-examples').catch(() => ({ examples: [] }))])
      .then(([problems, offered]) => { if (live) setCatalog({ adapters: problems.problems || [], examples: offered.examples || [], loaded: true, error: '' }); })
      .catch(e => { if (live) setCatalog(current => ({ ...current, loaded: true, error: errorText(e) })); });
    return () => { live = false; };
  }, []);
  return catalog;
}

/** Field schemas come from the catalog: rebuild them for instances made before it loaded, without touching values. */
export function withSchemas(instances: Instance[], adapters: Json[]) {
  return instances.map(row => row.evaluator_manifest || row.json ? row : { ...row, ...instanceFor({ ...row }, adapters), json: null });
}

export function LimitsFields({ limits, onChange, only }: { limits: Limits; onChange: (limits: Limits) => void; only?: 'llm' | 'others' }) {
  const set = (changes: Partial<Limits>) => onChange({ ...limits, ...changes });
  const llm = <Field label="API spending cap (USD)" hint="Applies only to paid API calls. Subscription calls are not charged here.">
    <input type="number" value={limits.llm} onChange={e => set({ llm: e.target.value })} min="0" step="0.01" required /></Field>;
  if (only === 'llm') return llm;
  return <>
    <Field label="Compute cap (seconds)" hint="Total numerical execution budget across the campaign."><input type="number" value={limits.compute} onChange={e => set({ compute: e.target.value })} min="1" step="1" required /></Field>
    <Field label="Implementation compute cap (seconds)" hint="Separate allocation for building and validating reusable implementations."><input type="number" min="0" step="1" value={limits.implementationCompute} onChange={e => set({ implementationCompute: e.target.value })} required /></Field>
    {only !== 'others' && llm}
    <Field label="Validation reserve (seconds)" hint="Held back from ordinary optimization trials."><input type="number" value={limits.reserve} onChange={e => set({ reserve: e.target.value })} min="0" max={limits.compute} required /></Field>
    <Field label="Agent per-experiment limit (seconds)" hint="The most time one experiment the agents start may use."><input type="number" value={limits.delegated} onChange={e => set({ delegated: e.target.value })} min="1" max="86400" step="1" required /></Field>
    <Field label="Research autonomy"><select value={limits.autonomy} onChange={e => set({ autonomy: e.target.value })}><option value="manual">Manual · researcher chooses each experiment</option><option value="guided">Guided · propose actions and request decisions</option><option value="delegated">Delegated · allow bounded, eligible probes</option></select></Field>
  </>;
}

/** The problem instances of a campaign: each one an installed adapter's configuration or a declared problem. */
export function ProblemInstancesEditor({ instances, onChange, adapters, onError, title = 'Problem instances', addLabel = 'Add configuration' }: {
  instances: Instance[]; onChange: (instances: Instance[]) => void; adapters: Json[]; onError: (message: string) => void; title?: string; addLabel?: string }) {
  function update(index: number, changes: Partial<Instance>) { onChange(instances.map((row, i) => i === index ? { ...row, ...changes } : row)); }
  function selectProblem(index: number, problemId: string) {
    const row = instances[index];
    if (problemId === '__commission__') { update(index, { ...declaredInstance(index), id: row.id, name: row.name, split: row.split }); return; }
    update(index, { ...blankInstance(adapters.find(a => a.id === problemId), index), id: row.id, name: row.name, split: row.split });
  }
  function toggleJson(index: number) {
    const row = instances[index];
    try {
      if (row.json) {
        const parsed = values(row), adapter = adapters.find(a => a.id === row.problem_id);
        update(index, { ...parsed, json: null, configurationSchema: observedSchema(adapter?.configuration_schema, parsed.configuration),
          fidelitySchema: observedSchema(adapter?.fidelity_schema, parsed.fidelity) });
      } else {
        const parsed = values(row);
        update(index, { json: { configuration: JSON.stringify(parsed.configuration, null, 2), fidelity: JSON.stringify(parsed.fidelity, null, 2) } });
      }
      onError('');
    } catch (e) { onError(errorText(e)); }
  }
  function add() {
    const last = instances.at(-1);
    onChange([...instances, last ? { ...last, id: undefined, name: `Configuration ${instances.length + 1}` } : blankInstance(undefined, 0)]);
  }
  return <>
    <div className="form-section-title"><h3>{title}</h3><button type="button" className="button small secondary" onClick={add}><Icon name="plus" size={15} />{addLabel}</button></div>
    <p className="help-text">Each instance fixes the problem's configuration and evaluation fidelity. Test instances remain governed by the study confirmation policy.</p>
    <div className="task-editors">{instances.map((row, index) => <div className="task-editor" key={index}><div className="form-grid">
      <Field label={`Configuration ${index + 1}`}><input value={row.name} onChange={e => update(index, { name: e.target.value })} required /></Field>
      <Field label="Evidence split"><select value={row.split} onChange={e => update(index, { split: e.target.value })}><option value="development">Development</option><option value="selection">Selection</option><option value="test">Locked test</option></select></Field>
      <Field label="Problem adapter"><select value={row.evaluator_manifest ? '__commission__' : row.problem_id} onChange={e => selectProblem(index, e.target.value)}>
        {!row.problem_id && !row.evaluator_manifest && <option value="">Choose a problem</option>}
        {adapters.map(adapter => <option key={adapter.id} value={adapter.id}>{adapter.name}</option>)}
        {row.problem_id && !row.evaluator_manifest && !adapters.some(a => a.id === row.problem_id) && <option value={row.problem_id}>{row.problem_id}</option>}
        <option value="__commission__">Declare a problem · evaluator needed</option></select></Field>
      {row.evaluator_manifest ? <div className="wide"><ProblemManifestFields value={row.evaluator_manifest} onChange={manifest => update(index, { evaluator_manifest: manifest, problem_id: manifest.id })} /></div>
        : !row.problem_id ? <p className="help-text wide">Choose an installed problem, or declare one whose evaluator is built later.</p>
        : row.json ? <>
          <Field label="Problem configuration (JSON)" hint="Use the adapter's declared fields and units." wide><textarea className="code-input" value={row.json.configuration} rows={10} spellCheck={false} onChange={e => update(index, { json: { ...row.json!, configuration: e.target.value } })} /></Field>
          <Field label="Fidelity (JSON)" wide><textarea className="code-input" value={row.json.fidelity} rows={3} spellCheck={false} onChange={e => update(index, { json: { ...row.json!, fidelity: e.target.value } })} /></Field></>
        : <>
          <fieldset className="setup-fields wide"><legend>Configuration</legend><div className="form-grid">
            {Object.keys(row.configurationSchema.properties || {}).length ? <SchemaFields schema={row.configurationSchema} values={row.configuration} setValues={configuration => update(index, { configuration })} />
              : <p className="help-text">This problem declares no configuration fields.</p>}</div></fieldset>
          {Object.keys(row.fidelitySchema.properties || {}).length > 0 && <fieldset className="setup-fields wide"><legend>Evaluation fidelity</legend><div className="form-grid">
            <SchemaFields schema={row.fidelitySchema} values={row.fidelity} setValues={fidelity => update(index, { fidelity })} /></div></fieldset>}</>}
      {!row.evaluator_manifest && row.problem_id && <div className="wide setup-instance-actions"><button type="button" className="text-button" onClick={() => toggleJson(index)}>{row.json ? 'Edit as fields' : 'Edit as JSON'}</button></div>}
    </div>{instances.length > 1 && <button type="button" className="text-button danger" onClick={() => onChange(instances.filter((_, i) => i !== index))}>Remove configuration</button>}</div>)}</div>
  </>;
}

/** Revise the campaign's charter: its name, objective, limits and problem. A campaign without a problem gets one here. */
export function CampaignForm({ state, onClose, onDone }: { state: State; onClose: () => void; onDone: Done }) {
  const [current] = useState(state.campaign);
  const command = useCommand(current);
  const catalog = useProblemCatalog();
  const [name, setName] = useState(current?.name || '');
  const [objective, setObjective] = useState(current?.objective || '');
  const [limits, setLimits] = useState(() => limitsOf(current));
  const [instances, setInstances] = useState<Instance[]>(() => state.tasks.map(t => instanceFor({ ...t,
    configuration: t.evaluator_manifest || t.configuration ? t.configuration || {} : t.physics }, [])));
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  useEffect(() => { if (catalog.loaded) setInstances(rows => withSchemas(rows, catalog.adapters)); }, [catalog.loaded, catalog.adapters]);

  const undefinedProblem = !state.tasks.length;
  async function save(event: FormEvent) {
    event.preventDefault(); setError(''); setBusy(true);
    try {
      // Without instances the problem is left as it is: still to be defined.
      const result = await command('campaign.update', { name, objective, ...limitsPayload(limits),
        ...(instances.length ? { tasks: tasksPayload(instances, { chosen: undefinedProblem }) } : {}) });
      onDone(result.campaign);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  return <Modal wide title="Revise the experiment charter" description={undefinedProblem
    ? 'This campaign has no problem yet. Attach one when the formulation is ready: an installed problem, or a declared one whose evaluator is built later.'
    : 'Changes create a new charter version. Earlier experiments retain their original problem definitions.'} onClose={onClose}><form onSubmit={save}>
    <div className="form-grid"><Field label="Campaign name" wide><input value={name} onChange={e => setName(e.target.value)} required maxLength={180} /></Field>
      <Field label="Research objective" wide><textarea value={objective} onChange={e => setObjective(e.target.value)} rows={4} required /></Field>
      <LimitsFields limits={limits} onChange={setLimits} /></div>
    <ProblemInstancesEditor instances={instances} onChange={setInstances} adapters={catalog.adapters} onError={setError}
      addLabel={instances.length ? 'Add configuration' : 'Attach a problem'} />
    {undefinedProblem && !instances.length && <p className="help-text">The problem stays undefined until you attach one.</p>}
    <ErrorNotice text={error || catalog.error} />
    <div className="modal-actions"><button className="button secondary" type="button" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{busy ? 'Saving…' : 'Save new version'}<Icon name="arrow" size={16} /></button></div>
  </form></Modal>;
}
