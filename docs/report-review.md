# Report review and technical writer

Open **Reports** in the workspace, then **Open review**. Each draft has its own
`/reports/<report_id>` page and immutable HTML source. Adding review controls does
not rewrite the report or change its figures.

Select text and click **Very good**, **Fine**, **Poor**, or **Bad**. Keyboard shortcuts
**1–4** do the same while text is selected. Green asks the writer to keep the phrase;
unmarked text can be reorganized; yellow asks for more precise or correct writing;
red asks for removal with editorial judgment. **Fine** clears the selected color.
**+ Remark** adds a short comment to a selection, including unmarked text. The
sidebar lets you find, edit, or clear each mark. **Undo** reverses recent changes.

Draft feedback saves in this browser. **Submit feedback** commits an immutable
review to the workspace and displays a submission ID. Submission does not call a
model or alter the report. You can tell the writing agent “revise report X using
submission Y,” download the complete writer handoff, or click **Revise draft with
writer** when ready. A revision creates a new linked draft with a fresh review.
The old report and review remain available. Failed or interrupted calls are shown
without silently retrying.

## Sharing and exports

**Share & export → Download review HTML** downloads one self-contained file with
the complete report, embedded figures, controls, and your current remarks. No
server, account, package installation, or internet connection is needed to review
the file. Browser edits do not modify the file on disk automatically: coworkers
must click **Download reviewed HTML** to send back their changes, or use
**Download feedback** to send a smaller JSON file.

Use **Import a review** on the matching report to load either returned HTML or
JSON, then **Submit feedback**. An import replaces the current *local* review;
Undo restores the previous one. It does not rewrite previous submissions or
silently merge conflicting reviewers. A report/version mismatch is rejected.
If another tab or device submitted a newer review, download your local draft and
use **Load submitted review** to inspect the saved one. Stale submissions cannot
overwrite the newer head. Local edits in another tab produce a warning.

**Clean HTML** includes the current draft and figures without review controls or
marks. **Markdown** preserves prose, lists and tables; figures remain inline SVG
HTML, whose rendering depends on the destination Markdown platform. **Print / PDF**
uses the browser's Save as PDF option and hides the review controls and highlights.
All exports retain current draft wording: red highlights take effect only in a
subsequent editorial revision.

## Importing a report

The command runs from the repository environment. Imported HTML must contain
non-nested `<section id="unique-id">` elements and have self-contained figures.
Import trusted, locally authored HTML; the API does not accept arbitrary HTML uploads.

```bash
.venv/bin/python -m optimization_framework.reports.cli \
  --directory runs/discovery/grating-luna-20260928/workspace import \
  --html /path/to/report.html --title 'Experiment report' \
  --campaign-id campaign_example --evidence /path/to/evidence-summary.json

.venv/bin/python -m optimization_framework.reports.cli \
  --directory runs/discovery/grating-luna-20260928/workspace export \
  --report-id report_example --review --output /tmp/report-review.html
```

The source HTML hash and UTF-16 section offsets identify every exact selected
phrase, including Unicode and selections across inline markup. The backend
validates the exact words, bounds, disjoint spans and source version. Every saved
annotation also carries its full section context for interpreting approximate
selection boundaries. Source sorting/filtering scripts are disabled in the review
page to keep positions stable. Figure artwork is preserved; prose and captions
can be marked, while text inside the vector artwork cannot.

SQLite records are canonical. A complete handoff is also projected to
`<workspace>/reports/<report_id>/<submission_id>.json` after submission. It includes
the original HTML, source hash, frozen evidence, review and writer instructions.
The packet download can regenerate this handoff if the projection file is missing.

## Technical report writer

Open **Reports**, enter a few observations in **What caught your attention?**, and
click **Write a draft**. Record IDs are optional starting points. The default
workflow implements the staged approach discussed in
[Scientific report writing](report-writing-discussion.md):

1. An editorial lead infers a provisional focus and expert-reader depth, separating
   explicit guidance from inference.
2. An investigator reads frozen evidence, looks for contradictions and adjacent
   findings, and assesses support and limitations for each claim.
3. The editorial lead selects an argument, assigns findings to the main article,
   supporting discussion or archive, and explains figure inclusions and omissions.
