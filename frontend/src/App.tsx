import { GeneralOverview, GeneralProblem, GeneralExperiments } from './problemViews';
import { GeneralComparison, AssetLibrary, ValidationView, StudyView } from './evidenceViews';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, emptyState, errorText, seconds } from './api';
import type { Hypothesis, Json, State, Trial } from './api';
import { ErrorNotice, Icon } from './ui';
import { DraftList } from './experimentForm';
import { CampaignForm, HypothesisForm, TrialActionForm, TrialForm, campaignUsage } from './forms';
import { Comparison, Decisions, Experiments, Hypotheses, Overview, Problem } from './views';
import type { WorkspaceActions } from './views';
import { Notebook, ResearchPanel } from './research';
import { CampaignMemoryView, ImplementationLibrary, ImplementationRequest } from './implementations';
import { CommandWorkspaceContext, useCommand } from './commands';
import { PendingCommands } from './pendingCommands';
import { ModelControlPanel } from './models';
import { ResearchProgress, researchRequestNotice } from './researchProgress';
import { ReportsView } from './reports';
import { ResourceDashboard } from './resourceDashboard';
import { LlmUsageView } from './llmUsage';

const navigation = [
  ['overview', 'Overview', 'overview'], ['problem', 'Problem workbench', 'problem'],
  ['hypotheses', 'Hypotheses', 'hypothesis'], ['experiments', 'Experiments', 'experiments'],
  ['drafts', 'Experiment drafts', 'experiments'], ['comparison', 'Compare results', 'comparison'], ['decisions', 'Decision inbox', 'decisions'],
  ['studies', 'Studies', 'problem'], ['resources', 'Resources', 'comparison'], ['llm-usage', 'LLM usage', 'spark'], ['validation', 'Validation', 'check'], ['assets', 'Research assets', 'branch'],
  ['implementations', 'Implementations', 'branch'], ['memory', 'Campaign memory', 'notebook'],
  ['notebook', 'Research notebook', 'notebook'], ['reports', 'Reports', 'notebook'], ['models', 'Models', 'spark'],
];
type ModalState = { type: 'implementation'; hypothesis: Hypothesis } | { type: 'campaign'; edit?: boolean } | { type: 'hypothesis'; parent?: Hypothesis } | { type: 'trial'; hypothesis?: Hypothesis; taskId?: string; draft?: Json } | { type: 'trialAction'; trial: Trial; action: 'extend' | 'validate' | 'prioritize' } | null;
function hashView() { const view = location.hash.slice(1).split('/')[0]; return navigation.some(([id]) => id === view) ? view : 'overview'; }

