"""Isolated HTTP fixture for the real portable report review client; no model calls."""
import argparse
from tempfile import TemporaryDirectory

import uvicorn

from optimization_framework.api.app import create_app
from optimization_framework.contracts.requests import CampaignInput
from optimization_framework.reports.editorial import Brief, Draft, EvidenceRead, Investigation, Review, Selection
from optimization_framework.research.engine import LLMAdapter
from optimization_framework.research import providers


SOURCE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Review fixture</title>
<style>body{font:16px/1.5 system-ui}section{padding:40px;background:white;margin:20px}svg{width:180px;height:60px}</style></head><body>
<section class="page" id="one"><h2>Findings</h2><p>A 😀 <b>very strong phrase</b> and a weak claim.</p>
<p>Results remained descriptive across three seeds.</p><figure><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 60"><text x="10" y="30">A chart, unchanged</text></svg><figcaption>Measured results.</figcaption></figure></section>
<section class="page" id="two"><h2>Limitations</h2><p>Only three seeds were tested. Further evidence is needed.</p>
<table><thead><tr><th>Method</th><th>Efficiency</th></tr></thead><tbody><tr><td>Annealing</td><td>97.81%</td></tr></tbody></table></section></body></html>'''


def respond(adapter, role, system, content, *, result_type):
    """Deterministic model substitute confined to this temporary test server."""
    adapter.usage["calls"] += 1
    if result_type is Brief:
        return Brief(focus="Practical cost under limited evidence", explicit_guidance=[content["researcher_notes"]],
            inferred_intent=["Costs matter more than a broad ranking"], reader_and_depth="Expert reader; concise evidence and caveats",
            alternative_interpretations=[], question="Emphasize cost or learning dynamics?" if "ask me" in content["researcher_notes"] else None)
    if result_type is Investigation:
        if not content["read_receipts"]:
            return Investigation(coverage="Inspect the saved campaign", requests=[EvidenceRead(tool="evidence.read",
                record_id=next(row["id"] for row in content["inventory"]["entries"] if row["kind"] == "campaign"))])
        return Investigation(coverage="Exploratory evidence only", claims=[{"id": "scope", "statement": "The campaign is exploratory.",
            "kind": "observation", "support": [content["read_receipts"][0]["id"]], "reliability": "Recorded campaign intent",
            "limitations": "No general ranking", "interest": "Clarifies the scope", "novelty": "Not established"}],
            missing_evidence=["Matched-budget replication"], proposed_experiments=["Propose replication separately"])
    if result_type is Selection:
        return Selection(focus="Practical cost under limited evidence", argument="Keep the conclusion narrow",
            claims=[{"claim_id": "scope", "place": "main", "reason": "Scope matters to this question"}],
            outline=["Findings"], figure_reasons={key: "Unneeded for this scope" for key in content["figures"]})
    if result_type is Draft:
        return Draft(title="An exploratory comparison", body_html='<section id="findings"><h2>Findings</h2><p>The campaign is exploratory.</p></section>',
            change_summary="Focused the argument", claim_uses={"scope": "The campaign is exploratory."})
    if result_type is Review:
        return Review(assessment="The narrow conclusion fits this fixture.")
    raise ValueError("Unexpected fixture model request")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8794)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="report-review-test-") as directory:
        providers.provider_status = lambda: {"configured": True, "model": "fixture-writer", "provider": "codex", "billing_mode": "subscription",
            "pricing_known": False, "transport": "codex_exec", "reasoning_effort": "medium"}
        LLMAdapter.call_with_prompt = respond
        app = create_app(directory, start_workers=False)
        app.state.workspace.create_campaign(CampaignInput(name="Report writing fixture", objective="An exploratory comparison",
            llm_budget_usd=0, tasks=[{"name": "Development", "physics": {"n_cells": 6, "fourier_order": 1}}]))

        @app.post("/_fixture/report")
        def add():
            report = app.state.reports.add(SOURCE, "Report review fixture")
            return {"id": report["id"], "source_hash": report["source_hash"]}

        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
