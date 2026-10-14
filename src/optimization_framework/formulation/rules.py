"""The notebook's code rules, checked before a cell is saved.

They are marimo's: a public name has one defining cell, and nothing is imported with a wildcard. A
reader can then find where any value comes from, and the notebook exports as a valid marimo file.
Names that begin with an underscore are a cell's own and are exempt.

Once a notebook has parameters, `P` is theirs: code reads them as P.name or P["name"], and only
names that are declared.
"""
import ast


class _Bindings(ast.NodeVisitor):
    """Names a cell binds at module level. Function, class and lambda bodies have their own scope."""

    def __init__(self):
        self.names, self.wildcard = set(), False

    def visit_FunctionDef(self, node):
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    def _comprehension(self, node):
        # A comprehension's own variables stay inside it; a walrus target lands in the enclosing scope.
        self.names.update(part.target.id for part in ast.walk(node) if isinstance(part, ast.NamedExpr))

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_Import(self, node):
        self.names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == "*":
                self.wildcard = True
            else:
                self.names.add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node):
        for statement in node.body:   # the handler's own name is unbound again when it ends
            self.visit(statement)

    def visit_MatchAs(self, node):
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node):
        if node.name:
            self.names.add(node.name)

    def visit_MatchMapping(self, node):
        if node.rest:
            self.names.add(node.rest)
        self.generic_visit(node)


def definitions(source):
    """The public names a code cell defines. Raises ValueError for code that does not parse or imports with *."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(f"line {error.lineno}: {error.msg}") from None
    found = _Bindings()
    found.visit(tree)
    if found.wildcard:
        raise ValueError("import the names it uses instead of *")
    return {name for name in found.names if not name.startswith("_")}


def reads(source):
    """The parameters a code cell reads by name, and whether it also uses P in a way that could read any of them."""
    names, dynamic = set(), False
    tree = ast.parse(source)
    named = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "P":
            names.add(node.attr)
            named.add(id(node.value))
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "P"
              and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            names.add(node.slice.value)
            named.add(id(node.value))
    dynamic = any(isinstance(node, ast.Name) and node.id == "P" and id(node) not in named for node in ast.walk(tree))
    return names, dynamic


def usage(cells, names):
    """For each parameter, the code cells that read it. A cell that uses P as a whole reads every one."""
    found = {name: [] for name in names}
    for cell in cells:
        if cell["kind"] != "code":
            continue
        try:
            read, dynamic = reads(cell["source"])
        except SyntaxError:
            continue
        for name in found:
            if dynamic or name in read:
                found[name].append(cell["name"])
    return found


def check(cells, parameters=()):
    """Refuse code cells that break a rule. `cells` are the notebook's cells in order; `parameters` the names declared."""
    owners = {}
    # A notebook written before parameters existed may define P itself; it keeps that name until it declares one.
    reserved = bool(parameters)
    for cell in cells:
        if cell["kind"] != "code":
            continue
        try:
            names = definitions(cell["source"])
        except ValueError as error:
            raise ValueError(f"Cell {cell['name']} is not saved: {error}") from None
        if "nb" in names:
            raise ValueError(f"Cell {cell['name']} is not saved: nb is the notebook's own name in every cell (nb.md, nb.table, nb.show); use another name")
        if reserved and "P" in names:
            raise ValueError(f"Cell {cell['name']} is not saved: P holds the notebook's parameters (P.name); use another name")
        if reserved:
            read, _ = reads(cell["source"])
            missing = sorted(read - set(parameters))
            if missing:
                raise ValueError(f"Cell {cell['name']} reads {', '.join('P.' + name for name in missing)}, which is not a parameter. "
                                 "Declare it in the same batch, or change the cell.")
        for name in sorted(names):
            if name in owners:
                raise ValueError(f"`{name}` is defined in both {owners[name]} and {cell['name']}. A name has one defining cell: "
                                 "change it there, or use another name. Names that begin with _ are a cell's own.")
            owners[name] = cell["name"]
    return owners
