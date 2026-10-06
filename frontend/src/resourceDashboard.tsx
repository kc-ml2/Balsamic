import { useEffect, useRef, useState } from 'react';
import { errorText, seconds, when } from './api';
import { ErrorNotice, Icon, Status } from './ui';
import './resourceDashboard.css';

type Measurement = number | null | undefined;
type Timestamp = string | number | null | undefined;
type ResourceJob = {
  id: string; kind: string; status: string; algorithm?: string | null; seed?: number | null; pid?: number | null; process_state?: string | null;
  rss_bytes?: Measurement; peak_rss_bytes?: Measurement; cpu_percent?: Measurement;
  threads?: Measurement; wall_seconds?: Measurement; spent_seconds?: Measurement; remaining_seconds?: Measurement;
};
type ResourcePhase = {
  name: string; state: string; jobs?: Measurement; worker_seconds?: Measurement;
  threads?: Measurement; concurrency?: Measurement; estimate_basis?: string;
};
type MemoryForecast = {
  fidelity?: Record<string, number> | null; predicted_bytes?: Measurement; headroom_bytes?: Measurement;
  available_bytes?: Measurement; historical_available_bytes?: Measurement; basis?: string; observed_at?: Timestamp;
  measured_peak_bytes?: Measurement; measured_fidelity?: Record<string, number> | null; measured_at?: Timestamp;
  safety_factor?: Measurement; historical_fits?: boolean | null; fits_now?: boolean | null; historical_reason?: string | null;
};
type MemoryCheck = {
  fidelity?: Record<string, number> | null; harmonic_count?: Measurement; predicted_bytes?: Measurement;
  single_matrix_bytes?: Measurement; measured_peak_bytes?: Measurement; basis?: string; headroom_bytes?: Measurement;
  fits_now?: boolean | null; expanded_complex_grid_bytes?: Measurement; expanded_grid_shape?: { x: number; y: number } | null;
  fft_workspace_lower_bound_bytes?: Measurement; analytical_lower_bound_bytes?: Measurement;
};
type ResourcePlan = {
  race_id: string; status: string; stage: string; deadline_at?: Timestamp; elapsed_seconds?: Measurement;
  total_seconds?: Measurement; worker_seconds_spent?: Measurement; worker_seconds_cap?: Measurement;
  pending_jobs?: Measurement; blocked_reason?: string | null; memory_forecast?: MemoryForecast | null;
  ended?: boolean; memory_checks?: MemoryCheck[];
  upcoming_jobs?: { configuration_id?: string; algorithm?: string; seed?: number; phase?: string; target_seconds?: Measurement; remaining_seconds?: Measurement; status?: string }[];
  phases?: ResourcePhase[]; decisions?: { action?: string; rationale?: string; created_at?: Timestamp }[];
};
type ResourceSnapshot = {
  sampled_at: string; campaign_id?: string | null;
  host: { cpu: { logical_count?: Measurement; capacity_cores?: Measurement; utilization_percent?: Measurement; load_average?: number[] | null };
    memory: { total_bytes?: Measurement; available_bytes?: Measurement; effective_available_bytes?: Measurement; used_bytes?: Measurement;
      swap_total_bytes?: Measurement; swap_used_bytes?: Measurement; swap_free_bytes?: Measurement; cgroup_limit_bytes?: Measurement };
    gpu?: { status?: string; devices?: GpuDevice[] };
    processes?: { pid: number; name: string; rss_bytes?: Measurement; threads?: Measurement; role?: string }[] };
  service?: { pid?: number; rss_bytes?: Measurement; peak_rss_bytes?: Measurement; threads?: Measurement; cpu_percent?: Measurement };
  workers: { configured_limit?: Measurement; running_count?: Measurement; queued_count?: Measurement; jobs: ResourceJob[] };
  budget?: { actual_seconds?: Measurement; allocated_seconds?: Measurement; limit_seconds?: Measurement; remaining_seconds?: Measurement; grants?: unknown[] } | null;
  plans: ResourcePlan[]; warnings?: string[];
};
type HistoryPoint = { at: number; cpu: Measurement; memory: Measurement };
type GpuDevice = { id?: string; vendor?: string; utilization_percent?: Measurement; memory_total_bytes?: Measurement; memory_used_bytes?: Measurement; memory_kind?: string };

