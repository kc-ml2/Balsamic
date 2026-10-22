# Balsamic workspace: the chat-centred UI and its service

Status: milestone M0 (foundations) built on branch `workspace-rebuild`, 2026-10-19. This document is the reference for the
rebuilt service and frontend. It supersedes `workspace.md`, `cli-research.md`, the discovery documents, the model control
panel, decision reconsideration, proposal exploration and the consolidation plans, each of which now carries a banner
pointing here. The scientific core (problem adapters, optimizers, scheduler and workers, races, confirmation, admission,
the implementation service, assets and accounting), the Pi agent tools and the report writer are unchanged; their own
documents remain current.

## What changed and why

The previous workspace had sixteen views behind a hash router, one `/api/state` blob, a legacy Codex "campaign manager"
beside the Pi lead agent, and research modes that duplicated what a message to the lead does. The UI plan asks for a
chat-centred workspace: the lead agent is the control surface, its replies link to the page worth looking at, the
researcher formulates the problem with it, chats branch and merge, a strategy evolution graph shows how candidates were
generated, reviewed, killed, promoted, mutated and merged, a monitor pill waits in the top bar, and a strategy library
carries winners forward. Rather than bend the old shell into that shape, the service layer and the frontend were rebuilt
from an empty directory over the ported core.

Removed: the campaign manager (`research.start/control/retry`, research runs, decision refresh, context import, model
policy commands), the discovery controller and its records, the Codex CLI transport, `/api/state`, `/api/events`, every
compatibility route, and the `dqn_meent.workspace` grating-lab service. The Pi lead is the only chat backend.

Kept rules: viewing never acts; honest labels (measured vs forecast, unknown is not zero, stopped is not failed); a disabled
control says why; records are append-only; only the researcher grants limits, waivers, the worker ceiling, confirmations
and removals; there is no automatic paid fallback; the lead acts on requests (autopilot, in M3, is the explicit exception).

## Layout

```
src/optimization_framework/   the ported core: execution/ evaluation/ optimizers/ implementations/ assets/ analysis/
                              storage/ problem_charter/ problem_import/ reports/ contracts/ agents/ campaigns/ research/ llm/
src/balsamic/                 the service: app.py (create_app, main) · workspace.py · guidance.py · commands.py
                              threads/ (records, AG-UI stream) · evolution/stages.py · monitor/rows.py
                              telemetry/ (ui_event records, gate signals) · api/ (one module per concern)
agent-harness/                the Node Pi harness: supervisor.ts, runtime.ts, server.ts, sessions.ts (session tree)
frontend/                     the React workspace: src/shell, chat, router (pages.json), api, commands, telemetry, pages
tests/balsamic/               tests of the service layer
```

`optimization_framework` never imports `balsamic` (`tests/balsamic/test_layering.py`). `balsamic.app:main` is the
`optimization-lab` entry point with the same flags as before (`--directory --host --port --workers --frontend-directory`),
so `scripts/labctl` and `deploy/*.json` keep working.

## Pages and addresses

`frontend/src/router/pages.json` is the one catalog of pages. The frontend router and the lead's page links
(`optimization_framework/agents/pages.py`) both read it, so a page cannot exist on one side only.

| Rail page | Sub pages | Agent desk |
|---|---|---|
| `note` (Problem note) | charter, revisions | |
| `hypotheses` | rounds, implementations | discussion moderator; implementation lead on `implementations` |
| `experiments` | checks, rules, drafts; one run: `#experiments/<trial_id>` | experiment operator |
| `evolution` | frontier, lineage, curves, races | |
| `files` | assets, reports, notebook, sources | |
| `inbox` | | |
| `monitor` | resources, usage, races, agents, log, models, guidance, digest | resource planner on `resources` |
| `library` | | |
| `branches` | | |

Addresses are `#t/<thread>/<page>[/<sub>][/<id>]?sel=<id>&tab=`. A thread-less address (what the lead writes, e.g.
`#experiments/trial_x`) opens in the current thread. `#new` starts a campaign. Every old address still works: the
`aliases` block of `pages.json` maps `#overview`, `#decisions`, `#problem`, `#implementations`, `#comparison`, `#studies`,
`#validation`, `#assets`, `#notebook`, `#reports`, `#resources`, `#agents[/usage|/log|/models|/guidance]`, `#discussions`,
`#drafts`, `#models`, `#llm-usage`, `#memory` and `#problem-import` to their current pages, and `/reports/<id>` stays a
server-rendered page.

