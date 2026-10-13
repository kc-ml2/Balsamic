# Lead agent readiness: what is implemented and what is proposed

Status on 13 October 2026. This page keeps two things apart: changes that exist in the code, and
changes that are only proposed. An item moves from one part to the other when it is built and
tested, and the date is updated.

The goal is that a researcher and the campaign's lead Pi agent can run a whole campaign on the 2D
deflector problem (choose a preset, generate hypotheses, review and run quick experiments, choose
finalists and run them longer, record what was learned) without an outside coding session, and
that a later campaign can read what an earlier one learned. The longer aim is for the lead agent
to replace that outside session.

| Item | State |
|---|---|
| Lead receives bounded results; comparison report made small | Implemented, live |
| Lead sees installed methods and their parameters | Implemented, live |
| Findings may cite measurements; Lead reads referenced findings | Implemented, live |
| Results carry every objective of the best design | Implemented, live |
| Journals pack long binary designs | Implemented, live |
| Comparison of chosen experiments; a continued run read as one | Implemented, live |
| Journal clean-up on the researcher's approval | Implemented, live |
| Ten findings of the transformer-policy experiment recorded and published | Done |
| Rehearsal: the Lead runs the whole workflow once | Done: passed, after two fixes |
| Finalists may be chosen from runs that ended at their time limit | Implemented, live |
| Convergence test of the old campaign's best designs, recorded and in the report | Done; see Part F |
| Scores re-checked at higher RCWA orders as a step of every campaign | Not in place; see Part F |
| Lead reads files and runs its own analysis | Implemented, live, used once by a Lead |
| Requests that apply themselves on approval | Implemented, live, used once by a Lead |
| Lead changes code through an implementation workspace | Proposed, acceptable in dev mode only |
| Lead and researcher write down a problem that is not yet defined, in a shared notebook | Implemented; rehearsed once on X870E, not on X670; see Part G |
| Lead acts only on the researcher's requests; starting the team starts no work | Implemented, live, checked with a Lead; see Part G |

"Live" means the running services on X670 use the code (last restarted on 13 October 2026).
"Not live" means the code is in the working tree and tested, and the services still run the old
code. "Tested"
alone means the automated tests pass; where a change was also checked on the running service, the
item says so. Everything on this page is committed on branch `lead-readiness`.

## Why this was needed

An audit of campaign `campaign_738bf861-d57f-4667-a797-669fc1bb101f` (transformer policies for
FLRL PPO, 12–13 October 2026) found:

- **The Lead did not drive the campaign.** All 8 hypotheses, the 25 `trial.create` and
  4 `trial.continue` commands, the finalist selection, the runtime profiles and the waiver were
  issued under the researcher's authority, none by the Lead. The Lead ran comparison reports,
  equivalence checks, snapshots and one fixed-mask check, and recorded one finding.
- **A comparison report ended the Lead's session.** `comparison.report` returned its whole result
  to the model: 11, 21, 24 and then 55 MB. After the last, every model request failed with
  HTTP 413 (from 06:22 KST on 13 October). The session file is 113 MB.
- **The Lead could not see what it could run.** The method catalog went only to the browser. The
  Lead asked the researcher for a mode-limit setting that already exists
  (`level_set_modes_x`, `level_set_modes_y`).
- **Nothing recorded reached a later campaign.** Agent reads are confined to their own campaign;
  no finding was published; the Lead's tools did not return findings referenced from another
  campaign; a finding could not cite runtime profiles, equivalence checks or fixed-mask reports
  (the Lead was refused twice).
- **The Lead asked for constraints to be lifted and got no answer.** Two questions have been
  pending since 12 October: one about mode limits, implementation workspaces and reading TE and
  TM of the best designs; one about a higher worker ceiling or a waiver.

## Part A — Implemented and live

Each item is covered by tests; the full suite passed on 13 October (1,278 passed, 1 skipped, plus
the three workspace tests run separately and the new tests listed here).

