import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { api, errorText, when } from './api';
import type { Json, State } from './api';
import { useCommand } from './commands';
import { startReview } from './review';
import { ParametersPanel } from './knobs';
import type { Parameter } from './knobs';
import { MaterialsPanel } from './materials';
import type { FailedUpload } from './materials';
import { TextContent } from './textContent';
import { Badge, Empty, ErrorNotice, Icon, Modal } from './ui';
import './formulation.css';

// What a code cell produced in the latest run. `stale`: the cell, or one above it, changed since.
type Output = {
  status: 'ok' | 'error' | 'skipped' | 'timeout' | 'not_run' | 'failed'; stdout: string; error?: string; after?: string; elapsed_seconds?: number; run_id: string; stale: boolean;
  outputs: ({ mime: 'text/markdown' | 'text/latex' | 'text/plain'; text: string } | { mime: 'image/png'; file: string })[];
};
type Cell = {
  id: string; name: string; kind: 'markdown' | 'code'; source: string; status: 'proposed' | 'agreed' | 'open';
  revision: number; notebook_revision: number; author: string; reason?: string; updated_at: string; agreed_revision?: number | null; output?: Output;
};
type Run = { id: string; status: string; requested_by: string; started_at: string; elapsed_seconds?: number; stderr?: string };
type Notebook = { notebook: Json | null; cells: Cell[]; parameters?: Parameter[]; outline: Json[]; active_run: Run | null; latest_run: Run | null; limits: { cells: number; source_characters: number; run_seconds: number } };
// The cell being written: an existing one (`id`) or a new one placed after `after`.
type Draft = { id?: string; after?: string; kind: Cell['kind']; name: string; source: string; revision?: number };
type Filter = '' | Cell['status'] | 'changed';

const statusTone: Record<Cell['status'], string> = { agreed: 'green', open: 'amber', proposed: '' };
const statusText: Record<Cell['status'], string> = { agreed: 'Agreed', open: 'Open', proposed: 'Proposed' };
function writer(author: string): string { return author === 'researcher' ? 'you' : author === 'lead' ? 'the lead agent' : author.replaceAll('_', ' '); }
function seenKey(campaignId: string): string { return `formulation.seen:${campaignId}`; }
function readSeen(campaignId: string): number { try { return Number(localStorage.getItem(seenKey(campaignId))) || 0; } catch { return 0; } }

/** A campaign that started from a description: until the lead agent is asked, the page offers to ask it. */
function ReviewCallout({ state, outline, onOutline, busy }: { state: State; outline: Json[]; onOutline: () => void; busy: boolean }) {
  const [sending, setSending] = useState(false), [noModel, setNoModel] = useState(false), [failure, setFailure] = useState('');
  const runtime = state.agent_runtime, unavailable = noModel || (!runtime?.configuration?.enabled && !runtime?.lead_model);
  async function ask() {
    setSending(true); setFailure('');
    try { setNoModel(await startReview(state.workspace_id, state.campaign!) === 'no_model'); }
    catch (e) { setFailure(errorText(e)); } finally { setSending(false); }
  }
  return <div className="callout formulation-review" role="note"><strong>Your description is cell <code>problem</code>.</strong> Ask the lead agent to review it: it restates the problem, writes down what your description supports, and asks about the rest as open cells.
    {unavailable && <p>No model is available for a lead agent on this server. <a href="#models">Choose one in Models</a>, then ask for the review here.</p>}
    <ErrorNotice text={failure} />
    <div className="inline-actions"><button type="button" className="button small primary" disabled={sending || unavailable} onClick={() => void ask()}>{sending ? 'Asking…' : 'Ask the lead agent to review it'}</button>
      {outline.length > 0 && <button type="button" className="button small secondary" disabled={busy} onClick={onOutline}>Add the standard outline</button>}</div></div>;
}

// The page re-renders on every workspace update; a cell is typeset again only when its text changed.
const CellBody = memo(function CellBody({ cell, showCode }: { cell: Cell; showCode: boolean }) {
  if (cell.kind === 'markdown') return <TextContent text={cell.source} math />;
  const first = cell.source.split('\n').find(line => line.trim()) || '';
  return <details className="formulation-code" open={showCode}><summary><span>code</span><code>{first}</code></summary><pre><code>{cell.source}</code></pre></details>;
}, (before, after) => before.cell.id === after.cell.id && before.cell.revision === after.cell.revision && before.showCode === after.showCode);

