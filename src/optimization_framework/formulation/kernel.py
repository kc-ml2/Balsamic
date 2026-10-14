"""Runs a notebook's code cells in order, in one namespace, and records what each printed, showed and drew.

This file is run as a script inside the sandbox, with the run's directory as its working directory.
It reads `input.json` and rewrites `result.json` after every cell, so a run that is stopped at its
time limit still has the cells that finished. Only the standard library is imported here; the cells
import what they use.
"""
import ast
import contextlib
import json
import linecache
import os
import shutil
import sys
import tempfile
import time
import traceback

OUTPUT_CHARACTERS = 12000
OUTPUTS_PER_CELL = 8
FIGURE_BYTES = 1_000_000
# Where a run finds the font list the server built (mounted read-only), so that matplotlib does not rebuild it.
FONT_LIST = "/opt/matplotlib-fonts"


def _markdown_table(rows, headers=None):
    rows = [list(row) for row in rows]
    headers = list(headers) if headers is not None else [""] * (len(rows[0]) if rows else 0)
    text = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(map(text, headers)) + " |", "|" + " --- |" * len(headers)]
    return "\n".join(lines + ["| " + " | ".join(map(text, row)) + " |" for row in rows])


def _pyomo_latex(component, symbols=None):
    """The LaTeX Pyomo generates for a model or one of its components, cut down to what the notebook can typeset.

    Each set gets its own first letter, in lower case, as its index (users I, index i; days T, index t), or the
    next free letter. `symbols` overrides this by component name: {"x": "q", "T": ["T", ["d"]]}.
    """
    import re
    import pyomo.environ as pyo
    from pyomo.common.collections import ComponentMap
    from pyomo.contrib.latex_printer import latex_printer
    model, names, taken = component.model(), ComponentMap(), set()
    # Every set is named here: the printer starts again at i for each one left to it, and two sets would share a letter.
    for found in model.component_objects((pyo.Set, pyo.RangeSet), descend_into=True):
        if not found.local_name.isidentifier() or found.local_name.endswith(("_index", "_domain")):
            continue   # the product sets Pyomo makes for itself
        wanted = found.local_name[0].lower()
        letter = next(letter for letter in wanted + "ijklmnpqrstuvwabcdefgh" if letter.isalpha() and letter not in taken)
        taken.add(letter)
        names[found] = [found.local_name, [letter]]
    for name, symbol in (symbols or {}).items():
        names[model.find_component(name)] = symbol
    latex = latex_printer(component, latex_component_map=names)
    latex = re.sub(r"\\label\{[^}]*\}", "", latex).replace("\\mathds", "\\mathbb")
    if "\\begin{align}" in latex:
        latex = latex.replace("\\begin{align}", "\\begin{aligned}").replace("\\end{align}", "\\end{aligned}")
    else:   # one equation: no environment, and no alignment marks left over from the model's layout
        latex = re.sub(r"\\(begin|end)\{equation\}", "", latex).replace("&", " ")
    return re.sub(r"[ \t]+", " ", latex).strip()


class Notebook:
    """`nb` in every cell: what a cell calls to show something other than a printed line."""

    def __init__(self):
        self.outputs, self.name = [], ""

    def _add(self, mime, text):
        if len(self.outputs) < OUTPUTS_PER_CELL:
            self.outputs.append({"mime": mime, "text": str(text)[:OUTPUT_CHARACTERS]})

    def md(self, text):
        """Show Markdown; $…$ and $$…$$ are equations."""
        self._add("text/markdown", text)

    def tex(self, latex):
        """Show one displayed equation, given as LaTeX without dollar signs."""
        self._add("text/latex", latex)

    def table(self, rows, headers=None):
        """Show rows of values as a table."""
        self._add("text/markdown", _markdown_table(rows, headers))

    def show(self, value, symbols=None):
        """Show a value in the richest form it offers: an equation, Markdown, a picture, or its text.

        A Pyomo model, constraint or objective is shown as the equation generated from it, so what is
        read is what is solved. `symbols` renames its components there (see _pyomo_latex).
        """
        if type(value).__module__.partition(".")[0] == "pyomo" and hasattr(value, "model"):
            try:
                return self._add("text/latex", _pyomo_latex(value, symbols))
            except Exception:   # the printer does not cover every expression: fall back to Pyomo's own listing
                import io
                listing = io.StringIO()
                value.pprint(ostream=listing)
                return self._add("text/plain", listing.getvalue())
        for method, mime in (("_repr_latex_", "text/latex"), ("_repr_markdown_", "text/markdown")):
            shown = getattr(value, method, None)
            shown = shown() if callable(shown) else None
            if isinstance(shown, str) and shown.strip():
                return self._add(mime, shown)
        picture = getattr(value, "_repr_png_", None)
        picture = picture() if callable(picture) else None
        if isinstance(picture, bytes) and picture:
            return self._picture(picture)
        self._add("text/plain", repr(value))

    def _picture(self, data):
        if len(self.outputs) < OUTPUTS_PER_CELL and len(data) <= FIGURE_BYTES:
            name = f"{self.name}_{len(self.outputs) + 1}.png"
            with open(name, "wb") as stream:
                stream.write(data)
            self.outputs.append({"mime": "image/png", "file": name})

    def _figures(self):
        """Keep the figures a cell left open, then close them: the next cell starts with none."""
        pyplot = sys.modules.get("matplotlib.pyplot")
        if pyplot is None:
            return
        import io
        for number in pyplot.get_fignums():
            buffer = io.BytesIO()
            pyplot.figure(number).savefig(buffer, format="png", dpi=110, bbox_inches="tight")
            self._picture(buffer.getvalue())
        pyplot.close("all")


