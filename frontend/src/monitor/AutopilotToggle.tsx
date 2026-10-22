/** Autopilot: off by default; the researcher turns it on with a compute cap and an optional report date. The exception to "the lead acts on requests". */
import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { send } from '../commands/bus';
import type { CampaignRow } from '../api/types';
import { Pill } from '../primitives/ui';
import { withThread } from '../router/routes';

export function AutopilotToggle({ campaign, version, threadId }: { campaign: CampaignRow; version: number | undefined; threadId: string | null }) {
  const [open, setOpen] = useState(false);
  const requested = campaign.autopilot?.requested || null;
  const [cap, setCap] = useState(requested?.compute_cap_seconds || 3600);
  const [reportBy, setReportBy] = useState(requested?.report_by || '');
  const [reason, setReason] = useState('');
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const enabled = campaign.autopilot?.enabled;
  const gate = campaign.next_step || null;   // the problem-definition gate: autopilot waits until it is None
  const set = async (on: boolean) => {
    if (version === undefined) return;
    setError(null);
    try {
      await send({ operation: 'autopilot.set', campaignId: campaign.id, expectedRevision: version, payload: on ? { enabled: true, compute_cap_seconds: cap, report_by: reportBy || null, rationale: reason } : { enabled: false, rationale: reason }, origin: { thread_id: threadId } });
      setOpen(false); await queryClient.invalidateQueries({ queryKey: ['campaigns'] }); await queryClient.invalidateQueries({ queryKey: ['monitor'] });
    } catch (e) { setError(String((e as Error).message || e)); }
  };
  return (
    <>
      <button className={'btn control toggle' + (enabled ? ' on' : '')} onClick={() => setOpen(o => !o)} data-testid="autopilot-toggle" title={enabled ? `Autopilot on: cap ${campaign.autopilot.compute_cap_seconds} s` : 'Autopilot off: the lead acts on your requests'}><span className={'switch' + (enabled ? ' on' : '')} aria-hidden="true" />Autopilot {enabled ? 'on' : 'off'}</button>
      {open ? <div className="drawer" data-testid="autopilot-dialog" style={{ width: 380 }}>
        <div className="row"><h2 className="grow">Autopilot</h2><button className="btn quiet small" onClick={() => setOpen(false)}>Close</button></div>
        <p className="small muted">While on, the lead runs rounds by itself: it reviews the frontier, stops, promotes, allocates and generates within the cap, with a reason on every command, and writes the report when the cap or the date is reached. Your messages still come first. Only you can raise limits. It starts only once the problem is fully defined: every section of the note, the adoption, the confirmed charter.</p>
        {enabled ? <><p className="small">On since {String(campaign.autopilot.enabled_at || '')}; cap {String(campaign.autopilot.compute_cap_seconds)} s{campaign.autopilot.report_by ? `; report by ${campaign.autopilot.report_by}` : ''}.</p>
          <input placeholder="Why turn it off (optional)" value={reason} onChange={e => setReason(e.target.value)} style={{ width: '100%' }} />
          <button className="btn primary small" style={{ marginTop: 6 }} onClick={() => set(false)} data-testid="autopilot-off">Turn autopilot off</button></>
        : gate ? <div className="callout attention" data-testid="autopilot-gate" data-kind={gate.kind}>
          <span className="callout-mark" aria-hidden="true">!</span>
          <div className="grow"><strong>{gate.kind === 'evaluator' ? 'Autopilot waits until the evaluator starts in this lab' : `Autopilot waits for the problem definition: ${gate.title.toLowerCase()}`}</strong>
            <div className="small">{gate.text}</div>
            {requested ? <div className="small faint">Chosen at setup (cap {String(requested.compute_cap_seconds)} s); the switch stays yours once the gate opens.</div> : null}
            {gate.page ? <a className="btn small primary" style={{ marginTop: 6 }} href={withThread(gate.page, threadId)} data-testid="autopilot-gate-open">{gate.title}</a> : null}</div>
        </div>
        : <div className="form">{requested ? <p className="small muted" style={{ gridColumn: '1 / -1' }}>Chosen at setup; the problem is defined now, so it can start.</p> : null}<label>Compute cap (s)</label><input type="number" min={60} value={cap} onChange={e => setCap(Number(e.target.value))} data-testid="autopilot-cap" />
          <label>Report by</label><input type="datetime-local" value={reportBy} onChange={e => setReportBy(e.target.value)} />
          <label>Why</label><input value={reason} onChange={e => setReason(e.target.value)} placeholder="overnight run of the current frontier" />
          <span /><button className="btn primary small" onClick={() => set(true)} data-testid="autopilot-on">Turn autopilot on</button></div>}
        {error ? <div className="pill bad">{error}</div> : null}
        <p className="why">Recorded as campaign.autopilot_changed; rows the lead touches under autopilot carry the badge <Pill tone="agent">autopilot</Pill>.</p>
      </div> : null}
    </>
  );
}
