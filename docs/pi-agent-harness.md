# Pi campaign runtime

Campaigns activated with `agent.activate` use the installed Pi SDK (pinned at
1.0.4 in `agent-harness/package.json`) and, without a dev profile, the
`openai-codex` subscription provider. The optional development container image
(`deploy/implementation-workspace`) installs the Pi CLI 0.87.1. The PI uses `gpt-6-astra`;
specialists use `gpt-6-sol`; proposal and implementation reviewers use Astra.
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
their artifacts/costs/unfinished states, pins existing H labels, and queues the PI.
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

## Numerical diagnostics

`mask_create` makes deterministic full-size masks without putting all cells into
model context. `fixed_mask.run` evaluates immutable masks at specified fidelities
and retains objective values, energy totals, startup/evaluation times, cache flags,
and actual solver counts. Each uncached TE/TM evaluation performs two solves.
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
