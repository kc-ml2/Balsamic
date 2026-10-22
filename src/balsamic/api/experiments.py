"""Experiments: the campaign's runs by kind, and one run's record, curve, logs and artifacts."""
from fastapi import Query
from fastapi.responses import FileResponse, JSONResponse

from balsamic.api.campaigns import public_trial
from balsamic.api.threads import scoped


def install(app, workspace):
    @app.get("/api/v1/campaigns/{campaign_id}/experiments")
    def experiments(campaign_id: str, after: int = Query(0, ge=0)):
        workspace.store.get(campaign_id, "campaign")

        def body():
            from optimization_framework.campaigns.experiment_rules import current, classify, check_trial_ids
            rules = current(workspace.store, campaign_id)
            trials = [public_trial(trial) for trial in workspace.store.list_trial_rows(campaign_id)]
            kinds = classify(trials, rules["long_run_threshold_seconds"] if "long_run_threshold_seconds" in rules else rules.get("threshold_seconds", 600),
                             check_trial_ids(workspace.store, campaign_id)) if trials else {}
            hidden = set(rules.get("hidden_trial_ids") or [])
            groups = {"short": [], "long": [], "checks": []}
            for trial in trials:
                kind = kinds.get(trial["id"], "short")
                groups["checks" if kind == "check" else kind].append({**trial, "hidden": trial["id"] in hidden})
            return {**groups, "drafts": workspace.store.list("experiment_draft", campaign_id), "rules": rules,
                    "profiles": workspace.store.list("runtime_profile", campaign_id)}
        return scoped(workspace, campaign_id, after, body)

    @app.get("/api/v1/experiments/{trial_id}")
    def experiment(trial_id: str):
        trial = workspace.store.get(trial_id, "trial")
        chain = []
        current = trial
        seen = set()
        while current.get("parent_trial_id") and current["id"] not in seen:
            seen.add(current["id"])
            try:
                current = workspace.store.get(current["parent_trial_id"], "trial")
            except KeyError:
                break
            chain.insert(0, current["id"])
        races = [race for race in workspace.store.list("adaptive_race", trial["campaign_id"])
                 if trial_id in {cell.get("trial_id") for cell in (race.get("cells") or {}).values()} if isinstance(race.get("cells"), dict)]
        return {"trial": public_trial(trial), "chain_trial_ids": [*chain, trial_id],
                "validations": [row for row in workspace.store.list("validation_result", trial["campaign_id"]) if row.get("trial_id") == trial_id],
                "race": {"race_id": races[-1]["id"]} if races else None,
                "artifacts": ["spec.json", "result.json", "archive.json", "metrics.jsonl", "progress.json"]}

    @app.get("/api/v1/experiments/{trial_id}/metrics")
    def metrics(trial_id: str, limit: int | None = Query(default=None, ge=1, le=1000), fields: str | None = None,
                points: int | None = Query(default=None, ge=2, le=5000)):
        """A run's metrics. fields: the scalar columns to return (comma separated); points: at most this many rows, evenly
        spread and ending with the last, for drawing a curve of a long run."""
        rows = workspace.metrics(trial_id, limit=limit, fields=[name for name in fields.split(",") if name] if fields else None)
        if points and len(rows) > points:
            step = (len(rows) - 1) / (points - 1)
            rows = [rows[round(index * step)] for index in range(points)]
        return rows

    @app.get("/api/v1/experiments/{trial_id}/logs")
    def logs(trial_id: str):
        workspace.store.get(trial_id, "trial")
        path = workspace.job_dir(trial_id) / "worker.log"
        return {"text": path.read_text(errors="replace")[-20000:] if path.exists() else ""}

    @app.get("/api/v1/experiments/{trial_id}/artifacts/{filename}")
    def artifact(trial_id: str, filename: str):
        trial = workspace.store.get(trial_id, "trial")
        if filename not in {"spec.json", "result.json", "archive.json", "metrics.jsonl", "progress.json"}:
            raise KeyError(filename)
        path = workspace.job_dir(trial_id) / filename
        if filename == "archive.json" and not path.is_file():
            return JSONResponse(trial.get("progress", {}).get("archive", []), headers={"Content-Disposition": 'attachment; filename="archive.json"'})
        if not path.is_file():
            raise KeyError(filename)
        return FileResponse(path, filename=filename)

    @app.get("/api/v1/experiments/{trial_id}/problem")
    @app.get("/api/v1/trials/{trial_id}/problem")
    def trial_problem_catalog(trial_id: str):
        return workspace.describe_trial_problem(trial_id)

    @app.get("/api/v1/campaigns/{campaign_id}/hypotheses")
    def hypotheses(campaign_id: str, after: int = Query(0, ge=0)):
        workspace.store.get(campaign_id, "campaign")

        def body():
            from optimization_framework.agents.discussions import view
            from optimization_framework.campaigns.proposals import readiness
            rows = workspace.store.list("hypothesis", campaign_id)
            return {"hypotheses": [{**row, "readiness": readiness(workspace.store, row)} for row in rows],
                    "aliases": workspace.store.list("agent_alias", campaign_id), "rounds": view(workspace, campaign_id)}
        return scoped(workspace, campaign_id, after, body)
