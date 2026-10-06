import { useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, errorText } from './api';
import type { State, Json } from './api';
import { useCommand } from './commands';
import { Badge, ErrorNotice, Field, Icon } from './ui';
import { TextContent } from './textContent';
import { DevelopmentPanel } from './development';

export function PiAgentPanel({ state, refresh, connectionError }: { state: State; refresh: () => Promise<void>; connectionError?: string }) {
  const runtime = state.agent_runtime!;
  const config = runtime.configuration;
  const command = useCommand(state.campaign);
  const [message, setMessage] = useState(''), [mode, setMode] = useState('steer');
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState<string | null>(null);
  const [login, setLogin] = useState<Json | null>(null);
  async function signIn() {
    setBusy(true); setError('');
    try {setLogin(await api(`/api/campaigns/${state.campaign!.id}/agents/login`,{method:'POST',body:'{}'}));}
    catch(failure){setError(errorText(failure));}finally{setBusy(false);}
  }
  async function send(event: FormEvent) {
    event.preventDefault(); if (!message.trim() || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await command('agent.message', {message, mode, ...(question ? {question_id: question} : {})});
      setMessage(''); setQuestion(null); setNotice('Message saved for the lead agent.');
      try { await refresh(); } catch { setNotice('Message saved. Reconnecting to refresh its status.'); }
    } catch (failure) { setError(errorText(failure)); } finally { setBusy(false); }
  }
  async function control(action: string, agent?: Json) {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await command('agent.control', {action, expected_control_revision: (agent || config).control_revision,
        ...(agent ? {agent_id: agent.id} : {})});
      setNotice(`${action === 'resume' ? 'Resume' : action === 'pause' ? 'Pause' : 'Stop'} requested; saved work is retained.`);
      try { await refresh(); } catch { /* Accepted control must not be resent after a refresh failure. */ }
    } catch (failure) { setError(errorText(failure)); } finally { setBusy(false); }
  }
  const agents: Json[] = runtime.agents || [];
  const lead = agents.find(a => a.role === 'lead');
  return <><section className="research-progress pi-panel" aria-label="Agent team">
    <div className="research-progress-heading"><div><h2>Lead agent</h2><Badge>{config.provider?.configured ? lead?.status || config.status : 'waiting for sign-in'}</Badge></div>
      <div className="research-progress-actions">
        {config.status === 'running' ? <button className="button small secondary" disabled={busy} onClick={() => void control('pause')}>Pause agents</button>
          : <button className="button small primary" disabled={busy} onClick={() => void control('resume')}>Resume agents</button>}
        <button className="button small secondary" disabled={busy || config.status === 'stopped'} onClick={() => void control('stop')}>Stop agents</button>
        <a className="button small secondary" href="#notebook/agent-log">View agent log</a>
      </div>
    </div>
    {!config.provider?.configured && <div role="status"><p>{config.provider?.reason || 'Pi needs a signed-in model provider.'} Your assignments are saved.</p>
      <button className="button secondary" disabled={busy} onClick={() => void signIn()}>Get browser sign-in link</button>
      {(login || config.provider?.login)?.status === 'pending' && <p>Open <a href={(login || config.provider.login).url} target="_blank" rel="noreferrer">OpenAI sign-in</a> and enter <strong>{(login || config.provider.login).user_code}</strong>. Pi detects completion automatically. The code expires after 15 minutes.</p>}
    </div>}
    {connectionError && <p className="research-progress-stale">Connection interrupted. Showing the last received agent status.</p>}
    {config.sync_error && <ErrorNotice text={`Agent status could not be synchronized: ${config.sync_error}. Saved work is retained; synchronization will retry.`} />}
    <p>{lead?.activity || 'Your campaign and saved work are ready. The lead agent will continue your latest request after sign-in.'}</p>
    {(runtime.questions || []).map((q: Json) => <article className="research-direction" key={q.id}>
      <strong>Lead agent’s request</strong><TextContent text={q.question} /><TextContent text={q.reason} />
      <button className="button small secondary" onClick={() => setQuestion(q.id)}>Answer this request</button>
    </article>)}
    <form onSubmit={send}>
      <Field label={question ? 'Your answer to the lead agent' : 'Message the lead agent'}><textarea rows={3} value={message} onChange={e => setMessage(e.target.value)} required /></Field>
      <div className="research-progress-actions"><select aria-label="Message timing" value={mode} onChange={e => setMode(e.target.value)}>
        <option value="steer">Steer current work</option><option value="follow_up">Queue a follow-up</option>
      </select><button className="button primary" disabled={busy || !message.trim()}>{busy ? 'Saving…' : 'Send to lead agent'}</button></div>
    </form>
    <ErrorNotice text={error} />{notice && <p role="status">{notice}</p>}
    <details open><summary>Agent team · {agents.length} sessions</summary>
      <div className="pi-agent-tree">{agents.map(agent => <article key={agent.id} className={agent.parent_agent_id ? 'pi-child' : ''}>
        <div><strong>{agent.role.replaceAll('_', ' ')}</strong> <Badge>{agent.status}</Badge></div>
        {agent.role === 'implementation_builder' && agent.status === 'stopped' &&
          runtime.development?.workspaces?.some((workspace: Json) => workspace.submissions?.length > 0) &&
          <p className="help-text">Earlier builder session. Its submitted source and review remain in the implementation workspace.</p>}
        {agent.role !== 'lead' && <p>{agent.objective.length > 300 ? agent.objective.slice(0, 300) + '…' : agent.objective}</p>}<small>{[agent.model, agent.reasoning_effort, agent.parent_agent_id ? 'reports to the lead agent' : ''].filter(Boolean).join(' · ')}</small>
        <details><summary>Assignment, activity and references</summary><TextContent text={agent.objective} /><TextContent text={agent.activity || 'Waiting for the next recorded event.'} />
          <a href={`/api/campaigns/${state.campaign!.id}/agents/records/${agent.id}`} target="_blank" rel="noreferrer">Inspect saved agent record</a>
          {agent.artifact_ids?.map((id:string)=><p key={id}><a href={`/api/campaigns/${state.campaign!.id}/agents/records/${id}`} target="_blank" rel="noreferrer">Saved artifact · {id.slice(-12)}</a></p>)}
          {agent.evidence_ids?.length > 0 && <p>Evidence: {agent.evidence_ids.join(', ')}</p>}
        </details>
        {agent.role !== 'lead' && <div className="research-progress-actions">
          <button className="button small secondary" disabled={busy || agent.status === 'completed'} onClick={() => void control(agent.status === 'paused' ? 'resume' : 'pause', agent)}>{agent.status === 'paused' ? 'Resume' : 'Pause'}</button>
          <button className="button small secondary" disabled={busy || ['completed', 'stopped'].includes(agent.status)} onClick={() => void control('stop', agent)}>Stop</button>
        </div>}
      </article>)}</div>
    </details>
  </section><DevelopmentPanel state={state} refresh={refresh} /></>;
}

