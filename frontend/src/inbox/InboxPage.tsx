/** What waits for the researcher: approvals and choices, issues, the lead's questions. Resolving is a command of your own. */
import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { PageProps } from '../shell/StagePanel';
import { useInbox, useSummary } from '../api/queries';
import { send } from '../commands/bus';
import { Empty, Failed, Loading, Pill, Section, when } from '../primitives/ui';
import type { Decision, Issue, Question } from '../api/types';
import { Composer } from '../chat/Composer';

export default function InboxPage({ campaignId, route }: PageProps) {
  const inbox = useInbox(campaignId);
  const summary = useSummary(campaignId);
  const queryClient = useQueryClient();
  const version = summary.data?.campaign.version;
  const after = () => queryClient.invalidateQueries({ queryKey: ['inbox', campaignId] });
  if (inbox.isPending) return <Loading what="Loading the inbox" />;
  if (inbox.error) return <Failed error={inbox.error} />;
  const { decisions, issues, questions, resolved } = inbox.data;
  return (
    <div className="col">
      <div className="stage-head"><h1>Inbox</h1><span className="muted small">{decisions.length + issues.length + questions.length} waiting for you</span></div>
      <Section title={`Decisions (${decisions.length})`}>
        {decisions.length ? decisions.map(d => <DecisionCard key={d.id} decision={d} version={version} campaignId={campaignId} selected={route.id === d.id} onDone={after} />) : <Empty>No approval or choice is waiting.</Empty>}
      </Section>
      <Section title={`Issues (${issues.length})`}>
        {issues.length ? issues.map(i => <IssueCard key={i.id} issue={i} version={version} campaignId={campaignId} onDone={after} />) : <Empty>No operational issue is open.</Empty>}
      </Section>
      <Section title={`Questions from the lead (${questions.length})`}>
        {questions.length ? questions.map(q => <QuestionCard key={q.id} question={q} version={version} campaignId={campaignId} threadId={route.thread} onDone={after} />) : <Empty>The lead has no open question.</Empty>}
      </Section>
      {resolved.length ? <Section title="Recently resolved">{resolved.slice(-10).reverse().map(d => <div key={d.id} className="row small"><span className="grow">{d.title}</span><Pill>{String(d.choice ?? d.status)}</Pill><span className="faint">{when(d.resolved_at)}</span></div>)}</Section> : null}
    </div>
  );
}

function DecisionCard({ decision, version, campaignId, selected, onDone }: { decision: Decision; version?: number; campaignId: string; selected: boolean; onDone: () => unknown }) {
  const [comment, setComment] = useState('');
  const [error, setError] = useState<string | null>(null);
  const choose = async (choice: string) => {
    if (version === undefined) return;
    setError(null);
    try {
      await send({ operation: 'decision.resolve', campaignId, expectedRevision: version, payload: { decision_id: decision.id, choice, comment, expected_resolution_revision: decision.resolution_revision || 0 } });
      await onDone();
    } catch (e) { setError(String((e as Error).message || e)); }
  };
  const blocked = new Set(decision.freshness?.blocked_choice_ids || []);
  return (
    <article className={'card' + (selected ? ' selected' : '')} data-testid="decision" data-record-id={decision.id}>
      <div className="row"><strong className="grow">{decision.title}</strong>{decision.requested_by ? <Pill tone="agent">asked by the {decision.requested_by}</Pill> : null}<span className="faint small">{when(decision.created_at)}</span></div>
      {decision.proposal ? <p>{decision.proposal}</p> : null}
      {decision.rationale && decision.rationale !== decision.proposal ? <p className="muted small">{decision.rationale}</p> : null}
      {decision.freshness?.reason ? <p className="why">{decision.freshness.reason}</p> : null}
      <textarea placeholder="A note with your choice (optional)" value={comment} onChange={e => setComment(e.target.value)} style={{ width: '100%', minHeight: 40 }} />
      <div className="row wrap" style={{ marginTop: 6 }}>
        {decision.options.map(option => (
          <button key={option.id} className={'btn' + (option.id === decision.recommendation ? ' primary' : '')} disabled={decision.status !== 'pending' || blocked.has(option.id)} title={option.description} onClick={() => choose(option.id)}>{option.label}</button>
        ))}
        {decision.status === 'executing' ? <Pill tone="agent">applying your choice…</Pill> : null}
      </div>
      {error ? <div className="pill bad" style={{ marginTop: 6 }}>{error}</div> : null}
    </article>
  );
}

function IssueCard({ issue, version, campaignId, onDone }: { issue: Issue; version?: number; campaignId: string; onDone: () => unknown }) {
  const [error, setError] = useState<string | null>(null);
  const resolve = async (choice: 'resolved' | 'deferred' | 'action', action?: string) => {
    if (version === undefined) return;
    try { await send({ operation: 'issue.resolve', campaignId, expectedRevision: version, payload: { issue_id: issue.id, choice, ...(action ? { action } : {}), comment: '', expected_revision: issue.revision } }); await onDone(); }
    catch (e) { setError(String((e as Error).message || e)); }
  };
  const actions = issue.actions || [];
  return (
    <article className="card" data-testid="issue">
      <div className="row"><Pill tone="attention">{issue.code.replaceAll('_', ' ')}</Pill><span className="faint small">{when(issue.created_at)}</span>{issue.affected ? <span className="mono small faint">{issue.affected}</span> : null}</div>
      <p>{issue.message}</p>
      <div className="row wrap">
        {actions.map((item, i) => <button key={item.id} className={`btn small${i === 0 ? ' primary' : ''}`} title={item.description} data-testid="issue-action" onClick={() => resolve('action', item.id)}>{item.label}</button>)}
        <button className="btn small" title="Records that you dealt with it; it changes nothing by itself" onClick={() => resolve('resolved')}>Mark as handled</button>
        <button className="btn small" onClick={() => resolve('deferred')}>Defer</button></div>
      {actions.length ? <div className="faint small">{actions.map(item => `${item.label}: ${item.description}`).join(' ')}</div> : null}
      {error ? <div className="pill bad">{error}</div> : null}
    </article>
  );
}

function QuestionCard({ question, version, campaignId, threadId, onDone }: { question: Question; version?: number; campaignId: string; threadId: string | null; onDone: () => unknown }) {
  const answer = async (text: string) => {
    if (version === undefined) throw new Error('The campaign is still loading');
    await send({ operation: 'agent.message', campaignId, expectedRevision: version, payload: { message: text, question_id: question.id, thread_id: threadId }, origin: { thread_id: threadId } });
    await onDone();
  };
  return (
    <article className="card" data-testid="question">
      <div className="row"><Pill tone="agent">the lead asks</Pill><span className="faint small">{when(question.created_at)}</span></div>
      <p>{question.question}</p>
      {question.suggestion ? <p className="small" data-testid="question-suggestion"><strong>The lead suggests:</strong> {String(question.suggestion)}</p> : null}
      {(question.options as string[] | undefined)?.length ? <div className="row wrap">{(question.options as string[]).map((option, i) =>
        <button key={option} className={`btn small${i === 0 ? ' primary' : ''}`} data-testid="question-option" onClick={() => void answer(option)}>{option}</button>)}</div> : null}
      <Composer onSend={answer} placeholder="Your answer goes to the lead as a message" testId="answer-composer" />
    </article>
  );
}
