"""What an edit of a campaign's formulation notebook may ask for."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import Contract

CELL_NAME = r"^[a-z][a-z0-9_]{0,47}$"
SOURCE_CHARACTERS = 32768
OPERATIONS_PER_EDIT = 40
CellKind = Literal["markdown", "code"]
CellStatus = Literal["proposed", "agreed", "open"]
# A parameter's name is how code reads it (P.B) and may be a capital, as symbols are.
PARAMETER_NAME = r"^[A-Za-z][A-Za-z0-9_]{0,47}$"
PARAMETERS = 64
ParameterKind = Literal["number", "integer", "boolean", "choice", "text"]
ParameterBasis = Literal["given", "assumed", "to_measure"]
Value = bool | int | float | str
# What describes a parameter, as opposed to its value.
DESCRIPTOR = ("name", "label", "kind", "minimum", "maximum", "step", "unit", "choices", "symbol", "description", "basis", "origin", "prominent")
# What an update may remove, by sending it as null.
CLEARABLE = ("minimum", "maximum", "step", "unit", "choices", "symbol", "description", "origin")


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


class ParameterOperation(Contract):
    """One change of a parameter: a number or a choice of the problem, shown as a control and read by code as P.<name>.

    Only the shape is checked here; whether a value fits its kind and range is the notebook's to refuse.
    """
    op: Literal["parameter.declare", "parameter.update", "parameter.set", "parameter.delete", "parameter.move"]
    parameter: str | None = Field(default=None, pattern=PARAMETER_NAME)
    expected_revision: int | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, pattern=PARAMETER_NAME)
    label: str | None = Field(default=None, min_length=1, max_length=120)
    kind: ParameterKind | None = None
    value: Value | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=40)
    choices: list[str] | None = Field(default=None, min_length=1, max_length=40)
    symbol: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=2000)
    basis: ParameterBasis | None = None
    origin: str | None = Field(default=None, max_length=1000)
    prominent: bool | None = None
    before: str | None = Field(default=None, pattern=PARAMETER_NAME)
    after: str | None = Field(default=None, pattern=PARAMETER_NAME)

    @model_validator(mode="after")
    def fields_fit_the_operation(self):
        given = {key for key in ("parameter", "expected_revision", "value", "before", "after", *DESCRIPTOR) if getattr(self, key) is not None}
        if self.op == "parameter.update":
            given |= {key for key in CLEARABLE if key in self.model_fields_set}
        if self.before is not None and self.after is not None:
            raise ValueError("Place a parameter before one parameter or after one, not both")
        if self.op == "parameter.declare":
            if given - {*DESCRIPTOR, "value", "before", "after"} or not {"name", "label", "kind", "value"} <= given:
                raise ValueError("parameter.declare takes name, label, kind and value, and optionally the other descriptor fields and before or after")
        elif self.op == "parameter.update":
            if given - {*DESCRIPTOR, "parameter", "expected_revision"} or not {"parameter", "expected_revision"} <= given:
                raise ValueError("parameter.update takes parameter and expected_revision, and the descriptor fields to change")
            if not given & set(DESCRIPTOR):
                raise ValueError("parameter.update changes at least one descriptor field; parameter.set changes the value")
        elif self.op == "parameter.set":
            if given != {"parameter", "expected_revision", "value"}:
                raise ValueError("parameter.set takes parameter, expected_revision and value")
        elif self.op == "parameter.delete":
            if given != {"parameter", "expected_revision"}:
                raise ValueError("parameter.delete takes parameter and expected_revision")
        elif given != {"parameter", "before"} and given != {"parameter", "after"}:
            raise ValueError("parameter.move takes parameter and before or after")
        return self


Operation = Annotated[CellOperation | ParameterOperation, Field(discriminator="op")]


class FormulationEditInput(Contract):
    """A batch of cell and parameter changes, applied together or not at all."""
    operations: list[Operation] = Field(min_length=1, max_length=OPERATIONS_PER_EDIT)
    reason: str = Field(default="", max_length=2000)
