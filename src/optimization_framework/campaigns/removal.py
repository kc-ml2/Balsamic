"""Removing a campaign from the workspace, and what stays on disk afterwards.

The researcher chooses what is kept:

    nothing   every record and file of the campaign is deleted;
    summary   a summary of the runs is written first (summary.md and summary.json);
    records   the summary, the campaign's records without their bulky values, the best design of each
              run, the score journals, the conversation and the campaign memory.

What is kept is written and synced before anything is deleted. The campaign's rows are deleted in
one transaction together with a removal record; the files are cleared after it, and a service that
stopped in between finishes the clearing when it starts again.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import threading

from optimization_framework.storage.sqlite import now

KEEP = ("nothing", "summary", "records")
# Work that still writes records. A campaign is removed only when none of it is left. A request to the
# campaign manager is not in this list: it keeps the status "dispatched" for good once its run exists, and
# one that is still queued does nothing by itself. Its run, while it lasts, is a research_run.
IN_PROGRESS = {   # kind: (statuses, what one of them is doing, what several are doing, what to do about it)
    "trial": ({"queued", "running", "pausing", "stopping"}, "experiment is queued or running", "experiments are queued or running",
              "Stop or finish every experiment first."),
    "agent_run": ({"queued", "submitted", "running"}, "agent turn is queued or running", "agent turns are queued or running",
                  "Stop the agent team, or wait for the turn to end."),
    "fixed_mask_job": ({"queued", "starting", "running"}, "fixed-design evaluation is running", "fixed-design evaluations are running", "Wait until this is finished."),
    "runtime_profile": ({"running"}, "runtime profile is running", "runtime profiles are running", "Wait until this is finished."),
    "journal_compaction": ({"queued", "running"}, "journal clean-up is running", "journal clean-ups are running", "Wait until this is finished."),
    "implementation_grant": ({"reserved", "queued", "submitted", "running", "building", "validating", "reviewing", "repairing", "stopping"},
                             "implementation job is in progress", "implementation jobs are in progress", "Cancel or finish this first."),
    "research_run": ({"running", "stopping"}, "campaign-manager run is in progress", "campaign-manager runs are in progress", "Stop or finish this first."),
    "study_execution": ({"running", "closing"}, "study execution is running", "study executions are running", "Wait until this is finished."),
    "adaptive_race": ({"preflight", "running"}, "race is running", "races are running", "Stop or finish this first."),
    "bundle_operation": ({"queued", "capturing", "inspecting", "publishing"}, "evidence bundle is being prepared", "evidence bundles are being prepared",
                         "Wait until this is finished."),
    "report_writer_job": ({"running", "awaiting_focus"}, "report is being written", "reports are being written", "Cancel or finish this first."),
    "development_workspace": ({"queued", "starting", "running", "resuming", "pausing", "stopping"}, "development workspace is active",
                              "development workspaces are active", "Stop this first."),
    "formulation_run": ({"running"}, "run of the formulation notebook is in progress", "runs of the formulation notebook are in progress", "Wait until this is finished."),
    "outbox": ({"pending"}, "background step is not delivered yet", "background steps are not delivered yet", "Try again in a moment."),
}
# Left out of the kept records: one row per evaluation, and what the agents' tools sent and received.
BULK_KINDS = ("cost_event", "agent_tool_intent", "agent_tool_receipt")
LIST_LIMIT = 16384          # a longer list inside a kept record is replaced by a note of its size
WORK_FILE_LIMIT = 5 * 1024**2
KEPT_TRIAL_FILES = ("metrics.jsonl", "worker.log", "error.txt")
CURVE_FIELDS = ("best_objective", "elapsed_seconds", "evaluations", "solver_calls", "unknown_worker_cost", "unknown_solver_cost")
SAFE = re.compile(r"[A-Za-z0-9_.-]{1,200}")


def tree_bytes(path):
    """Bytes of a file or of everything under a directory; links are counted, not followed."""
    path = Path(path)
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        total = 0
        with os.scandir(path) as entries:
            for entry in entries:
                total += tree_bytes(entry.path) if entry.is_dir(follow_symlinks=False) else entry.stat(follow_symlinks=False).st_size
        return total
    except OSError:
        return 0


def trimmed(value):
    """The same value, each list longer than LIST_LIMIT bytes replaced by a note of what was left out."""
    if isinstance(value, dict):
        return {key: trimmed(item) for key, item in value.items()}
    if isinstance(value, list):
        size = len(json.dumps(value, allow_nan=True))
        if size > LIST_LIMIT:
            return {"left_out": f"a list of {len(value)} entries, {size} bytes"}
        return [trimmed(item) for item in value]
    return value


def curve(rows):
    """[evaluations, best score, seconds] at the first evaluation, at each improvement and at the end."""
    points = [(row.get("evaluations"), row.get("best_objective"), row.get("elapsed_seconds")) for row in rows
              if isinstance(row.get("best_objective"), (int, float))]
    last = len(points) - 1
    return [list(point) for index, point in enumerate(points)
            if index in (0, last) or point[1] != points[index - 1][1]]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=1)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
        stream.flush()
        os.fsync(stream.fileno())
    return count


def amount(value, digits=4):
    return "" if value is None else f"{value:.{digits}g}" if isinstance(value, float) else str(value)


def duration(seconds):
    if not isinstance(seconds, (int, float)):
        return ""
    return f"{seconds / 3600:.1f} h" if seconds >= 3600 else f"{seconds / 60:.1f} min" if seconds >= 60 else f"{seconds:.0f} s"


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def counted(count, one, many=None):
    return f"{count} {one if count == 1 else many or one + 's'}"


class CampaignRemoval:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        self.directory = workspace.directory / "removed"
        self.one_at_a_time = threading.Lock()

    # ---- what stands in the way -------------------------------------------------------------

    def blockers(self, campaign_id, *, reach=True):
        found = []
        with self.store.connection() as db:
            for kind, (statuses, one, many, advice) in IN_PROGRESS.items():
                if kind == "trial":
                    # Through the status index: reading the status out of every experiment record, megabytes each, takes seconds.
                    count = len(self.store.list_trials_in_status(sorted(statuses), campaign_id))
                else:
                    marks = ",".join("?" for _ in statuses)
                    count = db.execute(f"SELECT COUNT(*) FROM records WHERE kind=? AND campaign_id=? AND json_extract(data,'$.status') IN ({marks})",
                                       (kind, campaign_id, *sorted(statuses))).fetchone()[0]
                if count:
                    found.append(f"{count} {one if count == 1 else many}. {advice}")
            # Evidence another campaign rests on: its lead reads these findings, or its runs start from these results.
            users = db.execute("""SELECT r.campaign_id, COUNT(*) FROM records r
                WHERE r.kind IN ('reuse_decision','imported_asset') AND r.campaign_id<>?
                  AND COALESCE(json_extract(r.data,'$.decision'),'reuse')<>'decline'
                  AND json_extract(r.data,'$.asset_id') IN (SELECT id FROM records WHERE kind='asset' AND campaign_id=?)
                GROUP BY r.campaign_id""", (campaign_id, campaign_id)).fetchall()
        capturing = sum(trial["campaign_id"] == campaign_id for trial in self.store.trials_requiring_capture())
        if capturing:
            found.append(f"{capturing} finished experiment{' is' if capturing == 1 else 's are'} still being catalogued. Try again in a moment.")
        for user, count in users:
            try:
                name = self.store.get(user, "campaign")["name"]
            except KeyError:
                name = user
            found.append(f"Campaign “{name}” uses {count} {'finding or result' if count == 1 else 'findings or results'} of this campaign. Remove that campaign first.")
        if reach and self.store.list("agent_session", campaign_id):
            try:
                self.workspace.pi.client.status()
            except ValueError:
                found.append("The agent service is not reachable, so the agents' saved sessions cannot be removed. Start it and try again.")
        return found

    # ---- what the campaign holds --------------------------------------------------------------

    def _identities(self, campaign_id, kind):
        with self.store.connection() as db:
            return [row[0] for row in db.execute("SELECT id FROM records WHERE kind=? AND campaign_id=? ORDER BY rowid", (kind, campaign_id))]

    def paths(self, campaign_id):
        """Every directory that holds only this campaign's files."""
        from optimization_framework.analysis.tensorboard_view import _component
        root = self.workspace.directory
        found = [root / "campaigns" / campaign_id, root / "tensorboard" / "generated" / _component(campaign_id)]
        for kind, parents in (("trial", ("trials", "execution-hosts")), ("agent_session", ("agents",)), ("fixed_mask_job", ("fixed-masks",)),
                              ("adaptive_race", ("races",)), ("technical_report", ("reports",)), ("development_workspace", ("development",))):
            for identity in self._identities(campaign_id, kind):
                found.extend(root / parent / identity for parent in parents)
        # Identities come from records, yet nothing outside the workspace's own folders may ever be cleared:
        # a path that is not plainly one of them is left where it is.
        return [path for path in found if len(path.relative_to(root).parts) >= 2
                and all(SAFE.fullmatch(part) and part not in {".", ".."} for part in path.relative_to(root).parts)]

    def _blob_digests(self, campaign_id):
        """Content hashes of the stored files this campaign's results and bundles point to."""
        digests = set()
        with self.store.connection() as db:
            for (raw,) in db.execute("SELECT json_extract(data,'$.artifacts') FROM records WHERE kind='asset' AND campaign_id=?", (campaign_id,)):
                digests.update(item.get("sha256") for item in json.loads(raw or "[]") if isinstance(item, dict))
            for (digest,) in db.execute("SELECT json_extract(data,'$.archive.sha256') FROM records WHERE kind='bundle_operation' AND campaign_id=?", (campaign_id,)):
                digests.add(digest)
        return sorted(digest for digest in digests if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest))

    def plan(self, campaign_id):
        """What removing this campaign deletes, what each choice keeps, and what stands in the way."""
        campaign = self.store.get(campaign_id, "campaign")
        with self.store.connection() as db:
            kinds = {kind: {"count": count, "bytes": size} for kind, count, size in db.execute(
                "SELECT kind, COUNT(*), SUM(octet_length(data)) FROM records WHERE campaign_id=? GROUP BY kind", (campaign_id,))}
            events = db.execute("SELECT COUNT(*) FROM events WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
        root = self.workspace.directory
        trials = self._identities(campaign_id, "trial")
        files = {"experiments": 0, "scores": 0, "other": 0}
        for path in self.paths(campaign_id):
            files["experiments" if path.parent.name in {"trials", "execution-hosts"} else "other"] += tree_bytes(path)
        for identity in trials:
            files["scores"] += sum(tree_bytes(root / "trials" / identity / name) for name in KEPT_TRIAL_FILES)
        count = lambda kind: kinds.get(kind, {}).get("count", 0)
        findings = sum(note.get("kind") in {"finding", "counterevidence"} for note in self.store.list("manager_note", campaign_id))
        return {"campaign": {key: campaign.get(key) for key in ("id", "name", "created_at")},
            "holds": {"experiments": count("trial"), "hypotheses": count("hypothesis"), "findings": findings, "messages": count("message"),
                      "agents": count("agent_session"), "records": sum(item["count"] for item in kinds.values()) + 1, "events": events},
            "bytes": {"files": files["experiments"] + files["other"], "experiment_files": files["experiments"],
                      "database": sum(item["bytes"] or 0 for item in kinds.values())},
            "blockers": self.blockers(campaign_id),
            "options": [
                {"keep": "nothing", "kept_bytes": 0},
                {"keep": "summary", "kept_bytes": None},
                {"keep": "records", "kept_bytes": files["scores"], "left_out": {kind: kinds[kind]["count"] for kind in BULK_KINDS if kind in kinds}}],
            "folder": str(self.directory / campaign_id)}

    # ---- the summary of runs --------------------------------------------------------------------

    def summary(self, campaign_id, keep):
        """Each run's method, parameters, budget and result, with the campaign's ideas, findings and costs.

        Generated from the records and from the score journals, which are read here for the last time:
        a run's course is kept as the evaluations at which its best score improved.
        """
        from optimization_framework.agents import usage
        store = self.store
        campaign = store.get(campaign_id, "campaign")
        hypotheses = {item["id"]: item for item in store.list("hypothesis", campaign_id)}
        probes = {identity for profile in store.list("runtime_profile", campaign_id) for level in profile.get("levels", [])
                  for identity in level.get("trial_ids", [])}
        replays = {item.get("replay_trial_id") for item in store.list("evaluator_equivalence", campaign_id)}
        runs = []
        for trial in store.list_compact_trials(campaign_id):
            latest = trial.get("result") or trial.get("progress") or {}
            purpose = ("runtime probe" if trial["id"] in probes else "equivalence replay" if trial["id"] in replays
                       else "validation" if trial.get("recipe") or trial.get("algorithm") == "validate" or trial.get("diagnostic_grant_id") else "optimization")
            try:
                course = curve(self.workspace.metrics(trial["id"], fields=CURVE_FIELDS))
            except (OSError, ValueError, KeyError):
                course = []
            hypothesis = hypotheses.get(trial.get("hypothesis_id")) or {}
            runs.append({"id": trial["id"], "purpose": purpose, "hypothesis_id": trial.get("hypothesis_id"), "hypothesis": hypothesis.get("title"),
                "task": trial.get("task_name"), "method": trial.get("algorithm"), "parameters": trimmed(trial.get("algorithm_config") or {}),
                "seed": trial.get("seed"), "status": trial.get("status"), "stopped_by": latest.get("reason") or trial.get("reason"),
                "continues": (trial.get("continues") or {}).get("trial_id"), "question": trial.get("question"),
                "allowed": {"evaluations": trial.get("max_steps"), "seconds": trial.get("wall_seconds")},
                "evaluations": latest.get("evaluations"), "solver_calls": latest.get("solver_calls"),
                "seconds": latest.get("elapsed_seconds", trial.get("execution_seconds")),
                "objective": (latest.get("objective_definition") or {}).get("name"),
                "direction": (latest.get("objective_definition") or {}).get("direction"),
                "best": latest.get("best_objective"), "best_objectives": latest.get("best_objectives"),
                "created_at": trial.get("created_at"), "finished_at": trial.get("finished_at"),
                "course": course, "course_columns": ["evaluations", "best", "seconds"]})
        spending = usage.summary(store, campaign_id, bucket="day")
        assessment = self.workspace.resources.assessment(campaign_id)
        return {"schema_version": 1, "kept": keep, "removed_at": now(),
            "campaign": {key: campaign.get(key) for key in ("id", "name", "objective", "created_at", "updated_at", "version", "autonomy",
                "compute_budget_seconds", "llm_budget_usd", "delegated_trial_seconds", "validation_reserve_seconds")},
            "tasks": [{key: trimmed(task.get(key)) for key in ("id", "name", "problem_id", "configuration", "fidelity", "split")}
                      for task in store.list("task", campaign_id)],
            "hypotheses": [{key: trimmed(item.get(key)) for key in ("id", "title", "mechanism", "rationale", "assumptions", "risks", "sources",
                "algorithm", "algorithm_config", "status", "origin", "parent_ids", "created_at")} for item in hypotheses.values()],
            "runs": runs,
            "findings": [{key: note.get(key) for key in ("id", "kind", "content", "interpretation", "problem_scope", "limitations",
                "counterevidence_for", "author", "created_at")} for note in store.list("manager_note", campaign_id)
                if note.get("kind") in {"finding", "counterevidence"}],
            "runtime_profiles": [{key: trimmed(profile.get(key)) for key in ("id", "task_id", "status", "ceiling", "parameters", "levels", "summary",
                "created_at", "finished_at")} for profile in store.list("runtime_profile", campaign_id)],
            "decisions": [{key: decision.get(key) for key in ("id", "title", "status", "choice", "comment", "requested_by", "created_at", "resolved_at")}
                          for decision in store.list("decision", campaign_id)],
            "documents": [{key: item.get(key) for key in ("name", "bytes", "pages", "added_at")}
                          for item in store.list("campaign_material", campaign_id) if not item.get("removed")],
            "usage": {"compute_seconds": assessment.get("actual_seconds"), "compute_allocated_seconds": assessment.get("allocated_seconds"),
                      "model_calls": spending["totals"], "model_calls_by_model": spending["by_model"]}}

    @staticmethod
    def markdown(summary):
        campaign, runs = summary["campaign"], summary["runs"]
        lines = [f"# {campaign['name']}", "",
            f"Campaign `{campaign['id']}`, created {str(campaign.get('created_at'))[:10]}, removed from the workspace {summary['removed_at'][:10]}.",
            {"summary": "Only this summary was kept; the records, journals and designs were deleted.",
             "records": "Kept beside this file: the campaign's records (`records/`), each run's best design (`best-designs.jsonl`), "
                        "the score journals (`experiments/`), the campaign memory (`memory/`) and the agents' work files (`agent-work/`). "
                        "The evaluated designs, checkpoints, per-evaluation accounting and the agents' tool traces were deleted."}[summary["kept"]],
            "", "## Objective", "", campaign.get("objective") or "", "", "## Problem", ""]
        for task in summary["tasks"]:
            lines.append(f"- {task['name']} (`{task['problem_id']}`), fidelity {json.dumps(task.get('fidelity'))}")
        if not summary["tasks"]:
            lines.append("No problem was defined. The formulation notebook, if one was written, is kept beside this file as `formulation.md`.")
        if summary.get("documents"):
            lines += ["", "## Documents", "", *(f"- {item['name']}" for item in summary["documents"]),
                      "", {"summary": "Their files were deleted.", "records": "Kept beside this file, in `documents/`."}[summary["kept"]]]
        usage = summary["usage"]
        calls = usage["model_calls"]
        lines += ["", "## Cost", "",
            f"- Compute: {duration(usage.get('compute_seconds') or 0)} used of {duration(campaign.get('compute_budget_seconds'))} allowed.",
            f"- Model calls: {calls.get('calls', 0)}, ${calls.get('charged_usd') or 0:.2f} charged of ${campaign.get('llm_budget_usd') or 0:.2f} allowed."]
        optimizations = [run for run in runs if run["purpose"] == "optimization"]
        others = [run for run in runs if run["purpose"] != "optimization"]
        lines += ["", f"## Runs ({counted(len(optimizations), 'optimization run')}, {counted(len(others), 'other experiment')})", ""]
        if optimizations:
            lines += ["Best is the best value of the objective the run reached"
                      + (f" ({optimizations[0]['objective']}, {optimizations[0]['direction']})." if optimizations[0].get("objective") else "."),
                      "", "| Run | Idea | Method | Parameters | Seed | Status | Evaluations | Time | Best |", "|---|---|---|---|---|---|---|---|---|"]
            for run in optimizations:
                continued = f" (continues `{run['continues']}`)" if run.get("continues") else ""
                lines.append("| " + " | ".join(cell(value) for value in (f"`{run['id']}`{continued}", run.get("hypothesis") or "", run.get("method") or "",
                    json.dumps(run.get("parameters"), ensure_ascii=False), amount(run.get("seed")),
                    f"{run.get('status')}" + (f" ({run['stopped_by']})" if run.get("stopped_by") and run["stopped_by"] != run.get("status") else ""),
                    amount(run.get("evaluations")), duration(run.get("seconds")), amount(run.get("best"), 5))) + " |")
            lines += ["", "The course of each run (the evaluations at which its best score improved) is in `summary.json`, under `runs[].course`."]
        if others:
            lines += ["", "Other experiments:", ""]
            for purpose in sorted({run["purpose"] for run in others}):
                group = [run for run in others if run["purpose"] == purpose]
                lines.append(f"- {counted(len(group), purpose + ' experiment')}: " + ", ".join(f"`{run['id']}`" for run in group[:40]) + (" …" if len(group) > 40 else ""))
        lines += ["", f"## Ideas ({len(summary['hypotheses'])})", ""]
        for item in summary["hypotheses"]:
            own = [run["best"] for run in optimizations if run["hypothesis_id"] == item["id"] and isinstance(run.get("best"), (int, float))]
            maximize = not optimizations or optimizations[0].get("direction") != "minimize"
            reached = f" Best of its {counted(len(own), 'run')}: {amount((max if maximize else min)(own), 5)}." if own else " No run."
            lines += [f"### {item['title']}", "", f"Status {item.get('status')}, from {item.get('origin')}. Method `{item.get('algorithm')}` "
                      f"with {json.dumps(item.get('algorithm_config'), ensure_ascii=False)}.{reached}", "", item.get("mechanism") or "", ""]
        lines += [f"## Findings ({len(summary['findings'])})", ""]
        for note in summary["findings"]:
            lines += [f"### {note['id']}", "", f"{note.get('interpretation')}, recorded by {note.get('author')} on {str(note.get('created_at'))[:10]}.", "",
                      note.get("content") or "", ""]
            if note.get("problem_scope"):
                lines += [f"Scope: {note['problem_scope']}", ""]
            limits = note.get("limitations")
            for limit in limits if isinstance(limits, list) else [limits] if limits else []:
                lines.append(f"- Limitation: {limit}")
            lines.append("")
        if summary["runtime_profiles"]:
            lines += ["## Measured speed", ""]
            for profile in summary["runtime_profiles"]:
                levels = "; ".join(f"{level.get('workers')} worker(s): {amount(level.get('evaluations_per_second'), 3)} evaluations/s"
                                   for level in profile.get("levels") or [] if isinstance(level, dict) and level.get("evaluations_per_second"))
                lines.append(f"- `{profile['id']}` ({profile.get('status')}): {levels or 'no measured level'}")
            lines.append("")
        return "\n".join(lines) + "\n"

    # ---- writing what is kept ---------------------------------------------------------------------

    def _folder(self, campaign_id):
        self.directory.mkdir(parents=True, exist_ok=True)
        folder, number = self.directory / campaign_id, 1
        while folder.exists():   # a later campaign that took the identity of a removed one
            number += 1
            folder = self.directory / f"{campaign_id}-{number}"
        return folder

    def keep(self, campaign_id, keep):
        """Write what stays to its folder and return (folder, bytes). Nothing has been deleted yet."""
        folder = self._folder(campaign_id)
        partial = folder.with_name("." + folder.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(parents=True)
        try:
            summary = self.summary(campaign_id, keep)
            write_json(partial / "summary.json", summary)
            (partial / "summary.md").write_text(self.markdown(summary), encoding="utf-8")
            if self.workspace.formulation.summary(campaign_id):
                # The formulation is what the campaign wrote, not a measurement of it: it stays with either choice.
                (partial / "formulation.md").write_text(self.workspace.formulation.export_markdown(campaign_id), encoding="utf-8")
            if keep == "records":
                self._keep_records(campaign_id, partial)
            for path in partial.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
            os.replace(partial, folder)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        from optimization_framework.storage.artifacts import sync_directory
        sync_directory(self.directory)
        return folder, tree_bytes(folder)

    def _keep_records(self, campaign_id, folder):
        store, root = self.store, self.workspace.directory
        write_lines(folder / "records" / "campaign.jsonl", [store.get(campaign_id, "campaign")])
        with store.connection() as db:
            kinds = [row[0] for row in db.execute("SELECT DISTINCT kind FROM records WHERE campaign_id=? ORDER BY kind", (campaign_id,))]
            calls = [dict(row) for row in db.execute("SELECT * FROM llm_usage WHERE campaign_id=? ORDER BY id", (campaign_id,))] \
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='llm_usage'").fetchone() else []
        designs = []
        for kind in kinds:
            if kind in BULK_KINDS:
                continue
            if kind == "trial":
                rows = []
                for trial in store.list_compact_trials(campaign_id):
                    latest = trial.get("result") or trial.get("progress") or {}
                    design = (trial.get("progress") or {}).get("best_design") or (trial.get("result") or {}).get("best_design")
                    if design is not None:
                        designs.append({"trial_id": trial["id"], "best": latest.get("best_objective"), "best_objectives": latest.get("best_objectives"),
                                        "design": design})
                    # The specification is its own record, and a finished run's progress repeats its result.
                    kept = {key: value for key, value in trial.items() if key not in {"experiment_spec", "pid", "process_identity"}
                            and not (key == "progress" and trial.get("result"))}
                    rows.append(trimmed(kept))
            else:   # one record at a time: a campaign's commands and results can add up to gigabytes
                rows = (trimmed(store.get(identity, kind)) for identity in self._identities(campaign_id, kind))
            write_lines(folder / "records" / f"{kind}.jsonl", rows)
        if calls:
            write_lines(folder / "records" / "model_calls.jsonl", calls)
        if designs:
            write_lines(folder / "best-designs.jsonl", designs)
        for identity in self._identities(campaign_id, "trial"):
            for name in KEPT_TRIAL_FILES:
                source = root / "trials" / identity / name
                if source.is_file() and source.stat().st_size:
                    target = folder / "experiments" / identity / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
        memory = root / "campaigns" / campaign_id / "manager"
        if (memory / "context.md").is_file():
            (folder / "memory").mkdir(exist_ok=True)
            shutil.copyfile(memory / "context.md", folder / "memory" / "context.md")
        if (memory / "records").is_dir():
            shutil.copytree(memory / "records", folder / "memory" / "records", dirs_exist_ok=True)
        uploads = root / "campaigns" / campaign_id / "materials" / "uploads"
        for source in sorted(uploads.glob("document-*")) if uploads.is_dir() else []:
            if source.is_file() and not source.is_symlink():
                (folder / "documents").mkdir(exist_ok=True)
                shutil.copyfile(source, folder / "documents" / source.name.removeprefix("document-"))
        for identity in self._identities(campaign_id, "agent_session"):
            work = root / "agents" / identity / "work"
            for source in sorted(work.rglob("*")) if work.is_dir() else []:
                if source.is_file() and not source.is_symlink() and source.stat().st_size <= WORK_FILE_LIMIT:
                    target = folder / "agent-work" / identity / source.relative_to(work)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)

    # ---- removing -----------------------------------------------------------------------------------

    def remove(self, campaign_id, keep):
        if keep not in KEEP:
            raise ValueError("Choose what to keep: nothing, summary or records")
        if not self.one_at_a_time.acquire(blocking=False):
            raise ValueError("Another campaign is being removed; try again when it is done")
        try:
            return self._remove(campaign_id, keep)
        finally:
            self.one_at_a_time.release()

    def _remove(self, campaign_id, keep):
        campaign = self.store.get(campaign_id, "campaign")
        self._refuse_if_blocked(campaign_id)
        # Reading journals and writing what stays can take a while: outside the workspace lock, before anything is deleted.
        folder, kept_bytes = self.keep(campaign_id, keep) if keep != "nothing" else (None, 0)
        deleted = False
        try:
            files = sum(tree_bytes(path) for path in self.paths(campaign_id))
            with self.workspace.lock:
                self._refuse_if_blocked(campaign_id, reach=False)   # nothing may have started meanwhile
                paths = self.paths(campaign_id)
                agents = self._identities(campaign_id, "agent_session")
                trials = self._identities(campaign_id, "trial")
                digests = self._blob_digests(campaign_id)
                record = {"id": self._record_identity(campaign_id), "removed_campaign_id": campaign_id, "name": campaign["name"],
                    "campaign_created_at": campaign.get("created_at"), "removed_at": now(), "keep": keep,
                    "folder": str(folder) if folder else None, "kept_bytes": kept_bytes, "status": "clearing",
                    "paths": [str(path) for path in paths], "agents": agents, "blob_digests": digests, "files_bytes": files}
                with self.store.transaction():
                    record["rows"] = self.store.delete_campaign(campaign_id)
                    self.store.put("campaign_removal", record, "campaign.removed")
                deleted = True
                self._forget(campaign_id, agents, trials)
        except BaseException:
            if folder and not deleted:   # the campaign is still here: leave no summary of it behind
                shutil.rmtree(folder, ignore_errors=True)
            raise
        if folder:
            with (self.directory / "index.jsonl").open("a", encoding="utf-8") as index:
                index.write(json.dumps({key: record[key] for key in ("removed_campaign_id", "name", "removed_at", "keep", "folder")}, ensure_ascii=False) + "\n")
        return self.clear(record)

    def _refuse_if_blocked(self, campaign_id, *, reach=True):
        blockers = self.blockers(campaign_id, reach=reach)
        if blockers:
            raise ValueError("This campaign cannot be removed yet. " + " ".join(blockers))

    def _record_identity(self, campaign_id):
        number = sum(item["removed_campaign_id"] == campaign_id for item in self.store.list("campaign_removal")) + 1
        return f"removal_{campaign_id}" + (f"_{number}" if number > 1 else "")

    def _forget(self, campaign_id, agents, trials):
        """Drop what the running service remembers about the campaign."""
        workspace = self.workspace
        controller = workspace.pi
        with controller.schedule_lock:
            controller.dirty.pop(campaign_id, None)
        controller.schedule.pop(campaign_id, None)
        controller.threads.pop(campaign_id, None)
        workspace.research_threads.pop("pi_" + campaign_id, None)
        for identity in agents:
            controller.tool_locks.pop(identity, None)
        for identity in trials:
            workspace.last_progress.pop(identity, None)
            workspace.processes.pop(identity, None)

    def clear(self, record):
        """Delete the removed campaign's files and its agents' saved sessions; finish the removal record."""
        left = []
        for identity in record.get("agents", []):
            try:
                self.workspace.pi.client.forget(identity)
            except ValueError as error:
                left.append(f"agent session {identity}: {error}")
        for raw in record.get("paths", []):
            path = Path(raw)
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            except OSError as error:
                left.append(f"{path}: {error}")
        # Stored files are shared by content: delete those nothing that remains points to.
        digests = set(record.get("blob_digests", []))
        if digests:
            with self.store.connection() as db:
                for (raw,) in db.execute("SELECT data FROM records WHERE kind IN ('asset','bundle_operation','archived_record')"):
                    digests.difference_update(re.findall(r"[0-9a-f]{64}", raw))
            artifacts = self.workspace.assets.artifacts
            for digest in sorted(digests):
                for path in (artifacts.blobs / digest[:2] / digest[2:], artifacts.external / (digest + ".json")):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as error:
                        left.append(f"{path}: {error}")
        done = {key: value for key, value in record.items() if key not in {"paths", "agents", "blob_digests"}}
        if left:
            # Kept on the record, so that the next start of the service tries these again.
            done.update(status="incomplete", left=left, paths=record.get("paths", []), agents=record.get("agents", []),
                        blob_digests=record.get("blob_digests", []))
        else:
            done.update(status="completed", cleared_at=now())
            done.pop("left", None)
        self.store.put("campaign_removal", done)
        return done

    def recover(self):
        """Finish removals whose files were not all cleared, after a stop of the service or a failed deletion."""
        for record in self.store.list("campaign_removal"):
            if record["status"] in {"clearing", "incomplete"}:
                try:
                    self.store.get(record["removed_campaign_id"], "campaign")
                except KeyError:
                    self.clear(record)
                else:
                    # The identity is in use again; its directories now belong to the new campaign.
                    self.store.put("campaign_removal", {**{key: value for key, value in record.items() if key not in {"paths", "agents", "blob_digests"}},
                        "status": "abandoned", "left": ["The identity was taken by a new campaign before the old files were cleared."]})

    def removed(self):
        return [{key: value for key, value in record.items() if key not in {"paths", "agents", "blob_digests"}}
                for record in self.store.list("campaign_removal")]
