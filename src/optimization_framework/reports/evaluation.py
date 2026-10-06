"""Prepare or run a blinded report comparison; human ratings establish writing quality.

python -m optimization_framework.reports.evaluation --output /tmp/report-evaluation
Add --run to call the configured provider. No experiment workers are started.
"""
import argparse
import json
from pathlib import Path
import random

from optimization_framework.contracts.base import content_hash
from optimization_framework.execution.service import Workspace
from .document import review_html
from .writer import ReportWriter


UNEQUAL = {"objective": "maximize efficiency", "allocation": "Adaptive exploratory campaign, not a randomized method comparison",
    "runs": [{"method": "annealing", "seeds": [1, 2, 3], "tuning_trials": 18, "evaluations_per_seed": 1000,
              "best_efficiencies": [0.91, 0.93, 0.90], "wall_seconds": [60, 63, 58], "status": "budget exhausted"},
             {"method": "DQN", "seeds": [1], "tuning_trials": 2, "evaluations_per_seed": 300,
              "best_efficiencies": [0.82], "wall_seconds": [180], "status": "still running",
              "trajectory": [{"evaluation": 100, "best_efficiency": 0.66}, {"evaluation": 200, "best_efficiency": 0.75},
                             {"evaluation": 300, "best_efficiency": 0.82}]}],
    "recorded_decision": "More annealing trials were allocated after promising early results. No matched-budget replication yet."}

CASES = [
    {"id": "unequal_practical", "notes": "Annealing looks strong. Wall time probably matters more. What can we use now?", "evidence": UNEQUAL,
     "review_criteria": ["Answers the practical budget question", "Shows tuning/seed/budget inequality beside the comparison", "Does not claim family-wide superiority"]},
    {"id": "unfinished_learning", "notes": "DQN still climbing. What does the trajectory actually tell us?", "evidence": UNEQUAL,
     "review_criteria": ["Changes topic/depth for these notes despite identical evidence", "Distinguishes improvement so far from eventual success", "Does not assert convergence or extrapolate"]},
    {"id": "counterevidence", "notes": "The average gain looks good, but seed three worries me.",
     "evidence": {"paired_seeds": [1, 2, 3], "baseline": [0.70, 0.71, 0.72], "candidate": [0.82, 0.84, 0.51],
                  "conditions": "Same tasks and budgets", "failure_log": "Seed three repeatedly fell into a low-performing basin", "replications": 1},
     "review_criteria": ["Preserves the harmful seed and its practical significance", "Does not hide instability behind the mean", "Separates observed failure from an untested mechanism"]},
    {"id": "routine_result", "notes": "Mostly a sanity check. Keep this short; did implementation overhead matter?",
     "evidence": {"purpose": "Implementation sanity check", "measurements": [{"backend": "A", "score": 0.75, "setup_seconds": 30, "solve_seconds": 5},
                   {"backend": "B", "score": 0.75, "setup_seconds": 2, "solve_seconds": 5}], "seeds": [1], "prior_expectation": "Equivalent numerical results", "literature_checked": False},
     "review_criteria": ["Avoids novelty or breakthrough language", "Selects setup overhead as relevant", "Does not expand into a campaign inventory"]},
    {"id": "narrow_support", "notes": "Can we say the cache is useful? Look carefully at what we timed.",
     "evidence": {"timed_operation": "Repeated evaluation of one unchanged candidate", "uncached_ms": [98, 101, 99], "cached_ms": [1, 1, 2],
                  "excluded_costs": ["cache construction", "misses", "memory", "end-to-end optimizer work"], "adaptive_candidates_tested": False},
     "review_criteria": ["Limits the speed claim to measured cache hits", "Does not imply optimizer-level acceleration", "States missing end-to-end and miss measurements"]},
]


