# Application command inventory

Rechecked against the research controls and CLI increment on 2026-09-28. This is the concrete
D1 migration inventory from the [consolidation plan](architecture-consolidation-plan.md).
All 23 compatibility mutations are translated. Numerical CLI integration has local, connected, interruption and restart qualification. The campaign manager's event turns and delivered actions also use this command boundary.

## Workspace HTTP mutations

The [workspace API](../src/optimization_framework/api/app.py) declares 25 POST/PUT
routes. One dispatches the shared command envelope; one stages uploaded bytes.
All campaign, context, issue, experiment, study, idea, implementation, research, decision and source mutations translate to that command boundary. External retrieval and subordinate decision actions are dispatched after admission commits.

| Existing route | Current owner/path | Target command or boundary |
|---|---|---|
| `POST /api/v1/commands` | `CommandService.execute` | Keep the shared envelope and boundary-supplied actor. |
| `POST /api/v1/bundle-uploads` | Blob staging and immutable upload reference | Keep bounded content-addressed upload as an ingestion primitive. Inspect/publish remain campaign commands. |
| `POST /api/v1/campaigns/{campaign_id}/studies` | Translated through the shared command | `study.create` |
| `POST /api/campaigns` | Translated through the shared command | `campaign.create`: stable requested identity, revision zero and create-if-absent semantics. |
| `PUT /api/campaigns/{campaign_id}` | Translated through the shared command | `campaign.update`: revision check, append-only resource amendments and a linked study for changed science. |
| `POST /api/trials` | Translated through the shared command | `trial.create`: accepted reply retains the original experiment snapshot. |
| `POST /api/trials/{trial_id}/control` | Translated through the shared command | `trial.control`: target control revision and post-commit worker delivery. |
| `POST /api/trials/{trial_id}/validate` | Translated through the shared command | `trial.validate`: legacy parameters compile through the parent's captured recipe. |
| `POST /api/trials/{trial_id}/recipes` | Translated through the shared command | `validation.run`: accepted retry retains the original subject and procedure. |
| `POST /api/hypotheses` | Translated through the shared command | `hypothesis.create`: parent scope, researcher origin, prepared exact package and exposure. |
| `POST /api/hypotheses/{hypothesis_id}/review` | Translated through the shared command | `hypothesis.review`: append one saved comment, independently of a model turn. |
| `POST /api/hypotheses/{hypothesis_id}/status` | Translated through the shared command | `hypothesis.status`: target status revision and shared frozen finalist nomination. |
| `POST /api/hypotheses/{hypothesis_id}/verify` | Translated through the shared command | `implementation.commission`: frozen source conversion and original service key on accepted retry. |
| `POST /api/hypotheses/{hypothesis_id}/implementation` | Translated through the shared command | `implementation.attach`: deliberate reuse and attachment delivered after commit. |
| `POST /api/hypotheses/{hypothesis_id}/implementation_jobs` | Translated through the shared command | `implementation.commission`: preserves an existing legacy grant under its service key. |
| `POST /api/implementation_jobs/{grant_id}/control` | Translated through the shared command | `implementation.control`: durable delivery with the library's idempotent control key. |
| `PUT /api/campaigns/{campaign_id}/manager/context` | Translated through the shared command | `context.edit`: campaign authority and expected context revision; text projection follows commit through the outbox. |
| `POST /api/manager/issues/{issue_id}/resolve` | Translated through the shared command | `issue.resolve`: campaign scope and issue revision; choice and rationale invalidate stale manager guidance. |
| `POST /api/manager/messages` | Translated through the shared command | `research.start`, retaining the durable manager inbox. |
| `POST /api/research` | Translated through the shared command | Same `research.start` operation. |
| `POST /api/research_runs/{run_id}/control` | Translated through the shared command | `research.control`: requires the observed control revision. |
| `POST /api/decisions/{decision_id}/resolve` | Translated through the shared command | `decision.resolve`: records the observed resolution revision; apply any resulting work through the same application commands. |
| `POST /api/sources` | Translated through the shared command | `source.record`: immutable researcher-supplied evidence. |
| `POST /api/sources/search` | Translated through the shared command | `literature.search`, with durable request/outcome and recorded sources. |
| `POST /api/sources/ingest` | Translated through the shared command | `source.ingest` with durable delivery and retained retrieval provenance. |

Campaign creation uses `expected_revision: 0` with the requested `campaign_id`;
every other command requires an existing campaign revision. Campaign, context,
issue, researcher idea and trial-control operations require researcher authority;
delegable operations retain their existing guidance and authority checks. Earlier accepted
command envelopes and hashes retain their reader and original outcomes.

Translated compatibility routes accept `Idempotency-Key`, `X-Campaign-Revision`
and `X-Guidance-Revision` headers. Accepted keyed retries use the original
envelope, even after subsequent changes. Changed payloads cannot reuse an
accepted identity. An old client without revision headers acts against authority
observed at admission. A request without an idempotency key receives a new
identity and is not a guaranteed retry of an unidentified earlier request.

Trial and research controls accept `X-Control-Revision`; idea status accepts
`X-Status-Revision`, and decisions accept `X-Resolution-Revision`. They detect changes to the target even when the campaign
charter itself has not changed. Legacy implementation commissioning retains its
body idempotency key; if a header key is also supplied, they must agree.
Asynchronous implementation compatibility replies contain the current resource
plus `command_id` and the fixed `command_outcome`. The current resource may
advance; the accepted command receipt remains immutable and separately readable.