### A1. Bounded results for the Lead

`src/optimization_framework/agents/tools.py`

- A command receipt above 48 kB is returned as its status plus an index of its outcome; the Lead
  reads the parts it needs with `evidence_read` and the listed pointers. For `comparison.report`
  the receipt also gives each method's summary and one line per experiment.
- No tool returns more than 256 kB; a larger result becomes an error that says the call was
  carried out and how to read its record.
- Tests: `tests/test_pi_agents.py::test_large_command_outcome_returns_as_an_index_the_lead_pages`,
  `::test_no_tool_hands_the_model_more_than_it_can_hold`.

### A2. A small comparison report

`src/optimization_framework/analysis/general.py`

- Curves keep both ends of every run of equal best-so-far values instead of one point per
  evaluation. Values read at any cost are unchanged.
- Runtime-profile probes and equivalence replays are listed under `excluded`; they measure the
  evaluator and are no method's result. This was not in the first plan: 26 of the 55 experiments
  in the saved report were such measurements, and each replay carried about 3.5 MB of designs.
  Because 30-second probes no longer take part, a report's common cost changes.
- Checked on the running service against the saved 61.6 MB report of the same campaign: the
  report is 477 kB, 29 experiments are compared with the same best value and end cost as before,
  26 are left out as measurements, and the Lead's receipt for it would be 14 kB. It still takes
  28 s to compute, because it reads every experiment's metrics journal.
- Tests: `tests/test_framework_comparison.py::test_a_thinned_curve_reads_the_same_at_every_cost`,
  `::test_evaluator_measurements_are_not_reported_as_methods`.

### A3. Methods and referenced findings in the Lead's view

`src/optimization_framework/agents/tools.py`

- `campaign_inspect` returns `methods`: every installed optimizer that can run on a current task,
  with the parameters its `algorithm_config` accepts.
- `campaign_inspect` returns `reference_evidence`: findings of earlier campaigns that the
  researcher referenced for this campaign's study. `evidence_read` with a referenced asset id
  returns the whole finding; nothing else of the earlier campaign is readable.
- The instructions tell the Lead to build experiments from `methods`, to read referenced findings
  before proposing, and to record and publish what the campaign learns.
- For the 2D task the list has 21 methods in 16 kB, among them `flrl_ppo` with
  `level_set_modes_x/y`, `policy_network`, the `transformer_*` settings and
  `stacked_observations` (computed from the registry and the recorded task, not yet read by a Lead).
- Tests: `::test_inspection_lists_the_methods_a_task_can_run_with_their_parameters`,
  `::test_the_lead_reads_a_finding_of_another_campaign_only_once_it_is_referenced`.

### A4. Findings may cite the campaign's measurements

`src/optimization_framework/campaigns/findings.py`

- A finding may cite its own campaign's runtime profiles, equivalence checks, fixed-mask jobs and
  reports, and agents' saved analyses. Evidence of another campaign is still refused.
- Test: `tests/test_framework_manager_context.py::test_findings_cite_the_campaigns_own_measurements_and_saved_analyses`.

### A5. Every objective of the best design

`src/optimization_framework/execution/worker.py`

- A result gives `best_objectives` (for the 2D problem: TE, TM, their minimum and mean). A best
  design found before this change has none.
- Test: `tests/test_framework_worker.py::test_result_gives_every_objective_of_the_best_design`.

### A6. Packed designs in the journals

`src/optimization_framework/execution/metrics.py`, `worker.py`, `evaluation/jobs.py`, `execution/racing.py`

- A binary design of 128 cells or more is written to `requests.jsonl` and `observations.jsonl` as
  packed bits: 5.5 kB instead of 65.5 kB for 32,768 cells. Readers return the list as before, and
  journals written earlier still read.
- Expected for a 500,000-sample run: about 7 GB of journals instead of 66 GB (14 kB per
  evaluation instead of 133 kB). This is computed from the row sizes of the 12-hour runs, not
  measured on a run.