def prepare(output, *, workflows=("legacy", "single", "staged"), case_ids=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cases = [case for case in CASES if not case_ids or case["id"] in case_ids]
    if not cases or case_ids and set(case_ids) - {case["id"] for case in CASES}:
        raise ValueError("Unknown evaluation case")
    manifest = {"version": 1, "workflows": list(workflows), "cases": cases,
        "comparison": "Same frozen evidence and notes per case; same configured model. Staged and single share call/tool/output caps. Legacy retains its original one-call budget. Compare actual usage as well as ratings."}
    path = output / "manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("This evaluation directory has a different manifest; choose a new output directory")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (output / "README.md").write_text("# Report writing evaluation\n\n"
        "Cases are synthetic stress tests, not experimental findings. Preparation makes no model calls.\n\n"
        "Run the same command with `--run` to generate drafts. Existing job IDs are reused; failed calls are not silently replayed.\n\n"
        "Give a reviewer `blind/` and `ratings.json`. Keep `key.json` and `workspace/` away from that reviewer until scoring is complete.\n\n"
        "Judge factual support, useful selection/depth, uncertainty, counterevidence, substantive corrections, retained first-draft fraction, and minutes to a usable draft. Compare the same case across drafts and both intents on the shared evidence.\n\n"
        "The single-author control uses the same stages and limits, one role, and accumulated self-review notes. The staged workflow separates reviewer contexts and uses distinct role instructions. The legacy baseline uses one call. Actual tokens/calls/status are saved in the private key. Automated tests establish workflow behavior; they do not establish superior writing quality.\n")
    return manifest


def run(output, manifest):
    output = Path(output)
    workspace = Workspace(output / "workspace")
    writer = ReportWriter(workspace)
    writer.recover()
    blind = output / "blind"
    blind.mkdir(exist_ok=True)
    key, ratings = [], []
    randomizer = random.Random(43891)
    for case_index, case in enumerate(manifest["cases"], 1):
        variants = list(manifest["workflows"])
        randomizer.shuffle(variants)
        for variant_index, workflow in enumerate(variants):
            label = f"case-{case_index}-{chr(65 + variant_index)}"
            request_id = "eval_" + content_hash([case, workflow])[:24]
            try:
                job = writer.start(evidence=case["evidence"], brief=case["notes"], workflow=workflow,
                    request_id=request_id, literature=False, background=False)
                entry = {"label": label, "case_id": case["id"], "workflow": workflow,
                    **{field: job.get(field) for field in ("id", "status", "usage", "error", "result_id", "provider_snapshot")}}
                if job.get("result_id"):
                    report = {**writer.reports.get(job["result_id"]), "revision": None}
                    (blind / (label + ".html")).write_text(review_html(report))
            except ValueError as error:
                entry = {"label": label, "case_id": case["id"], "workflow": workflow, "status": "not_started", "error": str(error)}
            key.append(entry)
            ratings.append({"label": label, "notes": case["notes"], "evidence": case["evidence"], "review_criteria": case["review_criteria"],
                "factual_accuracy_1_to_5": None, "selection_and_depth_1_to_5": None, "uncertainty_1_to_5": None,
                "counterevidence_1_to_5": None, "substantive_corrections": None, "retained_fraction": None,
                "minutes_to_usable_draft": None, "comments": ""})
            (output / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n")
            ratings_path = output / "ratings.json"
            if not ratings_path.exists():
                (output / "ratings-template.json").write_text(json.dumps(ratings, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"label": label, "status": entry["status"]}), flush=True)
    if not (output / "ratings.json").exists():
        (output / "ratings.json").write_text(json.dumps(ratings, ensure_ascii=False, indent=2) + "\n")
    return key


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--workflow", action="append", choices=("legacy", "single", "staged"))
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    manifest = prepare(args.output, workflows=args.workflow or ("legacy", "single", "staged"), case_ids=args.case_ids)
    if args.run:
        key = run(args.output, manifest)
        if any(row["status"] != "completed" for row in key):
            raise SystemExit(1)
    else:
        print(args.output / "manifest.json")


if __name__ == "__main__":
    main()
