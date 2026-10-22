"""Campaign setup as a conversation.

A `campaign_setup` record holds the transcript, the draft of the campaign input and the current step (a question with
optional choices). With a model available the assistant phrases the questions, suggests answers and fills the draft
from free text (one bounded model call per turn through the harness, no tools); without one the scripted steps ask the
same things. The researcher creates the campaign from the draft; the transcript becomes the first turns of its main
thread, so the conversation goes on with the lead.
"""
import json
import os
import re
from typing import Literal

from pydantic import Field

from optimization_framework.contracts.base import Contract, content_hash
from optimization_framework.storage.sqlite import identifier, now

FIELDS = ("name", "objective", "problem", "budget", "autonomy", "autopilot")
BUDGETS = {"small": {"compute_budget_seconds": 600, "llm_budget_usd": 1, "delegated_trial_seconds": 60},
           "medium": {"compute_budget_seconds": 3600, "llm_budget_usd": 5, "delegated_trial_seconds": 300},
           "large": {"compute_budget_seconds": 21600, "llm_budget_usd": 20, "delegated_trial_seconds": 900}}


class AnswerInput(Contract):
    text: str | None = Field(default=None, max_length=20000)
    choice: str | None = Field(default=None, max_length=120)


class EditInput(Contract):
    field: Literal["name", "objective", "problem", "budget", "autonomy", "autopilot"]


def slug(name):
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48] or "campaign"
    return base


def identity_for(workspace, name):
    base = slug(name)
    taken = {c["id"] for c in workspace.store.list("campaign")} | set(workspace.store.removed_campaigns)
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def problems_catalog():
    from optimization_framework.evaluation.registry import problems
    rows = []
    for name in problems.ids():
        try:
            definition = problems.get(name).describe()
            rows.append({"id": definition.id, "label": definition.name, "description": f"{definition.id} (evaluator {definition.evaluator_id} {definition.evaluator_version})"})
        except Exception:   # a problem adapter that cannot describe itself is not offered
            continue
    return rows


def empty_draft():
    return {"name": None, "identity": None, "objective": None, "problem_id": None, "compute_budget_seconds": None, "llm_budget_usd": None,
            "delegated_trial_seconds": None, "autonomy": None, "autopilot": None}


def missing(draft):
    for field in FIELDS:
        if field == "name" and not draft.get("name"):
            return "name"
        if field == "objective" and draft.get("objective") is None:
            return "objective"
        if field == "problem" and "problem_id" not in draft.get("decided", []):
            return "problem"
        if field == "budget" and draft.get("compute_budget_seconds") is None:
            return "budget"
        if field == "autonomy" and not draft.get("autonomy"):
            return "autonomy"
        if field == "autopilot" and draft.get("autopilot") is None:
            return "autopilot"
    return None


