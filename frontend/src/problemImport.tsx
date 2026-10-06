import { useCallback, useEffect, useState } from 'react';
import type { ComponentProps, FormEvent } from 'react';
import { api, ApiError, errorText } from './api';
import type { Json } from './api';
import { Badge, Empty, ErrorNotice, Field, Icon, Panel as BasePanel, Status } from './ui';
import './problemImport.css';

function Panel({ children, ...props }: ComponentProps<typeof BasePanel>) {
  return <BasePanel {...props}><div className="import-body">{children}</div></BasePanel>;
}

const DOCUMENTS = '.pdf,.docx,.md,.markdown,.txt,.tex,.rst,.html,.htm,.csv,.json,.yaml,.yml';
const ARCHIVES = '.zip,.tar,.tar.gz,.tgz,.tar.bz2,.tar.xz';
const base = '/api/v1/problem-imports';

async function upload(path: string, file: File): Promise<Json> {
  const response = await fetch(path, { method: 'PUT', body: file, headers: { 'Content-Type': 'application/octet-stream' } });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { const error = await response.json(); detail = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail); } catch { /* keep HTTP text */ }
    throw new ApiError(detail, response.status);
  }
  return response.json();
}
const money = (value: number) => `$${(value || 0).toFixed(value >= 1 ? 2 : 4)}`;
const tokens = (value: number) => value >= 1e6 ? `${(value / 1e6).toFixed(1)}M` : value >= 1e3 ? `${(value / 1e3).toFixed(1)}k` : String(value || 0);
const when = (value: string) => new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

/** Turn an import's draft into the shape the New campaign form applies. */
export function draftExample(record: Json): Json {
  const draft = record.draft;
  return { id: record.id, name: draft.title, campaign: { name: draft.title, objective: draft.objective }, instances: draft.instances };
}

