# Adaptive algorithm-testing protocol

This protocol allocates compute to promising methods, then tests the selected
methods on fresh seeds. The existing pilot is development evidence; its changing
rankings and limited PPO training do not support final elimination.

## Goal and limits

- Four-hour batches, with a sixteen-hour total elapsed-time cap.
- Primary objective: mean TE/TM +1 diffraction efficiency on the current paper
  problem. Report TE, TM, minimum polarization efficiency, seed variation, and
  failures separately.
- Initial ceiling: four simultaneous numerical jobs, including validation.
- Keep H12 on hold. Do not spend its implementation allocation.
- Conclusions apply to this physical condition and the tested horizons. This is
  not a reproduction of the paper's complete training protocol or evidence of
  generalization across grating problems.

Use staged adaptive resource allocation informed by
[Hyperband](https://jmlr.org/papers/v18/16-558.html) and asynchronous scheduling
informed by [ASHA](https://arxiv.org/abs/1810.05934), with additional protection
for methods that need longer learning horizons.

## Test order

| Stage | Tests | Decision enabled |
| --- | --- | --- |
| A: numerical and resource checks | Actual 2D binary-mask reevaluation and forward/gradient throughput, memory, and concurrency measurements | Trustworthy scores and a safe execution profile |
| B: longer development | Eight pilot configurations, seeds 17, 41, and 73; cumulative checkpoints at 15 and 30 minutes | Leaders, late improvement, instability, and insufficient training |
| C: adaptive follow-up | Continue promising runs toward 60 minutes, add seeds to uncertain candidates, or investigate stalled learning | Freeze two finalists |
| D: fresh-seed confirmation | Two finalists plus random search; seeds 1001 through 1010; 60 minutes per run | Reproducible improvement at a declared budget |
| E: final numerical checks | Higher-order reevaluation of confirmation designs and complete report | Separate screening scores from numerically stable device performance |

Stage A comes first. Select one median-performing design from each configuration,
plus Adam's unusually high-scoring, asymmetric design. Evaluate orders
`(10,5) -> (12,6) -> (14,7)` and extend the ladder when necessary and memory
permits. Require the final two settings to agree within 0.005 in mean, TE, and TM,
with existing energy-conservation checks. Repeat the final setting and require
agreement within 1e-6. This checks consistency over the tested truncations, not
independent-solver correctness.

If fidelity changes materially affect scores, choose a common higher optimization
fidelity and start separately identified trials. Never pool different fidelities.
Resume pilot checkpoints only when configuration, fidelity, source, and runtime
remain compatible; preserve optimizer, RNG, archive, and cumulative cost.

Initially prioritize motif surgery, residual PPO, Adam, and random search. Then
fill the remaining configurations. Give all eight their development allowance
before allocating heavily to a narrow shortlist.

## Parallelism and accounting

Start at two workers. Calibrate counts 1, 2, 3, and 4 and numerical thread counts
1, 2, and 4, subject to memory and CPU limits. Select the highest sustained
throughput for representative forward and gradient work.

The initial calibration measures identical finite forward and gradient work,
including startup. Slow first gradients receive longer allocations within the
unchanged fifteen-minute calibration envelope. This provides an initial resource
profile; its reported cold-start throughput is separate from sustained throughput
in the longer optimization runs.

- Keep 4 GiB of available RAM as headroom. Admission must account for predicted
  new-job peak memory and outstanding growth of running jobs.
- Run at most one high-order validation job at a time. All numerical jobs share
  the worker ceiling.
- Reject settings with sustained swapping or numerical discrepancies. Investigate
  the workspace's observed memory growth before increasing parallelism.
- Distinguish elapsed protocol time from aggregate worker time. Charge
  initialization, gradients, policy training, checkpoint work, and recovery.
- Reserve at least 20% of development compute for uncertain candidates, protected
  learners, and mechanism checks. PI may choose their order and purpose within
  the protocol, recording evidence and rationale.
- Checkpoint and report at each batch boundary. An independent deadline guard
  enforces the overall cutoff even if PI or the application is unavailable.

With four safe workers and compatible checkpoints, extending 24 runs from nine
to thirty minutes takes approximately 126 elapsed minutes. Continuing three
configurations across three seeds from thirty to sixty minutes takes another
68 minutes. Including thirty minutes of checks, this is about 3h44m. These are
estimates: fewer workers or higher fidelity carry unfinished work to the next
batch without changing the overall cap.

## Choosing the next test

Compare confirmed incumbent scores at equal cumulative worker time. Do not
extrapolate an unobserved endpoint.

Before deprioritizing a configuration, require three development seeds, the
thirty-minute checkpoint, and these minimum activity checks:

- PPO: ten policy updates and one completed episode.
- Adam: fifty gradient steps and one complete continuation cycle.
- ES/DE: twenty population-sized update cycles verified by diagnostics.
- Nested Fourier: one hundred proposals and a verified transition mechanism.

These are screening allowances, not convergence certificates. An unmet milestone
means undertrained or unassessed, not ineffective.

| Observation | Next action |
| --- | --- |
| Among the three strongest mean scores | Prioritize continuation or replication |
| At least two seeds gain 0.02 over the previous rung | Extend the horizon |
| Removing one seed changes the shortlist | Add a seed at the same horizon |
| Learning or representation progression appears stalled | Focused mechanism check before replicas |
| All three seeds trail the leader by over 0.02, gain under 0.01 over the last rung, and meet activity checks | Deprioritize at this horizon |
| Numerical validity fails | Hold affected evidence and repair or reevaluate |

Additional development seeds are 101, 131, 173, 211, and 257. Configuration changes
create new identities and cannot be pooled with predecessors.

## Confirmation and interpretation

Freeze two finalists, the baseline, runtime, fidelity, and seeds 1001 through 1010
before inspecting confirmation outcomes. The primary endpoint is mean incumbent
efficiency at sixty minutes. Run 20,000 seed-block bootstrap resamples and adjust
intervals for the three method comparisons.

A finalist is confirmed promising when its lower confidence bound exceeds the
baseline by 0.02 and it wins in at least eight of ten blocks. Claim superiority
over the other finalist only when that comparison also clears the margin.
Otherwise report competitive or inconclusive evidence. Same seed labels do not
mean identical starting gratings; this compares complete methods, including
their intended initialization.

At sixteen elapsed hours, stop and publish all evidence, including missing cells
and censored trajectories. Do not manufacture a conclusive result.

## Application requirements and acceptance

- Durable controller above the existing scheduler: `study.race.create`,
  `study.race.control`, and `study.race.decide`; status at
  `GET /api/v1/studies/{study_id}/race`.
- Persist protocol, method identities, seed assignments, checkpoints, rung
  endpoints, resource leases, and every decision.
- Separate renewable batch execution leases from scientific allocations;
  continuation cannot erase costs or change confirmation settings.
- Time-budget endpoint eligibility is separate from optimizer convergence.
  Expected time exhaustion remains usable evidence.
- A reviewed 2D RCWA recipe is required; the existing 1D convergence recipe is
  unsuitable.
- PI receives compact scalar summaries and evidence references. Display clocks,
  budgets, concurrency, maturity, decisions, and TensorBoard progress.
- Verify continuation without lost state or double charging; crossed learning
  curves; slow learners; outlier rankings; numerical failures; memory pressure;
  deadline/restart recovery; censored endpoints; and frozen confirmation rosters.

Researcher-approved defaults: four-hour batches, at most sixteen elapsed hours,
mean TE/TM objective, and fresh-seed confirmation rather than screening alone.

## Running this protocol

The current execution is `race_d8a1473dd2da45a5`, launched on October 1, 2026.
Its fixed elapsed cutoff is **October 2, 2026, 07:39 KST**. H12 remains held.
Numerical preflight comes before resource calibration and algorithm allocation;
a failed numerical check remains visible and triggers a bounded higher-order
follow-up when memory permits. Unresolved checks cannot become ranking evidence.

The source fixture list and operator receipts are saved under
`runs/pilots/adaptive-20261001/`. The frozen protocol and controller decisions
are in the workspace database. The **Studies** page displays protocol progress;
**Compare results** displays TensorBoard curves.

To reconcile or restart this execution's preflight operators, run from the
repository root:

```bash
.venv/bin/python scripts/launch_adaptive_grating_study.py
```

This reuses the existing authorization, command receipts, fixture roster, and
verified operator process identities. It does not restart finished development
or create another protocol. Once preflight is accepted, the workspace service
owns all subsequent allocation and reporting.

Progress and reports:

- `runs/pilots/adaptive-20261001/preflight/preflight-report.md`
- `runs/pilots/adaptive-20261001/resources/calibration.json`
- `runs/workspace/workspace/races/race_d8a1473dd2da45a5/status.json`
- `runs/workspace/workspace/races/race_d8a1473dd2da45a5/report.md`

An independent process enforces the fixed elapsed cutoff. Service restart
preserves running workers and reuses the deadline guard. The final report
distinguishes completed confirmation, numerical uncertainty, and censored work.
