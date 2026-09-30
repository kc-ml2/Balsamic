"""Report subpages and review APIs, mounted before the dashboard catch-all."""
import json
from typing import Literal

from fastapi.responses import HTMLResponse, Response

from optimization_framework.reports.document import clean_source, review_html
from optimization_framework.reports.service import Feedback, Reports
from optimization_framework.reports.writer import ReportWriter, WriterRequest


def install(app, workspace):
    reports, writer = Reports(workspace.store), ReportWriter(workspace)
    app.state.reports, app.state.report_writer = reports, writer

    @app.get("/api/reports")
    def listing(campaign_id: str | None = None):
        return {"reports": reports.listing(campaign_id)}

    @app.get("/api/reports/{report_id}")
    def detail(report_id: str):
        report = reports.get(report_id)
        return {key: value for key, value in report.items() if key not in {"html", "sections", "evidence"}} | {
            "feedback": reports.latest(report_id), "jobs": [public_job(job) for job in workspace.store.list("report_writer_job") if job["report_id"] == report_id]}

    @app.post("/api/reports/{report_id}/feedback")
    def submit(report_id: str, feedback: Feedback):
        return reports.submit(report_id, feedback)

    @app.get("/api/reports/{report_id}/feedback/{submission_id}/packet")
    def packet(report_id: str, submission_id: str):
        return Response(json.dumps(reports.packet(report_id, submission_id), ensure_ascii=False, indent=2),
            media_type="application/json", headers={"Content-Disposition": 'attachment; filename="report-review-packet.json"'})

    @app.post("/api/reports/{report_id}/revise")
    def revise(report_id: str, request: WriterRequest):
        return public_job(writer.start(report_id=report_id, request=request))

    @app.get("/api/report-writer/jobs/{job_id}")
    def job(job_id: str):
        return public_job(workspace.store.get(job_id, "report_writer_job"))

    @app.get("/api/reports/{report_id}/export")
    def export(report_id: str, format: Literal["html", "review"] = "html"):
        report = reports.get(report_id)
        content = clean_source(report["html"]) if format == "html" else review_html(report, reports.latest(report_id))
        return HTMLResponse(content, headers={"Content-Disposition": f'attachment; filename="{report_id}-{format}.html"'})

    @app.get("/reports/{report_id}", response_class=HTMLResponse)
    def page(report_id: str):
        return HTMLResponse(review_html(reports.get(report_id), reports.latest(report_id), workspace_id=workspace.store.identity()),
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"})


def public_job(job):
    return {key: value for key, value in job.items() if key not in {"input", "figures", "provider_snapshot"}}
