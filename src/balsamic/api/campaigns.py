"""Campaign-level reads: the workspace, the campaign list with stages, summaries, charters, studies, races, catalogs, removal, export."""
import json

from fastapi import Query
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from typing import Literal

from optimization_framework.contracts.requests import Model
from optimization_framework.storage.sqlite import now

from balsamic.evolution.stages import campaign_stage, next_step
from balsamic.threads.service import ensure_main, main_thread_id


class RemovalInput(Model):
    keep: Literal["nothing", "summary", "records"]


def campaign_row(workspace, campaign):
    stage = campaign_stage(workspace, campaign)
    return {"id": campaign["id"], "name": campaign["name"], "version": campaign["version"], "objective": campaign.get("objective"),
            "autonomy": campaign.get("autonomy"), "stage": stage["stage"], "stage_pinned": stage["pinned"], "stage_basis": stage["basis"],
            "autopilot": campaign.get("autopilot") or {"enabled": False}, "problem_note_id": campaign.get("problem_note_id"),
            "active_thread_id": main_thread_id(campaign["id"]), "created_at": campaign.get("created_at"), "updated_at": campaign.get("updated_at"),
            "next_step": next_step(workspace, campaign)}


def install(app, workspace):
    workspace_id = workspace.store.identity()

    @app.get("/api/v1/workspace")
    def workspace_info():
        from optimization_framework.agents.controller import dev_profile
        from optimization_framework.llm.providers import provider_status
        status = workspace.pi.harness_status or {}
        if not status or not status.get("configured"):   # an unsigned harness is asked again: a sign-in may have completed
            try:
                status = workspace.pi.client.status() or {}
                workspace.pi.harness_status = status
            except (ValueError, OSError):
                status = {}
        direct = provider_status()   # an HTTP provider for the report writer and the implementation service, when enabled
        admission = getattr(workspace, "admission", None)
        return {"workspace_id": workspace_id, "max_workers": workspace.max_workers, "dev_mode": bool(dev_profile()),
                "provider": {"configured": bool(status.get("configured")) or bool(direct.get("configured")), "providers": status.get("providers", {}),
                             "harness": bool(status.get("configured")), "direct": {key: direct.get(key) for key in ("configured", "provider", "model", "billing_mode")}},
                "level": admission.level() if admission is not None else None, "service": "balsamic"}

    @app.get("/api/v1/campaigns")
    def campaigns():
        return {"campaigns": [campaign_row(workspace, campaign) for campaign in workspace.store.list("campaign")]}

    @app.get("/api/v1/campaigns/{campaign_id}/summary")
    def summary(campaign_id: str):
        campaign = workspace.store.get(campaign_id, "campaign")
        ensure_main(workspace, campaign_id)
        tasks = workspace.store.list("task", campaign_id)
        charters = workspace.store.list("charter", campaign_id)
        counts = {kind: workspace.store.count(kind, campaign_id) for kind in ("task", "hypothesis", "trial", "study", "adaptive_race", "decision", "thread", "message")}
        try:
            budget = workspace.resources.assessment(campaign_id)
        except (KeyError, ValueError):
            budget = None
        return {"campaign": campaign, "row": campaign_row(workspace, campaign), "tasks": tasks, "charter": charters[-1] if charters else None,
                "budget": budget, "stage": campaign_stage(workspace, campaign), "counts": counts, "studies": workspace.store.list("study", campaign_id),
                # Rows, not records: the pages poll this on every event, and a run's designs are megabytes (/api/v1/experiments/{id} has them).
                "trials": [public_trial(trial) for trial in workspace.store.list_trial_rows(campaign_id)]}

    @app.get("/api/v1/campaigns/{campaign_id}/charters")
    def charters(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.store.list("charter", campaign_id)

    @app.get("/api/v1/campaigns/{campaign_id}/removal")
    def removal_plan(campaign_id: str):
        return workspace.removal.plan(campaign_id)

    @app.post("/api/v1/campaigns/{campaign_id}/removal")
    def remove_campaign(campaign_id: str, body: RemovalInput):
        # Removing a campaign is the researcher's alone and leaves no command behind: the commands go with it.
        workspace.store.get(campaign_id, "campaign")
        try:
            return workspace.removal.remove(campaign_id, body.keep)
        except ValueError as exc:
            # Answered here: the general handler would file the refusal as an issue of the campaign.
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/v1/removed-campaigns")
    def removed_campaigns():
        return workspace.removal.removed()

    @app.get("/api/v1/campaigns/{campaign_id}/validations")
    def validation_requirements(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return [workspace.validations.assess(item["id"]) for item in workspace.store.list("validation_requirement", campaign_id)]

    @app.get("/api/v1/confirmations/{protocol_id}")
    def confirmation_assessment(protocol_id: str):
        return workspace.confirmations.assess(protocol_id)

    @app.get("/api/v1/problems")
    def problem_catalog(campaign_id: str | None = None):
        from optimization_framework.evaluation.registry import problems
        definitions = [problems.get(name).describe().model_dump(mode="json") for name in problems.ids()]
        if campaign_id:
            workspace.store.get(campaign_id, "campaign")
            known = {(value["id"], value["version"], value["evaluator_version"]) for value in definitions}
            for task in workspace.store.list("task", campaign_id):
                if task.get("evaluator_manifest"):
                    value = workspace.evaluators.describe_task(workspace.evaluators.task_view(task)).model_dump(mode="json")
                    identity = (value["id"], value["version"], value["evaluator_version"])
                    if identity not in known:
                        definitions.append(value)
                        known.add(identity)
        return {"problems": definitions}

    @app.get("/api/v1/registered-recipes")
    def recipe_catalog():
        from optimization_framework.evaluation.registered_recipes import recipes
        return {"recipes": [recipes.get(identity)[1].model_dump(mode="json") for identity in sorted(recipes.entries())]}

    @app.get("/api/v1/inference-adapters")
    def inference_catalog():
        from optimization_framework.evaluation.inference import adapters
        return {"adapters": adapters.catalog()}

    @app.get("/api/v1/evaluator-contracts")
    def evaluator_contracts():
        from optimization_framework.implementations.models import EvaluatorSpec, EvaluatorPackage
        from optimization_framework.contracts.evaluators import EvaluatorManifest
        return {"specification": EvaluatorSpec.model_json_schema(), "package": EvaluatorPackage.model_json_schema(),
                "manifest": EvaluatorManifest.model_json_schema()}

    @app.get("/api/v1/campaigns/{campaign_id}/studies")
    def studies(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.store.list("study", campaign_id)

    @app.get("/api/v1/study-rules")
    def study_rule_catalog():
        from optimization_framework.analysis.rules import catalog
        return {"rules": catalog()}

    @app.get("/api/v1/study-templates")
    def study_templates(task_id: str | None = None):
        from optimization_framework.evaluation.registry import problems
        from optimization_framework.contracts.templates import StudyTemplateVersion
        from optimization_framework.contracts.problems import ProblemInstance
        from optimization_framework.assets.references import input_candidates, matching_sets
        instance = ProblemInstance(**workspace.store.get(task_id, "task")["problem"]) if task_id else None
        entries = []
        for problem_id in problems.ids():
            adapter = problems.get(problem_id)
            for template in adapter.study_templates() if hasattr(adapter, "study_templates") else []:
                parsed = StudyTemplateVersion(**template)
                entries.append({"problem_id": problem_id, "template": parsed.model_dump(mode="json"),
                    "input_candidates": input_candidates(workspace.assets, parsed.input_requirements,
                        instance if instance and instance.definition_id == problem_id else None),
                    "reference_sets": matching_sets(problem_id, parsed.input_requirements)})
        return {"templates": entries}

    @app.get("/api/v1/nominations/{nomination_id}")
    def nomination_evidence(nomination_id: str):
        return workspace.store.get(nomination_id, "nomination")

    @app.get("/api/v1/drafts/{draft_id}")
    def experiment_draft(draft_id: str):
        with workspace.lock, workspace.store.transaction():
            return {"draft": workspace.store.get(draft_id, "experiment_draft"), "readiness": workspace.drafts.readiness(draft_id)}

    @app.get("/api/v1/reproduction-sources")
    def reproduction_sources(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return {"sources": workspace.reproductions.sources(campaign_id)}

    @app.get("/api/v1/reproduction-comparisons/{comparison_id}")
    def reproduction_comparison(comparison_id: str):
        return workspace.store.get(comparison_id, "reproduction_comparison")

    @app.get("/api/v1/study-executions/{execution_id}")
    def study_execution(execution_id: str):
        with workspace.lock, workspace.store.transaction():
            return workspace.study_executions.assess(execution_id)

    @app.get("/api/v1/studies/{study_id}/selection")
    def selection_readiness(study_id: str):
        from optimization_framework.analysis.studies import selection_assessment
        with workspace.lock, workspace.store.transaction():
            return {key: value for key, value in selection_assessment(workspace.store, study_id).items() if key != "evidence"}

    @app.get("/api/v1/studies/{study_id}/race")
    def study_race(study_id: str):
        workspace.store.get(study_id, "study")
        matches = [race for race in workspace.store.list("adaptive_race")
                   if race.get("study_id") == study_id or (race.get("confirmation") or {}).get("study_id") == study_id]
        return {"race": workspace.racing.view(matches[-1]["id"]) if matches else None}

    @app.get("/api/v1/races/{race_id}")
    def adaptive_race(race_id: str):
        return {"race": workspace.racing.view(race_id)}

    @app.get("/api/v1/campaigns/{campaign_id}/comparison")
    def general_comparison(campaign_id: str, study_id: str | None = None, cost_axis: str | None = None, cost_view: str = "full_attributed_cost",
                           trial_ids: list[str] | None = Query(default=None)):
        from optimization_framework.analysis.general import report
        return report(workspace, campaign_id, study_id=study_id, cost_axis=cost_axis, cost_view=cost_view, trial_ids=trial_ids)

    @app.get("/api/v1/campaigns/{campaign_id}/analysis")
    def analysis(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        from optimization_framework.analysis.comparison import analyze_trials
        trials = workspace.store.list("trial", campaign_id)
        return analyze_trials(trials, {t["id"]: workspace.metrics(t["id"]) for t in trials}, workspace.store.list("task", campaign_id))

    @app.get("/api/v1/campaigns/{campaign_id}/export")
    def export(campaign_id: str):
        campaign = workspace.store.get(campaign_id, "campaign")
        return Response(export_markdown(workspace, campaign), media_type="text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="balsamic-{campaign_id}.md"'})


def public_trial(trial):
    return {k: v for k, v in trial.items() if k not in {"pid", "process_identity"}}


def export_markdown(workspace, campaign):
    campaign_id = campaign["id"]
    lines = [f"# {campaign['name']}", "", campaign["objective"], "",
        f"Charter version: {campaign['version']}. Exported: {now()}.", "",
        "## Experiment charter", "", "```json", json.dumps(campaign, indent=2), "```", "", "## Tasks", ""]
    for task in workspace.store.list("task", campaign_id):
        lines += [f"### {task['name']} ({task['split']})", "", "```json", json.dumps(task, indent=2), "```", ""]
    lines += ["## Hypotheses and evidence", ""]
    for h in workspace.store.list("hypothesis", campaign_id):
        lines += [f"### {h['title']}", "", f"Origin: {h.get('origin', 'unknown')}; status: {h.get('status', 'proposed')}.", "",
                  h.get("mechanism", ""), "", h.get("rationale", ""), "", "```json", json.dumps(h, indent=2), "```", ""]
    lines += ["## Trials", "", "Partial and stopped trials are retained. Search scores are not automatically Fourier-converged.", ""]
    for trial in workspace.store.list("trial", campaign_id):
        lines += [f"### {trial['algorithm']} — {trial['id']}", "", "```json", json.dumps(public_trial(trial), indent=2), "```", ""]
    lines += ["## Research decisions", ""]
    for d in workspace.store.list("decision", campaign_id):
        lines += [f"### {d['title']}", "", d.get("context", ""), "", "```json", json.dumps(d, indent=2), "```", ""]
    lines += ["## Conversation", ""]
    for message in workspace.store.list("message", campaign_id):
        lines += [f"### {message['role']} · {message['created_at']} · {message.get('thread_id', 'main')}", "", message["content"], ""]
    lines += ["## Researcher guidance", "", workspace.memory.state(campaign_id)["guidance"], ""]
    for kind, title in (("manager_note", "Recorded findings and guidance"), ("manager_issue", "Issues"),
                        ("implementation_grant", "Implementation jobs and accounting")):
        lines += [f"## {title}", "", "```json", json.dumps(workspace.store.list(kind, campaign_id), indent=2), "```", ""]
    version_ids = {h.get("implementation_version_id") for h in workspace.store.list("hypothesis", campaign_id)}
    version_ids.update(t.get("implementation_version_id") for t in workspace.store.list("trial", campaign_id))
    versions = [v for v in workspace.store.list("implementation_cache") if v["id"] in version_ids]
    lines += ["## Referenced implementation versions", "", "Implementation validation is distinct from performance evidence.",
              "", "```json", json.dumps(versions, indent=2), "```", ""]
    return "\n".join(lines)
