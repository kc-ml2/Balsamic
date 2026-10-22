/** The problem note: sections with math, annotations by tier (Define, Explain, Challenge, Visualize), revisions, Adopt, the charter it becomes. */
import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { PageProps } from '../shell/StagePanel';
import { useRead, useSummary } from '../api/queries';
import { api } from '../api/client';
import { send } from '../commands/bus';
import { CharterPanel } from './CharterPanel';
import { Markdown } from '../chat/render/Markdown';
import { VisualFrame } from '../chat/render/VisualFrame';
import { Empty, Failed, Kv, Loading, Pill, Section, when } from '../primitives/ui';
import { formatHash } from '../router/routes';
import { track } from '../telemetry/events';
import { useSelection } from '../stores/selection';
import { SKIP_REQUEST } from '../chat/slash';
import { SkipReason, SkipRow, type Skips } from './Skips';

interface SectionData { text: string; items: string[]; latex: string[] }
interface Revision { id: string; revision: number; sections: Record<string, SectionData>; glossary: { term: string; definition: string }[]; title: string; author: string; reason: string; created_at: string }
interface Annotation { id: string; anchor: { revision: number; section: string; item_index?: number | null; start: number; end: number; quote: string }; kind: string; question: string; tier: string;
  escalated_from_tier?: string | null; escalation_reason?: string; answer: { text: string; answered_by: string; cost_usd?: number | null } | null; proposed_edit: Record<string, unknown> | null; visual_id?: string | null; status: string; created_at: string }
interface NoteView { note: { id: string; title: string; current_revision: number; adopted_revision: number | null; charter_id: string | null; import_id: string | null; status: string }; revision: Revision | null;
  revisions: { id: string; revision: number; author: string; reason: string; created_at: string }[]; annotations: Annotation[]; charter: { import_id?: string; status?: string; error?: string | null; charter_id?: string | null; reply?: string | null; model?: { provider?: string | null; model?: string | null; effort?: string | null } | null; tier?: string | null; usage?: { calls?: number; cost_usd?: number } | null; charter?: Record<string, unknown> } | null; sections: string[]; titles: Record<string, string>; skips: Skips }

const KINDS = [['define', 'Define'], ['explain', 'Explain'], ['challenge', 'Challenge'], ['visualize', 'Visualize']] as const;

