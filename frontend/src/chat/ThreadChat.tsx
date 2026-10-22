/** The conversation with the lead in one thread: inherited turns (collapsed), saved turns, placeholders, the live turn, the composer with slash commands. */
import { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useHypotheses, useMessages, useMonitor, useSummary, useThreads } from '../api/queries';
import { NowPanel, nowLabel, useAgents } from './NowPanel';
import type { Message } from '../api/types';
import { send } from '../commands/bus';
import { initial, reduce } from './stream';
import { Turn } from './Turn';
import { LiveTurn } from './LiveTurn';
import { Composer } from './Composer';
import { ContextChip, ContextDrawer } from './ContextDrawer';
import { parseSlash, SLASH } from './slash';
import { Failed, Loading, Pill } from '../primitives/ui';
import { formatHash, navigate, type Route } from '../router/routes';
import { Folds } from './Folds';
import { track } from '../telemetry/events';

export function ThreadChat({ campaignId, threadId, route, onDetach }: { campaignId: string; threadId: string; route: Route; onDetach?: () => void }) {
  const queryClient = useQueryClient();
  const summary = useSummary(campaignId);
  const threads = useThreads(campaignId);
  const messages = useMessages(threadId);
  const hypotheses = useHypotheses(campaignId);
  const [stream, dispatch] = useReducer(reduce, initial);
  const [drawer, setDrawer] = useState(false);
  const [inheritedOpen, setInheritedOpen] = useState(false);
  const [editing, setEditing] = useState<Message | null>(null);
  const [help, setHelp] = useState(false);
  const [now, setNow] = useState(false);
  const agents = useAgents(campaignId);
  const monitor = useMonitor(campaignId);
  const pools = (monitor.data?.rows.compute as { pools?: Record<string, { taken: number }> } | undefined)?.pools;
  const running = Object.values(pools || {}).reduce((n, pool) => n + pool.taken, 0);
  const scroller = useRef<HTMLDivElement>(null);
  const aliases = useMemo(() => Object.fromEntries((hypotheses.data?.aliases || []).map(a => [a.label, a.record_id])), [hypotheses.data]);

  useEffect(() => {
    const source = new EventSource(`/api/v1/threads/${encodeURIComponent(threadId)}/stream`);
    source.onmessage = event => {
      let parsed: Record<string, unknown>; try { parsed = JSON.parse(event.data); } catch { return; }
      dispatch(parsed);
      if (parsed.type === 'MESSAGES_SNAPSHOT' || parsed.type === 'RUN_FINISHED' || parsed.type === 'RUN_ERROR') { void queryClient.invalidateQueries({ queryKey: ['messages', threadId] }); void queryClient.invalidateQueries({ queryKey: ['threads', campaignId] }); }
    };
    return () => source.close();
  }, [threadId, campaignId, queryClient]);
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight }); }, [messages.data?.messages.length, stream.live.text, stream.live.steps.length]);
  useEffect(() => { setDrawer(false); setEditing(null); setInheritedOpen(false); }, [threadId]);

  const campaign = summary.data?.campaign;
  const thread = threads.data?.threads.find(t => t.id === threadId);
  const lead = threads.data?.lead;
  const busy = stream.live.status === 'running' || !!thread?.busy;
  const others = (threads.data?.threads || []).filter(t => t.status !== 'merged');
  const base = () => { if (!campaign) throw new Error('The campaign is still loading'); return { campaignId, expectedRevision: campaign.version, origin: { thread_id: threadId } }; };
  const refresh = () => Promise.all([queryClient.invalidateQueries({ queryKey: ['messages', threadId] }), queryClient.invalidateQueries({ queryKey: ['threads', campaignId] })]);

  const ensureLead = async () => { if (!lead) { await send({ ...base(), operation: 'agent.activate', payload: {} }); await queryClient.invalidateQueries({ queryKey: ['threads', campaignId] }); } };
  const fork = async (message: Message | null, title?: string) => {
    const last = message || messages.data?.messages.filter(m => m.origin !== 'lab_event').at(-1);
    if (!last) throw new Error('Nothing to fork from yet');
    const receipt = await send({ ...base(), operation: 'thread.fork', payload: { thread_id: threadId, message_id: last.id, ...(title ? { title } : {}) } });
    track('thread.fork', { from: message ? 'turn' : 'slash' });
    await queryClient.invalidateQueries({ queryKey: ['threads', campaignId] });
    const created = receipt.outcome.thread as { id: string };
    navigate(formatHash({ page: route.page, sub: route.sub, id: route.id }, created.id));
    return created.id;
  };
  const sendMessage = async (text: string) => {
    const slash = parseSlash(text, aliases);
    if (slash?.kind === 'help' || slash?.kind === 'unknown') { setHelp(true); if (slash.kind === 'unknown') throw new Error(`No command /${slash.name}; the list is below`); return; }
    if (slash?.kind === 'fork') { await fork(null, slash.title); return; }
    if (slash?.kind === 'park') { await send({ ...base(), operation: 'thread.park', payload: { thread_id: threadId } }); track('thread.park', {}); await refresh(); return; }
    if (slash?.kind === 'merge') {
      const target = others.find(t => t.id === slash.into || t.name === slash.into || t.title === slash.into);
      if (!target) throw new Error('Name the thread to merge into (its title or id)');
      await send({ ...base(), operation: 'thread.merge', payload: { thread_id: threadId, into_thread_id: target.id, note: slash.note || 'Merged' } });
      track('thread.merge', {}); await refresh(); navigate(formatHash({ page: route.page, sub: route.sub }, target.id)); return;
    }
    await ensureLead();
    if (slash?.kind === 'generate') { await send({ ...base(), operation: 'research.generate', payload: { operation: slash.operation, parent_ids: slash.parents, count: slash.count, note: slash.note } }); await refresh(); return; }
    if (slash?.kind === 'probe') { await send({ ...base(), operation: 'runtime.profile', payload: {} }); await refresh(); return; }
    if (slash?.kind === 'compare') { await send({ ...base(), operation: 'comparison.report', payload: slash.ids.length ? { trial_ids: slash.ids } : {} }); await refresh(); return; }
    const body = slash?.kind === 'message' ? slash.text : text;
    const pageAgent = slash?.kind === 'message' ? slash.page_agent : undefined;
    if (editing) {
      // Edit and resend: a branch starts before the edited message and carries the new wording; the original stays.
      const before = messages.data?.messages.filter(m => m.origin !== 'lab_event');
      const index = before?.findIndex(m => m.id === editing.id) ?? -1;
      const anchor = index > 0 ? before![index - 1] : null;
      let target = threadId;
      if (anchor) target = await fork(anchor, 'edited: ' + body.slice(0, 40));
      await send({ campaignId, expectedRevision: campaign!.version, origin: { thread_id: target }, operation: 'agent.message', payload: { message: body, thread_id: target, edit_of_message_id: editing.id } });
      setEditing(null); await refresh(); return;
    }
    await send({ ...base(), operation: 'agent.message', payload: { message: body, thread_id: threadId, ...(pageAgent ? { page_agent: pageAgent } : {}) } });
    track('chat.send', { length: body.length, slash: slash?.kind || null });
    await refresh();
  };

  const data = messages.data;
  const merged = data?.status === 'merged';
  // What to ask next: the housekeeper's live suggestions after the lead's latest reply; stage defaults only until the lead has spoken.
  const stage = summary.data?.stage.stage;
  const counts = summary.data?.counts || {};
  const spoken = (data?.messages || []).filter(m => m.origin !== 'lab_event' && m.kind !== 'agents_link');
  const lastId = spoken.at(-1)?.id;
  const live = thread?.suggestions && thread.suggestions.as_of_message_id === lastId ? thread.suggestions.items : null;
  const leadSpoke = spoken.some(m => m.role === 'assistant' && (m.origin === 'lead' || m.origin === 'autopilot'));
  const suggestions = live ? live : leadSpoke ? [] : stage === 'formulate' ? [{ label: 'Draft the problem note', text: '/note' }, { label: 'What do you need from me?', text: 'What do you still need to know before we can formulate the problem?' }]
    : stage === 'generate' ? (counts.hypothesis ? [{ label: 'Three more strategies', text: '/generate 3' }, { label: 'Review the proposals', text: '/review' }, { label: 'How are the runs going?', text: 'How are the runs going, and what would you do next?' }]
      : [{ label: 'Generate three strategies', text: '/generate 3' }, { label: 'Measure evaluation speed first', text: '/probe' }, { label: 'What do you see?', text: 'What do you see in this problem, and where would you start?' }])
    : stage === 'optimize' ? [{ label: 'How is the race going?', text: 'How is the race going, and which strategies would you stop or extend?' }, { label: 'Compare at matched cost', text: '/compare' }]
    : stage === 'report' ? [{ label: 'What is left?', text: 'What remains open before the report is done?' }] : [];
  return (
    <div className="chat" data-testid="thread-chat">
      <div className="thread-head">
        <select aria-label="Thread" value={threadId} data-testid="thread-switch" onChange={e => { track('thread.switch', {}); navigate(formatHash({ page: route.page, sub: route.sub, id: route.id }, e.target.value)); }}>
          {(threads.data?.threads || []).filter(t => t.kind !== 'agents').map(t => <option key={t.id} value={t.id}>{t.title}{t.status === 'parked' ? ' (parked)' : t.status === 'merged' ? ' (merged)' : ''}{t.unread ? ' •' : ''}</option>)}
          {(threads.data?.threads || []).some(t => t.kind === 'agents') ? <optgroup label="Agents' conversations" data-testid="agents-group">
            {(threads.data?.threads || []).filter(t => t.kind === 'agents').map(t => <option key={t.id} value={t.id}>{t.title}{t.unread ? ' •' : ''}</option>)}</optgroup> : null}
        </select>
        {thread?.kind === 'agents' ? <Pill tone="agent" title="What the agents said to each other while working on one of your requests">agents</Pill> : thread?.name && thread.name !== 'main' ? <Pill>{thread.status === 'parked' ? 'parked branch' : 'branch'}</Pill> : null}
        <span className="grow" />
        <ContextChip threadId={threadId} onOpen={() => setDrawer(d => !d)} />
        {onDetach ? <button className="btn quiet small" onClick={onDetach} title="Open this chat in its own window; the page fills this one" data-testid="toggle-detach">Detach</button> : null}
        {lead ? null : <Pill title="No agent team yet; your first message starts it">No lead yet</Pill>}
      </div>
      {lead ? <button className={`now-bar ${busy ? 'agent' : ''}`} aria-expanded={now} data-testid="now-toggle" onClick={() => setNow(open => !open)}
        title="What is happening: which agent is at work, what the others last said, the runs and why queued ones wait">
        <span className="dot" aria-hidden />{nowLabel(lead, busy, agents.data?.agents, running)}<span className="grow" /><span className="faint">{now ? 'hide ▴' : 'what is happening ▾'}</span></button> : null}
      {now && lead ? <NowPanel campaignId={campaignId} threadId={threadId} route={route} /> : null}
      <div className="turns" ref={scroller}>
        {messages.isPending ? <Loading what="Loading the conversation" /> : messages.error ? <Failed error={messages.error} /> : null}
        {data?.inherited.length ? <div className="turn lab" data-testid="inherited">
          <button className="btn quiet small" onClick={() => setInheritedOpen(o => !o)}>{inheritedOpen ? 'Hide' : 'Show'} {data.inherited.reduce((n, i) => n + i.count, 0)} inherited turns from {data.inherited.map(i => i.title).join(' › ')}</button>
          {inheritedOpen ? data.inherited_messages.map(m => <Turn key={m.id} message={m as Message} thread={threadId} aliases={aliases} />) : null}
        </div> : null}
        {data?.messages.length === 0 && !busy && !data.inherited.length ? <div className="empty">Nothing yet. Describe the problem or ask the lead what it sees; it starts nothing until you ask. Type / for commands.</div> : null}
        {data ? <Folds messages={data.messages} render={message => <Turn key={message.id} message={message} thread={threadId} aliases={aliases}
          onFork={merged ? undefined : m => fork(m)} onEdit={merged ? undefined : (message.role === 'user' ? m => setEditing(m) : undefined)} />} /> : null}
        {data?.placeholders?.length ? <div className="turn agent" data-testid="placeholders"><div className="meta"><Pill tone="agent">generating</Pill></div>
          {data.placeholders.map(p => <span key={p.id} className="chip" title="A strategy the lead is recording">{p.id} · pending</span>)}</div> : null}
        {data?.questions.length ? data.questions.map(q => <div key={q.id} className="turn lab" data-testid="question">The lead asks: {q.question}
          {q.suggestion ? <div className="small"><strong>It suggests:</strong> {String(q.suggestion)} <a href={formatHash({ page: 'inbox' }, threadId)}>Answer in the Inbox</a></div> : null}</div>) : null}
        <LiveTurn live={stream.live} thread={threadId} />
        {merged ? <div className="turn lab">This branch was merged into another thread. <a href={formatHash({ page: route.page }, data!.merged_into)}>Open it</a>.</div> : null}
      </div>
      {help ? <div className="card small" style={{ margin: '0 12px' }}><div className="row"><strong className="grow">Slash commands</strong><button className="btn quiet small" onClick={() => setHelp(false)}>close</button></div>
        {SLASH.map(s => <div key={s.name}><code>{s.usage}</code> <span className="muted">{s.help}</span></div>)}</div> : null}
      {editing ? <div className="row small" style={{ padding: '4px 12px' }}><Pill tone="you">editing</Pill><span className="grow muted">Resending forks a branch before this message; the original stays.</span><button className="btn quiet small" onClick={() => setEditing(null)}>cancel</button></div> : null}
      <Composer key={editing?.id || 'new'} initial={editing?.content} onSend={sendMessage} disabled={!campaign || merged} why={!campaign ? 'Waiting for the campaign to load' : merged ? 'A merged branch takes no more messages' : undefined}
        placeholder={stage === 'generate' && !counts.hypothesis ? 'Ask the lead for strategies: /generate 3' : undefined} suggestions={busy ? [] : suggestions} suggestionsNote={live ? 'suggested from the last reply' : undefined} />
      {drawer ? <ContextDrawer threadId={threadId} campaignId={campaignId} onClose={() => setDrawer(false)} /> : null}
    </div>
  );
}
