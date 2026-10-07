import { useEffect, useState } from 'react';
import { activeStatuses, api, best, errorText, objectiveValue, seconds } from './api';
import type { Json, State, Trial } from './api';
import { Badge, Design, Empty, ErrorNotice, Field, Modal, Panel, Status } from './ui';
import type { WorkspaceActions } from './views';
import { CharterHistory } from './views';
import { EvaluatorRequest } from './evaluatorForms';
import { useCommand } from './commands';
import type { Task } from './api';
import { ReadableText } from './readableText';

type Props = { state: State; actions: WorkspaceActions };

function Candidate({ problem, values }: { problem?: Json; values?: number[] }) {
  // Only reviewed renderers compiled into the product are used. Package-provided
  // HTML, scripts, or renderer paths never reach this component.
  if (problem?.definition_id === 'meent_grating') return <Design cells={values} />;
  if (!values?.length) return <p className="muted">No feasible candidate observed yet.</p>;
  return <div className="table-scroll"><table className="data-table"><thead><tr><th>Coordinate</th><th>Value</th></tr></thead><tbody>
    {values.map((value, i) => <tr key={i}><td>x{i + 1}</td><td>{value.toPrecision(7)}</td></tr>)}
  </tbody></table></div>;
}

export function GeneralOverview({ state, actions }: Props) {
  const active = state.trials.filter(t => activeStatuses.includes(t.status));
  return <>
    <div className="page-heading"><div><span className="eyebrow">Research campaign</span><h1>{state.campaign?.name}</h1><ReadableText text={state.campaign?.objective} preview={2} /></div>
      <button className="button primary" onClick={() => actions.launch()}>Design an experiment</button></div>
    <Panel title="Research activity"><div className="overview-activity"><p>{active.length} active experiments · {state.hypotheses.length} candidate methods</p>
      <p>{seconds(state.budget?.spent_seconds || 0)} of {seconds(state.campaign?.compute_budget_seconds)} spent</p>
      <button className="text-button" onClick={() => actions.navigate('experiments')}>Inspect experiments</button></div></Panel>
    <Panel title="Objective by problem instance"><div className="configuration-grid">{state.tasks.map(task => {
      const objective = task.problem?.primary_objective;
      const measured = state.trials.filter(t => t.task_id === task.id && !t.diagnostic_grant_id && !t.recipe && best(t) !== undefined);
      const values = measured.map(t => best(t)!);
      const value = values.length ? (objective?.direction === 'minimize' ? Math.min(...values) : Math.max(...values)) : undefined;
      return <article className="configuration-card" key={task.id}><h3>{task.name}</h3><p>{objective?.direction} {objective?.name}</p>
        <strong>{objectiveValue(value, objective)}</strong><p>{objective?.units}</p><span className="help-text">Best observed value · validation evidence is separate</span></article>;
    })}</div></Panel>
    {(state.manager_issues || []).filter((i: Json) => i.status === 'pending').map((issue: Json) => <div className="callout amber" key={issue.id}>
      <strong>Campaign manager issue</strong><p>{issue.message}</p><button className="text-button" onClick={() => void actions.research(`Resolve issue ${issue.id}: ${issue.message}`, 'discuss')}>Discuss with manager</button></div>)}
  </>;
}

