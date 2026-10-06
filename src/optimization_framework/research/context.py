"""Bound the whole evidence view, not just the campaign-memory subsection."""
from copy import deepcopy
import json
import re

from optimization_framework.contracts.base import content_hash


# The working target leaves room for new instructions and role responses. The
# hard ceiling accommodates an explicitly bounded comparison cohort without
# discarding its configurations or measured outcomes to preserve old metadata.
LIMIT = 224 * 1024
WORKING_LIMIT = 155 * 1024
DISCOVERY_LIMIT = 180 * 1024
_SCHEMA_MAPS = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas", "dependencies"}
_SCHEMA_CHILDREN = {"allOf", "anyOf", "oneOf", "prefixItems", "items", "additionalItems", "additionalProperties",
    "contains", "if", "then", "else", "not", "unevaluatedProperties", "unevaluatedItems", "propertyNames", "contentSchema"}


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def trim_memory(memory, allowance):
    if size(memory) <= allowance or not memory.get("structured"):
        return
    memory["document"] = "Current guidance and constraints are retained in the structured context; complete history remains in the versioned export."
    structured = memory["structured"]
    memory.setdefault("history_counts", {key: len(structured.get(key, [])) for key in ("findings", "counterevidence", "reuse_decisions", "source_ids")})
    structured["source_ids"] = []
    for key in ("findings", "counterevidence", "reuse_decisions"):
        while structured.get(key) and size(memory) > allowance:
            structured[key].pop(0)
    while memory["retrieved_records"] and size(memory) > allowance:
        memory["retrieved_records"].pop()
    # Closed discovery cycles are historical evidence, not today's authority.
    # Preserve their identities while freeing room for current science and the
    # next researcher message. Active tasks and session policies remain exact.
    discovery = structured.get("discovery", {})
    if size(memory) > allowance:
        for session in discovery.get("sessions", []):
            if session.get("status") in {"completed", "stopped"} and "policy" in session:
                session.pop("policy")
                session["historical_policy_record_id"] = session["id"]
    if size(memory) > allowance:
        for artifact in discovery.get("recent_artifacts", []):
            if artifact.pop("limitations", None) is not None:
                artifact["limitations_in_saved_record"] = True
    if size(memory) > allowance and discovery.get("sessions") and all(
            session.get("status") in {"completed", "stopped"} for session in discovery["sessions"]):
        discovery["recent_artifacts"] = [{key: artifact[key] for key in ("id", "kind", "stale") if key in artifact}
            for artifact in discovery.get("recent_artifacts", [])]
        for task in discovery.get("active_tasks", []):
            brief = task.pop("brief", None)
            if brief is not None:
                task["brief_record_id"] = task["id"]
                task["role"] = brief.get("role")
                task["stage"] = brief.get("stage")
        discovery["history_projection"] = (
            "Closed discovery cycle artifact bodies, titles, limitations and outstanding task briefs remain in their saved "
            "records. These identity/status indexes are not supplied scientific findings or resolved-task claims.")


def _deduplicate_current_context(context, question):
    """Reference exact repeated text without reinterpreting a user decision."""
    memory = context.get("manager_context", {})
    for action in memory.get("structured", {}).get("next_actions", []):
        title = action.get("title", "")
        if question and title == question:
            action["title"] = "Current researcher request (full text supplied in researcher_request.message)"
            action["title_reference"] = "researcher_request.message"
        elif action.get("id", "").startswith("turn_") and len(title) > 256:
            # This field is an inbox label, not the command's authoritative
            # request. Older blocked requests must not inject their entire
            # prompts into every subsequent turn as navigation metadata.
            action["title"] = title.splitlines()[0][:160]
            action["title_is_excerpt"] = True
            action["full_request_record_id"] = action["id"]
    if question:
        for message in context.get("history", []):
            if message.get("content") == question:
                message.pop("content")
                message["content_reference"] = "researcher_request.message"
    supplied = {}
    for decision in context.get("decisions", []):
        if decision.get("status") not in {"pending", "executing"}:
            continue
        for field in ("rationale", "context"):
            text = decision.get(field)
            if not isinstance(text, str) or not text:
                continue
            if text in supplied:
                decision.pop(field)
                decision.setdefault("supplied_text_references", {})[field] = supplied[text]
            else:
                supplied[text] = {"decision_id": decision["id"], "field": field}