def scripted_step(workspace, draft, field):
    """The scripted question for a field: the same things the model would ask, with choices."""
    if field == "name":
        return {"field": "name", "question": "What shall we call the campaign? (The identity is made from the name.)", "choices": [], "free_text": True, "placeholder": "Grating deflector, round 3"}
    if field == "objective":
        return {"field": "objective", "question": "What do you want to find out? A rough sentence is enough; the lead refines it with you into the problem note.",
                "choices": [{"id": "later", "label": "Let the lead draft it with me", "description": "Start with an empty objective and formulate in the chat", "patch": {"objective": ""}}],
                "free_text": True, "placeholder": "Find a binary grating with higher first-order efficiency"}
    if field == "problem":
        return {"field": "problem", "question": "Which problem does it run on? Choose an installed problem, or formulate one with the lead first.",
                "choices": [{"id": "formulate", "label": "Formulate it with the lead first", "description": "No problem instance yet; the adopted note's charter sets the tasks", "patch": {"problem_id": None}},
                            *[{"id": row["id"], "label": row["label"], "description": row["description"], "patch": {"problem_id": row["id"]}} for row in problems_catalog()]],
                "free_text": True, "placeholder": "a problem id"}
    if field == "budget":
        return {"field": "budget", "question": "How much may it spend? Worker time is measured; the model budget caps API spend; the per-run cap is what the lead may give one run without asking.",
                "choices": [{"id": "small", "label": "Small: 10 min of worker time, $1, runs ≤ 1 min", "patch": BUDGETS["small"]},
                            {"id": "medium", "label": "Medium: 1 h, $5, runs ≤ 5 min", "patch": BUDGETS["medium"]},
                            {"id": "large", "label": "Large: 6 h, $20, runs ≤ 15 min", "patch": BUDGETS["large"]}],
                "free_text": True, "placeholder": "e.g. 3600 s, $5, runs 300 s"}
    if field == "autonomy":
        return {"field": "autonomy", "question": "How much may the lead do on its own?",
                "choices": [{"id": "delegated", "label": "Delegated: runs experiments within the caps", "patch": {"autonomy": "delegated"}},
                            {"id": "guided", "label": "Guided: asks before each experiment", "patch": {"autonomy": "guided"}},
                            {"id": "manual", "label": "Manual: I run everything", "patch": {"autonomy": "manual"}}], "free_text": False}
    if field == "autopilot":
        return {"field": "autopilot", "question": "Autopilot? Off means the lead acts on your requests only; on means it runs rounds by itself within a cap.",
                "choices": [{"id": "off", "label": "Off (recommended to start)", "patch": {"autopilot": {"enabled": False}}},
                            {"id": "on", "label": "On, with a cap of one hour of worker time", "patch": {"autopilot": {"enabled": True, "compute_cap_seconds": 3600}}}], "free_text": False}
    return ready_step(draft)


def ready_step(draft):
    return {"field": "ready", "question": "That is everything. Create the campaign, change something, or start over.",
            "choices": [{"id": "create", "label": "Create the campaign", "patch": {}}, *[{"id": "edit:" + f, "label": "Change the " + f, "patch": {}} for f in FIELDS],
                        {"id": "restart", "label": "Start over", "description": "Abandon this draft and begin a new setup", "patch": {}}], "free_text": True, "placeholder": "or tell me what to change"}


def abandon(workspace, setup_id):
    """A setup the researcher gave up on: kept as a record, never created; the page starts a new one."""
    with workspace.lock:
        setup = workspace.store.get(setup_id, "campaign_setup")
        if setup["status"] == "created":
            raise ValueError("This setup already created its campaign; remove the campaign instead")
        setup["transcript"].append({"role": "assistant", "text": "Abandoned. Starting over.", "field": setup["step"]["field"] if setup.get("step") else "ready", "choices": [], "at": now()})
        setup.update(status="abandoned", updated_at=now())
        workspace.store.put("campaign_setup", setup, "setup.abandoned")
        return setup


def parse_free_text(field, text):
    """What a typed answer sets, without a model: names, objectives, problem ids, budgets in words."""
    text = (text or "").strip()
    if field == "name":
        return {"name": text[:200]}
    if field == "objective":
        return {"objective": text[:20000]}
    if field == "problem":
        ids = {row["id"] for row in problems_catalog()}
        if text in ids:
            return {"problem_id": text}
        lowered = text.lower()
        for row in problems_catalog():
            if row["label"].lower() in lowered or row["id"] in lowered:
                return {"problem_id": row["id"]}
        if any(word in lowered for word in ("later", "formulate", "none", "no problem", "lead")):
            return {"problem_id": None}
        raise ValueError("Name an installed problem id, or choose to formulate it first")
    if field == "budget":
        patch = {}
        seconds = re.search(r"(\d+(?:\.\d+)?)\s*(h|hour|hours|min|minute|minutes|s|sec|seconds)\b", text.lower())
        if seconds:
            value, unit = float(seconds.group(1)), seconds.group(2)
            patch["compute_budget_seconds"] = value * (3600 if unit.startswith("h") else 60 if unit.startswith("min") else 1)
        dollars = re.search(r"\$\s*(\d+(?:\.\d+)?)", text)
        if dollars:
            patch["llm_budget_usd"] = float(dollars.group(1))
        runs = re.search(r"runs?\s*(?:≤|<=|of|up to)?\s*(\d+(?:\.\d+)?)\s*(h|hour|min|minute|s|sec)?", text.lower())
        if runs:
            value, unit = float(runs.group(1)), runs.group(2) or "s"
            patch["delegated_trial_seconds"] = value * (3600 if unit.startswith("h") else 60 if unit.startswith("min") else 1)
        if "compute_budget_seconds" not in patch:
            raise ValueError("Say how much worker time (for example '2 h' or '600 s'), optionally with a $ cap and a per-run cap")
        patch.setdefault("llm_budget_usd", 5)
        patch.setdefault("delegated_trial_seconds", min(patch["compute_budget_seconds"], 300))
        return patch
    if field == "autonomy":
        for key in ("delegated", "guided", "manual"):
            if key in text.lower():
                return {"autonomy": key}
        raise ValueError("Choose delegated, guided or manual")
    if field == "autopilot":
        lowered = text.lower()
        if lowered.startswith("on") or "yes" in lowered or "enable" in lowered:
            seconds = re.search(r"(\d+(?:\.\d+)?)\s*(h|hour|min|s)", lowered)
            cap = float(seconds.group(1)) * (3600 if seconds.group(2).startswith("h") else 60 if seconds.group(2).startswith("min") else 1) if seconds else 3600
            return {"autopilot": {"enabled": True, "compute_cap_seconds": cap}}
        return {"autopilot": {"enabled": False}}
    return {}


