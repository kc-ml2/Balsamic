# Balsamic

Balsamic is a research workspace in which a researcher and a lead agent develop an effective optimizer for the problem
at hand. The researcher talks to the lead in a chat; the lead runs experiments, asks assistants for reviews, keeps the
records and links its replies to the pages that show the evidence. Every write is a command the researcher can trace,
every measurement is labelled as measured or forecast, and nothing is started that the researcher did not ask for
(autopilot is the one explicit exception, and only the researcher turns it on).

The reference application is binary 1D grating inverse design with real MEENT RCWA evaluations; a bounded continuous
benchmark exercises the general problem interface. Finding a universal optimizer is not the goal: the framework is
reusable, the strategies are per problem, and the library carries winners from one campaign to the next.

This is research software. The workspace was rebuilt in October 2026 around the chat-centred design in
[docs/ui-workspace.md](docs/ui-workspace.md); the scientific core (problem adapters, optimizers, scheduler and workers,
races, confirmation, the implementation service) is the same code as before.

## Quick start

Linux, Python 3.12 or 3.13, `uv`, Node.js 22.

```bash
uv sync --extra dev
scripts/install-meent           # MEENT (the meent-private fork, Torch on CPU or CUDA), PyTorch and mask-optimizers (see below)
(cd agent-harness && npm ci && npm run build)
(cd frontend && npm ci && npm run build)
scripts/start                   # the real Pi harness with your model provider (see "Models and keys")
scripts/start --fake            # a scripted fake lead answers: no model, no keys needed
```

`scripts/start` brings up the three services (the workspace on 8765, the implementation service on 8767, the harness
on 8768), adds a tailnet-only HTTPS route to the workspace when Tailscale is installed (see "Remote access"), and
ends with the addresses and the token:

```
Balsamic is up (real harness; data and logs in …/runs/dev)
  local URL:    http://127.0.0.1:8765
  tailnet URL:  https://<machine>.<tailnet>.ts.net:8444   (tailnet only, never public)
  service token (workspace <-> harness, …/runs/dev/pi.token):
    <48 hex characters>
  implementation service token (…/runs/dev/library/service.token):
    <token>
```

Open the local or the tailnet URL and start a campaign from `#new`. The service token is what the workspace sends the
harness (and the harness sends back) as a bearer token; the browser never needs it. `scripts/start` is idempotent
(running services are left alone); `scripts/stop` stops the services and removes the tailnet route. Options:
`--no-tailnet` (localhost only), `--dir DIR` (data and logs elsewhere than `runs/dev`); `BALSAMIC_PORT`,
`BALSAMIC_LIBRARY_PORT`, `BALSAMIC_PI_PORT` and `BALSAMIC_TAILNET_PORT` change the ports. Logs are in `runs/dev/*.log`;
campaign data stay in `runs/dev/workspace` after the browser closes. For a frontend with hot reload run
`cd frontend && npm run dev` and open http://127.0.0.1:5173.

Underneath, `scripts/start` calls `scripts/dev-stack up [--real]`, which starts the services and nothing else
(`scripts/dev-stack down` and `status` are the other verbs). The services can also be run one by one
(`uv run optimization-lab --directory DIR --port 8765 --workers 2`,
`uv run optimization-implementations --directory DIR --port 8767`, `npm --prefix agent-harness start`) or from a
launcher configuration with `scripts/labctl` ([server control](docs/server-control.md)).

## How to use it

### Start a campaign

`#new` is a conversation. The setup assistant asks one thing at a time: a name (the campaign's identity is made from
it), what you want to find out (a rough sentence is enough, or "let the lead draft it with me"), the problem (an
installed one, or formulate it with the lead first), the budgets (worker time, model spend, the per-run cap the lead
may give one run without asking), delegation (delegated, guided, manual) and autopilot (off unless you choose a cap).
Pick a choice or type; typed answers such as "2 h, $7, runs 120 s" are understood. With a model available the
assistant phrases the questions and fills the draft from what you write; without one the same questions come scripted.
The draft page beside the chat shows what is filled, with "change" per field; "Start over" (or typing it) abandons the
draft. "Create the campaign" records it, and the setup turns become the first turns of its main thread.

