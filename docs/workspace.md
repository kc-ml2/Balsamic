# Grating Lab workspace guide

For recorded training, baselines, inference, evaluation and generic commands,
see the [CLI research guide](cli-research.md).

Grating Lab helps a researcher choose useful experiments and improve optimization strategies. The system keeps persuasive rationale, measured outcomes, and research decisions distinct. It does not automatically declare an algorithm the winner of an irreversible tournament.

## Install and start

The local pilot uses Linux, Python 3.12 or 3.13, `uv`, and Node.js/npm. Run from the repository root:

```bash
uv sync --frozen --all-extras
cd frontend
npm ci
npm run build
cd ..
uv run --no-sync grating-lab --directory runs/workspace --workers 2 --port 8765
```

Open **http://127.0.0.1:8765**. `--workers` controls concurrent numerical trial processes, not web server processes. Use one service instance per workspace; a file lock prevents two schedulers from owning the same records. The default listener is local (`127.0.0.1`). This pilot does not provide authentication or a multi-user security boundary for a public deployment.

The frontend build is served by FastAPI. For frontend development, keep the API on port 8765 and run `npm run dev` in `frontend/`; Vite uses port 5173 and proxies `/api` to the service. API contracts are available at **http://127.0.0.1:8765/docs**.

CPU execution is supported. PyTorch's Linux package may download CUDA dependencies even when running on CPU. The worker fixes BLAS thread settings so each solver does not independently occupy every core; concurrent runs can still compete for CPU and memory. Use dedicated, comparable worker allocations for final timing claims.

## Configure the research models

For the current review deployment, use the [server control script](server-control.md)
to start or stop both services and their private Tailnet route with one command.
Its explicit configuration enables Codex for both services; `--no-llm` disables
model calls for an inspection session.

**No model provider is selected by default**, and model execution is **disabled by default**, so you can configure both later. Once you choose a provider (`codex`, `openai_api` or `compatible`), every research role uses `gpt-6-sol` unless you set another model. Starting without a provider does not launch Codex or read `.key`. Numerical experiments, strategy records, and the notebook remain available. Research requests stay in the durable campaign inbox until model access is configured. One manager issue explains the blocker; enabling a provider lets the serialized queue continue with current guidance.

The Python supervisor uses a small adapter to run Codex noninteractively for bounded, structured research calls. It does not require this conversation or an interactive Codex session to remain open. Configure the local Codex installation with subscription authentication when you are ready, then start or restart the workspace with:

```bash
GRATING_LLM_PROVIDER=codex GRATING_LLM_MODEL=gpt-6-sol GRATING_LLM_ENABLED=true \
  uv run --no-sync grating-lab --directory runs/workspace --workers 2 --port 8765
```

These are instructions for later setup; installation, login, and model calls are not performed automatically. If the provider is unavailable or its quota is exhausted, the affected research run stops with an inspectable error. The application does not silently change models, switch to the API, or spend the existing `.key` balance.

Codex calls consume the authenticated account's subscription allowance. They have call, elapsed-time, and response-byte limits, and record tokens when reported, but their API-dollar cost is **not applicable**, rather than zero or free. The campaign's **API spending cap** applies only to explicitly enabled paid API transport. Historical API estimates remain visible separately from subscription call counts. The application does not estimate your remaining subscription quota.

| Variable | Purpose |
|---|---|
| `GRATING_LLM_PROVIDER` | Default `none`. `codex` selects the subscription CLI; `openai_api` and `compatible` explicitly select API transports. |
| `GRATING_LLM_MODEL` | Default `gpt-6-sol` once a provider is selected; all research roles use this model. |
| `GRATING_LLM_ENABLED=true` | Opt in to model execution after configuring the selected provider. Default is disabled. |
| `GRATING_LLM_DISABLED=true` | Disable model calls and retain queued research requests, overriding `GRATING_LLM_ENABLED`. |
| `GRATING_CODEX_BINARY` | Codex executable name or path; defaults to `codex`. |
| `GRATING_CODEX_TIMEOUT_SECONDS` | Codex call timeout; defaults to 120 seconds, clamped to 5–600 seconds. |
| `GRATING_LLM_REASONING_EFFORT` | Default `low`; applied by the selected adapter when supported. |
| `GRATING_LLM_API_KEY` | API transport only: explicit provider credential. |
| `GRATING_LLM_KEY_FILE` | API transport only: path to a plain-text key file. Relative paths use the service's working directory. |
| `GRATING_LLM_BASE_URL` | API transport only: endpoint URL, including `/v1` when required. |
| `GRATING_LLM_INPUT_USD_PER_MILLION` | API transport only: input token price used for estimates and caps. |
| `GRATING_LLM_OUTPUT_USD_PER_MILLION` | API transport only: output token price used for estimates and caps. |

