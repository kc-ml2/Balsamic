# Optimization Lab — experimental optimizer research framework

A general research harness for users and LLM agents to develop an effective optimizer for the problem at hand. The framework is reusable; finding a universal optimizer is not its goal. It combines durable campaigns, problem/evaluator plugins, literature evidence, independent strategy proposals, numerical experiments, and a separate implementation/validation service.

**This is a partial, experimental implementation.** The current branch contains real GPT-6 Luna discovery runs and real numerical experiments, but the complete autonomous analysis → literature → proposal → experiment → critique → revision cycle is not yet qualified. See the [publication checkpoint](docs/publication-checkpoint.md) for working features, test results and known failures.

The React dashboard exposes scientific work products and ordinary agent dialogue, tool receipts and append-only logs. The reference scientific application is binary **1D grating inverse design using real MEENT RCWA evaluations**. A bounded continuous benchmark exercises the general problem interface.

The original reconstruction of the DQN optimization loop in Seo et al., *Structural Optimization of a One-Dimensional Freeform Metagrating Deflector via Deep Reinforcement Learning*, ACS Photonics 9, 452–458 (2022), remains available alongside random search, restart hill climbing, annealing, block tabu, population, and surrogate search. The workspace supports reviewed custom optimizer source. Implementation and smoke tests do not establish a state-of-the-art result.

## Start here

The only requirements are Linux, Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/), and Node.js 22.19 or newer with npm. From a fresh clone:

```bash
uv sync --frozen --all-extras
(cd frontend && npm ci && npm run build)
uv run --no-sync optimization-lab --directory runs/workspace --workers 2 --port 8765
```

Open **http://127.0.0.1:8765**. Create a campaign, define development configurations and budgets, then launch a small experiment or discuss a hypothesis with the research partner. Data remain in the workspace directory after closing the browser.

**No model provider is selected by default.** The dashboard and numerical experiments work immediately, and research requests remain in the campaign's durable inbox until you choose a provider and enable model calls. Research roles accept `codex` (your ChatGPT subscription allowance, tracked separately from paid API spending), `openai_api` or `compatible`, and default to `gpt-6-sol` once a provider is chosen. Pi campaigns need a dev profile with a default model, or a launcher `pi_provider`. Existing `.key` credentials are ignored unless you explicitly enable an API provider, and there is no automatic paid fallback. See the [workspace guide](docs/workspace.md) when you are ready to configure a provider, and for workflows, custom algorithms, and recovery.

### Optional components

Each component below enables one feature; everything else runs without it.

