"""Project trial progress into TensorBoard without copying masks or archives.

The JSONL progress files remain the experiment record. TensorBoard event files
are disposable presentation data and contain only finite scalar observations.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import re
import shutil
import threading
import time

from starlette.middleware.wsgi import WSGIMiddleware
from tensorboard import program
from tensorboard.compat.proto import event_pb2, summary_pb2
from tensorboard.summary.writer.event_file_writer import EventFileWriter


def _component(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:100]


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _wall_time(row: dict) -> float:
    try:
        return datetime.fromisoformat(row["updated_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return time.time()


def scalar_events(row: dict) -> list[event_pb2.Event]:
    """One chart per cost axis, so their X coordinates keep their real units."""
    observations = _finite(row.get("evaluations"))
    if observations is None:
        return []
    worker_seconds = _finite(row.get("elapsed_seconds"))
    solver_calls = None if row.get("unknown_solver_cost") else _finite(row.get("solver_calls"))
    values = []
    for tag, key in (("efficiency/current_by_evaluation", "objective"),
                     ("efficiency/best_by_evaluation", "best_objective"),
                     ("cost/worker_seconds", "elapsed_seconds"),
                     ("cost/solver_executions", "solver_calls"),
                     ("cost/cache_hits", "cache_hits")):
        value = _finite(row.get(key))
        if value is not None and not (key == "solver_calls" and solver_calls is None):
            values.append((tag, int(observations), value))
    best = _finite(row.get("best_objective"))
    if best is not None:
        if worker_seconds is not None and not row.get("unknown_worker_cost"):
            values.append(("efficiency/best_by_worker_second", int(worker_seconds), best))
        if solver_calls is not None:
            values.append(("efficiency/best_by_solver_execution", int(solver_calls), best))
    wall = _wall_time(row)
    return [event_pb2.Event(wall_time=wall, step=step,
            summary=summary_pb2.Summary(value=[summary_pb2.Summary.Value(tag=tag, simple_value=value)]))
            for tag, step, value in values]


class ScalarExporter:
    def __init__(self, workspace):
        self.workspace = workspace
        self.directory = Path(workspace.directory) / "tensorboard" / "generated"
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.offsets: dict[str, int] = {}
        self.writers: dict[str, EventFileWriter] = {}

    def start(self):
        # This subtree is a derived cache. Rebuilding it avoids stale or
        # duplicate curves after a supervisor restart or truncated journal.
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.directory.mkdir(parents=True)
        self.thread = threading.Thread(target=self._run, name="tensorboard-scalar-export", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
        for writer in self.writers.values():
            writer.close()
        self.writers.clear()

    def _writer(self, trial):
        trial_id = trial["id"]
        if trial_id not in self.writers:
            run = self.directory / _component(trial["campaign_id"]) / _component(trial["algorithm"]) / f"seed-{trial['seed']}-{_component(trial_id)}"
            self.writers[trial_id] = EventFileWriter(str(run), flush_secs=5)
        return self.writers[trial_id]

    def sync_once(self):
        headers = getattr(self.workspace.store, "list_trial_headers", None)
        # Small legacy/test adapters can still supply a trial listing. The
        # application Store reads only metadata, even for unchanged journals.
        for trial in headers() if headers else self.workspace.store.list("trial"):
            trial_id = trial["id"]
            path = self.workspace.job_dir(trial_id) / "metrics.jsonl"
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            offset = self.offsets.get(trial_id, 0)
            if offset > size:
                # An interrupted trial can replace its progress journal.
                previous = self.writers.pop(trial_id, None)
                if previous:
                    previous.close()
                offset = 0
            if offset == size:
                continue
            writer = self._writer(trial)
            with path.open("rb") as stream:
                stream.seek(offset)
                while stream.tell() < size:
                    start = stream.tell()
                    raw = stream.readline()
                    if not raw.endswith(b"\n"):
                        stream.seek(start)
                        break
                    try:
                        row = json.loads(raw)
                    except (UnicodeDecodeError, ValueError):
                        continue
                    for event in scalar_events(row):
                        writer.add_event(event)
                self.offsets[trial_id] = stream.tell()
            writer.flush()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.sync_once()
            except Exception:
                # The projection is optional; a transient malformed journal or
                # database read must never stop experiment supervision.
                import logging
                logging.getLogger(__name__).exception("TensorBoard scalar export failed")
            self.stop_event.wait(5)


def tensorboard_app(logdir: Path) -> WSGIMiddleware:
    """Serve TensorBoard on the workspace origin, including over TailNet."""
    board = program.TensorBoard(server_class=lambda app, flags: app)
    board.configure(logdir=str(logdir), path_prefix="/tensorboard", reload_interval=5,
                    window_title="Optimization Lab results")
    wsgi = board._make_server()

    def prefixed(environ, start_response):
        # Starlette removes the mount path; TensorBoard's path_prefix middleware
        # expects the complete browser path in PATH_INFO.
        scoped = dict(environ)
        scoped["PATH_INFO"] = "/tensorboard" + scoped["PATH_INFO"]
        scoped["SCRIPT_NAME"] = ""
        return wsgi(scoped, start_response)

    return WSGIMiddleware(prefixed)