function finite(value: Measurement): value is number { return typeof value === 'number' && Number.isFinite(value); }
function bytes(value: Measurement) {
  if (!finite(value)) return 'Unknown';
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(0)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(1)} GiB`;
}
function number(value: Measurement, digits = 0) { return finite(value) ? value.toFixed(digits) : 'Unknown'; }
function ratio(value: Measurement, total: Measurement) { return finite(value) && finite(total) && total > 0 ? Math.max(0, Math.min(100, value / total * 100)) : 0; }
function label(value?: string | null) { return value ? value.replaceAll('_', ' ') : 'Unknown'; }
function stamp(value?: Timestamp) {
  if (typeof value === 'number') return Number.isFinite(value) ? value * 1000 : NaN;
  return value ? Date.parse(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`) : NaN;
}
function displayTime(value?: Timestamp) { const timestamp = stamp(value); return Number.isFinite(timestamp) ? when(new Date(timestamp).toISOString()) : '—'; }
function fidelityLabel(fidelity?: Record<string, number> | null) {
  if (!fidelity) return 'Unknown order';
  if ('rcwa_order_x' in fidelity && 'rcwa_order_y' in fidelity) return `(${fidelity.rcwa_order_x}, ${fidelity.rcwa_order_y})`;
  return Object.entries(fidelity).map(([key, value]) => `${key}: ${value}`).join(' · ');
}

function HistoryChart({ points, field, maximum, title }: { points: HistoryPoint[]; field: 'cpu' | 'memory'; maximum: Measurement; title: string }) {
  const usable = points.filter(point => finite(point[field]));
  if (!usable.length || !finite(maximum) || maximum <= 0) return <p className="resource-chart-empty">Waiting for a measured sample</p>;
  const first = usable[0].at, last = usable[usable.length - 1].at;
  const xy = usable.map(point => ({ x: usable.length === 1 ? 300 : (point.at - first) / Math.max(1, last - first) * 600, y: 64 - ratio(point[field], maximum) * .58, point }));
  return <svg className="resource-history-chart" viewBox="0 0 600 70" role="img" aria-label={`${title}, ${usable.length} measured samples`} preserveAspectRatio="none">
    <title>{title}. Recent samples are collected while this page is open.</title>
    {[6, 35, 64].map(y => <line key={y} x1="0" x2="600" y1={y} y2={y} className="resource-chart-grid" />)}
    {xy.length > 1 && <polyline fill="none" points={xy.map(point => `${point.x},${point.y}`).join(' ')} />}
    {xy.map(({ x, y, point }, index) => <circle key={`${point.at}-${index}`} cx={x} cy={y} r="2.5"><title>{new Date(point.at).toLocaleTimeString()}: {field === 'cpu' ? `${number(point.cpu, 1)}%` : bytes(point.memory)}</title></circle>)}
  </svg>;
}