4. An author writes around the selected argument.
5. Scientific and expert-reader reviewers make separate initial assessments.
   The scientific reviewer can inspect the broader evidence inventory.
6. An editor resolves their issues against evidence, followed by a verification
   pass. At most one additional edit/verification pair is allowed.

The writer normally proceeds autonomously. A short question appears only when
competing interpretations fundamentally change the article. Answer it on Reports
to continue the same job; completed stages and accounted calls are retained.
Progress survives navigation. **Stop writing** stops between calls or tool reads;
an inference already in flight finishes and is accounted before stopping.

The resulting HTML review shows an editable **Writing focus**, selection reasons,
evidence gaps and proposed follow-up experiments. Edit the focus, mark passages
and **Submit feedback**, then **Revise draft with writer**. The focus travels with
offline reviewed HTML and feedback JSON. These preferences apply to that revision;
they do not silently become permanent researcher preferences.

The `report_editor`, `report_evidence_investigator`, `technical_report_writer`,
`report_scientific_reviewer` and `report_reader_reviewer` roles use the campaign's
Models panel. Provider/model policy is frozen when a job is accepted. Every call
uses the common accounted adapter. Subscription calls appear in the campaign
total, with provider events in its agent log. Report writing supports the enabled
subscription backend or explicitly free local models; it does not draw on a paid
API allowance. Roles are separate model contexts, not experiment worker processes.

```bash
.venv/bin/python -m optimization_framework.reports.cli \
  --directory /path/to/workspace write \
  --campaign-id campaign_example \
  --brief 'Annealing looks strong. DQN still climbing. Wall time matters.'

# Or use an explicitly scoped evidence file without a campaign.
.venv/bin/python -m optimization_framework.reports.cli \
  --directory /path/to/workspace write \
  --evidence /path/to/frozen-evidence.json --brief 'Explain the narrow result.'

.venv/bin/python -m optimization_framework.reports.cli \
  --directory /path/to/workspace revise \
  --report-id report_example --submission-id mark_example \
  --instruction 'Apply the review and make the conclusions more concise.'

# Only needed if the job asked a focus question.
.venv/bin/python -m optimization_framework.reports.cli \
  --directory /path/to/workspace answer \
  --job-id report_writer_example --answer 'Emphasize practical wall time.'
```

Provider activation follows the existing environment/server configuration. These
CLI commands do not start experiment workers. `--reference ID` prioritizes a saved
record; `--no-literature` disables retrieval. Revisions reuse the source draft's
frozen scientific snapshot. Start a new report to incorporate newer campaign data.

### Evidence and operational bounds

Eligible campaign tasks, trials, hypotheses, decisions, studies and source metadata
are frozen before writing. Locked/unreleased non-development trials and records
referencing withheld task/trial evidence are excluded. This uses the existing
research access rules; a report does not release confirmation results. Supplied
evidence files are researcher-provided material, so scope those files appropriately.

Up to 32 scalar journals are captured, prioritizing referenced trials and then
recent trials. Reads are capped at 16 MB per journal, 128 MB total and 20,000
captured rows per journal. Large logs use bounded byte windows spanning the file,
including its beginning and tail. This is **not** uniform step/time sampling.
Receipts record byte ranges, hashes, field names, sampling method and gaps. Active
runs are captured prefixes; the snapshot is not a simultaneous campaign checkpoint.
All eligible trial records remain readable even when their traces are not captured.

The investigator and scientific reviewer can:

- Read exact frozen fields/pages using JSON pointers. Navigation indexes do not
  count as scientific support.
- Compare up to eight saved trials at an explicit or shared observed horizon.
  Results include actual coordinates, gaps to the horizon, allocations, configs
  and sampling coverage. There is no extrapolation, interpolation or pooled
  significance test. Observed resets and multiple attempts are rejected.