const outcome: Record<Output['status'], string> = { ok: 'ran', error: 'failed', skipped: 'not run', timeout: 'stopped at the time limit', not_run: 'not reached', failed: 'the run ended here' };
// An equation a cell returned: shown displayed unless it already carries its own dollar signs.
function equation(text: string): string { const body = text.trim(); return body.startsWith('$') ? body : `$$${body}$$`; }

const CellOutput = memo(function CellOutput({ cell, files }: { cell: Cell; files: string }) {
  const output = cell.output;
  if (!output) return null;
  return <section className={`formulation-output ${output.status} ${output.stale ? 'stale' : ''}`} aria-label={`Output of ${cell.name}`}>
    <p className="formulation-outcome"><span>{outcome[output.status]}{output.status === 'ok' && output.elapsed_seconds !== undefined ? ` in ${output.elapsed_seconds < 10 ? output.elapsed_seconds.toFixed(2) : Math.round(output.elapsed_seconds)} s` : ''}{output.after ? `: cell ${output.after} failed first` : ''}</span>
      {output.stale && <Badge tone="amber">out of date</Badge>}</p>
    {output.stdout && <pre className="formulation-printed"><code>{output.stdout}</code></pre>}
    {output.outputs.map((item, index) => item.mime === 'image/png'
      // The picture is a file this cell drew in the sandbox, checked to be a PNG and served from this workspace.
      ? <img key={index} src={`${files}/${encodeURIComponent(output.run_id)}/files/${encodeURIComponent(item.file)}`} alt={`Figure ${index + 1} of cell ${cell.name}`} loading="lazy" />
      : item.mime === 'text/plain' ? <pre key={index} className="formulation-value"><code>{item.text}</code></pre>
        : <TextContent key={index} text={item.mime === 'text/latex' ? equation(item.text) : item.text} math />)}
    {output.error && <pre className="formulation-error" role="alert"><code>{output.error}</code></pre>}
  </section>;
}, (before, after) => before.cell.id === after.cell.id && before.cell.output?.run_id === after.cell.output?.run_id
  && before.cell.output?.stale === after.cell.output?.stale && before.cell.output?.status === after.cell.output?.status && before.files === after.files);

function History({ campaignId, cell, onClose, onRestore }: { campaignId: string; cell: Cell; onClose: () => void; onRestore: (source: string) => void }) {
  const [revisions, setRevisions] = useState<Cell[]>([]), [selected, setSelected] = useState(0), [error, setError] = useState('');
  useEffect(() => {
    let current = true;
    api<{ revisions: Cell[] }>(`/api/campaigns/${encodeURIComponent(campaignId)}/formulation/cells/${encodeURIComponent(cell.id)}/history`)
      .then(value => { if (current) { setRevisions(value.revisions); setSelected(Math.max(1, cell.revision - 1)); } }).catch(e => { if (current) setError(errorText(e)); });
    return () => { current = false; };
  }, [campaignId, cell.id, cell.revision]);
  const shown = revisions.find(row => row.revision === selected);
  return <Modal wide title={`History of ${cell.name}`} description="Every saved revision of this cell, with who changed it and why." onClose={onClose}>
    <ErrorNotice text={error} />
    <ol className="formulation-history">{[...revisions].reverse().map(row => <li key={row.revision}>
      <button type="button" className={row.revision === selected ? 'selected' : ''} aria-pressed={row.revision === selected} onClick={() => setSelected(row.revision)}>
        <strong>Revision {row.revision}</strong><span>{statusText[row.status]} · {writer(row.author)} · {when(row.updated_at)}</span>{row.reason && <em>{row.reason}</em>}
      </button></li>)}</ol>
    {shown && <section className="formulation-history-shown" aria-label={`Revision ${shown.revision}`}>
      {shown.kind === 'markdown' ? <TextContent text={shown.source} math /> : <pre><code>{shown.source}</code></pre>}</section>}
    <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Close</button>
      <button type="button" className="button primary" disabled={!shown || shown.revision === cell.revision || shown.source === cell.source} onClick={() => shown && onRestore(shown.source)}>Restore this revision</button></div>
  </Modal>;
}

