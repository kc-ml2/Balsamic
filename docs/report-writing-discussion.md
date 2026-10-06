# Scientific report writing: discussion summary and tentative plan

Date: 30 September 2026

Status at the time of discussion: The HTML review interface is implemented. The proposed workflow for producing better first drafts remains a tentative conclusion.

Implementation follow-up: [Report review and technical writer](report-review.md) documents the subsequently implemented workflow. This file preserves the discussion and its tentative conclusion.

## 1. Overall objective

Help a researcher reach approximately 95% of a finished technical article quickly, with minimal effort. The researcher can then export the draft to Markdown, HTML, or PDF and finish it in their preferred writing platform.

The report artifact serves as an interface between writer and reader. It should support both a strong initial draft and efficient subsequent discussion.

## 2. Initial request: lightweight review and revision

The discussion began with a request to make an existing experiment report reviewable in HTML without rewriting its contents.

The researcher proposed four tiers of feedback:

| Tier | Appearance | Intended meaning |
|---|---|---|
| Very good | Green highlight | Keep this phrase; give it the highest preservation priority. |
| Fine | Unmarked | Acceptable material that can be rewritten or reorganized. |
| Poor | Yellow highlight | Improve wording, precision, or factual correctness. A replacement may not be supplied. |
| Bad | Red highlight | Remove the intended content, using editorial judgment. |

Highlights can carry short remarks, and the report can receive overall comments.

Revision must interpret selections in context. A highlight may accidentally include or omit neighboring words. Removing a marked word may require removing or rebuilding its sentence. The writer must check grammar, continuity, references, and the overall argument after making changes.

Green passages should guide the reorganization of surrounding material. They express a strong preference for retention, while factual problems still require correction and explanation.

### Implemented baseline

The feature was implemented on `feature/report-review`, in commit `d08918b`.

It includes:

- A Reports subpage with text highlighting, remarks, undo, and browser draft persistence.
- Explicit feedback submission, separate from requesting a revision.
- Immutable original reports and saved review submissions.
- A technical report writer that creates linked revisions.
- Self-contained HTML that coworkers can review offline.
- Import of returned reviewed HTML or feedback JSON.
- Clean HTML, Markdown, and browser Print / PDF export.

Coworkers must download their reviewed HTML or feedback file to return their changes; browser edits do not automatically modify the original file on disk.

The original report’s wording and figures were preserved. Validation included 53 Python checks, six browser tests, and confirmation that PDF export retained nine pages.

Implementation details are documented in [Report review and technical writer](report-review.md).

## 3. The deeper problem: producing a better first draft

The researcher emphasized that improving initial writing quality is more important, and harder, than making revision convenient.

An experiment campaign collects ingredients. A scientific article selects and prepares only those needed for its purpose. The existence of an experiment, result, or figure does not by itself justify including it.

The cooking analogy captures the degree of selectivity involved: an effective article may use only a small part of a campaign, just as a carefully prepared dish may use only the white part of a spring onion or the tail of a shrimp.

An accurate report can still disappoint an expert reader when it:

- Inventories activity without identifying a worthwhile scientific question.
- Explains familiar facts at excessive length.
- Misses implications the researcher considers important.
- Gives unequal evidence similar emphasis.
- Organizes the narrative around campaign chronology.
- Exaggerates routine results or implies a stronger comparison than was conducted.

The current writer exposes this gap. It uses one writing call, follows a general report structure, and preserves every supplied figure. It does not explicitly perform scientific selection before drafting.

## 4. Understanding sparse researcher input

The researcher should not need to spend hours explaining their complete intent.

A few incomplete sentences—perhaps observations made while monitoring graphs—should help the writer infer:

- The question or decision that matters.
- The reader’s existing knowledge.
- Which topics deserve attention.
- The appropriate depth of explanation.
- What appears surprising, routine, or uncertain.
- Related findings the researcher might also consider important.

These interpretations must remain provisional. Explicit instructions, inferred preferences, observed evidence, and scientific conclusions should remain distinguishable.

For example:

> “Annealing looks strong. DQN still climbing. Wall time probably matters more.”

These notes suggest investigating practical budgets, search maturity, and whether the comparison settles the question. They do not establish general superiority for annealing or eventual success for DQN.

The objective is intellectually appropriate writing that respects the researcher’s expertise. It is not to make results appear more interesting through persuasive framing.

## 5. Scientific standards established in the discussion