export default function App() {
  const [state, setState] = useState<State>(emptyState), [campaignId, setCampaignId] = useState('');
  const command = useCommand(state.campaign, state.workspace_id);
  const [view, setView] = useState(hashView);
  const [connected, setConnected] = useState(false), [loading, setLoading] = useState(true), [error, setError] = useState('');
  const [modal, setModal] = useState<ModalState>(null), [chat, setChat] = useState(window.innerWidth > 1200), [sidebar, setSidebar] = useState(false);
  const [toast, setToast] = useState(''), [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const mounted = useRef(true), requestId = useRef(0), toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const latestState = useRef<State>(emptyState);
  const selectedCampaign = useRef(campaignId);
  selectedCampaign.current = campaignId;
  const refreshing = useRef<{ campaignId: string; promise: Promise<void>; followup: Promise<void> | null } | null>(null);
  const refresh = useCallback((options: { background?: boolean } = {}): Promise<void> => {
    if (!mounted.current || selectedCampaign.current !== campaignId) return Promise.resolve();
    const active = refreshing.current;
    if (active?.campaignId === campaignId) {
      // Polls share the current read. A user action or event needs a read that
      // starts after that read, which may have captured pre-command state.
      if (options.background) return active.promise;
      if (!active.followup) active.followup = active.promise.then(() => {
        if (mounted.current && selectedCampaign.current === campaignId) return refresh({ background: true });
      });
      return active.followup;
    }
    const request = ++requestId.current;
    const flight = { campaignId, promise: Promise.resolve(), followup: null as Promise<void> | null };
    refreshing.current = flight;
    const current = () => mounted.current && selectedCampaign.current === campaignId && request === requestId.current;
    flight.promise = (async () => {
      try {
        const data = await api<State>(`/api/state${campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : ''}`);
        if (current()) { latestState.current = { ...emptyState, ...data, campaign: data.campaign ? { ...data.campaign, compute_used_seconds: data.budget?.spent_seconds ?? data.campaign.compute_used_seconds, llm_used_usd: data.budget?.llm_spent_usd ?? data.campaign.llm_used_usd } : null }; setState(latestState.current); setError(''); setUpdatedAt(new Date()); setLoading(false); }
      } catch (e) { if (current()) { setError(errorText(e)); setLoading(false); } }
      finally { if (refreshing.current === flight) refreshing.current = null; }
    })();
    return flight.promise;
  }, [campaignId]);
  useEffect(() => { mounted.current = true; void refresh({ background: true }); const interval = window.setInterval(() => void refresh({ background: true }), 6000); return () => { mounted.current = false; window.clearInterval(interval); }; }, [refresh]);
  useEffect(() => {
    let pending: ReturnType<typeof setTimeout> | undefined;
    const stream = new EventSource('/api/events');
    stream.onopen = () => setConnected(true);
    stream.onerror = () => setConnected(false);
    const update = () => { if (!pending) pending = setTimeout(() => { pending = undefined; void refresh(); }, 400); };
    stream.addEventListener('update', update);
    stream.onmessage = update;
    return () => { stream.close(); clearTimeout(pending); };
  }, [refresh]);
  useEffect(() => { const change = () => { const v = location.hash.slice(1).split('/')[0]; if (navigation.some(([id]) => id === v)) setView(v); }; window.addEventListener('hashchange', change); return () => window.removeEventListener('hashchange', change); }, []);
  useEffect(() => () => clearTimeout(toastTimer.current), []);
  function notify(message: string) { setToast(message); clearTimeout(toastTimer.current); toastTimer.current = setTimeout(() => setToast(''), 6000); }
  function navigate(value: string) { setView(value); location.hash = value; setSidebar(false); }
  async function research(message: string, mode: string, hypothesisId?: string, feedbackReviewIds?: string[], proposal?: Json): Promise<boolean> {
    if (!state.campaign) return false; setChat(true);
    try {
      const outcome = await command('research.start', { message, mode, hypothesis_id: hypothesisId, feedback_review_ids: feedbackReviewIds, ...proposal });
      await refresh(); notify(researchRequestNotice(latestState.current, outcome)); return true;
    } catch (e) { notify(`Research request failed: ${errorText(e)}`); return false; }
  }
  const actions: WorkspaceActions = { navigate, refresh, notify, research, implementation: hypothesis => { setChat(true); setModal({ type: 'implementation', hypothesis }); }, campaign: edit => setModal({ type: 'campaign', edit }), hypothesis: parent => setModal({ type: 'hypothesis', parent }), editDraft: draft => setModal({ type: 'trial', draft }), launch: (hypothesis, taskId) => setModal({ type: 'trial', hypothesis, taskId }), trialAction: (trial, action) => setModal({ type: 'trialAction', trial, action }) };
  function done(message: string, result?: Json, newCampaign = false) { setModal(null); notify(message); if (newCampaign && result?.id) setCampaignId(result.id); else void refresh(); }
  const pending = state.decisions.filter(d => d.status === 'pending').length, usage = campaignUsage(state.campaign, state.trials);
  return <CommandWorkspaceContext.Provider value={state.workspace_id || null}><div className={`app ${chat ? 'chat-open' : ''} ${sidebar ? 'nav-open' : ''}`}><a className="skip-link" href="#workspace-content">Skip to workspace</a>{sidebar && <button className="nav-scrim" aria-label="Close navigation" onClick={() => setSidebar(false)} />}<aside className="sidebar"><a className="brand" href="#overview" onClick={() => navigate('overview')}><span className="brand-mark"><i /><i /><i /><i /><i /></span><div>Optimization Lab<span>RESEARCH WORKSPACE</span></div></a><div className="campaign-switcher"><label htmlFor="campaign-picker">CAMPAIGN</label><select id="campaign-picker" aria-label="Active campaign" value={state.campaign?.id || ''} onChange={e => setCampaignId(e.target.value)}>{!state.campaign && <option value="">No campaign yet</option>}{state.campaigns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select><button className="new-campaign-link" disabled={!state.workspace_id} onClick={() => setModal({ type: 'campaign' })}><Icon name="plus" size={13} />New campaign</button></div><nav aria-label="Workspace navigation">{navigation.map(([id, label, icon]) => <button key={id} className={view === id ? 'active' : ''} onClick={() => navigate(id)} aria-current={view === id ? 'page' : undefined}><Icon name={icon} size={19} /><span>{label}</span>{id === 'decisions' && pending > 0 && <span className="nav-count">{pending}</span>}</button>)}</nav><div className="sidebar-bottom"><div className="sidebar-budget"><div><span>CAMPAIGN COMPUTE</span><Icon name="clock" size={14} /></div><strong>{seconds(usage.compute)}<span> / {state.campaign ? seconds(state.campaign.compute_budget_seconds) : '—'}</span></strong><div className="meter"><i style={{ width: `${state.campaign ? Math.min(100, usage.compute / state.campaign.compute_budget_seconds * 100) : 0}%` }} /></div><p>{state.campaign ? 'Budget enforced across all workers' : 'Create a campaign to allocate resources'}</p></div><div className="local-status"><span className={`dot ${error ? 'red' : 'green'}`} /><span>Local workspace</span><span className="version">v0.2</span></div></div></aside><div className="main-shell"><header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setSidebar(true)}><Icon name="menu" /></button><span>Workspace</span><Icon name="chevron" size={13} /><strong>{navigation.find(([id]) => id === view)?.[1]}</strong></div><div className="topbar-right"><span className="connection-status" title="Workspace update connection. Agent work is shown in the research progress panel."><i className={`dot ${connected && !error ? 'green' : 'amber'}`} />{error ? 'Connection issue' : connected ? 'Connected' : 'Polling'}{updatedAt && <time title="Last successful state update">{updatedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>}</span><button className={`button small ${chat ? 'chat-toggle active' : 'secondary'}`} onClick={() => setChat(!chat)} aria-expanded={chat}><Icon name="spark" size={16} />{state.agent_runtime?.configuration?.enabled ? 'Lead agent' : 'Campaign manager'}</button><span className="user-avatar" title="Local researcher">R</span></div></header><main id="workspace-content" className="workspace" tabIndex={-1}><PendingCommands workspaceId={state.workspace_id} campaigns={state.campaigns} refresh={refresh} />{!loading && view !== 'resources' && <ResearchProgress state={state} refresh={refresh} connectionError={error} />}{error && <div className="connection-error"><ErrorNotice text={`Unable to update the workspace: ${error}`} /><button className="button small secondary" onClick={() => void refresh()}><Icon name="refresh" size={14} />Retry</button></div>}{view === 'resources' ? <ResourceDashboard campaignId={campaignId || state.campaign?.id || undefined} /> : view === 'llm-usage' ? <LlmUsageView campaignId={campaignId || state.campaign?.id || undefined} /> : loading ? <div className="loading-state"><span className="loading-spinner" /><h2>Opening the research workspace</h2><p>Connecting to the experiment service…</p></div> : view === 'overview' ? (state.tasks.some(t => t.problem) ? <GeneralOverview state={state} actions={actions} /> : <Overview state={state} actions={actions} />) : view === 'problem' ? (state.tasks.some(t => t.problem) ? <GeneralProblem state={state} actions={actions} /> : <Problem state={state} actions={actions} />) : view === 'drafts' ? <DraftList state={state} actions={actions} /> : view === 'hypotheses' ? <Hypotheses state={state} actions={actions} /> : view === 'experiments' ? (state.tasks.some(t => t.problem) ? <GeneralExperiments state={state} actions={actions} /> : <Experiments state={state} actions={actions} />) : view === 'comparison' ? (state.tasks.some(t => t.problem) ? <GeneralComparison state={state} actions={actions} /> : <Comparison state={state} actions={actions} />) : view === 'studies' ? <StudyView state={state} actions={actions} /> : view === 'validation' ? <ValidationView state={state} actions={actions} /> : view === 'assets' ? <AssetLibrary state={state} actions={actions} /> : view === 'decisions' ? <Decisions state={state} actions={actions} /> : view === 'implementations' ? <ImplementationLibrary state={state} refresh={refresh} discuss={message => void research(message, 'discuss')} /> : view === 'reports' ? <ReportsView key={state.campaign?.id || 'no-campaign'} state={state} /> : view === 'models' ? <ModelControlPanel key={state.campaign?.id || 'no-campaign'} state={state} refresh={refresh} /> : view === 'memory' ? <CampaignMemoryView state={state} refresh={refresh} /> : <Notebook state={state} refresh={refresh} />}</main><footer className="workspace-footer"><span>OPTIMIZATION LAB</span><p>Ideas are hypotheses. Curves are observations. Decisions are yours.</p><span>Optimizer research</span></footer></div>{chat && <ResearchPanel state={state} refresh={refresh} onClose={() => setChat(false)} />}{toast && <div className="toast" role="status"><Icon name="check" size={17} /><span>{toast}</span><button className="icon-button" aria-label="Dismiss notification" onClick={() => setToast('')}><Icon name="close" size={15} /></button></div>}{modal?.type === 'implementation' && <ImplementationRequest hypothesis={modal.hypothesis} state={state} onClose={() => setModal(null)} onDone={() => { void refresh(); notify('Implementation request recorded with the campaign manager.'); }} />}{modal?.type === 'campaign' && <CampaignForm state={state} editing={modal.edit} onClose={() => setModal(null)} onDone={result => done(modal.edit ? 'Charter revision saved.' : 'Campaign created. Your workspace is ready.', result, !modal.edit)} />}{modal?.type === 'hypothesis' && <HypothesisForm state={state} parent={modal.parent} onClose={() => setModal(null)} onDone={() => done('Hypothesis saved with its rationale and lineage.')} />}{modal?.type === 'trial' && <TrialForm state={state} hypothesis={modal.hypothesis} taskId={modal.taskId} draft={modal.draft} onSaved={() => { void refresh(); }} onDiscuss={message => { setModal(null); void research(message, 'discuss'); }} onClose={() => setModal(null)} onDone={() => { done('Experiment queued. Open Experiments to follow its progress.'); navigate('experiments'); }} />}{modal?.type === 'trialAction' && <TrialActionForm trial={modal.trial} campaign={state.campaign} action={modal.action} onClose={() => setModal(null)} onDone={() => done(modal.action === 'validate' ? 'Physical validation requested.' : 'Trial allocation updated.')} />}</div></CommandWorkspaceContext.Provider>;
}
