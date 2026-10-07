import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, errorText, providerLabel, providerStatus, when } from './api';
import type { Json, State } from './api';
import { Badge, Empty, ErrorNotice, Field, Icon, SourceLink, Status } from './ui';
import { ManagerIssues } from './implementations';
import { useCommand } from './commands';
import { AgentLog } from './agentLog';
import { DiscoveryPanel } from './discovery';
import { PiConversation } from './piAgents';
import { TextContent } from './textContent';
export { TextContent } from './textContent';

export const researchModes = [
  ['discuss', 'Discuss & redirect'], ['generate', 'Generate strategies'], ['review', 'Critique assumptions'],
  ['compare', 'Compare strategies'], ['evolve', 'Evolve strategies'], ['probe', 'Select an informative probe'], ['plan', 'Plan the next action'],
];

function researchRunLabel(run: Json) {
  return run.parent_review_run_id ? 'Comparative reviewer' : run.decision_review ? 'Decision reassessment' : (run.mode || 'Research').replaceAll('_', ' ');
}

function researchActivity(run: Json) {
  if (run.decision_review) {
    if (run.status === 'stopping') return 'Stopping decision reassessment';
    if (run.status === 'stopped') return 'Decision reassessment stopped';
    if (['partial', 'failed', 'interrupted', 'needs_reconciliation'].includes(run.status)) return 'Decision reassessment needs attention';
    const labels: Record<string, string> = { queued: 'Decision reassessment queued', reviewing: 'Parallel reviewers working',
      synthesizing: 'Manager consolidating', completed: 'Manager review complete' };
    return labels[run.decision_review.phase] || 'Decision reassessment needs attention';
  }
  return `${run.stage || run.mode || 'Research'} in progress`;
}

function messageCommand(state: State, message: Json): Json | undefined {
  if (!['user', 'researcher', 'human'].includes(message.role)) return;
  const run = state.research_runs.find(item => item.id === message.research_run_id);
  return (state.manager_commands || []).find((item: Json) => item.id === message.manager_command_id
    || item.id === run?.manager_command_id || message.id === `message_${item.id}`);
}

function RequestStatus({ state, request, refresh, resumeDiscovery }: {
  state: State; request: Json; refresh: () => Promise<void>; resumeDiscovery?: () => Promise<void>;
}) {
  const command = useCommand(state.campaign);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const run = state.research_runs.find(item => item.id === request.research_run_id)
    || [...state.research_runs].reverse().find(item => item.manager_command_id === request.id && !item.parent_review_run_id);
  const answered = state.messages.some(message => !['user', 'researcher', 'human'].includes(message.role)
    && message.origin !== 'manager_system' && !message.stale && !message.stale_charter
    && (message.manager_command_id === request.id || Boolean(run && message.research_run_id === run.id)));
  const progress = state.research_progress?.request?.id === request.id ? state.research_progress : undefined;
  const status = run?.status || request.status;
  const failed = ['blocked', 'failed', 'interrupted', 'needs_reconciliation'].includes(status);
  const active = ['running', 'stopping'].includes(status) || Boolean(progress?.active);
  const labels: Record<string, string> = {
    blocked: 'Request blocked before the manager could answer', failed: 'Manager response failed',
    interrupted: 'Manager response interrupted', needs_reconciliation: 'Manager response needs reconciliation',
    waiting_provider: 'Waiting for model configuration', waiting_discovery: 'Request saved · discovery paused',
    queued: 'Queued for manager', admitted: 'Queued for manager', pending: 'Queued for manager',
    dispatched: 'Request dispatched · waiting for a response', completed: 'Manager work completed',
    awaiting_researcher: 'Manager work completed · decision requested', stopping: 'Stopping manager work',
    stopped: 'Manager work stopped', cancelled: 'Request cancelled', superseded: 'Request superseded',
  };
  const label = failed ? labels[status] : active ? (run ? researchActivity(run) : 'Agents are working on this request')
    : answered ? 'Response available below' : labels[status] || 'Request saved';
  const reason = failed ? run?.error || request.error || 'Review this request in the agent log.' : undefined;
  const canRetry = status === 'blocked' && !run && !request.research_run_id && !request.decision_refresh_id && !request.automatic;
  async function retry() {
    setBusy(true); setError('');
    try { await command('research.retry', { manager_command_id: request.id }); await refresh(); }
    catch (e) { setError(errorText(e)); await refresh(); }
    finally { setBusy(false); }
  }
  return <div className={`callout${failed ? ' amber' : ''}`} data-manager-command-id={request.id} data-request-status={status}>
    <p role="status" aria-live="polite"><strong>{label}</strong></p>
    {reason && <TextContent text={reason} />}
    {status === 'blocked' && !run && <p>Your question is saved. No model call started for this request.</p>}
    {status === 'waiting_discovery' && <p>Resume discovery to process this saved request.</p>}
    {['completed', 'awaiting_researcher'].includes(status) && !answered && <p>No conversational reply was recorded. Inspect the agent record for the outcome.</p>}
    <div className="inline-actions">
      {canRetry && <button className="button small secondary" type="button" disabled={busy} onClick={() => void retry()}>{busy ? 'Queuing retry…' : 'Retry saved request'}</button>}
      {status === 'waiting_discovery' && resumeDiscovery && <button className="button small primary" type="button" onClick={() => void resumeDiscovery()}>Resume discovery and process request</button>}
      {(failed || active || ['completed', 'awaiting_researcher', 'dispatched'].includes(status)) && <a href="#notebook/agent-log">View agent log</a>}
    </div>
    <ErrorNotice text={error} />
  </div>;
}