function Forecast({ forecast }: { forecast: MemoryForecast }) {
  const predicted = forecast.predicted_bytes, headroom = forecast.headroom_bytes, available = forecast.available_bytes;
  const required = finite(predicted) && finite(headroom) ? predicted + headroom : null;
  const blocked = finite(required) && finite(available) && required > available;
  const maximum = Math.max(finite(required) ? required : 0, finite(available) ? available : 0, 1);
  const fidelity = fidelityLabel(forecast.fidelity);
  return <div className={`resource-forecast ${blocked ? 'resource-forecast-blocked' : ''}`}>
    <div className="resource-card-heading"><h3>Next memory admission check</h3><span className="resource-source forecast">Forecast, not measured usage</span></div>
    <p>RCWA order {fidelity}</p>
    <div className="resource-forecast-row"><span>Estimated job + headroom</span><strong>{bytes(predicted)} + {bytes(headroom)}</strong></div>
    <div className="resource-forecast-track" role="img" aria-label={`Estimated job ${bytes(predicted)}, headroom ${bytes(headroom)}, available ${bytes(available)}`}>
      <span className="resource-forecast-job" style={{ width: `${ratio(predicted, maximum)}%` }} />
      <span className="resource-forecast-headroom" style={{ width: `${ratio(headroom, maximum)}%` }} />
      {finite(available) && <i style={{ left: `${ratio(available, maximum)}%` }} />}
    </div>
    <div className="resource-forecast-row"><span>Available now</span><strong>{bytes(available)}</strong></div>
    {blocked && <p className="resource-forecast-result"><Icon name="warning" size={15} /> Insufficient memory forecast · {bytes(required! - available!)} short</p>}
    {!blocked && finite(required) && finite(available) && <p className="resource-forecast-result">Fits current memory forecast · protocol admission still required</p>}
    {finite(forecast.historical_available_bytes) && <div className="resource-forecast-history"><strong>Historical check</strong><p>{bytes(forecast.historical_available_bytes)} was available when the decision was made{forecast.historical_fits === false ? ' · insufficient memory forecast' : ''}.</p>{forecast.historical_reason && <small>{forecast.historical_reason}</small>}</div>}
    {finite(forecast.measured_peak_bytes) && <div className="resource-forecast-anchor"><span className="resource-source">Measured worker peak</span><strong>{bytes(forecast.measured_peak_bytes)} at RCWA {fidelityLabel(forecast.measured_fidelity)}</strong><p>The completed worker's lifetime peak anchors the forecast{finite(forecast.safety_factor) && ` with a ${number(forecast.safety_factor, 1)}× safety factor`}. It is not an isolated solver measurement or a measurement at order {fidelity}.</p>{forecast.measured_at && <small>Measured {displayTime(forecast.measured_at)}</small>}</div>}
    <small>{forecast.basis || 'Forecast basis not recorded.'}{forecast.observed_at && <> · Checked {displayTime(forecast.observed_at)}</>}</small>
  </div>;
}

function Plan({ plan, now }: { plan: ResourcePlan; now: number }) {
  const expired = Number.isFinite(stamp(plan.deadline_at)) && stamp(plan.deadline_at) <= now;
  const terminal = ['completed', 'stopped', 'budget_exhausted', 'failed'].includes(plan.status);
  const closed = plan.ended || terminal || expired;
  const phases = plan.phases || [];
  return <article className="resource-plan" aria-label={`Resource plan ${plan.race_id}`}>
    <div className="resource-card-heading"><div><h3>{label(plan.stage)}</h3><small className="resource-plan-id">{plan.race_id}</small></div><Status status={plan.status} /></div>
    {closed && <div className="resource-blocker"><strong>{expired ? 'Deadline expired' : 'Protocol ended'} · no further work admitted</strong><p>Planned stages below are preserved for review; they are not running or scheduled to launch.</p></div>}
    {plan.blocked_reason && <div className="resource-blocker"><strong>Recorded blocker</strong><p>{plan.blocked_reason}</p></div>}
    <div className="resource-plan-clocks"><div><span>Elapsed window</span><strong>{seconds(plan.elapsed_seconds)} / {seconds(plan.total_seconds)}</strong><small>Deadline {displayTime(plan.deadline_at)}</small></div>
      <div><span>Measured worker time</span><strong>{seconds(plan.worker_seconds_spent)} / {seconds(plan.worker_seconds_cap)}</strong><small>Summed across workers; separate from elapsed time</small></div>
      <div><span>Unfinished planned jobs</span><strong>{number(plan.pending_jobs)}</strong><small>{closed ? 'Not admitted · protocol ended' : 'Admission depends on prerequisites and remaining time'}</small></div></div>
    {plan.memory_forecast && <Forecast forecast={plan.memory_forecast} />}
    {!!plan.memory_checks?.length && <div className="table-scroll"><table className="data-table resource-memory-checks"><caption>RCWA memory planning · dense-matrix size is a lower bound, not total solver usage</caption><thead><tr><th>Order / harmonics</th><th>One complex matrix</th><th>Measured peak</th><th>Forecast + headroom</th><th>Fits memory now</th></tr></thead><tbody>{plan.memory_checks.map((check, index) => <tr key={index}>
      <td><strong>{fidelityLabel(check.fidelity)}</strong><small>{number(check.harmonic_count)} harmonics</small>{check.expanded_grid_shape && <small>Expanded grid {check.expanded_grid_shape.x} × {check.expanded_grid_shape.y} · {bytes(check.expanded_complex_grid_bytes)} per complex array</small>}{finite(check.fft_workspace_lower_bound_bytes) && <small>Known Fourier workspace floor {bytes(check.fft_workspace_lower_bound_bytes)}</small>}</td><td>{bytes(check.single_matrix_bytes)}</td><td>{bytes(check.measured_peak_bytes)}</td><td>{bytes(check.predicted_bytes)} + {bytes(check.headroom_bytes)}<small>{check.basis || 'Estimate basis not recorded'}</small></td><td>{check.fits_now === true ? 'Yes, forecast only' : check.fits_now === false ? 'No' : 'Unknown'}</td>
    </tr>)}</tbody></table></div>}
    {!!phases.length && <div className="table-scroll"><table className="data-table resource-phases"><caption>Planned stages · estimates, not reserved jobs</caption><thead><tr><th>Stage</th><th>State</th><th>Jobs</th><th>Worker time</th><th>Concurrency</th><th>Threads per job</th></tr></thead>
      <tbody>{phases.map((phase, index) => <tr key={`${phase.name}-${index}`}><td><strong>{label(phase.name)}</strong>{phase.estimate_basis && <small>{phase.estimate_basis}</small>}</td><td>{label(phase.state)}</td><td>{number(phase.jobs)}</td><td>{seconds(phase.worker_seconds)}</td><td>{number(phase.concurrency)}</td><td>{number(phase.threads)}</td></tr>)}</tbody></table></div>}
    {!!plan.upcoming_jobs?.length && <details><summary>Planned runs ({plan.upcoming_jobs.length})</summary><p className="resource-footnote">This roster records conditional work. These entries are not queued workers or physical resource reservations.</p><div className="table-scroll"><table className="data-table resource-phases"><thead><tr><th>Algorithm / configuration</th><th>Seed</th><th>Stage / state</th><th>Target time</th><th>Remaining allowance</th></tr></thead><tbody>{plan.upcoming_jobs.map((job, index) => <tr key={index}><td><strong>{label(job.algorithm)}</strong><small>{job.configuration_id}</small></td><td>{job.seed ?? 'Unknown'}</td><td>{label(job.phase)}<small>{label(job.status)}</small></td><td>{seconds(job.target_seconds)}</td><td>{seconds(job.remaining_seconds)}</td></tr>)}</tbody></table></div></details>}
    {!!plan.decisions?.length && <details><summary>Recent allocation decisions ({plan.decisions.length})</summary><ol className="resource-decisions">{[...plan.decisions].reverse().map((decision, index) => <li key={index}><strong>{label(decision.action)}</strong>{decision.created_at && <time>{displayTime(decision.created_at)}</time>}<p>{decision.rationale}</p></li>)}</ol></details>}
  </article>;
}

export function ResourceDashboard({ campaignId }: { campaignId?: string }) {
  const [snapshot, setSnapshot] = useState<ResourceSnapshot | null>(null), [history, setHistory] = useState<HistoryPoint[]>([]);
  const [error, setError] = useState(''), [refreshing, setRefreshing] = useState(false), [now, setNow] = useState(Date.now());
  const refresh = useRef<() => void>(() => {});
  useEffect(() => {
    let active = true, busy = false, controller: AbortController | null = null;
    setSnapshot(null); setHistory([]); setError(''); setRefreshing(false);
    const sample = async () => {
      if (!active || busy) return;
      busy = true; controller = new AbortController(); setRefreshing(true);
      const requestController = controller;
      let timedOut = false;
      const requestTimeout = window.setTimeout(() => { timedOut = true; requestController.abort(); }, 10000);
      try {
        const response = await fetch(`/api/v1/resources${campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : ''}`, { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) {
          let detail = `${response.status} ${response.statusText}`;
          try { const value = await response.json(); detail = typeof value.detail === 'string' ? value.detail : detail; } catch { /* Preserve HTTP error. */ }
          throw new Error(detail);
        }
        const value = await response.json() as ResourceSnapshot;
        if (!active) return;
        setSnapshot(value); setError(''); setNow(Date.now());
        const point = { at: stamp(value.sampled_at), cpu: value.host.cpu.utilization_percent, memory: value.host.memory.used_bytes };
        if (Number.isFinite(point.at)) setHistory(previous => previous.at(-1)?.at === point.at ? previous : [...previous.slice(-59), point]);
      } catch (e) { if (active) setError(timedOut ? 'Resource sampling timed out after 10 seconds' : errorText(e)); }
      finally { window.clearTimeout(requestTimeout); busy = false; if (active) setRefreshing(false); }
    };
    refresh.current = () => void sample();
    void sample();
    const timer = window.setInterval(() => void sample(), 5000);
    return () => { active = false; controller?.abort(); window.clearInterval(timer); refresh.current = () => {}; };
  }, [campaignId]);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const age = snapshot && Number.isFinite(stamp(snapshot.sampled_at)) ? Math.max(0, (now - stamp(snapshot.sampled_at)) / 1000) : null;
  const stale = Boolean(error) || (finite(age) && age > 15);
  const memory = snapshot?.host.memory;
  const swapUsed = memory?.swap_used_bytes ?? (memory && finite(memory.swap_total_bytes) && finite(memory.swap_free_bytes) ? memory.swap_total_bytes - memory.swap_free_bytes : null);
  const budget = snapshot?.budget;
  const reserved = budget && finite(budget.allocated_seconds) && finite(budget.actual_seconds) ? Math.max(0, budget.allocated_seconds - budget.actual_seconds) : null;
  return <div className="resource-dashboard" aria-label="Resource dashboard">
    <header className="resource-heading"><div><span className="eyebrow">COMPUTE OBSERVABILITY</span><h1>Resources</h1><p>Current host usage and the work the campaign plans to admit.</p></div>
      <div className="resource-refresh"><span role="status" aria-live="off" className={stale ? 'resource-stale' : ''}>{snapshot ? <>{stale ? 'Stale sample' : 'Measured sample'} · {seconds(age)} old</> : refreshing ? 'Loading resource measurements…' : 'No resource sample'}</span><button className="button small secondary" disabled={refreshing} onClick={() => refresh.current()}><Icon name="refresh" size={14} />{error ? 'Retry resource status' : 'Refresh resources'}</button></div></header>
    <ErrorNotice text={error ? `Resource status could not update: ${error}. ${snapshot ? 'The last successful sample remains visible.' : 'Retry to load measurements.'}` : ''} />
    {snapshot && <>
      {!!snapshot.warnings?.length && <ul className="resource-warnings">{snapshot.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul>}
      {snapshot.workers.running_count === 0 && snapshot.plans?.some(plan => plan.ended || ['stopped', 'completed', 'budget_exhausted', 'failed'].includes(plan.status) || stamp(plan.deadline_at) <= now) && <div className="resource-idle-notice" role="status"><strong>Campaign idle · protocol ended</strong><p>No numerical jobs are running. The elapsed deadline and unfinished resource plans are shown below; they do not constitute queued work.</p><a href="#resources" onClick={event => { event.preventDefault(); document.getElementById('resource-plans')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }}>See the admission blocker and planned stages</a></div>}
      <section className={`resource-host-grid ${snapshot.host.gpu?.devices?.length ? 'has-gpu' : ''}`} aria-label="Current host usage">
        <article className="resource-card"><div className="resource-card-heading"><h2>CPU</h2><span className="resource-source">Measured</span></div><strong className="resource-value">{finite(snapshot.host.cpu.utilization_percent) ? `${number(snapshot.host.cpu.utilization_percent, 1)}%` : 'Awaiting CPU sample'}</strong>
          <p>{number(snapshot.host.cpu.capacity_cores, 1)} cores available · {number(snapshot.host.cpu.logical_count)} logical CPUs</p><HistoryChart points={history} field="cpu" maximum={100} title="Host CPU utilization history" /><small>Load average (1 / 5 / 15 min): {snapshot.host.cpu.load_average?.map(value => value.toFixed(2)).join(' / ') || 'Unknown'}</small></article>
        <article className="resource-card"><div className="resource-card-heading"><h2>Memory</h2><span className="resource-source">Measured</span></div><strong className="resource-value">{bytes(snapshot.host.memory.used_bytes)} <span>/ {bytes(snapshot.host.memory.total_bytes)}</span></strong>
          <p>{bytes(snapshot.host.memory.effective_available_bytes ?? snapshot.host.memory.available_bytes)} available to new work</p><HistoryChart points={history} field="memory" maximum={snapshot.host.memory.total_bytes} title="Host memory usage history" /><small>Available memory includes reclaimable cache and respects a container limit when present. This is host usage, not just campaign jobs.</small><>{finite(snapshot.host.memory.cgroup_limit_bytes) && <small>Container memory limit {bytes(snapshot.host.memory.cgroup_limit_bytes)} · host available {bytes(snapshot.host.memory.available_bytes)}</small>}</><p className="resource-swap">Swap: {bytes(swapUsed)} used / {bytes(snapshot.host.memory.swap_total_bytes)} total</p></article>
        <article className="resource-card resource-worker-card"><div className="resource-card-heading"><h2>{snapshot.campaign_id ? 'Campaign workers' : 'Workspace workers'}</h2><span className="resource-source">Current</span></div><strong className="resource-value">{number(snapshot.workers.running_count)} <span>/ {number(snapshot.workers.configured_limit)} configured</span></strong>
          <p>{number(snapshot.workers.queued_count)} queued jobs</p><div className="resource-capacity-meter" role="img" aria-label={`${number(snapshot.workers.running_count)} running workers of ${number(snapshot.workers.configured_limit)} configured`}><i style={{ width: `${ratio(snapshot.workers.running_count, snapshot.workers.configured_limit)}%` }} /></div>
          <small>{snapshot.workers.running_count === 0 ? `No numerical jobs running ${snapshot.campaign_id ? 'for this campaign' : 'in this workspace'}. Planned work is shown below.` : 'Process measurements are listed below.'}</small><small>The configured worker limit is shared across campaigns.</small><p className="resource-gpu">GPU: {label(snapshot.host.gpu?.status || 'not sampled')}{Boolean(snapshot.host.gpu?.devices?.length) && ` · ${snapshot.host.gpu!.devices!.length} device(s)`}</p></article>
        {!!snapshot.host.gpu?.devices?.length && <article className="resource-card resource-gpu-card"><div className="resource-card-heading"><h2>GPU memory and activity</h2><span className="resource-source">Driver measured</span></div>{snapshot.host.gpu.devices.map((device, index) => <div className="resource-gpu-device" key={device.id || index}><strong>{device.vendor || 'GPU'} {device.id}</strong><p className="resource-value">{bytes(device.memory_used_bytes)} <span>/ {bytes(device.memory_total_bytes)} driver memory</span></p><p>{finite(device.utilization_percent) ? `${number(device.utilization_percent, 1)}% utilization` : 'Utilization unknown'}</p><div className="resource-capacity-meter" role="img" aria-label={`${device.vendor || 'GPU'} driver memory ${bytes(device.memory_used_bytes)} of ${bytes(device.memory_total_bytes)}`}><i style={{ width: `${ratio(device.memory_used_bytes, device.memory_total_bytes)}%` }} /></div><small>{device.memory_kind || 'Driver memory may exclude shared host allocations.'}</small></div>)}<small>GPU driver pool is reported separately from host RAM. Shared allocations can overlap; pool totals are not combined.</small></article>}
      </section>
      <p className="resource-history-note">Auto-refresh every 5 seconds · graphs retain the last 60 samples while this page is open · sampled {when(snapshot.sampled_at)}.</p>
      {!!snapshot.host.processes?.length && <section className="resource-card resource-processes" aria-label="Largest host memory consumers"><div className="resource-card-heading"><h2>Largest host memory consumers</h2><span className="resource-source">Measured RSS</span></div><p>Includes services and other host processes, even when numerical workers are idle.</p><div className="table-scroll"><table className="data-table"><thead><tr><th>Process</th><th>Role</th><th>PID</th><th>Memory RSS</th><th>Threads</th></tr></thead><tbody>{snapshot.host.processes.map(process => <tr key={process.pid}><td>{process.name}</td><td>{label(process.role)}</td><td>{process.pid}</td><td>{bytes(process.rss_bytes)}</td><td>{number(process.threads)}</td></tr>)}</tbody></table></div></section>}
      <section className="resource-card resource-budget" aria-label="Campaign resource allowance"><div className="resource-card-heading"><h2>Campaign worker time</h2><span className="resource-source">Accounted</span></div>
        {budget ? <><div className="resource-budget-grid"><div><span>Spent</span><strong>{seconds(budget.actual_seconds)}</strong></div><div><span>Unspent allocated time</span><strong>{seconds(reserved)}</strong></div><div><span>Free allocation</span><strong>{seconds(budget.remaining_seconds)}</strong></div><div><span>Authorized ceiling</span><strong>{seconds(budget.limit_seconds)}</strong></div></div>
          <div className="resource-budget-meter" role="img" aria-label={`Spent ${seconds(budget.actual_seconds)}, unspent allocated ${seconds(reserved)}, authorized ${seconds(budget.limit_seconds)}`}><i style={{ width: `${ratio(budget.actual_seconds, budget.limit_seconds)}%` }} /><i style={{ width: `${ratio(reserved, budget.limit_seconds)}%` }} /></div><p>Worker time sums execution across processes. Allocated time is authorization for jobs; it does not reserve physical RAM. Each protocol also has its own elapsed deadline.</p></> : <p>No campaign selected. Host measurements remain available; choose a campaign to see its allowance and resource plans.</p>}
      </section>
      <section className="resource-card resource-jobs" aria-label="Current jobs"><div className="resource-card-heading"><h2>Current jobs</h2><span className="resource-source">Measured process usage</span></div>
        {!snapshot.workers.jobs?.length ? <p>No running or queued numerical jobs.</p> : <div className="table-scroll"><table className="data-table"><thead><tr><th>Job</th><th>State / PID</th><th>RSS / peak RSS</th><th>CPU</th><th>OS threads</th><th>Spent / remaining</th></tr></thead><tbody>{snapshot.workers.jobs.map(job => <tr key={job.id}>
          <td><strong>{label(job.algorithm || job.kind)}{typeof job.seed === 'number' && ` · seed ${job.seed}`}</strong><a href={job.kind === 'fixed_mask_job' ? '#validation' : `#experiments/${encodeURIComponent(job.id)}`}>{job.id}</a></td><td>{label(job.status)}<small>{label(job.process_state)}{job.pid ? ` · PID ${job.pid}` : ' · no process'}</small></td><td>{bytes(job.rss_bytes)} / {bytes(job.peak_rss_bytes)}</td><td>{finite(job.cpu_percent) ? `${number(job.cpu_percent, 1)}%` : 'Unknown'}</td><td>{number(job.threads)}</td><td>{seconds(job.spent_seconds)} / {seconds(job.remaining_seconds)}<small>{seconds(job.wall_seconds)} allowance</small></td>
        </tr>)}</tbody></table></div>}<p className="resource-footnote">Process CPU may exceed 100% when it uses multiple cores. RSS and CPU cover the verified owner process; sandbox descendants are excluded. Unknown measurements are not zero usage.</p>
      </section>
      <section id="resource-plans" className="resource-plans" aria-label="Planned resource usage"><div className="resource-card-heading"><h2>Planned resource usage</h2><span className="resource-source forecast">Estimates and admission state</span></div>
        {!snapshot.plans?.length ? <p className="resource-card">No adaptive resource plan recorded for this campaign.</p> : snapshot.plans.map(plan => <Plan key={plan.race_id} plan={plan} now={now} />)}
      </section>
    </>}
  </div>;
}
