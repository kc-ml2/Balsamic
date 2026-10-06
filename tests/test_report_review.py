from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from optimization_framework.api.app import create_app
from optimization_framework.reports.document import sections, review_html, utf16_slice
from optimization_framework.reports.service import Reports
from optimization_framework.reports.writer import ReportWriter, WriterRequest, WriterResult
from optimization_framework.storage.sqlite import Store


SOURCE = '''<!doctype html><html><head><title>Report</title></head><body>
<nav class="toolbar"><button onclick="window.print()">Print</button></nav>
<section id="one"><h2>Findings</h2><p>A 😀 <b>very strong phrase</b> and a weak claim.</p>
<figure><svg><text>Chart text is not prose</text></svg><figcaption>Measured results.</figcaption></figure></section>
<section id="two"><h2>Limits</h2><p>Only three seeds were tested.</p></section>
<script>throw new Error('Original interaction must be disabled')</script></body></html>'''


@pytest.fixture
def reports(tmp_path):
    return Reports(Store(tmp_path))


def mark(report, phrase="very strong phrase", tier="good", id="m1", section="one", note=""):
    text = report["sections"][section]
    start = len(text[:text.index(phrase)].encode("utf-16-le")) // 2
    return {"id": id, "section_id": section, "start": start, "end": start + len(phrase.encode("utf-16-le")) // 2,
            "exact": phrase, "tier": tier, "note": note}


def feedback(report, **kw):
    return {"schema_version": 1, "report_id": report["id"], "source_hash": report["source_hash"],
            "submission_id": "review_a", "annotations": [mark(report)], "comment": "Keep this concise.", **kw}


def test_source_is_immutable_and_anchors_match_browser_unicode(reports):
    report = reports.add(SOURCE, "Example")
    assert report["html"] == SOURCE
    assert "Chart text" not in report["sections"]["one"]
    assert "very strong phrase" in report["sections"]["one"]
    saved = reports.submit(report["id"], feedback(report))
    assert saved["annotations"][0]["context"] == report["sections"]["one"]
    assert reports.get(report["id"])["html"] == SOURCE
    assert reports.packet(report["id"], "review_a")["feedback"] == saved
    path = reports.store.directory / "reports" / report["id"] / "review_a.json"
    assert json.loads(path.read_text())["report"]["html"] == SOURCE
    with pytest.raises(ValueError, match="Unicode"):
        utf16_slice("😀", 0, 1)


def test_invalid_anchors_overlap_and_duplicate_ids_never_partially_save(reports):
    report = reports.add(SOURCE, "Example")
    for annotations in ([mark(report, phrase="weak claim", id="m2"), {**mark(report), "exact": "different words"}],
                        [mark(report), mark(report, id="m2")], [mark(report), mark(report, phrase="weak claim")],
                        [{**mark(report), "section_id": "missing"}], [{**mark(report), "end": 1000000}]):
        with pytest.raises(ValueError):
            reports.submit(report["id"], feedback(report, annotations=annotations))
        assert reports.latest(report["id"]) is None
    other = reports.add(SOURCE + "\n", "Other source")
    with pytest.raises(ValueError, match="different report"):
        reports.submit(other["id"], feedback(report))


def test_idempotent_submission_and_concurrent_review_protection(reports):
    report = reports.add(SOURCE, "Example")
    saved = reports.submit(report["id"], feedback(report))
    assert reports.submit(report["id"], feedback(report)) == saved
    with pytest.raises(ValueError, match="different feedback"):
        reports.submit(report["id"], feedback(report, comment="changed"))
    def send(n):
        try:
            return reports.submit(report["id"], feedback(report, submission_id=f"review_{n}", expected_submission_id="review_a"))
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(send, range(2)))
    assert sum(value is not None for value in outcomes) == 1
    assert reports.submission(report["id"], "review_a") == saved


def test_portable_html_roundtrip_contains_feedback_and_exact_prose(reports):
    report = reports.add(SOURCE, "Example")
    saved = reports.submit(report["id"], feedback(report, comment='</script><script>alert("injection")</script>'))
    html = review_html(report, saved)
    assert '"workspace_id": null' in html
    assert '\\u003c/script>' in html
    assert "Original interaction must be disabled" not in html
    assert sections(html) == sections(SOURCE)
    assert "Download review HTML" in html and "Import a review" in html


