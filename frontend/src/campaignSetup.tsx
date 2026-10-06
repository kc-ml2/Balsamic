import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, errorText } from './api';
import type { Json, State } from './api';
import { Badge, ErrorNotice, Field, Icon, Modal } from './ui';
import { newProblemManifest, ProblemManifestFields } from './evaluatorForms';
import { resolvedParameters, SchemaFields } from './schemaFields';
import { useCommand } from './commands';
import './campaignSetup.css';

type Done = (result?: Json) => void;
/** One problem instance. Field schemas are fixed when the instance is created, so typing never changes a field's type. */
type Instance = { id?: string; name: string; split: string; problem_id: string; evaluator_manifest?: Json;
  configuration: Json; fidelity: Json; configurationSchema: Json; fidelitySchema: Json; json: { configuration: string; fidelity: string } | null };

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
function instanceFor(setup: Json, adapters: Json[]): Instance {
  const adapter = adapters.find(a => a.id === setup.problem_id);
  const configuration = setup.configuration || {}, fidelity = setup.fidelity || {};
  return { id: setup.id, name: setup.name || adapter?.name || 'Configuration', split: setup.split || 'development',
    problem_id: setup.evaluator_manifest?.id || setup.problem_id || '', evaluator_manifest: setup.evaluator_manifest || undefined,
    configuration, fidelity, json: null,
    configurationSchema: observedSchema(adapter?.configuration_schema, configuration), fidelitySchema: observedSchema(adapter?.fidelity_schema, fidelity) };
}
function blankInstance(adapter: Json | undefined, index: number): Instance {
  return instanceFor({ name: adapter?.name || `Configuration ${index + 1}`, problem_id: adapter?.id || '',
    configuration: schemaDefaults(adapter?.configuration_schema), fidelity: schemaDefaults(adapter?.fidelity_schema) }, adapter ? [adapter] : []);
}
function values(instance: Instance) {
  if (instance.json) return { configuration: parseObject(instance.json.configuration, 'Problem configuration'), fidelity: parseObject(instance.json.fidelity, 'Fidelity') };
  return { configuration: resolvedParameters(instance.configurationSchema, instance.configuration), fidelity: resolvedParameters(instance.fidelitySchema, instance.fidelity) };
}