The shell: a top bar (campaign switcher, stage badge, search and autopilot placeholders that say when they arrive, the
monitor pill, Expand), the rail, the conversation with the lead on the left and the stage panel with the current page on
the right. Expand hides the chat; the choice is kept per browser.

## Reads

Every page read is scoped: it takes `?after=<event cursor>` and answers `{unchanged: true, event_cursor}` when the campaign
has no newer event. One SSE stream, `GET /api/v1/events?campaign_id=`, carries frames of
`{id, kind, record_id, campaign_id, status, revision}`; the frontend refetches the pages that show those kinds.

| Route | Returns |
|---|---|
| `GET /api/v1/workspace` | workspace id, worker ceiling, dev mode, provider, resource level |
| `GET /api/v1/campaigns` | rows with `stage` (formulate, generate, optimize, report; researcher pin overrides), autopilot, main thread |
| `GET /api/v1/campaigns/{id}/summary` | campaign, tasks, charter, budget, stage, counts, studies, trials (the CLI's state) |
| `GET /api/v1/campaigns/{id}/threads` | threads with message counts, the lead, the active run |
| `GET /api/v1/threads/{id}` · `/messages?after&before&limit` · `/stream` | the thread, its messages and open questions, the lead's turn as AG-UI events |
| `GET /api/v1/campaigns/{id}/experiments` · `/api/v1/experiments/{trial}[/metrics|/logs|/artifacts/{name}|/problem]` | runs by kind (short, long, checks), drafts, rules; one run |
| `GET /api/v1/campaigns/{id}/hypotheses` | proposals with readiness, aliases (H01…), review rounds |
| `GET /api/v1/campaigns/{id}/inbox` | decisions with freshness, issues, questions |
| `GET /api/v1/campaigns/{id}/monitor` | rows (problem, budget, compute, agents, needs you) and the pill |
| `GET /api/v1/campaigns/{id}/files` | assets, findings, reports and report jobs, sources, agent artifacts |
| `GET /api/v1/campaigns/{id}/agents[/live|/models|/records/{rid}]`, `/discussions`, `/guidance`, `/models`, `/charters`, `/export`, `/analysis`, `/comparison`, `/validations`, `/studies` | as before, under `/api/v1` |
| `GET /api/v1/resources`, `PUT /api/v1/resources/level`, `/api/v1/llm-usage`, `/api/v1/model-tiers`, `/api/v1/problems`, `/api/v1/assets*`, `/api/v1/bundles*`, `/api/v1/races/{id}`, `/api/v1/confirmations/{id}`, `/api/v1/problem-charters*`, `/api/v1/problem-imports*`, `/api/reports*`, `/api/campaigns/{id}/agent-log*`, `/api/campaigns/{id}/implementation-workspaces*` | unchanged |
| `GET /api/v1/command-catalog` | every operation's payload schema and whether the lead may run it |
| `POST /api/v1/ui-events`, `GET /api/v1/ui-events`, `GET /api/v1/ui-events/gate?campaign_id&milestone` | telemetry (below) |

## Writes: the command bus

Every write is `POST /api/v1/commands` with a `Command` (`contracts/commands.py`): a client-made `ui_<uuid>` id, the
campaign, the operation, `expected_revision` (the campaign version; 0 creates a campaign), optional
`expected_guidance_revision`, `expected_authority_hash` and, new, `expected_thread_revision` and `origin`
(`{thread_id, message_id, actor, agent_id, run_id, rationale}`). The receipt (`work_command`) carries the origin, so every
page can show who ran a command and from which conversation. The browser journals its commands in `localStorage`
(`balsamic.commands`) and retries a lost reply under the same id.

The service layer adds operations with `CommandService.register(operation, handler, model=, delegable=)`; an operation
must be declared in the `Operation` literal. M0 adds `campaign.stage_pin`; the thread, note, intervention, autopilot and
library operations are declared and arrive with their milestones.

Guidance: `manager_state.guidance_revision` stays the campaign-wide counter bumped by researcher graph and page commands.
Each thread has its own `guidance_revision` (bumped by a message in that thread, M1); a command pinned to a thread revision
is refused with "The researcher wrote in this conversation since this command was prepared" when that thread moved on, and
a message in another thread does not stale it.

A campaign may now be created without tasks or a charter: it starts in its formulation stage (`formulating: true`, no
study yet), the lead drafts the problem note with the researcher (M1), and the confirmed charter sets the tasks through
`campaign.update`. `CampaignInput` also carries `autopilot` settings, off by default.

## Threads on the lead's session tree

A thread is a branch of the lead's single Pi session. The harness (`agent-harness/src/sessions.ts`) exposes the tree:
`GET /v1/agents/{id}/tree`, `/entries?after&leaf`, `/context?leaf` (what the model would see from a leaf, with sizes),
`POST /v1/agents/{id}/leaf {entry_id, summary?}` (idle agents only) and `GET /v1/agents/{id}/export?leaf`. A run submitted
with `thread_id`, `leaf_entry_id` or `branch_from_entry_id` (and an optional `summary`) has its leaf positioned before the
prompt, and its result reports `entry_ids {parent, last}`. Steering reaches the turn in progress only from its own thread;
another thread's message waits as its own run. `tests/sessions.test.mjs` runs this against the pinned SDK 1.0.4.

M0 records one `thread` per campaign (`thread_main_<campaign>`); messages carry `thread_id` (absent means main) and a
`desk` when written to a page agent. The Python client (`agents/client.py`) has the matching calls. Fork, park, merge,
summaries, the context drawer and placeholders arrive with M1.

## M1: formulation and branchable chat

**Housekeeping.** `balsamic/housekeeping.py` runs naming and labelling jobs on the maintenance tick, a few per tick,
on the lightest model: the `housekeeper` role on the model tiers (Monitor → Models; default tier `fast`; the roles
table names every role as the pages do, the ones the researcher talks with first and accented, and a tier saved under
the old role name `problem_importer` is read as `formulator`; the Charter tab names the formulator's model, tier and
call count, fixed for the import's lifetime), or the
profile default before tiers are saved. Jobs: a two-to-six-word title for a branch forked without one (from the forked
message and the turns around it; `thread.title_source = housekeeper`), a two-or-three-word `short_title` for every
hypothesis (lineage nodes and chips show it; the title stays the full statement), and the branch summary cards.
Each record keeps the outcome under `housekeeping.<job>`; a failed call is retried at most three times. Nothing runs
with `GRATING_LLM_DISABLED` or without a model.

**Agents' side conversations.** What agents say to each other while working on one researcher request never appears in
the researcher's thread. `tools.assignment_origin` stamps each assistant (and the assistants it asks) with the lead run
and thread the work descends from; `threads.service.route_message`, installed as `pi.message_thread`, sends their
messages, the lead's requests to page agents and the lead's answers to agents' questions into the request's agents
thread (`thread_agents_<run>_<campaign>`, `kind: agents`, parent = the request's thread, named by the housekeeper). The
first message there posts one `agents_link` line in the researcher's thread; the chat shows it with a link, and lists
agents threads in their own group of the switcher. Messages carry `agent_role`. A page agent's desk (requests made on
its page) stays on the page.

**Charter form.** `note/CharterPanel.tsx` reads `GET /api/v1/problem-charters/{id}` (the record plus `describe`: `findings`,
`values`, `errors`, `rendered`, `blocking`) and settles each blocking finding in place: `assumption.unaccepted` → Accept
(`accept-assumptions {names}`) or a new value; `quantity.no_value` / `quantity.range` → a number or an expression
(`edit {changes: [{op: set, path: quantities.<name>.value|expression}]}`); `variable.bounds` / `variable.values` →
bounds or values (`variables.<index>.lower|upper|values`); any finding → its `suggestions` as one-click buttons (a
`waive` posts `waive {key, reason}`, a `request` posts `requests {text}`, an `edit` posts its computed `changes` with its
`rationale` as the researcher's own edit, shown as the primary button with a line saying what it does; "Ask the formulator" opens its box prefilled
with the suggested request) or Waive when waivable. The section is titled "Checks to pass before confirming" and says
the findings come from the checks, not the formulator; the questions section says the opposite. In a finding or a
question, every quantity and variable name is set apart (`.qname`, its description on hover) and the names it is about
are explained under it in words (`finding-meaning`); fields carry their guidance as a label above the box, never as a
placeholder that a narrow box cuts off. Across the app, an agent pill whose words say it is at work (running, revising,
deciding, …) carries a pulsing dot (`Pill`, `live`). Quantities and variables are editable tables (numbers typed as numbers; derived values shown computed);
questions are answered from their options (the formulator's recommendation first, marked) or in words (`answers`), and
none is required: "Send N answers, leave M to the formulator" (`send-answers`) sends the round as it stands, and a
question sent without an answer is shown as "left to the formulator" (`sent` without `answer`) while the formulator
decides it and records an assumption; the section's state reads "round r of 2 · n of m open" (`question_rounds`,
`question_rounds_max` from the view), its lead says whether another round can follow, and an answered question leaves
the open list and folds away under "N answered"; a proposal is shown as an answer to the request in the researcher's own words (`asked`,
recorded by `charters.propose` beside the formulator's `request`), each option a card with its label, its reason in
prose, the computed changes (`diff`) and the checks it leaves open folded away, never the raw changes; decided proposals
fold under "N earlier proposals"; one option is chosen (`…/decision`). Confirm is
enabled only with no blocking finding and no open question, and says why otherwise. The raw charter wraps.

**Panels.** Pages the lead and the formulator compose from a catalog of components, validated, resolved on the server
and rendered by our components: `docs/panels.md`. The to-do for A2UI adoption, with its review date, is there.

**Overview page.** `#overview`, first on the rail: name, objective, stage and basis, charter state, tasks, counts (linked),
budget meters, team, autopilot, threads, and the campaign's removal (moved here from Monitor → Resources).

**The problem-definition gate.** `evolution.stages.definition` decides whether a campaign's problem is fully defined:
every section of the problem note written (`sections` step lists the unwritten titles in `missing`), the note adopted at
its current revision (`adopt`, "again" after a revision), the charter confirmed (`confirm`, `#note/charter`), the tasks
installed (`tasks`). A campaign without a note that has tasks (a problem adapter or a charter confirmed without a note) is
defined by its charter. `next_step` is the gate's step, None once open; `campaign_row.next_step` carries it; the stage
panel shows a callout on every page until it is None, louder (`blocking`) once proposals wait on it. `evolution.gate`
applies it: `research.generate`, `hypothesis.allocate` and `intervention.batch` are refused for every actor, the lead's
and autopilot's `hypothesis.create`, `study.race.create`, `trial.create` and `draft.launch` through a bus guard, and
`autopilot.set {enabled: true}` in its handler; `autopilot.rounds.round_state` starts no round while the gate is closed.
`campaign_inspect.definition` (`workspace.definition_gate`) shows the lead the same state, and its briefing says what is
missing and tells it to name the missing step instead of starting. A setup-time autopilot choice is stored as
`campaign.autopilot.requested` (the switch stays off) and prefills the dialog once the gate opens. The housekeeper's
suggestions and the lead's briefing name the same step.

**Skipping a section** (`balsamic/notes/skips.py`). A section other than the objective may stay unwritten when that is
justified, and the researcher is not asked to judge it alone: the lead proposes, the researcher agrees. The lead's
`problem_note_skip` records a `note_skip` proposal: per section a basis, `charter` (the confirmed charter of the note's
adoption already states it; `charter_refs` are the paths that do, each must hold a value) or `not_applicable`, a reason,
and `confirm`, the one statement the researcher confirms by agreeing. It is refused for a written section, empty paths, or
open questions while the charter has unanswered ones; the lead's briefing tells it to sort each unwritten section (the
charter states it, it does not apply, or it matters and becomes a question of at most two, with a suggested answer) and
to write short sections before adoption rather than skip them. The proposal is a card in the conversation (message kind
`note_skip`) and a row in each section on `#note`; the researcher's `problem_note.skip {skip_id, agree, decline}` records
agreed skips on the note (`note.skipped`) without a revision, so the note is not reopened for adoption, and
`problem_note.unskip` takes one back. The gate counts a skipped section as settled while it holds: a `charter` skip lapses
when the confirmed charter no longer holds a value at a cited path. The `sections` step carries `skipped` and
`proposed_skips`; at Adopt the formulator reads each skipped section's reason. The note page's "Ask which can be skipped"
and `/skip` send the lead the same request.

**Desks.** `GET /api/v1/campaigns/{id}/desks/{role}` is a page agent's conversation with the researcher (messages with
`desk = role` and no thread). `DeskPanel` shows it docked under the agent's page (collapsible, agent-tinted header) and,
while the lead's chat is detached, in the chat column of that page. `deskRole(route)` maps the address to the agent via
the `agents` map in pages.json.

