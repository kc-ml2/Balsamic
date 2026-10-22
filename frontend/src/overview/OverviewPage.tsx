/** The campaign at a glance: what it is for, where it stands, what it has, and the one place to remove it. */
import type { ReactNode } from 'react';
import type { PageProps } from '../shell/StagePanel';
import { useMonitor, useSummary, useThreads, useCampaigns } from '../api/queries';
import { Empty, Failed, Kv, Loading, Pill, Section, duration, num, when } from '../primitives/ui';
import { Meter } from '../primitives/charts';
import { formatHash } from '../router/routes';
import { CampaignRemoval } from '../setup/CampaignRemoval';

export default function OverviewPage({ campaignId, route }: PageProps) {
  const summary = useSummary(campaignId);
  const monitor = useMonitor(campaignId);
  const threads = useThreads(campaignId);
  const campaigns = useCampaigns();
  const row = campaigns.data?.find(c => c.id === campaignId);
  if (summary.isPending) return <Loading what="Reading the campaign" />;
  if (summary.error) return <Failed error={summary.error} />;
  const { campaign, stage, counts, charter, tasks } = summary.data;
  const rows = monitor.data?.rows;
  const worker = (rows?.budget.worker || {}) as Record<string, unknown>;
  const llm = (rows?.budget.llm || {}) as Record<string, unknown>;
  const autopilot = (rows as unknown as { autopilot?: Record<string, unknown> } | undefined)?.autopilot;
  const objective = String(campaign.objective || campaign.description || '');
  const stats: [string, unknown, string][] = [['Proposals', counts.hypothesis, 'hypotheses'], ['Runs', counts.trial, 'experiments'], ['Races', counts.adaptive_race, 'evolution/races'], ['Decisions', counts.decision, 'inbox'], ['Threads', counts.thread, 'branches']];
  return (
    <div className="col" data-testid="overview">
      <div className="stage-head"><h1>{String(campaign.name)}</h1><Pill tone={stage.pinned ? 'you' : ''} title={stage.basis}>{stage.stage}{stage.pinned ? ' (pinned)' : ''}</Pill></div>
      {objective ? <p className="lede">{objective}</p> : null}
      <Section title="Where it stands">
        <Kv rows={[['Stage', <>{stage.stage} <span className="faint">{stage.basis}</span></>],
          ['Charter', charter ? <>{String(charter.status || 'drafted')} · revision {String(charter.revision ?? charter.version ?? '')} <a href={formatHash({ page: 'note', sub: 'charter' }, route.thread)}>open</a></> : <span className="faint">none yet; the problem note on <a href={formatHash({ page: 'note' }, route.thread)}>Problem note</a> drafts it</span>],
          ['Tasks', tasks.length ? tasks.map(t => String(t.id)).join(', ') : <span className="faint">none yet</span>],
          ['Delegation', String(campaign.autonomy || '')], ['Created', when(campaign.created_at)]]} />
      </Section>
      <Section title="What it has">
        <div className="stat-grid">
          {stats.map(([name, value, page]) => <a key={name} className="stat" href={formatHash({ page: page.split('/')[0] as 'monitor', sub: page.split('/')[1] || null }, route.thread)}><span className="big">{String(value ?? 0)}</span><span className="small">{name}</span></a>)}
        </div>
      </Section>
      <Section title="Budgets">
        {rows ? <>
          {!worker.unavailable ? <div className="meter-block"><div className="row"><span>Worker time</span><span className="faint">{duration(worker.actual_seconds)} used of {duration(worker.budget_seconds)}</span></div>
            <Meter total={Number(worker.budget_seconds)} segments={[{ value: Number(worker.allocated_seconds), tone: 'accent-soft', title: 'allocated' }, { value: Number(worker.actual_seconds), tone: 'accent', title: 'used' }]} markers={worker.forecast_seconds != null ? [{ value: Number(worker.forecast_seconds), tone: 'warn', title: 'forecast' }] : []} label="worker time" /></div> : <p className="small faint">{String(worker.unavailable)}</p>}
          <div className="meter-block"><div className="row"><span>Model spend</span><span className="faint">{num(llm.agent_charged_usd, 2, ' $')}{llm.cap_usd != null ? <> of {num(llm.cap_usd, 2, ' $')}</> : null}</span></div>
            <Meter total={llm.cap_usd != null ? Number(llm.cap_usd) : null} segments={[{ value: Number(llm.agent_charged_usd), tone: 'info', title: 'charged' }]} label="model spend" /></div>
        </> : <Loading />}
      </Section>
      <Section title="Team and autopilot">
        <Kv rows={[['Agents', rows ? <><Pill tone={rows.agents.active ? 'agent' : ''}>{String(rows.agents.status)}</Pill> <span className="small">{String(rows.agents.headline || '')}</span></> : <Loading />],
          ['Problem definition', row?.next_step && row.next_step.kind !== 'evaluator' ? <><Pill tone="warn">gate closed</Pill> <span className="small">{row.next_step.title}{row.next_step.missing?.length ? `: ${row.next_step.missing.join(', ')} not written` : ''}</span></> : <Pill tone="good">defined</Pill>],
          ...(row?.next_step?.kind === 'evaluator' ? [['Evaluator', <><Pill tone="bad">cannot start</Pill> <span className="small" data-testid="evaluator-blocked">{row.next_step.error}</span></>] as [string, ReactNode]] : []),
          ['Autopilot', autopilot?.enabled ? `on · ${String(autopilot.reason)}` : row?.autopilot?.requested ? 'chosen at setup; waits for the problem definition' : 'off: the lead acts on your requests'],
          ['Threads', threads.data ? threads.data.threads.filter(t => t.kind !== 'agents').map(t => <a key={t.id} href={formatHash({ page: 'overview' }, t.id)} style={{ marginRight: 8 }}>{t.title}</a>) : <Loading />]]} />
      </Section>
      {!counts.hypothesis && !counts.trial ? <Empty>Nothing has been proposed or run yet. Ask the lead in the chat.</Empty> : null}
      <CampaignRemoval campaignId={campaignId} />
    </div>
  );
}
