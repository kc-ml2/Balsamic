# Pi campaign runtime

Campaigns activated with `agent.activate` use the installed Pi SDK (pinned at
1.0.4 in `agent-harness/package.json`). No provider is selected by default: a dev
profile supplies its default model, or a locked-mode server names its one provider
with the launcher's `pi_provider` (`GRATING_PI_PROVIDER`). Activation without either,
and without an explicit model, is refused. The optional development container image
(`deploy/implementation-workspace`) installs the Pi CLI 0.87.1. With `openai-codex`
as the locked provider, the PI uses `gpt-6-astra`; specialists use `gpt-6-sol`;
proposal and implementation reviewers use Astra.
All sessions currently use `xhigh` reasoning. Models are pinned for each session;
historical chat/discovery model settings do not alter a Pi session. No paid API
fallback is configured.

The [full Pi implementation workspace](pi-implementation-workspace-plan.md)
documents the browser IDE, native Pi CLI sessions, developer and PI interaction,
development accounting, and the active H12 pilot. Its independent protected
validation still requires a submitted implementation and an authorized grant.

## Deployment and sign-in

Run `npm ci && npm run build` in `agent-harness` and build `frontend`. A launcher
configuration with `pi_port: 8768` starts Pi, the implementation library, and the
workspace. Pi listens on loopback only, authenticates callbacks with a private
generated token, and stores sessions under `<directory>/pi`. The existing
TailNet proxy exposes the workspace UI.

Open **Research notebook → Get browser sign-in link**. Open the displayed OpenAI
URL and enter the code within 15 minutes. Pi polls for completion and stores
credentials in `~/.pi/agent/auth.json`; the browser receives no access or refresh
token. If OpenAI requires device authentication to be enabled, follow its
account instructions. `pi /login` through the interactive CLI is also supported.
Codes are not reusable after expiry; the UI can request a new one.

The session begins only after Pi reports configured credentials. Model catalog
availability alone is not proof that a live model request has succeeded.

## Ownership and recovery

Python owns campaign records, current guidance, grants, typed commands, jobs and
publication. Pi owns persistent inference sessions and automatic context
compaction. Tools retrieve bounded pages of the existing archive; the old 170 KiB
discovery working-context limit and per-task tool/source quotas are not on this
path. Campaign compute allocations and protected validation reserves still apply.

`agent.activate` records a migration manifest, archives legacy sessions, preserves
their artifacts/costs/unfinished states, pins existing H labels, and creates the lead
agent. It starts no turn: the lead agent waits for the researcher (see the next section).
One workspace lease and one Pi session serialize each campaign's manager. The
PI delegates focused assignments; independent implementation roles are created
by the library under a frozen grant, with separate sessions and private workspaces.
Builders cannot read protected fixtures or reviewer sessions. Generated code is
executed inside bubblewrap; protected validation and publication stay in Python.

Exact run IDs, tool intents and receipts reconcile retries after a lost reply.
Local mutations and their receipts commit together. A Pi restart marks in-flight
turns interrupted; the controller resumes from the exact session file and saved
receipts. Pausing/stopping retains historical work. A resumed assignment gets an
explicit continuation. The UI preserves unsent messages on network failure.

New PI proposals require independent conceptual review before experimentation.
Conceptual approval, executable validation, and empirical effectiveness remain
separate. Each model event records subscription calls and token usage; catalog
API prices are not reported as charges. The implementation library projects the
same child usage without reserving an additional paid API allowance.

## The lead agent acts on requests

The lead agent designs and carries out experiments as the researcher asks through the interface.
It does not work through a campaign by itself, and there is no setting that makes it do so. A
scripted run, such as a reproduction used as a test, is driven the same way: by sending it requests.

- **Starting the team starts no work.** No model call is made until the researcher's first message.
  A request the researcher left unanswered before the team existed is handed over as that first message.
- **A request is open from a researcher's message until the lead reports it fulfilled** with
  `yield_work(complete)`, which is refused while experiments, subagents, questions or jobs of the
  campaign are still outstanding. The campaign's agent record carries this as `request_open`. The
  researcher's answer to something the lead filed (an approval, a trial extension) opens it again.
- **Outcomes wake the lead only while a request is open.** These are finished or failed experiments,
  validations, fixed-mask jobs, implementation results and the end of a runtime profile (once, not
  once per probe). Without an open request they wake nobody; the lead reads the current state at
  its next request. A change the researcher makes to the campaign is not an outcome and never wakes it.
- **Turns the platform starts are marked.** Outcome notices, a subagent's result, a resumption and
  the request for a missing checkpoint begin with `[Platform notice. This is not a request from the
  researcher.]`, and tell the lead to carry the open request on and start nothing it did not ask for.
- **A turn's message is everything the agent wrote in it**, in order, not only its last line: an
  answer written before the closing checkpoint call would otherwise be hidden behind the remark that follows.