**Layout.** Three resizable columns: gutters between rail and main and between chat and page; the rail folds to initials.
`balsamic.layout` in localStorage keeps widths and the fold. A page chunk that fails to load after a rebuild reloads the
tab once (`lazyPage`); a page that throws shows its error with Try again and Reload (`PageBoundary`) instead of a blank.

**Visual language.** Status is inert: `.pill`, a flat tinted capsule, default cursor, never clickable. Controls are
`.btn` (bordered, hover, pointer); `.btn.control` for the chrome controls that open something (monitor, context,
autopilot, with a state dot or switch); `.btn.primary` for the one main action. Links to records and pages are `.chip`
(mono, dotted underline) or plain links. Resource levels are `.level` option cards with `aria-pressed`.

**Live suggestions.** After each lead reply the housekeeper writes three next requests for the thread
(`thread.suggestions {items, as_of_message_id}`, event `thread.suggested`) from the stage, the counts and the last exchange;
the composer shows them while they match the last message, and the stage defaults only until the lead has spoken.

**Folded exchanges.** The chat groups messages into exchanges (a researcher message and the replies before the next one)
and folds all but the last two under a one-sentence title: the housekeeper's `fold_title` on the researcher message
(written once the next exchange begins), else the question's first sentence. Expand all / fold all is one button.

