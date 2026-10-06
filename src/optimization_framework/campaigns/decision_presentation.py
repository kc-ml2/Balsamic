"""Readable decision summaries without changing saved choices or authority."""
from copy import deepcopy


AMBIGUOUS_CHOICES = {"follow the proposed direction", "provide a different direction"}


def brief(value, limit=420):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    prefix = text[:limit].rsplit(" ", 1)[0]
    return prefix + "…"


def action_choices(action):
    title = str(action.get("title") or "the proposed action")
    operation = action.get("command_operation")
    kind = action.get("kind")
    labels = {
        "draft.save": (title if title.lower().startswith("save ") else "Save the proposed experiment draft",
            "Save this experiment design for later review. This command does not launch the experiment."),
        "draft.launch": ("Launch the proposed experiment draft", "Launch this saved design subject to its readiness checks and current allocation."),
        "trial.create": ("Queue the proposed experiment", "Create and queue this specific experiment within the stated allocation and readiness checks."),
        "study.create": ("Create the proposed study", "Record this study and its protocol. Creating a study does not itself launch its trials."),
        "campaign.update": ("Approve the proposed campaign changes", "Apply the exact charter or resource changes described in this proposal."),
    }
    if kind == "command" and operation in labels:
        label, description = labels[operation]
    elif kind in {"review", "compare", "evolve"}:
        label = {"review": "Request this evidence review", "compare": "Request this comparison review",
            "evolve": "Request this proposal revision"}[kind]
        description = "Start the stated LLM research task. Any further execution remains subject to campaign permissions."
    elif kind == "search":
        label, description = "Search for the proposed literature", "Run the specified literature search and record its sources."
    elif kind == "implement":
        label = "Attach this implementation" if action.get("implementation_version_id") else "Request this implementation"
        description = "Submit this specific implementation request under its stated allocation and validation requirements."
    elif kind == "probe":
        label, description = "Run this proposed experiment", "Run the specified trial only after a concrete design and current approval are available."
    else:
        label, description = "Approve: " + brief(title, 130), "Apply only this proposed action under its recorded scope and current permissions."
    return [{"id": "accept", "label": label, "description": description},
        {"id": "defer", "label": "Defer this proposal", "description": "Record that you want to consider this proposal later; no work is authorized by this choice."},
        {"id": "reject", "label": "Decline this proposal", "description": "Record your rejection of this recommendation. This does not cancel running work or reject the entire method family."}]


def present_decision(record, action=None):
    """Keep original IDs and text; unsupported interpretations need a manager."""
    original_options = [deepcopy(option) if isinstance(option, dict) else
        {"id": str(index), "label": option, "description": ""}
        for index, option in enumerate(record.get("options", []))]
    ambiguous = [option["id"] for option in original_options
        if str(option.get("label", "")).strip().casefold() in AMBIGUOUS_CHOICES]
    structured = record.get("decision_format") == "structured"
    manager = not action and (record.get("audience") == "manager" or (
        not structured and str(record.get("title", "")).lstrip().casefold().startswith(("manager:", "for the campaign manager:"))))
    needs_clarification = bool(record.get("needs_clarification") or ambiguous or not original_options or
        action and action.get("kind") == "ask_researcher" or
        not action and record.get("title") == "Research reasoning budget needs attention")
    details = str(record.get("context") or record.get("rationale") or "")
    background_source = record.get("background") or details
    background = str(background_source) if structured else brief(background_source)
    proposal = str(record.get("proposal") or "")
    options = original_options
    scope = "Research direction in this campaign; this choice does not launch work or change limits."
    basis = "structured" if structured else "legacy"
    if action:
        basis = "structured" if structured else "action"
        proposal = proposal or str(action.get("title") or record.get("title") or "")
        # The question often describes a scientific objective rather than the
        # operation being authorized, so keep it as background, never a command.
        background_source = record.get("background") or action.get("rationale") or details
        background = str(background_source) if structured else brief(background_source)
        mapped = {item["id"]: item for item in action_choices(action)}
        options = [mapped[option["id"]] if option["id"] in mapped else option for option in original_options]
        scope = "Only this proposed action, under its current campaign permissions."
        payload = action.get("command_payload") or {}
        if action.get("command_operation") == "campaign.update":
            fields = (("compute_budget_seconds", "Aggregate worker-time cap", "seconds"),
                ("delegated_trial_seconds", "Per-experiment limit", "seconds"),
                ("validation_reserve_seconds", "Validation reserve", "seconds"),
                ("implementation_compute_budget_seconds", "Implementation allocation", "seconds"),
                ("llm_budget_usd", "LLM spending cap", "USD"))
            changes = [f"{label}: {payload[key]:g} {unit}" for key, label, unit in fields
                if key in payload and isinstance(payload[key], (int, float))]
            if changes:
                limits = "Proposed limits: " + "; ".join(changes) + "."
                if limits not in proposal:
                    proposal += "\n" + limits
    elif record.get("trial_id") and record.get("incremental_solver_calls") is not None:
        scope = "Only the stated extension of this experiment, subject to current limits."
    if manager:
        scope = "Internal follow-up for the campaign manager; no researcher decision is requested."
    if needs_clarification:
        proposal = proposal or "A concrete proposal and distinct choices have not been recorded. Ask the manager to clarify this request."
    elif not proposal:
        # Legacy non-action choices are guidance. Listing their exact alternatives
        # is honest; synthesizing a preferred plan from old prose would not be.
        proposal = "Choose the next direction from the alternatives below."
    result = {"title": record.get("title", "Decision"), "background": background,
        "background_is_excerpt": not structured and len(details) > len(background),
        "proposal": proposal, "options": options,
        "recommendation_reason": str(record.get("recommendation_reason") or ""),
        "scope_label": scope, "needs_clarification": needs_clarification,
        "audience": "manager" if manager else "researcher", "basis": basis, "details": details}
    if action:
        result["action_details"] = {key: deepcopy(action[key]) for key in (
            "id", "kind", "title", "question", "expected_information", "stopping_condition", "alternatives",
            "requires_researcher", "command_operation", "command_payload",
            "probe_scope", "task_id", "hypothesis_id", "algorithm", "algorithm_config", "seed", "budget_calls",
            "wall_seconds", "implementation_version_id", "implementation_spec", "implementation_compute_seconds",
            "implementation_api_budget_usd", "implementation_max_calls",
            "charter_version", "guidance_revision") if key in action and action[key] is not None}
    return result