def _share_decision_context(context):
    """Losslessly supply selected historical records once in a batch review."""
    selected = context.get("decision_refresh", {}).get("decisions", [])
    if not selected:
        return
    originals = {item["decision"]["id"]: (index, item["decision"]) for index, item in enumerate(selected)}
    for index, current in enumerate(context.get("decisions", [])):
        source = originals.get(current["id"])
        if source is None:
            # This turn explicitly selects recommendations to reconsider.
            # Other unanswered informational questions stay visible, but their
            # repeated model analyses are retrievable history, not authority.
            if not current.get("action_id") and current.get("incremental_solver_calls") is None:
                omitted = [key for key in ("rationale", "context") if key in current]
                if omitted:
                    for key in omitted:
                        current.pop(key)
                    current["omitted_narrative_fields"] = omitted
                    current["narrative_record_id"] = current["id"]
            continue
        source_index, original = source
        context["decisions"][index] = {key: current[key] for key in
            ("id", "status", "title", "task_id", "locked") if key in current}
        context["decisions"][index]["supplied_record_reference"] = {
            "json_pointer": f"#/decision_refresh/decisions/{source_index}/decision",
            "overrides": {key: value for key, value in current.items() if key not in original or original[key] != value},
            "removed_fields": [key for key in original if key not in current]}
    # Narratives repeat across action rationales, decision context and option
    # descriptions. Preserve every exact byte in one supplied text registry.
    locations = {}
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"rationale", "context", "description", "expected_information", "question", "comment"} and isinstance(item, str) and len(item) >= 160:
                    locations.setdefault(item, []).append((value, key))
                elif isinstance(item, (dict, list)):
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(selected)
    collect(context.get("decisions", []))
    shared = {}
    for text, owners in locations.items():
        if len(owners) < 2:
            continue
        identity = content_hash(text)
        shared[identity] = text
        for owner, key in owners:
            owner[key] = {"$ref": "#/shared_decision_text/" + identity}
    if shared:
        context["shared_decision_text"] = shared
    context["decision_reference_basis"] = (
        "Exact selected historical decision/action snapshots are supplied in decision_refresh. For a current decision with "
        "supplied_record_reference, resolve its JSON pointer, remove removed_fields, then apply overrides to reconstruct the "
        "complete current record. Text $ref objects resolve to exact strings in shared_decision_text. These are lossless prompt "
        "references, not rewritten authority or omitted evidence. Expand references before composing any command payload.")
    context["decision_reference_basis"] += (
        " Unselected informational questions retain their exact title/question, options, status and user comments; "
        "omitted_narrative_fields lists historical model analysis available in narrative_record_id. It was not supplied "
        "or adjudicated by this selected review. Other executable decisions remain supplied.")


def _index_informational_decision_history(context, question, target_id):
    """Old unanswered questions are not current authority or measured evidence.

    Keep exact questions, choices and researcher answers. When space is tight,
    index their historical model commentary instead of replaying it forever.
    Executable decisions and explicitly selected decisions remain complete.
    """
    decisions = {row["id"]: row for row in context.get("decisions", [])}
    selected = {item["decision"]["id"] for item in context.get("decision_refresh", {}).get("decisions", [])}
    selected.add(target_id)
    omitted = {identity for identity, row in decisions.items()
        if row.get("status") == "pending" and not row.get("action_id")
        and row.get("incremental_solver_calls") is None and identity not in selected and identity not in question}
    omitted = {identity for identity in omitted if not identity.startswith("reconcile_")
        and not any(option.get("id") == "close_reserved" for option in decisions[identity].get("options", []))}
    if not omitted:
        return

    def supplied(identity, field, seen=()):
        key = identity, field
        if key in seen:
            raise ValueError("Decision text reference cycle")
        row = decisions[identity]
        if field in row:
            return row[field]
        reference = row["supplied_text_references"][field]
        return supplied(reference["decision_id"], reference["field"], (*seen, key))

    # Preserve any exact text still needed by another, executable decision.
    # Existing prompt references must never point to an indexed-away field.
    for identity, row in decisions.items():
        if identity in omitted:
            continue
        references = row.get("supplied_text_references", {})
        for field, reference in list(references.items()):
            if reference["decision_id"] in omitted:
                row[field] = deepcopy(supplied(reference["decision_id"], reference["field"]))
                references.pop(field)
        if not references:
            row.pop("supplied_text_references", None)
    for identity in omitted:
        row = decisions[identity]
        references = row.get("supplied_text_references", {})
        fields = [field for field in ("rationale", "context") if field in row or field in references]
        if not fields:
            continue
        for field in fields:
            row.pop(field, None)
            references.pop(field, None)
        if not references:
            row.pop("supplied_text_references", None)
        row["omitted_narrative_fields"] = sorted(set(row.get("omitted_narrative_fields", []) + fields))
        row["narrative_record_id"] = identity
    context["decision_reference_basis"] = context.get("decision_reference_basis", "") + (
        " Unselected informational decisions retain exact questions, choices, statuses and researcher answers. "
        "Their omitted_narrative_fields are historical model commentary saved in narrative_record_id, not supplied "
        "evidence or current constraints. Indexing does not answer or resolve a question. Executable decisions and "
        "explicitly selected decision records remain supplied; do not infer authorization from an unanswered question.")