**Inbox: issues and questions.** An issue shows the actions that resolve it as buttons (a run waiting on one that ended
without completing scientifically, for example: *Start them anyway*, keeping their order, or *Stop them*), beside *Mark as
handled*, which only records that the researcher dealt with it, and *Defer*. A lead question carries its `suggestion`
(what it recommends, as the researcher would do it) and up to four `options` answered with one click; `researcher_ask`
requires the suggestion, and the agents are told to ask only for what the researcher can do.

**What is happening.** A bar under the chat header says what the lead is doing in a line (`Lead waiting · experiment
operator working · 5 runs`); clicked, it opens a panel with each agent at work or active in the last ten minutes (what
it is doing now, else what it last said, from its disposition summary), the runs going and queued with their device,
progress and why a queued one waits (the admission reason, the run it depends on, or a runtime profile measuring),
the GPU and CPU slots taken, and a link to the agents' conversation where the request is being worked on. A queued
agent has a turn waiting for its previous one to finish. It reads `/agents`, `/monitor` and `/experiments`; no endpoint of its own.

**Detached chat.** "Detach" in the chat header opens `#t/<thread>/chat`, a chat-only window (`window.name = balsamic-chat`);
the main window replaces its chat column by a thin strip (Reattach) so the page fills it. Page links clicked in the detached window go over
`BroadcastChannel('balsamic.chat')` to the main window, which navigates; the chat window stays on the chat. Reattach,
from either window, closes the chat window and shows the column again.