Direct OpenAI API access remains an explicit alternative: set both `GRATING_LLM_PROVIDER=openai_api` and `GRATING_LLM_ENABLED=true`, choose its model, and provide known prices when required. Only that explicit mode can use the repository's default `.key`. An alternative endpoint uses `GRATING_LLM_PROVIDER=compatible` and needs its own explicit credentials when authentication is required; it never inherits the default OpenAI key. Native Claude and Pi adapters are not part of this change.

Never put credentials in a campaign, hypothesis, source record, screenshot, or committed example. `.key` and `.env` files are ignored by Git; the application does not automatically load `.env`. The dashboard receives only a safe configuration summary. Numerical workers do not inherit model key environment variables, and the custom optimizer sandbox receives neither the repository nor the key file.

Codex checks for saved ChatGPT subscription authentication before each request and rejects API-key authentication. It runs a fresh, ephemeral reasoning turn with tools disabled and a structured response schema; it does not reuse an interactive conversation. The configured output-token allowance is a prompt instruction for Codex, not a hard token cap exposed by the CLI. The adapter enforces a 2 MiB combined output limit and an elapsed-time limit, while the supervisor enforces the role-call cap.

Codex may retry transport connections internally within that overall timeout. The application does not retry a failed research call or switch to a paid provider automatically.

Paid API usage uses configured price estimates, not invoice reconciliation. A finite API dollar cap requires known prices. Reservations are written before provider execution, and uncertain interrupted calls remain inspectable. Malformed responses are recorded and are not automatically retried. Subscription and API transports share structured role results while retaining their distinct accounting.

## A practical first campaign

1. **Create the charter.** State the research objective, numerical compute cap, optional API spending cap, validation reserve, and autonomy preference. Add development configurations and, when appropriate, selection and test configurations. Each configuration produces a separate binary grating.
2. **Check the physical formulation.** The implemented objective is absolute transmitted power in order +1. Physics JSON sets the cells, wavelength, target angle, thickness, refractive indices, material model, and Fourier order. Objective prose does not install a new numerical objective. Multiwavelength performance of one shared device requires an additional objective implementation.
3. **Launch a bounded baseline.** Start random search or restart hill climbing on a development case. Record the question, seed, request budget, time limit, and search schedule. Start a second run to compare mechanisms.
4. **Use the hypothesis board.** Inspect assumptions, failure modes, sources, cheapest checks, and lineage. Add an idea, annotate a critique, fork a strategy, request an independent generation discussion, or nominate an implemented method as a finalist.
5. **Choose the next action together.** Research discussions may generate, review, compare, evolve, design a probe, or return to problem formulation. Resolve proposed actions in the decision inbox, or launch a more specific experiment yourself. A startup-limited surrogate or DQN run can warrant extension rather than rejection.
6. **Compare and validate.** Inspect matched task/seed comparisons and both solver-call and elapsed-time views. Request physical validation of archived binary designs at increasing Fourier orders. Keep early development conclusions narrower than generalization claims.
7. **Export the research record.** The notebook exports a Markdown report containing the charter, configurations, hypotheses, trials, and decision history. Keep the underlying workspace directory for executable provenance and checkpoints.

Task selection uses observed plateaus, disagreement between methods, task coverage, numerical reliability, and measured cost. A uniformly hopeless, flat case is not automatically more informative than a moderately hard one. Probe scores are transparent heuristics; they are not calibrated estimates of information gain.

In **guided** mode, proposals wait in the decision inbox. **Delegated** mode can launch an eligible bounded development probe when the campaign is idle, the charter is current, and no research decision blocks it. Completion of a numerical batch can trigger another short research discussion. Identical probes are rejected, researcher-stopped branches do not trigger retries, and unresolved allocations return to the researcher. Manual experiment controls remain available throughout.

