# Fourier optimizers for the 2D MEENT campaign

These installed methods share `dqn_meent.fourier.FourierGeometry` and the
campaign's `Meent2DEvaluator`. They execute through the ordinary experiment
worker, including its request journal, wall limit, observations, checkpoint
and best-binary-design archive. No optimizer calls MEENT outside that worker.

| Method | Executed mechanism |
| --- | --- |
| `flrl_lsf_random` | Independent seeded Gaussian coefficients, L2 normalization, binary threshold |
| `flrl_lsf_es` | Gaussian evolution strategy, elite recombination and adaptive diagonal mutation scale |
| `flrl_ppo` | Gaussian PPO coefficient increments, clipping/scaling, L2 normalization, stacked observations and the released CNN architecture |
| `flrl_autograd_adam` | Sigmoid index relaxation, electromagnetic autograd, Adam ascent, reference learning-rate/beta schedules and multistart epochs |
| `flrl_ppo_polish` | Raw PPO action followed by exactly two Adam steps with beta 5 then 10; score all three binary candidates and retain a strict improvement or roll back |
| `flrl_ppo_residual` | Bounded tangent Adam proposal plus a tanh Gaussian policy residual; signed hard-score rewards, deterministic tangent frames and moment transport |

The independent symmetric Fourier ordering and endpoint-inclusive grid follow
`jLabKAIST/flrl` commit `7838e71313d71cee8e2db3b432f41f80b9106a95`.
At mode limits (8,4), there are 85 real coordinates and a 256×128 binary mask.
The implementation evaluates the same basis with separable float64 operations.
Imaginary coefficients on the central column are constrained to zero.

The PPO and Adam baselines are campaign adaptations, not exact reproduction of
the released executable or evidence that its reported efficiency was achieved.
PPO uses a worker-driven PyTorch rollout implementation instead of SB3's
synchronous environment loop. It retains the reference CNN, raw Gaussian PPO
likelihoods, reward normalization, time-limit bootstrapping and configured PPO
hyperparameters. Adam retains persistent moments and the released detached
full-coefficient normalization, in the explicitly independent real basis.
Hard binary monitoring is always enabled, including when relaxed scores fall.

The reference timestep/sample counts describe the reproduction profile; the
experiment's frozen evaluation and wall limits determine actual work. Each run
uses fixed mode limits. `level_set_mode_pairs` documents the planned comparison
grid; it does not silently change a run's basis. The default ES population is
16 with four elites. Hybrid defaults are an action radius of 0.1 and two
macro-steps for polishing; residual control uses radii 0.1, four tangent axes,
a 128-step episode/restart patience and a declared beta stage at 128 decisions.
These defaults are correctness-tested starting settings, not tuned winners.

## Physical work and continuation

A proposal can request a gradient at the same coefficients that produced its
binary mask. The evaluator validates that correspondence. It returns hard
TE/TM/mean/min objectives and puts relaxed scores and coefficient gradients
only in observation metadata. Each polarization forward solve is counted;
one uncached hard score plus one relaxed gradient costs four forward solves.
Backward calls are recorded in gradient metadata and their time is included in
worker cost. Soft objectives never replace the binary campaign objective.

Hard-mask cache keys include the evaluator, configuration and fidelity identity.
Cache hits retain the previous hard scores and use zero additional hard solves.
Returning from B to cached A has the ordinary signed score-difference reward;
only staying on the current mask has zero reward. Gradient solves on a hard
cache hit still cost two solves. The cache is bounded to 256 entries and saved
in the evaluator checkpoint.

Checkpoints contain the coefficient trajectory, RNG state, Adam moments,
policy/value weights, PPO optimizer state, pending action, unfinished rollout,
reward statistics, local-polishing phase and evaluator cache. A restart cannot
silently replace a partially completed polishing transition or policy update.

## Verification

`tests/test_flrl_optimizers.py` checks analytic Fourier modes and symmetry,
finite-difference agreement for both relaxed material maps, deterministic
checkpoint continuation through PPO updates, tangent-frame invariants,
zero-gradient ablation, cached-return rewards, polishing rollback and worker
cost/archive integrity. Full campaign-configuration smoke reports are retained
under `runs/implementation-validation/`. These are correctness checks, not
matched-budget optimization comparisons or RCWA convergence studies.

The obsolete, review-rejected H08 draft is not bound to the corrected H09
implementation. Existing independent conceptual reviews remain separate from
these implementation checks. Native bindings retain old algorithm settings in
an immutable `builtin_binding` record rather than creating duplicate proposals.

## Standalone mask library in this campaign

`motif_surgery`, `nested_fourier`, and `phenotype_de` are implemented in the
separate `mask-optimizers` package at source commit
`9b38b8cdc81065196dbf7eb03895bc3f34719e4f`. The campaign adapter
converts its x-major masks to the MEENT evaluator's y-major candidate order,
then returns measured hard-mask utility through the library's ask/observe
contract. No simulator code is copied into the numerical library. The adapter
requires version `0.1.0`; if that version is absent, method readiness reports
the missing installation rather than treating a proposal as runnable.

The library is published at [kc-ml2/mask-optimizers](https://github.com/kc-ml2/mask-optimizers),
tag `v0.1.0` (the same source commit). It is the optional `masks` extra, so
`uv sync --frozen --all-extras` installs the locked commit. The adapter registers
these methods as an optimizer plug-in; see [optimizer plug-ins](optimizer-plugins.md)
for the design and for releasing a new library version.

`tests/test_mask_library_campaign.py` checks the boundary's mask order,
y-reflection, exact checkpoint continuation, 256×128 construction, one
direct MEENT evaluation and a three-step checkpointing campaign worker run
per method. These checks establish campaign execution compatibility, not
search effectiveness. H12 remains on its separate protected
implementation-service review path; the standalone library's H12 numerical
port is not substituted for that campaign verdict.