/** The campaign's formulation notebook: what the researcher and the lead agent have written down about the problem so far. */
export function FormulationView({ state, discuss, failedUploads = [], onFailedUploads = () => undefined }: { state: State; discuss: (text: string) => void;
  failedUploads?: FailedUpload[]; onFailedUploads?: (items: FailedUpload[]) => void }) {
  const campaign = state.campaign, command = useCommand(campaign);
  const [view, setView] = useState<Notebook | null>(null), [error, setError] = useState(''), [loadError, setLoadError] = useState(''), [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null), [filter, setFilter] = useState<Filter>(''), [showCode, setShowCode] = useState(false);
  const [history, setHistory] = useState<Cell | null>(null), [removing, setRemoving] = useState(''), [seen, setSeen] = useState(() => campaign ? readSeen(campaign.id) : 0);
  const path = campaign ? `/api/campaigns/${encodeURIComponent(campaign.id)}/formulation` : '';
  const revision = state.formulation?.revision ?? 0;
  const load = useCallback(async () => {
    if (!path) return;
    try { setView(await api<Notebook>(path)); setLoadError(''); } catch (e) { setLoadError(errorText(e)); }
  }, [path]);
  // The workspace state says only that the notebook changed; its cells are read here.
  const lastRun = state.formulation?.latest_run_id, activeRun = state.formulation?.active_run_id;
  useEffect(() => { void load(); }, [load, revision, lastRun, activeRun]);
  const running = !!view?.active_run;
  useEffect(() => { if (!running) return; const timer = window.setInterval(() => void load(), 2000); return () => window.clearInterval(timer); }, [running, load]);
  const cells = view?.cells || [];
  const target = useRef('');
  useEffect(() => {
    const show = () => {
      const name = location.hash.split('/')[1];
      if (!name || !cells.length || target.current === name) return;
      const element = document.getElementById(`formulation-cell-${name}`);
      if (element) { target.current = name; element.scrollIntoView({ block: 'center' }); element.focus(); }
    };
    const changed = () => { target.current = ''; show(); };
    show(); window.addEventListener('hashchange', changed);
    return () => window.removeEventListener('hashchange', changed);
  }, [cells.length]);
  if (!campaign) return <Empty icon="sigma" title="Create a campaign to formulate its problem">The notebook belongs to a campaign.</Empty>;

  async function edit(operations: Json[], reason = '') {
    setBusy(true); setError('');
    try { await command('formulation.edit', { operations, ...(reason ? { reason } : {}) }); await load(); return true; }
    catch (e) { setError(errorText(e)); await load(); return false; }
    finally { setBusy(false); }
  }
  async function run() {
    setBusy(true); setError('');
    try { await api(`${path}/runs`, {}); } catch (e) { setError(errorText(e)); }
    finally { await load(); setBusy(false); }
  }
  async function save() {
    if (!draft || !draft.source.trim()) return;
    const original = cells.find(cell => cell.id === draft.id);
    const saved = original
      ? await edit([{ op: 'update', cell: original.id, expected_revision: draft.revision, source: draft.source, ...(draft.name && draft.name !== original.name ? { name: draft.name } : {}) }])
      : await edit([{ op: 'create', kind: draft.kind, source: draft.source, ...(draft.name ? { name: draft.name } : {}), ...(draft.after ? { after: draft.after } : {}) }]);
    if (saved) setDraft(null);
  }
  function markRead() { try { localStorage.setItem(seenKey(campaign!.id), String(view?.notebook?.revision ?? 0)); } catch { /* Shown as read for this visit only. */ } setSeen(view?.notebook?.revision ?? 0); if (filter === 'changed') setFilter(''); }

  const fresh = (cell: Cell) => cell.notebook_revision > seen && cell.author !== 'researcher';
  const counts = { open: 0, proposed: 0, agreed: 0, changed: 0 };
  for (const cell of cells) { counts[cell.status] += 1; if (fresh(cell)) counts.changed += 1; }
  const shown = cells.filter(cell => !filter || (filter === 'changed' ? fresh(cell) : cell.status === filter));
  const edited = draft?.id ? cells.find(cell => cell.id === draft.id) : undefined;
  const question = (state.agent_runtime?.questions || []).find((item: Json) => item.status === 'pending');
  const tooLong = !!draft && draft.source.length > (view?.limits.source_characters ?? 32768);
  const hasCode = cells.some(cell => cell.kind === 'code'), outOfDate = cells.some(cell => cell.output?.stale), last = view?.latest_run;
  const unfinished = last && ['failed', 'interrupted'].includes(last.status);
  // Started from a description and not yet reviewed: the description is the researcher's, and the lead has had no request.
  const lead = (state.agent_runtime?.agents || []).find((agent: Json) => agent.role === 'lead');
  const reviewable = !state.tasks.length && cells.some(cell => cell.name === 'problem' && cell.author === 'researcher')
    && !state.messages.some(message => message.role === 'user' && !!lead && message.agent_id === lead.id);

  const editor = draft && <form className="formulation-editor" aria-label={draft.id ? `Edit ${edited?.name}` : 'New cell'} onSubmit={e => { e.preventDefault(); void save(); }}>
    {edited && edited.revision !== draft.revision && <div className="callout amber" role="status">
      <strong>{writer(edited.author)} changed this cell while you were writing.</strong> Saving now would be refused.
      <div className="inline-actions"><button type="button" className="button small secondary" onClick={() => setDraft({ ...draft, source: edited.source, name: edited.name, revision: edited.revision })}>Load that version</button>
        <button type="button" className="button small secondary" onClick={() => setDraft({ ...draft, revision: edited.revision })}>Keep my text and replace it</button></div></div>}
    <div className="formulation-editor-fields">
      {!draft.id && <label>Kind<select value={draft.kind} onChange={e => setDraft({ ...draft, kind: e.target.value as Cell['kind'] })}><option value="markdown">Text and equations</option><option value="code">Code</option></select></label>}
      <label>Name<input value={draft.name} pattern="[a-z][a-z0-9_]{0,47}" placeholder={draft.id ? '' : 'chosen for you if empty'} title="Lower-case letters, digits and underscores, starting with a letter" onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
    </div>
    <label className="sr-only" htmlFor="formulation-source">{draft.kind === 'markdown' ? 'Text (Markdown; $…$ and $$…$$ are equations)' : 'Python code'}</label>
    <textarea id="formulation-source" className="code-input" rows={Math.min(24, Math.max(6, draft.source.split('\n').length + 1))} value={draft.source} autoFocus spellCheck={draft.kind === 'markdown'}
      placeholder={draft.kind === 'markdown' ? 'Markdown. Write $x_i$ for an equation in a sentence and $$…$$ for one on its own line; \\$ is a dollar sign.' : 'Python'}
      onChange={e => setDraft({ ...draft, source: e.target.value })} />
    {draft.kind === 'markdown' && draft.source.trim() && <section className="formulation-preview" aria-label="Preview"><span className="eyebrow">Preview</span><TextContent text={draft.source} math /></section>}
    {tooLong && <p className="help-text" role="alert">A cell holds at most {view?.limits.source_characters.toLocaleString()} characters; split it.</p>}
    <div className="inline-actions"><button className="button small primary" disabled={busy || tooLong || !draft.source.trim()}>{draft.id ? 'Save cell' : 'Add cell'}</button>
      <button type="button" className="button small secondary" onClick={() => setDraft(null)}>Cancel</button></div>
  </form>;

  return <>
    <div className="page-heading"><div><span className="eyebrow">Formulation</span><h1>The problem, as far as it is written down.</h1>
      <p>The lead agent and you develop the formulation here: what is decided, what is given, the objective and each constraint, in words, as equations and as code. Talk it over in the conversation; what is settled stays on this page.</p></div>
      <div className="inline-actions">{view?.notebook && <Badge>Revision {view.notebook.revision}</Badge>}
        {cells.length > 0 && <><a className="button small secondary" href={`${path}/export`} download title="The notebook as one Markdown document"><Icon name="download" size={14} />Markdown</a>
          <a className="button small secondary" href={`${path}/export?format=py`} download title="The notebook as a marimo file, to open and run with marimo"><Icon name="download" size={14} />marimo notebook</a></>}</div></div>
    {question && <div className="callout amber formulation-question" role="status"><strong>The lead agent is waiting for your answer.</strong> {String(question.question).slice(0, 300)} <a href="#notebook">Answer it in the agent team panel</a></div>}
    <ErrorNotice text={error || loadError} />
    {view && <MaterialsPanel campaignId={campaign.id} failed={failedUploads} onFailed={onFailedUploads} />}
    {view && reviewable && <ReviewCallout state={state} outline={view.outline} busy={busy} onOutline={() => void edit(view.outline, 'Standard outline')} />}
    {view && (cells.length > 0 || !!view.parameters?.length) && <ParametersPanel parameters={view.parameters || []} path={path} busy={busy}
      onView={notebook => setView(notebook as Notebook)} edit={edit} />}
    {view && !cells.length && !draft && <Empty icon="sigma" title="Nothing is written down yet"
      action={<div className="inline-actions"><button className="button primary" disabled={busy} onClick={() => void edit(view.outline, 'Standard outline')}>Start from the standard outline</button>
        <button className="button secondary" onClick={() => discuss('Start the formulation notebook from the campaign objective: write down what you can already state, and what you need me to answer as open cells.')}>Ask the lead agent to start</button>
        <button className="button secondary" onClick={() => setDraft({ kind: 'markdown', name: '', source: '' })}>Write the first cell</button></div>}>
      The outline gives the notebook its sections: the problem in words, sets, data, decisions, objective, constraints, a toy instance and open questions.</Empty>}
    {cells.length > 0 && <div className="formulation-toolbar">
      <div className="segment-tabs" role="group" aria-label="Show cells">
        <button type="button" className={!filter ? 'selected' : ''} aria-pressed={!filter} onClick={() => setFilter('')}>All<span>{cells.length}</span></button>
        {(['open', 'proposed', 'agreed'] as const).map(status => <button type="button" key={status} className={filter === status ? 'selected' : ''} aria-pressed={filter === status} onClick={() => setFilter(status)}>{statusText[status]}<span>{counts[status]}</span></button>)}
        {counts.changed > 0 && <button type="button" className={filter === 'changed' ? 'selected' : ''} aria-pressed={filter === 'changed'} onClick={() => setFilter('changed')}>Changed since your last visit<span>{counts.changed}</span></button>}
      </div>
      <div className="inline-actions">{counts.changed > 0 && <button type="button" className="text-button" onClick={markRead}>Mark all as read</button>}
        <label className="checkbox-label"><input type="checkbox" checked={showCode} onChange={e => setShowCode(e.target.checked)} />Show code</label>
        {hasCode && <button type="button" className={`button small ${outOfDate || !last ? 'primary' : 'secondary'}`} disabled={busy || running} onClick={() => void run()}><Icon name="play" size={12} />{running ? 'Running…' : 'Run all'}</button>}</div>
    </div>}
    {hasCode && (running || last) && <p className="formulation-run help-text" role="status">{running ? `The code cells are running, top to bottom, for at most ${view?.limits.run_seconds} s.`
      : last && <>Last run by {writer(last.requested_by)}, {when(last.started_at)}: {last.status === 'completed' ? 'every cell ran' : last.status === 'error' ? 'a cell failed' : last.status === 'timeout' ? 'stopped at the time limit' : 'it did not finish'}{last.elapsed_seconds !== undefined ? ` (${last.elapsed_seconds.toFixed(1)} s)` : ''}.{outOfDate ? ' Some cells changed since; their outputs are out of date.' : ''}</>}</p>}
    {unfinished && last?.stderr && <pre className="formulation-error" role="alert"><code>{last.stderr}</code></pre>}
    <div className="formulation-cells">{shown.map(cell => {
      const index = cells.indexOf(cell), isEdited = draft?.id === cell.id;
      return <div key={cell.id} className="formulation-slot">
        <article id={`formulation-cell-${cell.name}`} tabIndex={-1} className={`formulation-cell ${cell.status} ${fresh(cell) ? 'fresh' : ''}`} aria-label={`Cell ${cell.name}`}>
          <header><code className="formulation-name">{cell.name}</code>
            <span className="formulation-meta">{fresh(cell) && <Badge tone="amber">new</Badge>}revision {cell.revision} · changed by {writer(cell.author)} · {when(cell.updated_at)}</span>
            <Badge tone={statusTone[cell.status]}>{statusText[cell.status]}</Badge></header>
          {cell.reason && cell.author !== 'researcher' && cell.status === 'proposed' && cell.agreed_revision && <p className="formulation-reason">Changed after you agreed to revision {cell.agreed_revision}: {cell.reason}</p>}
          {isEdited ? editor : <CellBody cell={cell} showCode={showCode} />}
          {!isEdited && cell.kind === 'code' && <CellOutput cell={cell} files={`${path}/runs`} />}
          {!isEdited && <footer className="inline-actions">
            {cell.status !== 'agreed' ? <button type="button" className="button small primary" disabled={busy} onClick={() => void edit([{ op: 'update', cell: cell.id, expected_revision: cell.revision, status: 'agreed' }])}><Icon name="check" size={13} />Agree</button>
              : <button type="button" className="button small secondary" disabled={busy} onClick={() => void edit([{ op: 'update', cell: cell.id, expected_revision: cell.revision, status: 'proposed' }])}>Withdraw agreement</button>}
            <button type="button" className="button small secondary" onClick={() => discuss(`About cell ${cell.name} (revision ${cell.revision}) of the formulation notebook: `)}><Icon name="chat" size={13} />Discuss</button>
            <button type="button" className="button small secondary" onClick={() => { setRemoving(''); setDraft({ id: cell.id, kind: cell.kind, name: cell.name, source: cell.source, revision: cell.revision }); }}>Edit</button>
            <button type="button" className="text-button" onClick={() => setHistory(cell)}>History</button>
            {cell.status !== 'open' && <button type="button" className="text-button" disabled={busy} onClick={() => void edit([{ op: 'update', cell: cell.id, expected_revision: cell.revision, status: 'open' }])}>Mark open</button>}
            <span className="formulation-spacer" />
            <button type="button" className="icon-button" aria-label={`Move ${cell.name} up`} disabled={busy || index === 0} onClick={() => void edit([{ op: 'move', cell: cell.id, before: cells[index - 1].id }])}>↑</button>
            <button type="button" className="icon-button" aria-label={`Move ${cell.name} down`} disabled={busy || index === cells.length - 1} onClick={() => void edit([{ op: 'move', cell: cell.id, after: cells[index + 1].id }])}>↓</button>
            {removing === cell.id ? <button type="button" className="button small danger-button" disabled={busy} onClick={() => { setRemoving(''); void edit([{ op: 'delete', cell: cell.id, expected_revision: cell.revision }]); }}>Delete {cell.name}</button>
              : <button type="button" className="text-button" onClick={() => setRemoving(cell.id)}>Delete…</button>}
          </footer>}
        </article>
        {draft && !draft.id && draft.after === cell.id ? <article className="formulation-cell">{editor}</article>
          : !draft && <button type="button" className="formulation-add" onClick={() => setDraft({ after: cell.id, kind: 'markdown', name: '', source: '' })}><Icon name="plus" size={12} />Add a cell here</button>}
      </div>;
    })}</div>
    {draft && !draft.id && !draft.after && <article className="formulation-cell">{editor}</article>}
    {filter && !shown.length && cells.length > 0 && <p className="help-text">No cell matches this filter.</p>}
    {history && <History campaignId={campaign.id} cell={cells.find(cell => cell.id === history.id) || history} onClose={() => setHistory(null)}
      onRestore={source => { const current = cells.find(cell => cell.id === history.id); setHistory(null); if (current) void edit([{ op: 'update', cell: current.id, expected_revision: current.revision, source }], `Restored an earlier revision of ${current.name}`); }} />}
  </>;
}