export function GeneralProblem({ state, actions }: Props) {
  const [selected, setSelected] = useState<Json | null>(null);
  const [commissioning, setCommissioning] = useState<Task | null>(null);
  const [history, setHistory] = useState(false);
  return <><div className="page-heading"><div><span className="eyebrow">Problem workbench</span><h1>A shared definition of success.</h1><ReadableText text={state.campaign?.objective} /></div>
    <div className="inline-actions"><button className="button secondary" onClick={() => setHistory(true)}>Revision history</button>
      <button className="button primary" onClick={() => actions.campaign(true)}>Revise charter</button></div></div>
    <Panel title="Problem instances"><div className="configuration-grid">{state.tasks.map(task => <article className="configuration-card" key={task.id}>
      <div className="row-between"><Badge>{task.split}</Badge><span>{task.problem?.candidate_schema.representation}</span></div><h3>{task.name}</h3>
      <p>{task.problem?.primary_objective.direction} <strong>{task.problem?.primary_objective.name}</strong> · {task.problem?.primary_objective.units}</p>
      <p>{task.problem?.candidate_schema.dimensions} dimensions · {task.problem?.candidate_schema.constraints?.length || 0} explicit constraints</p>
      {task.evaluator_readiness && <p className={task.evaluator_readiness.runnable ? 'help-text' : 'callout amber'}>{task.evaluator_readiness.reason}</p>}
      <div className="inline-actions"><button className="button small secondary" onClick={() => setSelected(task)}>Inspect definition</button>
        {task.evaluator_manifest && !task.evaluator_version_id && <button className="button small secondary" onClick={() => setCommissioning(task)}>Build or reuse evaluator</button>}
        {task.evaluator_version_id && task.evaluator_readiness?.state !== 'ready' && <button className="button small secondary" onClick={() => actions.navigate('validation')}>Review evaluator evidence</button>}
        <button className="button small primary" onClick={() => actions.launch(undefined, task.id)}>Design a probe</button></div></article>)}</div></Panel>
    <Panel title="Frozen study history">{(state.studies || []).map((study: Json) => <article key={study.id}><h3>{study.goal}</h3><Badge>{study.scope}</Badge>
      <p className="help-text">{study.id}{study.parent_study_id ? ` · follows ${study.parent_study_id}` : ''}</p></article>)}</Panel>
    {selected && <Modal title={selected.name} description="Resolved scientific definition, feasibility constraints, and evaluator fidelity." onClose={() => setSelected(null)}>
      <pre>{JSON.stringify(selected.problem, null, 2)}</pre></Modal>}
    {history && state.campaign && <CharterHistory campaignId={state.campaign.id} onClose={() => setHistory(false)} />}
    {commissioning && <EvaluatorRequest task={commissioning} state={state} onClose={() => setCommissioning(null)} onDone={() => { void actions.refresh(); }} />}</>;
}

