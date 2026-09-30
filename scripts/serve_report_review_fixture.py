"""Isolated HTTP fixture for the real portable report review client; no model calls."""
import argparse
from tempfile import TemporaryDirectory

import uvicorn

from optimization_framework.api.app import create_app


SOURCE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Review fixture</title>
<style>body{font:16px/1.5 system-ui}section{padding:40px;background:white;margin:20px}svg{width:180px;height:60px}</style></head><body>
<section class="page" id="one"><h2>Findings</h2><p>A 😀 <b>very strong phrase</b> and a weak claim.</p>
<p>Results remained descriptive across three seeds.</p><figure><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 60"><text x="10" y="30">A chart, unchanged</text></svg><figcaption>Measured results.</figcaption></figure></section>
<section class="page" id="two"><h2>Limitations</h2><p>Only three seeds were tested. Further evidence is needed.</p>
<table><thead><tr><th>Method</th><th>Efficiency</th></tr></thead><tbody><tr><td>Annealing</td><td>97.81%</td></tr></tbody></table></section></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8794)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="report-review-test-") as directory:
        app = create_app(directory, start_workers=False)

        @app.post("/_fixture/report")
        def add():
            report = app.state.reports.add(SOURCE, "Report review fixture")
            return {"id": report["id"], "source_hash": report["source_hash"]}

        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
