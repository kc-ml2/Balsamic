"""The notebook as a marimo file, to open, run and continue outside the dashboard.

A marimo notebook is a Python file in which every cell is a function: its parameters are the names
it takes from other cells and it returns the names it defines. The notebook's own rules (one defining
cell per name, no wildcard import) are marimo's, so every notebook that was saved can be written this way.

The notebook's parameters become marimo controls, one dictionary of them, and the cell after it gives
their values to the other cells as P, as in the dashboard. Moving a control there runs again the
cells that read P.
"""
import ast
import keyword
import textwrap

from .rules import definitions

# The marimo version whose file format this writes, and against which an export was checked.
FORMAT = "0.25.1"
SHIM = '''\
class nb:
    """What `nb` shows in the dashboard's notebook, done with marimo's own display."""

    @staticmethod
    def md(text):
        mo.output.append(mo.md(text))

    @staticmethod
    def tex(latex):
        mo.output.append(mo.md("$$" + latex + "$$"))

    @staticmethod
    def table(rows, headers=None):
        rows = [list(row) for row in rows]
        headers = list(headers) if headers is not None else [""] * (len(rows[0]) if rows else 0)
        lines = ["| " + " | ".join(map(str, headers)) + " |", "|" + " --- |" * len(headers)]
        mo.output.append(mo.md("\\n".join(lines + ["| " + " | ".join(map(str, row)) + " |" for row in rows])))

    @staticmethod
    def show(value, symbols=None):
        if type(value).__module__.partition(".")[0] == "pyomo" and hasattr(value, "model"):
            import re
            from pyomo.contrib.latex_printer import latex_printer
            latex = re.sub(r"\\\\label\\{[^}]*\\}", "", latex_printer(value)).replace("\\\\mathds", "\\\\mathbb")
            latex = re.sub(r"\\\\(begin|end)\\{equation\\}", "", latex) if "{align}" not in latex else latex.replace("{align}", "{aligned}")
            return mo.output.append(mo.md("$$" + latex + "$$"))
        mo.output.append(value)'''


def _function(name, parameters, body, returned=(), hidden=False):
    """One marimo cell: the cell's code as the body of a function."""
    body = textwrap.indent(body.rstrip(), "    ", lambda line: bool(line.strip()))
    result = f"({', '.join(returned)},)" if returned else ""
    return (f"@app.cell{'(hide_code=True)' if hidden else ''}\ndef {name}({', '.join(parameters)}):\n"
            f"{body or '    pass'}\n    return{' ' + result if result else ''}\n")


def _text(source):
    """A Markdown cell's text as the argument of mo.md, kept readable where the text allows it."""
    if '"""' in source:
        return f"mo.md({source!r})"
    return 'mo.md(r"""\n' + source.strip("\n") + '\n""")'


def _control(item):
    """One parameter as the marimo control that edits it."""
    label = item["label"] + (f" ({item['unit']})" if item.get("unit") else "")
    kind, value = item["kind"], item["value"]
    if kind == "boolean":
        return f"mo.ui.switch(value={value!r}, label={label!r})"
    if kind == "choice":
        return f"mo.ui.dropdown(options={list(item['choices'])!r}, value={value!r}, label={label!r})"
    if kind == "text":
        return f"mo.ui.text(value={value!r}, label={label!r})"
    step = item.get("step") if item.get("step") is not None else (1 if kind == "integer" else None)
    bounds = [f"{key}={item[name]!r}" for key, name in (("start", "minimum"), ("stop", "maximum")) if item.get(name) is not None]
    rest = [f"step={step!r}"] * (step is not None) + [f"value={value!r}", f"label={label!r}"]
    if item.get("minimum") is not None and item.get("maximum") is not None:
        return f"mo.ui.slider({', '.join(bounds + rest)}, show_value=True, include_input=True)"
    return f"mo.ui.number({', '.join(bounds + rest)})"


def _controls(parameters, taken):
    """The cells that hold the parameters' controls and give their values as P. Returns (cells, name of the controls)."""
    holder = next(name for name in ("parameters", "parameter_controls", "controls_for_P") if name not in taken)
    entries = "".join(f"\n    {item['name']!r}: {_control(item)}," for item in parameters)
    controls = f"{holder} = mo.ui.dictionary({{{entries}\n}}, label=\"Parameters\")\n{holder}"
    values = ("class _Parameters(dict):\n    \"\"\"The parameters' values, read as P.name or P[\"name\"].\"\"\"\n"
              "    __getattr__ = dict.__getitem__\n\n\n"
              f"P = _Parameters({holder}.value)")
    return [_function("_", ["mo"], controls, [holder]), _function("_", [holder], values, ["P"], hidden=True)], holder


def marimo_file(title, revision, cells, parameters=()):
    """The cells (name, kind, source), in order, and the parameters, as the text of a marimo notebook."""
    defined = {cell["name"]: definitions(cell["source"]) for cell in cells if cell["kind"] == "code"}
    every = set().union(*defined.values()) if defined else set()
    uses_nb = False
    shared = {"mo", "nb", "P"} if parameters else {"mo", "nb"}
    parts = [f"# The formulation notebook of {title!r}, revision {revision}, exported from Optimization Lab.\n"
             "# Open it with: marimo edit <this file>\nimport marimo\n\n"
             f"__generated_with = \"{FORMAT}\"\napp = marimo.App(width=\"medium\")\n",
             _function("_", [], "import marimo as mo", ["mo"])]
    reserved = {"mo", "nb", "app", "marimo"}
    if parameters:
        controls, holder = _controls(parameters, every)
        parts += controls
        reserved |= {"P", holder}
    functions = []
    for cell in cells:
        name = cell["name"]
        if keyword.iskeyword(name) or name in every or name in reserved:
            name = "cell_" + name   # a cell is a function in the file, and must not take a value's name
        if cell["kind"] == "markdown":
            functions.append(_function(name, ["mo"], _text(cell["source"]), hidden=True))
            continue
        read = {node.id for node in ast.walk(ast.parse(cell["source"])) if isinstance(node, ast.Name)}
        own = defined[cell["name"]]
        uses_nb = uses_nb or "nb" in read
        functions.append(_function(name, sorted((read & (every | shared)) - own), cell["source"], sorted(own)))
    if uses_nb:
        parts.append(_function("_", ["mo"], SHIM, ["nb"], hidden=True))
    return "\n\n".join(parts + functions + ['if __name__ == "__main__":\n    app.run()\n'])