**Checkpoint turns.** When a lead turn ends without `yield_work`, the controller asks once for the checkpoint
(`pi_continue_*`). The report of that checkpoint is not posted as a message: the researcher already has the answer.
Only text the lead writes beside it in that turn would be.

**Threads.** `thread.fork {thread_id, message_id, title?}` starts a branch at a message; its `fork_entry_id` is the entry
of the lead's reply to that message, and the branch's first turn carries `branch_from_entry_id` so the harness branches
the session there. Each later turn carries the thread's `agent_leaf`; the reply moves it (`agent_run.result.entry_ids`,
`message.entry_id`). A title the researcher gives is kept (shortened by `threads.service.branch_title` to 48 characters at a word). A branch forked without a title gets a capped placeholder and `title_pending`; the housekeeper (below) names it on the next maintenance tick. `thread.rename`, `thread.park` (writes a summary card), `thread.unpark`, `thread.pin {record_id,
pinned, excluded}` (delegable: the lead's `thread_pin` tool) and `thread.merge {thread_id, into_thread_id, note}` (one
message `merge_of_thread_id` in the target, the target's revision bumped, the source parked as merged, a lead turn on the
target's leaf). A merged thread takes no more messages. Edit-and-resend is a fork before the edited message plus
`agent.message {edit_of_message_id}` in the branch; the original message stays.

`GET /threads/{id}/messages` returns the branch's own messages, `inherited [{thread_id, title, count, from, to}]` with
`inherited_messages` (what the branch descends from, through the reply at the fork), `parent_path`, open `questions`,
pending `placeholders`, the thread's `status`, `merged_into` and `summary`. The threads list adds `queued`, `unread` and
an activity `strip` (glyphs from records: message, strategy, run, decision, note, merge). `GET /threads/{id}/context`
reports the three layers: project (campaign, tasks, charter, guidance, with byte counts), branch (measured from the
harness's context at the thread's leaf when it answers, otherwise estimated and labelled so) and live (pinned and
excluded records with their current status), plus the lab events since the thread's last turn.

**Guidance.** A message bumps its thread's `guidance_revision`; the lead's `campaign_command` pins
`expected_thread_revision` from its run (never from the model) and `expected_guidance_revision` from the run unless the
lead read a newer one with `campaign_inspect`. A message in thread A does not stale a command of a turn in thread B
(`tests/balsamic/test_threads.py`).

**Lab events.** When a command with an origin thread completes (runs, strategies, allocations, races, findings, adoption,
autopilot), every other active thread gets one `lab_event` line; the lead's `lab_events` tool lists them.

**Summaries.** `balsamic/threads/summaries.py`: with model calls allowed, two sentences from the lead's default model
through the harness's single call; otherwise an activity card (`written_by: activity_only`) that says how many turns each
side wrote and the lead's last line. Parking writes one; merging writes an activity card.

**Who generates proposals.** The researcher asks the lead (`/generate N`, or in words); the lead owns the request, proposes itself or asks the discussion moderator for a generation round with its specialists, and records each strategy. The moderator reviews recorded proposals in rounds (`/review`) and never starts rounds by itself. The chat suggests the next request by stage; once a problem is set, the default suggestion is `/generate 3`.

**Placeholders.** `research.generate {operation diversify|hybrid|revise, parent_ids, count, note}` (delegable) writes
`count` `placeholder` records, posts the request as a message with `refs`, and gives the lead a turn on the thread with
`batch_id` in its origin; each `hypothesis.create` from that run resolves the next placeholder. `/generate N …`,
`/evolve H01`, `/review`, `/compare`, `/probe`, `/plan`, `/note`, `/discuss`, `/fork`, `/merge into`, `/park` and `/help`
are the composer's slash commands (`frontend/src/chat/slash.ts`). The housekeeper's suggestions are requests to the lead;
the researcher's own next step (adopt the note, confirm the charter) is the callout every page shows, never a suggested
message.

**The problem note.** `problem_note` (mutable head: title, current and adopted revision, charter and import ids, status
drafting/adopted/confirmed), `problem_note_revision` (immutable: the seven sections, each `{text, items, latex}`, the
glossary, the author, the reason, the source message and run, the proposing annotation) and `note_annotation` (anchor,
kind, question, tier, escalation, answer, proposed edit, visual, status). The lead writes it with `problem_note_draft`
(each call a new revision in its name) and answers questions with `problem_note_propose` (an answer and, only when the
note should change, a proposed edit the researcher accepts with `note.resolve`; the note never changes silently). The
researcher revises with `problem_note.revise`: the page says so while the note is a draft (a callout under the tabs:
question it, edit it, or tell the lead; adopt when it is right), an outline strip names every section with ✓ written
or ! missing (a skipped section counts as written, a proposed skip is marked) and "n of 7 still to write" (a click scrolls to it; "Fold written" leaves only the unwritten in view, each
section folds on its own), and every section has "Ask about this" (the annotation menu with the section's opening words
as the quote, no selection needed) and "Edit" (text, items and formulas in three fields that grow with the text, saved
as a revision in the researcher's name); a proposed edit is shown as the note would read, the raw data folded. Tiers (`balsamic/notes/router.py`): lookup (the glossary) and quick (one
cheap model call with the note as its only context) answer at `POST /problem-notes/{id}/ask` without the bus; with
model calls off the quick tier escalates to the lead tier and says why; lead and visual go through `note.ask` as a turn
of the lead (`render_visual` saves a self-contained HTML/SVG of at most 256 KiB as an `agent_artifact` of kind visual;
the page shows it in a sandboxed frame that can only ask to open a page). `problem_note.adopt {note_id, revision}` turns
the note's markdown into the statement of a problem import and starts the formulator; the charter it writes appears on
the note's Charter tab, where the researcher confirms it (`/problem-charters/{id}/confirm`, then `campaign.update` with
the charter reference sets the tasks and ends the formulation stage). A revision after adoption reopens the draft.