def apply_patch(workspace, draft, patch):
    draft = dict(draft)
    draft.setdefault("decided", [])
    for key, value in (patch or {}).items():
        if key not in empty_draft() and key != "decided":
            continue
        draft[key] = value
        if key == "problem_id" and "problem_id" not in draft["decided"]:
            draft["decided"] = [*draft["decided"], "problem_id"]
    if draft.get("name") and not draft.get("identity_chosen"):
        draft["identity"] = identity_for(workspace, draft["name"])
    return draft


# The model-backed assistant ------------------------------------------------------------------------------
SYSTEM = ("You are the setup assistant of a research workspace: a researcher is starting an optimization campaign in which a lead agent "
          "will later run experiments for them. Your job is only to fill the campaign's draft through a short, friendly conversation: one "
          "question at a time, with two to four concrete choices when choices help, and free text always allowed. Fields: name; objective "
          "(what to find out; may stay empty to formulate later with the lead); problem_id (one of the installed problems, or null to "
          "formulate first); compute_budget_seconds, llm_budget_usd, delegated_trial_seconds; autonomy (delegated, guided, manual); "
          "autopilot ({enabled, compute_cap_seconds}). Never invent facts about the researcher's problem; suggest, do not decide. When "
          "the researcher's free text answers the current question, put what it sets in draft_patch. Reply with one JSON object: "
          "{\"reply\": a short acknowledgement or suggestion, \"draft_patch\": {...}, \"field\": the next field or \"ready\", "
          "\"question\": the next question, \"choices\": [{\"id\", \"label\", \"description\", \"patch\": {...}}], \"free_text\": true|false}. "
          "Keep replies under 60 words.")


def model_for(workspace):
    if os.environ.get("GRATING_LLM_DISABLED", "").lower() == "true":
        return None
    try:
        choice = workspace.pi.lead_model()
    except Exception:
        return None
    if not choice or not choice.get("provider") or not choice.get("model"):
        return None
    status = workspace.pi.harness_status or {}
    if status and not status.get("configured"):
        return None
    return {"provider": choice["provider"], "model": choice["model"], "effort": "low"}