The material defaults use a fixed silicon index. When changing wavelength, set the intended index or choose `material: "meent_green"` for MEENT's dispersive complex material table. These are different physical conditions and are separated in comparisons. `fourier_order: F` retains `2F + 1` harmonics; this parameter is independent of the number of binary cells.

## Give an idea feedback and request a revision

Open an idea from **Hypotheses**, then use **Feedback and agent critiques**:

- **Save comment** records your feedback on this idea. It makes no model call.
- **Revise with my feedback** saves any text in **Your feedback**, then asks the agents to revise the selected idea using its saved researcher comments. The request captures those comments at that moment; later comments can guide another round. Your original idea remains available, and new revisions appear as linked descendants.
- **Ask agents for critique** requests an assessment of assumptions, weaknesses, and useful checks. Agent critiques are saved on the idea and summarized in the research conversation. This action does not create a revised idea.
- **Edit a copy myself** opens the manual idea editor with the selected idea as its parent.

A revised idea shows **What changed**, the agents' response to your feedback, and the exact **Feedback used**. An explanation is a proposal for your assessment, not proof that the revision works. Open a descendant, add another comment, and repeat to continue the conversation about that idea.

Revision and critique require a configured model. Comments can still be saved while setup is deferred or a discussion is running. If the comment is saved but the revision request fails, the comment remains available for retry. Revive archived ideas before revising them. Neither targeted critique nor targeted revision automatically launches numerical experiments, including in delegated mode; proposed experiments remain available for a separate decision.

This implements an individual idea's feedback loop. Batch keep/reject decisions, broader kill/revival scopes, and a general scientific knowledge library remain planned extensions. Campaign memory and the executable implementation library are available separately.

## Experiments and controls

The built-in methods are uniform random search, restart hill climbing, Double DQN, simulated annealing, adaptive block tabu search, population search, and a ridge-kernel surrogate method. The surrogate is a lightweight implementation; it is not an implementation or reproduction of BOCS. Fourier-informed search, relaxed gradients, and adaptive portfolios begin as dossiers until a runnable implementation is supplied.

All workspace methods submit binary designs to the same trusted evaluator. The request count includes initial designs and cache hits; actual RCWA calls, cache hits, execution time, and optimizer diagnostics are recorded separately. Comparative trials have independent solver caches. DQN has an explicit, immutable exploration schedule horizon; extending the allocation preserves that schedule.

| Control | Meaning |
|---|---|
| Pause | Request a checkpoint at the next safe evaluation boundary. A running solver call may delay the pause. |
| Resume | Continue a compatible checkpoint with retained algorithm/RNG/cache state. Unsupported failure states require a new trial. |
| Stop | Record researcher intent, request cooperative termination, and escalate to the owned process group after a grace period. Completed evidence remains visible. |
| Extend | Increase request/time allocations within the campaign cap; preserve the original algorithm and schedule. |
| Queue priority | Reorder queued experiments without interrupting other workers. |
| Validate designs | Run a separate, metered convergence check over archived designs and specified Fourier orders. |

Browser closure does not control numerical jobs. The service reconciles worker identities and artifacts after restart; an interrupted compatible trial can be resumed explicitly. A researcher stop is never an instruction for automatic retry. A deliberate new extension or resume is recorded as a new control action.

Changing physics, source, or algorithm configuration creates a new experimental identity. Charter revisions retain earlier snapshots and make stale recommendations inspectable. Test trials require explicitly confirmatory execution from a nominated finalist. Development reasoning excludes locked test observations.

Finalist nomination freezes its algorithm/configuration, custom source identity, scientific implementation files, and numerical dependency versions. The first accepted confirmation launch conservatively exposes that physical condition and records the cohort of finalists already nominated at that moment. Members of that cohort can collect further seeds; newly invented or evolved finalists require fresh physical conditions. Renaming a task, revising the charter, or changing only Fourier order/cache settings does not make an exposed condition fresh. A configuration already used in development cannot be relabeled an untouched test.

