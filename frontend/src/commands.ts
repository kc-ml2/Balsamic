import { createContext, useContext } from 'react';
import { api, ApiError } from './api';
import type { Campaign, Json } from './api';

export const CommandWorkspaceContext = createContext<string | null>(null);
export const commandJournalEvent = 'optimization-command-journal';
export type PendingCommand = { workspace_id: string; signature: string; request: Json; created_at: string };
const inFlight = new Map<string, Promise<Json>>();
const prefix = (workspaceId: string) => `optimization.commands.v1:${encodeURIComponent(workspaceId)}:`;
const key = (entry: PendingCommand) => `${prefix(entry.workspace_id)}${entry.request.id}`;
const notify = () => window.dispatchEvent(new Event(commandJournalEvent));

function actionUUID(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // Remote HTTP origins expose getRandomValues, but not randomUUID.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function canonical(value: any): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).filter(k => value[k] !== undefined).sort()
    .map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  return JSON.stringify(value);
}

export function pendingCommands(workspaceId: string): PendingCommand[] {
  const entries: PendingCommand[] = [];
  for (let index = 0; index < localStorage.length; index++) {
    const name = localStorage.key(index);
    if (!name?.startsWith(prefix(workspaceId))) continue;
    const entry = JSON.parse(localStorage.getItem(name)!);
    if (entry.workspace_id !== workspaceId || typeof entry.signature !== 'string'
      || typeof entry.request?.id !== 'string' || key(entry) !== name) {
      throw new Error('A saved pending action cannot be read. Its record has been retained.');
    }
    entries.push(entry);
  }
  return entries.sort((a, b) => a.created_at.localeCompare(b.created_at));
}

export const commandIsSending = (entry: PendingCommand) => inFlight.has(key(entry));

function remove(entry: PendingCommand) {
  localStorage.removeItem(key(entry));
  notify();
}

export async function checkPendingCommand(entry: PendingCommand): Promise<Json | null> {
  let accepted: Json;
  try {
    accepted = await api(`/api/v1/commands/${encodeURIComponent(entry.request.id)}`, undefined, 'GET', { 'X-Workspace-Id': entry.workspace_id });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
  if (accepted.actor !== 'researcher' || accepted.status !== 'completed'
    || Object.entries(entry.request).some(([field, value]) => canonical(accepted.request?.[field]) !== canonical(value))) {
    throw new Error('The recorded action differs from this saved request. Its pending record has been retained.');
  }
  remove(entry);
  return accepted;
}

export function retryPendingCommand(entry: PendingCommand, firstSubmission = false): Promise<Json> {
  const running = inFlight.get(key(entry));
  if (running) return running;
  const delivery = (async () => {
    // A missing acknowledgement may hide an accepted command. Checking it is
    // read-only and must not erase a pending request on a transient failure.
    if (!firstSubmission) {
      const accepted = await checkPendingCommand(entry);
      if (accepted) return accepted;
    }
    try {
      const result = await api('/api/v1/commands', entry.request, 'POST', { 'X-Workspace-Id': entry.workspace_id });
      remove(entry);
      return result;
    } catch (error) {
      if (error instanceof ApiError && [400, 403, 404, 409, 422].includes(error.status)) remove(entry);
      throw error;
    }
  })();
  inFlight.set(key(entry), delivery);
  notify();
  // Register cleanup without creating an unhandled rejected promise.
  const finished = () => { inFlight.delete(key(entry)); notify(); };
  delivery.then(finished, finished);
  return delivery;
}

/** Send a researcher command for `campaign`, which need not be the selected one (null to create a campaign). */
export async function sendCommand(workspaceId: string | null | undefined, campaign: Pick<Campaign, 'id' | 'version'> | null, operation: string, payload: Json) {
  const creating = operation === 'campaign.create';
  if (!workspaceId) throw new Error('Refresh the workspace before submitting an action.');
  if (!campaign && !creating) throw new Error('Select a campaign first.');
  // Preconditions describe what was seen, not a new user intent. A refresh
  // cannot turn an uncertain retry into another command with newer revisions.
  const { expected_revision: _revision, expected_control_revision: _control, expected_status_revision: _status, expected_resolution_revision: _resolution, ...intent } = payload;
  const signature = canonical({ campaign_id: creating ? null : campaign?.id, operation, payload: intent });
  let entry = pendingCommands(workspaceId).find(item => item.signature === signature);
  const firstSubmission = !entry;
  if (!entry) {
    const id = actionUUID();
    entry = JSON.parse(JSON.stringify({ workspace_id: workspaceId, signature, created_at: new Date().toISOString(),
      request: { id: `ui_${id}`, campaign_id: creating ? `campaign_${id}` : campaign!.id,
        expected_revision: creating ? 0 : campaign!.version, operation, payload } }));
    // Persist before the network request. Storage failure cannot lose the
    // identity of an action that the server might already have accepted.
    localStorage.setItem(key(entry!), JSON.stringify(entry));
  }
  const accepted = await retryPendingCommand(entry!, firstSubmission);
  return accepted.outcome;
}

export function useCommand(campaign: Campaign | null, explicitWorkspaceId?: string | null) {
  const inheritedWorkspaceId = useContext(CommandWorkspaceContext);
  const workspaceId = explicitWorkspaceId ?? inheritedWorkspaceId;
  return (operation: string, payload: Json) => sendCommand(workspaceId, campaign, operation, payload);
}
