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

The independent `technical_report_writer` role uses the campaign's Models panel
and common accounted provider adapter. It can write a new report from an explicit
evidence snapshot or revise an exact submitted review. Each request makes at most
one model call and retains its usage and failure status. Writer subscription calls
are included in the campaign subscription total and provider events in its agent
log. Report writing currently supports the enabled subscription backend or a free
local backend; it never draws on a paid API allowance.

```bash
.venv/bin/python -m optimization_framework.reports.cli \
  --directory /path/to/workspace write \
  --evidence /path/to/frozen-evidence.json --brief 'Write a concise experiment report.' \
  --campaign-id campaign_example

.venv/bin/python -m optimization_framework.reports.cli \
  --directory /path/to/workspace revise \
  --report-id report_example --submission-id mark_example \
  --instruction 'Apply the review and make the conclusions more concise.'
```

Provider activation follows the existing environment/server configuration. These
CLI commands do not start experiment workers. Evidence plus source and feedback
must fit the bounded writer context; oversized inputs fail explicitly. Source
figures are replaced by references for the model and restored from the frozen
original after its response. Model markup is validated as inert HTML. A response
must address every annotation and preserve green phrases, or explicitly explain
why a green phrase needed correction. Those explanations and the change summary
are retained on the new report record. These checks enforce the handoff contract;
the researcher still evaluates writing quality and factual conclusions.

Validation:

```bash
.venv/bin/python -m pytest -q tests/test_report_review.py
cd frontend
npm run build
npm run test:e2e -- --config=playwright.reports.config.ts --workers=1
```

The browser suite starts an isolated HTTP service with no experiment workers or
model calls. It covers Unicode selections, inline formatting, overlap and undo,
durable submission, an actual offline HTML round trip, concurrent reviews, export,
printing, and mobile controls.
