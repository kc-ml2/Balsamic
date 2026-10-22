/** Starting a campaign as a conversation: the setup assistant asks, offers choices, fills the draft; you create. */
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { Composer, shortcutDigit } from '../chat/Composer';
import { Markdown } from '../chat/render/Markdown';
import { Failed, Loading, Pill, when } from '../primitives/ui';
import { navigate } from '../router/routes';
import { track } from '../telemetry/events';

export interface Choice { id: string; label: string; description?: string; patch?: Record<string, unknown> }
export interface Setup { id: string; status: 'asking' | 'ready' | 'created' | 'abandoned'; mode: 'model' | 'scripted'; model: { provider: string; model: string } | null; cost_usd?: number; model_note?: string;
  draft: Record<string, unknown>; transcript: { role: 'user' | 'assistant'; text: string; field?: string; choices?: Choice[]; at?: string }[]; step: { field: string; question: string; choices: Choice[]; free_text: boolean; placeholder?: string } | null; campaign_id: string | null }

export function useSetup(setupId: string | null) {
  return useQuery<Setup>({ queryKey: ['setup', setupId], enabled: !!setupId, queryFn: () => api<Setup>(`/api/v1/setup/${setupId}`), staleTime: 1000 });
}

export function SetupChat({ setupId }: { setupId: string | null }) {
  const queryClient = useQueryClient();
  const setup = useSetup(setupId);
  const scroller = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (setupId) return;
    void api<Setup>('/api/v1/setup', { method: 'POST' }).then(created => {
      track('setup.start', { mode: created.mode });
      if (location.hash.replace(/^#/, '').split('/')[0] === 'new') navigate('#new/' + created.id);   // unless the researcher already went elsewhere
    });
  }, [setupId]);
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight }); }, [setup.data?.transcript.length]);
  const data = setup.data;
  const send = async (body: { text?: string; choice?: string }) => {
    if (!setupId) return;
    setBusy(true); setError(null);
    try {
      const next = await api<Setup>(`/api/v1/setup/${setupId}/answer`, { method: 'POST', body: JSON.stringify(body) });
      queryClient.setQueryData(['setup', setupId], next);
      if (next.status === 'abandoned') { track('setup.abandon', {}); navigate('#new'); return; }
      if (next.status === 'ready') await create();
    } catch (e) { setError(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };
  // Ctrl+1 … Ctrl+9 picks the current step's choices, wherever the focus is on the page.
  const choices = data?.status === 'asking' ? data.step?.choices || [] : [];
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const n = shortcutDigit(e), choice = n ? choices[n - 1] : undefined;
      if (!choice) return;
      e.preventDefault();
      if (!busy) void send({ choice: choice.id });
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
  const startOver = async () => {
    if (!setupId) return;
    try { await api(`/api/v1/setup/${setupId}/abandon`, { method: 'POST' }); } catch { /* a closed setup is already over */ }
    track('setup.abandon', {}); navigate('#new');
  };
  const create = async () => {
    if (!setupId) return;
    const created = await api<{ campaign_id: string; thread_id: string }>(`/api/v1/setup/${setupId}/create`, { method: 'POST' });
    track('setup.create', {});
    await queryClient.invalidateQueries({ queryKey: ['campaigns'] });
    navigate(`#t/${created.thread_id}/${(setup.data?.draft.problem_id) ? 'hypotheses' : 'note'}`);
  };
  if (!setupId || setup.isPending) return <div className="chat"><Loading what="Starting the setup" /></div>;
  if (setup.error) return <div className="chat"><Failed error={setup.error} /></div>;
  const step = data!.step;
  return (
    <div className="chat" data-testid="setup-chat">
      <div className="thread-head"><strong className="grow">New campaign</strong>
        {data!.status !== 'created' ? <button className="btn quiet small" onClick={startOver} data-testid="start-over" title="Abandon this draft and begin again">Start over</button> : null}
        <Pill tone={data!.mode === 'model' ? 'agent' : ''} title={data!.mode === 'model' ? `Questions phrased by ${data!.model?.model}` : 'No model configured: scripted questions'}>{data!.mode === 'model' ? `assistant · ${data!.model?.model}` : 'scripted assistant'}</Pill></div>
      <div className="turns" ref={scroller}>
        {data!.transcript.map((turn, i) => (
          <article key={i} className={'turn ' + (turn.role === 'user' ? 'user' : 'agent')} data-testid="setup-turn">
            <div className="meta"><Pill tone={turn.role === 'user' ? 'you' : 'agent'}>{turn.role === 'user' ? 'You' : 'Setup'}</Pill><span>{when(turn.at)}</span></div>
            <Markdown text={turn.text} thread={null} />
          </article>))}
        {data!.model_note ? <div className="turn lab">{data!.model_note}</div> : null}
        {step && data!.status === 'asking' && step.choices.length ? <div className="row wrap" data-testid="setup-choices" style={{ padding: '0 4px' }}>
          {step.choices.map((c, i) => <button key={c.id} className="btn small" disabled={busy} title={(c.description || c.label) + (i < 9 ? `  (Ctrl+${i + 1})` : '')} onClick={() => send({ choice: c.id })} data-choice={c.id}
            data-shortcut={i < 9 ? i + 1 : undefined} aria-keyshortcuts={i < 9 ? `Control+${i + 1}` : undefined}>{c.label}</button>)}</div> : null}
        {error ? <div className="pill bad">{error}</div> : null}
      </div>
      <Composer key={step?.field} onSend={text => send({ text })} disabled={busy || data!.status !== 'asking' || (step ? !step.free_text && !!step.choices.length : true)}
        why={step && !step.free_text ? 'Pick one of the choices above' : undefined} placeholder={step?.placeholder || 'Type your answer'} testId="setup-composer" autoFocus />
    </div>
  );
}