| Component | Enables | Setup |
|---|---|---|
| Codex CLI | Research roles with the `codex` provider | `codex login --device-auth`; see [server control](docs/server-control.md) |
| Pi agent harness | Pi campaigns and `scripts/pi-dev` | `(cd agent-harness && npm ci && npm run build)`; sign in from the notebook, or put provider keys in `~/.config/balsamic/secrets.env` ([Pi runtime](docs/pi-agent-harness.md)) |
| bubblewrap (`bwrap`) | Executing generated and custom optimizer packages | Distribution package; Ubuntu also needs `deploy/grating-bwrap.apparmor` ([Pi runtime](docs/pi-agent-harness.md#host-prerequisite-found-during-rollout)) |
| Docker | Full development workspaces with a browser IDE | `docker build -t grating-implementation-workspace:pi-0.87.1 deploy/implementation-workspace`, then set `development_enabled` |
| `mask-optimizers` 0.1.0 | The `motif_surgery`, `nested_fourier` and `phenotype_de` methods | The `masks` extra, included by `--all-extras`, from [kc-ml2/mask-optimizers](https://github.com/kc-ml2/mask-optimizers); see [optimizer plug-ins](docs/optimizer-plugins.md) |
| Paper and FLRL references | `/references` inside development workspaces | Set `paper_reference` in the launcher JSON or `GRATING_PAPER_REFERENCE`; clone `jLabKAIST/flrl` beside this repository or set `GRATING_FLRL_REFERENCE` |
| Tailscale and `socat` | Remote access to the dashboard | [Server control](docs/server-control.md) |
| Chrome or Playwright Chromium | Browser tests | `npx playwright install chromium` |

The launcher configurations and service units in `deploy/` describe particular hosts; copy and edit one for a new machine.

Git does not carry `runs/` (campaign databases, checkpoints and logs), provider credentials (`.key`, `~/.codex`, `~/.pi/agent/auth.json`, `~/.config/balsamic/secrets.env`), or sibling checkouts. To continue existing campaigns on another machine, stop the lab and copy the workspace directory, for example `rsync -a runs/workspace/ other-host:dqn-meent/runs/workspace/`.

The [implementation evidence map](docs/implementation-status.md) connects the [research-system plan](docs/agentic-algorithm-discovery-plan.md) to code, tests, and unverified research outcomes.

The [proposal exploration guide](docs/proposal-exploration.md) covers additional ideas, diversification, two-parent hybrids, independent conceptual review, and implementation requests. A [recorded Luna hybrid review](docs/grating-hybrid-review.md) shows the generated mechanism and the reviewer's concrete objections.

The [model control panel](docs/model-control-panel.md) configures campaign defaults and per-role model/reasoning settings, including the separate implementation service.

For the original single-run numerical interface:

```bash
uv run --no-sync dqn-meent train --config configs/smoke.json --output runs/smoke
uv run --no-sync dqn-meent evaluate --run runs/smoke --orders 5 15 25 40
uv run --no-sync dqn-meent design --run runs/smoke --output runs/smoke/design.png
uv run --no-sync pytest -q
```

The full test suite takes about 15 minutes on CPU. If you install without the `masks` extra, the tests in `tests/test_mask_library_campaign.py` and one native-confirmation test in `tests/test_framework_racing.py` fail with "Install mask-optimizers 0.1.0".

The lock file pins the environment. PyTorch's default Linux distribution can download several GB of CUDA dependencies even when using CPU. A GPU is **not required**. This setup uses NumPy/complex128 for MEENT and CPU PyTorch by default; `training.device="cuda"` moves only the Q-network, not the optical solver. Small Q-networks and 1D RCWA are often suitable for CPU. The CLI limits BLAS to one thread; training likewise defaults to one PyTorch thread.

The included configurations serve different purposes:

| Config | Cells | RCWA truncation F / harmonics | Interactions | Purpose |
|---|---:|---:|---:|---|
| `smoke.json` | 16 | 5 / 11 | 256 | Verify installation, replay updates, checkpoints and evaluation |
| `starter.json` | 64 | 15 / 31 | 10,000 | Explore training at moderate cost; reevaluate at higher F |
| `validation64.json` | 64 | 40 / 81 | 4,096 | Short full-geometry run executed for this delivery |
| `paper_geometry.json` | 64 | 40 / 81 | 200,000 | Larger experiment retaining the paper's geometry; still a modified learning algorithm/budget |

None is a promise that its interaction count is enough to learn a competitive design. F is the **maximum Fourier order**, not the total number of retained harmonics. The number of binary design cells and the RCWA basis size are independent.

## What the agent controls

The default device has a 325 nm patterned silicon layer, 64 binary cells per period, and normally incident TM light from a glass half-space (`n=1.45`). Light exits into air (`n=1`). At wavelength 1100 nm and target angle 50°, the period is `1100/sin(50°)` nm. The objective is **absolute transmitted power in diffraction order +1, divided by incident power**; it is not the fraction of total transmitted power.

The default real silicon index, 3.551726470588235, comes from interpolation of the authors' material table at 1100 nm. Their released simulator discards absorption. Changing wavelength requires supplying the corresponding index too; documented values for 900 and 1000 nm appear in `docs/paper-mapping.md`. Alternatively select `physics.material="meent_green"` for the wavelength-dependent complex Green-2008 table included with MEENT. That is a different material model and includes absorption.

| Component | Implementation |
|---|---|
| Design/state | Binary Si/air array; network receives ±1 cells plus remaining episode fraction |
| Action | Choose a cell and flip its material; repeated flips are legal |
| Initial design | All silicon, at each episode reset |
| Forward model | MEENT 0.13.2, NumPy complex128, continuous integration of piecewise-constant cells, TM |
| Reward | `eta(next)^3`, matching the article's reward |
| Episode | 128 actions for the 64-cell configuration; clock is observed; terminal transition does not bootstrap |
| Agent | MLP 128–128 with ReLU, epsilon-greedy actions, replay, Double DQN, target network, Huber loss, Adam, gradient clipping |
| Output | Best design encountered during search, final policy, full checkpoint, CSV metrics, high-order evaluation |

```mermaid
flowchart TD
    A["Binary design + remaining steps"] --> B["Q-network: one value per cell"]
    B --> C["Epsilon-greedy cell flip"]
    C --> D["MEENT RCWA or exact-design cache"]
    D --> E["+1 efficiency and reward"]
    E --> A
    E --> F["Replay buffer"]
    F --> G["Double DQN update + target network"]
    G --> B
    D --> H["Best-design archive"]
    H --> I["Higher-order RCWA reevaluation"]
```

DQN does not differentiate through RCWA. Only the Q-network's regression loss is differentiated; each discrete design is evaluated by the forward solver. This avoids requiring eigenvalue gradients but does not remove forward-solver accuracy requirements.

The default reward favors repeated occupancy of high-efficiency states; it does **not** optimize only final-state efficiency or the best state seen. We therefore preserve the best discovered design separately and evaluate the final greedy policy separately. The optional `training.reward_mode="difference"`, with `gamma=1`, makes the episode return telescope to final efficiency minus initial efficiency. It changes the objective and is not the paper's reward.

The observed clock and terminal treatment intentionally differ from the original released code, which bootstraps across episode cutoffs. Double DQN is also a deliberate change. See `docs/paper-mapping.md` for the full correspondence and original hyperparameters.

## Run an experiment and compare searches

```bash
uv run --no-sync dqn-meent train --config configs/starter.json --output runs/dqn-s0 --seed 0
uv run --no-sync dqn-meent baseline --config configs/starter.json --output runs/random-s0 --method random --budget 10000 --seed 0
uv run --no-sync dqn-meent baseline --config configs/starter.json --output runs/hillclimb-s0 --method hillclimb --budget 10000 --seed 0
uv run --no-sync dqn-meent evaluate --run runs/dqn-s0 --orders 15 25 40 60
uv run --no-sync dqn-meent evaluate --run runs/random-s0 --orders 15 25 40 60
uv run --no-sync dqn-meent evaluate --run runs/hillclimb-s0 --orders 15 25 40 60
uv run --no-sync dqn-meent plot --runs runs/dqn-s0 runs/random-s0 runs/hillclimb-s0 --output runs/comparison.png
```

Random search generates independent uniform binary designs after the initial all-Si design. Hill climbing proposes a random single-cell change and restarts after `2*N` rejected moves; it is not the article's exhaustive depth-1/depth-2 greedy baseline. Use several seeds for each algorithm before making comparative claims. Train a separate model for each wavelength/angle; this initial framework does not claim cross-condition generalization.

A DQN training budget counts actions. Baseline budgets count evaluation requests, including the initial design. DQN also evaluates the initial design on each episode reset; after the first reset these normally hit the cache. The CSV logs both actual RCWA calls and cache hits, so compare actual solver calls and wall time as well as action count. Baseline and training each have separate caches. Higher-order evaluation uses fresh solvers and is a separate validation expense.

**Always reevaluate the discovered geometry.** Training-order conservation of energy does not demonstrate Fourier convergence. `evaluate` reports each order, all transmitted/reflected diffraction efficiencies, and the last-two-order difference. Its 0.005 default tolerance is 0.5 percentage points; passing that one diagnostic is not a proof of convergence. For publication, increase orders further, examine more than one geometry, and compare with an independent solver.

## Outputs and resume

A training run produces:

- `config.json`: exact resolved physics and learning configuration.
- `metrics.csv`: per-action efficiency, best-so-far efficiency, reward, exploration, loss, solver calls, cache hits and wall time.
- `best_design.npy` and `best_design.json`: the best encountered geometry (0 = air, 1 = silicon).
- `checkpoint.pt`: online/target networks, optimizer, replay and resumable state.
- `summary.json`: final run counts and timing.
- `evaluation.json` after evaluation: high-order best-design results and a separate greedy-policy rollout.

To resume an **interrupted run with the same configuration and original total-step schedule**:

```bash
uv run --no-sync dqn-meent train --config configs/starter.json --output runs/dqn-s0 --seed 0 --resume runs/dqn-s0/checkpoint.pt
```

Do not modify the step count to extend a completed checkpoint: that changes the exploration schedule. Start a new run for a new experimental budget. Checkpoints contain Python/PyTorch objects; load only checkpoints you trust. New runs reject nonempty output directories to protect existing results.

## Code map

- `src/dqn_meent/workspace/`: API, persistent records, worker service, adaptive research, literature metadata, statistical comparisons, and isolated custom optimizers.
- `frontend/`: React/TypeScript dashboard and browser interaction tests.
- `docs/workspace.md`: installation, research workflows, provider setup, and custom optimizer protocol.
- `docs/optimizer-plugins.md`: how optimizer libraries are packaged, pinned and registered as plug-ins.
- `src/dqn_meent/config.py`: validated experiment configuration.
- `physics.py`: MEENT adapter, material resolution, physical checks and bounded LRU cache.
- `environment.py`: binary design MDP and Gymnasium interface.
- `dqn.py`: Q-network, replay, action selection and Double DQN targets.
- `training.py`: experiment loop, logging and checkpoint/resume.
- `experiments.py`: baselines, high-order evaluation and plots.
- `tests/`: Fresnel/energy/symmetry/material checks, environment semantics, Bellman targets and restart behavior.
- `docs/paper2agent-assessment.md`: the actual Paper2Agent attempt and its limits.
- `docs/validation.md`: measured results and remaining limitations for this delivered setup.

## Paper2Agent and source provenance

[Paper2Agent](https://github.com/jmiao24/Paper2Agent) was actually run on the requested PDF: preparation, seven-page extraction, and review-aid generation succeeded. The extraction was not promoted to a fully reviewed reading skill. Its paper-ingestion tooling helped; the scientific training framework was implemented directly around MEENT. This delivery does not require an LLM service or MCP server during numerical training.

Sources: [paper](https://www.janglab.org/documents/publications/2022ACSPhotonics.pdf), [authors' code](https://github.com/dongjin-seo2020/1DFreeFormDQN), [MEENT](https://github.com/kc-ml2/meent), [Double DQN](https://arxiv.org/abs/1509.06461), [Gymnasium termination semantics](https://gymnasium.farama.org/tutorials/gymnasium_basics/handling_time_limits/).

The published reference design in `tests/fixtures/` comes from the authors' MIT-licensed repository; its license and source attribution accompany it. A reference-design agreement validates the solver mapping, not the success of a newly trained agent. The article PDF and third-party repositories are not redistributed in this package.