export default function ProblemNotePage({ campaignId, route, threadId }: PageProps) {
  const view = useRead<NoteView>(['note', campaignId], `/api/v1/campaigns/${campaignId}/problem-note`, { refetchInterval: 8000 });
  const summary = useSummary(campaignId);
  const queryClient = useQueryClient();
  const selection = useSelection();
  const [menu, setMenu] = useState<{ section: string; quote: string; start: number; end: number } | null>(null);
  const [question, setQuestion] = useState('');
  const [kind, setKind] = useState<typeof KINDS[number][0]>('explain');
  const [error, setError] = useState<string | null>(null);
  const [shownRevision, setShownRevision] = useState<number | null>(null);
  const [editing, setEditing] = useState<string | null>(null);   // the section whose editor is open
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [asked, setAsked] = useState(false);   // the lead was asked which sections can be skipped
  const toggle = (name: string, open?: boolean) => setFolded(prev => { const next = new Set(prev); if (open ?? next.has(name)) next.delete(name); else next.add(name); return next; });
  const other = useRead<NoteView>(['note', campaignId, shownRevision], shownRevision ? `/api/v1/problem-notes/note_${campaignId}?revision=${shownRevision}` : null);
  useEffect(() => { setShownRevision(null); }, [campaignId]);
  if (view.isPending) return <Loading what="Loading the problem note" />;
  if (view.error) return <Failed error={view.error} />;
  const data = view.data;
  const revision = (shownRevision && other.data?.revision) || data.revision;
  const sub = route.sub;
  const version = summary.data?.campaign.version;
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['note', campaignId] });

  const capture = (section: string, event: React.MouseEvent) => {
    if ((event.target as HTMLElement).closest('[data-testid="annotation-menu"], [data-testid="annotation"], button, input')) return;
    const sel = window.getSelection();
    const text = sel?.toString().trim() || '';
    if (!text) return;
    setMenu({ section, quote: text.slice(0, 500), start: 0, end: text.length });
  };
  // "Ask about this" needs no selection: the quote is the section's opening words, so the menu is one click away.
  const askAbout = (section: string, data: SectionData | undefined) => {
    const quote = (data?.text?.trim() || data?.items?.[0] || data?.latex?.[0] || '').slice(0, 160);
    setMenu({ section, quote: quote || '(unwritten)', start: 0, end: quote.length });
  };
  const revise = async (section: string, value: SectionData) => {
    if (version === undefined) return;
    setError(null);
    try {
      await send({ operation: 'problem_note.revise', campaignId, expectedRevision: version, payload: { note_id: data.note.id, sections: { [section]: value }, reason: `Edited ${data.titles[section] || section} on the Problem note page` }, origin: { thread_id: threadId } });
      setEditing(null); await refresh();
    } catch (e) { setError(String((e as Error).message || e)); }
  };
  const ask = async () => {
    if (!menu || !revision || !question.trim() || version === undefined) return;
    setError(null);
    const anchor = { revision: revision.revision, section: menu.section, start: menu.start, end: menu.end, quote: menu.quote };
    try {
      if (kind === 'define' || kind === 'explain') {
        const answered = await api<Annotation>(`/api/v1/problem-notes/${data.note.id}/ask`, { method: 'POST', body: JSON.stringify({ note_id: data.note.id, anchor, question: question.trim(), kind }) });
        track('note.ask', { tier: answered.tier, escalated: !!answered.escalated_from_tier });
      } else {
        const receipt = await send({ operation: 'note.ask', campaignId, expectedRevision: version, payload: { note_id: data.note.id, anchor, question: question.trim(), kind }, origin: { thread_id: threadId } });
        track('note.ask', { tier: String(receipt.outcome.tier), escalated: false });
      }
      setMenu(null); setQuestion(''); await refresh();
    } catch (e) { setError(String((e as Error).message || e)); }
  };
  const resolve = async (annotation: Annotation, choice: 'accept' | 'reject' | 'resolve') => {
    if (version === undefined) return;
    setError(null);
    try { await send({ operation: 'note.resolve', campaignId, expectedRevision: version, payload: { annotation_id: annotation.id, choice } }); await refresh(); }
    catch (e) { setError(String((e as Error).message || e)); }
  };
  const adopt = async () => {
    if (version === undefined || !data.revision) return;
    setError(null);
    try { await send({ operation: 'problem_note.adopt', campaignId, expectedRevision: version, payload: { note_id: data.note.id, revision: data.revision.revision }, origin: { thread_id: threadId } }); await refresh(); }
    catch (e) { setError(String((e as Error).message || e)); }
  };
  // The lead makes the case for each skip in the conversation; nothing is skipped until you agree.
  const askSkips = async () => {
    if (version === undefined || !threadId) return;
    setError(null);
    try { await send({ operation: 'agent.message', campaignId, expectedRevision: version, payload: { message: SKIP_REQUEST, thread_id: threadId }, origin: { thread_id: threadId } }); setAsked(true); track('note.ask_skips', {}); }
    catch (e) { setError(String((e as Error).message || e)); }
  };
  const unskip = async (section: string) => {
    if (version === undefined) return;
    setError(null);
    try { await send({ operation: 'problem_note.unskip', campaignId, expectedRevision: version, payload: { note_id: data.note.id, section } }); await refresh(); }
    catch (e) { setError(String((e as Error).message || e)); }
  };
  const skipped = (name: string) => { const s = data.skips?.skipped[name]; return !!s && !s.lapsed; };
  const proposed = (name: string) => { for (const p of data.skips?.proposals || []) { const item = p.items.find(i => i.section === name && i.status === 'proposed'); if (item) return { proposal: p, item }; } return null; };
  const tabs = [['', 'Note'], ['charter', 'Charter'], ['revisions', 'Revisions']] as const;
  const open = data.annotations.filter(a => a.status !== 'rejected');
  return (
    <div className="col">
      <div className="stage-head"><h1>{data.note.title}</h1><Pill tone={data.note.status === 'confirmed' ? 'clear' : data.note.status === 'adopted' ? 'agent' : ''}>{data.note.status}</Pill>
        <span className="muted small">revision {revision?.revision ?? 0}{shownRevision ? ' (viewing an older one)' : ''}</span>
        {data.note.status === 'drafting' && data.revision ? <button className="btn primary small" onClick={adopt} data-testid="adopt" title="Hand the note to the formulator, which writes the charter you then confirm">Adopt</button> : null}</div>
      <nav className="tabs">{tabs.map(([key, label]) => <a key={key} href={formatHash({ page: 'note', sub: key || null }, route.thread)} className={(sub || '') === key ? 'active' : ''}>{label}</a>)}</nav>
      {error ? <div className="pill bad">{error}</div> : null}
      {!sub ? <>
        {revision && data.note.status !== 'confirmed' && !shownRevision ? <div className="callout" role="note" data-testid="note-guide"><span className="callout-mark" aria-hidden="true">→</span><div className="grow small">
          <strong>{data.note.status === 'adopted' ? 'Adopted. It can still change: a change reopens the draft, to adopt again.' : 'A draft by the lead, not yet adopted. Make it say what you mean before you adopt it.'}</strong>
          <div>Each section has <b>Ask about this</b> (Define, Explain, Challenge or Visualize a passage; select any words to ask about those exactly) and <b>Edit</b> (change the text yourself; saved as a revision in your name). Or tell the lead in the chat what to change, and it revises the note. Adopt when it is right: the formulator writes the charter from it, and you confirm the charter.</div>
        </div></div> : null}
        {!revision ? <Empty>No note yet. Describe the problem to the lead in the conversation, or type /note.</Empty> : <>
        {(() => {
          const written = (name: string) => { const s = revision.sections[name]; return !!(s?.text?.trim() || s?.items?.length || s?.latex?.length); };
          const missing = data.sections.filter(name => !written(name) && !skipped(name));
          const settled = (name: string) => written(name) || skipped(name);
          const state = (name: string) => written(name) ? 'written' : skipped(name) ? 'skipped' : proposed(name) ? 'proposed' : 'missing';
          const waiting = missing.filter(name => proposed(name)).length;
          return <nav className="outline" data-testid="outline" aria-label="Sections">
            {data.sections.map(name => <a key={name} href={`#section-${name}`} className={settled(name) ? 'done' : 'missing'} data-testid="outline-item" data-state={state(name)}
              title={{ written: 'Written', skipped: 'Left unwritten with your agreement', proposed: 'The lead proposes leaving it unwritten; agree or decline in the section', missing: 'Not written yet: the problem is not defined until it is written or skipped with your agreement' }[state(name)]} onClick={e => { e.preventDefault(); toggle(name, true); document.getElementById(`section-${name}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }}>
              {data.titles[name]}{open.some(a => a.anchor.section === name) ? <span className="faint">· {open.filter(a => a.anchor.section === name).length}</span> : null}</a>)}
            <span className="small faint grow">{missing.length ? `${missing.length} of ${data.sections.length} still to write` + (waiting ? `, ${waiting} with a skip proposed` : '') : Object.keys(data.skips?.skipped || {}).some(skipped) ? 'every section is written or skipped' : 'every section is written'}</span>
            {missing.length > waiting && data.note.status !== 'confirmed' && !shownRevision ? <button className="btn small" onClick={askSkips} disabled={asked || !threadId} data-testid="ask-skips"
              title="The lead says which empty sections the confirmed charter already states or that do not apply here, with its reasons, and asks you about the rest; you agree to each skip">{asked ? 'Asked the lead (see the chat)' : 'Ask which can be skipped'}</button> : null}
            <button className="btn quiet small" onClick={() => setFolded(new Set(data.sections.filter(written)))} title="Fold the written sections to see what is left">Fold written</button>
            <button className="btn quiet small" onClick={() => setFolded(new Set())}>Expand all</button>
          </nav>;
        })()}
        {data.sections.map(name => {
          const section = revision.sections[name];
          const anns = open.filter(a => a.anchor.section === name);
          const empty = !(section?.text?.trim() || section?.items?.length || section?.latex?.length);
          const skip = empty ? data.skips?.skipped[name] : undefined;
          const offer = empty && (!skip || skip.lapsed) ? proposed(name) : null;
          const editable = data.note.status !== 'confirmed' && !shownRevision;
          const isFolded = folded.has(name) && editing !== name;
          return (
            <section key={name} id={`section-${name}`} className={'card' + (empty ? ' unwritten' : '') + (isFolded ? ' folded' : '')} data-testid={`section-${name}`} data-empty={empty ? '1' : undefined} data-folded={isFolded ? '1' : undefined} onMouseUp={e => capture(name, e)}>
              <div className="section-head"><button className="fold-toggle" onClick={() => toggle(name)} aria-label={isFolded ? 'Expand' : 'Fold'} data-testid="section-fold">{isFolded ? '▸' : '▾'}</button><h2>{data.titles[name]}</h2>
                {skip && !skip.lapsed ? <Pill tone="clear" title="Left unwritten with your agreement; the gate counts it as settled">skipped</Pill>
                  : offer ? <Pill tone="agent" title="The lead proposes leaving this section unwritten">skip proposed</Pill>
                  : skip?.lapsed ? <Pill tone="warn" title={'The skip no longer holds: ' + skip.lapsed}>skip lapsed</Pill>
                  : empty ? <Pill tone="warn" title="The gate needs every section written, or skipped with your agreement, before proposals, allocations or autopilot">not written</Pill> : null}{anns.length ? <Pill>{anns.length} notes</Pill> : null}
                {editable && editing !== name ? <><button className="btn small" data-testid="section-ask" title="Ask about this section: define a term, have it explained, challenge it, or ask the lead to draw it. Select words in the text to ask about those exactly." onClick={() => askAbout(name, section)}>Ask about this</button>
                  <button className="btn small" data-testid="section-edit" title="Change this section yourself; the change is saved as a revision in your name" onClick={() => { setMenu(null); toggle(name, true); setEditing(name); }}>Edit</button></> : null}</div>
              {isFolded ? null : editing === name ? <SectionEditor section={section} onSave={value => revise(name, value)} onCancel={() => setEditing(null)} /> : <>
              {section?.text ? <Markdown text={section.text} thread={route.thread} />
                : offer ? <SkipRow proposal={offer.proposal} item={offer.item} campaignId={campaignId} />
                : skip ? <div className="skip-row" data-testid="skipped" data-lapsed={skip.lapsed ? '1' : undefined}>
                    {skip.lapsed ? <p className="small">This skip no longer holds: {skip.lapsed}. Write the section, or ask the lead again.</p> : <p className="small faint">Left unwritten with your agreement.</p>}
                    <SkipReason item={skip} />
                    {!skip.lapsed && editable ? <div className="row"><button className="btn small" onClick={() => unskip(name)} data-testid="unskip" title="Take the skip back: the section is to be written before the gate opens">Write it instead</button></div> : null}</div>
                : empty ? <p className="faint small">Not written yet. Ask the lead to write it, or write it with Edit. If the charter already says it or it does not apply here, ask which sections can be skipped.</p> : null}
              {section?.items?.length ? <ul>{section.items.map((item, i) => <li key={i}><Markdown text={item} thread={route.thread} className="md inline" /></li>)}</ul> : null}
              {section?.latex?.map((formula, i) => <Markdown key={i} text={'$$' + formula + '$$'} thread={route.thread} />)}</>}
              {menu?.section === name ? <div className="card" data-testid="annotation-menu"><div className="row wrap"><span className="small faint">“{menu.quote.slice(0, 80)}”</span>
                {KINDS.map(([k, label]) => <button key={k} className={'btn small' + (kind === k ? ' primary' : '')} onClick={() => setKind(k)}>{label}</button>)}</div>
                <div className="row" style={{ marginTop: 6 }}><input className="grow" placeholder={kind === 'define' ? 'What is …?' : kind === 'visualize' ? 'What should the lead draw?' : 'Your question'} value={question} onChange={e => setQuestion(e.target.value)} data-testid="annotation-question" />
                  <button className="btn primary small" onClick={ask} disabled={!question.trim()}>Ask</button><button className="btn quiet small" onClick={() => setMenu(null)}>cancel</button></div>
                <p className="why">Define and Explain answer here from the glossary or one cheap model call; Challenge and Visualize take a turn of the lead and come back as proposed edits.</p></div> : null}
              {isFolded ? null : anns.map(a => <AnnotationCard key={a.id} annotation={a} campaignId={campaignId} selected={selection.id === a.id} thread={route.thread} onResolve={resolve} />)}
            </section>);
        })}</>}
        {revision?.glossary?.length ? <Section title="Glossary"><Kv rows={revision.glossary.map(g => [g.term, g.definition])} /></Section> : null}
      </> : null}
      {sub === 'charter' ? <CharterTab data={data} campaignId={campaignId} version={version} onChanged={refresh} /> : null}
      {sub === 'revisions' ? <Section title="Revisions">{data.revisions.slice().reverse().map(r => <div key={r.id} className="row small" style={{ padding: '3px 0' }}>
        <button className={'btn small' + ((shownRevision || data.note.current_revision) === r.revision ? ' primary' : '')} onClick={() => setShownRevision(r.revision === data.note.current_revision ? null : r.revision)}>r{r.revision}</button>
        <Pill tone={r.author === 'lead' ? 'agent' : 'you'}>{r.author}</Pill><span className="grow">{r.reason || 'no reason recorded'}</span><span className="faint">{when(r.created_at)}</span></div>)}</Section> : null}
    </div>
  );
}

function SectionEditor({ section, onSave, onCancel }: { section: SectionData | undefined; onSave: (value: SectionData) => Promise<void>; onCancel: () => void }) {
  const [text, setText] = useState(section?.text || '');
  const [items, setItems] = useState((section?.items || []).join('\n'));
  const [latex, setLatex] = useState((section?.latex || []).join('\n'));
  const [busy, setBusy] = useState(false);
  const lines = (value: string) => value.split('\n').map(line => line.trim()).filter(Boolean);
  const rows = (value: string, least: number) => Math.min(40, Math.max(least, value.split('\n').reduce((n, line) => n + 1 + Math.floor(line.length / 90), 0) + 1));   // the whole text in view, growing as it is typed
  const save = async () => { setBusy(true); try { await onSave({ text: text.trim(), items: lines(items), latex: lines(latex) }); } finally { setBusy(false); } };
  return <div className="col editor" data-testid="section-editor" style={{ gap: 8, marginTop: 8 }}>
    <label className="field"><span className="field-label">Text, in markdown with $math$</span><textarea rows={rows(text, 6)} placeholder="What this section says" value={text} onChange={e => setText(e.target.value)} data-testid="section-editor-text" /></label>
    <label className="field"><span className="field-label">Items, one per line (optional)</span><textarea rows={rows(items, 3)} placeholder="one item per line" value={items} onChange={e => setItems(e.target.value)} data-testid="section-editor-items" /></label>
    <label className="field"><span className="field-label">Formulas in LaTeX, one per line (optional)</span><textarea rows={rows(latex, 2)} placeholder="\\eta = P_1 / P_0" value={latex} onChange={e => setLatex(e.target.value)} data-testid="section-editor-latex" /></label>
    <div className="row"><button className="btn primary small" disabled={busy} onClick={save} data-testid="section-editor-save">Save as a revision</button><button className="btn quiet small" disabled={busy} onClick={onCancel}>cancel</button>
      <span className="small faint">Saved in your name; the lead sees it on its next turn.</span></div>
  </div>;
}

/** A proposed edit as the note would read, section by section; the raw form folded away for whoever wants it. */
function ProposedEdit({ edit, thread }: { edit: Record<string, unknown>; thread: string | null }) {
  const sections = (edit.sections && typeof edit.sections === 'object' ? edit.sections : {}) as Record<string, { text?: string; items?: string[]; latex?: string[] }>;
  const title = typeof edit.title === 'string' ? edit.title : null;
  const names = Object.keys(sections);
  return <div className="col" style={{ gap: 6 }}>
    {title ? <div className="small"><span className="faint">Title: </span>{title}</div> : null}
    {names.map(name => { const part = sections[name] || {}; return <div key={name} className="small" data-testid="proposed-section">
      <strong style={{ textTransform: 'capitalize' }}>{name.replace(/_/g, ' ')}</strong>
      {part.text ? <Markdown text={part.text} thread={thread} /> : null}
      {part.items?.length ? <ul style={{ margin: '2px 0' }}>{part.items.map((item, i) => <li key={i}><Markdown text={String(item)} thread={thread} className="md inline" /></li>)}</ul> : null}
      {part.latex?.map((formula, i) => <Markdown key={i} text={'$$' + formula + '$$'} thread={thread} />)}
    </div>; })}
    {!names.length && !title ? <span className="small faint">No section change.</span> : null}
    <details className="fold" style={{ borderBottom: 'none' }}><summary><span className="fold-title small">As data</span></summary><pre className="small">{JSON.stringify(edit, null, 1).slice(0, 2000)}</pre></details>
  </div>;
}

function AnnotationCard({ annotation: a, campaignId, selected, thread, onResolve }: { annotation: Annotation; campaignId: string; selected: boolean; thread: string | null; onResolve: (a: Annotation, c: 'accept' | 'reject' | 'resolve') => void }) {
  const visual = useRead<{ data: { content: { html: string }; title: string } } | Record<string, unknown>>(['artifact', a.visual_id], a.visual_id ? `/api/v1/campaigns/${campaignId}/agents/records/${a.visual_id}` : null);
  const html = (visual.data as { data?: { content?: { html?: string } } } | undefined)?.data?.content?.html;
  return (
    <article className={'card' + (selected ? ' selected' : '')} data-testid="annotation" data-record-id={a.id}>
      <div className="row"><Pill tone="you">{a.kind}</Pill><Pill title={a.escalated_from_tier ? `escalated from ${a.escalated_from_tier}: ${a.escalation_reason || ''}` : 'tier'}>{a.tier}{a.escalated_from_tier ? ` ← ${a.escalated_from_tier}` : ''}</Pill>
        <span className="grow small">“{a.anchor.quote}” — {a.question}</span><Pill>{a.status}</Pill></div>
      {a.answer ? <div className="small" style={{ marginTop: 4 }}><Markdown text={a.answer.text} thread={thread} /> <span className="faint">by {a.answer.answered_by}{a.answer.cost_usd ? `, $${a.answer.cost_usd.toFixed(3)}` : ''}</span></div>
        : a.status === 'open' ? <p className="small faint">{a.tier === 'lead' || a.tier === 'visual' ? 'Waiting for the lead\'s turn.' : 'Not answered.'}</p> : null}
      {html ? <VisualFrame html={html} title={a.question} thread={thread} /> : null}
      {a.proposed_edit ? <div className="card" data-testid="proposed-edit"><h4>Proposed edit</h4><ProposedEdit edit={a.proposed_edit} thread={thread} />
        {a.status === 'answered' ? <div className="row"><button className="btn primary small" onClick={() => onResolve(a, 'accept')}>Accept into the note</button><button className="btn small" onClick={() => onResolve(a, 'reject')}>Reject</button></div> : null}</div> : null}
      {a.status === 'answered' && !a.proposed_edit ? <button className="btn quiet small" onClick={() => onResolve(a, 'resolve')}>Done</button> : null}
    </article>
  );
}

function CharterTab({ data, campaignId, version, onChanged }: { data: NoteView; campaignId: string; version?: number; onChanged: () => unknown }) {
  const charterId = data.charter?.charter_id || data.note.charter_id;
  if (data.note.status === 'drafting' && !charterId) return <Empty>Adopt the note first: the formulator writes the charter from it, and you confirm the charter here.</Empty>;
  if (!charterId) return <div className="card"><Pill tone="agent">formulator {data.charter?.status || 'starting'}</Pill> {data.charter?.error ? <span className="pill bad">{data.charter.error}</span> : <span className="small muted">The formulator is writing the charter from the adopted note.</span>}</div>;
  return <CharterPanel charterId={charterId} campaignId={campaignId} version={version} onChanged={onChanged} formulator={data.charter?.status}
    formulatorModel={data.charter?.model?.model ? { name: `${data.charter.model.provider}/${data.charter.model.model}${data.charter.model.effort ? ` · ${data.charter.model.effort}` : ''}`, tier: data.charter.tier || null, calls: data.charter.usage?.calls ?? null } : null} />;
}
