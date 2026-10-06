import { useEffect, useState } from 'react';
import { errorText, when } from './api';
import { Badge, Empty, ErrorNotice, Icon } from './ui';
import './llmUsage.css';

type Totals = { calls: number; input: number; output: number; cache_read: number; cache_write: number; reasoning: number;
  total_tokens: number; cost_usd: number | null; priced_calls: number; charged_usd: number; first_at: string | null; last_at: string | null };
type Bucketed = Totals & { bucket: string };
type Usage = {
  totals: Totals; series: Bucketed[]; family: string | null; bucket: 'hour' | 'day';
  by_model: (Totals & { family: string; provider: string; model: string; billing: string })[];
  by_agent: (Totals & { agent_id: string; role: string })[];
  by_thinking: (Totals & { provider: string; model: string; thinking_level: string | null })[];
  budget: { cap_usd: number | null; agent_charged_usd: number; implementation_committed_usd: number; spent_usd: number };
  agents: Record<string, { role: string; status: string; provider: string; model: string; effort: string | null }>;
};

// The app's teal, amber and blue, re-stepped to pass the categorical checks on #fafaf7
// (chroma >= 0.10, CVD ΔE >= 8 between stacked neighbours, >= 3:1 against the surface).
const SERIES = [
  { key: 'input', label: 'Input', color: '#008a74' },
  { key: 'output', label: 'Output', color: '#bb6522' },
  { key: 'cache_read', label: 'Cache read', color: '#5468c4' },
] as const;
const RANGES = [
  { id: '24h', label: 'Last 24 hours', ms: 86400e3, bucket: 'hour' },
  { id: '7d', label: 'Last 7 days', ms: 7 * 86400e3, bucket: 'day' },
  { id: 'all', label: 'All time', ms: 0, bucket: 'day' },
] as const;

const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
const tokens = (value: number) => compact.format(value || 0);
const usd = (value: number | null | undefined) => value === null || value === undefined ? '—'
  : value === 0 ? '$0' : value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
const roleLabel = (role: string) => role.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase());
const cacheRate = (row: Totals) => {
  const prompt = row.input + row.cache_read + row.cache_write;
  return prompt ? `${Math.round(row.cache_read / prompt * 100)}%` : '—';
};
const bucketLabel = (bucket: string, size: 'hour' | 'day') => {
  const date = new Date(bucket);
  return size === 'hour' ? date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};

/** Bar with a 4px rounded data end and a square base, anchored at `base`. */
function barPath(x: number, top: number, width: number, base: number, rounded: boolean) {
  const r = rounded ? Math.min(4, width / 2, base - top) : 0;
  return `M${x},${base}V${top + r}${r ? `Q${x},${top} ${x + r},${top}` : ''}H${x + width - r}${r ? `Q${x + width},${top} ${x + width},${top + r}` : ''}V${base}Z`;
}

function niceMax(value: number) {
  if (value <= 0) return 1;
  const step = 10 ** Math.floor(Math.log10(value));
  return [1, 2, 2.5, 5, 10].map(m => m * step).find(candidate => candidate >= value) || value;
}