def model_step(workspace, setup, answer_text, model):
    transcript = "\n".join(f"{'Researcher' if turn['role'] == 'user' else 'Assistant'}: {turn['text']}" for turn in setup["transcript"][-12:])
    prompt = (f"Draft so far: {json.dumps({k: v for k, v in setup['draft'].items() if k not in {'decided', 'identity_chosen'}})}\n"
              f"Installed problems: {json.dumps(problems_catalog())}\nFields still missing, in order: {[f for f in FIELDS if missing_after(setup['draft'], f)]}\n"
              f"Conversation:\n{transcript}\nResearcher's latest answer to the question about '{setup['step']['field']}': {answer_text}")
    reply = workspace.pi.client.complete("setup_" + content_hash([setup["id"], len(setup["transcript"])])[:16],
                                         {**model, "system": SYSTEM, "prompt": prompt, "label": "campaign setup", "max_tokens": 700}, timeout=120)
    from optimization_framework.llm.adapter import structured_reply
    from pydantic import BaseModel

    class Step(BaseModel):
        reply: str = ""
        draft_patch: dict = {}
        field: str = "ready"
        question: str = ""
        choices: list[dict] = []
        free_text: bool = True
    parsed = Step.model_validate_json(json.dumps(structured_reply((reply or {}).get("text"), dict) if False else json.loads(_json_text((reply or {}).get("text")))))
    return parsed.model_dump(), ((reply or {}).get("usage") or {}).get("cost_usd")


def _json_text(text):
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if not value.startswith("{"):
        start, end = value.find("{"), value.rfind("}")
        value = value[start:end + 1] if 0 <= start < end else "{}"
    return value


def missing_after(draft, field):
    probe = dict(draft)
    return missing(probe) == field or (missing(probe) is not None and FIELDS.index(missing(probe)) < FIELDS.index(field))


# Sessions -----------------------------------------------------------------------------------------------------
def start(workspace):
    model = model_for(workspace)
    record = {"id": identifier("setup"), "status": "asking", "draft": {**empty_draft(), "decided": []}, "transcript": [], "step": None,
              "model": model, "mode": "model" if model else "scripted", "cost_usd": 0.0, "campaign_id": None, "created_at": now(), "updated_at": now()}
    opening = ("Let's set up a campaign. I'll ask a few things; pick a choice or just type. First: " if not model else "Let's set up a campaign together; pick a choice or type freely. First: ")
    record["step"] = scripted_step(workspace, record["draft"], "name")
    record["transcript"].append({"role": "assistant", "text": opening + record["step"]["question"], "field": "name", "choices": record["step"]["choices"], "at": now()})
    with workspace.lock:
        workspace.store.put("campaign_setup", record, "setup.started")
    return record


def answer(workspace, setup_id, values):
    with workspace.lock:
        setup = workspace.store.get(setup_id, "campaign_setup")
        if setup["status"] in {"created", "abandoned"}:
            raise ValueError("This setup is closed; start a new one")
        step = setup["step"]
        text = (values.text or "").strip()
        chosen = next((c for c in step.get("choices", []) if c["id"] == values.choice), None) if values.choice else None
        if chosen is None and not text:
            raise ValueError("Pick a choice or write an answer")
        if values.choice == "restart" or (text and re.fullmatch(r"(start over|restart|abandon|start again)[.!]?", text.lower())):
            return abandon(workspace, setup_id)
        setup["transcript"].append({"role": "user", "text": chosen["label"] if chosen else text, "choice": chosen["id"] if chosen else None, "field": step["field"], "at": now()})
        if step["field"] == "ready" and chosen and chosen["id"].startswith("edit:"):
            field = chosen["id"].split(":", 1)[1]
            return _ask(workspace, setup, field, "Sure. ")
        if step["field"] == "ready" and chosen and chosen["id"] == "create":
            setup["status"] = "ready"
            setup["transcript"].append({"role": "assistant", "text": "Creating it now.", "field": "ready", "choices": [], "at": now()})
            workspace.store.put("campaign_setup", setup, "setup.ready")
            return setup
        reply = ""
        if chosen is not None:
            setup["draft"] = apply_patch(workspace, setup["draft"], chosen.get("patch") or {})
            if step["field"] == "problem" and chosen["id"] == "formulate":
                setup["draft"]["decided"] = list({*setup["draft"].get("decided", []), "problem_id"})
        elif setup.get("model"):
            try:
                parsed, cost = model_step(workspace, setup, text, setup["model"])
                setup["cost_usd"] = (setup.get("cost_usd") or 0) + float(cost or 0)
                setup["draft"] = apply_patch(workspace, setup["draft"], parsed.get("draft_patch") or {})
                if step["field"] == "problem" and "problem_id" in (parsed.get("draft_patch") or {}):
                    setup["draft"]["decided"] = list({*setup["draft"].get("decided", []), "problem_id"})
                reply = (parsed.get("reply") or "").strip()
                nxt = parsed.get("field") or "ready"
                field = nxt if nxt in FIELDS and missing(setup["draft"]) == nxt else missing(setup["draft"])
                if field is None:
                    return _ready(workspace, setup, reply)
                question = parsed.get("question") if nxt == field and parsed.get("question") else None
                choices = parsed.get("choices") if nxt == field and parsed.get("choices") else None
                return _ask(workspace, setup, field, (reply + " ") if reply else "", question=question, choices=choices, free_text=parsed.get("free_text", True))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                reply = ""
                setup["model_note"] = f"The model did not answer ({str(exc)[:120]}); continuing with the scripted questions."
                try:
                    setup["draft"] = apply_patch(workspace, setup["draft"], parse_free_text(step["field"], text))
                except ValueError as inner:
                    return _ask(workspace, setup, step["field"], str(inner) + " ")
        else:
            try:
                setup["draft"] = apply_patch(workspace, setup["draft"], parse_free_text(step["field"], text))
            except ValueError as exc:
                return _ask(workspace, setup, step["field"], str(exc) + " ")
            if step["field"] == "problem":
                setup["draft"]["decided"] = list({*setup["draft"].get("decided", []), "problem_id"})
        field = missing(setup["draft"])
        if field is None:
            return _ready(workspace, setup, reply)
        return _ask(workspace, setup, field, (reply + " ") if reply else _ack(step["field"], setup["draft"]))