### Talk to the lead

The chat on the left is the campaign's conversation with the lead; the page on the right is what the chat is about.
The lead starts nothing until you ask; the chat suggests the next request by stage (once a problem is set: `/generate 3`). Ask the lead for proposals, not the discussion moderator: the lead records them and the moderator reviews them in rounds. Its replies link records and pages (`trial_…`, `H03`, `#experiments/rules`) as
chips that open the page in this thread. Slash commands: `/generate 3` (three new strategies), `/generate 2 hybrid of
H01 H02`, `/evolve H01`, `/review H01`, `/compare`, `/probe`, `/plan`, `/note`, `/fork`, `/park`, `/merge into
<thread> <note>`, `/help`.

### Confirm the charter

Adopting the note hands it to the formulator, which writes the charter. The **Charter** tab of the Problem note page is
a form with two kinds of open item. **Checks to pass** are what the charter's own checks found (code, run on every
revision, no model): a value missing, a bound missing, an assumption not yet accepted, a constraint stated in words only
that nothing can evaluate. Each has its control right there (accept, type the number, set the bounds) and suggested ways
out made from the charter itself: a one-click waiver with its reason where waiving is allowed, and a worded request to
the formulator that one click sends (the "Ask the formulator" box starts from the same words, for editing). **Questions
from the formulator** are what it could not decide from the sources alone; they come with options, its recommendation
first, or take an answer in words. Answer what you can and send the rest: the formulator decides each unanswered one from
your answers and the sources and records it as an assumption you can change later, once a run has shown what it meant.
Every quantity and bound is an editable field. Confirm is enabled once nothing blocks; it compiles the tasks and
experiments can start.

### Panels the agents compose

When a fixed page lacks an input or a view a particular problem needs, the lead (any page) or the formulator (the
charter page) composes a panel from a catalog of Balsamic components: text, bound values, tables of records, number and
select fields bound to the charter, charts, buttons that issue commands, links. It appears on its page and, when made in
a conversation, as a card in it. Values are read live and every change carries a receipt. See `docs/panels.md`.

### Find your way

**Overview** (first on the rail) is the campaign at a glance and the place to remove it. Until the charter is confirmed, a
callout at the top of every page names the step still required (draft the note, adopt it, confirm the charter): no
experiment runs before that, even though proposals can be recorded. Each page with an agent in charge has that agent's
desk docked at its bottom; open it to talk to the moderator, the operator, the implementation lead or the resource
planner. Drag the gutters to resize the three columns; fold the rail with the button at its foot. Flat capsules are
status and do nothing when clicked; bordered buttons act; dotted mono chips open records.

### Read the chat