def _inventory(context):
    """Keep executable identities and measured outcomes when detailed rows shrink.

    This is an exact field projection, not a model-authored scientific summary.
    In particular, a missing library version does not mean a bundled method is
    missing, and a narrative trial question does not identify its algorithm.
    """
    proposal_fields = {"id", "title", "candidate_id", "family_id", "algorithm", "algorithm_config",
        "implementation_version_id", "parent_ids", "status", "claim_level", "concept_review"}
    trial_fields = {"id", "task_id", "task_split", "locked", "confirmation_released", "study_id",
        "hypothesis_id", "algorithm", "algorithm_config", "implementation_version_id", "builtin_implementation_id",
        "evaluator_version_id", "evaluator_eligibility", "seed", "max_steps", "schedule_steps", "wall_seconds",
        "status", "reason", "dependencies", "initial_assets", "reuse_decision_ids", "experiment_spec_id"}
    trial_fields.add("completion")
    measurement_fields = {"best_objective", "best_efficiency", "objective_definition", "evaluations", "solver_calls",
        "budget_requests", "cache_hits", "confirmed_observations", "elapsed_seconds", "scientific_complete",
        "allocation_stop", "reason", "unknown_solver_cost", "unknown_worker_cost", "interrupted_requests",
        "training_updates", "learning_updates"}
    tasks = {row["id"]: row for row in context.get("tasks", [])}
    proposals = []
    for row in context.get("hypotheses", []):
        item = {key: value for key, value in row.items() if key in proposal_fields}
        item["implementation_readiness"] = context.get("implementation_readiness", {}).get(row["id"])
        item["review_ids"] = [review["id"] for review in row.get("reviews", []) if isinstance(review, dict) and review.get("id")]
        proposals.append(item)
    trials = []
    for row in context.get("trials", []):
        item = {key: value for key, value in row.items() if key in trial_fields}
        if row.get("training") and row.get("algorithm") not in {
                "random", "hillclimb", "block_tabu", "annealing", "population", "surrogate"}:
            item["training"] = row["training"]
        result = row.get("result") or row.get("progress") or {}
        item["measurement"] = {key: value for key, value in result.items() if key in measurement_fields}
        if isinstance(result.get("diagnostics"), dict):
            item["measurement"]["diagnostics"] = {key: value for key, value in result["diagnostics"].items()
                if value is None or isinstance(value, (str, int, float, bool))}
        item["measurement_basis"] = "result" if row.get("result") else "progress" if row.get("progress") else "not_available"
        problem = row.get("problem") or {}
        task = tasks.get(row.get("task_id"), {})
        objective = item["measurement"].get("objective_definition")
        if objective and objective == (task.get("problem") or {}).get("primary_objective"):
            item["measurement"].pop("objective_definition")
            item["measurement"]["objective_definition_task_reference"] = task["id"]
        if problem and problem == task.get("problem"):
            item["problem_task_reference"] = task["id"]
        else:
            item["problem_identity"] = {key: problem[key] for key in
                ("definition_id", "definition_version", "evaluator_id", "evaluator_version", "scientific_identity",
                 "primary_objective", "fidelity") if key in problem}
        if row.get("physics"):
            if row["physics"] == task.get("physics"):
                item["physics_task_reference"] = task["id"]
            else:
                item["physics"] = row["physics"]
        trials.append(item)
    return {"basis": "Exact saved identity, settings, readiness and measurement fields. Detailed mechanisms, reviews, source passages, "
            "trajectories and omitted fields are not supplied by this index. Readiness is current at context creation; "
            "it does not establish effectiveness or replace execution-time checks. A trial's algorithm/configuration, "
            "not its question or title, identifies what actually ran.",
        "proposals": proposals, "trials": trials}