Browser controls retain the complete pending envelope across an uncertain
response and background refresh. A definitive rejection permits a new submission;
a changed campaign revision cannot silently turn an uncertain retry into a second
accepted update. Campaign forms keep the charter revision they opened with, and
context edits retain the revision the researcher actually edited.

`formulation.edit` has no earlier route. It carries the researcher's batch of changes to the campaign's
[formulation notebook](formulation-notebook.md); each update or delete names the cell revision that was read.
It is not delegable: agents change the notebook through their own `notebook_edit` tool.

The browser now saves the complete pending envelope in a per-workspace local
storage journal before dispatch. Reload recovery checks the accepted receipt
first; it offers the original request for retry only when no accepted record is
found. Transient lookup errors retain the record. A stable workspace identity
and request header prevent sending an old action to a different workspace
reusing the same server address. Model credentials are not stored in the journal.
The selected behavior and its acceptance case are in the plan's
[implementation handoff](architecture-consolidation-plan.md#implementation-handoff-for-the-remaining-cycle).

Read endpoints may refresh derived caches or Markdown projections. Such writes
cannot allocate work, change scientific records or grant authority. The separate
library's authenticated job/control/import endpoints retain their service-owned
receipts and receive campaign grants through the workspace outbox.

## Browser controls

All browser mutations use the durable `useCommand` hook in
[commands.ts](../frontend/src/commands.ts), apart from byte upload staging.
Research controls, decision choices and source requests now retain their original
envelopes and recover accepted replies after a reload. Source retrieval remains
asynchronous and its pending/failed status is visible in the source library.

Outdated execution recommendations use researcher-only `decision.refresh`
through the same shared envelope. A single card or selected batch retains exact
originals and notes, then queues a current manager review. This never renews old
execution authority. See [decision reconsideration](decision-reconsideration.md).

Preserve the existing feedback behavior: saving a comment, requesting a critique
and requesting a revision are distinct operations. A failed model submission
must not discard a saved comment or save it twice on retry.

## Manager and CLI paths

The [research coordinator](../src/optimization_framework/research/coordinator.py)
dispatches typed proposals through `CommandService.execute`. Decision acceptance
and budget extensions commit a frozen child command with researcher authority;
redelivery reconciles that receipt before checking newer guidance. Research
admission freezes selected feedback and records guidance inside its transaction.
Automatic event turns use the same research-start command with pinned manager
authority. A durable result receipt precedes atomic projection and action outbox
delivery. Current command delivery is available through the separate read-only
`GET /api/v1/commands/{command_id}/delivery` projection; admission receipts remain
immutable. Delivery follows manager actions into their subordinate work.

If a saved researcher question is blocked before dispatch, researcher-only
`research.retry` accepts `{"manager_command_id": "turn_<original-command-id>"}`
through `POST /api/v1/commands`. It requeues that exact question and its original
inbox entry against current campaign context, without creating another message
or changing scientific guidance. The shared envelope checks current charter,
guidance and authority. Retry is rejected once any research run, discovery task,
or consumed input shows that the question was dispatched; uncertain provider
calls still require their existing reconciliation controls. The original error
stays in retry history, and its technical issue resolves only after successful
dispatch. A repeated context failure stays blocked until another explicit retry.

The shipped `dqn-meent train`, `baseline` and `evaluate` commands now use
[recorded.py](../src/dqn_meent/recorded.py) and the common
[CLI session](../src/optimization_framework/cli.py). Local mode owns the workspace
lease; connected mode uses HTTP with a persistent local command journal. Both
create studies, reservations, attempts and costs. Legacy output files are exported
from verified evidence bundles. Policy inference is a separate `inference.run`
analysis allocation; convergence checks use `trial.validate` and captured code.

Older standalone runs enter via a hashed retrospective bundle before new
measurements. Import and optimizer-input reuse are separate recorded commands.
Missing historical costs and exposure remain unknown. Low-level Python numerical
helpers in `lifecycle.py` and `experiments.py` remain regression/reference APIs;
they are no longer invoked by shipped work-creating CLI commands. Plot and design
commands remain derived outputs.

The `optimization-research` entry point accepts typed command envelopes for other
workflows, including studies and implementation work. Local asynchronous commands
wait for their recorded delivery or an actionable interruption; shutdown retains
the lease until returning model/source writers have saved their receipts.
[CLI interruption](../runs/consolidation/cli-controls-20260928/interruption-3/qualification.json)
and [connected restart](../runs/consolidation/manager-lifecycle-20260928/connected-restart.json)
have actual process evidence. The [D2 lifecycle scenario](../runs/consolidation/manager-lifecycle-20260928/restart-http-final/qualification.json)
qualifies manager event consumption and service restart using explicit model fixtures.

## Completion evidence

Close D1 when every route and shipped work-creating entry point above has an
implemented mapping, and tests demonstrate the same revision, authority,
reservation, rejection and replay behavior through browser, HTTP, manager and
CLI callers. In particular, cover creation retry, stale guidance, actor spoofing,
budget amendments, failed feedback submission, lost service acknowledgements and
restart delivery. D2 then qualifies the complete campaign-manager lifecycle
using that command surface.