export function GeneralExperiments({ state, actions }: Props) {
  const linkedTrial = () => location.hash.startsWith('#experiments/') ? location.hash.slice('#experiments/'.length) : null;
  const [selected, setSelected] = useState<string | null>(linkedTrial);
  const [studyId, setStudyId] = useState(state.campaign?.active_study_id || 'all');
  useEffect(() => {
    const change = () => setSelected(linkedTrial());
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);
  useEffect(() => { setStudyId(state.campaign?.active_study_id || 'all'); }, [state.campaign?.id, state.campaign?.active_study_id]);
  const trials = state.trials.filter(item => studyId === 'all' || item.study_id === studyId);
  const trial = state.trials.find(t => t.id === selected);
  function closeDetail() { setSelected(null); history.replaceState(null, '', '#experiments'); }
  return <><div className="page-heading"><div><span className="eyebrow">Experiments</span><h1>Follow the evidence.</h1><p>Raw objectives, measured expenditure, and scientific completion remain distinct.</p></div>
    <button className="button primary" onClick={() => actions.launch()}>Design an experiment</button></div>
    <Field label="Experiment study"><select value={studyId} onChange={event => setStudyId(event.target.value)}>
      <option value="all">All studies</option>{(state.studies || []).map((study: Json) => <option key={study.id} value={study.id}>{study.id === state.campaign?.active_study_id ? 'Current · ' : ''}{study.scope} · {study.id}</option>)}
    </select></Field>
    <p className="help-text">Live counters refresh automatically. <a href="#studies">Open Studies for the run roster, allocation and launch status.</a></p>
    {!trials.length ? <Empty title="No experiments in this study">A selected study does not start runs automatically. Open Studies to review its allocation and schedule its runs.</Empty> :
      <Panel title="Experiment history"><div className="table-scroll"><table className="data-table"><thead><tr><th>Method / problem</th><th>Status</th><th>Best objective</th><th>Requests / solves</th><th>Worker time</th><th>Scientific completion</th></tr></thead>
        <tbody>{trials.map(t => { const p = { ...t.result, ...t.progress }; return <tr key={t.id}>
          <td><button className="table-link" onClick={() => setSelected(t.id)}>{state.algorithms.find(a => a.id === t.algorithm)?.name || t.algorithm}</button><small>Seed {t.seed} · {t.id}</small><small>{state.tasks.find(task => task.id === t.task_id)?.name}{t.diagnostic_grant_id ? ' · Diagnostic job' : ''}</small></td>
          <td><Status status={t.status} /></td><td>{objectiveValue(best(t), t.problem?.primary_objective)}</td><td>{p.evaluations || 0} / {p.solver_calls || 0}{p.unknown_solver_cost && ' + unknown'}</td>
          <td>{seconds(p.elapsed_seconds)}</td><td>{p.scientific_complete === true ? 'Complete' : 'Incomplete'}</td></tr>; })}</tbody></table></div></Panel>}
    {trial && <ExperimentDetail trial={trial} state={state} actions={actions} onClose={closeDetail} />}</>;
}

function ExperimentDetail({ trial, state, actions, onClose }: { trial: Trial; state: State; actions: WorkspaceActions; onClose: () => void }) {
  const command = useCommand(state.campaign);
  const [rows, setRows] = useState<Json[]>([]), [error, setError] = useState('');
  const progress = { ...trial.result, ...trial.progress };
  useEffect(() => { let current = true; api<Json[]>(`/api/trials/${trial.id}/metrics?limit=20`).then(items => { if (current) { setRows(items); setError(''); } }).catch(e => { if (current) setError(errorText(e)); }); return () => { current = false; }; }, [trial.id, progress.step]);
  async function control(action: string) {
    try { await command('trial.control', { trial_id: trial.id, action, expected_control_revision: trial.control_revision ?? 0 }); await actions.refresh(); }
    catch (e) { setError(errorText(e)); }
  }
  return <Modal wide title={`${trial.algorithm} · seed ${trial.seed}`} description={trial.question || 'Bounded optimization experiment'} onClose={onClose}>
    <div className="row-between"><Status status={trial.status} /><Badge tone={progress.scientific_complete ? 'green' : 'amber'}>{progress.scientific_complete ? 'Scientific procedure complete' : 'Scientific procedure incomplete'}</Badge></div>
    <p>{trial.problem?.primary_objective.direction} {trial.problem?.primary_objective.name}: <strong>{objectiveValue(best(trial), trial.problem?.primary_objective)}</strong></p>
    <Candidate problem={trial.problem} values={progress.best_candidate || progress.best_design} />
    <p>{progress.evaluations || 0} evaluation requests · {progress.solver_calls || 0} confirmed solver executions · {seconds(progress.elapsed_seconds)} actual worker time</p>
    {progress.recovered_suffix_observations > 0 && <p className="callout amber">{progress.recovered_suffix_observations} observations are retained from the prior attempt after its last checkpoint. Recovery repeated work from that checkpoint.</p>}
    {progress.reason && <p>{progress.reason}</p>}
    {trial.diagnostic_grant_id && <p className="callout">This is an independent diagnostic job. Its discoveries do not update the source optimizer or its search result.</p>}
    <div className="inline-actions">
      {['queued', 'running'].includes(trial.status) && <button className="button secondary" onClick={() => void control('pause')}>Pause</button>}
      {['paused', 'interrupted'].includes(trial.status) && <button className="button secondary" onClick={() => void control('resume')}>Resume</button>}
      {activeStatuses.includes(trial.status) && <button className="button secondary" onClick={() => void control('stop')}>Stop</button>}
      <button className="button secondary" onClick={() => { onClose(); actions.trialAction(trial, 'extend'); }}>Extend allocation</button>
      {trial.problem?.definition_id === 'meent_grating' && <button className="button secondary" onClick={() => { onClose(); actions.trialAction(trial, 'validate'); }}>Check physical convergence</button>}
    </div><ErrorNotice text={error} />
    {(state.diagnostic_grants || []).some((grant: Json) => grant.parent_trial_id === trial.id) && <><h3>Declared diagnostic milestones</h3>
      <div className="table-scroll"><table className="data-table"><thead><tr><th>Milestone</th><th>Capture / dispatch</th><th>Evidence</th><th>Independent jobs</th></tr></thead>
        <tbody>{state.diagnostic_grants.filter((grant: Json) => grant.parent_trial_id === trial.id).map((grant: Json) => <tr key={grant.id}>
          <td>{grant.count} {grant.unit.replaceAll('_', ' ')}</td><td>{grant.status.replaceAll('_', ' ')}</td><td>{grant.asset_ids?.length || 0} assets</td>
          <td>{(grant.trial_ids || []).map((identity: string) => { const child = state.trials.find(item => item.id === identity); return <div key={identity}>{child?.algorithm} · {child?.status}</div>; })}</td>
        </tr>)}</tbody></table></div><p className="help-text">Each snapshot binds its observed search prefix. Checks and policy episodes have their own evidence and costs.</p></>}
    <h3>Recent observations</h3><div className="table-scroll"><table className="data-table"><thead><tr><th>Trajectory step</th><th>Raw objective</th><th>Best objective</th><th>Actual requests</th></tr></thead>
      <tbody>{rows.slice(-20).map((row, i) => <tr key={i}><td>{row.step}</td><td>{objectiveValue(row.objective, trial.problem?.primary_objective)}</td><td>{objectiveValue(row.best_objective, trial.problem?.primary_objective)}</td><td>{row.evaluations}</td></tr>)}</tbody></table></div>
    <details><summary>Frozen problem and execution diagnostics</summary><pre>{JSON.stringify({ problem: trial.problem, diagnostics: progress.diagnostics, study_id: trial.study_id }, null, 2)}</pre></details>
  </Modal>;
}