def _compact_active_trials(context):
    inventory = {row["id"]: row for row in context["research_inventory"]["trials"]}
    active = {"queued", "running", "pausing", "stopping", "paused"}
    for index, trial in enumerate(context.get("trials", [])):
        if trial.get("status") not in active:
            continue
        row = deepcopy(inventory[trial["id"]])
        basis = row.pop("measurement_basis")
        measurement = row.pop("measurement")
        if basis in {"result", "progress"}:
            row[basis] = measurement
        # Preserve training settings for methods that can use them; the legacy
        # trial contract also stores irrelevant DQN defaults on classical jobs.
        if trial.get("training") and trial.get("algorithm") not in {
                "random", "hillclimb", "block_tabu", "annealing", "population", "surrogate"}:
            row["training"] = trial["training"]
        for field in ("control_revision", "attempt", "execution_grant_id", "absolute_deadline"):
            if field in trial:
                row[field] = trial[field]
        row["context_projection"] = "Exact active work settings and measurement fields; full history remains in the saved trial record."
        if size(row) >= size(trial):
            continue
        context["trials"][index] = row
        inventory[trial["id"]] = {key: row[key] for key in
            ("id", "task_id", "task_split", "locked", "confirmation_released", "status") if key in row}
        inventory[trial["id"]]["supplied_trial_reference"] = row["id"]
    context["research_inventory"]["trials"] = list(inventory.values())