@contextlib.contextmanager
def captured(record):
    """Everything written to standard output and error while a cell runs, including what a solver prints from C."""
    sys.stdout.flush(); sys.stderr.flush()
    with tempfile.TemporaryFile() as sink:
        saved = os.dup(1), os.dup(2)
        os.dup2(sink.fileno(), 1); os.dup2(sink.fileno(), 2)
        try:
            yield
        finally:
            sys.stdout.flush(); sys.stderr.flush()
            for descriptor, original in zip((1, 2), saved):
                os.dup2(original, descriptor)
                os.close(original)
            size = sink.seek(0, 2)
            sink.seek(max(0, size - OUTPUT_CHARACTERS))
            record["stdout"] = sink.read().decode(errors="replace")
            if size > OUTPUT_CHARACTERS:
                record["stdout_bytes_before_this_end"] = size - OUTPUT_CHARACTERS


def run_cell(name, source, namespace, notebook):
    """Run one cell. Its last line, when it is an expression with a value, is shown."""
    record, started = {"status": "ok", "stdout": "", "outputs": []}, time.monotonic()
    filename = f"<cell {name}>"
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    notebook.outputs, notebook.name = record["outputs"], name
    with captured(record):
        try:
            tree = ast.parse(source, filename)
            last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
            exec(compile(tree, filename, "exec"), namespace)
            if last is not None:
                value = eval(compile(ast.Expression(last.value), filename, "eval"), namespace)
                if value is not None:
                    notebook.show(value)
            notebook._figures()
        except BaseException as error:   # a cell that calls exit() has failed too
            frames = traceback.extract_tb(error.__traceback__)
            own = [frame for frame in frames if frame.filename.startswith("<cell ")]   # not this runner's own frames
            lines = traceback.format_list(own) + traceback.format_exception_only(type(error), error)
            record.update(status="error", error="".join(lines)[-OUTPUT_CHARACTERS:])
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return record


class Parameters:
    """The notebook's parameters, as cells read them: P.B or P["B"]. Cells cannot change them; the controls on the page do."""
    __slots__ = ("_values",)

    def __init__(self, values):
        object.__setattr__(self, "_values", dict(values))

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(f"There is no parameter {name}; the parameters are {', '.join(self._values) or 'none yet'}") from None

    def __getitem__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise KeyError(f"There is no parameter {name}") from None

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __contains__(self, name):
        return name in self._values

    def __setattr__(self, name, value):
        raise AttributeError("Parameters are set with the controls on the Formulation page, not by a cell")

    __delattr__ = __setattr__

    def __setitem__(self, name, value):
        raise TypeError("Parameters are set with the controls on the Formulation page, not by a cell")

    def __repr__(self):
        return "P(" + ", ".join(f"{name}={value!r}" for name, value in self._values.items()) + ")"


def save(result):
    with open("result.json.partial", "w") as stream:
        json.dump(result, stream)
    os.replace("result.json.partial", "result.json")


def fonts():
    """Start from the font list the server built, so that matplotlib does not search the fonts again in every run."""
    if os.path.isdir(FONT_LIST):
        target = os.path.join(os.path.expanduser("~"), ".cache", "matplotlib")
        os.makedirs(target, exist_ok=True)
        for name in os.listdir(FONT_LIST):
            if name.startswith("fontlist-") and name.endswith(".json"):
                shutil.copyfile(os.path.join(FONT_LIST, name), os.path.join(target, name))


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    fonts()
    with open("input.json") as stream:
        request = json.load(stream)
    notebook = Notebook()
    namespace = {"__name__": "__notebook__", "nb": notebook}
    if request.get("parameters") is not None:
        namespace["P"] = Parameters(request["parameters"])
    result = {"cells": {}, "running": None, "finished": False}
    failed = None
    for cell in request["cells"]:
        if failed:
            result["cells"][cell["id"]] = {"status": "skipped", "stdout": "", "outputs": [], "after": failed}
            continue
        result["running"] = cell["id"]
        save(result)
        record = result["cells"][cell["id"]] = run_cell(cell["name"], cell["source"], namespace, notebook)
        if record["status"] == "error":
            failed = cell["name"]
    if request.get("scratch") is not None:
        result["running"] = "scratch"
        save(result)
        result["scratch"] = ({"status": "skipped", "stdout": "", "outputs": [], "after": failed} if failed
                             else run_cell("scratch", request["scratch"], namespace, notebook))
    result.update(running=None, finished=True)
    save(result)


if __name__ == "__main__":
    main()
