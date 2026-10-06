"""Scientific handoff, isolation, provenance and durable workflow tests; no model calls."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from optimization_framework.api.app import create_app
from optimization_framework.reports.editorial import Analyze, Brief, Draft, EvidenceRead, Investigation, Review, Selection, SourceRead, SourceSearch
from optimization_framework.reports.evidence import EvidenceTools, freeze, freeze_curve
from optimization_framework.reports.writer import WriterRequest
from test_report_review import SOURCE, fake_provider, feedback


@pytest.fixture
def app(tmp_path, monkeypatch):
    fake_provider(monkeypatch)
    return create_app(tmp_path, start_workers=False)


def scripted_model(monkeypatch, *, question=None, blocking=False, fail_stage=None, science_reads=False, forged=False):
    calls = []
    def respond(adapter, role, system, content, *, result_type):
        calls.append({"role": role, "content": deepcopy(content), "schema": result_type.__name__})
        adapter.usage["calls"] += 1
        adapter.usage["subscription_calls"] += 1
        if fail_stage == result_type.__name__:
            raise ValueError("Simulated interrupted provider response")
        if result_type is Brief:
            return Brief(focus="A practical, budget-limited comparison", explicit_guidance=[content["researcher_notes"]],
                inferred_intent=["Practical cost matters"], reader_and_depth="Expert reader; explain confounds, not textbook definitions.",
                alternative_interpretations=["Training maturity may be the main question"], question=question)
        if result_type is Investigation:
            receipts = content["read_receipts"]
            if not receipts:
                return Investigation(requests=[EvidenceRead(tool="evidence.read", record_id="supplied-evidence")], coverage="Need to inspect the saved results.")
            return Investigation(coverage="Uneven exploratory evidence; no family-wide ranking.", claims=[{
                "id": "c1", "statement": "The short campaign does not establish general superiority.", "kind": "inference",
                "support": ["invented" if forged else receipts[0]["id"]], "reliability": "Limited to recorded budgets",
                "limitations": "Unequal tuning and unfinished learning", "interest": "Practical decision under current budget",
                "novelty": "Not established by these runs"}], missing_evidence=["Matched budgets and completed learning curves"],
                proposed_experiments=["A separately approved matched-budget replication"])
        if result_type is Selection:
            return Selection(focus="Practical comparison under the saved budgets", argument="Describe the measured advantage with confounds.",
                claims=[{"claim_id": "c1", "place": "main", "reason": "Answers the researcher's practical question"}],
                figure_ids=[], figure_reasons={key: "Redundant to this narrow argument" for key in content["figures"]},
                outline=["Budget-limited findings", "What remains unsettled"], essential_caveats=["Unequal tuning and incomplete learning"])
        if result_type is Draft:
            phrase = "The short campaign does not establish general superiority."
            marks = content.get("feedback", {}).get("annotations", [])
            green = " ".join(mark["exact"] for mark in marks if mark["tier"] == "good")
            return Draft(title="A bounded comparison", body_html=f'<article><section id="findings"><h2>Measured scope</h2><p>{phrase}</p><p>{green}</p></section></article>',
                change_summary="Selected the narrow conclusion and removed redundant figures.", claim_uses={"c1": phrase},
                feedback_response={mark["id"]: "Handled in context" for mark in marks},
                review_response={issue["id"]: "Qualified the conclusion against the saved results" for review in content.get("reviews", []) for issue in review["issues"]})
        if result_type is Review:
            if "Use issue IDs starting science_" in system:
                if science_reads and "initial_review" not in content:
                    return Review(assessment="Check the broader evidence before concluding", requests=[EvidenceRead(tool="evidence.read", record_id="supplied-evidence")])
                return Review(assessment="scientific_review_private_framing", issues=[{"id": "science_1", "severity": "improve", "problem": "Avoid causal ranking",
                    "evidence_ids": [content["read_receipts"][0]["id"]], "action": "Retain the budget qualification"}])
            if "issue IDs starting\nverify_" in system or "issue IDs starting\n+verify_" in system:
                return Review(assessment="Evidence check", issues=[{"id": "verify_1", "severity": "blocking", "problem": "A consequential contradiction remains", "action": "Narrow the claim"}] if blocking else [])
            return Review(assessment="Useful and appropriately narrow.")
        raise AssertionError(result_type)
    monkeypatch.setattr("optimization_framework.research.engine.LLMAdapter.call_with_prompt", respond)
    return calls


def start(app, **kw):
    return app.state.report_writer.start(evidence={"outcome": "Unequal budgets; unfinished learning; observed short-run advantage"},
        brief="Annealing looks strong. DQN still climbing. Wall time matters.", background=False, **kw)


def test_stages_select_figures_preserve_green_and_isolate_reviews(app, monkeypatch):
    calls = scripted_model(monkeypatch, science_reads=True)
    report = app.state.reports.add(SOURCE, "Original", evidence={"budgets": [100, 1000]})
    app.state.reports.submit(report["id"], feedback(report, focus="Explain wall time and unfinished learning"))
    request = WriterRequest(submission_id="review_a", request_id="staged_once")
    job = app.state.report_writer.start(report_id=report["id"], request=request, background=False)
    assert job["status"] == "completed", job.get("error")
    draft = app.state.reports.get(job["result_id"])
    assert draft["parent_id"] == report["id"] and "very strong phrase" in draft["html"]
    assert "<figure" not in draft["html"]  # A reasoned omission is now allowed.
    assert app.state.reports.get(report["id"])["html"] == SOURCE
    assert draft["revision"]["editorial"]["selection"]["figure_reasons"]["figure-1"]
    reader = next(call for call in calls if call["role"] == "report_reader_reviewer")
    assert "scientific_review_private_framing" not in json.dumps(reader)
    assert reader["content"]["inventory"] and len(reader["content"]["read_receipts"]) == 2
    assert calls[0]["content"]["feedback"]["focus"] == "Explain wall time and unfinished learning"
    count = len(calls)
    assert app.state.report_writer.start(report_id=report["id"], request=request, background=False) == job
    assert len(calls) == count and job["usage"]["calls"] == count <= 14
    stages = [app.state.workspace.store.get(key) for key in job["stage_ids"]]
    assert all(stage["content_hash"] and stage["status"] == "completed" for stage in stages)


def test_focus_question_resumes_saved_brief_once_even_after_restart(app, monkeypatch):
    calls = scripted_model(monkeypatch, question="Emphasize deployment cost or learning dynamics?")
    job = start(app)
    assert job["status"] == "awaiting_focus" and len(calls) == 1
    app.state.report_writer.recover()
    answered = app.state.report_writer.answer(job["id"], "Deployment cost", background=False)
    assert answered["status"] == "completed", answered.get("error")
    assert sum(call["schema"] == "Brief" for call in calls) == 1
    assert calls[1]["content"]["focus_answer"] == "Deployment cost"
    assert answered["usage"]["calls"] == len(calls)
    assert app.state.report_writer.answer(job["id"], "Deployment cost", background=False) == answered


def test_blocking_reviews_are_bounded_and_visible_on_provisional_draft(app, monkeypatch):
    scripted_model(monkeypatch, blocking=True)
    job = start(app)
    assert job["status"] == "needs_attention", job.get("error")
    assert sum("_edit_" in key for key in job["stage_ids"]) == 2
    draft = app.state.reports.get(job["result_id"])
    assert draft["revision"]["editorial"]["unresolved"][0]["severity"] == "blocking"


def test_provider_failure_keeps_receipts_and_does_not_replay(app, monkeypatch):
    calls = scripted_model(monkeypatch, fail_stage="Selection")
    job = start(app, request_id="failure")
    assert job["status"] == "failed" and job["receipt_ids"]
    assert job["stage_ids"] and not app.state.reports.listing()
    n = len(calls)
    assert start(app, request_id="failure")["id"] == job["id"]
    app.state.report_writer.recover()
    assert len(calls) == n
    stage = app.state.workspace.store.get(job["stage_ids"][-1])
    assert stage["status"] == "failed" and stage["content_hash"]


def test_unknown_citations_fail_before_prose(app, monkeypatch):
    calls = scripted_model(monkeypatch, forged=True)
    job = start(app)
    assert job["status"] == "failed" and "invented receipts" in job["error"]
    assert not any(call["schema"] == "Draft" for call in calls)


def test_single_author_baseline_uses_same_bounds_and_records_self_review(app, monkeypatch):
    calls = scripted_model(monkeypatch)
    job = start(app, workflow="single")
    assert job["status"] == "completed", job.get("error")
    assert {call["role"] for call in calls} == {"technical_report_writer"}
    assert job["limits"]["model_calls"] == 14
    assert any("scientific_review_private_framing" in json.dumps(call["content"].get("working_notes")) for call in calls)


def campaign(app):
    store = app.state.workspace.store
    store.put("campaign", {"id": "campaign_test", "name": "Adaptive comparison", "objective": "Practical budgets"})
    store.put("task", {"id": "task_dev", "campaign_id": "campaign_test", "split": "development"})
    store.put("task", {"id": "task_secret", "campaign_id": "campaign_test", "split": "confirmation", "locked": True})
    for identity, task in (("trial_a", "task_dev"), ("trial_b", "task_dev"), ("trial_secret", "task_secret")):
        store.put("trial", {"id": identity, "campaign_id": "campaign_test", "task_id": task, "algorithm": "annealing" if identity == "trial_a" else "dqn",
            "status": "running", "seed": 1, "max_steps": 100 if identity == "trial_a" else 1000, "algorithm_config": {"tuning": "adaptive"},
            "result": {"best_objective": 0.9}})
        directory = app.state.workspace.job_dir(identity)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "metrics.jsonl").write_text(''.join(json.dumps({"step": n, "best_objective": n / 10}) + '\n' for n in range(1, 6 if identity == "trial_a" else 11)))
    return store


def evidence_tools(app):
    store = campaign(app)
    snapshot = freeze(app.state.workspace, "test", "campaign_test", {}, ["trial_a"])
    job = {"id": "test", "campaign_id": "campaign_test", "snapshot_id": snapshot["id"], "receipt_ids": [], "literature": True}
    store.put("report_writer_job", job)
    return EvidenceTools(app.state.workspace, job, {})


def test_frozen_snapshot_excludes_holdout_and_records_comparison_confounders(app):
    tools = evidence_tools(app)
    assert "trial_secret" not in tools.snapshot["records"]
    assert "task_secret" not in tools.snapshot["records"]
    assert tools.snapshot["gaps"]
    receipt = tools.execute(Analyze(tool="analysis.compare", trial_ids=["trial_a", "trial_b"], plot=False))
    assert receipt["status"] == "completed"
    result = receipt["result"]
    assert result["requested_horizon"] == 5 and [row["recorded_axis"] for row in result["comparisons"]] == [5, 5]
    assert [row["max_steps"] for row in result["comparisons"]] == [100, 1000]
    assert "does not equalize tuning" in result["limitations"]
    app.state.workspace.store.put("trial", {**tools.snapshot["records"]["trial_a"], "status": "failed"})
    (app.state.workspace.job_dir("trial_a") / "metrics.jsonl").write_text('{"step": 999,"best_objective":99}\n')
    again = tools.execute(Analyze(tool="analysis.compare", trial_ids=["trial_a", "trial_b"], plot=False))
    assert again["result"] == result
    outside = tools.execute(EvidenceRead(tool="evidence.read", record_id="trial_secret"))
    assert outside["status"] == "failed" and "outside" in outside["error"]
    extrapolation = tools.execute(Analyze(tool="analysis.compare", trial_ids=["trial_a", "trial_b"], horizon=100, plot=False))
    assert extrapolation["status"] == "failed" and "extrapolation" in extrapolation["error"]


def test_indirect_withheld_references_and_cross_campaign_pins_are_excluded(app):
    store = campaign(app)
    store.put("study", {"id": "private_study", "campaign_id": "campaign_test", "scope": {"instances": ["task_secret"]}})
    store.put("decision", {"id": "private_decision", "campaign_id": "campaign_test", "context": "Based on trial_secret results"})
    snapshot = freeze(app.state.workspace, "withheld", "campaign_test", {}, [])
    assert "private_study" not in snapshot["records"] and "private_decision" not in snapshot["records"]
    with pytest.raises(ValueError, match="unavailable"):
        freeze(app.state.workspace, "outside", "campaign_test", {}, ["foreign_trial"])


def test_trace_prefixes_and_resets_are_not_misrepresented_as_comparisons(app, tmp_path):
    path = tmp_path / "trace"
    path.write_text('{"step":1,"best_objective":0.2}\n{"step":2,"best_objective":0.4}\n')
    trace = freeze_curve(path, 35)
    assert trace["coverage"] == "sampled" and trace["source_bytes_at_open"] > trace["scanned_bytes"]
    assert trace["windows"] and "Not uniform" in trace["sampling"]
    tools = evidence_tools(app)
    tools.snapshot["records"]["curve:trial_a"]["rows"].append({"step": 1, "best_objective": 0.2})
    receipt = tools.execute(Analyze(tool="analysis.compare", trial_ids=["trial_a"], plot=False))
    assert receipt["status"] == "failed" and "reset" in receipt["error"]


def test_large_journal_sampling_keeps_tail_and_reports_unequal_horizon_gaps(app, tmp_path):
    path = tmp_path / "large.jsonl"
    path.write_text(''.join(json.dumps({"step": index, "best_objective": index / 10000, "attempt_id": "first", "unused": "x" * 200}) + '\n' for index in range(1000)))
    trace = freeze_curve(path, 65536)
    assert trace["coverage"] == "sampled" and trace["scanned_bytes"] <= 65536
    assert trace["rows"][0]["step"] == 0 and trace["rows"][-1]["step"] == 999
    assert all("unused" not in row for row in trace["rows"])
    tools = evidence_tools(app)
    tools.snapshot["records"]["curve:trial_a"] = trace
    result = tools.execute(Analyze(tool="analysis.compare", trial_ids=["trial_a"], horizon=500, plot=False))
    assert result["status"] == "completed"
    comparison = result["result"]["comparisons"][0]
    assert comparison["trace_coverage"] == "sampled"
    assert comparison["horizon_gap"] == 500 - comparison["recorded_axis"] > 0
    assert "cannot establish convergence" in result["result"]["limitations"]


def test_derived_plot_is_self_contained_and_links_exact_analysis(app):
    pytest.importorskip("matplotlib")
    tools = evidence_tools(app)
    receipt = tools.execute(Analyze(tool="analysis.compare", trial_ids=["trial_a", "trial_b"]))
    assert receipt["status"] == "completed"
    html = tools.figures[receipt["result"]["figure_id"]]
    assert "<svg" in html and "<figcaption>" in html and "src=" not in html
    assert receipt["result"]["receipt_id"] == receipt["id"]


def test_literature_is_scoped_bounded_and_failed_reads_are_saved(app, monkeypatch):
    tools = evidence_tools(app)
    calls = []
    def search(workspace, effect):
        calls.append(effect)
        return {"id": "retrieval_test", "result": {"sources": [{"id": "paper", "title": "A relevant primary study"}]}}
    monkeypatch.setattr("optimization_framework.research.sources.deliver", search)
    monkeypatch.setattr("optimization_framework.research.literature.LiteratureReader.read", lambda *a, **kw: (_ for _ in ()).throw(ValueError("Full text unavailable")))
    receipt = tools.execute(SourceSearch(tool="source.search", query="cost comparisons"))
    assert "metadata only" in receipt["result"]["coverage"]
    failed = tools.execute(SourceRead(tool="source.read", source_id="paper"))
    assert failed["status"] == "failed" and "unavailable" in failed["error"]
    outside = tools.execute(SourceRead(tool="source.read", source_id="another_campaign_source"))
    assert outside["status"] == "failed"
    tools.execute(SourceSearch(tool="source.search", query="novelty"))
    capped = tools.execute(SourceSearch(tool="source.search", query="one more"))
    assert capped["status"] == "failed" and "limit" in capped["error"] and len(calls) == 2


def test_cancel_waiting_job_and_recovery_never_restart_provider(app, monkeypatch):
    calls = scripted_model(monkeypatch, question="Which practical decision?")
    job = start(app)
    assert app.state.report_writer.cancel(job["id"])["status"] == "cancelled"
    with pytest.raises(ValueError, match="not waiting"):
        app.state.report_writer.answer(job["id"], "Budget", background=False)
    assert len(calls) == 1
    job = {**job, "id": "interrupted", "status": "running", "stage_ids": []}
    app.state.workspace.store.put("report_writer_job", job)
    app.state.report_writer.recover()
    assert app.state.workspace.store.get("interrupted")["status"] == "interrupted" and len(calls) == 1


def test_model_policy_is_frozen_for_all_editorial_roles(app, monkeypatch):
    campaign(app)
    policy = {"default": {"model": "author", "reasoning_effort": "medium"},
        "roles": {"report_scientific_reviewer": {"model": "critic", "reasoning_effort": "high"}}}
    monkeypatch.setattr(app.state.workspace.models, "snapshot", lambda campaign_id: deepcopy(policy))
    scripted_model(monkeypatch, question="Which focus?")
    job = start(app, campaign_id="campaign_test")
    policy["roles"]["report_scientific_reviewer"]["model"] = "different"
    assert job["provider_snapshot"]["model_policy"]["roles"]["report_scientific_reviewer"]["model"] == "critic"


def test_evaluation_pack_is_blinded_and_preserves_human_ratings(tmp_path, monkeypatch):
    from optimization_framework.reports.evaluation import prepare, run
    fake_provider(monkeypatch)
    scripted_model(monkeypatch)
    output = tmp_path / "evaluation"
    manifest = prepare(output, workflows=["staged", "single"], case_ids=["narrow_support"])
    key = run(output, manifest)
    assert all(row["status"] == "completed" for row in key)
    html_files = list((output / "blind").glob("*.html"))
    assert len(html_files) == 2
    assert all('"revision": null' in path.read_text() for path in html_files)
    (output / "ratings.json").write_text('{"researcher": "Keep my ratings"}')
    calls_before = [row["usage"]["calls"] for row in key]
    repeated = run(output, manifest)
    assert [row["usage"]["calls"] for row in repeated] == calls_before
    assert json.loads((output / "ratings.json").read_text())["researcher"] == "Keep my ratings"
    with pytest.raises(ValueError, match="different manifest"):
        prepare(output, workflows=["legacy"])


def test_report_tools_cannot_launch_experiments():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Investigation.model_validate({"coverage": "Need a new run", "requests": [{"tool": "experiment.launch", "trial_id": "anything"}]})


def test_job_api_enforces_workspace_and_exports_durable_artifacts(app, monkeypatch):
    campaign(app)
    calls = scripted_model(monkeypatch, question="Which focus?")
    # Keep the fixture synchronous while exercising real request validation/admission.
    writer = app.state.report_writer
    launch = writer.launch
    monkeypatch.setattr(writer, "launch", lambda job, **kw: launch(job, background=False))
    with TestClient(app) as client:
        payload = {"request_id": "api", "campaign_id": "campaign_test", "notes": "Focus on budget"}
        assert client.post("/api/report-writer/jobs", json=payload, headers={"X-Workspace-Id": "other"}).status_code == 412
        job = client.post("/api/report-writer/jobs", json=payload).json()
        assert job["status"] == "awaiting_focus" and "provider_snapshot" not in job and "input" not in job
        assert client.post("/api/report-writer/jobs", json=payload).json()["id"] == job["id"]
        assert len(calls) == 1
        assert client.post("/api/report-writer/jobs", json={**payload, "notes": "Changed"}).status_code == 409
        artifact = client.get(job["artifacts_url"])
        assert "attachment" in artifact.headers["content-disposition"]
        assert artifact.json()["snapshot"]["content_hash"] and artifact.json()["stages"][0]["output"]["question"]
        assert client.post(f"/api/report-writer/jobs/{job['id']}/cancel", json={}).json()["status"] == "cancelled"