export function ResearchPanel({ state, onClose, refresh, onResearch }: { state: State; onClose: () => void; refresh: () => Promise<void>; onResearch?: (message: string, mode: string) => void }) {
  if (state.agent_runtime?.configuration?.enabled) return <PiConversation key={state.campaign?.id} state={state} refresh={refresh} onClose={onClose} />;
  return <LegacyResearchPanel state={state} onClose={onClose} refresh={refresh} onResearch={onResearch} />;
}

function LegacyResearchPanel({ state, onClose, refresh, onResearch }: { state: State; onClose: () => void; refresh: () => Promise<void>; onResearch?: (message: string, mode: string) => void }) {
  const command = useCommand(state.campaign);
  const [text, setText] = useState(''), [mode, setMode] = useState('discuss'), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const bottom = useRef<HTMLDivElement>(null);
  const messages = state.messages;
  const provider = providerStatus(state);
  const linkedCommands = new Set(messages.map(message => messageCommand(state, message)?.id).filter(Boolean));
  const pendingRequests = (state.manager_commands || []).filter((c: Json) => !linkedCommands.has(c.id)
    && ['queued', 'waiting_provider', 'admitted', 'waiting_discovery', 'blocked'].includes(c.status));
  const issueState = { ...state, manager_issues: (state.manager_issues || []).filter((issue: Json) =>
    !(issue.code === 'manager_request' && linkedCommands.has(issue.affected))) };
  const managerRuns = state.research_runs.filter(run => !run.parent_review_run_id);
  const latestRun = managerRuns.at(-1);
  const working = managerRuns.filter(run => ['queued', 'running', 'pending', 'stopping'].includes(run.status));
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, [messages.length, working.length]);
  async function submit(e?: FormEvent, content = text, selectedMode = mode) {
    e?.preventDefault(); if (!content.trim() || !state.campaign) return;
    setBusy(true); setError('');
    try { await command('research.start', { message: content.trim(), mode: selectedMode }); setText(''); await refresh(); onResearch?.(content, selectedMode); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  async function resumeDiscovery() {
    try {
      const view = await api<Json>(`/api/campaigns/${state.campaign!.id}/discovery`);
      const session = [...view.sessions].reverse().find((s: Json) => s.status === 'paused');
      if (session) await command('discovery.control', { session_id: session.id, action: 'resume', expected_control_revision: session.control_revision });
      await refresh();
    } catch (e) { setError(errorText(e)); }
  }
  async function stopResearch(id: string) { try { await command('research.control', { run_id: id, action: 'stop', expected_control_revision: state.research_runs.find(run => run.id === id)?.control_revision || 0 }); await refresh(); } catch (e) { setError(errorText(e)); } }
  return <aside className="research-panel" aria-label="Research conversation"><div className="research-header"><div className="assistant-mark"><Icon name="spark" /></div><div><h2>Campaign manager</h2><a className="model-settings-link" href="#models" title="Configure campaign model assignments">{providerLabel(provider)} · Models</a></div><button className="icon-button" aria-label="Close research panel" onClick={onClose}><Icon name="close" size={18} /></button></div><div className="research-body"><ManagerIssues state={issueState} refresh={refresh} discuss={message => { setText(message); }} />{pendingRequests.map((request: Json) => <div key={request.id}><RequestStatus state={state} request={request} refresh={refresh} resumeDiscovery={resumeDiscovery} /><details><summary>Read request</summary><TextContent text={request.request?.message} /></details></div>)}{!provider.configured && <div className="connection-note"><span className="dot amber" /><div><strong>{provider.enabled ? 'Model configuration needed' : 'Model configuration deferred'}</strong><p>{provider.provider === 'none' ? 'No model provider is selected. Choose one in the server configuration to use agent research.' : provider.provider === 'codex' ? `Campaign manager assignment: ${providerLabel(provider)}. Choose per-role assignments in Models and configure provider access on the server.` : 'Configure and enable this provider to use agent research.'} Your requests are saved until model access is available.</p><p>{provider.status_reason || 'Experiments and your notebook remain available.'}</p></div></div>}{provider.configured && <div className="connection-note"><span className="dot green" /><div><strong>{provider.billing_mode === 'subscription' ? 'Subscription selected' : 'Paid API selected'}</strong><p>{provider.billing_mode === 'subscription' ? 'Saved ChatGPT login is checked before each call. Uses your Codex allowance, with no automatic paid fallback.' : 'Calls use the campaign API spending cap.'}</p></div></div>}{messages.length === 0 ? <div className="research-intro"><div className="orbit-icon"><Icon name="spark" size={30} /></div><h3>Good research starts<br />with a better question.</h3><p>Bring a hypothesis, challenge an assumption, or ask which experiment would teach us the most.</p><div className="prompt-options"><button disabled={!state.campaign || busy} onClick={() => void submit(undefined, 'Analyze the objective and physical assumptions of this campaign. What should we clarify before comparing algorithms?', 'discuss')}><Icon name="problem" size={17} /><span>Help formulate the problem</span><Icon name="chevron" size={15} /></button><button disabled={!state.campaign || busy} onClick={() => void submit(undefined, 'Propose competing optimization strategies from independent perspectives. Explain the physical rationale, assumptions, startup cost, and cheapest discriminating test for each.', 'generate')}><Icon name="hypothesis" size={17} /><span>Develop competing strategies</span><Icon name="chevron" size={15} /></button><button disabled={!state.campaign || busy} onClick={() => void submit(undefined, 'Choose the most informative development configuration to distinguish our current strategies. Use available evidence and explain uncertainty; do not simply assume that the hardest case is the most useful.', 'probe')}><Icon name="experiments" size={17} /><span>Find the most useful probe</span><Icon name="chevron" size={15} /></button></div></div> : <div className="message-list">{messages.map((message, index) => { const request = messageCommand(state, message); return <ResearchMessage key={message.id || index} message={message}>{request && <RequestStatus state={state} request={request} refresh={refresh} resumeDiscovery={resumeDiscovery} />}</ResearchMessage>; })}</div>}{working.map(run => <div className="research-working" key={run.id}><Icon name="spark" size={16} /><span role="status">{researchActivity(run)}</span><span className="typing"><i /><i /><i /></span><button type="button" className="icon-button" aria-label={run.decision_review ? 'Stop decision reassessment' : 'Stop research run'} disabled={run.status === 'stopping'} onClick={() => void stopResearch(run.id)}><Icon name="stop" size={13} /></button></div>)}{(!linkedCommands.size && !pendingRequests.length && latestRun?.status === 'failed' ? [latestRun] : []).map(run => <ErrorNotice key={run.id} text={run.error || 'The latest research run failed. Review the record in the notebook.'} />)}<div ref={bottom} /></div><form className="research-composer" onSubmit={submit}><label className="sr-only" htmlFor="research-mode">Research activity</label><div className="composer-mode"><Icon name="spark" size={14} /><select id="research-mode" value={mode} onChange={e => setMode(e.target.value)}>{researchModes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><label className="sr-only" htmlFor="research-message">Message to campaign manager</label><textarea id="research-message" value={text} onChange={e => setText(e.target.value)} rows={3} placeholder={state.campaign ? 'Ask, challenge, or change direction…' : 'Create a campaign to start the conversation…'} disabled={!state.campaign} onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submit(e); }} /><div className="composer-footer"><span>⌘ / Ctrl + Enter to send</span><button className="send-button" disabled={busy || !text.trim() || !state.campaign} aria-label="Send research message"><Icon name="send" size={17} /></button></div><ErrorNotice text={error} /></form><p className="research-footnote">Rationale guides the next step. Measurements establish performance.</p></aside>;
}

