import type { Message } from '../api/types';
import { Markdown } from './render/Markdown';
import { Pill, when } from '../primitives/ui';
import { formatHash, withThread } from '../router/routes';
import { useRead } from '../api/queries';
import type { Panel } from '../api/types';
import { PanelView } from '../panels/PanelView';
import { SkipCard } from '../note/Skips';

const ORIGIN_LABEL: Record<string, string> = { researcher: 'You', lead: 'Lead', page_agent: 'Agent', autopilot: 'Autopilot', lab_event: 'Lab', manager_system: 'Lab', pi: 'Lead', setup: 'Setup' };

export function Turn({ message, thread, aliases, onFork, onEdit }: { message: Message; thread: string | null; aliases?: Record<string, string>; onFork?: (message: Message) => void; onEdit?: (message: Message) => void }) {
  const user = message.role === 'user' && message.origin !== 'lab_event';
  const lab = message.origin === 'lab_event' || message.origin === 'manager_system' || !!message.issue_id;
  const role = message.agent_role && message.agent_role !== 'lead' ? message.agent_role.replaceAll('_', ' ') : null;
  const who = user ? 'You' : role || ORIGIN_LABEL[String(message.origin)] || (message.role === 'assistant' ? 'Lead' : message.role);
  if (message.kind === 'panel_link' && message.panel_id) return <PanelCard message={message} thread={thread} />;
  if (message.kind === 'note_skip' && message.skip_id) return <SkipCard campaignId={message.campaign_id} skipId={message.skip_id} thread={thread} />;
  if (message.kind === 'agents_link' && message.link_thread_id) {
    return (
      <article className="turn lab agents-link" data-message-id={message.id} data-testid="agents-link">
        <Pill tone="agent">agents</Pill> <span>{message.content.replace(/:\s*[^:]*$/, '')}: </span>
        <a href={formatHash({ page: 'hypotheses' }, message.link_thread_id)} data-testid="open-agents-thread">open the side conversation</a>
      </article>
    );
  }
  return (
    <article className={'turn ' + (user ? 'user' : lab ? 'lab' : 'agent')} data-message-id={message.id} data-testid="turn">
      <div className="meta">
        <Pill tone={user ? 'you' : lab ? '' : 'agent'}>{who}</Pill>
        {message.to_agent_id && message.origin === 'lead' ? <span className="faint small">to an agent</span> : null}
        <span>{when(message.created_at)}</span>
        {message.refs?.length ? <span className="faint">· {message.refs.length} refs</span> : null}
        {message.merge_of_thread_id ? <Pill>merged branch</Pill> : null}
        {message.edited_from_message_id ? <Pill title="An edited resend; the original stays in the parent thread">edited</Pill> : null}
        {onFork && !lab ? <button className="btn quiet small" onClick={() => onFork(message)} title="Start a branch of the conversation from this message" data-testid="fork-here">Fork here</button> : null}
        {onEdit && user ? <button className="btn quiet small" onClick={() => onEdit(message)} title="Edit and resend as a new branch" data-testid="edit-resend">Edit</button> : null}
      </div>
      {lab ? <div>{message.content}</div> : <Markdown text={message.content} thread={thread} aliases={aliases} />}
    </article>
  );
}

/** A panel the lead composed in this conversation: shown inline, with a link to the page it belongs to. */
function PanelCard({ message, thread }: { message: Message; thread: string | null }) {
  const panel = useRead<Panel>(['panel', message.panel_id], `/api/v1/panels/${message.panel_id}`, { refetchInterval: 15000 });
  return (
    <article className="turn lab panel-card" data-message-id={message.id} data-testid="panel-card">
      <div className="row"><Pill tone="agent">panel</Pill><span className="grow">{message.content}</span>{panel.data ? <a href={withThread(panel.data.page, thread)} data-testid="open-panel-page">open the page</a> : null}</div>
      {panel.data ? <PanelView panel={panel.data} threadId={thread} compact /> : <span className="faint small">{panel.error ? 'This panel is gone.' : 'Loading the panel…'}</span>}
    </article>
  );
}