def _ack(field, draft):
    if field == "name":
        return f"\"{draft['name']}\" it is; its identity will be {draft['identity']}. "
    if field == "objective":
        return "Noted. " if draft.get("objective") else "Fine, the lead drafts it with you. "
    if field == "problem":
        return (f"Running on {draft['problem_id']}. " if draft.get("problem_id") else "Formulating first. ")
    if field == "budget":
        return f"{draft['compute_budget_seconds']:g} s of worker time, ${draft['llm_budget_usd']:g}, runs up to {draft['delegated_trial_seconds']:g} s. "
    return "Good. "


def _ask(workspace, setup, field, prefix, *, question=None, choices=None, free_text=None):
    step = scripted_step(workspace, setup["draft"], field)
    if question:
        step["question"] = question
    if choices:
        step["choices"] = [{"id": str(c.get("id") or i), "label": str(c.get("label") or c.get("id")), "description": str(c.get("description") or ""), "patch": c.get("patch") or {}} for i, c in enumerate(choices)][:6]
    if free_text is not None:
        step["free_text"] = bool(free_text) or step["free_text"]
    setup["step"] = step
    setup["status"] = "asking"
    setup["transcript"].append({"role": "assistant", "text": prefix + step["question"], "field": field, "choices": step["choices"], "at": now()})
    setup["updated_at"] = now()
    workspace.store.put("campaign_setup", setup, "setup.asked")
    return setup


def _ready(workspace, setup, reply):
    setup["step"] = ready_step(setup["draft"])
    setup["status"] = "asking"
    setup["transcript"].append({"role": "assistant", "text": ((reply + " ") if reply else "") + summary(setup["draft"]) + " " + setup["step"]["question"], "field": "ready", "choices": setup["step"]["choices"], "at": now()})
    setup["updated_at"] = now()
    workspace.store.put("campaign_setup", setup, "setup.asked")
    return setup


def summary(draft):
    auto = draft.get("autopilot") or {}
    return (f"So: \"{draft.get('name')}\" ({draft.get('identity')}), " + (f"objective \"{draft['objective'][:120]}\"" if draft.get("objective") else "objective to be formulated with the lead")
            + f", on {draft.get('problem_id') or 'a problem to formulate first'}, {float(draft.get('compute_budget_seconds') or 0):g} s of worker time and ${float(draft.get('llm_budget_usd') or 0):g}, "
            f"runs up to {float(draft.get('delegated_trial_seconds') or 0):g} s, {draft.get('autonomy')} delegation, autopilot {'on with a cap of ' + format(float(auto.get('compute_cap_seconds') or 0), 'g') + ' s' if auto.get('enabled') else 'off'}.")


