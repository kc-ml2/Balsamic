# Start and stop the review servers

Run these commands on X670 as `chs`, from the repository directory. The launcher
starts the workspace and the **normal implementation service** in the background,
then enables the workspace's private Tailnet route. Both services use the same
explicit model configuration. It does not use the mocked implementation reviewer.

```bash
cd ~/Work/dqn-meent-latest
./scripts/labctl start --replace-fixture   # first handover from the review fixtures
./scripts/labctl status
./scripts/labctl stop
./scripts/labctl start
./scripts/labctl restart
./scripts/labctl logs                     # Ctrl+C stops following logs only
./scripts/labctl logs workspace
```

The first handover retains the campaigns you created. `--replace-fixture` matches
the old fixture's checkout, data directory, role, port and OS user before stopping
it. Other servers occupying those ports must be stopped by their owner. The
original services on 8765/8766 are independent of this launcher.

The [default configuration](../deploy/review-servers.json) selects:

| Setting | Value |
|---|---|
| Workspace | `http://127.0.0.1:8791` |
| Implementation library | `http://127.0.0.1:8790` |
| Tailnet UI | `https://x670.tail096b61.ts.net:8449` on this machine |
| Data | `runs/consolidation/experiment-controls-20260927/destination` |
| Frontend | The saved `experiment-controls-20260927/frontend-dist` build |
| Models | Codex, `gpt-6-sol`, enabled for both services |

The launcher discovers the host's actual Tailnet name and prints its URL.
Services keep running when the launching terminal closes. They are not registered
for automatic startup after reboot; run `start` again then.

## Authentication and model configuration

Before enabling models, verify Codex authentication as the same OS user:

```bash
codex login status
# If sign-in is needed:
codex login --device-auth
```