export function CampaignForm({ state, editing, initial, onClose, onDone, onImport }: { state: State; editing?: boolean; initial?: Json;
  onClose: () => void; onDone: Done; onImport?: () => void }) {
  const [current] = useState(editing ? state.campaign : null);
  const command = useCommand(current);
  const [name, setName] = useState(current?.name || 'Optimizer research');
  const [objective, setObjective] = useState(current?.objective || 'Develop an effective optimizer for the selected problem under the declared resource budget.');
  const [compute, setCompute] = useState(String(current?.compute_budget_seconds ?? 3600));
  const [implementationCompute, setImplementationCompute] = useState(String(current?.implementation_compute_budget_seconds ?? 0));
  const [llm, setLlm] = useState(String(current?.llm_budget_usd ?? 5));
  const [reserve, setReserve] = useState(String(current?.validation_reserve_seconds ?? 120));
  const [delegated, setDelegated] = useState(String(current?.delegated_trial_seconds ?? 60));
  const [autonomy, setAutonomy] = useState(current?.autonomy || 'guided');
  const [adapters, setAdapters] = useState<Json[]>([]), [examples, setExamples] = useState<Json[]>([]);
  const [instances, setInstances] = useState<Instance[]>(() => current ? state.tasks.map(t => instanceFor({ ...t,
    configuration: t.evaluator_manifest || t.configuration ? t.configuration || {} : t.physics }, [])) : [blankInstance(undefined, 0)]);
  const [selected, setSelected] = useState(''), touched = useRef(Boolean(current));
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    Promise.all([api('/api/v1/problems'), api('/api/v1/problem-examples').catch(() => ({ examples: [] }))]).then(([catalog, offered]) => {
      if (!live) return;
      const definitions: Json[] = catalog.problems || [], available: Json[] = offered.examples || [];
      setAdapters(definitions); setExamples(available);
      // Field schemas come from the catalog; rebuild them without touching values.
      setInstances(rows => rows.map(row => row.evaluator_manifest ? row : { ...row, ...instanceFor({ ...row }, definitions), json: row.json }));
      if (initial) { apply(initial, definitions, true); return; }
      if (!touched.current) {
        if (available.length) apply(available[0], definitions, false);
        else if (definitions.length) setInstances([blankInstance(definitions[0], 0)]);
      }
    }).catch(e => { if (live) setError(errorText(e)); });
    return () => { live = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Use an example or an imported draft. Charter fields change only when explicitly chosen. */
  function apply(example: Json, definitions = adapters, charter = true) {
    setSelected(example.id || 'initial');
    setInstances((example.instances || []).map((setup: Json) => instanceFor(setup, definitions)));
    const defaults = example.campaign || {};
    if (!charter) return;
    if (defaults.name) setName(defaults.name);
    if (defaults.objective) setObjective(defaults.objective);
    if (defaults.compute_budget_seconds != null) setCompute(String(defaults.compute_budget_seconds));
    if (defaults.validation_reserve_seconds != null) setReserve(String(defaults.validation_reserve_seconds));
    if (defaults.implementation_compute_budget_seconds != null) setImplementationCompute(String(defaults.implementation_compute_budget_seconds));
    if (defaults.delegated_trial_seconds != null) setDelegated(String(defaults.delegated_trial_seconds));
    if (defaults.llm_budget_usd != null) setLlm(String(defaults.llm_budget_usd));
    if (defaults.autonomy) setAutonomy(defaults.autonomy);
  }
  function update(index: number, changes: Partial<Instance>) { touched.current = true; setInstances(rows => rows.map((row, i) => i === index ? { ...row, ...changes } : row)); }
  function selectProblem(index: number, problemId: string) {
    const row = instances[index];
    if (problemId === '__commission__') { update(index, { problem_id: newProblemManifest(index).id, evaluator_manifest: newProblemManifest(index), configuration: {}, fidelity: {}, json: null }); return; }
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
      setError('');
    } catch (e) { setError(errorText(e)); }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setError(''); setBusy(true);
    try {
      const tasks = instances.map(row => {
        const { configuration, fidelity } = row.evaluator_manifest ? { configuration: row.configuration, fidelity: row.fidelity } : values(row);
        const problem = row.evaluator_manifest?.id || row.problem_id;
        return { ...(row.id ? { id: row.id } : {}), name: row.name, split: row.split, ...(problem ? { problem_id: problem } : {}), configuration,
          ...(row.evaluator_manifest || Object.keys(fidelity).length ? { fidelity } : {}), ...(row.evaluator_manifest ? { evaluator_manifest: row.evaluator_manifest } : {}) };
      });
      const payload = { name, objective, compute_budget_seconds: Number(compute), implementation_compute_budget_seconds: Number(implementationCompute),
        llm_budget_usd: Number(llm), validation_reserve_seconds: Math.min(Number(reserve), Number(compute)), delegated_trial_seconds: Number(delegated), autonomy, tasks };
      const result = await command(current ? 'campaign.update' : 'campaign.create', payload);
      onDone(result.campaign);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  const adapterName = (id: string) => adapters.find(a => a.id === id)?.name || id;
  return <Modal wide title={current ? 'Revise the experiment charter' : 'Start a research campaign'} description={current ? 'Changes create a new charter version. Earlier experiments retain their original problem definitions.' : 'Choose a starting problem, then define the scientific question and boundaries. You can revise them as evidence develops.'} onClose={onClose}><form onSubmit={save}>
    {!current && <section className="setup-start" aria-label="Starting point"><h3>Start from</h3><div className="setup-cards">
      {examples.map(example => <button type="button" key={example.id} className={`setup-card ${selected === example.id ? 'selected' : ''}`} aria-pressed={selected === example.id} onClick={() => { touched.current = true; apply(example); }}>
        <strong>{example.name}</strong><span>{example.summary}</span>
        <small>{[...new Set((example.instances || []).map((s: Json) => s.evaluator_manifest ? 'Declared problem' : adapterName(s.problem_id)))].join(' · ')}{example.source === 'saved' && <Badge>Saved</Badge>}</small></button>)}
      {onImport && <button type="button" className="setup-card import" onClick={onImport}><strong><Icon name="notebook" size={16} />Import from documents</strong><span>Upload a paper and optional code; a model drafts the problem for your review.</span></button>}
      <button type="button" className={`setup-card ${selected === 'blank' ? 'selected' : ''}`} aria-pressed={selected === 'blank'} onClick={() => { touched.current = true; setSelected('blank'); setInstances([blankInstance(adapters[0], 0)]); }}>
        <strong>Blank problem</strong><span>Pick an installed problem adapter, or declare a new problem whose evaluator is built later.</span></button>
    </div></section>}
    <div className="form-grid"><Field label="Campaign name" wide><input value={name} onChange={e => setName(e.target.value)} required maxLength={180} /></Field>
      <Field label="Research objective" wide><textarea value={objective} onChange={e => setObjective(e.target.value)} rows={4} required /></Field>
      <Field label="Compute cap (seconds)" hint="Total numerical execution budget across the campaign."><input type="number" value={compute} onChange={e => setCompute(e.target.value)} min="1" step="1" required /></Field>
      <Field label="Implementation compute cap (seconds)" hint="Separate allocation for building and validating reusable implementations."><input type="number" min="0" step="1" value={implementationCompute} onChange={e => setImplementationCompute(e.target.value)} required /></Field>
      <Field label="API spending cap (USD)" hint="Applies only to paid API calls. Subscription calls are not charged here."><input type="number" value={llm} onChange={e => setLlm(e.target.value)} min="0" step="0.01" required /></Field>
      <Field label="Validation reserve (seconds)" hint="Held back from ordinary optimization trials."><input type="number" value={reserve} onChange={e => setReserve(e.target.value)} min="0" max={compute} required /></Field>
      <Field label="Agent per-experiment limit (seconds)" hint="The most time one experiment the agents start may use."><input type="number" value={delegated} onChange={e => setDelegated(e.target.value)} min="1" max="3600" step="1" required /></Field>
      <Field label="Research autonomy"><select value={autonomy} onChange={e => setAutonomy(e.target.value)}><option value="manual">Manual · researcher chooses each experiment</option><option value="guided">Guided · propose actions and request decisions</option><option value="delegated">Delegated · allow bounded, eligible probes</option></select></Field></div>
    <div className="form-section-title"><h3>Problem instances</h3><button type="button" className="button small secondary" onClick={() => { touched.current = true; setInstances(rows => [...rows, { ...(rows.at(-1) || blankInstance(adapters[0], 0)), id: undefined, name: `Configuration ${rows.length + 1}` }]); }}><Icon name="plus" size={15} />Add configuration</button></div>
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
        : row.json ? <>
          <Field label="Problem configuration (JSON)" hint="Use the adapter's declared fields and units." wide><textarea className="code-input" value={row.json.configuration} rows={10} spellCheck={false} onChange={e => update(index, { json: { ...row.json!, configuration: e.target.value } })} /></Field>
          <Field label="Fidelity (JSON)" wide><textarea className="code-input" value={row.json.fidelity} rows={3} spellCheck={false} onChange={e => update(index, { json: { ...row.json!, fidelity: e.target.value } })} /></Field></>
        : <>
          <fieldset className="setup-fields wide"><legend>Configuration</legend><div className="form-grid">
            {Object.keys(row.configurationSchema.properties || {}).length ? <SchemaFields schema={row.configurationSchema} values={row.configuration} setValues={configuration => update(index, { configuration })} />
              : <p className="help-text">This problem declares no configuration fields.</p>}</div></fieldset>
          {Object.keys(row.fidelitySchema.properties || {}).length > 0 && <fieldset className="setup-fields wide"><legend>Evaluation fidelity</legend><div className="form-grid">
            <SchemaFields schema={row.fidelitySchema} values={row.fidelity} setValues={fidelity => update(index, { fidelity })} /></div></fieldset>}</>}
      {!row.evaluator_manifest && <div className="wide setup-instance-actions"><button type="button" className="text-button" onClick={() => toggleJson(index)}>{row.json ? 'Edit as fields' : 'Edit as JSON'}</button></div>}
    </div>{instances.length > 1 && <button type="button" className="text-button danger" onClick={() => { touched.current = true; setInstances(rows => rows.filter((_, i) => i !== index)); }}>Remove configuration</button>}</div>)}</div>
    <ErrorNotice text={error} />
    <div className="modal-actions"><button className="button secondary" type="button" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{busy ? 'Saving…' : current ? 'Save new version' : 'Create campaign'}<Icon name="arrow" size={16} /></button></div>
  </form></Modal>;
}
