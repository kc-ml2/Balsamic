import {useEffect, useState} from 'react';
import {api, errorText, seconds, when} from './api';
import type {Json, State} from './api';
import {Badge, ErrorNotice, Field} from './ui';
import {TextContent} from './textContent';
import {useCommand} from './commands';

function key() {return `dev-${Date.now()}-${Math.random().toString(36).slice(2)}`;}
function delivery(scope:string, payload:unknown) {
  const location=`optimization.development.v1:${scope}`;
  const signature=JSON.stringify(payload);
  const prior=localStorage.getItem(location);
  if(prior){
    try{const saved=JSON.parse(prior);if(saved.signature===signature&&typeof saved.id==='string')return {id:saved.id,done:()=>localStorage.removeItem(location)};}
    catch{/* Replace an unreadable local draft; server-side receipts remain authoritative. */}
  }
  const id=key();localStorage.setItem(location,JSON.stringify({signature,id}));
  return {id,done:()=>localStorage.removeItem(location)};
}

export function DevelopmentPanel({state, refresh}:{state:State; refresh:()=>Promise<void>}) {
  const campaign=state.campaign;
  const command=useCommand(campaign);
  const [hypothesis,setHypothesis]=useState(''),[objective,setObjective]=useState(''),[evidence,setEvidence]=useState('');
  const [selected,setSelected]=useState(''),[draft,setDraft]=useState(''),[mode,setMode]=useState<'steer'|'follow_up'>('steer');
  const [records,setRecords]=useState<Json[]>([]),[events,setEvents]=useState<Json[]>([]),[questions,setQuestions]=useState<Json[]>([]);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const active=records.find(record=>record.id===selected) || records[0];
  const cid=campaign?.id;
  const base=cid ? `/api/campaigns/${encodeURIComponent(cid)}/implementation-workspaces` : '';
  async function load() {
    if(!cid)return;
    const view=await api<Json>(base);
    setRecords(view.workspaces || []);
    const choice=(view.workspaces || []).find((item:Json)=>item.id===selected) || view.workspaces?.[0];
    if(choice){
      setSelected(choice.id);
      const result=await api<Json>(`${base}/${encodeURIComponent(choice.id)}`);
      setEvents(result.events || []);setQuestions(result.workspace?.questions || []);
    } else {setEvents([]);setQuestions([]);}
  }
  useEffect(()=>{void load().catch(failure=>setError(errorText(failure)));
    const timer=window.setInterval(()=>{void load().catch(()=>{/* Keep last saved state during network loss. */});},3000);
    return ()=>window.clearInterval(timer);
  // Refresh for a campaign switch or selected workspace only.
  },[cid,selected]);
  async function create() {
    if(!hypothesis||busy)return;
    setBusy(true);setError('');setNotice('');
    try{
      const request={hypothesis_id:hypothesis,objective:objective.trim()||'Implement this method from its saved evidence and reference code.',
        evidence_ids:evidence.split(/[\s,]+/).filter(Boolean)};
      const receipt=delivery(`create:${cid}:${hypothesis}`,request);
      const result=await api<Json>(base,{...request,request_key:receipt.id});receipt.done();
      setSelected(result.id);setNotice('Implementation workspace created. Its browser IDE will open when ready.');
      await load();await refresh();
    }catch(failure){setError(errorText(failure));}finally{setBusy(false);}
  }
  async function message(question?:Json) {
    if(!active||busy||!draft.trim())return;
    setBusy(true);setError('');setNotice('');
    try{
      const request={message:draft,mode,...(question?{question_id:question.id}:{})};
      const receipt=delivery(`message:${active.id}`,request);
      await api(`${base}/${encodeURIComponent(active.id)}/messages`,{...request,request_key:receipt.id});receipt.done();
      setDraft('');setNotice('Instruction saved for the existing Pi session.');await load();
    }catch(failure){setError(errorText(failure));}finally{setBusy(false);}
  }
  async function control(action:'pause'|'resume'|'stop') {
    if(!active||busy)return;
    setBusy(true);setError('');
    try{
      const request={action,expected_revision:active.control_revision};
      const receipt=delivery(`control:${active.id}`,request);
      await api(`${base}/${encodeURIComponent(active.id)}/controls`,{...request,request_key:receipt.id});receipt.done();
      setNotice(`${action} requested; files and session history are retained.`);await load();
    }catch(failure){setError(errorText(failure));}finally{setBusy(false);}
  }
  async function askPi(submission:Json) {
    if(!active||busy)return;
    setBusy(true);setError('');
    try{
      const envelope=active.validation_envelopes?.[0];
      await command('agent.message',{message:`Review submission ${submission.id} (commit ${submission.commit}) in implementation workspace ${active.id}. Use frozen validation envelope ${envelope?.id || '(prepare one first)'} and only an explicitly available implementation allocation. Report findings and repair requests to the same coding session.`,mode:'steer'});
      setNotice('Validation request sent to the lead agent.');await refresh();
    }catch(failure){setError(errorText(failure));}finally{setBusy(false);}
  }
  if(!state.agent_runtime?.development?.enabled)return null;
  const choices=state.hypotheses.filter(h=>!['archived','finalist'].includes(h.status));
  return <section className="research-progress pi-panel" aria-label="Implementation workspaces">
    <div className="research-progress-heading"><div><h2>Implementation workspaces</h2><Badge>{records.length} sessions</Badge></div></div>
    <p>Each workspace has a full Pi CLI, source editor, Git checkout, development shell, and saved session. Protected validation remains a separate step.</p>
    <details><summary>Start an implementation workspace</summary>
      <Field label="Method"><select value={hypothesis} onChange={event=>setHypothesis(event.target.value)}><option value="">Choose a method</option>
        {choices.map((h,index)=><option value={h.id} key={h.id}>{`H${String(index+1).padStart(2,'0')} · ${h.title}`}</option>)}</select></Field>
      <Field label="Assignment"><textarea rows={3} value={objective} onChange={event=>setObjective(event.target.value)} placeholder="Describe the implementation and acceptance criteria…" /></Field>
      <Field label="Saved evidence IDs (optional)"><input value={evidence} onChange={event=>setEvidence(event.target.value)} placeholder="Exact saved handoff and paper evidence IDs" /></Field>
      <button className="button primary" disabled={busy||!hypothesis} onClick={()=>void create()}>Create workspace</button>
    </details>
    {records.length>0 && <><Field label="Current implementation"><select value={active?.id || ''} onChange={event=>setSelected(event.target.value)}>
      {records.map(record=><option key={record.id} value={record.id}>{record.title} · {record.status}</option>)}</select></Field>
      {active && <><div className="research-progress-actions"><Badge>{active.status}</Badge>
        {active.runtime?.running && !active.runtime?.paused && <a className="button small primary" href={active.ide_url} target="_blank" rel="noreferrer">Open browser IDE</a>}
        <button className="button small secondary" disabled={busy||active.status==='paused'} onClick={()=>void control('pause')}>Pause</button>
        <button className="button small secondary" disabled={busy||active.status==='idle'||active.status==='running'} onClick={()=>void control('resume')}>Resume</button>
        <button className="button small secondary" disabled={busy||active.status==='stopped'} onClick={()=>void control('stop')}>Stop</button></div>
        <p>Development CPU: {seconds(active.usage?.container_cpu_seconds)}{active.cpu_budget_seconds?` / ${seconds(active.cpu_budget_seconds)}`:''} · Pi calls: {active.usage?.subscription_calls || 0}</p>
        {active.error && <ErrorNotice text={active.error} />}
        {active.checkpoint && <details open><summary>Latest checkpoint</summary><TextContent text={active.checkpoint.summary || ''} />
          {active.checkpoint.next_steps && <TextContent text={active.checkpoint.next_steps} />}
          {active.checkpoint.blocker && <TextContent text={active.checkpoint.blocker} />}</details>}
        {questions.map(question=><div key={question.id} className="research-direction"><strong>Agent question</strong><TextContent text={question.question} />
          <button className="button small secondary" disabled={busy||!draft.trim()} onClick={()=>void message(question)}>Reply with message below</button></div>)}
        <Field label="Message the implementation agent"><textarea rows={3} value={draft} onChange={event=>setDraft(event.target.value)} placeholder="Steer the current Pi coding session…" /></Field>
        <div className="research-progress-actions"><select aria-label="Implementation message timing" value={mode} onChange={event=>setMode(event.target.value as 'steer'|'follow_up')}>
          <option value="steer">Steer current work</option><option value="follow_up">Queue follow-up</option></select>
          <button className="button primary" disabled={busy||!draft.trim()} onClick={()=>void message()}>Send to agent</button></div>
        {(active.submissions || []).map((s:Json)=><article className="research-direction" key={s.id}><strong>Committed submission · {s.commit.slice(0,12)}</strong>
          <p><Badge>{s.status}</Badge> {s.manifest?.test_summary}</p>
          {s.validation_outcome && <TextContent text={JSON.stringify(s.validation_outcome)} />}
          {s.status==='submitted' && <button className="button small secondary" disabled={busy} onClick={()=>void askPi(s)}>Ask lead agent to validate</button>}</article>)}
        {(active.validation_envelopes || []).map((envelope:Json)=><p key={envelope.id}>
          Frozen validation envelope <code>{envelope.id}</code> · {envelope.checks?.behavior || 0} behavior, {envelope.checks?.mechanism || 0} state, {envelope.checks?.diagnostic || 0} diagnostic checks.
        </p>)}
        <details><summary>Agent activity · {events.length} recent events</summary><div className="pi-agent-tree" role="log" aria-label="Implementation activity">
          {events.slice(-40).map((event:Json)=><article key={event.id}><small>{when(event.occurred_at)} · {event.type}</small>
            <TextContent text={event.payload?.text || event.payload?.summary || event.payload?.output || event.payload?.error || ''} /></article>)}</div></details>
      </>}
    </>}
    <ErrorNotice text={error} />{notice && <p role="status">{notice}</p>}
  </section>;
}
