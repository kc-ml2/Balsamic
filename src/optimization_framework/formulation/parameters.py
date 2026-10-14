"""Parameters: the given and assumed numbers and choices of a problem.

Each is shown on the Formulation page as a control the researcher can move, and code reads it as
P.<name>. They are stored as an ordered list on the notebook's head. Their descriptions map onto the
small JSON schema that a declared problem's configuration accepts, so the values of a formulation
can become the configuration of the problem it turns into.
"""
import math

from optimization_framework.contracts.formulation import PARAMETERS   # noqa: F401  (the limit, kept beside its rules)

TEXT_CHARACTERS = 2000
SCHEMA_TYPES = {"number": "number", "integer": "integer", "boolean": "boolean", "choice": "string", "text": "string"}


def check_descriptor(parameter):
    """Refuse a description that its kind cannot have. Raises ValueError naming the parameter."""
    name, kind = parameter["name"], parameter["kind"]
    numeric = kind in {"number", "integer"}
    for key in ("minimum", "maximum", "step"):
        if parameter.get(key) is not None and not numeric:
            raise ValueError(f"Parameter {name} is a {kind}: {key} belongs to a number or an integer")
        if parameter.get(key) is not None and not math.isfinite(parameter[key]):
            raise ValueError(f"Parameter {name}: {key} must be a finite number")
    if numeric and parameter.get("minimum") is not None and parameter.get("maximum") is not None and parameter["minimum"] > parameter["maximum"]:
        raise ValueError(f"Parameter {name}: its minimum is above its maximum")
    if kind == "choice":
        choices = parameter.get("choices") or []
        if len(choices) < 2 or len(set(choices)) != len(choices):
            raise ValueError(f"Parameter {name} is a choice: give at least two different choices")
    elif parameter.get("choices"):
        raise ValueError(f"Parameter {name} is a {kind}: only a choice has choices")


def check_value(parameter, value):
    """The value, as stored, if it fits the parameter's kind, range and choices. Raises ValueError otherwise."""
    name, kind = parameter["name"], parameter["kind"]
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Parameter {name} is yes or no: give true or false")
        return value
    if kind in {"choice", "text"}:
        if not isinstance(value, str):
            raise ValueError(f"Parameter {name} takes text")
        if kind == "choice" and value not in parameter.get("choices", []):
            raise ValueError(f"Parameter {name} is one of {', '.join(parameter.get('choices', []))}, not {value!r}")
        if len(value) > TEXT_CHARACTERS:
            raise ValueError(f"Parameter {name} holds at most {TEXT_CHARACTERS} characters")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Parameter {name} takes a finite number")
    if kind == "integer":
        if float(value) != int(value):
            raise ValueError(f"Parameter {name} takes a whole number, not {value}")
        value = int(value)
    low, high = parameter.get("minimum"), parameter.get("maximum")
    if (low is not None and value < low) or (high is not None and value > high):
        bounds = f"from {low:g}" * (low is not None) + " " * (low is not None and high is not None) + f"to {high:g}" * (high is not None)
        raise ValueError(f"Parameter {name} is {bounds}; {value:g} is outside it")
    return value


def values(parameters):
    """The parameters' values by name, as code reads them."""
    return {parameter["name"]: parameter["value"] for parameter in parameters}


def schema(parameters):
    """The parameters as the flat JSON schema of a declared problem's configuration."""
    properties = {}
    for parameter in parameters:
        rule = {"type": SCHEMA_TYPES[parameter["kind"]], "title": parameter["label"], "default": parameter["value"]}
        if parameter.get("description"):
            rule["description"] = parameter["description"]
        for key in ("minimum", "maximum"):
            if parameter.get(key) is not None:
                rule[key] = parameter[key]
        if parameter["kind"] == "choice":
            rule["enum"] = list(parameter["choices"])
        properties[parameter["name"]] = rule
    return {"type": "object", "properties": properties}
