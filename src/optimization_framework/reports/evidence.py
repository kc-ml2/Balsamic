"""Frozen, scoped report evidence and bounded read/analysis tools. No experiment controls."""
from copy import deepcopy
from hashlib import sha256
from html import escape
from io import StringIO
import json
import math

from optimization_framework.contracts.base import content_hash
from optimization_framework.research.discovery.record_view import view_record
from optimization_framework.research.engine import _safe_context
from optimization_framework.storage.sqlite import now


SCALARS = ("step", "iteration", "elapsed_seconds", "wall_seconds", "solver_calls", "evaluations",
           "objective", "best_objective", "efficiency", "best_efficiency", "loss", "reward")
MAX_TRACE_BYTES = 16_000_000
MAX_TOTAL_TRACE_BYTES = 128_000_000


def freeze(workspace, job_id, campaign_id, supplied, references):
    """Snapshot eligible records plus bounded scalar journals before any model call.

    The database snapshot is transactional. Active journals are append-only prefixes,
    separately timestamped and hashed; no claim of an atomic campaign-wide checkpoint.
    """
    records, kinds, gaps = {}, {}, []
    if campaign_id:
        store = workspace.store
        campaign = store.get(campaign_id, "campaign")
        safe = _safe_context({"campaign": campaign, **{plural: store.list(kind, campaign_id)
            for plural, kind in (("tasks", "task"), ("trials", "trial"), ("hypotheses", "hypothesis"), ("decisions", "decision"))}})
        permitted = {row["id"] for group in ("tasks", "trials") for row in safe[group]}
        withheld = {row["id"] for kind in ("task", "trial") for row in store.list(kind, campaign_id)} - permitted
        def references_withheld(value):
            if isinstance(value, str):
                return any(key in value for key in withheld)
            if isinstance(value, dict):
                return any(references_withheld(item) for item in value.values())
            if isinstance(value, list):
                return any(references_withheld(item) for item in value)
            return False
        records[campaign_id], kinds[campaign_id] = safe["campaign"], "campaign"
        for plural, kind in (("tasks", "task"), ("trials", "trial"), ("hypotheses", "hypothesis"), ("decisions", "decision")):
            for row in safe[plural]:
                if kind in {"hypothesis", "decision"} and references_withheld(row):
                    gaps.append(f"One {kind} record referencing unreleased evidence was withheld.")
                    continue
                records[row["id"]], kinds[row["id"]] = row, kind
        for row in store.list("source", campaign_id):
            records[row["id"]], kinds[row["id"]] = row, "source"
        # Studies/decisions may reference embargoed trials. Reuse only records with
        # no locked scope and with every explicit task/trial reference admitted above.
        for row in store.list("study", campaign_id):
            linked = [row[key] for key in ("task_id", "trial_id") if row.get(key)]
            linked += row.get("task_ids", []) + row.get("trial_ids", [])
            if not row.get("locked") and all(key in records for key in linked) and not references_withheld(row):
                records[row["id"]], kinds[row["id"]] = row, "study"
        excluded = len(store.list("trial", campaign_id)) - len(safe["trials"])
        if excluded:
            gaps.append(f"{excluded} locked or unreleased non-development trials excluded; no results exposed.")
    if supplied:
        records["supplied-evidence"], kinds["supplied-evidence"] = deepcopy(supplied), "supplied"
    if any(key not in records for key in references):
        raise ValueError("A referenced record is unavailable in this campaign's permitted evidence")
    if len(records) > 3000 or len(json.dumps(records).encode()) > 20_000_000:
        raise ValueError("Campaign evidence exceeds the report snapshot limit; use a scoped evidence file through the report CLI")
    trials = [key for key in records if kinds[key] == "trial"]
    chosen = list(dict.fromkeys([key for key in references if key in trials] + list(reversed(trials))))[:32]
    remaining = MAX_TOTAL_TRACE_BYTES
    for trial_id in chosen:
        curve = freeze_curve(workspace.job_dir(trial_id) / "metrics.jsonl", min(MAX_TRACE_BYTES, MAX_TOTAL_TRACE_BYTES // max(1, len(chosen)), remaining))
        remaining -= curve["scanned_bytes"]
        records["curve:" + trial_id], kinds["curve:" + trial_id] = curve, "curve"
        if curve["coverage"] != "complete":
            gaps.append(f"Trace for {trial_id}: {curve['coverage']}.")
    if len(chosen) < len(trials):
        gaps.append(f"Scalar traces frozen for {len(chosen)} of {len(trials)} trials (references first, then most recent). All eligible trial records remain available.")
    inventory = [{"id": key, "kind": kinds[key], "summary": summary(row), "sha256": content_hash(row)} for key, row in records.items()]
    records["inventory"] = inventory
    snapshot = {"id": "report_evidence_" + job_id, "campaign_id": campaign_id, "created_at": now(),
        "records": records, "kinds": kinds, "gaps": gaps, "references": references,
        "basis": "Saved record snapshot with separately captured metric journal prefixes; not a complete or balanced experimental design."}
    return workspace.store.put_immutable("report_evidence_snapshot", snapshot)


def summary(row):
    if isinstance(row, dict):
        keys = ("name", "title", "objective", "status", "algorithm", "task_id", "seed", "max_steps", "wall_seconds",
                "progress", "result", "reason", "coverage", "row_count", "fields")
        text = json.dumps({key: row[key] for key in keys if key in row}, ensure_ascii=False)
        if text == "{}":
            text = "Fields: " + ", ".join(row.keys())
    else:
        text = str(row)
    return text[:700] + (" [summary truncated; read the record]" if len(text) > 700 else "")


def freeze_curve(path, byte_limit):
    rows, fields, invalid, scanned, windows = [], set(), 0, 0, []
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return {"rows": [], "row_count": 0, "fields": [], "coverage": "missing", "scanned_bytes": 0, "captured_at": now()}
    with stream:
        size = stream.seek(0, 2)
        sampled = size > byte_limit
        count = 16 if byte_limit >= 65536 else 2
        width = max(0, byte_limit // count) if sampled else size
        starts = [int((size - width) * i / (count - 1)) for i in range(count)] if sampled else [0]
        for start in starts:
            if not width:
                break
            stream.seek(start)
            data = stream.read(width)
            scanned += len(data)
            windows.append({"offset": start, "bytes": len(data), "sha256": sha256(data).hexdigest()})
            lines = data.split(b"\n")
            # Window boundaries can split JSON records. Keep complete lines only.
            if start:
                lines.pop(0)
            tail = lines.pop() if lines else b""
            if tail and start + width >= size:
                invalid += 1
            for raw in lines:
                if len(rows) >= 20000:
                    sampled = True
                    break
                try:
                    item = json.loads(raw)
                    if not isinstance(item, dict):
                        raise ValueError("Journal row must be an object")
                    row = {key: item[key] for key in SCALARS if type(item.get(key)) in (float, int) and math.isfinite(item[key])}
                    if isinstance(item.get("attempt_id"), str):
                        row["attempt_id"] = item["attempt_id"]
                    rows.append(row)
                    fields.update(row)
                except (ValueError, TypeError):
                    invalid += 1
    return {"rows": rows, "row_count": len(rows), "fields": sorted(fields), "captured_at": now(),
        "scanned_bytes": scanned, "source_bytes_at_open": size, "windows": windows,
        "invalid_rows": invalid, "coverage": "sampled" if sampled else "complete" if scanned == size and not invalid else "partial",
        "sampling": "Bounded byte windows spanning the saved file, including its beginning and tail. Not uniform step/time sampling; gaps and unobserved counter resets are possible." if sampled else "Complete saved prefix at capture time; active runs may continue."}


class EvidenceTools:
    def __init__(self, workspace, job, figures):
        self.workspace, self.store, self.job, self.figures = workspace, workspace.store, job, figures
        self.snapshot = self.store.get(job["snapshot_id"], "report_evidence_snapshot")
        self.receipts = [self.store.get(key, "report_evidence_receipt") for key in job.get("receipt_ids", [])]

    def inventory(self):
        entries = self.snapshot["records"]["inventory"]
        pinned = self.snapshot["references"]
        selected = sorted(entries, key=lambda row: row["id"] not in pinned)[:70]
        return {"snapshot_id": self.snapshot["id"], "basis": self.snapshot["basis"], "gaps": self.snapshot["gaps"],
            "total_records": len(entries), "entries": selected,
            "more": "Read record_id=inventory with offset/limit for remaining entries." if len(selected) < len(entries) else None}

    def execute(self, request):
        from .editorial import LIMITS
        if len(self.receipts) >= LIMITS["tool_calls"]:
            raise ValueError("Report investigation tool limit reached")
        spec = request.model_dump()
        identity = f"{self.job['id']}_read_{len(self.receipts) + 1}"
        receipt = {"id": identity, "job_id": self.job["id"], "campaign_id": self.job["campaign_id"], "request": spec, "created_at": now()}
        try:
            if request.tool == "evidence.read":
                if request.record_id not in self.snapshot["records"]:
                    raise ValueError("Record is outside the frozen report snapshot")
                result = view_record(self.snapshot["records"][request.record_id], record_id=request.record_id,
                    pointer=request.pointer, offset=request.offset, limit=request.limit, max_bytes=12000)
            elif request.tool == "analysis.compare":
                result = self.compare(request, identity)
            elif request.tool.startswith("source."):
                if not self.job.get("literature") or not self.job["campaign_id"]:
                    raise ValueError("Literature tools require a campaign and enabled literature checks")
                if sum(r["request"]["tool"].startswith("source.") for r in self.receipts) >= LIMITS["literature_calls"]:
                    raise ValueError("Literature check limit reached; novelty may remain unestablished")
                result = self.literature(request, identity)
            else:
                raise ValueError("Unknown report tool")
            receipt.update(status="completed", result=result)
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            receipt.update(status="failed", error=str(error)[:1000])
        saved = self.store.put_immutable("report_evidence_receipt", receipt)
        self.receipts.append(saved)
        with self.store.transaction():
            job = self.store.get(self.job["id"], "report_writer_job")
            job.update(receipt_ids=[r["id"] for r in self.receipts], figures=self.figures)
            self.store.put("report_writer_job", job)
        return saved

    def literature(self, request, identity):
        if request.tool == "source.search":
            from optimization_framework.research.sources import deliver
            receipt = deliver(self.workspace, {"id": identity, "campaign_id": self.job["campaign_id"],
                "kind": "literature_search", "query": request.query, "provider": request.provider, "limit": request.limit})
            return {"sources": receipt["result"]["sources"], "retrieval_id": receipt["id"],
                "coverage": "Search metadata only; read primary source passages before using scientific claims."}
        from optimization_framework.research.literature import LiteratureReader
        allowed = {key for key, kind in self.snapshot["kinds"].items() if kind == "source"}
        for receipt in self.receipts:
            allowed.update(row["id"] for row in receipt.get("result", {}).get("sources", []))
        if request.source_id not in allowed:
            raise ValueError("Read a source from this snapshot or this job's search receipts")
        result = LiteratureReader(self.workspace).read(self.job["campaign_id"], source_id=request.source_id,
            query=request.query, offset=request.offset, limit=request.limit)
        # A capture can contain thousands of passage IDs. Preserve provenance and
        # the passages actually read, without sending the entire navigation list.
        capture = dict(result["capture"])
        capture["passage_count"] = len(capture.pop("passage_ids", []))
        source = self.store.get(request.source_id, "source")
        return {**result, "capture": capture, "source": {key: source[key] for key in ("id", "title", "url", "doi") if key in source}}

    def compare(self, request, identity):
        if request.metric not in SCALARS or request.axis not in SCALARS or request.axis == request.metric:
            raise ValueError("Select distinct scalar fields present in the frozen journals")
        if len(set(request.trial_ids)) != len(request.trial_ids):
            raise ValueError("Repeated trials are not independent replicates")
        curves, trials = {}, []
        for key in request.trial_ids:
            trial = self.snapshot["records"].get(key)
            if self.snapshot["kinds"].get(key) != "trial":
                raise ValueError("Analysis requires trial IDs in the frozen snapshot")
            curve = self.snapshot["records"].get("curve:" + key, {})
            points = []
            for row in curve.get("rows", []):
                if request.axis in row and request.metric in row:
                    point = (row[request.axis], row[request.metric])
                    if not points or point != points[-1]:
                        points.append(point)  # Identical terminal status rows aren't new observations.
            if curve.get("coverage") not in {"complete", "sampled"} or not points:
                raise ValueError(f"{key} has no usable frozen {request.metric}/{request.axis} samples; inspect the record and report the coverage gap")
            if len({row["attempt_id"] for row in curve["rows"] if row.get("attempt_id")}) > 1:
                raise ValueError(f"{key} contains multiple execution attempts; inspect them separately")
            if any(a[0] >= b[0] for a, b in zip(points, points[1:])):
                raise ValueError(f"{key} has repeated/reset counters; compare separate attempts manually instead of joining them")
            curves[key] = points
            trials.append(trial)
        horizon = request.horizon if request.horizon is not None else min(points[-1][0] for points in curves.values())
        if any(points[0][0] > horizon or points[-1][0] < horizon for points in curves.values()):
            raise ValueError("The requested horizon is outside at least one recorded trace; no extrapolation performed")
        comparisons = []
        for trial in trials:
            points = curves[trial["id"]]
            measured = next(point for point in reversed(points) if point[0] <= horizon)
            comparisons.append({"trial_id": trial["id"], "algorithm": trial.get("algorithm"), "task_id": trial.get("task_id"),
                "seed": trial.get("seed"), "status": trial.get("status"), "max_steps": trial.get("max_steps"),
                "wall_seconds": trial.get("wall_seconds"), "algorithm_config": trial.get("algorithm_config"),
                "recorded_axis": measured[0], "value": measured[1], "final_recorded_axis": points[-1][0],
                "configuration_hash": content_hash({key: trial.get(key) for key in ("task_id", "physics", "recipe", "algorithm_config")})})
            curve = self.snapshot["records"]["curve:" + trial["id"]]
            comparisons[-1].update(trace_coverage=curve["coverage"], sampling=curve["sampling"],
                horizon_gap=horizon - measured[0], snapshot_point_count=len(points))
        result = {"metric": request.metric, "axis": request.axis, "requested_horizon": horizon, "comparisons": comparisons,
            "method": "Last captured observation at or before a shared horizon, with each actual coordinate and gap reported; no interpolation, extrapolation or pooled significance test.",
            "limitations": "Matching an axis does not equalize tuning, total cost, task conditions or seed selection. Inspect configurations and recorded rationale. Sampled journals may miss intermediate events or resets; they cannot establish convergence or full-trajectory statistics. Live traces are prefixes, not converged outcomes."}
        if request.plot:
            # Plot the exact frozen samples using a standard plotting library. A
            # missing optional plotting dependency doesn't erase numeric results.
            try:
                from matplotlib.figure import Figure
                figure = Figure(figsize=(8, 4), layout="constrained")
                axes = figure.subplots()
                for trial in trials:
                    points = [point for point in curves[trial["id"]] if point[0] <= horizon]
                    axes.scatter([p[0] for p in points], [p[1] for p in points], label=trial["id"], s=5)
                axes.set(xlabel=request.axis, ylabel=request.metric)
                axes.legend(fontsize=6)
                stream = StringIO()
                figure.savefig(stream, format="svg", metadata={"Date": None})
                svg = stream.getvalue()[stream.getvalue().index("<svg"):]
                key = f"analysis-{len(self.figures) + 1}"
                caption = f"Captured {request.metric} observations against {request.axis}, through {horizon:g}. Large journals are sampled in byte windows; gaps are not interpolated. Allocation and configuration may differ."
                self.figures[key] = "<figure>" + svg + "<figcaption>" + escape(caption) + "</figcaption></figure>"
                result.update(figure_id=key, figure_caption=caption, receipt_id=identity)
            except ImportError:
                result["plot_unavailable"] = "Install the project's meent extra for matplotlib; numeric analysis is available."
        return result