Earlier exchanges fold under a one-sentence title (written by the housekeeper once the next exchange begins); the last
two stay open, and one button expands or folds them all. "Detach" in the chat's header opens the conversation in its own
window and lets the page fill the main one (a thin strip in the chat's place reattaches it); links the lead writes open
in the main window. The three suggestion buttons above the composer are written by the housekeeper after each reply of
the lead, from the campaign's state and the last exchange; stage defaults show only until the lead has spoken. When the lead hands work to
agents, their conversation with each other goes to a side thread, linked from your thread by one line and listed under
"Agents' conversations" in the switcher; only the lead's own reply comes back to you.

### Branch the conversation

"Fork here" on any turn starts a branch at that point (a branch of the lead's own session tree, so the lead continues
from exactly there); "Edit" on your own message resends it as a branch, leaving the original. The thread switcher at the
top of the chat and the **Branches** page (lanes, summary cards, activity strips) move between them. Park a branch when
it is done for now (a summary card is written), merge it into another with a note (ask the lead to draft the note from
the branch), or leave it. A message in one branch never stales the lead's work in another; what another branch did
arrives as one-line lab events. The **Context** chip opens what the lead sees in this thread: the project layer, the
branch layer (measured by the harness when it answers), and the records you pinned or excluded.

### Formulate the problem

With no problem instance chosen, the campaign starts in Formulate. Describe the problem in the chat (or `/note`); the
lead writes the **Problem note** (objective, knobs, observables, noise, constraints, confounders, open questions, with
math). The note is a draft until you adopt it, and the page says so: every section has **Ask about this** and **Edit**.
Ask (or select any words in the text) to **Define**, **Explain**, **Challenge** or **Visualize** it: definitions come
from the glossary, explanations from one cheap model call, challenges and visuals from a turn of the lead, which answers
as a proposed edit you accept or reject; the note never changes silently. Edit changes a section in your own words,
saved as a revision in your name; or tell the lead in the chat what to change. **Adopt** hands the note to the formulator, which
writes the charter (quantities, relations, assumptions); you confirm it on the note's Charter tab, which sets the
campaign's tasks and ends formulation.

Every section must be written before proposals or experiments, unless it is skipped with your agreement. You don't have
to decide that alone: click **Ask which can be skipped** on the note (or type `/skip`) and the lead goes through the
empty sections. Where the confirmed charter already states a section (the design variables, the objective and metrics,
a deterministic evaluator) or the section does not apply, it proposes a skip with its reason and one statement for you
to confirm. It asks you about what the records don't settle. You agree or decline on the card in the chat or in the
section. A skip changes no text, so the note needn't be adopted again; it lapses if the charter stops saying what it cited.

### Watch the strategies evolve

**Hypotheses** lists every strategy with its stage; a round in progress shows "1 of 3 ready". **Evolution** is the
graph: the frontier table (select several and Stop with a reason, Promote, Allocate time, Hybrid or Revise, Discuss in
a branch, all as one batch that either applies whole or not at all), the lineage tree (rounds as columns, typed edges:
revision, variant, hybrid, inspired by the library), the measured score curves, and the races. Stages are proposed,
reviewed, screening, extended, finalist, confirmed, stopped; stopped is a decision with a reason, never a failure.
Every card has "Open in graph" and "Trace origin" (the command, the run, the message, the thread, the model).
**Experiments** shows runs by kind with their curves, logs and checks; **Files** the findings, reports, notebook and
sources; **Inbox** what waits for you (approvals, issues, the lead's questions).

### Keep an eye on it

The pill in the top bar reads "All clear" unless a row needs attention; it opens the **Monitor** drawer: budget
(measured use, and a forecast labelled as such), compute (runs, queue, the GPU row), agents, autopilot, needs-you. The
Monitor's pages add resources (polled only while open), model usage, race progress, the agent team and its log, models,
guidance, and the away digest. Removing a campaign is on the Resources page (you choose what to keep).

### Autopilot

Off by default, and gated: autopilot, proposal generation, allocations and interventions are refused until the problem is
fully defined. The gate is passed when every section of the problem note is written (objective, knobs, observables, noise,
constraints, confounders, open questions, which reads "none remaining" once settled), the note is adopted at its current
revision, the charter is confirmed and its tasks are installed. The callout at the top of every page, the Overview's
"Problem definition" row, the autopilot dialog and the lead's briefing all name the same missing step; a note revised after
adoption closes the gate again until it is adopted again. A campaign set up with autopilot remembers the choice and the lead
asks you to turn it on once the gate opens. "Autopilot off" in the top bar turns it on with a compute cap and an optional report date. The lead then
runs rounds by itself in a system thread, within the cap, with a reason on every command and a badge on every row it
touches; your messages still come first; the last round writes the report. When you come back after half an hour, the
pill reads "Away digest": what was stopped, promoted, started and finished, and what waits for you.

### Reuse what won

**Library** holds strategies published from finalists (or confirmed ones), with their problem signature and evidence.
On a new campaign of the same problem the lead's first reply mentions matching entries; "Apply here" makes a root
strategy inspired by one, reviewed like any other. Ctrl+K searches pages, threads and messages; `g` then a letter jumps
to a page; threads export as Markdown and the graph as JSON. See [docs/strategy-library.md](docs/strategy-library.md).

## Models and keys

Model calls go through the Pi agent harness; nothing is called without a signed-in provider, and there is no automatic
paid fallback.

1. **Sign in a provider.** The harness offers the providers your own Pi offers: custom endpoints such as a LiteLLM
   proxy are read from `~/.pi/agent/models.json` (the profile's own `models.json` takes precedence;
   `GRATING_PI_MODELS_FILE` names another), with their keys in `~/.pi/agent/auth.json` as `pi /login` leaves them.
   Key-based providers (DeepSeek, OpenAI API, Anthropic, …) can also take their key from
   `~/.config/balsamic/secrets.env` (one `NAME=value` per line, mode 600), which `scripts/dev-stack up --real` passes
   to the harness only. The ChatGPT subscription (`openai-codex`) signs in with a device code from Monitor → Models.
   Restart the stack after a change.
2. **The default model** of the harness is in `agent-harness/profiles/dev/settings.json` (`defaultProvider`,
   `defaultModel`, optional `defaultThinkingLevel`). Every tier starts from it.
3. **Which model each agent runs on:** **Monitor → Models**. Tiers name a model and a thinking level each; every role
   (the lead, the review-round specialists, the proposal reviewer, the implementation builder, test designer and
   validator, the results analyst, the page agents, the problem importer, and the housekeeper, which names branches,
   labels strategies for the lineage graph and writes branch summaries on the lightest tier) is assigned a tier. Saving applies to running
   agents within their campaign's model family; others switch at their next campaign. The page also says which
   providers are signed in; "… is not available from a signed-in Pi provider" means step 1 is missing.

Model spend per call, agent and model is on **Monitor → Model usage**; API-billed calls count against the campaign's
model budget, subscription calls do not. Details: [docs/pi-agent-harness.md](docs/pi-agent-harness.md).

## Remote access

The services bind to localhost. On a Tailscale machine, `scripts/start` exposes the workspace to your tailnet only
(never Funnel) and prints the URL; `scripts/stop` removes the route. What it runs, if you want it by hand:

```bash
sudo tailscale set --operator=$USER          # once, so that `tailscale serve` needs no sudo
tailscale serve --bg --https=8444 http://127.0.0.1:8765
tailscale status --json | jq -r .Self.DNSName   # the <machine>.<tailnet>.ts.net part of the URL
```

Then open `https://<machine>.<tailnet>.ts.net:8444`. `tailscale serve --https=8444 off` removes the route
(`BALSAMIC_TAILNET_PORT` chooses another port for both scripts). The implementation service and the harness stay on
localhost and use their token files.

## Optional components

| Component | Enables | Setup |
|---|---|---|
| Pi agent harness | The lead and its assistants, `scripts/pi-dev` | `(cd agent-harness && npm ci && npm run build)`; a signed-in provider ("Models and keys") |
| bubblewrap (`bwrap`) | Executing generated and custom optimizer packages | Distribution package; Ubuntu also needs `deploy/grating-bwrap.apparmor` ([Pi runtime](docs/pi-agent-harness.md#host-prerequisite-found-during-rollout)) |
| Docker | Full development workspaces with a browser IDE | `docker build -t grating-implementation-workspace:pi-0.87.1 deploy/implementation-workspace`, then set `development_enabled` |
| MEENT: the meent-private fork, branch `feature/cuda-acceleration` | Both grating problems. The fork is Torch-only: the 1D solver runs it on CPU, the 2D solver on the GPU or on CPU, wherever the scheduler places each run (about 0.3 s per FLRL design on an RTX 3060, 2.2 s on one CPU core) | The `meent` extra, which `pyproject.toml` points at the fork (a private repository: installing needs read access) and `uv.lock` pins to a commit; `scripts/install-meent --update` moves it to the branch tip. The PyPI release with the same version number is refused. `scripts/start` runs one GPU slot when PyTorch sees a CUDA device, beside one CPU slot per physical core less one for the system and less the GPU slot (`BALSAMIC_GPU_WORKERS` and `BALSAMIC_WORKERS` change them) |
| `mask-optimizers` 0.1.0 | The `motif_surgery`, `nested_fourier` and `phenotype_de` methods | The `masks` extra from [kc-ml2/mask-optimizers](https://github.com/kc-ml2/mask-optimizers), which `scripts/install-meent` installs; see [optimizer plug-ins](docs/optimizer-plugins.md) |
| `torch`, `gymnasium` | The learning-based optimizers, the original DQN loop, and MEENT | The `learners` extra; `scripts/install-meent` installs it with the `meent` extra |
| Paper and FLRL references | `/references` inside development workspaces | `paper_reference` in the launcher JSON or `GRATING_PAPER_REFERENCE`; `jLabKAIST/flrl` beside this repository or `GRATING_FLRL_REFERENCE` |
| Chrome or Playwright Chromium | Browser tests | `npx playwright install chromium` |

Before any experiment or runtime probe is queued, the lab builds the task's evaluator once in a fresh worker process. If a
solver or package is missing, the run is refused with the evaluator's own error and what to install. Every page shows the
same error as the next step, and the lead is told it instead of finding out from a failed probe. The check runs again by
itself once the installed software changes.

Git does not carry `runs/` (campaign databases, checkpoints and logs), provider credentials (`~/.pi/agent/auth.json`,
`~/.config/balsamic/secrets.env`) or sibling checkouts.

## Testing

```bash
GRATING_LLM_DISABLED=true uv run pytest -q          # the core and tests/balsamic (service layer, end to end over the fake harness)
(cd agent-harness && npm test)                      # the harness, against the pinned Pi SDK
(cd frontend && npx tsc -b && npx playwright test)  # the workspace, against a mocked backend
```

`tests/fake_harness.py` is a scripted stand-in for the Pi harness with a session tree; `scripts/start --fake` (and
`scripts/dev-stack up` without `--real`) uses it.

## Code map

- `src/balsamic/`: the workspace service: `app.py` (routes, the command bus boundary), `threads/` (branches of the
  lead's session, the chat stream), `notes/` (the problem note), `evolution/` (ledger, lineage, interventions,
  provenance), `monitor/`, `autopilot/`, `library/`, `setup/` (the campaign setup conversation), `telemetry/`.
- `src/optimization_framework/`: the scientific core: `execution/` (scheduler, workers, admission, races),
  `evaluation/`, `optimizers/`, `implementations/` (the separate build-and-validate service), `assets/`, `analysis/`,
  `problem_charter/`, `problem_import/` (the formulator), `reports/` (the staged report writer), `agents/` (the Pi
  controller and tools; `pages.py` shares `frontend/src/router/pages.json` with the router), `campaigns/`, `llm/`.
- `agent-harness/`: the Node Pi harness (`supervisor.ts`, `runtime.ts`, `sessions.ts`, `server.ts`), SDK 1.0.4.
- `frontend/`: React 19 + Vite; `src/shell`, `chat`, `setup`, `note`, `hypotheses`, `evolution`, `experiments`,
  `files`, `inbox`, `monitor`, `library`, `branches`; `tests/` Playwright specs with `tests/mocks/`.
- `src/dqn_meent/`: the grating problem adapters and the original DQN reconstruction; `src/optimization_benchmarks/`:
  the bounded continuous benchmark.
- `docs/`: [ui-workspace.md](docs/ui-workspace.md) (the workspace, milestone by milestone),
  [strategy-library.md](docs/strategy-library.md), [problem-setup.md](docs/problem-setup.md),
  [lead-agent-readiness.md](docs/lead-agent-readiness.md), [pi-agent-harness.md](docs/pi-agent-harness.md),
  [resource-allocation.md](docs/resource-allocation.md), [adaptive-algorithm-testing.md](docs/adaptive-algorithm-testing.md),
  [report-review.md](docs/report-review.md), [server-control.md](docs/server-control.md); documents of the earlier
  workspace carry a "Superseded" banner.

## The reference problem

The original reconstruction of the DQN optimization loop in Seo et al., *Structural Optimization of a One-Dimensional
Freeform Metagrating Deflector via Deep Reinforcement Learning*, ACS Photonics 9, 452–458 (2022), remains available as
a single-run interface (`uv run dqn-meent train --config configs/smoke.json --output runs/smoke`, then `evaluate`).
Its physics, the differences from the published code and the measured results are in
[docs/paper-mapping.md](docs/paper-mapping.md), [docs/validation.md](docs/validation.md) and
[docs/paper2agent-assessment.md](docs/paper2agent-assessment.md). Search scores are not automatically converged at high
RCWA order; the lab's checks and confirmation runs exist for that reason.
