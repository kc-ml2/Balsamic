"""The standard outline of a formulation: the headings an empty notebook can start from."""
import re

OUTLINE = [
    ("problem", "## The problem in words\n\nWhat is decided, by whom, over which period, and what a good outcome is. No symbols yet."),
    ("sets", "## Sets and indices\n\nThe things the model counts over, each with its index: for example users $i \\in I$ and days $t \\in T$."),
    ("parameters", "## Data and parameters\n\nEvery number the model takes is a parameter, listed at the top of this page with its value, "
                   "range, unit and source: given, assumed or to be measured. Code reads it as `P.name`. "
                   "Say here what the numbers mean together and where they come from."),
    ("decisions", "## Decisions\n\nWhat the model chooses, each with its symbol, its unit, and whether it is a number, a whole number or a yes/no."),
    ("objective", "## Objective\n\nThe one quantity to maximise or minimise, in words and then as an expression."),
    ("constraints", "## Constraints\n\nOne constraint per cell below this one: a sentence saying what it prevents, then the equation, then its code."),
    ("toy_instance", "## A toy instance\n\nA case small enough to check by hand, and what the model answers for it."),
    ("open_questions", "## Open questions\n\nWhat has to be answered before the formulation is complete. One question per cell below this one."),
]


def outline(skip=()):
    """Create-operations for the outline, in order; each cell stays open until it has content."""
    return [{"op": "create", "kind": "markdown", "name": name, "source": source, "status": "open"}
            for name, source in OUTLINE if name not in skip]


def seed(description):
    """The create-operation that opens a campaign's notebook with the researcher's description, as written.

    A dollar sign is written as \\$ so that amounts are not read as equations. The cell is agreed: it is the
    researcher's own text, and an agent that changes it must give a reason and propose it again.
    """
    text = re.sub(r"(?<!\\)\$", r"\\$", description.strip())
    return [{"op": "create", "kind": "markdown", "name": "problem", "status": "agreed",
             "source": "## The problem in words\n\n" + text}]