type Segment = { key: string; label: string; color: string; value: number };
function BarChart({ title, rows, size, segments, format }: { title: string; rows: Bucketed[]; size: 'hour' | 'day';
    segments: (row: Bucketed) => Segment[]; format: (value: number) => string }) {
  const [hover, setHover] = useState<number | null>(null), [table, setTable] = useState(false);
  const width = 820, height = 220, margin = { top: 12, right: 12, bottom: 28, left: 56 };
  const plotW = width - margin.left - margin.right, plotH = height - margin.top - margin.bottom, base = margin.top + plotH;
  const totals = rows.map(row => segments(row).reduce((sum, s) => sum + s.value, 0));
  const max = niceMax(Math.max(0, ...totals));
  const step = plotW / Math.max(1, rows.length), barW = Math.max(2, Math.min(28, step * 0.7));
  const y = (value: number) => base - value / max * plotH;
  const labelEvery = Math.max(1, Math.ceil(rows.length / 8));
  const legend = segments(rows[0]).length > 1;
  return <section className="llm-chart">
    <div className="llm-chart-heading"><h3>{title}</h3>
      {legend && <ul className="llm-legend">{segments(rows[0]).map(s => <li key={s.key}><span style={{ background: s.color }} />{s.label}</li>)}</ul>}
      <button className="button small secondary" onClick={() => setTable(!table)}>{table ? 'Show chart' : 'Show table'}</button></div>
    {table ? <table className="llm-table"><thead><tr><th>{size === 'hour' ? 'Hour' : 'Day'}</th>
        {segments(rows[0]).map(s => <th key={s.key} className="numeric">{s.label}</th>)}<th className="numeric">Total</th></tr></thead>
      <tbody>{rows.map((row, i) => <tr key={row.bucket}><td>{bucketLabel(row.bucket, size)}</td>
        {segments(row).map(s => <td key={s.key} className="numeric">{format(s.value)}</td>)}<td className="numeric">{format(totals[i])}</td></tr>)}</tbody></table>
    : <div className="llm-chart-frame" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}, ${rows.length} ${size === 'hour' ? 'hours' : 'days'}`}>
        {[0, 0.5, 1].map(f => <g key={f}><line className="llm-grid" x1={margin.left} x2={width - margin.right} y1={y(max * f)} y2={y(max * f)} />
          <text className="llm-axis" x={margin.left - 8} y={y(max * f) + 4} textAnchor="end">{format(max * f)}</text></g>)}
        {rows.map((row, i) => {
          const x = margin.left + i * step + (step - barW) / 2;
          let top = base;
          const parts = segments(row).filter(s => s.value > 0);
          return <g key={row.bucket} className={hover !== null && hover !== i ? 'llm-dim' : ''}>
            {parts.map((s, j) => {
              const next = top - s.value / max * plotH;
              // 2px surface gap between stacked segments.
              const segmentBase = j === 0 ? top : top - 1, segmentTop = j === parts.length - 1 ? next : next + 1;
              const path = segmentBase - segmentTop >= 0.5 ? <path key={s.key} d={barPath(x, segmentTop, barW, segmentBase, j === parts.length - 1)} fill={s.color} /> : null;
              top = next;
              return path;
            })}
            {i % labelEvery === 0 && <text className="llm-axis" x={x + barW / 2} y={height - 8} textAnchor="middle">{bucketLabel(row.bucket, size)}</text>}
            <rect className="llm-hit" x={margin.left + i * step} y={margin.top} width={step} height={plotH}
              onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} tabIndex={0} aria-label={`${bucketLabel(row.bucket, size)}: ${format(totals[i])}`} />
          </g>;
        })}
        <line className="llm-baseline" x1={margin.left} x2={width - margin.right} y1={base} y2={base} />
      </svg>
      {hover !== null && <div className="llm-tooltip" style={{ left: `${(margin.left + (hover + 0.5) * step) / width * 100}%` }}>
        <strong>{bucketLabel(rows[hover].bucket, size)}</strong>
        {segments(rows[hover]).map(s => <div key={s.key}>{legend && <span style={{ background: s.color }} />}{s.label}<b>{format(s.value)}</b></div>)}
        {legend && <div className="llm-tooltip-total">Total<b>{format(totals[hover])}</b></div>}
        <small>{rows[hover].calls} calls</small>
      </div>}
    </div>}
  </section>;
}

function Stat({ label, value, note, children }: { label: string; value: string; note?: string; children?: React.ReactNode }) {
  return <div className="llm-stat"><span>{label}</span><strong>{value}</strong>{children}{note && <small>{note}</small>}</div>;
}

export function LlmUsageView({ campaignId }: { campaignId?: string }) {
  const [range, setRange] = useState<(typeof RANGES)[number]['id']>('24h');
  const [usage, setUsage] = useState<Usage | null>(null), [error, setError] = useState(''), [updated, setUpdated] = useState<Date | null>(null);
  useEffect(() => {
    if (!campaignId) return;
    let active = true;
    const option = RANGES.find(r => r.id === range)!;
    const load = async () => {
      const params = new URLSearchParams({ campaign_id: campaignId, bucket: option.bucket });
      if (option.ms) params.set('since', new Date(Date.now() - option.ms).toISOString());
      try {
        const response = await fetch(`/api/v1/llm-usage?${params}`, { cache: 'no-store' });
        const body = await response.json();
        if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `${response.status} ${response.statusText}`);
        if (active) { setUsage(body); setError(''); setUpdated(new Date()); }
      } catch (reason) { if (active) setError(`Usage could not be refreshed: ${errorText(reason)}. The last loaded values remain visible.`); }
    };
    void load();
    const timer = window.setInterval(load, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, [campaignId, range]);

  if (!campaignId) return <Empty icon="spark" title="No campaign selected">Choose a campaign to see its agents' model usage.</Empty>;
  const totals = usage?.totals, budget = usage?.budget;
  const share = budget && budget.cap_usd ? Math.min(1, budget.spent_usd / budget.cap_usd) : 0;
  const capped = Boolean(budget && budget.cap_usd !== null && budget.spent_usd >= budget.cap_usd);
  return <div className="llm-usage">
    <div className="page-heading"><div><span className="eyebrow">Agent team</span><h1>LLM usage</h1>
      <p>Every model call made by this campaign's agents: tokens, cache reuse, and cost.</p></div>
      <div className="llm-filters" role="group" aria-label="Time range">
        {RANGES.map(r => <button key={r.id} className={`button small ${range === r.id ? 'primary' : 'secondary'}`} aria-pressed={range === r.id} onClick={() => setRange(r.id)}>{r.label}</button>)}
      </div></div>
    <ErrorNotice text={error} />
    {!usage ? <p className="llm-muted">Loading usage…</p> : <>
      <div className="llm-stats">
        <Stat label="API spend" value={usd(budget!.spent_usd)} note={budget!.cap_usd === null ? 'No cap set' : `of ${usd(budget!.cap_usd)} campaign cap · all time`}>
          {budget!.cap_usd !== null && <div className="llm-meter" role="meter" aria-valuemin={0} aria-valuemax={budget!.cap_usd} aria-valuenow={budget!.spent_usd} aria-label="API spend against the cap"><span style={{ width: `${share * 100}%` }} /></div>}
          {capped && <Badge tone="amber"><Icon name="warning" size={13} /> Cap reached: new agent turns are on hold</Badge>}
        </Stat>
        <Stat label="List-price cost" value={usd(totals!.cost_usd)}
          note={totals!.priced_calls < totals!.calls ? `${totals!.calls - totals!.priced_calls} older calls have no price` : 'Catalog prices, including subscription calls'} />
        <Stat label="Model calls" value={compact.format(totals!.calls)} note={totals!.last_at ? `Last ${when(totals!.last_at)}` : 'None in this range'} />
        <Stat label="Tokens" value={tokens(totals!.total_tokens)} note={`${tokens(totals!.input + totals!.cache_read + totals!.cache_write)} in · ${tokens(totals!.output)} out${totals!.reasoning ? ` (${tokens(totals!.reasoning)} reasoning)` : ''}`} />
        <Stat label="Cache hit rate" value={cacheRate(totals!)} note="Share of prompt tokens served from cache" />
      </div>
      {usage.family && <p className="llm-muted">This campaign is locked to the <b>{usage.family}</b> model family. Models within it can be switched in dev mode.</p>}
      {!usage.series.length ? <Empty icon="spark" title="No model calls in this range">Calls appear here as agents work.</Empty> : <>
        <BarChart title={`Tokens per ${usage.bucket}`} rows={usage.series} size={usage.bucket} format={tokens}
          segments={row => SERIES.map(s => ({ ...s, value: row[s.key] }))} />
        <BarChart title={`List-price cost per ${usage.bucket}`} rows={usage.series} size={usage.bucket} format={usd}
          segments={row => [{ key: 'cost', label: 'Cost', color: SERIES[0].color, value: row.cost_usd || 0 }]} />
      </>}
      <section className="llm-breakdown"><h3>By model</h3><table className="llm-table"><thead><tr><th>Model</th><th>Family</th><th>Billing</th>
          <th className="numeric">Calls</th><th className="numeric">Tokens</th><th className="numeric">Cache hit</th><th className="numeric">List price</th><th className="numeric">Charged</th></tr></thead>
        <tbody>{usage.by_model.map(row => <tr key={`${row.provider}/${row.model}/${row.billing}`}><td>{row.provider}/{row.model}</td><td>{row.family}</td><td>{row.billing}</td>
          <td className="numeric">{row.calls}</td><td className="numeric">{tokens(row.total_tokens)}</td><td className="numeric">{cacheRate(row)}</td>
          <td className="numeric">{usd(row.cost_usd)}</td><td className="numeric">{usd(row.charged_usd)}</td></tr>)}</tbody></table></section>
      <section className="llm-breakdown"><h3>By agent</h3><table className="llm-table"><thead><tr><th>Agent</th><th>Current model</th><th>Status</th>
          <th className="numeric">Calls</th><th className="numeric">Tokens</th><th className="numeric">Cache hit</th><th className="numeric">List price</th></tr></thead>
        <tbody>{usage.by_agent.map(row => { const agent = usage.agents[row.agent_id];
          return <tr key={row.agent_id}><td>{roleLabel(row.role || 'agent')}<small className="llm-id">{row.agent_id.slice(0, 18)}</small></td>
            <td>{agent ? `${agent.model}${agent.effort ? ` · ${agent.effort}` : ''}` : '—'}</td><td>{agent?.status || '—'}</td>
            <td className="numeric">{row.calls}</td><td className="numeric">{tokens(row.total_tokens)}</td><td className="numeric">{cacheRate(row)}</td>
            <td className="numeric">{usd(row.cost_usd)}</td></tr>; })}</tbody></table></section>
      <section className="llm-breakdown"><h3>By thinking level</h3><table className="llm-table"><thead><tr><th>Model</th><th>Thinking</th>
          <th className="numeric">Calls</th><th className="numeric">Output</th><th className="numeric">Reasoning</th><th className="numeric">List price</th></tr></thead>
        <tbody>{usage.by_thinking.map(row => <tr key={`${row.provider}/${row.model}/${row.thinking_level}`}><td>{row.provider}/{row.model}</td><td>{row.thinking_level || 'default'}</td>
          <td className="numeric">{row.calls}</td><td className="numeric">{tokens(row.output)}</td><td className="numeric">{tokens(row.reasoning)}</td>
          <td className="numeric">{usd(row.cost_usd)}</td></tr>)}</tbody></table></section>
      <p className="llm-muted">Subscription calls are priced at catalog rates for comparison and are not charged against the cap.
        Calls recorded before per-call accounting have token counts but no price.{updated && ` Updated ${updated.toLocaleTimeString()}.`}</p>
    </>}
  </div>;
}
