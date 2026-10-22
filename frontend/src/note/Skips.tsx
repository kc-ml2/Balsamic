/** Leaving a section of the problem note unwritten: the lead proposes each skip with its reason and the statement you confirm; you agree or decline. */
import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useRead, useSummary } from '../api/queries';
import { send } from '../commands/bus';
import { Pill } from '../primitives/ui';
import { withThread } from '../router/routes';

export interface SkipItem { section: string; basis: 'charter' | 'not_applicable'; charter_refs: string[]; reason: string; confirm: string; status: 'proposed' | 'agreed' | 'declined' | 'superseded' }
export interface SkipProposal { id: string; note_id: string; author: string; items: SkipItem[]; status: 'open' | 'decided'; created_at: string }
export interface Skipped { section: string; basis: 'charter' | 'not_applicable'; charter_refs: string[]; reason: string; confirm: string; agreed_at: string; skip_id: string; lapsed?: string | null }
export interface Skips { skipped: Record<string, Skipped>; proposals: SkipProposal[] }

const BASIS = { charter: 'the charter states it', not_applicable: 'does not apply here' } as const;
const STATUS = { agreed: ['clear', 'skipped'], declined: ['', 'to be written'], superseded: ['', 'replaced by a later proposal'], proposed: ['agent', 'proposed'] } as const;

/** Agree to or decline skips of one proposal, with the campaign's current revision. */
export function useSkipDecision(campaignId: string) {
  const summary = useSummary(campaignId);
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const decide = async (skipId: string, agree: string[], decline: string[]) => {
    const version = summary.data?.campaign.version;
    if (version === undefined) return;
    setBusy(true); setError(null);
    try {
      await send({ operation: 'problem_note.skip', campaignId, expectedRevision: version, payload: { skip_id: skipId, agree, decline } });
      await Promise.all([queryClient.invalidateQueries({ queryKey: ['note', campaignId] }), queryClient.invalidateQueries({ queryKey: ['summary', campaignId] }), queryClient.invalidateQueries({ queryKey: ['campaigns'] })]);
    } catch (e) { setError(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };
  return { decide, busy, error };
}

/** One skip: why the section need not be written, what agreeing confirms, and what in the charter says so. */
export function SkipReason({ item, title }: { item: Pick<SkipItem, 'basis' | 'charter_refs' | 'reason' | 'confirm'>; title?: string }) {
  return (
    <div className="skip-reason">
      <div className="row wrap">{title ? <strong>{title}</strong> : null}<span className="small faint">{BASIS[item.basis]}</span>
        {item.charter_refs.map(ref => <code key={ref} className="small" title="Where the confirmed charter states it">{ref}</code>)}</div>
      <div>{item.reason}</div>
      <div className="small"><span className="faint">By agreeing you confirm: </span>{item.confirm}</div>
    </div>
  );
}

export function SkipRow({ proposal, item, title, campaignId }: { proposal: SkipProposal; item: SkipItem; title?: string; campaignId: string }) {
  const { decide, busy, error } = useSkipDecision(campaignId);
  const [tone, label] = STATUS[item.status];
  return (
    <div className="skip-row" data-testid="skip-item" data-section={item.section} data-status={item.status}>
      <SkipReason item={item} title={title} />
      <div className="row wrap">
        {item.status === 'proposed' ? <>
          <button className="btn small primary" disabled={busy} onClick={() => decide(proposal.id, [item.section], [])} data-testid="skip-agree" title="Leave this section unwritten; the gate counts it as settled">Agree</button>
          <button className="btn small" disabled={busy} onClick={() => decide(proposal.id, [], [item.section])} data-testid="skip-decline" title="The section is written after all; the lead writes it with you">Write it instead</button>
        </> : <Pill tone={tone}>{label}</Pill>}
        {error ? <span className="pill bad">{error}</span> : null}
      </div>
    </div>
  );
}

/** The card in the conversation: the lead's proposal, decided item by item or all at once. */
export function SkipCard({ campaignId, skipId, thread }: { campaignId: string; skipId: string; thread: string | null }) {
  const view = useRead<{ skips: Skips; titles: Record<string, string> }>(['note', campaignId], `/api/v1/campaigns/${campaignId}/problem-note`, { refetchInterval: 15000 });
  const { decide, busy, error } = useSkipDecision(campaignId);
  const proposal = view.data?.skips.proposals.find(p => p.id === skipId);
  const open = (proposal?.items || []).filter(i => i.status === 'proposed').map(i => i.section);
  return (
    <article className="turn lab skip-card" data-testid="skip-card" data-skip-id={skipId}>
      <div className="row"><Pill tone="agent">skip</Pill><span className="grow">The lead proposes leaving {proposal ? proposal.items.length : 'some'} section{proposal?.items.length === 1 ? '' : 's'} of the problem note unwritten. Nothing is skipped until you agree.</span>
        <a href={withThread('#note', thread)} data-testid="skip-open-note">open the note</a></div>
      {!proposal ? <span className="faint small">{view.error ? 'This proposal is gone.' : 'Loading the proposal…'}</span> : <>
        {proposal.items.map(item => <SkipRow key={item.section} proposal={proposal} item={item} title={view.data?.titles[item.section] || item.section} campaignId={campaignId} />)}
        {open.length > 1 ? <div className="row"><button className="btn small" disabled={busy} onClick={() => decide(proposal.id, open, [])} data-testid="skip-agree-all">Agree to all {open.length}</button>
          {error ? <span className="pill bad">{error}</span> : null}</div> : null}
      </>}
    </article>
  );
}