### Campaign completeness cannot be assumed

An experiment campaign may be exploratory, adaptive, incomplete, or deliberately uneven. Different methods may receive different tuning, budgets, stopping conditions, and follow-up attention.

Those choices may be part of the researcher’s strategy. The writer should inspect their recorded rationale and distinguish it from an inferred explanation.

Missing evidence does not automatically establish a negative result.

### Reliability belongs to individual claims

A campaign can support a strong statement about measured implementation cost while supporting only a weak statement about algorithm-family superiority.

A single campaign-wide reliability score would conceal that distinction.

### Usefulness, surprise, and novelty are different

A result can be useful for a decision without being surprising. It can surprise this researcher without being novel within the field.

Literature checks may help assess novelty. Enthusiasm or apparent surprise in the researcher’s notes cannot establish it.

### Selection must preserve material counterevidence

Routine history can remain outside the article. Evidence that materially weakens or changes its central conclusion must remain visible.

A limitation necessary to interpret the argument should appear where that argument is made.

## 6. Preferences confirmed by the researcher

Two workflow choices were explicitly selected:

1. **Draft autonomously.** Infer the focus and produce a draft with a short, editable explanation of that interpretation. Ask a targeted question only when competing interpretations would substantially change the article.
2. **Analyze and verify.** Permit bounded analysis and new plots from saved runs, plus targeted literature checks. Proposals for new experiments remain separate.

The intended interaction is lightweight and should not become a lengthy questionnaire or a routine approval gate before writing.

## 7. Tentative conclusion: a staged scientific editorial workflow

The proposed next step is to give the writing system explicit responsibility for deciding what deserves to be said before generating prose.

| Stage | Role | Responsibility |
|---|---|---|
| 1 | Editorial lead | Infer the researcher’s question, intended depth, and likely interests. Consider plausible alternative interpretations and choose a provisional brief. |
| 2 | Evidence investigator | Inspect relevant runs, trajectories, settings, failures, decisions, and sources. Establish which questions were investigated thoroughly and which remain weakly supported. |
| 3 | Editorial lead | Select a central question and a small set of supporting findings. Decide what belongs in the article, supporting material, or archive. |
| 4 | Scientific author | Draft around the selected argument. Allocate space according to what the reader needs to understand and evaluate it. |
| 5 | Independent reviewers | Review scientific support and expert-reader usefulness through separate initial assessments. |
| 6 | Editor | Resolve critiques against evidence, repair the argument, remove unnecessary material, and verify the resulting draft. |

### Different reviewers should challenge different failures

The scientific reviewer checks comparisons, factual support, inference, missing evidence, and novelty claims. It needs access to the broader evidence inventory, including material the author omitted.

The expert-reader reviewer checks relevance, depth, explanatory value, and wasted attention.

Their initial reviews should be separate so that neither merely follows the other’s framing. Agreement among agents is not a substitute for evidence.

### Product implications

The proposed workflow would:

- Accept rough notes and optional references to plots or passages.
- Preserve source evidence while allowing derived analyses.
- Show the inferred focus alongside the draft.
- Save intermediate briefs, evidence assessments, editorial selections, and reviews.
- Bound investigation and revision cycles.
- Replace mandatory retention of every figure with deliberate selection.
- Continue using the existing HTML review interface for subsequent feedback.

Feedback should inform future writing cautiously. A green highlight can endorse wording without endorsing a scientific claim. A red highlight can reject phrasing without indicating permanent disinterest in the topic.

## 8. How the tentative approach should be evaluated

Multiple roles are a design hypothesis whose value must be demonstrated.

Compare the proposed workflow with the current writer and a strong single-agent workflow under comparable resource budgets.

Evaluation cases should include:

- Uneven tuning and unequal allocations.
- Unfinished learning or search.
- Contradictory observations.
- Routine or unsurprising outcomes.
- Campaigns supporting only narrow conclusions.
- Different researcher notes applied to the same evidence.

Success means:

- Fewer substantive corrections to the initial draft.
- Better selection of topics and depth.
- Appropriate treatment of uncertainty and novelty.
- Preservation of material counterevidence.
- Less researcher effort to reach a usable draft.
- No loss of factual accuracy.

The researcher’s willingness to retain the first draft is an important outcome. The agents’ own assessment of how polished or convincing their prose appears is insufficient.

This staged workflow is the tentative conclusion of the discussion. It has not yet been implemented or demonstrated to improve initial report quality.