function SourceLibrary({ state, refresh }: { state: State; refresh: () => Promise<void> }) {
  const command = useCommand(state.campaign);
  const [query, setQuery] = useState(''), [provider, setProvider] = useState('arxiv'), [identifier, setIdentifier] = useState('');
  const [busy, setBusy] = useState(''), [error, setError] = useState('');
  async function retrieve(kind: 'search' | 'ingest') {
    if (!state.campaign) return; setBusy(kind); setError('');
    try {
      const payload = kind === 'search' ? { query, provider, limit: 5 } : { identifier };
      await command(kind === 'search' ? 'literature.search' : 'source.ingest', payload);
      await refresh();
    } catch (e) { setError(errorText(e)); } finally { setBusy(''); }
  }
  const sources: Json[] = state.sources || [];
  return <><section className="panel source-search-panel"><div className="form-grid"><Field label="Search scientific literature"><input value={query} onChange={e => setQuery(e.target.value)} placeholder="e.g. binary grating optimization surrogate" onKeyDown={e => { if (e.key === 'Enter') void retrieve('search'); }} /></Field><Field label="Metadata provider"><select value={provider} onChange={e => setProvider(e.target.value)}><option value="arxiv">arXiv</option><option value="crossref">Crossref</option></select></Field></div><button className="button secondary" disabled={!query.trim() || !!busy || !state.campaign} onClick={() => void retrieve('search')}><Icon name="notebook" size={15} />{busy === 'search' ? 'Searching…' : 'Search sources'}</button><div className="source-import"><Field label="Add a paper by DOI or URL"><input value={identifier} onChange={e => setIdentifier(e.target.value)} placeholder="https://doi.org/… or https://arxiv.org/abs/…" onKeyDown={e => { if (e.key === 'Enter') void retrieve('ingest'); }} /></Field><button className="button secondary" disabled={!identifier.trim() || !!busy || !state.campaign} onClick={() => void retrieve('ingest')}><Icon name="plus" size={15} />{busy === 'ingest' ? 'Retrieving…' : 'Retrieve paper'}</button></div><ErrorNotice text={error} />{(state.source_requests || []).filter((request: Json) => request.status !== 'completed').slice(-5).map((request: Json) => <div key={request.id} className="callout"><strong>{request.status === 'failed' ? 'Source retrieval failed' : 'Source retrieval queued'}</strong><p>{request.query || request.identifier}</p>{request.error && <ErrorNotice text={request.error} />}</div>)}<p className="help-text">Retrieved metadata and excerpts support source-grounded review. They do not establish whether an algorithm will perform well on this problem.</p></section><section className="panel">{sources.length ? <div className="source-library-list">{sources.map((source, index) => <article key={source.id || index}><div className="row-between"><Badge>{source.provider || source.verification || 'Research source'}</Badge><span className="muted">{source.year || source.published || ''}</span></div><h3><SourceLink source={source} /></h3>{source.authors && <p className="source-authors">{Array.isArray(source.authors) ? source.authors.map((author: string | Json) => typeof author === 'string' ? author : author.name || `${author.given || ''} ${author.family || ''}`).join(', ') : String(source.authors)}</p>}{source.supports && <p>{source.supports}</p>}{(source.abstract || source.excerpt) && <details><summary>Read available excerpt</summary><TextContent text={source.abstract || source.excerpt} /></details>}<details><summary>Source provenance</summary><pre>{JSON.stringify(source, null, 2)}</pre></details></article>)}</div> : <Empty icon="notebook" title="Connect ideas to their sources">Search the literature or contribute a paper, then ask the research partner how its assumptions apply here.</Empty>}</section></>;
}

