/** Shapes of the reads the workspace shows. Kept narrow: pages read what they render. */
export interface WorkspaceInfo { workspace_id: string; max_workers: number; dev_mode: boolean; provider: { configured: boolean; providers: Record<string, unknown> }; level: string | null; service: string }
export type Stage = 'formulate' | 'generate' | 'optimize' | 'report';
export interface NextStep { kind: 'note' | 'sections' | 'adopt' | 'confirm' | 'tasks' | 'evaluator'; title: string; text: string; page: string; blocking: boolean; proposals: number; missing?: string[]; skipped?: string[]; proposed_skips?: string[]; error?: string; problem_id?: string }
export interface CampaignRow { id: string; name: string; version: number; objective?: string; autonomy?: string; stage: Stage; stage_pinned: boolean; stage_basis: string;
  autopilot: { enabled: boolean; compute_cap_seconds?: number | null; report_by?: string | null; enabled_at?: string | null; rounds_completed?: number; requested?: { compute_cap_seconds?: number | null; report_by?: string | null; by?: string } | null }; problem_note_id: string | null; active_thread_id: string; created_at?: string; next_step?: NextStep | null }
export interface Campaign { id: string; name: string; version: number; objective: string; autonomy: string; compute_budget_seconds: number; llm_budget_usd: number;
  delegated_trial_seconds: number; validation_reserve_seconds: number; implementation_compute_budget_seconds: number; active_study_id: string | null; formulating?: boolean; [key: string]: unknown }
export interface Summary { campaign: Campaign; row: CampaignRow; tasks: Record<string, unknown>[]; charter: Record<string, unknown> | null; budget: Record<string, unknown> | null;
  stage: { stage: Stage; pinned: boolean; basis: string }; counts: Record<string, number>; studies: Record<string, unknown>[]; trials: Trial[] }
export interface Scoped { event_cursor: number; unchanged?: boolean; campaign_id?: string }
export interface Thread { id: string; campaign_id: string; name: string; title: string; kind?: 'agents' | null; title_pending?: boolean; root_run_id?: string | null; suggestions?: { items: { label: string; text: string }[]; as_of_message_id: string; model?: string; written_at?: string } | null; parent_thread_id: string | null; fork_message_id: string | null; status: string;
  merged_into: string | null; pinned_ids: string[]; excluded_ids: string[]; anchors?: string[]; summary: ThreadSummary | null; guidance_revision: number; message_count: number;
  last_message_id: string | null; last_activity_at: string | null; busy: boolean; queued: number; unread?: boolean; strip?: { at: string; glyph: string; id: string }[]; merge_draft?: { into_thread_id: string; text: string | null; run_id?: string; written_at?: string | null } | null; created_at: string }
export interface Threads extends Scoped { threads: Thread[]; lead: { id: string; status: string } | null; request_open: boolean; active_run: { id: string; thread_id: string } | null }
export interface Message { id: string; campaign_id: string; role: 'user' | 'assistant' | 'system'; content: string; created_at: string; origin?: string; agent_id?: string; agent_role?: string; fold_title?: string | null; kind?: string; link_thread_id?: string; panel_id?: string; skip_id?: string; to_agent_id?: string;
  thread_id?: string; refs?: string[]; issue_id?: string; run_id?: string; desk?: string; parts?: string[]; merge_of_thread_id?: string; edited_from_message_id?: string; entry_id?: string; [key: string]: unknown }
