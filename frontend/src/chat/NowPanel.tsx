/** What is happening now: which agent is at work and on what, what the others last said, the runs and why queued ones wait. */
import { useExperiments, useMonitor, useRead, useThreads } from '../api/queries';
import type { Trial } from '../api/types';
import { Pill, duration, statusTone, when } from '../primitives/ui';
import { formatHash, type Route } from '../router/routes';

export type Agent = { id: string; role: string; status: string; activity?: string; last_activity_at?: string; updated_at?: string;
  disposition?: { disposition?: string; summary?: string } | null };

const ROLES: Record<string, string> = { lead: 'Lead', experiment_operator: 'Experiment operator', resource_planner: 'Resource planner',
  implementation_lead: 'Implementation lead', discussion_moderator: 'Discussion moderator', proposal_reviewer: 'Proposal reviewer',
  skeptical_domain_analyst: 'Skeptical analyst' };
const STATUS_WORDS: Record<string, string> = { running: 'working', queued: 'turn queued', submitted: 'starting a turn', waiting: 'waiting', completed: 'done' };
const STATUS_HELP: Record<string, string> = {
  running: 'In a turn now: thinking or calling tools.',
  queued: 'A turn is waiting to start: a message or a notice came while its previous turn was still running.',
  submitted: 'A turn was handed to the harness and is starting.',
  waiting: 'Between turns: waiting for a reply, a run result or your next request.',
  completed: 'Finished the work it was given.' };
const LIVE = new Set(['queued', 'running', 'pausing', 'stopping']);

export const roleName = (role: string) => ROLES[role] || role.replaceAll('_', ' ');
const atWork = (agent: Agent) => ['running', 'queued', 'submitted'].includes(agent.status);

export function useAgents(campaignId: string) {
  return useRead<{ agents: Agent[] }>(['agents', campaignId], `/api/v1/campaigns/${campaignId}/agents`);
}

/** The lead's pill: its state, and who else is at work and how many runs go, so a waiting lead says what it waits on. */
export function nowLabel(lead: { status: string } | null | undefined, busy: boolean, agents: Agent[] | undefined, running: number) {
  if (!lead) return 'No lead yet';
  const others = (agents || []).filter(agent => agent.role !== 'lead' && atWork(agent));
  const parts = [busy ? 'Lead working' : lead.status === 'queued' ? 'Lead: turn queued' : `Lead ${STATUS_WORDS[lead.status] || lead.status}`];
  if (others.length) parts.push(others.length === 1 ? `${roleName(others[0].role).toLowerCase()} working` : `${others.length} agents working`);
  if (running) parts.push(`${running} run${running === 1 ? '' : 's'}`);
  return parts.join(' · ');
}

function runName(trial: Trial) {
  const first = String(trial.question || '').split('\n')[0].trim();
  return first && first.length <= 90 ? first : `${trial.algorithm} · seed ${trial.seed ?? '?'}`;
}

