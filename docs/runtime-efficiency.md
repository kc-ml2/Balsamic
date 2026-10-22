# Runtime efficiency: measured, gated and owned

Balsamic runs problems from many fields and cannot contain speed-ups for each one's tools. It
treats speed the way it treats correctness instead: it measures every evaluator with one
standard procedure, holds long runs until that measurement has been judged, leaves the
tool-specific fixing to the campaign's agents under a cap, and makes a faster evaluator safe to
adopt. The platform owns the measurement, the gate and its own overhead; the project owns the
optimization.

## The runtime profile

`runtime.profile {task_id}` measures a task's evaluator on this host. It needs an idle
workspace and takes a few minutes. It runs short ordinary trials of the built-in random search
(30 s each by default) at 1, 2, 4… concurrent workers, for each device the task's runs may be
placed on: the GPU slots up to `gpu_workers`, then the CPU slots up to the rest of `workers`
([server control](server-control.md#gpu-solver-and-concurrent-trials)). While it measures, the
scheduler starts nothing but its probes. Their cost is charged to the campaign like any trial.
Each device is judged on its own load (a CPU level by its share of the physical cores) and gets
its own recommendation; the summary adds the two devices' speeds, since they run side by side.

For each level it records evaluations per second (over the time all workers of the level were
evaluating), CPU and GPU load, the platform's share of each step, bytes stored per evaluation
and solver executions per evaluation. From these it derives the recommended number of workers,
evaluations per hour, and flags:

| Flag | What was measured | Holds long runs |
| --- | --- | --- |
| `no_scaling` | Two workers deliver less than 1.15× one while the device is under 60% busy | Yes |
| `unsaturated` | Throughput was still growing by 15% or more at the worker ceiling | No, a note |
| `idle_device` | Throughput stops growing while the device is under 60% busy | Yes |
| `platform_overhead` | More than 20% of a step is spent outside the evaluator | Yes |
| `storage_heavy` | An evaluation stores more than four candidates' worth (at least 64 kB) | Yes |
| `repeated_solves` | An evaluation runs the solver more than once | Yes |

A flag is a symptom, not a diagnosis: Balsamic does not know why a tool behaves that way.
`unsaturated` is a note: it says a higher worker ceiling might be faster, and the ceiling is the
researcher's launcher setting, not something the evaluator wastes. A profile whose only flags are
notes passes; the note is shown on the Resources page and in the view agents read (`note_flags`),
and the lead agent mentions it and carries on. A profile judged before a flag became a note is
judged again on its next read, which appends a passing result to its requirement. A study can
change the limits with `validation_policy.runtime_thresholds`
(`scaling_gain`, `busy_percent`, `overhead_share`, `storage_candidates`, `storage_bytes`).

The Resources page shows each task's profile under **Evaluator speed**, and agents read the
same view with `resource_inspect`. GPU load is read from sysfs, or from `nvidia-smi` for
NVIDIA devices; where neither is available it is reported as unknown.

## The gate

A profile answers for one state of its subject: the task's problem, the scientific source, the
host, and the launcher's resource settings (`workers`, `gpu_workers`, `cuda_mps`). The
scientific source is the digest every experiment records (the source files a worker loads for
the problem), so a change elsewhere in the platform leaves a profile valid, and it is the digest
an equivalence record names. When any of them changes, the profile, the requirement and its
waivers read as stale and a new measurement is needed; returning to a measured state makes its
profile current again.

An ordinary run of `runtime_gate_seconds` or longer (launcher setting, default 600; `null` sets
no gate) is refused until the subject has a completed profile with every flag that holds runs
fixed or waived.
Shorter runs, the profile's own probes, recipes and frozen protocols are not gated. A workspace
started without the launcher has no gate.

The requirement is a `runtime_efficiency` validation requirement and appears under
**Validation**. Only the researcher waives it, unless the study delegates waivers to the
manager. A waiver covers the flags of the profile it cites as evidence; citing the subject
record itself waives the check for the subject's current state altogether, for example when the
workspace cannot be made idle to measure. Measuring again keeps a waiver when the new profile
raises only flags the waiver covered; a flag it did not cover ends it. This holds for any
validation result that names its flags (`measurements.flags`): a later failure ends a waiver only
when it names something the results the waiver cites did not. A waiver still belongs to one
requirement, so a new campaign, study, source, host or launcher setting needs its own.

## Who fixes a flag

The lead agent is told to profile before planning long runs, to plan budgets from the measured
evaluations per hour, and for a flag to delegate one bounded optimization assignment in an
implementation workspace. It stops when the last attempt gained less than it cost or the
allowance is spent, and then asks the researcher. The allowance is the time the campaign may
spend on profiling: a twentieth of its compute budget unless the campaign record sets
`runtime_optimization_seconds`. The blocker states what a 10% gain is worth in hours of the
campaign's unallocated compute.

Two things stay with the researcher: launcher settings such as the worker ceiling and device
sharing, and built-in evaluators, which can only be changed in the repository.

## Adopting a faster evaluator

`evaluator.equivalence {trial_id}` replays a sample of a finished experiment's recorded
candidates (the best ones and an even spread, 12 by default) under the current source and
compares every objective and constraint with what was recorded, within 1e-9 absolute and
relative by default. The result is an `evaluator_equivalence` record from the old source digest
to the new one. It is evidence for the sampled candidates, not a proof, and it only makes sense
for deterministic evaluators.

`trial.continue {trial_id, wall_seconds}` queues a new experiment that goes on from where a
finished one stopped: the optimizer's saved state and the archive come along, and the new
experiment keeps its own counters, budget and journals. If the scientific source changed since
the first experiment ran, a passing equivalence record for exactly that change is required;
only the researcher can pass `accept_unverified`, and the chain is then marked unverified. The
comparison analysis reads a chain as one run on cumulative axes.

Experiments that complete by optimizer decisions, and experiments of frozen protocols, are not
continued this way.

## The platform's own overhead

`tests/test_framework_overhead.py` bounds what the worker itself writes for a free evaluator
with a large candidate: journals grow by at most three candidates per evaluation, the progress
record rewritten after every step holds the best candidate but not the archive (which goes to
`archive.json` only when it changes), and the state response is encoded once per snapshot. The
`platform_overhead` and `storage_heavy` flags report the same quantities for real runs.

A long search of large designs leaves gigabytes of journal. Looking up one recorded design, or
choosing the sample of an equivalence check, reads the journals for their scalar fields and
reads whole only the records it uses; the scheduler waits while such a command runs, so the
same test checks that the other designs are not parsed.

Cataloging a finished experiment's outputs reads each output's own dependencies, not the whole
asset catalog, so its cost does not grow with the number of experiments in the workspace.

The profile measures what a worker does. It does not see what the service does between
experiments, such as cataloging evidence, during which the scheduler starts nothing.
