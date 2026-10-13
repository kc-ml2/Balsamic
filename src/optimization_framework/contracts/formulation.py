"""What an edit of a campaign's formulation notebook may ask for."""
from typing import Literal

from pydantic import Field, model_validator

from .base import Contract

CELL_NAME = r"^[a-z][a-z0-9_]{0,47}$"
SOURCE_CHARACTERS = 32768
OPERATIONS_PER_EDIT = 40
CellKind = Literal["markdown", "code"]
CellStatus = Literal["proposed", "agreed", "open"]


class CellOperation(Contract):
    """One change of a batch. `cell`, `before` and `after` name a cell by its name or its id."""
    op: Literal["create", "update", "delete", "move"]
    cell: str | None = Field(default=None, min_length=1, max_length=80)
    expected_revision: int | None = Field(default=None, ge=1)
    kind: CellKind | None = None
    name: str | None = Field(default=None, pattern=CELL_NAME)
    source: str | None = Field(default=None, min_length=1, max_length=SOURCE_CHARACTERS)
    status: CellStatus | None = None
    before: str | None = Field(default=None, min_length=1, max_length=80)
    after: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def fields_fit_the_operation(self):
        given = {key for key in ("cell", "expected_revision", "kind", "name", "source", "status", "before", "after")
                 if getattr(self, key) is not None}
        if self.before is not None and self.after is not None:
            raise ValueError("Place a cell before one cell or after one, not both")
        if self.op == "create":
            if given - {"kind", "name", "source", "status", "before", "after"} or not {"kind", "source"} <= given:
                raise ValueError("create takes kind and source, and optionally name, status and before or after")
        elif self.op == "update":
            if given - {"cell", "expected_revision", "name", "source", "status"} or not {"cell", "expected_revision"} <= given:
                raise ValueError("update takes cell and expected_revision, and the name, source or status to change")
            if not given & {"name", "source", "status"}:
                raise ValueError("update changes a name, a source or a status")
        elif self.op == "delete":
            if given != {"cell", "expected_revision"}:
                raise ValueError("delete takes cell and expected_revision")
        elif given != {"cell", "before"} and given != {"cell", "after"}:
            raise ValueError("move takes cell and before or after")
        return self


class FormulationEditInput(Contract):
    """A batch of cell changes, applied together or not at all."""
    operations: list[CellOperation] = Field(min_length=1, max_length=OPERATIONS_PER_EDIT)
    reason: str = Field(default="", max_length=2000)