- Tests: `tests/test_framework_worker.py::test_journals_pack_a_long_binary_design_and_read_either_form`,
  `tests/test_framework_overhead.py::test_a_long_binary_design_costs_a_fraction_of_its_list_in_the_journals`.

### A7. Comparison of chosen experiments, a continued run read as one

`src/optimization_framework/analysis/general.py`, `contracts/commands.py`, `api/app.py`

- `comparison.report` takes `trial_ids`: only those experiments are compared, at the largest cost
  all of them reached. Without it one short run sets that cost for a whole study.
- A run continued with `trial.continue` is one row, named by its last experiment, with the earlier
  experiments' curves in front and their costs added; its method is the one it began as. An
  earlier experiment named on its own is the run as it stood then.
- Checked on the running service: the three 12-hour runs of campaign `738bf861` compare at 87,630
  evaluations (frame tokens 88.25%, coefficient tokens 84.12%, reference CNN 81.72%); the report
  is 66 kB and the Lead's receipt 2 kB. Each of those runs shows its last two experiments only:
  their first three hours were joined by the earlier warm-start workaround, which leaves no link.
- The report still takes about 26 s. Not addressed.
- Test: `tests/test_trial_continuation.py::test_a_comparison_reads_a_continued_run_as_one_and_compares_the_runs_it_is_given`.

### A8. Journal clean-up on the researcher's approval

`src/optimization_framework/execution/compaction.py`, `campaigns/commands.py`, `campaigns/research_commands.py`

The journals of every evaluated design are working data: a few gigabytes per long run are fine
while a campaign is analysed, and are removed afterwards, when the researcher confirms.

- The Lead files `journal.compaction_request` once results are analysed and recorded; it cannot
  run a compaction itself. The request appears in the decision inbox with the number of
  experiments, the gigabytes held, what stays, what is lost, and which of the experiments no
  recorded finding cites yet (it recommends keeping the journals while any is uncited).
- Approval runs `journal.compact` under the researcher's authority, one job at a time off the
  scheduler. The researcher can also issue `journal.compact` directly.
- What stays: every row and every number of both journals; the designs of each experiment's
  archive, of its 32 best evaluations and of 32 spread over the run; results, metrics, costs and
  saved states. What goes, for good: the other evaluated designs. They can no longer be
  snapshotted; an equivalence check replays the designs that stayed, and `trial.continue` is
  unaffected.
- A journal is replaced only after its copy reads back as the original without those designs.
  Only finished experiments are compacted, and compacting twice changes nothing.
- Tests: `tests/test_journal_compaction.py` (both), `tests/test_pi_agents.py::test_the_lead_may_ask_for_a_journal_compaction_but_not_run_one`.
- Not yet used on real data. The 72 GB of campaign `738bf861` is untouched.

## Part B — Agreed, not yet done

### B1. Record the transformer-policy experiment as findings — done

Ten published findings and one note superseding the stale finding
(`finding_pi_command_a2e616b9…`) were recorded on 13 October after the researcher's review
(ids in `runs/transformer-screen-20261012/journal/findings-recorded.json`; wording in
`findings-draft.md`, payloads in `findings-draft.json`).
Four are about the problem (efficiencies reached, comparison with the manuscript, evaluation
cost, what is unverified) and six about practice (a 1,024-design screen does not rank policies,
rankings change with budget, mid-run collapses, the CNN's plateau, measuring speed first, what was
not tested). Recording them exposed a fault in A4, fixed the same day: an equivalence record
names its problem by digest, which the publisher did not expect.

### B2. Rehearsal — done, see Part E

A scratch campaign from the preset at RCWA orders (10, 5) with a small budget. The researcher's
part is limited to what the interface offers (approve, choose finalists, waive); the Lead does the
rest: runtime profile, three hypotheses built from the method list and the referenced findings,
review, quick runs, finalist runs, `trial.continue`, a comparison of the finalists, a published
finding, and a request to compact the journals. It is
the acceptance test for "the Lead can run the workflow", and its profile gives the measured speed
at (10, 5) from which the new campaign's compute budget is set. Needs Part A live and B1 done.

