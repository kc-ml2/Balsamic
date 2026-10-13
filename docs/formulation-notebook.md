# Formulation notebook

A campaign can start before its problem is defined. The formulation notebook is where the
researcher and the lead agent write the problem down as it takes shape: what is decided, what is
given, the objective and each constraint, in words, as equations and as code. The conversation is
where they talk; the notebook is what stays.

Status on 14 October 2026: cells, editing, agreement, history, rendered equations, running the
code cells in a sandbox, equations generated from a Pyomo model, and export as Markdown or as a
marimo notebook are implemented.

The design follows marimo's agent-pair model. The notebook is the shared artifact, not the chat.
An agent reads a cell before it changes it. A batch of changes is applied together or not at all.
A name has one defining cell, so a reader can find where any value comes from. Code runs top to
bottom in a fresh process every time, so what a cell shows always follows from the cells above it.

## The page

**Formulation** in the dashboard shows the cells in order, beside the lead-agent conversation.

- A cell is text (Markdown with equations) or Python code. It has a short name, a status, a
  revision, and the name of who changed it last.
- **Agree** marks a cell as accepted. Only the researcher can do this.
- **Discuss** puts the cell's name and revision in the conversation's message box. Nothing is sent
  until the researcher sends it.
- **Edit** opens the cell's text. If the lead agent changes the cell meanwhile, the page says so
  and nothing typed is lost.
- **History** lists every revision with who made it and why, and can restore one.
- Cells that someone else changed since the last visit are marked new.
- **Run all** runs every code cell, top to bottom. Under each code cell the page shows what it
  printed, the value of its last line, the tables, equations and figures it showed, or its error.
  An output is marked out of date once its cell, or a code cell above it, has changed since the run.
- **Markdown** and **marimo notebook** download the notebook. The marimo file opens with
  `marimo edit`, where the cells run reactively and can be continued outside the dashboard.
- Saving an edit does not start the lead agent. Write in the conversation to involve it.

## Status of a cell

| Status | Meaning |
|---|---|
| `proposed` | Written down, not yet accepted by the researcher |
| `agreed` | The researcher accepted this revision |
| `open` | A question or an assumption that the researcher has to settle |

The counts of the three are the progress of the formulation. When an agent changes the text of an
agreed cell, the cell becomes proposed again and the change must carry a reason, which the page shows.

## For the agent

Work in the notebook when a request concerns what the problem is. A question about a cell is
answered in the reply; change a cell only when the request asks for it. In the reply, say what
changed and name the cells, for example `[c_budget](#formulation/c_budget)`, which links to the
cell. Do not repeat a cell's content in the conversation.

| Tool | Use |
|---|---|
| `notebook_read` | Without arguments: the outline (name, kind, status, revision, last author of every cell) and `changed_since_your_last_read`. With `cells`: those cells in full |
| `notebook_edit` | A batch of operations with a `reason`. Lead agent only |
| `notebook_scratch` | Runs the notebook's code cells and then your `code`, in one fresh process, and keeps nothing. Use it to try a constraint or a solve before writing it into a cell |
| `notebook_run` | Runs every code cell and keeps the outputs the researcher sees. Returns each cell's outcome. Lead agent only |
| `campaign_inspect` | Its `formulation` entry lists the cells changed since you last read them |

Operations of `notebook_edit`:

| `op` | Fields |
|---|---|
| `create` | `kind` (`markdown` or `code`), `source`; optional `name`, `status` (`proposed` or `open`), and `before` or `after` another cell |
| `update` | `cell`, `expected_revision`, and any of `source`, `name`, `status` |
| `delete` | `cell`, `expected_revision` |
| `move` | `cell`, and `before` or `after` another cell |

Rules:

1. Read before you write. `expected_revision` is the revision you read; if the cell changed since,
   the whole batch is refused and you read it again.
2. One batch per coherent change. If any operation is refused, none is applied.
3. Read the cells listed in `changed_since_your_last_read` before you build on them: the
   researcher may have corrected something.
4. A cell name is lower-case letters, digits and underscores, starting with a letter, at most 48
   characters. Choose names the researcher can say: `c_budget`, `p_daily_cap`, `q_rollover`.
5. Keep cells small. One idea per cell.
6. The campaign's task list may hold a placeholder problem (for example a quadratic benchmark)
   because a campaign cannot exist without one. It is not the problem being formulated.
7. After changing code, try it with `notebook_scratch`, write it with `notebook_edit`, then call
   `notebook_run` and read every cell's outcome. Fix what failed before you report. A cell that
   fails stops the run: the cells after it are not run.

## How to write a formulation

Use these sections, in this order. The page can create them as an outline.

| Section | Holds |
|---|---|
| The problem in words | What is decided, by whom, over which period, and what a good outcome is. No symbols |
| Sets and indices | What the model counts over: users $i \in I$, days $t \in T$ |
| Data and parameters | Every given number: symbol, meaning, value or range, and whether it is given, assumed or to be measured |
| Decisions | What the model chooses: symbol, unit, and whether it is a number, a whole number or a yes/no |
| Objective | The one quantity to maximise or minimise |
| Constraints | One constraint per pair of cells |
| A toy instance | A case small enough to check by hand, and the model's answer for it |
| Open questions | One question per cell, status `open` |