def campaign_input(draft):
    body = {"name": draft["name"], "objective": draft.get("objective") or "", "compute_budget_seconds": draft["compute_budget_seconds"], "llm_budget_usd": draft["llm_budget_usd"],
            "delegated_trial_seconds": draft["delegated_trial_seconds"], "autonomy": draft.get("autonomy") or "guided"}
    if not body["objective"]:
        body["objective"] = "Develop an effective optimizer for the chosen problem."
    if draft.get("problem_id"):
        body["tasks"] = [starting_task(draft["problem_id"])]
    return body


def starting_task(problem_id):
    """The task an installed problem starts on: its first published example instance, else the adapter's own defaults.
    Not every adapter has defaults (the 2D deflector declares every field), so an empty configuration is the last resort."""
    from optimization_framework.evaluation.registry import problems
    hook = getattr(problems.get(problem_id), "examples", None)
    for example in (hook() if hook is not None else []):
        for instance in example.get("instances") or []:
            if instance.get("problem_id") == problem_id:
                return {key: instance[key] for key in ("name", "problem_id", "configuration", "fidelity") if key in instance}
    return {"name": problem_id, "problem_id": problem_id, "configuration": {}}


def create(workspace, setup_id):
    from optimization_framework.contracts.commands import Command
    setup = workspace.store.get(setup_id, "campaign_setup")
    if setup["status"] == "created":
        return {"setup": setup, "campaign_id": setup["campaign_id"], "already": True}
    if missing(setup["draft"]):
        raise ValueError("The draft is not complete: " + missing(setup["draft"]) + " is still open")
    draft = setup["draft"]
    identity = draft.get("identity") or identity_for(workspace, draft["name"])
    receipt = workspace.commands.execute(Command(id="ui_setup_" + content_hash([setup_id, identity])[:24], campaign_id=identity, operation="campaign.create",
                                                 expected_revision=0, payload=campaign_input(draft)), actor="researcher")
    campaign = receipt["outcome"]["campaign"]
    from balsamic.threads.service import ensure_main
    thread = ensure_main(workspace, campaign["id"])
    with workspace.lock:
        for index, turn in enumerate(setup["transcript"]):
            workspace.store.put("message", {"id": f"message_setup_{setup_id}_{index}", "campaign_id": campaign["id"], "thread_id": thread["id"],
                                            "role": "user" if turn["role"] == "user" else "assistant", "origin": "researcher" if turn["role"] == "user" else "setup",
                                            "content": turn["text"], "created_at": turn.get("at") or now(), "setup_id": setup_id}, "message.created")
        setup.update(status="created", campaign_id=campaign["id"], updated_at=now())
        workspace.store.put("campaign_setup", setup, "setup.created")
    if (draft.get("autopilot") or {}).get("enabled"):
        # Autopilot waits for the problem-definition gate: the choice is remembered on the campaign, the lead starts now, and the
        # researcher turns autopilot on (prefilled) once the note is complete, adopted and the charter confirmed.
        with workspace.lock:
            record = workspace.store.get(campaign["id"], "campaign")
            record["autopilot"] = {"enabled": False, "requested": {"compute_cap_seconds": draft["autopilot"].get("compute_cap_seconds"),
                                                                   "report_by": draft["autopilot"].get("report_by"), "at": now(), "by": "setup"}}
            workspace.store.put("campaign", record, "campaign.autopilot_requested")
        try:
            workspace.commands.execute(Command(id="ui_setup_activate_" + content_hash([setup_id])[:20], campaign_id=campaign["id"], operation="agent.activate",
                                               expected_revision=workspace.store.get(campaign["id"], "campaign")["version"], payload={}), actor="researcher")
        except ValueError as exc:
            workspace.memory.issue(campaign["id"], "autopilot_setup", str(exc))
    return {"setup": setup, "campaign_id": campaign["id"], "thread_id": thread["id"], "receipt_id": receipt["id"], "already": False}