export function PiConversation({state,refresh,onClose}:{state:State;refresh:()=>Promise<void>;onClose:()=>void}) {
  const command=useCommand(state.campaign);
  const [message,setMessage]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const lead=state.agent_runtime?.agents.find((agent:Json)=>agent.role==='lead');
  // origin 'pi' marks every harness agent's reply, not only the lead's.
  const messages=state.messages.filter(m=>m.agent_id===lead?.id);
  const scrollArea=useRef<HTMLDivElement>(null), followLatest=useRef(true);
  const lastMessage=messages.at(-1);
  useLayoutEffect(()=>{
    const area=scrollArea.current;
    if(area && followLatest.current) area.scrollTop=area.scrollHeight;
  },[messages.length,lastMessage?.id,lastMessage?.content]);
  async function send(event:FormEvent){
    event.preventDefault();if(!message.trim()||busy)return;
    setBusy(true);setError('');
    try{await command('agent.message',{message,mode:'steer'});followLatest.current=true;setMessage('');try{await refresh();}catch{/* Saved already. */}}
    catch(failure){setError(errorText(failure));}finally{setBusy(false);}
  }
  return <aside className="research-panel pi-conversation" aria-label="Lead agent conversation">
    <div className="research-header"><div className="assistant-mark"><Icon name="spark" /></div><div><h2>Lead agent</h2><a className="model-settings-link" href="#models">{lead?.model || "Models"} · Models</a></div>
      <button className="icon-button" aria-label="Close research panel" onClick={onClose}><Icon name="close" /></button></div>
    <div className="research-body research-messages" ref={scrollArea} tabIndex={0} role="region" aria-label="Lead agent message history" onScroll={event=>{
      const area=event.currentTarget;
      followLatest.current=area.scrollHeight-area.scrollTop-area.clientHeight<48;
    }}><p><a href="#notebook">Open the agent team and controls</a></p>
      {!state.agent_runtime?.configuration.provider?.configured && <p>Sign in through the agent team panel to begin. Your messages will stay queued.</p>}
      {messages.map(m=><article className={`message ${m.role==='user'?'user':'assistant'}`} key={m.id}><div className="message-meta"><span>{m.role==='user'?'You':'Lead agent'}</span></div><TextContent text={m.content} /></article>)}
      <p><a href="#notebook/conversation">Earlier campaign discussions</a></p>
    </div><form className="research-composer" onSubmit={send}><label className="sr-only" htmlFor="pi-sidebar-message">Message to lead agent</label>
      <textarea id="pi-sidebar-message" rows={3} value={message} onChange={event=>setMessage(event.target.value)} placeholder="Guide the lead agent’s next step…" />
      <div className="composer-footer"><span>Steer current work</span><button className="send-button" disabled={busy||!message.trim()} aria-label="Send message to lead agent"><Icon name="send" size={17} /></button></div>
      <ErrorNotice text={error} /></form>
  </aside>;
}