export function ResearchMessage({ message, children }: { message: Json; children?: ReactNode }) {
  const user = ['user', 'researcher', 'human'].includes(message.role);
  return <article className={`message ${user ? 'user' : 'assistant'}`}><div className="message-meta"><span>{user ? 'You' : message.origin === 'manager_system' ? 'Campaign manager · system status' : message.agent || message.persona || 'Campaign manager'}</span><time>{when(message.created_at)}</time></div><TextContent text={message.content || message.text || message.message} />{message.mode && !user && <Badge>{message.mode}</Badge>}{children}</article>;
}

export function Notebook({ state, refresh }: { state: State; refresh: () => Promise<void> }) {
  const command = useCommand(state.campaign);
  const notebookFilter = () => { const tab = location.hash.split('/')[1]; return ['all', 'conversation', 'research', 'discovery', 'agent-log', 'sources'].includes(tab) ? tab : 'all'; };
  const [filter, setFilter] = useState(notebookFilter);
  useEffect(() => { const change = () => setFilter(notebookFilter()); window.addEventListener('hashchange', change); return () => window.removeEventListener('hashchange', change); }, []);
  const [controlError, setControlError] = useState('');
  async function controlRun(id: string, action: string) { setControlError(''); try { await command('research.control', { run_id: id, action, expected_control_revision: state.research_runs.find(run => run.id === id)?.control_revision || 0 }); await refresh(); } catch (e) { setControlError(errorText(e)); } }
  const events = [...state.events].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  return <><div className="page-heading"><div><span className="eyebrow">Research memory</span><h1>The notebook</h1><p>Decisions, evidence, and changes of direction, kept in context.</p></div>{state.campaign && <a className="button secondary" href={`/api/campaigns/${state.campaign.id}/export`} download><Icon name="download" size={17} />Export research report</a>}</div><div className="segment-tabs" aria-label="Notebook filter">{[['all', 'Activity'], ['conversation', 'Conversation'], ['research', 'Agent runs'], ['discovery', 'Discovery'], ['agent-log', 'Agent log'], ['sources', 'Source library']].map(([key, label]) => <button key={key} className={filter === key ? 'selected' : ''} onClick={() => { setFilter(key); location.hash = key === 'all' ? 'notebook' : `notebook/${key}`; }}>{label}</button>)}</div><ErrorNotice text={controlError} />{filter === 'discovery' && state.campaign ? <DiscoveryPanel key={state.campaign.id} state={state} refresh={refresh} /> : filter === 'agent-log' && state.campaign ? <AgentLog key={state.campaign.id} campaignId={state.campaign.id} /> : filter === 'sources' ? <SourceLibrary state={state} refresh={refresh} /> : filter === 'all' ? <section className="panel notebook-events">{events.length ? events.map((event, index) => <article className="timeline-entry" key={event.id || index}><div className="timeline-dot"><Icon name={/trial|experiment/.test(event.kind || event.type) ? 'experiments' : /hypothesis/.test(event.kind || event.type) ? 'hypothesis' : 'notebook'} size={15} /></div><div><div className="timeline-meta"><strong>{(event.kind || event.type || 'Research event').replaceAll('_', ' ').replaceAll('.', ' · ')}</strong><time>{when(event.created_at)}</time></div><TextContent text={event.message || event.summary || event.text || event.payload?.message || ''} />{(event.payload || event.data) && <details><summary>Event record</summary><pre>{JSON.stringify(event.payload || event.data, null, 2)}</pre></details>}</div></article>) : <Empty icon="notebook" title="A clean page">Campaign changes, experiment actions, and research decisions will be recorded here.</Empty>}</section> : filter === 'conversation' ? <section className="panel notebook-conversation">{state.messages.length ? state.messages.map((message, index) => <ResearchMessage message={message} key={message.id || index} />) : <Empty icon="chat" title="Start a conversation">Use the research partner panel to explore the objective or propose a direction.</Empty>}</section> : <section className="panel">{state.research_runs.length ? <div className="run-list">{[...state.research_runs].reverse().map(run => <article key={run.id} data-run-id={run.id}><div className="row-between"><strong>{researchRunLabel(run)}</strong><Status status={run.status} /></div>{run.decision_review && <p>{researchActivity(run)}</p>}{run.parent_review_run_id && <p>This reviewer report belongs to a campaign manager reassessment.</p>}<p>{run.message || run.request?.message || run.error || run.stage}</p><div className="meta-row"><span>{when(run.created_at)}</span><span>{run.model || run.usage?.model}</span>{run.usage?.billing_mode === 'subscription' ? <><span>{run.usage.subscription_calls ?? run.usage.calls ?? 0} subscription calls</span><span>Uses subscription allowance</span></> : <>{(run.usage?.api_cost_usd ?? run.usage?.cost_usd ?? run.cost_usd) != null && <span>${Number(run.usage?.api_cost_usd ?? run.usage?.cost_usd ?? run.cost_usd).toFixed(4)} estimated API cost</span>}<span>{run.usage?.calls ?? 0} model calls</span></>}{run.usage?.input_tokens != null && <span>{run.usage.input_tokens} input tokens</span>}{run.usage?.output_tokens != null && <span>{run.usage.output_tokens} output tokens</span>}</div>{!run.parent_review_run_id && (run.status === 'running' || run.status === 'interrupted' && !run.decision_review) && <button className="button small secondary" onClick={() => void controlRun(run.id, run.status === 'interrupted' ? 'resume' : 'stop')}><Icon name={run.status === 'interrupted' ? 'play' : 'stop'} size={13} />{run.status === 'interrupted' ? 'Resume research' : run.decision_review ? 'Stop decision reassessment' : 'Stop research'}</button>}{(run.parent_review_run_id || run.decision_review) && <a className="button small secondary" href="#decisions">{run.parent_review_run_id ? 'View parent reassessment' : ['completed', 'awaiting_researcher'].includes(run.status) ? 'View decision review' : 'Review or retry in Decision inbox'}</a>}<details><summary>Inspect research record</summary><pre>{JSON.stringify(run, null, 2)}</pre></details></article>)}</div> : <Empty icon="spark" title="No agent runs yet">Agent activity, model usage, reasoning stages, and failures will remain inspectable.</Empty>}</section>}</>;
}
