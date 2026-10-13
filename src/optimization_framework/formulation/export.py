"""The notebook as a marimo file, to open, run and continue outside the dashboard.

A marimo notebook is a Python file in which every cell is a function: its parameters are the names
it takes from other cells and it returns the names it defines. The notebook's own rules (one defining
cell per name, no wildcard import) are marimo's, so every notebook that was saved can be written this way.
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


def marimo_file(title, revision, cells):
    """The cells (name, kind, source), in order, as the text of a marimo notebook."""
    defined = {cell["name"]: definitions(cell["source"]) for cell in cells if cell["kind"] == "code"}
    every = set().union(*defined.values()) if defined else set()
    uses_nb = False
    parts = [f"# The formulation notebook of {title!r}, revision {revision}, exported from Optimization Lab.\n"
             "# Open it with: marimo edit <this file>\nimport marimo\n\n"
             f"__generated_with = \"{FORMAT}\"\napp = marimo.App(width=\"medium\")\n",
             _function("_", [], "import marimo as mo", ["mo"])]
    functions = []
    for cell in cells:
        name = cell["name"]
        if keyword.iskeyword(name) or name in every or name in {"mo", "nb", "app", "marimo"}:
            name = "cell_" + name   # a cell is a function in the file, and must not take a value's name
        if cell["kind"] == "markdown":
            functions.append(_function(name, ["mo"], _text(cell["source"]), hidden=True))
            continue
        read = {node.id for node in ast.walk(ast.parse(cell["source"])) if isinstance(node, ast.Name)}
        own = defined[cell["name"]]
        uses_nb = uses_nb or "nb" in read
        functions.append(_function(name, sorted((read & (every | {"mo", "nb"})) - own), cell["source"], sorted(own)))
    if uses_nb:
        parts.append(_function("_", ["mo"], SHIM, ["nb"], hidden=True))
    return "\n\n".join(parts + functions + ['if __name__ == "__main__":\n    app.run()\n'])
