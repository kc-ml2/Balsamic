"""The formulation notebook's records, and the one way to change them.

A notebook is a head record with the order of its cells, one record per cell, and an immutable
snapshot of every revision of a cell. The researcher's command and the agents' tool both change it
through `Formulation.edit`, which applies a batch of operations together or not at all.

Its code cells run together, top to bottom, in a fresh sandboxed process each time: a run leaves no
state behind, so what a cell shows always follows from the cells above it.

Its parameters are an ordered list on the head: the numbers and choices the researcher moves with
controls on the page, which code reads as P.<name>.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import shutil
import subprocess
import threading

from optimization_framework.contracts.formulation import CLEARABLE, DESCRIPTOR, PARAMETERS, SOURCE_CHARACTERS, CellOperation, ParameterOperation
from optimization_framework.storage.sqlite import identifier, now

from .parameters import check_descriptor, check_value, values
from .rules import check, definitions, reads, usage
from .runner import RUN_SECONDS, execute, fonts, picture
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


def _parameter(item, used_by=None):
    """A parameter as an agent reads it."""
    shown = {key: item.get(key) for key in ("name", "label", "kind", "value", "unit", "minimum", "maximum", "step", "choices", "symbol",
                                            "basis", "origin", "description", "prominent", "revision", "value_author")}
    if used_by is not None:
        shown["used_by"] = used_by
    return {key: value for key, value in shown.items() if value not in (None, "", [])}


def _operation(item):
    if isinstance(item, (CellOperation, ParameterOperation)):
        return item
    return (ParameterOperation if str(item.get("op", "")).startswith("parameter.") else CellOperation)(**item)


def _binds_parameters(cells, parameters):
    """Whether code reads parameters as P: always once one is declared; before that, unless a cell defines P itself."""
    if parameters:
        return True
    for cell in cells:
        if cell["kind"] == "code":
            try:
                if "P" in definitions(cell["source"]):
                    return False
            except ValueError:
                continue
    return True


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
        return {"id": self.notebook_id(campaign_id), "schema_version": 1, "campaign_id": campaign_id, "revision": 0, "order": [], "parameters": [],
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
        # The outline is offered until the notebook holds more than the researcher's own description.
        offered = outline(skip={cell["name"] for cell in cells}) if all(cell["name"] == "problem" for cell in cells) else []
        parameters = head.get("parameters", []) if head else []
        used = usage(cells, [item["name"] for item in parameters])
        return {"notebook": head, "cells": [{**cell, **({"output": outputs[cell["id"]]} if cell["id"] in outputs else {})} for cell in cells],
                "parameters": [{**item, "used_by": used[item["name"]]} for item in parameters],
                "outline": offered, "active_run": runs["active"], "latest_run": runs["latest"],
                "limits": {"cells": CELLS, "source_characters": SOURCE_CHARACTERS, "run_seconds": RUN_SECONDS, "parameters": PARAMETERS}, "guide": GUIDE}

    def history(self, campaign_id, cell_id):
        """Every revision of one cell, oldest first; a deleted cell keeps its history."""
        rows = [row for row in self.store.list("formulation_cell_revision", campaign_id) if row["cell_id"] == cell_id]
        if not rows:
            raise KeyError(cell_id)
        return rows

    def edit(self, campaign_id, operations, *, author=RESEARCHER, author_id=None, reason=""):
        """Apply a batch of cell and parameter operations together, or refuse all of them.

        `author` is "researcher" or an agent's role. Only the researcher marks a cell agreed; when
        anyone else changes an agreed cell it is proposed again and the batch must say why. In the
        same way, an agent that changes a value the researcher set must say why, and a given value
        it changes becomes an assumed one.
        """
        operations = [_operation(op) for op in operations]
        reason = reason.strip()
        with self.workspace.lock, self.store.transaction():
            self.store.get(campaign_id, "campaign")
            head = self._head(campaign_id) or self._new(campaign_id)
            before = {cell["id"]: cell for cell in self._cells(campaign_id, head)}
            cells, order, removed = deepcopy(before), list(head["order"]), []
            held_parameters = {item["id"]: item for item in head.get("parameters", [])}
            parameters, dropped = deepcopy(head.get("parameters", [])), []

            def parameter(reference):
                for item in parameters:
                    if item["name"] == reference:
                        return item
                raise Refusal(f"This notebook has no parameter {reference}; read its outline again")

            def held(operation):
                item = parameter(operation.parameter)
                revision = held_parameters[item["id"]]["revision"] if item["id"] in held_parameters else None
                if revision != operation.expected_revision:
                    raise Refusal(f"Parameter {item['name']} is at revision {revision}, not {operation.expected_revision}: "
                                  "it changed since it was read. Read it again before changing it.")
                return item

            def arrange(item, operation):
                parameters[:] = [other for other in parameters if other is not item]
                anchor = operation.before or operation.after
                if anchor is None:
                    parameters.append(item)
                    return
                target = parameter(anchor)
                parameters.insert(parameters.index(target) + (operation.after is not None), item)

            def given_without_origin(item):
                return author != RESEARCHER and item["basis"] == "given" and not item.get("origin")

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
                if isinstance(operation, ParameterOperation):
                    if operation.op == "parameter.declare":
                        item = {"id": identifier("fparam"), "revision": 0, **{key: getattr(operation, key) for key in DESCRIPTOR}, "value": operation.value}
                        item.update(basis=operation.basis or ("given" if author == RESEARCHER else "assumed"), prominent=bool(operation.prominent),
                                    value_author=author)
                        if given_without_origin(item):
                            raise Refusal(f"Parameter {item['name']} is declared as given: quote where the researcher gave it in origin, "
                                          "or declare it assumed")
                        arrange(item, operation)
                    elif operation.op == "parameter.move":
                        arrange(parameter(operation.parameter), operation)
                    elif operation.op == "parameter.delete":
                        item = held(operation)
                        parameters[:] = [other for other in parameters if other is not item]
                        dropped.append(item)
                    elif operation.op == "parameter.update":
                        item = held(operation)
                        item.update({key: getattr(operation, key) for key in DESCRIPTOR
                                     if getattr(operation, key) is not None or (key in CLEARABLE and key in operation.model_fields_set)})
                        if given_without_origin(item):
                            raise Refusal(f"Parameter {item['name']} would be given: quote where the researcher gave it in origin")
                    else:
                        item = held(operation)
                        if operation.value != item["value"] and author != RESEARCHER and item.get("value_author") == RESEARCHER:
                            if not reason:
                                raise Refusal(f"The researcher set parameter {item['name']} to {item['value']!r}. Give the reason for changing it; "
                                              "it is shown as set by you until the researcher confirms it.")
                            if item["basis"] == "given":
                                item["basis"] = "assumed"
                        # The researcher setting a value, even the one shown, confirms it.
                        item.update(value=operation.value, value_author=author)
                    continue
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
            if len(parameters) > PARAMETERS:
                raise Refusal(f"A notebook holds at most {PARAMETERS} parameters")
            declared = [item["name"] for item in parameters]
            for name in declared:
                if declared.count(name) > 1:
                    raise Refusal(f"Two parameters would be named {name}; a parameter's name is unique in its notebook")
            try:
                for item in parameters:
                    check_descriptor(item)
                    item["value"] = check_value(item, item["value"])
                check(ordered, parameters=declared if _binds_parameters(ordered, parameters) else ())
            except ValueError as error:
                raise Refusal(str(error)) from None

            changed = [cell for cell in ordered if cell != before.get(cell["id"])]
            moved = [item for item in parameters if item != held_parameters.get(item["id"])]
            if (not changed and not removed and order == head["order"] and not moved and not dropped
                    and [item["id"] for item in parameters] == [item["id"] for item in head.get("parameters", [])]):
                return self._outcome(head, [], [])
            revision, stamp = head["revision"] + 1, now()
            for item in [*moved, *({**item, "deleted": True} for item in dropped)]:
                previous = held_parameters.get(item["id"], {})
                value_only = item["id"] in held_parameters and not item.get("deleted") and \
                    {key for key in item if item.get(key) != previous.get(key)} <= {"value", "value_author"}
                if not value_only:
                    item.update(author=author, reason=reason)
                item.update(revision=item["revision"] + 1, updated_at=stamp, notebook_revision=revision)
                if item.get("value_author") == RESEARCHER:
                    item["confirmed_value"] = item["value"]
                elif item["value"] != previous.get("value"):
                    item["value_reason"] = reason
                # A researcher moving a control is not a revision worth keeping; every other change is.
                if not (value_only and author == RESEARCHER):
                    self.store.put_immutable("formulation_parameter_revision", {**item, "id": f"{item['id']}_r{item['revision']}",
                                                                                "parameter_id": item["id"], "campaign_id": campaign_id})
            for cell in [*changed, *({**cell, "deleted": True} for cell in removed)]:
                cell.update(revision=cell["revision"] + 1, author=author, author_id=author_id, reason=reason,
                            notebook_revision=revision, updated_at=stamp)
                if cell["status"] == "agreed" and author == RESEARCHER:
                    cell["agreed_revision"] = cell["revision"]
                self.store.put("formulation_cell", cell)
                self.store.put_immutable("formulation_cell_revision", {**cell, "id": f"{cell['id']}_r{cell['revision']}", "cell_id": cell["id"]})
            head.update(revision=revision, order=order, parameters=parameters, updated_at=stamp, updated_by=author,
                        counts={"cells": len(ordered), **{status: sum(cell["status"] == status for cell in ordered)
                                                          for status in ("proposed", "agreed", "open")}})
            # One event for the whole batch: every event makes the dashboard read its state again.
            self.store.put("formulation_notebook", head, "formulation.updated")
            if author_id:
                self._seen(campaign_id, author_id, [*changed, *moved])
            return self._outcome(head, changed, removed, moved, dropped)

    @staticmethod
    def _outcome(head, changed, removed, moved=(), dropped=()):
        warnings = [f"Cell {cell['name']} writes math as \\( \\) or \\[ \\], which the notebook does not render; use $…$ and $$…$$."
                    for cell in changed if cell["kind"] == "markdown" and ("\\(" in cell["source"] or "\\[" in cell["source"])]
        return {"notebook_id": head["id"], "notebook_revision": head["revision"], "cells": [_brief(cell) for cell in changed],
                "deleted": [cell["name"] for cell in removed],
                **({"parameters": [_parameter(item) for item in moved]} if moved else {}),
                **({"parameters_deleted": [item["name"] for item in dropped]} if dropped else {}),
                **({"warnings": warnings} if warnings else {})}

    def set_values(self, campaign_id, chosen, *, author=RESEARCHER, author_id=None, reason=""):
        """Set parameters' values by name, over whatever revision they are at: the researcher's controls write the latest value."""
        with self.workspace.lock:
            head = self._head(campaign_id)
            held = {item["name"]: item for item in (head.get("parameters", []) if head else [])}
            unknown = sorted(set(chosen) - set(held))
            if unknown:
                raise Refusal(f"This notebook has no parameter {', '.join(unknown)}")
            return self.edit(campaign_id, [{"op": "parameter.set", "parameter": name, "expected_revision": held[name]["revision"], "value": value}
                                           for name, value in chosen.items()], author=author, author_id=author_id, reason=reason)

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

    def unread_parameters(self, campaign_id, reader_id, parameters):
        """Parameters whose current revision this reader has not seen."""
        seen = self._reader(campaign_id, reader_id)["seen"]
        return [item for item in parameters if seen.get(item["id"]) != item["revision"]]

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
                parameters = head.get("parameters", [])
                used = usage(ordered, [item["name"] for item in parameters])
                moved = self.unread_parameters(campaign_id, reader_id, parameters) if reader_id else []
                if reader_id and parameters:
                    self._seen(campaign_id, reader_id, parameters)   # the outline gives every parameter in full
                return {**base, "cells": [{**_brief(cell), "title": _title(cell), "characters": len(cell["source"]),
                                           **({"changed_since_your_last_read": True} if cell["id"] in changed else {})} for cell in ordered],
                        "changed_since_your_last_read": [cell["name"] for cell in ordered if cell["id"] in changed],
                        "parameters": [_parameter(item, used[item["name"]]) for item in parameters],
                        **({"parameters_changed_since_your_last_read": [item["name"] for item in moved]} if moved else {}),
                        "read": "Name cells to read them in full. Code reads a parameter as P.name."}
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
        moved = self.unread_parameters(campaign_id, reader_id, head.get("parameters", []))
        return {"notebook_revision": head["revision"], "counts": head["counts"], "guide": GUIDE,
                "changed_since_your_last_read": [{"name": cell["name"], "by": cell["author"], "status": cell["status"]}
                                                 for cell in self.unread(campaign_id, reader_id, ordered)][:CELLS],
                **({"parameters_changed_since_your_last_read": [{"name": item["name"], "value": item["value"], "set_by": item.get("value_author")}
                                                                for item in moved]} if moved else {}),
                "read": "notebook_read gives the outline, the parameters, and the cells in full."}

    def export_markdown(self, campaign_id):
        """The notebook as one Markdown document: prose as written, code in fenced blocks."""
        campaign = self.store.get(campaign_id, "campaign")
        head = self._head(campaign_id)
        parts = [f"# Formulation: {campaign['name']}", f"Revision {head['revision'] if head else 0}, exported {now()}."]
        parameters = head.get("parameters", []) if head else []
        if parameters:
            cell = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
            rows = ["## Parameters", "", "| Name | Meaning | Value | Range or choices | Basis |", "| --- | --- | --- | --- | --- |"]
            for item in parameters:
                span = " or ".join(item["choices"]) if item.get("choices") else \
                    " to ".join(f"{bound:g}" if bound is not None else "…" for bound in (item.get("minimum"), item.get("maximum"))) \
                    if item.get("minimum") is not None or item.get("maximum") is not None else ""
                rows.append(f"| `{item['name']}` | {cell(item['label'])} | {cell(item['value'])}{' ' + cell(item['unit']) if item.get('unit') else ''} "
                            f"| {cell(span)} | {item['basis'].replace('_', ' ')} |")
            parts.append("\n".join(rows))
        for cell in self._cells(campaign_id, head):
            parts.append(f"<!-- cell {cell['name']}: {cell['status']}, revision {cell['revision']}, last changed by {cell['author']} -->")
            parts.append(cell["source"].rstrip() if cell["kind"] == "markdown" else f"```python\n{cell['source'].rstrip()}\n```")
        return "\n\n".join(parts) + "\n"

    def export_marimo(self, campaign_id):
        """The notebook as a marimo file: every cell a function, to open and run outside the dashboard."""
        from .export import marimo_file
        campaign = self.store.get(campaign_id, "campaign")
        head = self._head(campaign_id)
        return marimo_file(campaign["name"], head["revision"] if head else 0, self._cells(campaign_id, head),
                           parameters=head.get("parameters", []) if head else [])

    # Running the code cells.

    def _code(self, cells):
        return [{"id": cell["id"], "name": cell["name"], "source": cell["source"]} for cell in cells if cell["kind"] == "code"]

    @staticmethod
    def _prefixes(code, chosen=None):
        """For each code cell, a digest of everything that runs up to and including it, with the parameter values
        it reads. Its output is current while that holds: moving a control dates the cells from its first reader on."""
        digests, running = {}, hashlib.sha256()
        for cell in code:
            running.update(cell["source"].encode() + b"\0")
            if chosen:
                try:
                    named, dynamic = reads(cell["source"])
                except SyntaxError:
                    named, dynamic = set(), False
                used = sorted(chosen) if dynamic else sorted(named & set(chosen))
                if used:
                    running.update(json.dumps({name: chosen[name] for name in used}, sort_keys=True).encode() + b"\0")
            digests[cell["id"]] = running.hexdigest()[:24]
        return digests

    @staticmethod
    def _arguments(head, cells):
        """The values code reads as P, or None for a notebook whose own cell defines P."""
        parameters = head.get("parameters", []) if head else []
        return values(parameters) if _binds_parameters(cells, parameters) else None

    def _directory(self, campaign_id, *parts):
        return self.workspace.directory.joinpath("campaigns", campaign_id, "formulation", *parts)

    def _fonts(self, campaign_id):
        try:
            return fonts(self._directory(campaign_id, "support", "matplotlib"))
        except (OSError, subprocess.SubprocessError):
            return None   # a run without it only takes longer to draw

    def _brief_run(self, run_id):
        if not run_id:
            return None
        run = self.store.get(run_id, "formulation_run")
        return {key: run.get(key) for key in ("id", "status", "requested_by", "notebook_revision", "started_at", "finished_at", "elapsed_seconds", "stderr")}

    def _outputs(self, head, cells):
        """What the latest finished run produced, per code cell, and whether the cells above it have changed since."""
        if not head or not head["latest_run_id"]:
            return {}
        run, current = self.store.get(head["latest_run_id"], "formulation_run"), self._prefixes(self._code(cells), self._arguments(head, cells))
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

    def run(self, campaign_id, *, requested_by=RESEARCHER, background=False, wait=0., queue=False):
        """Run every code cell, top to bottom, with the parameters' current values, and keep what each produced for the page.
        One run at a time per campaign.

        In the background the record is returned as "running" and finished by a thread, unless it finishes within
        `wait` seconds; otherwise the finished record is returned. With `queue`, a request made while a run is in
        progress is remembered and the notebook runs once more when that run ends, with the values saved by then:
        None is returned.
        """
        with self.workspace.lock, self.store.transaction():
            self.store.get(campaign_id, "campaign")
            head = self._head(campaign_id)
            cells = self._cells(campaign_id, head)
            code = self._code(cells)
            if not code:
                raise Refusal("The notebook has no code cell to run")
            self._release_stale(head)
            if head["active_run_id"]:
                if queue:
                    head["rerun"] = True
                    self.store.put("formulation_notebook", head)
                    return None
                raise Refusal("A run of this notebook is in progress; wait for it to finish")
            arguments = self._arguments(head, cells)
            run = {"id": identifier("frun"), "schema_version": 1, "campaign_id": campaign_id, "notebook_revision": head["revision"],
                   "requested_by": requested_by, "status": "running", "started_at": now(), "seconds": self._allowance(head, requested_by),
                   "prefixes": self._prefixes(code, arguments), "parameters": arguments, "cells": {}}
            self.store.put("formulation_run", run)
            head["active_run_id"] = run["id"]
            self.store.put("formulation_notebook", head, "formulation.run_started")
        if not background:
            return self._finish(run, code)
        threads = self.workspace.research_threads
        for identity in [key for key, thread in list(threads.items()) if key.startswith("frun_") and not thread.is_alive()]:
            threads.pop(identity, None)
        thread = threading.Thread(target=self._finish, args=(run, code), name=run["id"], daemon=True)
        threads[run["id"]] = thread
        thread.start()
        if wait > 0:
            thread.join(wait)
            if not thread.is_alive():
                return self.store.get(run["id"], "formulation_run")
        return run

    def _finish(self, run, code):
        campaign_id = run["campaign_id"]
        try:
            result = execute(self._directory(campaign_id, "runs", run["id"]), code, seconds=run["seconds"], parameters=run.get("parameters"),
                             font_list=self._fonts(campaign_id))
        except Exception as error:   # whatever went wrong, the record does not stay "running"
            result = {"status": "failed", "cells": {}, "elapsed_seconds": 0.0, "stderr": str(error)[:2000]}
        with self.workspace.lock, self.store.transaction():
            head = self._head(campaign_id)
            if self.store.get(run["id"], "formulation_run")["status"] != "running":
                return self.store.get(run["id"], "formulation_run")   # released as lost meanwhile
            finished = self.store.put("formulation_run", {**run, **result, "finished_at": now()})
            previous = head["latest_run_id"]
            head["run_seconds"]["agents" if run["requested_by"] != RESEARCHER else RESEARCHER] += result["elapsed_seconds"]
            again = head.pop("rerun", False)
            head.update(active_run_id=None, latest_run_id=run["id"])
            self.store.put("formulation_notebook", head, "formulation.run_completed")
            if previous:
                # Only the latest run's outputs are shown, so only they are kept.
                self.store.put("formulation_run", {**self.store.get(previous, "formulation_run"), "cells": {}, "pruned": True})
                shutil.rmtree(self._directory(campaign_id, "runs", previous), ignore_errors=True)
        if again:
            # A control was moved during this run: run once more with the values saved since.
            try:
                self.run(campaign_id, background=True)
            except Refusal:
                pass
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

    def scratch(self, campaign_id, code, *, seconds=30., parameters=None):
        """Run the notebook's code cells and then `code` in the same process, and keep nothing: where an agent tries
        something against the notebook's values before writing it into a cell. `parameters` replaces some values
        for this run only."""
        with self.workspace.lock, self.store.transaction():
            self.store.get(campaign_id, "campaign")
            head = self._head(campaign_id) or self.store.put("formulation_notebook", self._new(campaign_id))
            notebook = self._cells(campaign_id, head)
            cells = self._code(notebook)
            seconds = self._allowance(head, "agent", min(seconds, SCRATCH_SECONDS))
            arguments = self._arguments(head, notebook)
            held = {item["name"]: item for item in head.get("parameters", [])}
            for name, value in (parameters or {}).items():
                if name not in held or arguments is None:
                    raise Refusal(f"This notebook has no parameter {name} to try another value of")
                try:
                    arguments[name] = check_value(held[name], value)
                except ValueError as error:
                    raise Refusal(str(error)) from None
        directory = self._directory(campaign_id, "scratch", identifier("scratch"))
        try:
            result = execute(directory, cells, scratch=code, seconds=seconds, parameters=arguments, font_list=self._fonts(campaign_id))
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