### B3. Settings for the new campaign (the researcher's, at creation; no code)

- RCWA orders (10, 5): the preset default.
- 500,000 samples and 3 seeds per method, one environment: `max_steps: 500000`, continued with
  `trial.continue` when 24 hours is not enough.
- Compute budget: at the measured 20.6 evaluations per second for four concurrent runs, a
  500,000-sample run takes about 27 hours, so three methods with three seeds need about 250
  worker-hours (preset 69 h). This assumes a learner adds little to the probe's time, as in the
  earlier campaign, and it is for orders (10, 5), which Part F puts in question. Model budget
  about $25 (preset $5): the rehearsal used $1.02 in 52 minutes.
- Per-experiment limit for the Lead: at most 3,600 s. The contract caps it there, so an earlier
  version of this page was wrong to suggest 86,400 s. A run longer than an hour therefore goes one
  of three ways: the Lead starts it and files `trial.extension_request`, which the researcher
  approves once per run; the Lead carries it on an hour at a time with `trial.continue`; or the
  researcher launches it. Raising the cap is a code change, proposed in Part D.
- Reference the ten findings in the new campaign's Evidence view before activating the Lead.
- Worker ceiling: stay at 4 and waive the `unsaturated` flag, unless a higher ceiling is tried.

## Part C — Widening the Lead's access

The researcher approved points 1 and 2 on 13 October after the rehearsal; point 3 stays proposed.

### C1. Read access and its own analysis — implemented

`src/optimization_framework/agents/files.py`, `agents/tools.py`

- `file_list`, `file_read`, `file_search`: the repository, limited to the files git tracks, and
  the directories of the campaign's own experiments without their saved states.
- `analysis_run`: the agent's own Python script in a sandbox, with the experiments it names
  mounted read-only at `/data/<trial_id>`, numpy and the framework's journal readers importable,
  no network, no GPU, at most 300 s a run and 7,200 s per campaign.
- What the agent reads goes to its model provider. Outside the repository's tracked files and
  the campaign's experiments nothing is reachable: the private MEENT source and the manuscript lie
  outside the checkout, and credentials are untracked.
- Tests: `tests/test_pi_agents.py::test_agents_read_committed_repository_files_and_their_own_experiments_only`,
  `::test_an_agent_analyses_its_experiments_with_its_own_script_in_a_sandbox`.

### C2. Requests that apply themselves on approval — implemented

`campaigns/commands.py`, `campaigns/research_commands.py`, `contracts/commands.py`

- `approval.request` with kind `campaign_limits`, `waiver` or `worker_ceiling` puts a decision in
  the inbox; approval issues `campaign.update`, `validation.waive` or `workers.set` under the
  researcher's authority. Run extensions and journal compaction already worked this way.
- The per-experiment limit may now be set up to 86,400 s (it was capped at 3,600 s), in the
  campaign form as well. This goes with the request and was not separately confirmed.
- `workers.set` changes the ceiling of the running services at once and until they restart; the
  launcher configuration is not rewritten.
- Not covered: switching features on, and anything that needs a restart.
- Test: `tests/test_pi_agents.py::test_what_only_the_researcher_grants_is_asked_for_and_applied_on_approval`.

### Checked with a Lead on the running service

On 13 October the rehearsal campaign's Lead was asked to use the new tools. It found and read the
compaction rules in the repository's documents and stated them correctly; wrote a script and ran
it in the sandbox over four experiments (0.5 s, the evaluation counts and window medians it
printed match the records); and filed a request to raise the per-experiment limit from 1,200 s to
7,200 s, which was applied on approval as a budget amendment.