def test_http_workflow_survives_restart_and_no_write_on_get(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    report = app.state.reports.add(SOURCE, "Example")
    base = "/api/reports/" + report["id"]
    with TestClient(app) as client:
        before = app.state.workspace.store.events()
        page = client.get("/reports/" + report["id"])
        assert page.status_code == 200 and "Text review" in page.text
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert client.get("/api/reports").json()["reports"][0]["id"] == report["id"]
        assert app.state.workspace.store.events() == before
        assert client.post(base + "/feedback", json=feedback(report), headers={"Origin": "https://elsewhere.invalid"}).status_code == 403
        assert client.post(base + "/feedback", json=feedback(report), headers={"X-Workspace-Id": "wrong"}).status_code == 412
        assert client.post(base + "/feedback", json=feedback(report)).status_code == 200
        assert client.post(base + "/feedback", json=feedback(report, source_hash="0" * 64)).status_code == 409
        packet = client.get(base + "/feedback/review_a/packet").json()
        assert packet["report"]["html"] == SOURCE
        assert client.get(base + "/export?format=review").status_code == 200
    restarted = create_app(tmp_path, start_workers=False)
    with TestClient(restarted) as client:
        assert client.get(base).json()["feedback"]["submission_id"] == "review_a"
        assert client.get("/api/reports/missing").status_code == 404


def fake_provider(monkeypatch):
    config = {"configured": True, "model": "fixture-writer", "provider": "codex", "billing_mode": "subscription",
              "pricing_known": False, "transport": "codex_exec"}
    monkeypatch.setattr("optimization_framework.research.providers.provider_status", lambda: config)
    return config


def test_writer_uses_review_priority_and_creates_linked_draft(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_workers=False)
    reports = app.state.reports
    report = reports.add(SOURCE, "Example", evidence={"seed_count": 3})
    review = reports.submit(report["id"], feedback(report))
    fake_provider(monkeypatch)
    calls = []
    def respond(adapter, role, system, content, *, result_type):
        calls.append(content)
        assert role == "technical_report_writer" and "Do not mechanically delete" in system
        assert content["feedback"]["id"] == review["id"]
        assert "<svg>" not in content["source_html"]
        return WriterResult(title="Revised", body_html='<section id="one"><h2>Findings</h2><p>A very strong phrase.</p><figure data-report-figure="figure-1"></figure></section>',
                            change_summary="Improved structure.", feedback_response={"m1": "Retained the phrase."})
    monkeypatch.setattr("optimization_framework.reports.writer.LLMAdapter.call_with_prompt", respond)
    writer = app.state.report_writer
    request = WriterRequest(submission_id="review_a", request_id="once", workflow="legacy")
    result = writer.start(report_id=report["id"], request=request, background=False)
    assert result["status"] == "completed"
    draft = reports.get(result["result_id"])
    assert draft["parent_id"] == report["id"] and "<svg>" in draft["html"]
    assert reports.get(report["id"])["html"] == SOURCE
    assert reports.latest(draft["id"]) is None
    assert writer.start(report_id=report["id"], request=request, background=False) == result
    assert len(calls) == 1


@pytest.mark.parametrize("body,responses,error", [
    ('<p>changed everything</p><figure data-report-figure="figure-1"></figure>', {"m1": "Rewritten"}, "green phrase"),
    ('<p>very strong phrase</p><figure data-report-figure="figure-1"></figure>', {}, "every annotation"),
    ('<script>alert(1)</script>', {"m1": "Kept"}, "unsupported HTML"),
    ('<p>very strong phrase</p>', {"m1": "Kept"}, "omitted a supplied figure"),
])
def test_writer_rejects_broken_handoffs_without_touching_original(tmp_path, monkeypatch, body, responses, error):
    app = create_app(tmp_path, start_workers=False)
    report = app.state.reports.add(SOURCE, "Example")
    app.state.reports.submit(report["id"], feedback(report))
    fake_provider(monkeypatch)
    monkeypatch.setattr("optimization_framework.reports.writer.LLMAdapter.call_with_prompt", lambda *args, **kwargs:
        WriterResult(title="Broken", body_html='<section id="one">' + body + '</section>', change_summary="Changed", feedback_response=responses))
    result = app.state.report_writer.start(report_id=report["id"], request=WriterRequest(submission_id="review_a", request_id="failed", workflow="legacy"), background=False)
    assert result["status"] == "failed" and error in result["error"]
    assert len(app.state.reports.listing()) == 1
    assert app.state.reports.get(report["id"])["html"] == SOURCE


def test_new_report_agent_and_restart_do_not_replay_calls(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_workers=False)
    fake_provider(monkeypatch)
    monkeypatch.setattr("optimization_framework.reports.writer.LLMAdapter.call_with_prompt", lambda *args, **kwargs:
        WriterResult(title="New report", body_html='<section id="summary"><h2>Findings</h2><p>Three seeds were tested.<br/>Further tests are needed.</p></section>', change_summary="Drafted from supplied evidence."))
    writer = app.state.report_writer
    job = writer.start(evidence={"seed_count": 3}, brief="Write a technical report.", background=False, workflow="legacy")
    assert job["status"] == "completed" and app.state.reports.get(job["result_id"])["parent_id"] is None
    interrupted = deepcopy(job) | {"id": "interrupted_report", "status": "running"}
    app.state.workspace.store.put("report_writer_job", interrupted)
    writer.recover()
    assert app.state.workspace.store.get("interrupted_report")["status"] == "interrupted"