def _schema_without_titles(value):
    """Drop schema display labels, retaining property names and all constraints."""
    if isinstance(value, list):
        return [_schema_without_titles(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "title":
            continue
        # These maps contain user property/definition names, not schema keywords.
        if key in _SCHEMA_MAPS and isinstance(item, dict):
            result[key] = {name: _schema_without_titles(schema) for name, schema in item.items()}
        elif key in _SCHEMA_CHILDREN:
            result[key] = _schema_without_titles(item)
        else:
            result[key] = item
    return result


def _map_schema(schema, visit):
    if isinstance(schema, list):
        return [_map_schema(item, visit) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    for key, item in schema.items():
        if key in _SCHEMA_MAPS and isinstance(item, dict):
            result[key] = {name: _map_schema(value, visit) for name, value in item.items()}
        elif key in _SCHEMA_CHILDREN:
            result[key] = _map_schema(item, visit)
    return visit(result)


def _share_command_definitions(context):
    """Factor exact JSON Schema definitions into supplied, resolvable resources."""
    registry = {}
    for operation in context.get("application_commands", {}).values():
        schema = operation.get("payload_schema", {})
        definitions = schema.get("$defs", {})
        if not definitions:
            continue
        unsupported = []

        def references(node):
            if "$id" in node or "$anchor" in node or "$dynamicRef" in node:
                unsupported.append(True)
            ref = node.get("$ref", "")
            if ref.startswith("#") and not ref.startswith("#/$defs/"):
                unsupported.append(True)
            return node

        _map_schema(schema, references)
        if unsupported:
            continue  # Preserve uncommon nested scopes and recursive roots intact.

        def ref_name(ref):
            from urllib.parse import unquote
            name, _, suffix = ref.removeprefix("#/$defs/").partition("/")
            return unquote(name).replace("~1", "/").replace("~0", "~"), suffix

        dependencies = {}
        for name, definition in definitions.items():
            found = set()

            def collect(node):
                if node.get("$ref", "").startswith("#/$defs/"):
                    found.add(ref_name(node["$ref"])[0])
                return node

            _map_schema(definition, collect)
            dependencies[name] = found
        if any(name not in definitions for names in dependencies.values() for name in names):
            continue
        identifiers = {}
        for name in definitions:
            closure, pending = set(), [name]
            while pending:
                current = pending.pop()
                if current not in closure:
                    closure.add(current)
                    pending.extend(dependencies[current])
            signature = {"definition": definitions[name], "dependencies": {
                key: definitions[key] for key in sorted(closure - {name})}}
            identifiers[name] = "urn:opt:def:" + content_hash(signature)[:24]

        def rewrite(node):
            ref = node.get("$ref", "")
            if ref.startswith("#/$defs/"):
                name, suffix = ref_name(ref)
                if name in identifiers:
                    node["$ref"] = identifiers[name] + ("#/" + suffix if suffix else "")
            return node

        for name, definition in definitions.items():
            value = {**_map_schema(definition, rewrite), "$id": identifiers[name]}
            previous = registry.get(identifiers[name])
            if previous is not None and previous != value:
                raise ValueError("Shared command schema identity collision")
            registry[identifiers[name]] = value
        operation["payload_schema"] = _map_schema({key: value for key, value in schema.items() if key != "$defs"}, rewrite)
    if registry:
        # A definition used once need not pay for a resource name and reference.
        # Inline it after all references have absolute identities; shared and
        # recursive definitions stay in the registry with their exact semantics.
        while True:
            uses = {identity: [] for identity in registry}

            def count(node):
                ref = node.get("$ref", "")
                identity = ref.partition("#")[0]
                if identity in uses:
                    uses[identity].append(ref)
                return node

            for operation in context.get("application_commands", {}).values():
                _map_schema(operation.get("payload_schema", {}), count)
            for definition in registry.values():
                _map_schema(definition, count)
            single = next((identity for identity, refs in uses.items() if refs == [identity]), None)
            if single is None:
                break
            value = {key: item for key, item in registry[single].items() if key != "$id"}

            def inline(node):
                if node.get("$ref") != single:
                    return node
                siblings = {key: item for key, item in node.items() if key != "$ref"}
                return {"allOf": [deepcopy(value), siblings]} if siblings else deepcopy(value)

            for operation in context.get("application_commands", {}).values():
                operation["payload_schema"] = _map_schema(operation.get("payload_schema", {}), inline)
            registry = {identity: _map_schema(definition, inline) for identity, definition in registry.items() if identity != single}
        context["application_schema_definitions"] = registry
        context["application_schema_reference_basis"] = (
            "Every absolute urn:opt:def $ref in application_commands resolves to the supplied schema with that $id "
            "in application_schema_definitions. These are exact shared JSON Schema definitions, not omitted schemas.")


def _share_trial_settings(context):
    """Supply repeated large training settings once, with explicit JSON pointers."""
    locations = []

    def study_values(value):
        if isinstance(value, list):
            for item in value:
                study_values(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in {"training", "algorithm_config"} and isinstance(item, dict) and size(item) >= 256:
                    locations.append((value, key, item))
                else:
                    study_values(item)

    study_values(context.get("active_study", {}))
    for trial in [*context.get("trials", []), *context.get("research_inventory", {}).get("trials", [])]:
        if isinstance(trial.get("training"), dict) and size(trial["training"]) >= 256:
            locations.append((trial, "training", trial["training"]))
    groups = {}
    for owner, key, value in locations:
        groups.setdefault(content_hash(value), []).append((owner, key, value))
    shared = {}
    for identity, group in groups.items():
        if len(group) < 2:
            continue
        shared[identity] = deepcopy(group[0][2])
        for owner, key, value in group:
            if value != shared[identity]:
                raise ValueError("Shared trial setting identity collision")
            owner[key] = {"$ref": "#/shared_trial_settings/" + identity}
    if shared:
        context["shared_trial_settings"] = shared
        context["shared_trial_settings_basis"] = (
            "Repeated exact study/trial parameter objects are supplied once. Resolve these JSON pointers against this "
            "context to recover every original setting. Expand references before constructing application command payloads; "
            "references are prompt compression, not optimizer parameters.")


def bounded(context, *, question="", target_id=None):
    if size(context) <= WORKING_LIMIT:
        return context
    context = deepcopy(context)
    _share_decision_context(context)
    _deduplicate_current_context(context, question)
    # Preserve the scientific working inventory before selecting detailed
    # evidence. Otherwise a large command catalog can erase every proposal and
    # every measurement while leaving a syntactically valid, useless prompt.
    context["research_inventory"] = _inventory(context)
    for operation in context.get("application_commands", {}).values():
        if isinstance(operation, dict) and "payload_schema" in operation:
            operation["payload_schema"] = _schema_without_titles(operation["payload_schema"])
    # Leave room for the selected scientific records as well as their memory
    # index. This only reduces retrievable history, never current constraints.
    trim_memory(context["manager_context"], 64 * 1024)
    for trial in context["trials"]:
        if trial.get("scientific_environment"):
            trial["scientific_environment_digest"] = content_hash(trial.pop("scientific_environment"))
        if trial.get("result") == trial.get("progress"):
            trial.pop("progress", None)
        curve = trial.get("curve", [])
        if len(curve) > 40:
            trial["curve"] = [curve[round(i * (len(curve) - 1) / 39)] for i in range(40)]
    _compact_active_trials(context)
    retrieved = {row["id"] for row in context["manager_context"]["retrieved_records"]}
    active = {row["id"] for row in context["trials"] if row["status"] in {"queued", "running", "pausing", "stopping", "paused"}}
    pinned = {target_id, *active}
    active_hypotheses = {row.get("hypothesis_id") for row in context["trials"] if row["id"] in active}
    pinned.update(row.get("implementation_version_id") for row in context["hypotheses"] if row["id"] in pinned)
    words = set(re.findall(r"[\w-]{4,}", question.lower()))
    tables = ("trials", "hypotheses", "evidence_library", "history", "decisions", "available_implementations",
        "applicable_assets", "reuse_decisions", "reproduction_comparisons", "historical_reproduction_sources")
    counts = {key: len(context.get(key, [])) for key in tables}
    context["evidence_selection"] = {"basis": "Pinned target, active work, relevant retrieval, question matches, then recent records.",
        "original_counts": counts, "omitted_counts": {},
        "history_location": "Versioned campaign context and records.jsonl; omission is not rejection or contrary evidence."}
    candidates = []
    for key in tables:
        for index, row in enumerate(context.get(key, [])):
            identity = row.get("id", row.get("key"))
            if identity in pinned or key == "decisions" and row.get("status") in {"pending", "executing"}:
                continue
            relevant = identity in retrieved or identity in active_hypotheses
            label = " ".join(str(row.get(field, "")) for field in ("title", "name", "question", "mechanism", "content")).lower()
            matches = len(words & set(re.findall(r"[\w-]{4,}", label)))
            candidates.append((relevant, matches, index, key, row))
    candidates.sort(key=lambda item: item[:4])
    estimated_size = size(context)
    for relevant, matches, _, key, row in candidates:
        if estimated_size <= WORKING_LIMIT - 2048:
            break
        # A specifically retrieved scientific source can use the remaining hard
        # allowance. Headroom is a target, not permission to discard the paper
        # the researcher explicitly asked this turn to review.
        if key == "evidence_library" and relevant and matches and estimated_size <= LIMIT - 2048:
            break
        context[key].remove(row)
        estimated_size -= size(row) + 1
    selection = context["evidence_selection"]
    selection["omitted_counts"] = {key: count - len(context.get(key, [])) for key, count in counts.items() if count > len(context.get(key, []))}
    # Memory history is also retrievable. Current guidance, authority, resources,
    # studies, pending issues and next actions are never reduced here.
    memory = context["manager_context"]
    visible_hypotheses = {row["id"] for row in context["hypotheses"]}
    context["implementation_readiness"] = {key: value for key, value in context["implementation_readiness"].items() if key in visible_hypotheses}
    if size(context) > WORKING_LIMIT:
        trim_memory(memory, WORKING_LIMIT - size(context) + size(memory))
    if size(context) > WORKING_LIMIT:
        _share_command_definitions(context)
    if size(context) > WORKING_LIMIT:
        _share_trial_settings(context)
    if size(context) > WORKING_LIMIT:
        _index_informational_decision_history(context, question, target_id)
    if size(context) > LIMIT:
        raise ValueError("Current campaign constraints, active work and the selected evidence exceed the model context allowance. Scope the request; no active constraints were discarded.")
    return context