That check found a fault, now fixed (`agents/controller.py`): a researcher's message that arrived
while the Lead's last turn was calling the campaign complete was never delivered, because the
campaign was closed with the message still waiting. Test:
`tests/test_pi_agents.py::test_a_message_that_arrives_as_the_lead_finishes_is_still_delivered`.

### C3. Code changes, in dev mode only — proposed

Switch on the implementation workspace (a coding session in a container with a copy of the
repository) and add a path from an accepted commit to the running services. Dev mode is a server
started with a Pi profile (`pi_profile` in the launcher configuration, as on X670); a locked-mode
server would not offer this. Untested on this host, and the largest piece. Not built.

## Part D — Found on the running service

1. **A comparison had no common cost when one experiment was very short.** In campaign `738bf861`
   a 2.5-second rehearsal run emptied every method's summary; before that run existed the common
   cost was 473 s, the length of the quick screens, which is where the stale finding came from.
   Addressed by A7.
2. **The comparison report takes 26 to 28 s** on that campaign, whatever is compared. Not addressed.
3. **Recording a finding takes 13 to 29 s** on that campaign. Not addressed.
4. **The Lead cannot start a run longer than an hour.** `delegated_trial_seconds` is capped at
   3,600 s in the campaign contracts, while a 500,000-sample run takes many hours. Proposed: let
   the researcher set it up to 86,400 s, the limit a single experiment already has. Undecided.

## Part E — The rehearsal (13 October 2026, 14:32 to 15:24 KST)

Campaign `campaign_rehearsal-20261013-a`, created from the 2D preset at RCWA orders (10, 5) with the
ten findings referenced. It passed: the Lead carried out every step, and the researcher's part
was limited to what the interface offers.

| Who | What |
|---|---|
| Lead (24 commands) | Runtime profile; three hypotheses built from the method list, each citing referenced findings; a reviewer agent for them; six quick runs; two finalist runs of 900 s; two continuations of 300 s; a comparison of the two finalists by name; three findings (one published); a request to compact the journals |
| Researcher | Created the campaign, referenced the findings, activated the Lead; waived the `unsaturated` flag; approved the hypotheses; chose the finalists; approved the compaction |

- Cost: 52 minutes, 3,333 s of compute in 17 experiments, $1.02 of model use (97 calls). The
  Lead's session file ended at 0.7 MB and its largest tool result was 49 kB; the session that died
  in the earlier campaign had reached 113 MB.
- Measured speed at (10, 5): 8.1, 13.9 and 20.6 evaluations per second with 1, 2 and 4 workers.
- Journals: 193 MB for about 14,000 evaluations, 44 MB after the approved compaction.

Broken and fixed during the rehearsal:

1. A run that ended at its time limit could not be chosen as a finalist; only a run that completed
   its step count could. Fixed in `analysis/finalists.py` (test:
   `tests/test_framework_finalists.py::test_a_run_that_used_its_whole_time_allocation_can_become_a_finalist`).
2. The Lead could not read the result of the runtime check, a waiver or the shortlist. They are
   now readable (`agents/tools.py`). Not exercised again after the fix.

Seen and left as it is:

- After each answer from the researcher the Lead's next commands were refused once for an outdated
  guidance revision (four refusals); it re-read the revision and repeated them.
- Reading a result field of a run that is still going returns "The JSON pointer continues beyond a
  scalar value" (14 times), which does not say that the run has no result yet.

Not exercised: a failing run or a rejected hypothesis; implementing a new method; a run longer
than the Lead's limit; PPO at (10, 5); more than one reviewer or other agent roles.

## Part F — Scores at the searched RCWA orders do not hold at higher orders

First seen on 13 October while checking the rehearsal's best result, then tested on the three best
designs of campaign `738bf861`.

### What was run