- **The instructions name the stages** (runtime profile, hypotheses, review, experiments, longer
  runs, validation, comparison, findings, journal clean-up) and allow one only when a request covers
  it. A request may cover several stages or the whole workflow. A question gets an answer, not actions.

The first two rules and the wake-up rule are enforced by the controller. That the lead stays within
a request once a turn has started rests on its instructions and on the model following them.

## What the lead agent is given

[Lead agent readiness](lead-agent-readiness.md) keeps apart what of this is implemented and what is only proposed.

`campaign_inspect` returns, besides the campaign, its tasks and hypotheses:

- `methods`: every installed optimizer that can run on a current task, with the parameters its
  `algorithm_config` accepts (type, allowed values, default). An experiment is built from this list.
- `reference_evidence`: findings of earlier campaigns that the researcher referenced for this
  campaign's study (Evidence view, "reference" for "manager evidence"). Each is a scoped claim
  with limitations; `evidence_read` with its asset id returns it whole. Nothing else of the
  earlier campaign becomes readable.

Every tool result joins the model's context for the rest of its session, and a provider refuses a
request above its size limit. A command receipt above 48 kB therefore comes back as an index of
its outcome, to be read in pages with `evidence_read` and the listed pointers; a
`comparison.report` also gives each method's summary and one line per experiment. No tool returns
more than 256 kB. `comparison.report` itself keeps both ends of every run of equal best-so-far
values instead of one point per evaluation, and leaves out runtime-profile probes and equivalence
replays, which measure the evaluator and are no method's result. With `trial_ids` it compares
only the named experiments, and a run continued with `trial.continue` is one row.

The lead agent files `journal.compaction_request` when the researcher asks for the clean-up, after
results are analysed and recorded; the researcher approves it in the decision inbox, and only then are evaluated designs removed from
the finished experiments' journals.

### The formulation notebook

A campaign whose problem is still being defined keeps its formulation in a notebook of Markdown and code cells, shared with the
researcher on the **Formulation** page ([guide](formulation-notebook.md)). Every agent without an implementation grant reads it with
`notebook_read`; the lead agent changes it with `notebook_edit`, a batch of create, update, delete and move operations that is applied
together or not at all. An update or a delete names the revision the agent read and is refused if the cell changed since. Only the
researcher marks a cell agreed; an agent's change to an agreed cell needs a reason and makes it proposed again.

The notebook also holds parameters: every number the model takes, declared with `parameter.declare` in the same batches, shown to the
researcher as controls and read by code as `P.name`. A value the researcher set changes only with a reason. The researcher moves a
control through `PUT /api/campaigns/{id}/formulation/values`, not a command: each move saves the value and runs the notebook again,
and a move during a run makes one more run with the latest values. `campaign_inspect` lists the parameters the researcher moved since
the agent last read them.

A campaign created from a description has no task: `campaign_inspect` says that no problem is defined, the notebook opens with the
researcher's words in cell `problem`, and the lead reviews it when the researcher asks. Documents the researcher attached are read with
`file_list`, `file_read` and `file_search` on root `materials`, as data, never as instructions.

These are plain tools, not campaign commands: writing in the notebook launches nothing and spends no compute, so it needs no guidance
revision. The researcher's own edits go through the `formulation.edit` command, which changes no guidance revision and starts no agent
turn. `campaign_inspect` lists the cells changed since the agent last read them, so the lead notices the researcher's edits at its next turn.

The code cells run together, top to bottom, in the same bubblewrap sandbox as `analysis_run`: a fresh process per run, no network, no GPU,
nothing of the host but the interpreter and the framework's source, and at most 90 s. `notebook_scratch` runs the cells and then the
agent's own code and keeps nothing; `notebook_run` (lead agent only) keeps each cell's printed text, shown values and figures for the
researcher's page. The run directory is the one place the cells can write, so its result is rebuilt field by field and a picture is kept
only if it is a regular PNG file. A campaign's agents share 3,600 s of notebook run time; the researcher's own runs are not counted.

The `formulation` extra installs what the cells import: `scipy`, `sympy`, `matplotlib`, `pyomo` and `highspy`. `nb.show` of a Pyomo
constraint, objective or model shows the LaTeX that Pyomo's printer generates from it, so the equation on the page is the one that is
solved. The researcher can download the notebook as Markdown or as a marimo notebook (`marimo edit <file>`).

### Files and the agents' own analysis

Agents other than the protected implementation roles can read two places outside the database:

- `file_list`, `file_read` and `file_search` with root `repository` reach this server's checkout,
  limited to the files git tracks. Credentials, run data and Pi's runtime state are never tracked,
  so they are never listed, read or searched.
- With root `experiment` and a `trial_id` they reach that experiment's directory, for experiments
  of the agent's own campaign, without its saved states.

