import { useEffect, useState } from 'react';
import { errorText, when } from './api';
import type { Campaign } from './api';
import { ErrorNotice } from './ui';
import { checkPendingCommand, commandIsSending, commandJournalEvent, pendingCommands, retryPendingCommand } from './commands';
import type { PendingCommand } from './commands';

function label(entry: PendingCommand) {
  const request = entry.request;
  if (request.operation === 'trial.control') return `${request.payload.action} experiment`;
  const names: Record<string, string> = { 'campaign.create': 'Create campaign', 'campaign.update': 'Revise campaign',
    'context.edit': 'Save campaign guidance', 'issue.resolve': 'Resolve manager issue', 'trial.create': 'Queue experiment',
    'trial.validate': 'Check physical convergence', 'draft.save': 'Save experiment draft', 'draft.launch': 'Launch experiment',
    'study.create': 'Create linked study', 'validation.run': 'Run validation', 'validation.waive': 'Record validation waiver', 'runtime.profile': 'Measure evaluator speed',
    'hypothesis.create': 'Create idea', 'hypothesis.review': 'Save researcher comment', 'hypothesis.status': 'Change idea status',
    'formulation.edit': 'Save formulation notebook' };
  return names[request.operation] || request.operation.replaceAll('.', ' ').replaceAll('_', ' ');
}

export function PendingCommands({ workspaceId, campaigns, refresh }: { workspaceId?: string; campaigns: Campaign[]; refresh: () => Promise<void> }) {
  const [entries, setEntries] = useState<PendingCommand[]>([]), [error, setError] = useState('');
  const [missing, setMissing] = useState<string[]>([]), [busy, setBusy] = useState<string | null>(null), [notice, setNotice] = useState('');
  useEffect(() => {
    setMissing([]); setNotice(''); setError('');
    function read() {
      try { setEntries(workspaceId ? pendingCommands(workspaceId).filter(entry => !commandIsSending(entry)) : []); }
      catch (e) { setError(errorText(e)); }
    }
    read(); window.addEventListener(commandJournalEvent, read); window.addEventListener('storage', read);
    return () => { window.removeEventListener(commandJournalEvent, read); window.removeEventListener('storage', read); };
  }, [workspaceId]);
  async function reconcile(entry: PendingCommand, retry: boolean) {
    setBusy(entry.request.id); setError(''); setNotice('');
    try {
      const accepted = retry ? await retryPendingCommand(entry) : await checkPendingCommand(entry);
      if (accepted) { setNotice(`${label(entry)} was recorded. The workspace now shows its current state.`); await refresh(); }
      else setMissing(current => [...new Set([...current, entry.request.id])]);
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(null); }
  }
  if (!entries.length && !error && !notice) return null;
  return <section className="pending-commands" aria-label="Unconfirmed actions">
    {!!entries.length && <><h2>Check an interrupted action</h2><p>A reply was not confirmed. Check the recorded result before retrying.</p></>}
    {entries.map(entry => <div key={entry.request.id} className="pending-command row-between">
      <div><strong>{label(entry)}</strong><p>{campaigns.find(c => c.id === entry.request.campaign_id)?.name || entry.request.payload.name || 'Campaign action'} · {when(entry.created_at)}</p>
        {missing.includes(entry.request.id) && <p>No accepted result is recorded yet. Retrying keeps the original request and limits.</p>}</div>
      <div className="inline-actions"><button className="button small secondary" disabled={busy !== null} onClick={() => void reconcile(entry, false)}>Check result</button>
        {missing.includes(entry.request.id) && <button className="button small primary" disabled={busy !== null} onClick={() => void reconcile(entry, true)}>Retry original action</button>}</div>
    </div>)}
    <ErrorNotice text={error} />
    {notice && <div className="row-between"><p role="status">{notice}</p><button className="text-button" onClick={() => setNotice('')}>Dismiss</button></div>}
  </section>;
}