export interface Question { id: string; agent_id?: string; question: string; status: string; created_at: string; [key: string]: unknown }
export interface Inherited { thread_id: string; title: string; count: number; from: string; to: string }
export interface Placeholder { id: string; batch_id: string; status: string; hypothesis_id: string | null }
export interface Messages extends Scoped { thread_id: string; messages: Message[]; truncated: boolean; inherited: Inherited[]; inherited_messages: Message[]; parent_path: { thread_id: string; title: string; fork_message_id: string | null }[]; fork_message_id: string | null; questions: Question[]; placeholders: Placeholder[]; status: string; merged_into: string | null; summary: ThreadSummary | null }
export interface ThreadSummary { text: string; strip: { at: string; glyph: string; id: string; who?: string; operation?: string }[]; as_of_message_id: string | null; written_by: 'model' | 'activity_only'; stale: boolean; edited: boolean; reason?: string; written_at?: string; model?: string }
export interface Trial { id: string; campaign_id: string; task_id: string; algorithm: string; status: string; seed?: number; max_steps?: number; wall_seconds?: number; created_at?: string;
  progress?: Record<string, unknown>; result?: Record<string, unknown>; stopped_by?: string; hidden?: boolean; hypothesis_id?: string; [key: string]: unknown }
export interface Experiments extends Scoped { short: Trial[]; long: Trial[]; checks: Trial[]; drafts: Record<string, unknown>[]; rules: Record<string, unknown>; profiles: Record<string, unknown>[] }
export interface Hypothesis { id: string; title: string; short_title?: string | null; status: string; mechanism?: string; rationale?: string; algorithm?: string; algorithm_chosen?: boolean; origin?: string; parent_ids?: string[];
  reviews?: { id: string; author: string; verdict?: string; text: string; created_at: string }[]; readiness?: { state: string; eligible: boolean; reason?: string }; created_at?: string; [key: string]: unknown }
export interface Hypotheses extends Scoped { hypotheses: Hypothesis[]; aliases: { record_id: string; label: string }[]; rounds: Record<string, unknown> }
export interface Decision { id: string; title: string; proposal?: string; rationale?: string; status: string; options: { id: string; label: string; description?: string }[]; recommendation?: string;
  resolution_revision?: number; requested_by?: string; created_at: string; freshness: { state: string; reason: string; blocked_choice_ids: string[] }; presentation: Record<string, unknown>; [key: string]: unknown }
export interface Issue { id: string; code: string; message: string; status: string; revision: number; options: string[]; created_at: string; affected?: string;
  actions?: { id: string; label: string; description: string }[] }
export interface Inbox extends Scoped { decisions: Decision[]; issues: Issue[]; questions: Question[]; resolved: Decision[] }
export interface Monitor extends Scoped { rows: { problem: Record<string, unknown>; budget: { worker: Record<string, unknown>; llm: Record<string, unknown> }; compute: Record<string, unknown>;
  agents: Record<string, unknown>; autopilot?: Record<string, unknown>; needs_you: { decisions: number; issues: number; questions: number } }; pill: { state: 'clear' | 'attention' | 'digest'; text: string; row: string | null }; attention?: { row: string; text: string }[]; digest_id: string | null }
export interface Files extends Scoped { assets: Record<string, unknown>[]; findings: Record<string, unknown>[]; reports: Record<string, unknown>[]; report_jobs: Record<string, unknown>[]; sources: Record<string, unknown>[]; artifacts: Record<string, unknown>[] }
export interface CommandReceipt { id: string; campaign_id: string; status: string; outcome: Record<string, unknown>; actor: string; origin: unknown; request: { operation: string; payload: Record<string, unknown> }; created_at: string }
export interface EventFrame { last_event_id: number; events: { id: number; kind: string; record_id: string | null; campaign_id: string | null; status?: string | null; revision?: number | null }[] }

export interface PanelComponent { type: 'markdown' | 'kv' | 'table' | 'number' | 'select' | 'chart' | 'button' | 'link' | 'section'; id?: string; [key: string]: unknown }
export interface Panel { id: string; campaign_id: string; title: string; page: string; placement: 'top' | 'bottom'; note?: string; components: PanelComponent[]; author: string; agent_id?: string | null;
  run_id?: string | null; thread_id?: string | null; revision: number; status: string; created_at: string; updated_at: string; data: Record<string, Record<string, unknown>>; resolved_at?: string }
export interface Panels { campaign_id: string; panels: Panel[] }
