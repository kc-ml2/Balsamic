import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, errorText, percent, seconds, when } from './api';
import type { Json, State } from './api';
import { useCommand } from './commands';
import { ErrorNotice, Status } from './ui';
import type { WorkspaceActions } from './views';
import './raceProgress.css';

type RaceEndpoint = {
  seed: number; rung_seconds: number; score?: number; eligible?: boolean;
  censored?: boolean; training_updates?: number;
};
type RaceConfiguration = {
  id: string; algorithm: string; status: string; mean_score?: number; seeds: number[];
  maturity: { eligible: boolean; reasons: string[] }; endpoints: RaceEndpoint[]; trial_ids: string[];
};
type RaceView = {
  id: string; revision: number; status: string; stage: string; deadline_at?: string; batch_deadline_at?: string;
  elapsed_seconds: number; total_seconds: number; batch_seconds: number;
  running_workers: number; max_workers: number; worker_seconds_spent: number; worker_seconds_cap: number;
  remaining_worker_seconds: number; configurations: RaceConfiguration[];
  decisions: { id: string; action: string; rationale: string; created_at: string; configuration_ids: string[] }[];
  preflight?: Json; confirmation?: Json;
};
const phases: Record<string, string> = {
  numerical_checks: 'Numerical and resource checks', development: 'Longer development runs',
  adaptive_followup: 'Adaptive follow-up', confirmation: 'Fresh-seed confirmation', final_validation: 'Final numerical checks',
};
const terminal = new Set(['stopped', 'completed', 'budget_exhausted']);
function remaining(deadline?: string) {
  if (!deadline) return null;
  const date = Date.parse(deadline.endsWith('Z') || /[+-]\d\d:\d\d$/.test(deadline) ? deadline : `${deadline}Z`);
  return Number.isFinite(date) ? Math.max(0, (date - Date.now()) / 1000) : null;
}
function endpointLabel(endpoint: RaceEndpoint) {
  if (endpoint.censored) return 'Censored';
  return endpoint.eligible ? 'Eligible time endpoint' : 'Endpoint pending checks';
}

