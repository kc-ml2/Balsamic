import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

const terminal = new Set(['completed', 'failed', 'paused', 'stopped', 'interrupted']);
const safeID = (id: string) => { if (!/^[a-zA-Z0-9_-]{1,200}$/.test(id)) throw new Error('Invalid identity'); return id; };
function atomic(file: string, value: any) {
  const temp = file + '.tmp'; fs.writeFileSync(temp, JSON.stringify(value), {mode: 0o600}); fs.renameSync(temp, file);
}
function digest(value: any) { return createHash('sha256').update(JSON.stringify(value)).digest('hex'); }
export class Supervisor {
  agents = new Map<string, any>();
  closing = false;
  changed = new Set<string>();
  flushTimer: ReturnType<typeof setTimeout> | null = null;
  // notify(agentIds) tells the workspace which agents to pull; it is a hint, so
  // a lost notice only delays reconciliation until the workspace's next sweep.
  constructor(public directory: string, public factory: any, public gateway: any, public authPath: string,
              public notify: ((ids: string[]) => Promise<unknown> | void) | null = null,
              public reconfigure: ((runtime: any, spec: any) => Promise<void>) | null = null) {
    fs.mkdirSync(directory, {recursive: true, mode: 0o700});
    for (const id of fs.readdirSync(directory)) {
      const file = path.join(directory, id, 'state.json');
      if (!fs.existsSync(file)) continue;
      const agent = JSON.parse(fs.readFileSync(file, 'utf8'));
      const journal = path.join(directory, id, 'events.jsonl');
      if (fs.existsSync(journal)) {
        // The append can survive a crash before the state snapshot does.
        const lines = fs.readFileSync(journal, 'utf8').split('\n').filter(Boolean);
        for (const line of lines) { try { agent.seq = Math.max(agent.seq, JSON.parse(line).seq); } catch { /* torn final append */ } }
      }
      agent.runtime = null; agent.busy = false; agent.specChanged = false;
      // The campaign lead role was once named "pi"; Python migrated its records.
      if (agent.spec?.role === 'pi') agent.spec.role = 'lead';
      if (agent.control === 'interrupted') agent.control = null;
      for (const run of Object.values(agent.runs) as any[]) {
        if (!terminal.has(run.status)) { run.status = 'interrupted'; run.error = 'Harness restarted; inspect durable receipts before continuing.'; }
      }
      this.agents.set(id, agent); this.save(agent);
    }
  }
  save(agent: any) {
    const {runtime, busy, ...persisted} = agent;
    atomic(path.join(this.directory, agent.id, 'state.json'), persisted);
    this.changed.add(agent.id);
    if (this.notify && !this.flushTimer) this.flushTimer = setTimeout(() => this.flush(), 200);
  }
  flush() {
    this.flushTimer = null;
    const ids = [...this.changed]; this.changed.clear();
    if (!ids.length || !this.notify) return;
    try { void Promise.resolve(this.notify(ids)).catch(() => {}); } catch { /* the sweep recovers */ }
  }
  event(agent: any, type: string, data: any = {}) {
    // Never publish hidden reasoning or authentication material.
    const item = {seq: ++agent.seq, type, occurred_at: new Date().toISOString(), ...data};
    fs.appendFileSync(path.join(this.directory, agent.id, 'events.jsonl'), JSON.stringify(item) + '\n', {mode: 0o600});
    this.save(agent);
    return item;
  }
  view(id: string, after = 0) {
    const agent = this.agents.get(safeID(id)); if (!agent) return null;
    const file = path.join(this.directory, id, 'events.jsonl');
    const events = fs.existsSync(file) ? fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean)
      .flatMap(line => { try { return [JSON.parse(line)]; } catch { return []; } }).filter(e => e.seq > after).slice(0, 200) : [];
    return {id, session_id: agent.sessionId, session_file: agent.sessionFile, runs: agent.runs, events, cursor: events.at(-1)?.seq ?? after};
  }
  async submit(id: string, input: any) {
    safeID(id); safeID(input.run_id);
    let agent = this.agents.get(id);
    if (!agent) {
      fs.mkdirSync(path.join(this.directory, id), {recursive: true, mode: 0o700});
      agent = {id, spec: input.spec, seq: 0, runs: {}, sessionFile: null, runtime: null, busy: false};
      this.agents.set(id, agent);
    }
    // The role is the agent's identity. Model, provider and thinking level may change
    // (the workspace enforces which changes are allowed); they apply at the next turn.
    if (agent.spec.role !== input.spec.role) throw new Error('Agent identity cannot change its role');
    if (digest(agent.spec) !== digest(input.spec)) {
      agent.spec = input.spec; agent.specChanged = Boolean(agent.runtime);
      this.event(agent, 'spec.updated', {provider: input.spec.provider, model: input.spec.model, effort: input.spec.effort});
    }
    const requestHash = digest({text: input.text, mode: input.mode || 'follow_up'});
    const old = agent.runs[input.run_id];
    if (old) { if (old.requestHash !== requestHash) throw new Error('Run identity already belongs to another request'); return old; }
    const run = {id: input.run_id, text: input.text, mode: input.mode || 'follow_up', requestHash,
      status: 'queued', created_at: new Date().toISOString(), result: null, deadline_at: input.deadline_at};
    agent.runs[run.id] = run; this.event(agent, 'run.queued', {run_id: run.id});
    if (agent.busy && run.mode === 'steer' && agent.runtime) {
      // Queue once, persist the acknowledgment; the active turn still owns tool
      // receipts. Ordinary follow-ups are scheduled after the active run settles.
      await agent.runtime.steer(`[Researcher steering ${run.id}]\n${run.text}`);
      Object.assign(run, {status: 'completed', result: {delivery: 'steering_queued'}}); this.save(agent);
    } else void this.pump(agent);
    return run;
  }
  async pump(agent: any) {
    if (this.closing || agent.busy || agent.control) return;
    const run: any = Object.values(agent.runs).find((r: any) => r.status === 'queued'); if (!run) return;
    agent.busy = true; run.status = 'running'; this.event(agent, 'run.started', {run_id: run.id});
    let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
    let expired = false;
    try {
      if (run.deadline_at && run.deadline_at * 1000 <= Date.now()) throw new Error('Implementation allocation expired before dispatch; saved work is retained.');
      if (!agent.runtime) {
        const manifest = await this.gateway('manifest', {agent_id: agent.id, run_id: run.id});
        const tools = manifest.tools.map((tool: any) => ({...tool, label: tool.name, parameters: tool.input_schema,
          execute: async (callId: string, args: any, signal: AbortSignal) => {
            const active: any = Object.values(agent.runs).find((r: any) => r.status === 'running');
            if (!active) throw new Error('No active assignment');
            const result = await this.gateway('tool', {agent_id: agent.id, run_id: active.id, call_id: callId, name: tool.name, arguments: args}, signal);
            return {content: [{type: 'text', text: JSON.stringify(result)}], details: {}};
          }}));
        agent.runtime = await this.factory({directory: path.join(this.directory, agent.id), sessionFile: agent.sessionFile,
          authPath: this.authPath, ...agent.spec, instructions: manifest.instructions, tools});
        agent.sessionFile = agent.runtime.sessionFile; agent.sessionId = agent.runtime.sessionId; this.save(agent);
        agent.runtime.subscribe((event: any) => {
          if (event.type === 'message_end' && event.message?.role === 'assistant') {
            const message = event.message;
            const content = message.content.filter((c: any) => c.type === 'text').map((c: any) => c.text).join('\n');
            const usage: any = Object.fromEntries(['input', 'output', 'cacheRead', 'cacheWrite', 'reasoning', 'totalTokens']
              .map(key => [key, message.usage?.[key] || 0]));
            // Pi prices every call from its catalog; whether that is a charge depends on billing.
            usage.cost_usd = message.usage?.cost?.total || 0;
            this.event(agent, 'assistant.message', {text: content, usage, stop_reason: message.stopReason,
              provider: message.provider, model: message.model, thinking_level: message.thinkingLevel ?? agent.runtime?.thinkingLevel,
              billing: agent.runtime?.billing || 'unknown'});
          } else if (['tool_execution_start', 'tool_execution_end', 'auto_compaction_start', 'auto_compaction_end', 'auto_retry_start', 'auto_retry_end'].includes(event.type)) {
            this.event(agent, event.type, {tool: event.toolName, call_id: event.toolCallId, error: event.isError,
              summary: event.type.replaceAll('_', ' ')});
          }
        });
      }
      if (agent.specChanged && this.reconfigure) {
        await this.reconfigure(agent.runtime, agent.spec);
        agent.specChanged = false; this.save(agent);
        this.event(agent, 'model.changed', {provider: agent.spec.provider, model: agent.spec.model, effort: agent.spec.effort});
      }
      if (this.closing || agent.control) {run.status = agent.control || 'interrupted'; return;}
      if (run.deadline_at) deadlineTimer = setTimeout(() => { expired = true; void agent.runtime.abort(); }, Math.max(1, run.deadline_at * 1000 - Date.now()));
      await agent.runtime.prompt(`[Assignment ${run.id}]\n${run.text}`);
      if (expired) throw new Error('Implementation time allocation exhausted; session and artifacts are saved.');
      const last = agent.runtime.messages?.filter((m: any) => m.role === 'assistant').at(-1);
      if (last?.stopReason === 'error') throw new Error(last.errorMessage || 'Provider request failed');
      // abort() can return before prompt() settles. Preserve shutdown's durable
      // interruption marker even if close() has already cleared agent.control.
      run.status = agent.control || (run.status === 'interrupted' || last?.stopReason === 'aborted' ? 'interrupted' : 'completed');
      run.result = {text: agent.runtime.getLastAssistantText() || ''};
    } catch (e: any) {
      run.status = agent.control || (run.status === 'interrupted' ? 'interrupted' : 'failed'); run.error = String(e.message || e).slice(0, 1500);
    } finally {
      if (deadlineTimer) clearTimeout(deadlineTimer);
      run.finished_at = new Date().toISOString(); agent.busy = false;
      this.event(agent, 'run.' + run.status, {run_id: run.id, error: run.error});
      if (!this.closing && !agent.control) void this.pump(agent);
    }
  }
  async control(id: string, action: string) {
    const agent = this.agents.get(safeID(id)); if (!agent) return {status: action};
    if (!['pause', 'resume', 'stop'].includes(action)) throw new Error('Invalid control');
    agent.control = action === 'resume' ? null : action === 'pause' ? 'paused' : 'stopped'; this.save(agent);
    if (action !== 'resume') {
      for (const run of Object.values(agent.runs) as any[]) if (run.status === 'queued') run.status = agent.control;
      await agent.runtime?.abort(); this.save(agent);
    } else void this.pump(agent);
    return {status: agent.control || 'idle'};
  }
  async close() {
    this.closing = true;
    for (const agent of this.agents.values()) {
      if (agent.control) continue; // Preserve an explicit researcher pause/stop.
      agent.control = 'interrupted'; this.save(agent);
      await agent.runtime?.abort();
      for (const run of Object.values(agent.runs) as any[]) if (!terminal.has(run.status)) run.status = 'interrupted';
      agent.control = null; this.save(agent); agent.runtime?.dispose?.();
    }
  }
}
