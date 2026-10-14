import { api } from './api';
import type { Campaign, Json, State } from './api';
import { sendCommand } from './commands';

/** What the researcher asks the lead agent when a campaign starts from a description. It is sent as their message. */
export const REVIEW_REQUEST = 'Review the problem description in cell problem, and the documents attached to this campaign if there are any. '
  + 'Keep my words as they are and add your restatement. Say what kind of problem this appears to be. '
  + 'Write sets, decisions, objective and constraints only as far as the description supports them. '
  + 'Declare every given or assumed number as a parameter, and put what you cannot reasonably assume into open cells, one question each. '
  + 'Reply with what you wrote and link the cells.';

export type ReviewOutcome = 'sent' | 'no_model';

/** Ask the campaign's lead agent for the review, starting the agent team first if it has none. Sends nothing without a model. */
export async function startReview(workspaceId: string | null | undefined, campaign: Pick<Campaign, 'id'>): Promise<ReviewOutcome> {
  const state = await api<State>(`/api/state?campaign_id=${encodeURIComponent(campaign.id)}`);
  const runtime: Json | undefined = state.agent_runtime, current = state.campaign;
  if (!current) throw new Error('The campaign could not be read; ask for the review from the Formulation page.');
  if (!runtime?.configuration?.enabled) {
    if (!runtime?.lead_model) return 'no_model';
    await sendCommand(workspaceId, current, 'agent.activate', {});
  }
  await sendCommand(workspaceId, current, 'agent.message', { message: REVIEW_REQUEST, mode: 'steer' });
  return 'sent';
}