- **Campaign records.** The campaign's convergence recipe (`meent_2d_order_convergence:v1`) on
  each long run's best design at orders (10, 8), (12, 10), (14, 12) and (18, 14): experiments
  `trial_3403da5762864d63` (frame-token), `trial_62c85acd1a8f4bca` (coefficient-token) and
  `trial_0a6c551f476149e0` (reference CNN). Its verdict compares the two highest orders with a
  tolerance of half a point.
- **Direct calls of the same evaluator**, not campaign records: orders (10, 5) to (26, 22), x and
  y raised separately, and y raised to 36 with x held at 14, for those three designs and for the
  rehearsal's evolution-strategy design. Kept in
  `runs/transformer-screen-20261012/journal/convergence-study-20261013.json`.
- Every score recorded at (10, 8) was reproduced to within 1e-13, and energy is conserved to
  2e-11 at every order.

### What it shows

Mean efficiency, with TE / TM in brackets (%):

| Design, and where it was found | (10, 5) | (10, 8) | (12, 10) | (18, 14) | (26, 22) | (14, 36) |
|---|---|---|---|---|---|---|
| Frame-token PPO, (10, 8) | 81.81 | **88.25** (81.8 / 94.7) | 86.48 | 77.33 (60.2 / 94.4) | 69.05 (43.8 / 94.3) | 70.41 |
| Coefficient-token PPO, (10, 8) | 79.42 | **84.12** (69.9 / 98.3) | 70.01 | 68.30 (37.9 / 98.7) | 67.48 (36.3 / 98.6) | 63.98 |
| Reference CNN PPO, (10, 8) | 76.36 | **81.72** (71.8 / 91.6) | 65.71 | 70.24 (48.4 / 92.1) | 70.50 (49.2 / 91.8) | 70.79 |
| Evolution strategy, rehearsal, (10, 5) | **93.75** (90.8 / 96.7) | 85.19 | 72.56 | 70.59 (44.5 / 96.7) | 67.56 (38.7 / 96.4) | 62.48 |

- **TM holds, TE does not.** From (10, 8) upwards TM moves by at most half a point; TE of the
  three campaign designs falls by 23 to 38 points by (26, 22).
- **The campaign's check fails two designs of three**: frame-token (5.3 points between its two
  highest orders) and reference (1.4). It passes the coefficient-token design (0.15), which had
  already fallen from 84.12% to 70.01% one step above the searched orders.
- **The ranking does not survive.** The designs are 6.5 points apart at (10, 8) and 3.0 at
  (26, 22), where the reference CNN's is highest.
- **Only the reference CNN's design settles**: 70.1 to 70.8% at all eight settings with 14 or
  more orders in each direction. The two transformer designs were still falling at (14, 36).
- **The orders that were short are those in y**, the direction of the 525 nm period. Raising
  only x from 10 to 26 moves the frame-token design from 88.25% to 86.19%; raising only y from 8
  to 22 moves it to 77.40%.
- **A search can gain from the truncation.** The evolution strategy reached 93.75% in 15
  minutes at (10, 5); that design scores 67.56% at (26, 22).
- **What it does not show**: the true efficiency of the transformer designs (still changing, and
  no second solver was used), or anything about designs this project has not evaluated, the
  manuscript's included.

### Where it is recorded

- Campaign `738bf861`: published finding `finding_finding-transformer-policies-convergence-20261013`,
  recorded as qualifying the findings on the efficiencies reached, the comparison with the
  manuscript and what was unverified. A new campaign should reference it with the other ten.
- The HTML report (`scripts/transformer_policy_report.py`): a section with a chart and both
  tables; the findings and the results table now lead with it.

### Consequence for the next campaign, undecided

RCWA orders (10, 5) match the manuscript's released code, and at those orders a score is cheap to
raise and does not transfer. One evaluation takes about 0.1 s at (10, 5), 0.2 s at (10, 8), 1.4 s at
(18, 14), 6 to 14 s at (22, 18) and 13 to 15 s at (26, 22), so a search of 500,000 samples cannot
run at orders where TE is stable. No step of the campaign workflow re-checks best designs at
higher orders unless someone asks for it.

