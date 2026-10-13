"""The formulation notebook's records, and the one way to change them.

A notebook is a head record with the order of its cells, one record per cell, and an immutable
snapshot of every revision of a cell. The researcher's command and the agents' tool both change it
through `Formulation.edit`, which applies a batch of operations together or not at all.

Its code cells run together, top to bottom, in a fresh sandboxed process each time: a run leaves no
state behind, so what a cell shows always follows from the cells above it.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import shutil
import threading

from optimization_framework.contracts.formulation import CellOperation, SOURCE_CHARACTERS
from optimization_framework.storage.sqlite import identifier, now

from .rules import check
from .runner import RUN_SECONDS, execute, picture
from .template import outline

CELLS = 200
# What a campaign's agents may spend running its notebook. The researcher's own runs are not counted against it.
AGENT_RUN_SECONDS = 3600.
SCRATCH_SECONDS = 60.
GUIDE = "docs/formulation-notebook.md"
RESEARCHER = "researcher"


class Refusal(ValueError):
    """An edit or a read the notebook does not accept. It is reported to whoever asked, not filed as a campaign issue."""


def _title(cell):
    """The first line of a cell, for outlines."""
    for line in cell["source"].splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()[:120]
    return ""


def _brief(cell):
    return {key: cell[key] for key in ("id", "name", "kind", "status", "revision", "author", "updated_at")}


class Formulation:
    def __init__(self, workspace):
        self.workspace, self.store = workspace, workspace.store

    @staticmethod
    def notebook_id(campaign_id):
        return "formulation_" + campaign_id

    def _head(self, campaign_id):
        try:
            return self.store.get(self.notebook_id(campaign_id), "formulation_notebook")
        except KeyError:
            return None

    def _new(self, campaign_id):
        stamp = now()
        return {"id": self.notebook_id(campaign_id), "schema_version": 1, "campaign_id": campaign_id, "revision": 0, "order": [],
                "counts": {"cells": 0, "proposed": 0, "agreed": 0, "open": 0}, "latest_run_id": None, "active_run_id": None,
                "run_seconds": {"agents": 0.0, RESEARCHER: 0.0}, "created_at": stamp, "updated_at": stamp, "updated_by": None}

    def _cells(self, campaign_id, head):
        """The notebook's live cells, in order."""
        if not head:
            return []
        live = {cell["id"]: cell for cell in self.store.list("formulation_cell", campaign_id) if not cell["deleted"]}
        return [live[identity] for identity in head["order"]]

    def summary(self, campaign_id):
        """What the dashboard needs to know that the notebook changed. Reads the head only and creates nothing."""
        head = self._head(campaign_id)
        return head and {"notebook_id": head["id"], **{key: head[key] for key in
                                                        ("revision", "counts", "updated_at", "updated_by", "latest_run_id", "active_run_id")}}

    def view(self, campaign_id):
        """The whole notebook, for the researcher's page: every cell, and under each code cell what its last run produced."""
        self.store.get(campaign_id, "campaign")
        with self.workspace.lock:
            head = self._head(campaign_id)
            if head:
                self._release_stale(head)
            cells = self._cells(campaign_id, head)
            outputs = self._outputs(head, cells)
            runs = {key: self._brief_run(head and head[key + "_run_id"]) for key in ("active", "latest")}
        return {"notebook": head, "cells": [{**cell, **({"output": outputs[cell["id"]]} if cell["id"] in outputs else {})} for cell in cells],
                "outline": [] if cells else outline(), "active_run": runs["active"], "latest_run": runs["latest"],
                "limits": {"cells": CELLS, "source_characters": SOURCE_CHARACTERS, "run_seconds": RUN_SECONDS}, "guide": GUIDE}

    def history(self, campaign_id, cell_id):
        """Every revision of one cell, oldest first; a deleted cell keeps its history."""
        rows = [row for row in self.store.list("formulation_cell_revision", campaign_id) if row["cell_id"] == cell_id]
        if not rows:
            raise KeyError(cell_id)
        return rows

    def edit(self, campaign_id, operations, *, author=RESEARCHER, author_id=None, reason=""):
        """Apply a batch of cell operations together, or refuse all of them.

        `author` is "researcher" or an agent's role. Only the researcher marks a cell agreed; when
        anyone else changes an agreed cell it is proposed again and the batch must say why.
        """
        operations = [op if isinstance(op, CellOperation) else CellOperation(**op) for op in operations]
        reason = reason.strip()
        with self.workspace.lock, self.store.transaction():
            self.store.get(campaign_id, "campaign")
            head = self._head(campaign_id) or self._new(campaign_id)
            before = {cell["id"]: cell for cell in self._cells(campaign_id, head)}
            cells, order, removed = deepcopy(before), list(head["order"]), []

            def find(reference):
                for cell in cells.values():
                    if reference in (cell["id"], cell["name"]):
                        return cell
                raise Refusal(f"This notebook has no cell {reference}; read its outline again")

            def current(operation):
                cell = find(operation.cell)
                held = before[cell["id"]]["revision"] if cell["id"] in before else None
                if held != operation.expected_revision:
                    raise Refusal(f"Cell {cell['name']} is at revision {held}, not {operation.expected_revision}: "
                                  "it changed since it was read. Read it again before changing it.")
                return cell

            def place(identity, operation):
                if identity in order:
                    order.remove(identity)
                anchor = operation.before or operation.after
                if anchor is None:
                    order.append(identity)
                    return
                target = find(anchor)["id"]
                if target == identity:
                    raise Refusal("A cell cannot be placed next to itself")
                order.insert(order.index(target) + (operation.after is not None), identity)

            for operation in operations:
                if operation.op == "create":
                    if operation.status == "agreed" and author != RESEARCHER:
                        raise Refusal("Only the researcher marks a cell agreed; create it as proposed or open")
                    taken = {cell["name"] for cell in cells.values()}
                    stem = "note" if operation.kind == "markdown" else "code"
                    name = operation.name or next(f"{stem}_{n}" for n in range(1, CELLS + 2) if f"{stem}_{n}" not in taken)
                    cell = {"id": identifier("fcell"), "schema_version": 1, "campaign_id": campaign_id, "notebook_id": head["id"],
                        "name": name, "kind": operation.kind, "source": operation.source, "status": operation.status or "proposed",
                        "revision": 0, "agreed_revision": None, "deleted": False, "created_at": now()}
                    cells[cell["id"]] = cell
                    place(cell["id"], operation)
                elif operation.op == "move":
                    place(find(operation.cell)["id"], operation)
                elif operation.op == "delete":
                    cell = current(operation)
                    order.remove(cell["id"])
                    removed.append(cells.pop(cell["id"]))
                else:
                    cell = current(operation)
                    if operation.status == "agreed" and author != RESEARCHER:
                        raise Refusal("Only the researcher marks a cell agreed")
                    rewritten = operation.source is not None and operation.source != cell["source"]
                    if rewritten and cell["status"] == "agreed" and author != RESEARCHER:
                        if not reason:
                            raise Refusal(f"The researcher agreed to cell {cell['name']}. Give the reason for changing it; it becomes proposed again.")
                        cell["status"] = "proposed"
                    for key in ("name", "source", "status"):
                        if getattr(operation, key) is not None:
                            cell[key] = getattr(operation, key)

            if len(order) > CELLS:
                raise Refusal(f"A notebook holds at most {CELLS} cells")
            ordered = [cells[identity] for identity in order]
            names = [cell["name"] for cell in ordered]
            for name in names:
                if names.count(name) > 1:
                    raise Refusal(f"Two cells would be named {name}; a cell's name is unique in its notebook")
            try:
                check(ordered)
            except ValueError as error:
                raise Refusal(str(error)) from None

            changed = [cell for cell in ordered if cell != before.get(cell["id"])]
            if not changed and not removed and order == head["order"]:
                return self._outcome(head, [], [])
            revision, stamp = head["revision"] + 1, now()
            for cell in [*changed, *({**cell, "deleted": True} for cell in removed)]:
                cell.update(revision=cell["revision"] + 1, author=author, author_id=author_id, reason=reason,
                            notebook_revision=revision, updated_at=stamp)
                if cell["status"] == "agreed" and author == RESEARCHER:
                    cell["agreed_revision"] = cell["revision"]
                self.store.put("formulation_cell", cell)
                self.store.put_immutable("formulation_cell_revision", {**cell, "id": f"{cell['id']}_r{cell['revision']}", "cell_id": cell["id"]})
            head.update(revision=revision, order=order, updated_at=stamp, updated_by=author,
                        counts={"cells": len(ordered), **{status: sum(cell["status"] == status for cell in ordered)
                                                          for status in ("proposed", "agreed", "open")}})
            # One event for the whole batch: every event makes the dashboard read its state again.
            self.store.put("formulation_notebook", head, "formulation.updated")
            if author_id:
                self._seen(campaign_id, author_id, changed)
            return self._outcome(head, changed, removed)

    @staticmethod
    def _outcome(head, changed, removed):
        warnings = [f"Cell {cell['name']} writes math as \\( \\) or \\[ \\], which the notebook does not render; use $…$ and $$…$$."
                    for cell in changed if cell["kind"] == "markdown" and ("\\(" in cell["source"] or "\\[" in cell["source"])]
        return {"notebook_id": head["id"], "notebook_revision": head["revision"], "cells": [_brief(cell) for cell in changed],
                "deleted": [cell["name"] for cell in removed], **({"warnings": warnings} if warnings else {})}

    def _reader(self, campaign_id, reader_id):
        identity = "formulation_reader_" + reader_id
        try:
            return self.store.get(identity, "formulation_reader")
        except KeyError:
            return {"id": identity, "campaign_id": campaign_id, "reader_id": reader_id, "seen": {}}

    def _seen(self, campaign_id, reader_id, cells):
        """Remember the revisions a reader has now seen in full. Written without an event: nothing visible changed."""
        record = self._reader(campaign_id, reader_id)
        record["seen"].update({cell["id"]: cell["revision"] for cell in cells})
        self.store.put("formulation_reader", record)

    def unread(self, campaign_id, reader_id, cells=None):
        """Cells whose current revision this reader has not read in full."""
        cells = self._cells(campaign_id, self._head(campaign_id)) if cells is None else cells
        seen = self._reader(campaign_id, reader_id)["seen"]
        return [cell for cell in cells if seen.get(cell["id"]) != cell["revision"]]

    def read(self, campaign_id, *, cells=None, reader_id=None, max_bytes=48000):
        """The outline, or the named cells in full up to `max_bytes` of source.

        The outline lists what changed since this reader last read each cell in full, so an agent
        notices the researcher's edits without reading the whole notebook again.
        """
        with self.workspace.lock:
            head = self._head(campaign_id)
            ordered = self._cells(campaign_id, head)
            if not head:
                return {"notebook": None, "cells": [], "guide": GUIDE,
                        "note": "The notebook is empty. notebook_edit creates its first cells; read the guide before writing."}
            base = {"notebook_revision": head["revision"], "counts": head["counts"], "guide": GUIDE}
            if cells is None:
                changed = {cell["id"] for cell in self.unread(campaign_id, reader_id, ordered)} if reader_id else set()
                return {**base, "cells": [{**_brief(cell), "title": _title(cell), "characters": len(cell["source"]),
                                           **({"changed_since_your_last_read": True} if cell["id"] in changed else {})} for cell in ordered],
                        "changed_since_your_last_read": [cell["name"] for cell in ordered if cell["id"] in changed],
                        "read": "Name cells to read them in full."}
            wanted, missing = [], []
            for reference in dict.fromkeys(cells):
                match = next((cell for cell in ordered if reference in (cell["id"], cell["name"])), None)
                (wanted if match else missing).append(match or reference)
            if missing:
                raise Refusal(f"This notebook has no cell {', '.join(missing)}; read its outline again")
            returned, used = [], 0
            for cell in wanted:
                size = len(cell["source"].encode())
                if returned and used + size > max_bytes:
                    break
                returned.append(cell)
                used += size
            if reader_id:
                self._seen(campaign_id, reader_id, returned)
            left = [cell["name"] for cell in wanted[len(returned):]]
            outputs = self._outputs(head, ordered)
            return {**base, "cells": [{**_brief(cell), "source": cell["source"], "reason": cell["reason"], "agreed_revision": cell["agreed_revision"],
                                       **({"output": _shortened(outputs[cell["id"]], 1500)} if cell["id"] in outputs else {})} for cell in returned],
                    **({"not_returned": left, "read": "These did not fit; read them in another call."} if left else {})}

    def inspect(self, campaign_id, reader_id):
        """The few lines an agent's campaign inspection carries about the notebook."""
        head = self._head(campaign_id)
        if not head:
            return {"notebook": None, "guide": GUIDE}
        ordered = self._cells(campaign_id, head)
        return {"notebook_revision": head["revision"], "counts": head["counts"], "guide": GUIDE,
                "changed_since_your_last_read": [{"name": cell["name"], "by": cell["author"], "status": cell["status"]}
                                                 for cell in self.unread(campaign_id, reader_id, ordered)][:CELLS],
                "read": "notebook_read gives the outline and the cells in full."}

    def export_markdown(self, campaign_id):
        """The notebook as one Markdown document: prose as written, code in fenced blocks."""
        campaign = self.store.get(campaign_id, "campaign")
        head = self._head(campaign_id)
        parts = [f"# Formulation: {campaign['name']}", f"Revision {head['revision'] if head else 0}, exported {now()}."]
        for cell in self._cells(campaign_id, head):
            parts.append(f"<!-- cell {cell['name']}: {cell['status']}, revision {cell['revision']}, last changed by {cell['author']} -->")
            parts.append(cell["source"].rstrip() if cell["kind"] == "markdown" else f"```python\n{cell['source'].rstrip()}\n```")
        return "\n\n".join(parts) + "\n"

    def export_marimo(self, campaign_id):
        """The notebook as a marimo file: every cell a function, to open and run outside the dashboard."""
        from .export import marimo_file
        campaign = self.store.get(campaign_id, "campaign")
        head = self._head(campaign_id)
        return marimo_file(campaign["name"], head["revision"] if head else 0, self._cells(campaign_id, head))

    # Running the code cells.

    def _code(self, cells):
        return [{"id": cell["id"], "name": cell["name"], "source": cell["source"]} for cell in cells if cell["kind"] == "code"]

    @staticmethod
    def _prefixes(code):
        """For each code cell, a digest of everything that runs up to and including it. Its output is current while that holds."""
        digests, running = {}, hashlib.sha256()
        for cell in code:
            running.update(cell["source"].encode() + b"\0")
            digests[cell["id"]] = running.hexdigest()[:24]
        return digests

    def _directory(self, campaign_id, *parts):
        return self.workspace.directory.joinpath("campaigns", campaign_id, "formulation", *parts)

    def _brief_run(self, run_id):
        if not run_id:
            return None
        run = self.store.get(run_id, "formulation_run")
        return {key: run.get(key) for key in ("id", "status", "requested_by", "notebook_revision", "started_at", "finished_at", "elapsed_seconds", "stderr")}

    def _outputs(self, head, cells):
        """What the latest finished run produced, per code cell, and whether the cells above it have changed since."""
        if not head or not head["latest_run_id"]:
            return {}
        run, current = self.store.get(head["latest_run_id"], "formulation_run"), self._prefixes(self._code(cells))
        return {identity: {**record, "run_id": run["id"], "stale": run["prefixes"].get(identity) != current[identity]}
                for identity, record in run["cells"].items() if identity in current}

    def _release_stale(self, head):
        """A run that outlived its time limit by far has lost its process: it no longer holds the notebook."""
        if not head["active_run_id"]:
            return
        run = self.store.get(head["active_run_id"], "formulation_run")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(run["started_at"])).total_seconds()
        thread = self.workspace.research_threads.get(run["id"])
        if age > run["seconds"] + 30 and not (thread and thread.is_alive()):
            self._interrupt(head, run)

    def _interrupt(self, head, run):
        self.store.put("formulation_run", {**run, "status": "interrupted", "finished_at": now(),
            "stderr": "The service stopped during this run. Nothing was kept from it; run the notebook again."})
        head["active_run_id"] = None
        self.store.put("formulation_notebook", head, "formulation.run_completed")

    def recover(self):
        """After a restart no run is alive: release the notebooks that one was holding."""
        with self.workspace.lock, self.store.transaction():
            for run in self.store.list("formulation_run"):
                head = self._head(run["campaign_id"]) if run["status"] == "running" else None
                if head and head["active_run_id"] == run["id"]:
                    self._interrupt(head, run)

    def _allowance(self, head, requested_by, wanted=RUN_SECONDS):
        if requested_by == RESEARCHER:
            return min(wanted, RUN_SECONDS)
        left = AGENT_RUN_SECONDS - head["run_seconds"]["agents"]
        if left <= 0:
            raise Refusal(f"This campaign's agents have used their {AGENT_RUN_SECONDS:g} s of notebook run time. "
                          "The researcher can still run the notebook with Run all on the Formulation page.")
        return min(wanted, RUN_SECONDS, left)

    def run(self, campaign_id, *, requested_by=RESEARCHER, background=False):
        """Run every code cell, top to bottom, and keep what each produced for the page. One run at a time per campaign.

        In the background the record is returned as "running" and finished by a thread; otherwise the
        finished record is returned.
        """
        with self.workspace.lock, self.store.transaction():
            self.store.get(campaign_id, "campaign")
            head = self._head(campaign_id)
            code = self._code(self._cells(campaign_id, head))
            if not code:
                raise Refusal("The notebook has no code cell to run")
            self._release_stale(head)
            if head["active_run_id"]:
                raise Refusal("A run of this notebook is in progress; wait for it to finish")
            run = {"id": identifier("frun"), "schema_version": 1, "campaign_id": campaign_id, "notebook_revision": head["revision"],
                   "requested_by": requested_by, "status": "running", "started_at": now(), "seconds": self._allowance(head, requested_by),
                   "prefixes": self._prefixes(code), "cells": {}}
            self.store.put("formulation_run", run)
            head["active_run_id"] = run["id"]
            self.store.put("formulation_notebook", head, "formulation.run_started")
        if not background:
            return self._finish(run, code)
        thread = threading.Thread(target=self._finish, args=(run, code), name=run["id"], daemon=True)
        self.workspace.research_threads[run["id"]] = thread
        thread.start()
        return run

    def _finish(self, run, code):
        campaign_id = run["campaign_id"]
        try:
            result = execute(self._directory(campaign_id, "runs", run["id"]), code, seconds=run["seconds"])
        except Exception as error:   # whatever went wrong, the record does not stay "running"
            result = {"status": "failed", "cells": {}, "elapsed_seconds": 0.0, "stderr": str(error)[:2000]}
        with self.workspace.lock, self.store.transaction():
            head = self._head(campaign_id)
            if self.store.get(run["id"], "formulation_run")["status"] != "running":
                return self.store.get(run["id"], "formulation_run")   # released as lost meanwhile
            finished = self.store.put("formulation_run", {**run, **result, "finished_at": now()})
            previous = head["latest_run_id"]
            head["run_seconds"]["agents" if run["requested_by"] != RESEARCHER else RESEARCHER] += result["elapsed_seconds"]
            head.update(active_run_id=None, latest_run_id=run["id"])
            self.store.put("formulation_notebook", head, "formulation.run_completed")
            if previous:
                # Only the latest run's outputs are shown, so only they are kept.
                self.store.put("formulation_run", {**self.store.get(previous, "formulation_run"), "cells": {}, "pruned": True})
                shutil.rmtree(self._directory(campaign_id, "runs", previous), ignore_errors=True)
        return finished

    def report(self, campaign_id, run):
        """A finished run as an agent reads it: each code cell's outcome in notebook order, shortened to fit."""
        head = self._head(campaign_id)
        cells = [cell for cell in self._cells(campaign_id, head) if cell["id"] in run["cells"]]
        share = max(300, 20000 // max(1, len(cells)))
        return {"run_id": run["id"], "status": run["status"], "elapsed_seconds": run.get("elapsed_seconds"),
                **({"stderr": run["stderr"]} if run.get("stderr") else {}),
                "cells": [{"name": cell["name"], **_shortened(run["cells"][cell["id"]], share)} for cell in cells],
                "agent_run_seconds_left": round(AGENT_RUN_SECONDS - head["run_seconds"]["agents"])}

    def scratch(self, campaign_id, code, *, seconds=30.):
        """Run the notebook's code cells and then `code` in the same process, and keep nothing: where an agent tries
        something against the notebook's values before writing it into a cell."""
        with self.workspace.lock, self.store.transaction():
            self.store.get(campaign_id, "campaign")
            head = self._head(campaign_id) or self.store.put("formulation_notebook", self._new(campaign_id))
            cells = self._code(self._cells(campaign_id, head))
            seconds = self._allowance(head, "agent", min(seconds, SCRATCH_SECONDS))
        directory = self._directory(campaign_id, "scratch", identifier("scratch"))
        try:
            result = execute(directory, cells, scratch=code, seconds=seconds)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        with self.workspace.lock, self.store.transaction():
            head = self._head(campaign_id)
            head["run_seconds"]["agents"] += result["elapsed_seconds"]
            self.store.put("formulation_notebook", head)
        names = {cell["id"]: cell["name"] for cell in cells}
        failed = [{"name": names[identity], **_shortened(record, 1500)} for identity, record in result["cells"].items()
                  if record["status"] not in {"ok", "skipped", "not_run"}]
        return {**_shortened(result["scratch"], 6000, pictures=False), "elapsed_seconds": result["elapsed_seconds"],
                **({"notebook_cells_that_failed": failed, "note": "The scratch code did not run: the notebook's own cells failed first."} if failed else {}),
                **({"stderr": result["stderr"]} if result.get("stderr") else {}),
                "agent_run_seconds_left": round(AGENT_RUN_SECONDS - head["run_seconds"]["agents"]), "kept": "Nothing was saved to the notebook."}

    def figure(self, campaign_id, run_id, name):
        """The path of a picture of a run of this campaign's notebook."""
        if self.store.get(run_id, "formulation_run")["campaign_id"] != campaign_id:
            raise KeyError(run_id)
        path = picture(self._directory(campaign_id, "runs", run_id), name)
        if path is None:
            raise KeyError(name)
        return path


def _shortened(record, characters, pictures=True):
    """A cell's run record cut to `characters` per text, for an agent's context. A picture is named, not sent."""
    cut = lambda text: text if len(text) <= characters else "…" + text[-characters:]
    shown = []
    for output in record.get("outputs", []):
        if output["mime"] == "image/png":
            shown.append({"mime": "image/png", "note": "A figure; the researcher sees it on the Formulation page." if pictures
                          else "A figure was drawn and discarded: the scratchpad keeps nothing."})
        else:
            shown.append({"mime": output["mime"], "text": cut(output["text"])})
    return {key: value for key, value in {"status": record["status"], "stale": record.get("stale"), "stdout": cut(record.get("stdout", "")),
            "error": cut(record["error"]) if record.get("error") else None, "outputs": shown, "after": record.get("after"),
            "elapsed_seconds": record.get("elapsed_seconds")}.items() if value not in (None, "", [])}
