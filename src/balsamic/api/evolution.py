"""The evolution graph and its cards: ledger rows, lineage, races, finalists, confirmations; one strategy; provenance."""
from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse

from balsamic.evolution import ledger, lineage
from balsamic.evolution.provenance import trail
from balsamic.evolution.stages import campaign_stage


def _no_store(value, status=200):
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


def install(app, workspace):
    @app.get("/api/v1/campaigns/{campaign_id}/evolution")
    def evolution(campaign_id: str, after: int = Query(0, ge=0), progress: bool = False, curves: bool = False):
        campaign = workspace.store.get(campaign_id, "campaign")
        rows, cursor = ledger.rows(workspace, campaign_id, with_curves=curves)
        if after and after >= cursor:
            return _no_store({"campaign_id": campaign_id, "event_cursor": cursor, "unchanged": True})
        nodes, last_round = lineage.layout(rows)
        races = []
        for race in workspace.store.list("adaptive_race", campaign_id):
            try:
                view = workspace.racing.view(race["id"])
            except (KeyError, ValueError):
                continue
            races.append({key: view.get(key) for key in ("id", "study_id", "status", "stage", "started_at", "deadline_at", "elapsed_seconds", "total_seconds", "running_workers",
                                                          "worker_seconds_spent", "worker_seconds_cap", "configurations", "decisions")})
        body = {"campaign_id": campaign_id, "event_cursor": cursor, "unchanged": False, "nodes": rows, "layout": nodes, "edges": lineage.edges_of(rows), "rounds": last_round,
                "races": races, "finalists": workspace.store.list("finalist_selection", campaign_id), "confirmations": workspace.store.list("confirmation_protocol", campaign_id),
                "stage": campaign_stage(workspace, campaign), "interventions": workspace.store.list("intervention", campaign_id)[-20:]}
        if progress:
            live = workspace.store.list_trials_in_status({"queued", "running", "pausing", "stopping"}, campaign_id)
            best = max((row["best"]["score"] for row in rows if row["best"]["score"] is not None), default=None)
            body["progress"] = {"best_so_far": best, "live_trials": [{"id": t["id"], "hypothesis_id": t.get("hypothesis_id"), "status": t["status"]} for t in live],
                                "finalist_rungs": [{"id": row["id"], "rung": row["highest_rung_seconds"]} for row in rows if row["finalist"]]}
        return _no_store(body)

    @app.get("/api/v1/hypotheses/{hypothesis_id}")
    def hypothesis_card(hypothesis_id: str):
        try:
            hypothesis = workspace.store.get(hypothesis_id, "hypothesis")
        except KeyError:
            raise HTTPException(404, "Unknown hypothesis")
        campaign_id = hypothesis["campaign_id"]
        rows, _ = ledger.rows(workspace, campaign_id, with_curves=True)
        row = next((r for r in rows if r["id"] == hypothesis_id), None)
        parents = [workspace.store.get(link["id"], "hypothesis") for link in hypothesis.get("parent_links") or [] if _exists(workspace, link["id"], "hypothesis")]
        children = [h for h in workspace.store.list("hypothesis", campaign_id) if any(link["id"] == hypothesis_id for link in h.get("parent_links") or [])]
        trials = [t for t in workspace.store.list_trial_rows(campaign_id) if t.get("hypothesis_id") == hypothesis_id]
        from optimization_framework.agents.discussions import view as rounds_view
        rounds = rounds_view(workspace, campaign_id)
        mine = [r for r in rounds.get("rounds", []) if hypothesis_id in str(r)]
        validations = [v for v in workspace.store.list("validation_result", campaign_id) if v.get("trial_id") in {t["id"] for t in trials}]
        implementation = workspace.implementations.readiness(hypothesis)
        return _no_store({"hypothesis": hypothesis, "ledger_row": row, "parents": [{k: p.get(k) for k in ("id", "title", "status", "parent_links")} for p in parents],
                          "children": [{k: c.get(k) for k in ("id", "title", "status", "parent_links")} for c in children],
                          "reviews": hypothesis.get("reviews") or [], "rounds": mine, "implementation": implementation,
                          "validations": validations, "trials": [{k: t.get(k) for k in ("id", "status", "algorithm", "seed", "wall_seconds", "execution_seconds", "created_at", "stopped_by", "race_phase")} for t in trials],
                          "race_cells": (row or {}).get("race"), "provenance": trail(workspace, hypothesis_id)["trail"]})

    @app.get("/api/v1/records/{record_id}/provenance")
    def provenance(record_id: str):
        try:
            return _no_store(trail(workspace, record_id))
        except KeyError:
            raise HTTPException(404, "Unknown record")


def _exists(workspace, identity, kind):
    try:
        workspace.store.get(identity, kind)
        return True
    except KeyError:
        return False
