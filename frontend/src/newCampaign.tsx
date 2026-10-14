import { useEffect, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { errorText } from './api';
import type { Json, State } from './api';
import { Badge, ErrorNotice, Field, Icon } from './ui';
import { blankInstance, declaredInstance, exampleLimits, instanceFor, limitsOf, limitsPayload, LimitsFields, ProblemInstancesEditor,
  tasksPayload, useProblemCatalog, withSchemas } from './campaignSetup';
import type { Instance, Limits } from './campaignSetup';
import { sendCommand } from './commands';
import { DOCUMENTS } from './problemImport';
import { DOCUMENT_NOTICE, uploadMaterials } from './materials';
import type { FailedUpload } from './materials';
import './campaignSetup.css';

export type ProblemPath = 'formulate' | 'installed' | 'declare';
type Draft = { name: string; description: string; path: ProblemPath; llm: string };
const OBJECTIVE = 'Develop an effective optimizer for the selected problem under the declared resource budget.';
const PLACEHOLDER = 'What is decided, by whom, and how often? What limits it? What would a good outcome be?\n\n'
  + 'Write it as you would explain it to a colleague. It does not need to be complete: the open points become questions in the formulation.';

const draftKey = (workspace: string) => `optimization.newCampaign.v1:${encodeURIComponent(workspace)}`;
function readDraft(workspace: string): Partial<Draft> {
  try { return JSON.parse(localStorage.getItem(draftKey(workspace)) || '{}'); } catch { return {}; }
}
function writeDraft(workspace: string, draft: Draft) {
  try { localStorage.setItem(draftKey(workspace), JSON.stringify(draft)); } catch { /* The draft is a convenience. */ }
}
function clearDraft(workspace: string) {
  try { localStorage.removeItem(draftKey(workspace)); } catch { /* Nothing to clear. */ }
}

function PathCard({ value, path, setPath, title, children }: { value: ProblemPath; path: ProblemPath; setPath: (path: ProblemPath) => void; title: string; children: ReactNode }) {
  return <label className={`setup-card path-card ${path === value ? 'selected' : ''}`}>
    <span className="path-card-title"><input type="radio" name="problem-path" value={value} checked={path === value} onChange={() => setPath(value)} /><strong>{title}</strong></span>
    <span>{children}</span></label>;
}

/** Start a campaign from the problem as the researcher sees it: in words, from an installed problem, or as a declaration. */
export function NewCampaignPage({ state, seed, onSeedUsed, onCreated, onCancel, onImport }: { state: State; seed: Json | null; onSeedUsed: () => void;
  onCreated: (campaign: Json, review: boolean, failed: FailedUpload[]) => void; onCancel: () => void; onImport: (notes: string) => void }) {
  const workspace = state.workspace_id || '';
  const [saved] = useState(() => readDraft(workspace));
  const catalog = useProblemCatalog();
  const [name, setName] = useState(saved.name || '');
  const [description, setDescription] = useState(saved.description || '');
  const [path, setPathValue] = useState<ProblemPath>(saved.path || 'formulate');
  const [limits, setLimits] = useState<Limits>(() => ({ ...limitsOf(null), ...(saved.llm ? { llm: saved.llm } : {}) }));
  const [objective, setObjective] = useState(OBJECTIVE);
  const [instances, setInstances] = useState<Instance[]>(() => saved.path === 'declare' ? [declaredInstance(0)] : [blankInstance(undefined, 0)]);
  const [example, setExample] = useState('');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [progress, setProgress] = useState('');
  // Documents are kept in memory only: a file cannot be saved with the draft.
  const [files, setFiles] = useState<File[]>([]);

  useEffect(() => { if (workspace) writeDraft(workspace, { name, description, path, llm: limits.llm }); }, [workspace, name, description, path, limits.llm]);
  useEffect(() => { if (catalog.loaded) setInstances(rows => withSchemas(rows, catalog.adapters)); }, [catalog.loaded, catalog.adapters]);
  // A draft from the importer, or an example chosen elsewhere, arrives once as the page's starting point.
  useEffect(() => {
    if (!seed || !catalog.loaded) return;
    apply(seed);
    if (!description.trim() && seed.summary) setDescription(seed.summary);
    onSeedUsed();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed, catalog.loaded]);

  function setPath(next: ProblemPath) {
    setPathValue(next); setError('');
    if (next === 'installed' && (!instances.length || instances.some(row => row.evaluator_manifest))) setInstances([blankInstance(undefined, 0)]);
    if (next === 'declare' && !instances.some(row => row.evaluator_manifest)) setInstances([declaredInstance(0)]);
  }
  /** Use an example's problem instances and suggested limits. The name is filled only when none is typed. */
  function apply(chosen: Json) {
    const setups: Json[] = chosen.instances || [];
    setExample(chosen.id || 'seed');
    setPathValue(setups.length && setups.every(setup => setup.evaluator_manifest) ? 'declare' : 'installed');
    setInstances(setups.map(setup => instanceFor(setup, catalog.adapters)));
    const defaults = chosen.campaign || {};
    setLimits(current => exampleLimits(current, defaults));
    if (defaults.objective) setObjective(defaults.objective);
    if (defaults.name) setName(current => current.trim() ? current : defaults.name);
  }
  async function create(event: FormEvent) {
    event.preventDefault(); setError('');
    const formulate = path === 'formulate';
    if (formulate && !description.trim()) { setError('Describe the problem first: the lead agent starts from your description.'); return; }
    if (formulate && !(Number(limits.llm) > 0)) { setError('Give the campaign an API spending cap above 0: with a cap of 0 the lead agent cannot take a turn.'); return; }
    setBusy(true);
    try {
      const payload = { name, description, ...limitsPayload(limits), ...(formulate ? {} : { objective, tasks: tasksPayload(instances, { chosen: true }) }) };
      setProgress('Creating the campaign…');
      const outcome = await sendCommand(workspace, null, 'campaign.create', payload);
      clearDraft(workspace);
      // The campaign exists before its documents: they are attached to it, and a refused one is reported, not lost.
      const failed = files.length ? await uploadMaterials(outcome.campaign.id, files, setProgress) : [];
      onCreated(outcome.campaign, formulate, failed);
    } catch (e) { setError(errorText(e)); setBusy(false); setProgress(''); }
  }
  const adapterName = (id: string) => catalog.adapters.find(a => a.id === id)?.name || id;
  return <div className="new-campaign">
    <div className="page-heading"><div><span className="eyebrow">New campaign</span><h1>Start from the problem as you see it.</h1>
      <p>Describe it in your own words. If you cannot say yet how it should be modelled, the lead agent works that out with you in the Formulation notebook.</p></div></div>
    <form onSubmit={create} aria-label="New campaign">
      <section className="panel new-campaign-section"><div className="form-grid">
        <Field label="Campaign name" wide><input value={name} onChange={e => setName(e.target.value)} required maxLength={180} placeholder="Token sharing" /></Field>
        <Field label="Describe the problem" wide hint="Kept in this browser until you create the campaign. It becomes the first cell of the formulation notebook, as you wrote it.">
          <textarea className="problem-description" value={description} onChange={e => setDescription(e.target.value)} rows={10} maxLength={20000} placeholder={PLACEHOLDER} /></Field>
        <Field label="Documents (optional)" wide hint={`Papers, notes, data descriptions: the lead agent reads them with your description. ${DOCUMENT_NOTICE}`}>
          <input type="file" multiple accept={DOCUMENTS} onChange={e => { const chosen = [...(e.target.files || [])]; e.target.value = ''; setFiles(current => [...current, ...chosen.filter(file => !current.some(other => other.name === file.name))]); }} /></Field>
        {files.length > 0 && <ul className="new-campaign-files wide" aria-label="Documents to attach">{files.map(file => <li key={file.name}><span>{file.name}</span>
          <button type="button" className="text-button danger" aria-label={`Do not attach ${file.name}`} onClick={() => setFiles(current => current.filter(other => other !== file))}>Remove</button></li>)}</ul>}
      </div></section>
      <section className="panel new-campaign-section" aria-labelledby="problem-path-title"><h2 id="problem-path-title">How is the problem defined?</h2>
        <div className="setup-cards path-cards" role="radiogroup" aria-label="How the problem is defined">
          <PathCard value="formulate" path={path} setPath={setPath} title="Not defined yet">Work it out with the lead agent. It reviews your description, writes what it supports, and asks about the rest. You attach a problem when the formulation is ready.</PathCard>
          <PathCard value="installed" path={path} setPath={setPath} title="An installed problem">A problem this server can already evaluate, configured here.</PathCard>
          <PathCard value="declare" path={path} setPath={setPath} title="Declare it myself">Say what a candidate is and how it is scored. An evaluator is built after the campaign is created.</PathCard>
        </div>
        <p className="help-text">Have a paper or code that defines a runnable setup? <button type="button" className="text-button" onClick={() => onImport(description)}>Draft one from documents<Icon name="arrow" size={14} /></button></p>
        {path === 'installed' && catalog.examples.length > 0 && <div className="setup-start" aria-label="Examples"><h3>Start from an example</h3><div className="setup-cards">
          {catalog.examples.map(item => <button type="button" key={item.id} className={`setup-card ${example === item.id ? 'selected' : ''}`} aria-pressed={example === item.id} onClick={() => apply(item)}>
            <strong>{item.name}</strong><span>{item.summary}</span>
            <small>{[...new Set((item.instances || []).map((s: Json) => s.evaluator_manifest ? 'Declared problem' : adapterName(s.problem_id)))].join(' · ')}{item.source === 'saved' && <Badge>Saved</Badge>}</small></button>)}
        </div></div>}
        {path !== 'formulate' && <>
          <ProblemInstancesEditor instances={instances} onChange={rows => { setExample(''); setInstances(rows); }} adapters={catalog.adapters} onError={setError} />
          <div className="form-grid"><Field label="Research objective" wide hint="What the campaign's research should achieve on this problem.">
            <textarea value={objective} onChange={e => setObjective(e.target.value)} rows={3} required /></Field></div></>}
      </section>
      <section className="panel new-campaign-section"><h2>Limits</h2>
        <div className="form-grid"><LimitsFields limits={limits} onChange={setLimits} only="llm" /></div>
        <details className="new-campaign-limits" open={path !== 'formulate'}><summary>Compute, autonomy and other limits</summary>
          <div className="form-grid"><LimitsFields limits={limits} onChange={setLimits} only="others" /></div></details>
      </section>
      <ErrorNotice text={error || catalog.error} />
      {progress && <p className="help-text" role="status">{progress}</p>}
      <div className="modal-actions"><button className="button secondary" type="button" onClick={onCancel}>Cancel</button>
        <button className="button primary" disabled={busy || !workspace}>{busy ? 'Creating…' : path === 'formulate' ? 'Create and review' : 'Create campaign'}<Icon name="arrow" size={16} /></button></div>
    </form></div>;
}