- Generate a self-contained SVG scatter plot of captured observations with
  matplotlib (the project's `meent` extra). Without matplotlib, numerical results
  remain available and the missing plot is reported.
- Search arXiv/Crossref and read primary-source passages through the existing
  literature service. Search metadata does not count as scientific support;
  captures identify the passages actually supplied and acquisition limitations.

The tool allowlist has no experiment launcher or arbitrary code execution. New
experiments remain proposals in the writing notes. Sampling cannot establish
convergence or full-trajectory statistics. Equalizing a plotted axis does not
equalize tuning, task conditions or total cost. These limits are provided to the
writer and reviewers, alongside each analysis.

Each staged job allows at most 14 model calls, 12 tool calls (including at most
four literature calls), three investigation rounds and two edit/verification
rounds. Prompts are capped at 220,000 UTF-8 bytes, with an 8,192-token output
allowance per call; the Codex transport expresses its output allowance in the
prompt rather than as a hard token cutoff. Oversized contexts fail explicitly.
The configured provider timeout bounds individual inference calls.

Draft markup must be inert HTML with non-nested, uniquely identified sections.
Only editorially selected figure placeholders are restored. Each included claim
must identify a passage in the draft, and every review issue and user annotation
must receive a response. Green phrases must survive or have a specific exception.
These are structural checks, not proofs of factual correctness. Scientific review
and the researcher's judgment remain necessary.

### Saved work and failure behavior

SQLite retains immutable evidence snapshots, read receipts and completed stage
inputs/outputs, together with model usage. **Download writing record** exports the
full audit packet. Briefs, selection decisions, reviews and derived figures remain
inspectable after a failure. A server restart marks active work interrupted;
failed/interrupted provider calls are never automatically replayed. Retrying the
same request ID returns its existing job; an explicitly new request creates new
work. Clicking **Write a draft** again after a job finishes explicitly starts new
work. A pending focus clarification can resume without repeating its brief.

If blocking scientific issues remain after the final permitted revision, the job
is **needs attention** and the saved draft exposes those issues in its writing
notes. It is not reported as a passed review. Earlier reports and submissions are
never overwritten.

The HTTP entry points are `POST /api/report-writer/jobs` (campaign ID, notes,
optional references), `GET /api/report-writer/jobs/{id}`,
`POST /api/report-writer/jobs/{id}/answer`, `POST .../{id}/cancel`, and
`GET .../{id}/artifacts`. Existing feedback/revision/export routes remain available.
Workspace identity, cross-origin protections and request idempotency apply.

### Comparing writing quality

`--workflow legacy` retains the original one-call writer. `--workflow single`
uses the same staged call/tool caps but one author role with accumulated
self-review notes. `--workflow staged` uses separate reviewer contexts and role
assignments. Compare actual usage as well as outputs; equal caps are not equal
token consumption, and the legacy baseline deliberately uses less computation.

Prepare the synthetic evaluation cases without making model calls:

```bash
.venv/bin/python -m optimization_framework.reports.evaluation \
  --output /tmp/report-writing-evaluation
```

Add `--run` to generate the three variants using the configured provider. Optional
`--case narrow_support` and `--workflow staged` restrict the run. The harness
creates blind portable HTML, a ratings template, and a separate private key with
workflow, status, configuration and usage. Existing ratings are not overwritten.
Cases cover unequal allocation, unfinished learning, contradictions, routine
outcomes, narrow claims and different notes applied to identical evidence.

Human evaluation should measure substantive corrections, useful selection/depth,
retained first-draft fraction, factual accuracy, visible counterevidence and time
to a usable draft. Workflow tests and a synthetic model smoke test do not establish
that multiple roles produce better articles. That remains an empirical question.

Validation:

```bash
.venv/bin/python -m pytest -q tests/test_report_review.py tests/test_report_pipeline.py
cd frontend
npm run build
npm run test:e2e -- --config=playwright.reports.config.ts --workers=1
```

The browser suite starts an isolated HTTP service with no experiment workers or
model calls. It covers Unicode selections, inline formatting, overlap and undo,
durable submission, an actual offline HTML round trip, concurrent reviews, export,
printing, mobile controls, staged draft creation, a focus clarification and portable
editorial notes. Python tests cover independent review contexts, figure selection,
claim provenance, withheld evidence, immutable traces, sampling/reset checks,
bounded revisions, model policy snapshots and failure/restart accounting.

Validation on 30 September 2026: 70 report/API/model-routing Python tests and eight
browser tests passed, and the frontend built successfully. A real subscription
model completed the `narrow_support` synthetic case in nine calls. The live
original report retained all nine source sections and five figures, exported to
nine PDF pages, and its portable HTML made no external requests. This verifies
operation and source preservation, not comparative article quality.