Each finalist's first confirmation run also freezes its complete training configuration, schedule horizon, request allocation, and time cap; the replicate seed remains selectable. Later confirmation runs must match that protocol. Confirmation allocations cannot be extended in place. Nominate all intended competitors before launching the first confirmation, and choose the first allocation deliberately. Frozen source is copied when the run is queued and checked again, together with dependency versions, before the worker starts. Re-nomination of the same hypothesis does not erase a freeze or exposure history.

## Read comparisons correctly

The comparison service groups observations by physical configuration, charter, fidelity, objective, and numerical label. Algorithm configurations, source hashes, shared-data conditions, and DQN schedules distinguish method variants.

- Paired differences use matched tasks and seeds at a common **observed** solver or time budget. No stopped trajectory is projected into an unobserved future.
- Full-allocation endpoint comparisons require matching allocations and completed runs. Campaign interruptions that occur early remain partial.
- Cross-task summaries weight compatible tasks equally, then compare paired seeds within each task. Bootstrap intervals describe observed variability, and single observations have no estimated interval.
- Duplicate seed records are not cherry-picked into pairs. Repeated or missing seeds do not produce an independent-seed confidence interval.
- Threshold hits are descriptive counts over observed allocations. Censored runs and the cost among successful runs are shown explicitly; the latter omits failures.
- Best screening efficiency, a convergence-checked device, and typical algorithm performance answer different questions. The dashboard does not turn a persuasive dossier into measured superiority.

A small last-two-order difference is a convergence diagnostic, not a complete physical certificate. Publication claims still need more extensive order studies and an independent solver comparison. No independent electromagnetic solver has been added to this workspace.

## Bring papers and new executable strategies

Open **Research notebook → Source library** to search arXiv/Crossref or retrieve a paper by DOI/URL. The same operations are available through the source APIs:

| Endpoint | JSON body |
|---|---|
| `POST /api/sources/search` | `{"campaign_id":"…","query":"binary grating optimization","provider":"arxiv","limit":5}` |
| `POST /api/sources/ingest` | `{"campaign_id":"…","identifier":"https://arxiv.org/abs/2406.12904"}` |
| `POST /api/sources` | Researcher-provided `campaign_id`, `title`, `url`, optional `excerpt` and `supports`. |

Search providers are `arxiv` and `crossref`. Ingestion recognizes DOI/arXiv identifiers, the supplied Nature article URL, and citation metadata on a small primary-source allowlist. Retrieval has size/time/result limits and does not follow arbitrary redirects. A successful metadata fetch verifies bibliographic content and available abstracts; it does not establish the paper's claims or applicability. Unavailable sources remain explicit failures.

New strategies use the independent [implementation service](implementation-service.md). An idea can have **Implementation missing**, **Implementation validation required**, a job state, or **Implementation available**. The backend resolves this state; the UI displays its reason beside the experiment control. **Request implementation** opens a commissioning form with a frozen mechanism, acceptance criteria, exact dependencies, supported parameters, and a bounded allocation. **Implementations** lets you reuse a specific published version across campaigns and local workspaces.

The package interface is `create_optimizer(context)` returning an object with `ask()`, `tell(design, efficiency)`, `checkpoint() -> bytes`, and `restore(bytes)`. A persistent isolated Python process can use declared, locked numerical libraries. Only the trusted parent evaluates proposed designs. The service freezes protected behavior checks before building, performs reproducibility and checkpoint checks plus four real MEENT evaluations, obtains an independent semantic review, and permits at most three candidate attempts inside the fixed grant. These checks concern implementation correctness; comparative performance remains workbench evidence.

Old `algorithm: "custom"` source and protocol-verification records remain historical records. Old workers and their pinned continuations remain usable. New launches require package validation. The compatibility `POST /api/hypotheses/{id}/verify` endpoint now returns an asynchronous job and needs `compute_seconds`, `api_budget_usd`, and `idempotency_key`; it imports the existing source through an adapter. A protocol-only pass is never promoted into a stronger validation claim.

The current evaluator exposes `binary_forward`. Continuous designs, RCWA gradients, and new objectives require a separate evaluator extension. Unsupported capabilities create a campaign manager issue, rather than a pretend successful implementation.

## Campaign manager and durable context