An item of the formulation is a pair of cells: a Markdown cell that states it in plain words and
then as an equation, followed by the code cell that implements it. For example:

Markdown cell `c_budget`:

```markdown
### Monthly budget

All tokens granted in the month stay within the allocation $B$.

$$\sum_{i \in I} \sum_{t \in T} x_{it} \le B$$

*Assumed:* tokens that are not used on a day are not carried over.
```

Code cell `c_budget_code`:

```python
m.budget = pyo.Constraint(rule=lambda m: sum(m.x[i, t] for i in m.I for t in m.T) <= m.B)
nb.show(m.budget)
```

The second line shows the equation that Pyomo generates from the constraint itself, under the one
written by hand. When the two differ, the code is not what the text says.

Give the toy instance numbers small enough to check by hand, solve it in a cell, and show the
answer with `nb.table`. A solved toy instance is how the researcher sees what the equations mean,
and an infeasible or unbounded one usually points at a missing constraint.

Guidance for a researcher who is not an optimization specialist:

- Say it in words first. An equation without a sentence is not finished.
- Define every symbol in the sets, parameters or decisions before using it.
- Mark each number as given by the researcher, assumed by you, or to be measured. An assumption
  that changes the model is an `open` cell until the researcher settles it.
- Ask one question per `open` cell, with the choices and what each would change in the model.
  Ask for what you cannot reasonably assume, not for everything.
- When two formulations are possible, write the simpler one and name the other as an open question.

## Writing equations

- `$x_i$` is an equation inside a sentence. `$$ … $$` is a displayed equation.
- Write `\$` for a dollar sign in text, as in `\$1.02`.
- `\( … \)` and `\[ … \]` are not rendered. `notebook_edit` warns when a cell uses them.
- Equations are typeset by KaTeX. Standard LaTeX math works; `\label`, `\href` and custom macros do not.

## How code cells run

- All code cells run together, in order, in one Python process that starts fresh for every run.
  Nothing carries over from an earlier run, and a cell cannot be run on its own.
- The sandbox has no network and no GPU, and sees nothing of the server but Python and its
  libraries. A run is stopped after 90 seconds: keep the toy instance small.
- `numpy`, `scipy` and `sympy` can be imported. `scipy.optimize.milp` solves mixed-integer linear
  programs and `scipy.optimize.linprog` linear ones.
- A cell shows what it prints and the value of its last line. For more, every cell has `nb`:

| Call | Shows |
|---|---|
| `nb.md(text)` | Markdown, with `$…$` and `$$…$$` as equations |
| `nb.tex(latex)` | One displayed equation, given without dollar signs |
| `nb.table(rows, headers)` | A table |
| `nb.show(value)` | A value in its richest form, for example a `sympy` expression as an equation |

- A `matplotlib` figure left open at the end of a cell is kept as a picture.
- The agents of a campaign share 3,600 seconds of notebook run time. The researcher's own runs are
  not counted against it.

## Writing the model with Pyomo

`pyomo` and the HiGHS solver are installed. Writing the model in Pyomo lets the notebook show each
constraint as the equation generated from its code.

```python
import pyomo.environ as pyo

m = pyo.ConcreteModel()
m.I = pyo.Set(initialize=["ana", "bo"])                  # users
m.T = pyo.RangeSet(1, 30)                                # days
m.B = pyo.Param(initialize=4_000_000, mutable=True)      # monthly allocation
m.x = pyo.Var(m.I, m.T, domain=pyo.NonNegativeReals)     # tokens granted
```

- Declare parameters with `mutable=True`. The equation then shows the symbol `B`; otherwise it
  shows the number.
- Give every constraint and the objective as `rule=`, a function of the model and its indices, not
  as `expr=`. A rule keeps its sums and its "for all"; an expression is expanded term by term.
- Build the model, its sets, parameters and variables in one cell. Then attach one constraint per
  cell and show it: `m.daily = pyo.Constraint(m.T, rule=…)` followed by `nb.show(m.daily)`.
- Each set's index is its first letter in lower case: `I` gives `i`, `T` gives `t`. To choose
  other symbols, pass them by component name: `nb.show(m.daily, symbols={"x": "q", "T": ["D", ["d"]]})`.
- `nb.show(m)` shows the whole model as one block: objective, constraints and bounds.
- Solve with `pyo.SolverFactory("appsi_highs").solve(m)` and read values with `pyo.value(m.x[i, t])`.
- Several cells may attach components to `m`: that changes the model, it does not define `m`
  again. The cells run in order, so a constraint cell sits below the cell that builds `m`.

`scipy.optimize.milp` remains available for a model given as matrices.

## Rules for code cells

- A public name is defined in one cell only: assigned, imported, or declared as a function or
  class. To change a value, edit its defining cell. A second definition is refused, naming both cells.
- Names that begin with an underscore are a cell's own and may repeat.
- `from module import *` is refused. Import the names used.
- Code that does not parse is refused with the line of the error.
- Put imports in one cell near the top.
- `nb` is the notebook's own name; a cell cannot define it.

## Limits

A notebook holds at most 200 cells, a cell at most 32,768 characters, and a batch at most 40
operations. A run lasts at most 90 seconds, and only the latest run's outputs are kept.

## What it does not do yet

- There is no live kernel: a cell cannot be run alone, and there are no interactive controls.
- An agreed formulation is not yet turned into a problem that campaign experiments can run.