Follow the browser instructions from [OpenAI's authentication guide](https://learn.chatgpt.com/docs/auth#login-on-headless-devices).
The launcher checks saved ChatGPT authentication without making a model call.
Model access and allowance are checked by actual research requests. Existing
queued or delegated work may use the enabled provider when services resume.
Subscription mode has no automatic paid API fallback.

Edit `model`, `provider`, or `llm_enabled` in `deploy/review-servers.json`, then run
`restart`. `provider` may be omitted or `null` (no provider, the default) while
`llm_enabled` is `false`, or while a Pi harness (`pi_port`) supplies its agents' models.
`provider` and `model` only set the earlier campaign manager's research roles; a campaign
whose agent team has started is managed by the lead agent, on the model chosen on the
Models page. Without a `pi_profile`, set `pi_provider` (for example
`"openai-codex"`) to name the single provider Pi campaigns may use; none is assumed. For an inspection session with all model calls disabled:

```bash
./scripts/labctl start --replace-fixture --no-llm
# Or, if already managed and running:
./scripts/labctl restart --no-llm
```

`--no-llm` is a per-invocation override. Set `llm_enabled` to `false` in the config
to make that the persistent default. The application reads neither shell `.env`
files nor model credentials from this JSON file. Existing provider environment
variables (for example `GRATING_CODEX_BINARY`) can be set in the invoking shell.

Set `codex_timeout_seconds` in the server JSON to persist the per-call Codex
deadline (5–600 seconds). The discovery review instance allows 600 seconds for
extra-high reasoning. This limit does not change model-call counts, API budgets,
or implementation job allocations; an implementation job's remaining deadline
can shorten a call. Timeout receipts retain unknown subscription usage, and
failed tasks require an explicit retry rather than an invisible repeat call.

Set `paper_reference` to a PDF path (`~` and repository-relative paths are
accepted) to copy that paper into each development workspace as
`/references/paper.pdf` and `paper.txt`. It is omitted by default; outside the
launcher, set `GRATING_PAPER_REFERENCE` instead. Reference code is copied from
`../flrl` when that checkout exists, or from `GRATING_FLRL_REFERENCE`.

## GPU solver and concurrent trials

These optional settings concern the 2D MEENT evaluator:

| Setting | Effect |
|---|---|
| `gpu_workers` | How many of the `workers` slots compute on the GPU, one run each (default `0`); the rest compute on CPU, where a run takes one slot per numerical thread. At least one slot stays on CPU. `scripts/start` sets one GPU slot when PyTorch sees a CUDA device. The former `meent_device` and `meent_2d_source` settings are refused. |
| `cuda_mps` | `true` starts a CUDA Multi-Process Service daemon for this OS user and points the services, and therefore their workers, at it. |
| `runtime_gate_seconds` | Runs at least this long wait for a [runtime profile](runtime-efficiency.md) of their evaluator. Default `600`; `null` sets no gate. |
| `workers` | How many trials run at once: the GPU slots and the CPU slots together. `scripts/start` uses one per physical core, less one kept for the system and the services. |

The scheduler places each run when it starts and records the device on it: on the device the run names (`device` on a
trial), on its race's or confirmation protocol's device once one of its runs has started, and otherwise on the GPU when
its evaluator can use one and a GPU slot is free, else on CPU. Only the 2D MEENT evaluator can use the GPU; the 1D
evaluator, commissioned evaluators, sandboxed runs and fixed-mask jobs compute on CPU. A CPU worker sees no CUDA device.
The evaluator check before a run covers every device the task's runs may take. Worker-seconds on the GPU and on CPU buy
different numbers of evaluations, so the comparison report keeps the devices apart on `worker_seconds` and pools them on
`evaluation_requests`.

Without MPS, concurrent GPU workers take turns and add no throughput. With it, an
RTX 5090 at orders (10, 8) reached 4.9, 7.7, 9.4, 10.5 and 11.6 designs per second
for 1, 2, 3, 4 and 6 workers, shared evenly. Each worker holds about 5 GB of GPU
memory. One worker is as fast with MPS as without.

A trial budgeted in wall-clock time gets fewer evaluations when it shares the GPU,
so compare such trials only when they ran with the same number of neighbours.
Under MPS a fatal GPU fault in one worker can end the others. `stop` quits the
daemon unless a worker that outlived the servers is still using it. Its log is in
`<directory>/.server-control/mps/`; other CUDA programs are unaffected.

## Tailnet permissions

Connect the machine to Tailscale beforehand. The launcher uses
[`tailscale serve --bg --https=PORT TARGET`](https://tailscale.com/docs/reference/tailscale-cli/serve)
and removes that route with the matching `off` command. It never runs `tailscale
down`, `serve reset`, or Funnel. Only the UI is exposed; the implementation service
remains on localhost and uses its token file.

If Tailscale requires administrator permission, the launcher invokes `sudo` for
the specific route command and lets your terminal request the password. Run
`labctl` itself as your normal user. Without an interactive terminal, it prints
the exact command to run. If route setup fails, the local services remain running;
retry `start` after fixing the permission. If route removal fails, `stop` still
stops the managed servers and retains the route record so a second `stop` can
complete cleanup. A conflicting or shared route is left unchanged. `restart` keeps the route when the
new configuration asks for the same one, so it needs no administrator permission.

## State, shutdown and other instances

PID identities, active configuration and append-only logs live in
`<directory>/.server-control/`. PID birth identity and Linux process handles
prevent a stale record from signaling an unrelated process. Repeated `start` and
`stop` calls are safe. Startup failures clean up newly started services. Shutdown
sends SIGTERM to the workspace before the library and waits; a slow shutdown is
reported without escalating to SIGKILL. Campaign data and library artifacts are
retained.

Stopping the servers is not a campaign/experiment stop command. Use the workbench
controls to pause or stop experiments before shutting down if you want numerical
work to stop too. Independently supervised experiment workers can outlive the
web server; interrupted model or implementation work follows the application's
existing recovery and reconciliation rules. Previously recorded fixture reviews
remain fixture evidence after the handover.

For a separate instance, copy the configuration and choose a new data directory,
frontend build and three distinct ports. Paths in the configuration are relative
to the repository, not the calling terminal. Set `tailnet_port` to `null` for local
access only. Use the same config file for each lifecycle command:

```bash
./scripts/labctl start --config /path/to/servers.json
./scripts/labctl stop --config /path/to/servers.json
```

Stop the old instance before changing its data directory, because each directory
has its own process registry. Do not edit `.server-control/state.json` manually.