### Faults found while running it, not fixed

1. The convergence recipe holds every order's solver in one job. With (22, 18) and (26, 22) in
   the ladder it ran out of GPU memory (15 to 22 GB), twice, while another job used part of the
   GPU. The recorded ladder therefore stops at (18, 14).
2. `validation.run` on a continued run did not answer within the request time (four attempts)
   when the run's best design had been observed in an earlier experiment of the chain: the
   service searches the whole journal for it. It answered at once for the experiment that found
   the design.
3. The evaluator loads the solver from a working checkout (`meent_2d_source`). That checkout was
   edited on 13 October at 16:29 KST while the services ran; later experiments record its source
   as modified. The scores reproduced, but nothing pins the solver for the length of a campaign.

## Part G — The Lead acts on requests (13 October 2026)

### What went wrong

A campaign created from the preset as a real campaign (`campaign_7cc90a3b`) had its agent team
started at 20:02 KST. The Lead began at once: it launched a runtime profile 29 seconds later, and
each probe that finished woke it again. Before the researcher's "stop" reached it, it had taken
8 turns and 86 model calls ($0.57) for work nobody asked for.

This was the design, not a slip of the model. Starting the team queued a first turn that said
"continue the campaign", the Lead's instructions said the researcher had "authorized end-to-end
work", every outcome in the campaign woke it with "inspect the records and continue", and the
Start panel announced that the lead agent "begins working at once". The rehearsal in Part E
showed that the Lead can carry out each stage. It is not how a campaign is meant to run: the
Lead designs and carries out experiments as the researcher asks, and does not run a programme.

### What changed

| Change | Where | Enforced by |
|---|---|---|
| Starting the team starts no turn; the Lead waits for the first message | `agents/controller.py` `activate` | The controller |
| A request is open from a researcher's message until the Lead reports it fulfilled; outcomes wake the Lead only while one is open | `controller.py` `_request`, `_campaign_events`, `_receive` | The controller |
| A runtime profile wakes the Lead once, at its end, not once per probe; a change the researcher makes to the campaign wakes nobody | `controller.py` `_campaign_events` | The controller |
| Resuming a paused team starts a Lead turn only if a request was in progress | `controller.py` `control` | The controller |
| Turns the platform starts are marked as notices, not requests | `agents/models.py` `NOTICE` | The Lead's instructions |
| The Lead's instructions: act on requests, one stage only when a request covers it, a question gets an answer | `agents/tools.py` `INSTRUCTIONS` | The model following them |
| The standing assignment replaces "continue the campaign"; saved teams are migrated at start-up | `agents/models.py`, `controller.py` `_migrate_records` | The controller |
| A turn returns everything the Lead wrote, not only its last line | `agent-harness/src/supervisor.ts` | The harness |
| The Start panel and the Lead's status say that it waits for a request | `frontend/src/models.tsx`, `piAgents.tsx` | — |

There is no setting that restores the old behaviour. A scripted run, such as a reproduction used
as a test, sends the Lead requests like a researcher would.

### Checked

- Automated: 1,296 tests pass (the full suite, before the harness change); the 49 agent tests and
  the 12 harness tests pass with it. New tests are in `tests/test_pi_agents.py`
  (`test_starting_the_team_starts_no_work_until_the_researcher_asks`,
  `test_outcomes_wake_the_lead_only_for_an_open_request_and_a_profile_reports_once`,
  `test_a_silent_turn_is_asked_for_its_checkpoint_not_told_to_continue`,
  `test_instructions_make_the_lead_act_on_requests_and_saved_assignments_follow`) and in
  `agent-harness/test/supervisor.test.mjs`.
