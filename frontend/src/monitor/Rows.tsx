import type { Monitor } from '../api/types';
import { Kv, Pill, duration, num } from '../primitives/ui';
import { Meter, Ring, Slots } from '../primitives/charts';

export function Rows({ monitor }: { monitor: Monitor }) {
  const { problem, budget, compute, agents, needs_you } = monitor.rows;
  const worker = (budget.worker || {}) as Record<string, unknown>;
  const llm = budget.llm as Record<string, unknown>;
  const autopilot = (monitor.rows as unknown as { autopilot?: Record<string, unknown> }).autopilot;
  const gpuRow = compute.gpu as Record<string, unknown> | undefined;
  const pools = compute.pools as Record<string, { slots: number; taken: number }> | undefined;
  const slots = pools && pools.cuda?.slots ? `GPU ${pools.cuda.taken} of ${pools.cuda.slots} · CPU ${pools.cpu.taken} of ${pools.cpu.slots}` : null;
  const waiting = Number(needs_you.decisions) + Number(needs_you.issues) + Number(needs_you.questions);
  return (
    <div className="col" data-testid="monitor-rows">
      <div className="card"><h3>Problem</h3><Kv rows={[['Campaign', String(problem.name)], ['Tasks', String(problem.tasks)]]} /></div>
      <div className="card"><h3>Budget</h3>
        {!worker.unavailable ? <div className="meter-block">
          <div className="row"><span>Worker time</span><span className="faint">{duration(worker.budget_seconds)} budget</span></div>
          <Meter total={Number(worker.budget_seconds)} label="worker time"
            segments={[{ value: Number(worker.allocated_seconds), tone: 'accent-soft', title: 'allocated' }, { value: Number(worker.actual_seconds), tone: 'accent', title: 'used (measured)' }]}
            markers={worker.forecast_seconds != null ? [{ value: Number(worker.forecast_seconds), tone: 'warn', title: 'forecast' }] : []} />
        </div> : null}
        <Kv rows={[
          ['Worker time', worker.unavailable ? <span className="faint">{String(worker.unavailable)}</span> : <>{duration(worker.actual_seconds)} used, {duration(worker.allocated_seconds)} allocated of {duration(worker.budget_seconds)} (measured)</>],
          ['Forecast', worker.forecast_seconds != null ? <>{duration(worker.forecast_seconds)} <span className="faint">forecast: {String(worker.forecast_basis)}</span></> : <span className="faint">unknown</span>],
        ]} />
        <div className="meter-block">
          <div className="row"><span>Model spend</span><span className="faint">{llm.cap_usd != null ? <>{num(llm.cap_usd, 2, ' $')} cap</> : 'no cap set'}</span></div>
          <Meter total={llm.cap_usd != null ? Number(llm.cap_usd) : null} label="model spend"
            segments={[{ value: Number(llm.agent_charged_usd) + Number(llm.implementation_committed_usd || 0), tone: 'info-soft', title: 'charged plus committed' }, { value: Number(llm.agent_charged_usd), tone: 'info', title: 'charged to agents' }]} />
        </div>
        <Kv rows={[['Model spend', <>{num(llm.agent_charged_usd, 2, ' $')} charged{llm.cap_usd != null ? <> of {num(llm.cap_usd, 2, ' $')}</> : null}{Number(llm.implementation_committed_usd) ? <>, {num(llm.implementation_committed_usd, 2, ' $')} committed to implementations</> : null}</>]]} />
      </div>
      <div className="card"><h3>Compute</h3>
        <div className="row wrap" style={{ gap: 16, alignItems: 'center', marginBottom: 8 }}>
          <div className="col" style={{ gap: 4 }}><span className="small faint">Workers</span><Slots busy={Number(compute.busy)} limit={Number(compute.limit)} queued={Number(compute.queued ?? 0)} /></div>
          {gpuRow?.measured ? <div className="row" style={{ gap: 8 }}><Ring percent={Number(gpuRow.utilization_percent)} size={56} label="GPU utilization" tone={gpuRow.busy ? 'accent' : 'info'} /><Ring percent={Number(gpuRow.memory_used_bytes) / Number(gpuRow.memory_total_bytes) * 100} size={56} label="GPU memory" tone="info" text={`${(Number(gpuRow.memory_used_bytes) / 2 ** 30).toFixed(0)}G`} /></div> : null}
        </div>
        <Kv rows={[['Runs now', `${compute.busy} of ${compute.limit} workers`], ...(slots ? [['Slots taken', slots] as [string, string]] : []), ['Queued', compute.queued != null ? String(compute.queued) : <span className="faint">admission not reported</span>], ['Level', compute.level ? String(compute.level) : <span className="faint">unknown</span>],
          ['GPU', gpu(gpuRow)]]} />
      </div>
      <div className="card"><h3>Agents</h3><div className="row"><Pill tone={agents.active ? 'agent' : ''}>{String(agents.status)}</Pill><span className="small">{String(agents.headline || '')}</span></div></div>
      <div className="card"><h3>Autopilot</h3>{autopilot?.enabled ? <>
        <Meter total={Number((autopilot.settings as Record<string, unknown>)?.compute_cap_seconds)} segments={[{ value: Number(autopilot.spent_seconds), tone: 'you', title: 'spent of the cap' }]} label="autopilot cap" />
        <Kv rows={[['State', String(autopilot.reason)], ['Spent of cap', `${duration(autopilot.spent_seconds)} of ${duration((autopilot.settings as Record<string, unknown>)?.compute_cap_seconds)}`], ['Rounds', String(autopilot.rounds_completed)]]} /></> : <p className="small muted">Off: the lead acts on your requests.</p>}</div>
      <div className="card"><h3>Needs you</h3>
        <div className="stat-grid">
          {([['Decisions', needs_you.decisions], ['Issues', needs_you.issues], ['Questions', needs_you.questions]] as [string, unknown][]).map(([name, value]) =>
            <div key={name} className="stat"><span className="big" style={{ color: Number(value) ? 'var(--warn)' : 'var(--ink-3)' }}>{String(value)}</span><span className="small">{name}</span></div>)}
        </div>
        {!waiting ? <p className="small faint" style={{ marginTop: 6 }}>Nothing waits for you.</p> : null}
      </div>
    </div>
  );
}

function gpu(row: Record<string, unknown> | undefined) {
  if (!row || !row.reported) return <span className="faint">{String(row?.basis || 'not reported')}</span>;
  if (!row.measured) return <span className="faint">{String(row.devices)} device(s), {String(row.basis)}</span>;
  return <>{row.busy ? 'busy' : 'idle'} · {String(row.utilization_percent)} % · {(Number(row.memory_used_bytes) / 2 ** 30).toFixed(1)} of {(Number(row.memory_total_bytes) / 2 ** 30).toFixed(1)} GiB <span className="faint">({String(row.basis)})</span></>;
}