**Fake harness.** `tests/fake_harness.py` speaks the harness protocol with a scripted turn (read the campaign, reply
through `yield_work`) and a session tree; `tests/balsamic/test_e2e_fake_harness.py` runs a message, a fork and a branch
turn through the authenticated callback routes. For a local stack without a model:
`GRATING_PI_TOKEN_FILE=… uv run python tests/fake_harness.py --port 8768`.

## M2: the evolution graph

**Ledger** (`balsamic/evolution/ledger.py`, cached by event cursor): one row per strategy with its alias (H01…), stage,
round, typed `parent_links`, the measured best score and the run it came from, worker seconds (measured) against the
allotted time, evaluations, the highest rung, the race configuration bound to it with the latest decision, the review
verdict and readiness, implementation readiness, live runs, finalist and confirmation state, the stop reason and who
acted (researcher, lead, page agent, autopilot; `autonomous` marks autopilot). Stages: proposed → reviewed → screening
(short runs) → extended (long runs or race rungs) → finalist → confirmed → stopped; stopped is a decision with a
reason, never a failure. `GET /campaigns/{id}/evolution?after&progress&curves` returns the rows, the lineage layout
(rounds as columns, lanes under the first parent), typed edges, race views, finalist selections, confirmations, recent
interventions and, with `progress`, the best so far, the live runs and the finalists' rungs. `GET /hypotheses/{id}`
is the card: the record, its ledger row with the best run's curve, parents and children, reviews, the review rounds
that mention it, runs, race cell, implementation and provenance. `GET /records/{id}/provenance` walks a record to the
command that made it, the run, the agent and its model, the message and the thread; a strategy's parents; a run's
strategy; an asset's producers.