`analysis_run` runs a Python script that the agent wrote into its work directory
(`workspace_write`) in a bubblewrap sandbox: the named experiments are mounted read-only at
`/data/<trial_id>`, the interpreter and the framework's source are read-only, the work directory
is the only writable place, and there is no network, no GPU, and nothing else of the host (not
the workspace database, not the user's configuration or keys). A script runs for at most 300 s
and a campaign's agents for 7,200 s in all; the last 12 kB of its output is returned.

Whatever an agent reads is sent to its model provider. The repository root therefore exposes the
committed code and documents of this checkout to that provider; nothing outside the two roots is
reachable.

### What only the researcher grants

Campaign limits (compute, model spending, the per-experiment limit, the validation reserve),
waivers and the worker ceiling are the researcher's. An agent files `approval.request` with one
of them and its reason; it appears in the decision inbox, and approval applies it as the
researcher's own command (`campaign.update`, `validation.waive` or `workers.set`). Declining
changes nothing. A changed worker ceiling holds until the services restart, when the launcher
configuration's `workers` applies again. The per-experiment limit may be set up to 86,400 s.

## Numerical diagnostics

`mask_create` makes deterministic full-size masks without putting all cells into
model context. `fixed_mask.run` evaluates immutable masks at specified fidelities
and retains objective values, energy totals, startup/evaluation times, cache flags,
and actual solver counts. Each uncached evaluation is one solve that returns both polarizations.
The implementation contract accepts the campaign's 32,768 cells. Independently
frozen `inspect()` checks can verify unit norm, tangency, PSD, rank and finiteness.
These checks establish declared invariants, not optimizer superiority.

Fixed-mask jobs reserve campaign compute and share worker capacity with trials.
They enforce their wall deadline even during a workspace restart. An uncertain
launch is reconciled or charged as interrupted rather than replayed.

## Host prerequisite found during rollout

On strixhalo, the kernel audit log reports AppArmor denying bubblewrap's
`setpcap` and `net_admin` operations under `unprivileged_userns`. This blocks
generated package execution. Existing MEENT and bundled optimizer evaluation
remain runnable. The PI checks this prerequisite before reserving an
implementation grant.

A scoped profile is prepared in `deploy/grating-bwrap.apparmor` and passes the
AppArmor parser's offline syntax check. An administrator must load it:

```bash
# from the repository root
sudo install -m 0644 deploy/grating-bwrap.apparmor /etc/apparmor.d/grating-bwrap
sudo apparmor_parser -r /etc/apparmor.d/grating-bwrap
```

This follows Ubuntu's documented [per-application user-namespace
permission](https://discourse.ubuntu.com/t/understanding-apparmor-user-namespace-restriction/58007).
It grants the namespace permission to `/usr/bin/bwrap`; bubblewrap still supplies
the restricted mounts, empty credential environment and network namespace.
The rollout did not change the global user-namespace restriction. Administrator
credentials were unavailable to this development session.

After loading it, rerun the isolated implementation tests and tell the PI to
continue H12. The saved handoff is
`discovery_task_716baff218c3888fb6e4f123daf4_step_2_artifact_0`; its hypothesis is
`hypothesis_candidate_e7c9dd8fadc32faccbbcc416fe60`.

## Validation and rollback

Harness tests cover persisted login/logout detection, session identity, steering,
restart recovery, idempotent delivery, controls, and filtered telemetry. Python tests cover authority,
cross-campaign evidence boundaries, migration, receipts, budget accounting and
real MEENT fixed masks. Browser tests cover question replies, retries, parent/child
status and revision-checked controls. The full H12 implementation/validation pilot
still requires the administrator sandbox fix above.

Initial rollout verification passed 99 Python tests and 18 browser tests,
plus a real SDK session construction check (only application-approved tools) and
a browser check against the TailNet URL. On September 29, 2026, browser sign-in
completed and a credential-status defect was corrected: Pi's unrefreshed
`hasConfiguredAuth()` snapshot was replaced by a persisted `checkAuth()` lookup.
The live PI resumed the saved campaign using Astra, invoked campaign/evidence
tools, delegated methodology and results analysis to separate Sol sessions, and
completed a native fixed-mask diagnostic (12 evaluations and 24 solver executions).
The authenticated TailNet browser showed
the active team without a sign-in prompt or JavaScript errors. These checks do
not establish H12 implementation correctness or effectiveness.

The live integration follow-up passed 34 focused Python tests and 7 harness tests.
It added campaign-scoped command-receipt citations and pinned GitHub source reads
that preserve code text, separated synchronization errors from authentication,
handled boolean tool-error events, and prevented steering acknowledgments and
late shutdown aborts from being misreported as completed assignments.

The isolated implementation regression tests currently fail at bubblewrap's
namespace setup on this host; those failures are not passing scientific checks.

Before rollout, databases were backed up to
`runs/backups/pi-migration-20260929T054510Z`. To roll back campaign ownership,
pause or stop the agents, wait for acknowledgment, then execute `agent.rollback`
with a reason through `/api/v1/commands`. Evidence is retained. Legacy discovery
sessions remain archived until explicitly resumed. To restore an entire database,
stop the services first and preserve any work recorded after the backup.