The campaign manager is the interface for unexpected failures, unresolved capabilities, stale actions, and implementation questions. Ordinary launch, pause, stop, extend, and charter controls remain directly available. Manager messages queue durably while another turn runs. A new researcher direction makes outstanding recommendations stale; older turns cannot silently dispatch their actions against newer guidance.

**Campaign memory** presents the current structured Markdown context, editable researcher guidance, source references, and revision history. Restoring old guidance creates a new revision. Concurrent edits report a conflict. Authoritative budgets and physics still change through the charter; model interpretations and measured records remain separate.

SQLite owns the context revisions, typed findings/decisions, command inbox, issues, and event journal. Projections live at `campaigns/<id>/manager/context.md`, `context.json`, `revisions/`, `records.jsonl`, `records/`, and `journal.jsonl`. The service reconstructs model context from these durable records each turn and retains the exact snapshot it used. Relevant older records are retrieved through SQLite full-text search. The manager memory package is bounded to 96 KiB; large histories use recent and retrieved evidence, while overflowing active constraints produces a visible issue rather than silently dropping them. Locked observations and records derived from them are excluded from development reasoning.

Use `context.import` to submit an edited JSON context with its original revision. Only `narrative_guidance` is writable through that document; authority, evidence and work use their owning commands. Published findings require a problem scope, evidence and limitations. Observations, provisional interpretations, researcher endorsements and counterevidence remain distinguishable. Another campaign reads a published finding only after an explicit manager-evidence reference decision.

Each relevant completion has a durable inbox identity and consumption position. Model output is saved before its messages and proposed actions commit. Accepted commands reconcile before delivery is retried; stale unissued proposals are reconsidered against current guidance. Pending issues block affected actions, while independent authorized work can continue.

Implementation compute has its own charter allocation (zero by default for existing campaigns). Implementation and research API usage share the campaign API spending cap. Subscription calls retain separate accounting. Interrupted provider calls cannot replay automatically, and their uncertain usage is retained for an explicit manager decision.

## Persistence, verification, and current limits

The workspace directory contains `workspace.sqlite3`, the event/record ledger, a service lock, and `trials/<id>/` artifacts. Trial artifacts include `spec.json`, `metrics.jsonl`, `progress.json`, `result.json`, `archive.json`, `checkpoint.pkl`, logs, and a pinned code snapshot. Checkpoints are trusted local data, not an upload format. Preserve the directory for reproducibility; use a consistent filesystem/database backup procedure when copying a live workspace.

Research roles record a durable reservation before contacting the provider and completed-call checkpoints afterward. API reservations include conservative cost estimates; Codex reservations track subscription calls without inventing a dollar price. A discussion interrupted after a completed checkpoint can resume with the same request and context. An unresolved in-flight reservation prohibits automatic replay and requires researcher reconciliation; reconcile the provider outcome before replaying it. Cancellation before provider execution releases its reservation. Job stop controls are independent of LLM response latency.

Run verification with:

```bash
uv run --no-sync pytest -q
cd frontend
npm run build
npm run test:e2e
```

Browser tests use `/usr/bin/google-chrome` when available, or `CHROME_PATH`, otherwise a Playwright-installed browser. Install Chromium with `npx playwright install chromium` if needed. The default browser suite uses mocked API responses and skips the opt-in live scenario. Sandbox tests report skips when the OS cannot establish the required namespaces.

To run the real API/browser scenario, start a separate disposable workspace with model calls disabled on an available port 8765:

```bash
GRATING_LLM_DISABLED=true uv run --no-sync grating-lab --directory /tmp/grating-lab-browser-check --workers 2 --port 8765
```

Then run `npm run test:live` from `frontend/`. This creates an actual campaign, performs small MEENT baseline and validation jobs, and exercises the comparison/history/export surfaces. It uses the running backend rather than mocked API data. Both browser scopes and the production build were exercised during implementation; see the evidence map for the exact coverage and remaining audit items.

See [implementation status](implementation-status.md) for the evidence map and remaining validation. Known development and validation exposure follows reused implementation versions across local workspaces. This cannot certify what a researcher already knew from other projects or papers. No full campaign comparing researcher-alone, researcher-plus-agents, and system-only performance has been conducted as part of implementing this software. No state-of-the-art algorithm or device claim follows from these software tests.