- On the running service, after the restart: no campaign's Lead took a turn and no model call was made.
- With a Lead on DeepSeek V4 Pro, in a scratch campaign from the same preset and charter text
  (`campaign_service-check-20261013-a`, team stopped afterwards, $0.12 in all):

  | Step | What the Lead did |
  |---|---|
  | Team started, no message for 150 s | Nothing: no turn, no command, no model call |
  | "What can you do for me in this campaign, and what would you suggest as the first step?" | One turn, read the campaign and the host, answered, started nothing, closed the request ($0.03) |
  | "Create two hypotheses ..., each runnable with an installed method. Nothing else for now." | One turn, two `hypothesis.create` commands, no experiment, no profile, closed the request |

Not checked with a Lead: a request that starts experiments and is woken by their outcomes; a
request covering several stages; a subagent's result; an approval answered while no request is
open. The first of these is covered by the automated tests only.

### Seen during the check

- The first question's answer was 3,280 characters, written before the Lead's last tool call; the
  conversation showed only the 624-character closing line that followed. Fixed by the harness
  change above. A turn's message now also carries the short remarks the Lead makes between tool calls.
- After each researcher message the Lead's first commands are still refused once for an outdated
  guidance revision (Part E). Not addressed.

## Part G — Formulating a problem that is not yet defined (14 October 2026)

A campaign on a new kind of problem (sharing one LLM token budget among a team) began with a
statement its researcher could not complete, and the Lead's only surface for developing it was the
conversation. The [formulation notebook](formulation-notebook.md) is the durable surface: Markdown
cells with typeset equations and code cells, written by the Lead at the researcher's request, read
and edited by the researcher on the **Formulation** page.

What is implemented, each with tests (`tests/test_formulation.py`, `tests/test_formulation_runs.py`,
`frontend/tests/formulation.spec.ts`):

- Cells with a name, a status (proposed, agreed, open), a revision and a history. One edit entry
  point for the researcher's `formulation.edit` command and the Lead's `notebook_edit` tool: a batch
  is applied together or not at all, an update names the revision that was read, and a public name
  has one defining code cell.
- Only the researcher agrees to a cell. The Lead's change to an agreed cell needs a reason and
  makes it proposed again. A researcher's edit changes no guidance revision and starts no turn.
- Code cells run together in the sandbox of `analysis_run`, 90 s at most. `notebook_scratch` tries
  code against the notebook and keeps nothing; `notebook_run` keeps each cell's output for the page.
- `nb.show` of a Pyomo constraint, objective or model shows the equation generated from the code.
- Export as Markdown and as a marimo notebook; campaign removal keeps the notebook as Markdown.

Rehearsal on X870E, campaign `campaign_formulation-rehearsal-20261014` (a two-product bakery, stated
incompletely on purpose), Lead on `deepseek-v4-pro`, three requests, 17 model calls, $0.11:

| Request | What the Lead did |
|---|---|
| Start the notebook | Read the guide, then wrote 16 cells in one batch: the problem in words, sets, parameters, decisions, objective, three constraints with code, and four open questions |
| Change a constraint the researcher had agreed to | Saw the two cells the researcher had changed, read them, and changed three cells in one batch with a reason; the agreed cell became proposed again |
| Add a toy instance and solve it | Tried it in the scratchpad, learned that the notebook's own cells failed, reordered and fixed them, ran the notebook, and reported the answer with a hand check |

Seen and corrected during it: the Lead linked cells without the `#`, which would have left the
dashboard; the conversation now reads such a link as a link to the cell.

Not exercised: a specialist reading the notebook; a Pyomo model written by a Lead (the printer and
HiGHS were checked in the sandbox directly); a notebook near its limits; the agents' 3,600 s
allowance running out. Not built: turning an agreed formulation into a problem that campaign
experiments can run. The problem contracts take one flat vector of one kind and dense linear
constraints, which a mixed-integer model does not fit.

## Not planned

- Several parallel environments, Stable-Baselines3 PPO, and parity with MEENT 0.9.5 for the new
  campaign.
- An HTML report command; the summary step is findings plus the interface's comparison views.
- Reviving the old campaign's Lead session.