export function RaceProgress({ studyId, state, actions }: {
  studyId?: string; state: State; actions: WorkspaceActions;
}) {
  const [race, setRace] = useState<RaceView | null>(null), [error, setError] = useState('');
  const [busy, setBusy] = useState(false), [refreshing, setRefreshing] = useState(false);
  const mounted = useRef(false), path = useRef<string | null>(null), fetching = useRef(false);
  const command = useCommand(state.campaign);
  const refresh = useCallback(async () => {
    const requested = path.current;
    if (!requested || fetching.current) return;
    fetching.current = true;
    if (mounted.current) setRefreshing(true);
    try {
      const value = await api<{ race?: RaceView | null } & Partial<RaceView>>(requested);
      if (mounted.current && path.current === requested) {
        setRace(value.race !== undefined ? value.race : value.id ? value as RaceView : null);
        setError('');
      }
    } catch (e) {
      if (mounted.current && path.current === requested) {
        if (e instanceof ApiError && e.status === 404) { setRace(null); setError(''); }
        else setError(errorText(e));
      }
    } finally {
      fetching.current = false;
      if (mounted.current) setRefreshing(false);
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    path.current = studyId ? `/api/v1/studies/${encodeURIComponent(studyId)}/race` : null;
    setRace(null); setError('');
    void refresh();
    const timer = window.setInterval(() => void refresh(), 6000);
    return () => { mounted.current = false; path.current = null; window.clearInterval(timer); };
  }, [studyId, refresh]);
  async function control(action: 'pause' | 'resume' | 'stop') {
    if (!race) return;
    setBusy(true); setError('');
    try {
      await command('study.race.control', { race_id: race.id, action, expected_revision: race.revision });
      await refresh();
      await actions.refresh();
      actions.notify(`Protocol ${action} requested.`);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  if (!race) return error ? <section className="race-progress" aria-label="Adaptive testing protocol"><h2>Adaptive testing protocol</h2>
    <ErrorNotice text={error} /><button type="button" className="button small secondary" disabled={refreshing} onClick={() => void refresh()}>Retry protocol status</button></section> : null;
  const batchRemaining = remaining(race.batch_deadline_at), overallRemaining = remaining(race.deadline_at);
  const ended = terminal.has(race.status);
  return <section className="race-progress" aria-label="Adaptive testing protocol">
    <div className="race-progress-heading"><div><span className="eyebrow">Adaptive testing protocol</span><h2>{phases[race.stage] || race.stage?.replaceAll('_', ' ')}</h2><Status status={race.status} /></div>
      <div className="race-progress-controls">
        <button type="button" className="button small secondary" disabled={busy || refreshing} onClick={() => void refresh()}>Refresh protocol</button>
        {!ended && <><button type="button" className="button small secondary" disabled={busy} onClick={() => void control(race.status === 'paused' ? 'resume' : 'pause')}>{race.status === 'paused' ? 'Resume protocol' : 'Pause protocol'}</button>
          <button type="button" className="button small secondary" disabled={busy} onClick={() => void control('stop')}>Stop protocol</button></>}
      </div></div>
    <div className="race-progress-summary" role="status">
      <div><span>Overall elapsed / limit</span><strong>{seconds(race.elapsed_seconds)} / {seconds(race.total_seconds)}</strong>{!ended && overallRemaining !== null && <small>{seconds(overallRemaining)} remaining</small>}</div>
      <div><span>Batch wall-clock limit</span><strong>{seconds(race.batch_seconds)}</strong>{!ended && batchRemaining !== null && <small>{seconds(batchRemaining)} remaining</small>}</div>
      <div><span>Concurrent workers</span><strong>{race.running_workers} / {race.max_workers}</strong><small>Admission follows resource checks</small></div>
      <div><span>Worker time spent / cap</span><strong>{seconds(race.worker_seconds_spent)} / {seconds(race.worker_seconds_cap)}</strong><small>{seconds(race.remaining_worker_seconds)} remaining worker time</small></div>
    </div>
    <p className="help-text">Elapsed time and summed worker time have separate limits. Pausing preserves the overall deadline. Scores are development evidence until fresh-seed confirmation and numerical checks are complete.</p>
    <p className="help-text">Mean incumbents summarize available runs, which may have different allowances. Allocation decisions compare measured endpoints at the same cumulative worker time.</p>
    {!!race.configurations?.length && <div className="table-scroll"><table className="data-table race-progress-table"><thead><tr><th>Configuration</th><th>Allocation state</th><th>Mean incumbent</th><th>Learning maturity</th><th>Observed time endpoints</th></tr></thead>
      <tbody>{race.configurations.map(configuration => <tr key={configuration.id}>
        <td><strong>{configuration.algorithm}</strong><small>{configuration.id}</small><small>Seeds {(configuration.seeds || []).join(', ') || 'not allocated'}</small>
          {(configuration.trial_ids || []).map(id => <a key={id} href={`#experiments/${encodeURIComponent(id)}`}>Open run {id.slice(-8)}</a>)}</td>
        <td><Status status={configuration.status} /></td><td>{percent(configuration.mean_score)}</td>
        <td>{configuration.maturity?.eligible ? 'Minimum activity met' : 'Undertrained or unassessed'}
          {(configuration.maturity?.reasons || []).map(reason => <small key={reason}>{reason}</small>)}</td>
        <td>{!(configuration.endpoints || []).length ? 'No measured endpoints' : configuration.endpoints.map((endpoint, index) => <div className="race-progress-endpoint" key={`${endpoint.seed}-${endpoint.rung_seconds}-${index}`}>
          <span>Seed {endpoint.seed} · {seconds(endpoint.rung_seconds)} · {percent(endpoint.score)}</span>
          <small>{endpointLabel(endpoint)}{typeof endpoint.training_updates === 'number' && ` · ${endpoint.training_updates} training updates`}</small></div>)}</td>
      </tr>)}</tbody></table></div>}
    <details className="race-progress-decisions"><summary>Allocation decisions ({race.decisions?.length || 0})</summary>
      {!race.decisions?.length ? <p className="help-text">No allocation decisions recorded yet.</p> : <ol>{[...race.decisions].reverse().map(decision => <li key={decision.id}>
        <strong>{decision.action.replaceAll('_', ' ')}</strong><time>{when(decision.created_at)}</time><p>{decision.rationale}</p>
        <small>{decision.configuration_ids?.join(', ')}</small></li>)}</ol>}
    </details>
    <details className="race-progress-evidence"><summary>Numerical checks and confirmation</summary><pre>{JSON.stringify({ preflight: race.preflight, confirmation: race.confirmation }, null, 2)}</pre></details>
    <p className="help-text">Protocol <code>{race.id}</code> · revision {race.revision} · <a href="#comparison">Compare learning curves in TensorBoard</a>.</p>
    <ErrorNotice text={error} />
  </section>;
}