**Lineage.** `HypothesisInput` gains `parent_links [{id, kind revision|variant|hybrid|inspired_by}]`, `round` and
`library_entry_id`; `parent_ids` stays and the two are kept consistent (plain parents become revision links, or hybrid
links when there are several). `hypothesis.status archived` records `stop_reason` and `stopped_at`, and holds the
strategy's configuration in any active race (`study.race.decide hold`), so the race spends nothing more on it.

**Interventions.** `hypothesis.allocate {hypothesis_id, worker_seconds, rationale, replicate?}` (delegable within the
delegated per-run allowance): a race decision (extend or replicate) when a configuration is bound, else a continuation
of the latest finished run when it saved state, else a fresh run with the next unused seed (the outcome says which).
`intervention.batch {items[{operation, payload}], rationale}` runs stops, promotions, allocations, rounds and new
strategies as one batch, all or nothing, with one guidance bump and one immutable `intervention` record; the frontier
table sends it. Every row shows who acted; autopilot rows carry the badge.

**Pages.** Evolution: the frontier table (multi-select; Stop with a reason, Promote, Allocate, Hybrid of / Revise,
Discuss, which forks a branch anchored on the selection), the lineage tree (hand-written SVG: rounds as columns, typed
edges, stage colours, double-click opens the card), curves (each strategy's best run, measured) and the races. The
hypothesis card has Open in graph, Trace origin and Discuss. The Hypotheses page shows a round in progress ("1 of 3
ready" from the placeholders) and each proposal's stage. The experiment log reads a continued run as one chain and has
Trace origin.

## M3: merge, monitor, autopilot

**Merge.** `thread.merge_draft {thread_id, into_thread_id}` gives the lead a turn on the source branch's leaf with the
`merge_note_draft` tool; the draft is saved on the thread (`merge_draft`), the researcher edits it in the merge dialog
and sends `thread.merge` with the note. A draft merges nothing by itself.

**Monitor** (`balsamic/monitor/rows.py`). Rows: problem; budget (worker: used, allocated and the budget, all measured,
plus a forecast labelled as such: measured use plus the remaining allotment of live runs; model spend: charged by the
harness's price catalogue plus implementation commitments against the cap); compute (runs now and the ceiling, the
admission queue, the resource level, the GPU row from the host snapshot, saying when no device is reported or when
devices are listed but not measured); agents; autopilot (state, spent of cap, rounds); needs you. The pill reads
"All clear" unless a row needs attention (something waits for you, the team is paused, stopped or failed, a budget is
exhausted or unaccounted), or "Away digest" while an unseen digest exists. The drawer polls resources only while open.

**Away digest** (`balsamic/monitor/digest.py`). When an autopilot round completes and the researcher has shown no sign
of life (a UI event or a message) for 30 minutes, an `away_digest` is written: rounds completed, strategies stopped
(with reasons and who stopped them), promoted, runs started and finished, decisions and questions waiting, the
commands. `GET /campaigns/{id}/away-digest` returns the open one (and `?since=<event>` computes one on demand);
`POST …/away-digest/{id}/seen` closes it, which the page does when it opens.

**Autopilot** (`balsamic/autopilot/`). `autopilot.set {enabled, compute_cap_seconds, report_by, rationale}` is the
researcher's; it records the baseline allocation, sets delegation while on and restores it after. The maintenance tick
starts a round when autopilot is on, the team runs, no request is open (researcher messages come first), the lead is
idle, and the previous round's runs have finished: a request turn in the system thread `autopilot` with
`agent_run.autopilot = true`. Its `campaign_command` runs as actor `autopilot`: every command needs a rationale, the
receipt's origin says so, ledger rows carry `autonomous` and the pages a badge. The cap guard refuses a command whose
reservation would take `allocated − baseline` past the cap. When the cap is reached or the report date is within a day,
the last round asks for the report (`report_write`) and no further round starts. Rounds completed are counted when the
run ends; the digest is written then. This is the one exception to "the lead acts on requests" (lead-agent-readiness.md).

**Iteration progress.** During Optimize the top of the stage shows the best so far (measured), the live runs and the
finalists' rungs, from the evolution read's `progress`.

**Lineage canvas.** The hand-written SVG tree is the lineage view; the `@xyflow` canvas stays behind the M2 gate
(`ui-events/gate?milestone=M2`, the time from a round's end to the next action) and is not built.

## M4: reuse and polish

See [strategy-library.md](strategy-library.md) for the library, the search palette, keyboard navigation and exports.

## Telemetry and gates

`ui_event` records hold the kind, time, campaign, thread, page, a session tag and a few scalars: which controls are used,
never what was written. The frontend batches them (`frontend/src/telemetry/events.ts`). `GET /api/v1/ui-events/gate`
computes the plan's gate table per milestone: forks, switches, merges and parks per session and the share of page opens
outside `main` (keep branching?); context-drawer opens, pins, exclusions and forgot reports (context before features?);
interventions per kind and the override rate (which controls stay?); the median time from a round's end to the next action
(canvas needed?); monitor opens and alerts acted on (which rows?); note asks by tier and escalations (router tuning).

## Running it

```bash
uv sync --extra dev --extra meent
(cd agent-harness && npm ci && npm run build)
(cd frontend && npm ci && npm run build)
uv run optimization-implementations --directory runs/dev/library --port 8767      # the implementation service
GRATING_PI_TOKEN_FILE=... GRATING_PI_DIRECTORY=runs/dev/pi GRATING_PI_WORKSPACE_URL=http://127.0.0.1:8765 \
  GRATING_PI_PROFILE=agent-harness/profiles/dev npm --prefix agent-harness start   # the Pi harness
GRATING_PI_URL=http://127.0.0.1:8768 uv run optimization-lab --directory runs/dev/workspace --port 8765 --workers 2
(cd frontend && npm run dev)                                                      # http://127.0.0.1:5173 during development
```

`scripts/dev-stack up` starts the three services locally in one go (the scripted fake harness by default, `--real` for the Pi harness) and serves the built frontend at http://127.0.0.1:8765; `scripts/dev-stack down` stops them. `scripts/labctl` starts the same services from a launcher configuration. Tests: `GRATING_LLM_DISABLED=true uv run pytest -q`,
`(cd agent-harness && npm test)`, `(cd frontend && npx tsc -b && npx playwright test)`.

## Milestones

| Milestone | State | Contents |
|---|---|---|
| M0 Foundations | built | service layer, scoped reads, SSE with kinds, command origin and thread pin, pages.json, session-tree routes, shell with chat on `main`, Inbox, Files, Monitor, Experiments, Hypotheses, New campaign, telemetry, legacy address redirects |
| M1 Formulation and branchable chat | built | thread fork, edit-and-resend, switch, park, merge; branch map; context chip and drawer; placeholders; slash commands; problem note with annotations and Adopt; the scripted fake harness |
| M2 Evolution graph v1 | built | ledger rows, lineage, stages, provenance, frontier interventions, allocation, stop reasons with race holds, hypothesis card, experiment log |
| M3 Merge, monitor, autopilot | built | merge note drafted by the lead, monitor rows with forecasts and the GPU row, the pill with a digest state, the away digest, autopilot rounds within a cap, iteration progress |
| M4 Reuse and polish | built | strategy library (publish, search by signature, apply as an inspired_by root, the lead's first-turn matches), Ctrl+K search over pages, threads and messages, keyboard navigation, thread and graph export |
| M5 Problem-agnostic constraints | planned, not started | design constraints as expressions over the candidate and outcome constraints on reported metrics, in the contract, the manifest, the checks, the guideline and the Charter page; starts only once the present framework is confirmed correct end to end, as the reference point ([problem-setup.md](problem-setup.md#next-step-problem-agnostic-constraints-planned-not-started)) |

The page placeholders for later milestones say which milestone brings them; nothing pretends to exist.