export function NowPanel({ campaignId, threadId, route }: { campaignId: string; threadId: string; route: Route }) {
  const agents = useAgents(campaignId);
  const experiments = useExperiments(campaignId);
  const monitor = useMonitor(campaignId);
  const threads = useThreads(campaignId);
  const team = agents.data?.agents || [];
  const lead = team.find(agent => agent.role === 'lead');
  // The lead, anyone at work, and anyone who did something in the last ten minutes.
  const recent = (agent: Agent) => Date.now() - Date.parse(agent.last_activity_at || agent.updated_at || '') < 600_000;
  const shown = team.filter(agent => agent.role === 'lead' || atWork(agent) || recent(agent))
    .sort((a, b) => Number(b.role === 'lead') - Number(a.role === 'lead') || Number(atWork(b)) - Number(atWork(a)));
  const data = experiments.data;
  const runs = data ? [...data.long, ...data.short, ...data.checks] : [];
  const live = runs.filter(trial => LIVE.has(trial.status)).sort((a, b) => Number(b.status !== 'queued') - Number(a.status !== 'queued'));
  const names = Object.fromEntries(runs.map(trial => [trial.id, runName(trial)]));
  const compute = monitor.data?.rows.compute as { waiting?: { trial_id: string; reason: string }[]; pools?: Record<string, { slots: number; taken: number }> } | undefined;
  const waits = Object.fromEntries((compute?.waiting || []).map(row => [row.trial_id, row.reason]));
  const pools = compute?.pools;
  const profiles = (data?.profiles || []).filter(profile => profile.status === 'running') as { id: string; levels: { device?: string; workers: number }[]; plan?: unknown[] }[];
  const side = (threads.data?.threads || []).filter(thread => thread.kind === 'agents' && thread.id !== threadId)
    .sort((a, b) => String(b.last_activity_at || '').localeCompare(String(a.last_activity_at || '')))[0];
  const why = (trial: Trial) => {
    if (trial.status !== 'queued') return null;
    if (waits[trial.id]) return waits[trial.id];
    const before = ((trial.dependencies as string[] | undefined) || []).filter(id => runs.some(row => row.id === id && LIVE.has(row.status)));
    if (before.length) return `After ${before.map(id => names[id] || id).join(', ')}`;
    if (profiles.length) return 'Held while the runtime profile measures';
    return 'Waiting for the scheduler';
  };
  return (
    <div className="now-panel" data-testid="now-panel">
      <div className="now-section">
        {shown.map(agent => {
          const said = agent.disposition?.summary;
          const doing = atWork(agent) && agent.activity && !agent.activity.startsWith('run.') ? agent.activity : null;
          return <div key={agent.id} className="now-row" data-testid="now-agent">
            <Pill tone={atWork(agent) ? 'agent' : agent.status === 'completed' ? 'clear' : ''} title={STATUS_HELP[agent.status] || agent.status}>{roleName(agent.role)} · {STATUS_WORDS[agent.status] || agent.status}</Pill>
            <span className="grow small" title={said || undefined}>{doing || (said ? `Last said: ${said}` : <span className="faint">nothing reported yet</span>)}</span>
            <span className="faint small" title="Last activity">{when(agent.last_activity_at || agent.updated_at)}</span>
          </div>;
        })}
        {!agents.data ? <div className="faint small">Loading the agents…</div> : !lead ? <div className="faint small">No agent team yet.</div> : null}
      </div>
      <div className="now-section">
        <div className="small"><strong>Runs:</strong> {!data ? <span className="faint">loading…</span> : <>{live.filter(t => t.status !== 'queued').length} going, {live.filter(t => t.status === 'queued').length} queued</>}
          {pools ? <span className="faint"> · GPU {pools.cuda.taken} of {pools.cuda.slots} slots, CPU {pools.cpu.taken} of {pools.cpu.slots}</span> : null}</div>
        {profiles.map(profile => {
          const level = profile.levels.at(-1);
          return <div key={profile.id} className="now-row small" data-testid="now-profile"><Pill tone="attention">profile</Pill>
            <span>Runtime profile measuring {level?.device === 'cuda' ? 'the GPU' : level?.device === 'cpu' ? 'CPU' : ''} with {level?.workers ?? '?'} worker{level?.workers === 1 ? '' : 's'}
              {profile.plan ? ` (level ${profile.levels.length} of ${profile.plan.length})` : ''}: nothing else starts until it finishes.</span></div>;
        })}
        {live.slice(0, 12).map(trial => {
          const progress = (trial.progress || {}) as Record<string, unknown>;
          const elapsed = Number(progress.elapsed_seconds), left = Number(trial.wall_seconds) - elapsed;
          const reason = why(trial);
          return <div key={trial.id} className="now-row small" data-testid="now-run">
            <Pill tone={statusTone(trial.status)}>{trial.status}</Pill>
            <a href={formatHash({ page: 'experiments', id: trial.id }, threadId)} className="grow" title={trial.id}>{names[trial.id]}</a>
            {trial.device ? <span className="faint">{trial.device === 'cuda' ? 'GPU' : 'CPU'}</span> : null}
            {reason ? <span className="faint" title={reason}>{reason.length > 80 ? reason.slice(0, 79) + '…' : reason}</span>
              : <span className="faint">{Number(progress.evaluations) || 0} evaluations{Number.isFinite(left) ? <>, {duration(Math.max(0, left))} left</> : null}</span>}
          </div>;
        })}
        {live.length > 12 ? <div className="faint small">and {live.length - 12} more on <a href={formatHash({ page: 'experiments' }, threadId)}>Experiments</a></div> : null}
      </div>
      {side ? <div className="now-section small">Agents' conversation: <a href={formatHash({ page: route.page, sub: route.sub, id: route.id }, side.id)}>{side.title}</a>{side.busy ? ' (active now)' : ''}</div> : null}
    </div>
  );
}
