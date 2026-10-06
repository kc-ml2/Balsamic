# Resource observability and RCWA memory planning

The **Resources** dashboard shows current host and worker usage alongside the next
planned work, with measured values and forecasts labeled separately. Open
[Resources on Tailnet](http://strixhalo.tail096b61.ts.net:8765/#resources), or select
**Resources** in the application navigation. Reading the dashboard does not launch
an optimization trial, RCWA evaluation, calibration, or benchmark.

The read-only API is `GET /api/v1/resources?campaign_id=<campaign ID>`. The PI can
read the same campaign-scoped view with `resource_inspect`. This makes resource
constraints visible to both the researcher and the agent before allocating work.

## Could the previous memory hold have been predicted?

**The growth and risk were predictable from configuration and source code without
running the simulator. Exact peak RAM and the necessary convergence order were
not.** The earlier protocol should have displayed the entire fidelity ladder's
resource forecast at the start instead of revealing the next memory hold only
after completing lower-order checks.

There are two different sources of memory growth in this implementation:

1. With Fourier orders `(n_x, n_y)`, the harmonic count is
   `N = (2 n_x + 1) (2 n_y + 1)`. A complex128 `N × N` matrix requires
   `16 N²` bytes; a `2N × 2N` electromagnetic block requires `64 N²` bytes.
   MEENT keeps several such matrices and creates additional intermediate results.
2. The current CPU evaluator uses discrete Fourier analysis with MEENT's default
   `enhanced_dfs=True`. Before the FFT, MEENT repeats the **whole input raster**.
   For positive orders, a `256 × 128` raster becomes
   `256 (4 n_x + 2) × 128 (4 n_y + 2)`. These large grids consume more memory than
   the single electromagnetic matrix at the orders considered here.

The second behavior matters especially in this campaign:

| Fourier orders | Harmonics | Expanded FFT raster (width × height) | One complex FFT raster | Known retained FFT arrays, lower bound |
| --- | ---: | ---: | ---: | ---: |
| `(10, 5)` | 231 | 10,752 × 2,816 | 0.451 GiB | 1.130 GiB |
| `(14, 7)` | 435 | 14,848 × 3,840 | 0.850 GiB | 2.132 GiB |
| `(18, 9)` | 703 | 18,944 × 4,864 | 1.373 GiB | 3.455 GiB |
| `(22, 11)` | 1,035 | 23,040 × 5,888 | 2.021 GiB | 5.102 GiB |

The FFT lower bound counts the real epsilon grid, the converted complex grid,
the first FFT's complex output, and three allocated convolution matrices per
layer. These arrays are simultaneously retained in the installed MEENT 0.13.2
code. It excludes transient normalization arrays, FFT/LAPACK scratch, allocator
retention, runtime overhead, prior solver state, and autograd. It is **not** a safe
peak-memory requirement or a reason to override admission checks.

The matrix lower bound separately counts three convolution matrices per layer,
the retained `W`, `V`, `A_i`, and `B` matrices per layer, and the current `F`, `G`,
`T`, and `X` blocks. The estimator takes the larger of these two bounds, rather
than adding allocations belonging to different execution stages. Gradient-based
optimization may retain additional computation graphs; an observed forward-only
validation peak does not establish its memory requirement.

This analysis comes from the installed package's
`on_torch/emsolver/convolution_matrix.py:to_conv_mat_raster_discrete`,
`fourier_analysis.py:dfs2d`, `_base.py:solve_2d`, and
`transfer_method.py:transfer_2d_1`, `transfer_2d_2`, and `transfer_2d_3`.
The campaign uses that Torch CPU path in
[Meent2DEvaluator](../src/dqn_meent/problem_2d.py).

## What the previous hold actually meant

The largest recorded completed-validation worker high-water mark at `(18, 9)` was
**6,653,087,744 bytes = 6.196 GiB**, from PPO median validation
`trial_8b63ec712a1544d4`. The process identity was checked when sampling. Its
`VmHWM` describes the whole worker's lifetime peak after completed evaluations,
not the isolated memory allocation of one particular solve. Repeated polls of
that worker are not independent measurements.

The admission policy predicted `(22, 11)` with:

```text
predicted peak = 2 × 6,653,087,744 × (1,035 / 703)²
               = 28,841,862,122 bytes, rounded up
               = 26.861 GiB
```

The factor of two is an uncertainty allowance. Four GiB of host memory headroom
is then reserved separately. At the decision time, available RAM was 17.644 GiB,
so the proposed check was held. **The 26.861 GiB value was a conservative forecast;
no `(22, 11)` solve was launched or measured.** The raw, unmargined extrapolation
was 13.431 GiB; it is also an estimate.

The harmonic-squared RSS model assumes dense matrix growth. From `(18, 9)` to
`(22, 11)`, dense matrix sizes grow by about 2.17 times, whereas the expanded FFT
grid grows by about 1.47 times. If FFT arrays dominate a worker's peak, scaling
the whole RSS quadratically and adding a factor of two can overestimate its next
peak. Conversely, solver scratch, retained arrays, and gradients can add memory
not captured by the source-based lower bounds. A measured resource profile is
needed to replace the uncertainty allowance with a tighter estimate.

Preflight paused the protocol on October 1, 2026 at approximately 16:40 KST. The
fixed deadline expired October 2 at 07:39 KST. Approximately 29.6 aggregate worker
minutes were used; the remaining elapsed window was idle. Resource calibration
and the new algorithm trials did not run. A deadline-elapsed protocol must not
appear as ongoing compute merely because its configuration rows remain eligible.
H12 remains on hold.

## Read the dashboard

Use measured host RAM, available RAM, swap, CPU activity, and identity-checked
live worker RSS to answer **what is using resources now**. A process high-water
mark is historical peak usage; it should not be added to current RSS.

Worker rows cover the verified owner process. Sandbox descendants are not
included in that row's RSS or CPU; the host process list can expose additional
consumers. GPU driver memory is a separate pool and must not be added to process
RSS or host RAM. Worker rows are scoped to the selected campaign, while host
measurements and the configured worker ceiling belong to the shared workspace.

Live readings refresh every five seconds. The graphs retain the last sixty
samples while the page is open; historical preflight measurements and admission
decisions remain in the saved campaign evidence. Accounting projections refresh
on numerical events and have a thirty-second cache fallback, with their age
included in the API. A failed or timed-out fetch leaves the last successful
sample visible and labels it stale.

Use the planned-work view to answer **what could run next**. Compare each order's
source-based array lower bound and conservative predicted worker peak with
available RAM and the headroom reserve. The forecast's reference order, observed
peak, uncertainty factor, and source trial make the estimate reviewable. An
unknown estimate is shown as unknown rather than zero. Estimated safe concurrency
is a memory-only upper limit, subject to CPU, configured worker limits, campaign
state, scientific prerequisites, and the remaining deadline.

The protocol view separates elapsed allowance from aggregate worker time and
shows paused, blocked, and deadline-expired states. Planned capacity does not mean
resources have already been allocated, and an eligible method is not necessarily
executing. Monitor the hold reason alongside utilization: a mostly idle host
with a held next stage calls for revising the plan, rather than waiting for a
calibration or optimization job that has never been created.

## Plan an order without running MEENT

[rcwa_memory.py](../src/optimization_framework/execution/rcwa_memory.py) uses only
the Python standard library. It does not import Torch or MEENT, allocate the
predicted arrays, sample performance with a solver, or change campaign state.
From the repository root:

```bash
.venv/bin/python -m optimization_framework.execution.rcwa_memory \
  --order-x 22 --order-y 11 --grid-x 256 --grid-y 128
```

To reuse existing measurements and compare against a specified amount of
available RAM:

```bash
.venv/bin/python -m optimization_framework.execution.rcwa_memory \
  --order-x 22 --order-y 11 \
  --samples runs/pilots/adaptive-20261001/preflight/preflight.json \
  --available-gib 17.64
```

The JSON includes harmonic and matrix sizes, expanded FFT dimensions, analytical
lower bounds, the separately labeled heuristic forecast, observed reference peak
and fidelity, measurement provenance, headroom, and estimated safe concurrency.
Without measurements, it uses the existing conservative 5 GiB planning allowance
at `(14, 7)`; that allowance already includes conservatism and is not doubled.

The source-based planner cannot establish the Fourier order necessary for a
particular grating's numerical convergence, exact optimizer runtime, or exact
peak memory. Those require measurements. Planning and live monitoring make that
uncertainty visible before a campaign spends its elapsed allowance.