export function ProblemImportView({ onUseDraft }: { onUseDraft: (example: Json) => void }) {
  const [imports, setImports] = useState<Json[]>([]), [selected, setSelected] = useState<string>(() => location.hash.split('/')[1] || '');
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    try { setImports((await api(base)).imports || []); setError(''); } catch (e) { setError(errorText(e)); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  function open(id: string) { setSelected(id); history.replaceState(null, '', `#problem-import/${id}`); }
  return <>
    <div className="page-heading"><div><span className="eyebrow">Problem importer</span><h1>Formulate a problem from papers and code.</h1>
      <p>A model reads your documents and code base, then drafts the problem for your review. Nothing runs until you create a campaign from the draft.</p></div></div>
    <ErrorNotice text={error} />
    <div className="import-layout">
      <div className="import-column">
        <NewImport onStarted={record => { void refresh(); open(record.id); }} />
        <Panel title="Imports">{imports.length ? <ul className="import-list">{imports.map(row => <li key={row.id}>
          <button type="button" className={selected === row.id ? 'active' : ''} aria-current={selected === row.id ? 'true' : undefined} onClick={() => open(row.id)}>
            <strong>{row.title}</strong><span><Status status={row.status} />{row.has_draft && <Badge tone="green">draft</Badge>}<small>{when(row.created_at)} · {row.model?.model}</small></span></button></li>)}</ul>
          : <p className="help-text">No imports yet.</p>}</Panel>
      </div>
      <div className="import-column import-main">{selected ? <ImportDetail key={selected} id={selected} onChange={refresh} onUseDraft={onUseDraft} />
        : <Empty icon="notebook" title="Choose or start an import">The draft, the importer's reading log and its cost appear here.</Empty>}</div>
    </div>
  </>;
}

function NewImport({ onStarted }: { onStarted: (record: Json) => void }) {
  const [catalog, setCatalog] = useState<Json | null>(null), [catalogError, setCatalogError] = useState('');
  const [documents, setDocuments] = useState<File[]>([]), [codeKind, setCodeKind] = useState('none');
  const [archive, setArchive] = useState<File | null>(null), [url, setUrl] = useState(''), [ref, setRef] = useState(''), [path, setPath] = useState('');
  const [notes, setNotes] = useState(''), [title, setTitle] = useState(''), [budget, setBudget] = useState('2');
  const [tier, setTier] = useState('');
  const [progress, setProgress] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  useEffect(() => { api('/api/v1/model-tiers').then(result => {
    setCatalog(result); setTier(result.roles?.problem_importer || result.tiers?.[0]?.id || '');
  }).catch(e => setCatalogError(errorText(e))); }, []);
  const chosen = catalog?.tiers.find((t: Json) => t.id === tier);
  const billing = chosen ? catalog?.providers?.[chosen.model.provider]?.billing : undefined;
  async function start(event: FormEvent) {
    event.preventDefault(); setError(''); setBusy(true);
    let record: Json | null = null;
    try {
      if (!documents.length) throw new Error('Add at least one document.');
      if (!chosen) throw new Error('Choose a model tier.');
      setProgress('Creating the import…');
      record = await api(base, { title, notes, tier, budget_usd: Number(budget) });
      for (const file of documents) { setProgress(`Uploading and reading ${file.name}…`); await upload(`${base}/${record!.id}/documents/${encodeURIComponent(file.name)}`, file); }
      if (codeKind === 'archive' && archive) { setProgress(`Unpacking ${archive.name}…`); await upload(`${base}/${record!.id}/code-archive/${encodeURIComponent(archive.name)}`, archive); }
      if (codeKind === 'git') { setProgress('Fetching the repository…'); await api(`${base}/${record!.id}/code`, { kind: 'git', url, ref }); }
      if (codeKind === 'folder') { setProgress('Copying the folder…'); await api(`${base}/${record!.id}/code`, { kind: 'folder', path }); }
      setProgress('Starting the importer…');
      const started = await api(`${base}/${record!.id}/start`, {});
      setDocuments([]); setArchive(null); setNotes(''); setTitle(''); setProgress('');
      onStarted(started);
    } catch (e) {
      setProgress(''); setError(errorText(e) + (record ? ' The import was saved; fix the source and start a new import.' : ''));
      if (record) onStarted(record);
    } finally { setBusy(false); }
  }
  return <Panel title="New import"><form className="import-form" onSubmit={start}>
    <Field label="Documents" hint="PDF, Word (.docx), Markdown, TeX or text. Text is extracted on this machine; figures and equations may be lost.">
      <input type="file" multiple accept={DOCUMENTS} onChange={e => setDocuments([...(e.target.files || [])])} /></Field>
    {documents.length > 0 && <ul className="import-files">{documents.map(file => <li key={file.name}>{file.name} <small>{(file.size / 1024 / 1024).toFixed(1)} MB</small></li>)}</ul>}
    <Field label="Code base"><select value={codeKind} onChange={e => setCodeKind(e.target.value)}><option value="none">None</option><option value="archive">Archive upload (.zip, .tar.gz)</option>
      <option value="git">Git repository</option><option value="folder">Folder on this machine</option></select></Field>
    {codeKind === 'archive' && <Field label="Code archive"><input type="file" accept={ARCHIVES} onChange={e => setArchive(e.target.files?.[0] || null)} required /></Field>}
    {codeKind === 'git' && <><Field label="Repository URL" hint="https:// or ssh. The code is fetched, never run."><input value={url} onChange={e => setUrl(e.target.value)} required placeholder="https://github.com/org/repo.git" /></Field>
      <Field label="Branch, tag or commit" hint="Leave empty for the default branch."><input value={ref} onChange={e => setRef(e.target.value)} placeholder="7838e71313d71cee8e2db3b432f41f80b9106a95" /></Field></>}
    {codeKind === 'folder' && <Field label="Folder path" hint="A project folder under your home folder. Credentials and tool folders are skipped."><input value={path} onChange={e => setPath(e.target.value)} required placeholder="~/Work/project" /></Field>}
    <Field label="Notes for the importer" hint="Optional: which condition to formulate, what to ignore, known corrections."><textarea value={notes} onChange={e => setNotes(e.target.value)} rows={3} /></Field>
    <Field label="Import name" hint="Defaults to the first document's name."><input value={title} onChange={e => setTitle(e.target.value)} maxLength={200} /></Field>
    {catalogError ? <ErrorNotice text={`Models are unavailable: ${catalogError}`} /> : <div className="import-model">
      <Field label="Model tier" hint={chosen ? `${chosen.model.provider}/${chosen.model.model}${chosen.model.effort ? ` · ${chosen.model.effort}` : ''}. Tiers are set on the Models page.` : undefined}>
        <select value={tier} onChange={e => setTier(e.target.value)}>{(catalog?.tiers || []).map((t: Json) => <option key={t.id} value={t.id}>{t.label}</option>)}</select></Field>
      <Field label="API budget (USD)" hint={billing === 'api' ? 'The importer stops when its API cost reaches this.' : 'Subscription calls are not charged against it.'}><input type="number" min="0.01" max="100" step="0.01" value={budget} onChange={e => setBudget(e.target.value)} required /></Field>
    </div>}
    <p className="help-text">The documents' text and the code are sent to the chosen model provider.</p>
    <ErrorNotice text={error} />{progress && <p className="help-text" role="status">{progress}</p>}
    <button className="button primary" disabled={busy || !catalog}>{busy ? 'Working…' : 'Start import'}<Icon name="arrow" size={16} /></button>
  </form></Panel>;
}

function ImportDetail({ id, onChange, onUseDraft }: { id: string; onChange: () => void; onUseDraft: (example: Json) => void }) {
  const [record, setRecord] = useState<Json | null>(null), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [message, setMessage] = useState(''), [busy, setBusy] = useState(false);
  const load = useCallback(async () => { try { setRecord(await api(`${base}/${id}`)); setError(''); } catch (e) { setError(errorText(e)); } }, [id]);
  useEffect(() => { void load(); }, [load]);
  const running = record?.status === 'running';
  useEffect(() => { if (!running) return; const timer = window.setInterval(() => void load(), 3000); return () => window.clearInterval(timer); }, [running, load]);
  async function act(path: string, body: Json, done?: string) {
    setBusy(true); setError(''); setNotice('');
    try { const result = await api(path, body); if (result?.id === id) setRecord(result); if (done) setNotice(done); onChange(); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  if (!record) return error ? <ErrorNotice text={error} /> : <p className="help-text">Loading the import…</p>;
  const draft = record.draft, usage = record.usage || {};
  return <div className="import-detail">
    <Panel title={record.title} eyebrow="Import" action={running && <button className="button small secondary" disabled={busy} onClick={() => void act(`${base}/${id}/stop`, {})}>Stop</button>}>
      <dl className="import-facts"><div><dt>Status</dt><dd><Status status={record.status} /></dd></div>
        <div><dt>Model</dt><dd>{record.model.model}{record.model.effort ? ` · ${record.model.effort}` : ''}</dd></div>
        <div><dt>Model calls</dt><dd>{usage.calls || 0} · {tokens((usage.input || 0) + (usage.output || 0))} tokens</dd></div>
        <div><dt>{record.billing === 'api' ? 'API cost' : 'List-price cost'}</dt><dd>{money(record.billing === 'api' ? usage.charged_usd : usage.cost_usd)} of {money(record.budget_usd)}</dd></div></dl>
      <p className="import-sources">{record.documents.map((d: Json) => d.name).join(', ')}{record.code ? ` · code: ${record.code.source}${record.code.commit ? ` @ ${record.code.commit.slice(0, 10)}` : ''} (${record.code.files} files)` : ''}</p>
      <ErrorNotice text={record.error && record.status !== 'running' ? record.error : ''} />
      {record.reply && <div className="import-reply"><strong>Importer</strong><p>{record.reply}</p></div>}
    </Panel>
    <ErrorNotice text={error} />{notice && <p className="help-text" role="status">{notice}</p>}
    {draft ? <Panel title={draft.title} eyebrow="Draft for review" action={<div className="inline-actions">
        <button className="button small primary" onClick={() => onUseDraft(draftExample(record))}>Use in a new campaign</button>
        <button className="button small secondary" disabled={busy} onClick={() => void act('/api/v1/problem-examples', { import_id: id }, 'Saved. It appears under “Start from” when you create a campaign.')}>Save as example</button></div>}>
      <p className="import-summary">{draft.summary}</p>
      <h3>Objective</h3><p className="import-objective">{draft.objective}</p>
      <h3>Problem instances</h3>{draft.instances.map((setup: Json, index: number) => <div key={index} className="import-instance">
        <strong>{setup.name}</strong> <Badge>{setup.evaluator_manifest ? `declared · ${setup.evaluator_manifest.id} · evaluator needed` : setup.problem_id}</Badge> <Badge>{setup.split}</Badge>
        {setup.rationale && <p className="help-text">{setup.rationale}</p>}
        <table className="data-table import-values"><tbody>{Object.entries({ ...setup.configuration, ...setup.fidelity }).map(([key, value]) => <tr key={key}><th>{key}</th><td>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</td></tr>)}</tbody></table>
        {setup.evaluator_manifest && <pre className="import-json">{JSON.stringify(setup.evaluator_manifest, null, 2)}</pre>}</div>)}
      {draft.evaluator_notes && <><h3>Evaluator notes</h3><p className="import-objective">{draft.evaluator_notes}</p></>}
      <div className="import-lists">
        <div><h3>Assumptions</h3>{draft.assumptions.length ? <ul>{draft.assumptions.map((item: string, i: number) => <li key={i}>{item}</li>)}</ul> : <p className="help-text">None recorded.</p>}</div>
        <div><h3>Open questions</h3>{draft.open_questions.length ? <ul>{draft.open_questions.map((item: string, i: number) => <li key={i}>{item}</li>)}</ul> : <p className="help-text">None recorded.</p>}</div></div>
      {draft.citations.length > 0 && <><h3>Where values came from</h3><ul className="import-citations">{draft.citations.map((c: Json, i: number) => <li key={i}>
        <span>{c.source}{c.location ? ` · ${c.location}` : ''}</span>{c.quote && <q>{c.quote}</q>}</li>)}</ul></>}
    </Panel> : <Panel title="Draft"><p className="help-text">{running ? 'The importer is reading the material. The draft appears here when it submits one.' : 'No draft yet.'}</p></Panel>}
    {!running && record.status !== 'collecting' && <Panel title="Ask for a revision"><form onSubmit={e => { e.preventDefault(); void act(`${base}/${id}/messages`, { message }).then(() => setMessage('')); }}>
      <Field label="Request" hint="The importer keeps its reading context and resubmits the full draft."><textarea value={message} onChange={e => setMessage(e.target.value)} rows={3} required /></Field>
      <button className="button small secondary" disabled={busy || !message.trim()}>Send</button></form></Panel>}
    <Panel title="Reading log">{record.activity?.length ? <ol className="import-activity">{[...record.activity].reverse().map((row: Json, i: number) => <li key={i} className={row.kind}>
      <time>{new Date(row.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time><span>{row.text}</span></li>)}</ol>
      : <p className="help-text">No activity yet.</p>}</Panel>
  </div>;
}
