"""Import existing HTML, export a portable review, or run the report writer."""
import argparse
import json
from pathlib import Path

from optimization_framework.execution.service import Workspace
from optimization_framework.storage.sqlite import Store, identifier
from .document import clean_source, review_html
from .service import Reports
from .writer import ReportWriter, WriterRequest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, help="Workspace directory containing workspace.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("import")
    load.add_argument("--html", type=Path, required=True)
    load.add_argument("--title", required=True)
    load.add_argument("--campaign-id")
    load.add_argument("--evidence", type=Path)
    export = commands.add_parser("export")
    export.add_argument("--report-id", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--review", action="store_true")
    write = commands.add_parser("write")
    write.add_argument("--evidence", type=Path, required=True)
    write.add_argument("--brief", required=True)
    write.add_argument("--campaign-id")
    revise = commands.add_parser("revise")
    revise.add_argument("--report-id", required=True)
    revise.add_argument("--submission-id", required=True)
    revise.add_argument("--instruction", default="Revise the report using this submitted feedback.")
    args = parser.parse_args(argv)
    reports = Reports(Store(args.directory))
    if args.command == "import":
        result = reports.add(args.html.read_text(), args.title, campaign_id=args.campaign_id,
            evidence=json.loads(args.evidence.read_text()) if args.evidence else {})
        print(json.dumps({"id": result["id"], "url": "/reports/" + result["id"], "source_hash": result["source_hash"]}))
    elif args.command == "export":
        report = reports.get(args.report_id)
        args.output.write_text(review_html(report, reports.latest(report["id"])) if args.review else clean_source(report["html"]))
        print(args.output)
    else:
        workspace = Workspace(args.directory)
        writer = ReportWriter(workspace)
        if args.command == "write":
            job = writer.start(evidence=json.loads(args.evidence.read_text()), brief=args.brief,
                campaign_id=args.campaign_id, background=False)
        else:
            job = writer.start(report_id=args.report_id, request=WriterRequest(submission_id=args.submission_id,
                request_id=identifier("cli"), instruction=args.instruction), background=False)
        print(json.dumps({key: job.get(key) for key in ("id", "status", "result_id", "error", "usage")}, indent=2))
        if job["status"] != "completed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
