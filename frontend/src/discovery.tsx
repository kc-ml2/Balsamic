import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, errorText, providerStatus } from './api';
import type { Json, State } from './api';
import { useCommand } from './commands';
import { ErrorNotice, Field, Modal, Status } from './ui';
import { DiscoveryDialogue } from './discoveryDialogue';
import { researchRequestNotice } from './researchProgress';

const terminal = new Set(['completed', 'stopped', 'exhausted']);

export function DiscoveryPanel({ state, refresh }: { state: State; refresh: () => Promise<void> }) {
  const command = useCommand(state.campaign);
  const [view, setView] = useState<Json>({ sessions: [], tasks: [], artifacts: [], candidates: [] });
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const tasks = state.tasks.filter(task => task.split === 'development');
  const [taskId, setTaskId] = useState(tasks[0]?.id || '');
  const [calls, setCalls] = useState(96), [parallel, setParallel] = useState(3), [sourceLimit, setSourceLimit] = useState(64);
  const [experimentSeconds, setExperimentSeconds] = useState(Math.min(300, Math.max(0,
    (state.campaign?.compute_budget_seconds || 0) - (state.campaign?.validation_reserve_seconds || 0))));
  const [objective, setObjective] = useState(state.campaign?.objective || 'Find an effective optimizer for this problem.');
  const [note, setNote] = useState(''), [target, setTarget] = useState<Json | null>(null);
  const [assessmentViews, setAssessmentViews] = useState<Record<string, Json>>({});
  const campaignId = state.campaign?.id;
  const piOwned = !!state.agent_runtime?.configuration?.enabled;
  const active = [...(view.sessions || [])].reverse().find((session: Json) => !terminal.has(session.status));
  const session = active || view.sessions?.at(-1);
  async function load() {
    if (campaignId) setView(await api(`/api/campaigns/${encodeURIComponent(campaignId)}/discovery`));
  }
  useEffect(() => {
    if (!campaignId) return;
    let alive = true;
    const poll = async () => {
      try {
        const next = await api(`/api/campaigns/${encodeURIComponent(campaignId)}/discovery`);
        if (alive) setView(next);
      } catch (e) { if (alive) setError(errorText(e)); }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [campaignId]);
  async function act(operation: string, payload: Json) {
    setBusy(true); setError('');
    try { await command(operation, payload); await load(); await refresh(); return true; }
    catch (e) { setError(errorText(e)); return false; }
    finally { setBusy(false); }
  }
  async function start(e: FormEvent) {
    e.preventDefault();
    await act('discovery.start', { task_id: taskId, objective, model_call_limit: calls,
      max_concurrent_tasks: parallel, source_request_limit: sourceLimit, experiment_compute_seconds: experimentSeconds });
  }
  async function inspectAssessment(id: string) {
    try {
      const result = await api(`/api/campaigns/${encodeURIComponent(campaignId!)}/discovery/assessments/${encodeURIComponent(id)}`);
      setAssessmentViews(previous => ({ ...previous, [id]: result }));
    } catch (e) { setError(errorText(e)); }
  }
  function discuss(kind: string, item: Json) {
    setTarget({ ...item, discussionKind: kind, prerequisites: (item.dependencies || []).map((id: string) => {
      const dependency = (view.tasks || []).find((task: Json) => task.id === id);
      return { id, status: dependency?.status || 'unknown', title: dependency?.brief?.key?.replaceAll('_', ' ') || id };
    }) });
  }
  const sessionTasks = (view.tasks || []).filter((task: Json) => task.session_id === session?.id);
  return <section className="panel discovery-panel" aria-label="Optimizer discovery">
    <h2>{piOwned ? 'Discovery archive' : 'Optimizer discovery'}</h2>
    <p>{piOwned ? 'Pi continues this campaign from the saved discoveries below. Send new work through the lead agent conversation above.' : 'Agents analyze the executable problem, study sources, and develop candidates through the campaign manager. Their work is saved across sessions.'}</p>
    {!providerStatus(state).configured && <p className="callout">Model configuration is required for discovery. Starting a session saves its agenda and waits for an enabled model.</p>}
    <ErrorNotice text={error} />
    {!active && !piOwned && <form onSubmit={start} className="discovery-start">
      <Field label="Executable problem"><select aria-label="Executable problem" value={taskId} onChange={e => setTaskId(e.target.value)}>
        {!taskId && <option value="">Select a development problem</option>}
        {tasks.map(task => <option key={task.id} value={task.id} disabled={task.evaluator_readiness?.runnable === false}>{task.name}{task.evaluator_readiness?.runnable === false ? ' — evaluator unavailable' : ''}</option>)}
      </select></Field>
      <Field label="Discovery objective"><textarea aria-label="Discovery objective" rows={3} value={objective} onChange={e => setObjective(e.target.value)} required /></Field>
      <div className="form-grid">
        <Field label="Model call limit"><input aria-label="Model call limit" type="number" min={4} max={10000} value={calls} onChange={e => setCalls(Number(e.target.value))} /></Field>
        <Field label="Concurrent tasks"><input aria-label="Concurrent tasks" type="number" min={1} max={8} value={parallel} onChange={e => setParallel(Number(e.target.value))} /></Field>
        <Field label="Source request limit"><input aria-label="Source request limit" type="number" min={0} max={1000} value={sourceLimit} onChange={e => setSourceLimit(Number(e.target.value))} /></Field>
        <Field label="Experiment allocation (seconds)"><input aria-label="Experiment allocation (seconds)" type="number" min={0} max={604800} value={experimentSeconds} onChange={e => setExperimentSeconds(Number(e.target.value))} /></Field>
      </div>
      <p>API usage remains inside the campaign cap. Three calls are reserved for the manager. Numerical experiments and implementation work retain their campaign permissions and budgets.</p>
      <button className="button primary" disabled={busy || !taskId || !objective.trim()}>Start discovery</button>
    </form>}
    {session && <>
      <div className="row-between"><h3>{session.policy.objective}</h3><Status status={session.status} /></div>
      <p>{session.policy.model_call_limit} model calls · {session.policy.source_request_limit ?? 64} source requests · {session.policy.max_concurrent_tasks} concurrent tasks · {session.policy.experiment_compute_seconds ?? 0}s for experiments</p>
      {active && !piOwned && <div className="button-row">
        <button className="button secondary" disabled={busy} onClick={() => void act('discovery.control', {
          session_id: active.id, action: active.status === 'paused' ? 'resume' : 'pause', expected_control_revision: active.control_revision,
        })}>{active.status === 'paused' ? 'Resume discovery' : 'Pause discovery'}</button>
        <button className="button secondary" disabled={busy} onClick={() => void act('discovery.control', {
          session_id: active.id, action: 'stop', expected_control_revision: active.control_revision,
        })}>Stop discovery</button>
      </div>}
      <p>Pausing prevents new discovery dispatch. Work already sent can finish into saved results. Existing experiments have their own controls.</p>
      <DiscoveryDialogue view={view} sessionId={session.id} sources={state.sources || []} />
      <div className="discovery-agenda">{sessionTasks.map((task: Json) => <article key={task.id}>
        <div className="row-between"><strong>{task.brief.role.replaceAll('_', ' ')}</strong><Status status={task.status} /></div>
        <p>{task.brief.objective}</p>
        {task.wait_reason && <p>Waiting: {task.wait_reason.replaceAll('_', ' ')}</p>}
        {task.result?.summary && <p>{task.result.summary}</p>}
        {task.handoff_id && <p>Partial work saved for the manager. {task.handoff_reason}</p>}
        {task.error && <ErrorNotice text={task.error} />}
        <button className="button small secondary" onClick={() => discuss('task', task)}>Discuss this task</button>
        <details><summary>Task and dependencies</summary><pre>{JSON.stringify(task, null, 2)}</pre></details>
      </article>)}</div>
      {(view.wrap_ups || []).filter((item: Json) => item.id === session.wrap_up_id).map((item: Json) =>
        <article key={item.id}><h3>Saved partial wrap-up</h3><p>{item.summary}</p>
          <details><summary>Remaining work and saved evidence</summary><pre>{JSON.stringify(item, null, 2)}</pre></details>
        </article>)}
      {(view.handoffs || []).filter((item: Json) => item.session_id === session.id).map((item: Json) =>
        <details key={item.id}><summary>Continuation: {item.objective}</summary><p>{item.summary}</p>
          <pre>{JSON.stringify(item, null, 2)}</pre>
          <button className="button small secondary" onClick={() => discuss('continuation', item)}>Discuss this continuation</button>
        </details>)}
      {(view.artifacts || []).filter((item: Json) => item.session_id === session.id).map((item: Json) => <details key={item.id}>
        <summary>{item.kind.replaceAll('_', ' ')}: {item.title}{item.stale ? ' (earlier guidance)' : ''}</summary>
        <pre>{JSON.stringify(item, null, 2)}</pre>
        {item.kind === 'assessment_plan' && !item.stale && !(view.assessments || []).some((row: Json) => row.source_artifact_id === item.id) &&
          <button className="button small secondary" disabled={busy} onClick={() => void act('discovery.assessment.save', {
            session_id: session.id, plan: item.content, source_artifact_id: item.id,
          })}>Prepare assessment drafts</button>}
        <button className="button small secondary" onClick={() => discuss('result', item)}>Discuss this result</button>
      </details>)}
      {(view.assessments || []).filter((item: Json) => item.session_id === session.id).map((item: Json) => {
        const inspected = assessmentViews[item.id];
        const launched = inspected?.readiness.cells.every((cell: Json) => cell.readiness.trial_id);
        return <article key={item.id} className="discovery-assessment">
          <h3>{item.plan.question}</h3>
          <p>{item.configurations.length} configurations × {item.plan.seeds.length} seeds · {item.plan.evaluations_per_trial} evaluations per run</p>
          <button className="button small secondary" onClick={() => void inspectAssessment(item.id)}>Inspect assessment readiness and evidence</button>
          {inspected && <>
            <p>Assessment: {inspected.evidence.status.replaceAll('_', ' ')}. {inspected.evidence.informative_configurations} of {inspected.evidence.required_configurations} configurations have the required evidence.</p>
            {inspected.readiness.blockers.map((blocker: Json, index: number) => <p key={index}>{blocker.message}</p>)}
            {!launched && <button className="button secondary" disabled={busy || !inspected.readiness.ready} onClick={() => void act('discovery.assessment.launch', {
              assessment_id: item.id, expected_readiness_hash: inspected.readiness.readiness_hash,
            }).then(() => inspectAssessment(item.id))}>Launch tuning batch</button>}
            <details><summary>Measurements, missing evidence and frozen configurations</summary><pre>{JSON.stringify(inspected, null, 2)}</pre></details>
          </>}
          <button className="button small secondary" onClick={() => discuss('assessment', item)}>Discuss this assessment</button>
        </article>;
      })}
      <form onSubmit={e => { e.preventDefault(); void act('research.start', { mode: 'discuss', message: note }).then(ok => { if (ok) setNote(''); }); }}>
        <Field label="Message to campaign manager">
          <textarea aria-label="Discovery message to manager" value={note} onChange={e => setNote(e.target.value)} rows={3} required />
        </Field>
        <button className="button secondary" disabled={busy || !note.trim()}>Send to manager</button>
      </form>
      {target && <DiscoveryDiscussion key={target.id} target={target} state={state} session={session}
        refresh={async () => { await load(); await refresh(); }} onClose={() => setTarget(null)} />}
      <details><summary>Session policy and history</summary><pre>{JSON.stringify(view.sessions, null, 2)}</pre></details>
      <p>Detailed prompts, ordinary responses, tool activity and agent handoffs are in the Agent log tab.</p>
    </>}
  </section>;
}

function DiscoveryDiscussion({ target, state, session, refresh, onClose }: {
  target: Json; state: State; session: Json; refresh: () => Promise<void>; onClose: () => void;
}) {
  const command = useCommand(state.campaign);
  const [note, setNote] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false), [saved, setSaved] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);
  // The parent effect runs after Modal opens its native dialog. React's
  // autoFocus runs earlier, while the textarea is still inside a closed dialog.
  useEffect(() => { editor.current?.focus(); }, []);
  const prerequisites = (target.prerequisites || []).filter((task: Json) => task.status !== 'completed');
  const reference = [`Regarding ${target.id}:`, target.status && `Recorded status: ${target.status}`,
    target.error && `Reported blocker: ${target.error}`,
    ...prerequisites.map((task: Json) => `Prerequisite ${task.id}: ${task.status}`)].filter(Boolean).join('\n');
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || saved || !note.trim()) return;
    setBusy(true); setError('');
    try {
      const outcome = await command('research.start', { mode: 'discuss', message: `${reference}\n\n${note.trim()}` });
      setSaved(true);
      setNotice(session.status === 'paused'
        ? 'Message saved for the campaign manager. Discovery is paused; resume it in the progress panel to process your message.'
        : researchRequestNotice(state, outcome));
      // An accepted message stays accepted even if the following read fails.
      try { await refresh(); }
      catch (failure) { setError(`Your message was saved, but refreshing the workspace failed: ${errorText(failure)}`); }
    } catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); }
  }
  return <Modal title={`Discuss this ${target.discussionKind}`} description="Send a message to the campaign manager with this item's reference attached." onClose={onClose}>
    <p><strong>{target.brief?.key?.replaceAll('_', ' ') || target.title || target.plan?.question || target.brief?.role?.replaceAll('_', ' ') || 'Saved continuation'}</strong>{target.status && <> · <Status status={target.status} /></>}</p>
    {target.error && <p className="callout">{target.error}</p>}
    {!!prerequisites.length && <div><strong>Unfinished prerequisites</strong><ul>{prerequisites.map((task: Json) =>
      <li key={task.id}>{task.title} · {task.status.replaceAll('_', ' ')}</li>)}</ul></div>}
    <details><summary>Attached reference and context</summary>
      {target.brief?.objective && <p>{target.brief.objective}</p>}<pre>{reference}</pre>
    </details>
    {saved ? <><p role="status">{notice}</p><ErrorNotice text={error} />
      <div className="modal-actions"><button type="button" className="button primary" onClick={onClose}>Done</button></div></> :
      <form onSubmit={submit}>
        <Field label="Message to campaign manager">
          <textarea aria-label="Discovery message to manager" ref={editor} value={note} onChange={e => setNote(e.target.value)}
            rows={4} maxLength={Math.max(0, 20000 - reference.length - 2)} required placeholder="Ask about this item or suggest how to proceed." />
        </Field>
        <ErrorNotice text={error} />
        <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button>
          <button className="button primary" disabled={busy || !note.trim()}>{busy ? 'Sending…' : 'Send to manager'}</button></div>
      </form>}
  </Modal>;
}
