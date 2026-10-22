"""Problem note records: a mutable head, immutable revisions, annotations with tiers, and Adopt.

Sections: objective, knobs, observables, noise, constraints, confounders, open_questions, each {text, items, latex}.
The lead revises the note through its tools (author lead); the researcher through problem_note.revise. A question on a
span is an annotation; the lookup and quick tiers answer without the bus, the lead tier answers in a turn as a proposed
edit the researcher accepts or rejects, the visual tier adds a drawn answer. Adopt hands the note to the formulator as the
problem statement of a problem import; the charter it writes is linked back, and the researcher confirms it.
"""
import os
from typing import Literal

from pydantic import Field

from optimization_framework.contracts.base import Contract, content_hash
from optimization_framework.storage.sqlite import now

from balsamic.notes.router import classify

SECTIONS = ("objective", "knobs", "observables", "noise", "constraints", "confounders", "open_questions")
TITLES = {"objective": "Objective", "knobs": "Knobs", "observables": "Observables", "noise": "Noise", "constraints": "Constraints",
          "confounders": "Confounders", "open_questions": "Open questions"}
TIERS = ("lookup", "quick", "lead", "visual")


def note_id(campaign_id):
    return "note_" + campaign_id


def empty_sections():
    return {name: {"text": "", "items": [], "latex": []} for name in SECTIONS}


def ensure_note(workspace, campaign_id, *, thread_id=None):
    identity = note_id(campaign_id)
    try:
        return workspace.store.get(identity, "problem_note")
    except KeyError:
        campaign = workspace.store.get(campaign_id, "campaign")
        record = {"id": identity, "campaign_id": campaign_id, "thread_id": thread_id, "title": campaign["name"], "current_revision": 0,
                  "adopted_revision": None, "charter_id": None, "import_id": None, "status": "drafting", "created_at": now(), "updated_at": now()}
        with workspace.lock:
            workspace.store.put("problem_note", record, "problem_note.created")
            revise(workspace, identity, {"sections": {"objective": {"text": campaign.get("objective") or ""}}}, author="researcher",
                   reason="the campaign's question, as written when it was created")
            return workspace.store.get(identity, "problem_note")


def revision_of(workspace, identity, number):
    return workspace.store.get(f"{identity}_r{number}", "problem_note_revision")


def _merge_sections(previous, patch):
    sections = {name: dict(previous.get(name) or {"text": "", "items": [], "latex": []}) for name in SECTIONS}
    for name, value in (patch or {}).items():
        if name not in SECTIONS:
            raise ValueError(f"Unknown section {name}; the note has " + ", ".join(SECTIONS))
        if not isinstance(value, dict):
            raise ValueError(f"Section {name} is an object with text, items and latex")
        current = sections[name]
        if "text" in value:
            current["text"] = str(value["text"])[:20000]
        if "items" in value:
            current["items"] = [str(item)[:2000] for item in (value["items"] or [])][:100]
        if "latex" in value:
            current["latex"] = [str(item)[:4000] for item in (value["latex"] or [])][:50]
        sections[name] = current
    return sections


def revise(workspace, identity, changes, *, author, reason="", source_message_id=None, run_id=None, annotation_id=None):
    """A new immutable revision from a sections patch; the head moves to it."""
    if author not in {"lead", "researcher"}:
        raise ValueError("A revision is written by the lead or the researcher")
    with workspace.lock, workspace.store.transaction():
        note = workspace.store.get(identity, "problem_note")
        if note["status"] == "confirmed":
            raise ValueError("The note was adopted and its charter confirmed; fork the campaign to change the problem")
        previous = revision_of(workspace, identity, note["current_revision"]) if note["current_revision"] else {"sections": empty_sections(), "glossary": []}
        sections = _merge_sections(previous["sections"], changes.get("sections"))
        glossary = previous.get("glossary") or []
        if changes.get("glossary") is not None:
            glossary = [{"term": str(entry.get("term", ""))[:200], "definition": str(entry.get("definition", ""))[:2000]} for entry in changes["glossary"] if isinstance(entry, dict) and entry.get("term")][:200]
        if sections == previous["sections"] and glossary == (previous.get("glossary") or []) and not changes.get("title"):
            raise ValueError("The revision changes nothing")
        number = note["current_revision"] + 1
        record = {"id": f"{identity}_r{number}", "note_id": identity, "campaign_id": note["campaign_id"], "revision": number, "sections": sections, "glossary": glossary,
                  "title": (changes.get("title") or note["title"])[:200], "author": author, "reason": (reason or "")[:2000], "source_message_id": source_message_id,
                  "run_id": run_id, "proposed_by_annotation_id": annotation_id, "created_at": now()}
        workspace.store.put_immutable("problem_note_revision", record, "problem_note.revised")
        note.update(current_revision=number, title=record["title"], updated_at=now())
        if note["status"] == "adopted":
            note["status"] = "drafting"   # a note changed after adoption must be adopted again
        workspace.store.put("problem_note", note, "problem_note.updated")
        return record


def annotations_of(workspace, identity):
    return [row for row in workspace.store.list("note_annotation", workspace.store.get(identity, "problem_note")["campaign_id"]) if row["note_id"] == identity]


def view(workspace, identity, *, revision=None):
    note = workspace.store.get(identity, "problem_note")
    number = revision or note["current_revision"]
    current = revision_of(workspace, identity, number) if number else None
    revisions = [{key: row[key] for key in ("id", "revision", "author", "reason", "created_at", "source_message_id", "proposed_by_annotation_id")}
                 for row in workspace.store.list("problem_note_revision", note["campaign_id"]) if row["note_id"] == identity]
    charter = None
    if note.get("import_id"):
        try:
            record = workspace.problem_imports.sync(note["import_id"]) if hasattr(workspace, "problem_imports") else workspace.store.get(note["import_id"], "problem_import")
        except (KeyError, ValueError):
            record = None
        if record:
            if record.get("charter_id") and note.get("charter_id") != record["charter_id"]:
                # The note follows the charter of its current adoption: adopting again writes a new charter.
                note["charter_id"] = record["charter_id"]
                with workspace.lock:
                    workspace.store.put("problem_note", note, "problem_note.linked")
            charter = {"import_id": record["id"], "status": record.get("status"), "error": record.get("error"), "charter_id": record.get("charter_id"), "reply": record.get("reply"),
                       "model": record.get("model"), "tier": record.get("tier"), "usage": record.get("usage")}   # which model the formulator runs on: fixed for the import's lifetime
    if note.get("charter_id"):
        try:
            charter = {**(charter or {}), "charter": workspace.charters.view(note["charter_id"])}
        except KeyError:
            pass
    from balsamic.notes import skips
    return {"note": note, "revision": current, "revisions": revisions, "annotations": annotations_of(workspace, identity), "charter": charter, "sections": list(SECTIONS), "titles": TITLES,
            "skips": skips.view(workspace, note)}


# Annotations and tiers ------------------------------------------------------------------------------------
class Anchor(Contract):
    revision: int = Field(ge=1)
    section: Literal["objective", "knobs", "observables", "noise", "constraints", "confounders", "open_questions"]
    item_index: int | None = Field(default=None, ge=0)
    start: int = Field(default=0, ge=0)
    end: int = Field(default=0, ge=0)
    quote: str = Field(default="", max_length=2000)


class AskInput(Contract):
    note_id: str
    anchor: Anchor
    question: str = Field(min_length=1, max_length=8000)
    kind: Literal["ask", "define", "explain", "challenge", "visualize"] = "ask"
    tier: Literal["lookup", "quick", "lead", "visual"] | None = None


class ResolveInput(Contract):
    annotation_id: str
    choice: Literal["accept", "reject", "resolve"]


def lookup(revision, question):
    text = question.lower()
    for entry in revision.get("glossary") or []:
        term = entry["term"].lower()
        if term and (term in text):
            return f"{entry['term']}: {entry['definition']}"
    return None


def quick_answer(workspace, note, revision, question):
    """One cheap model call with the note as its only context; no tools, nothing recorded as a decision."""
    if os.environ.get("GRATING_LLM_DISABLED"):
        raise ValueError("Model calls are disabled in this workspace; ask the lead instead")
    config = workspace.pi.configuration(note["campaign_id"]) or {}
    default = (config.get("models") or {}).get("default")
    if not default:
        raise ValueError("No model is configured for quick answers; ask the lead instead")
    context = "\n".join(f"## {TITLES[name]}\n{section.get('text', '')}\n" + "\n".join("- " + item for item in section.get("items", []))
                        for name, section in revision["sections"].items())
    reply = workspace.pi.client.complete("note_quick_" + content_hash([note["id"], question])[:16], {
        "provider": default.get("provider"), "model": default.get("model"), "effort": "low", "max_tokens": 400, "label": "quick answer",
        "system": "Answer the researcher's question about this problem note in at most four sentences, from the note alone. Say when the note does not say.",
        "prompt": "Problem note:\n" + context + "\n\nQuestion: " + question}, timeout=120)
    return {"text": (reply or {}).get("text", "").strip(), "cost_usd": ((reply or {}).get("usage") or {}).get("cost_usd"), "model": default.get("model")}


def annotate(workspace, values, *, tier, escalated_from=None, answer=None, run_id=None, identity=None):
    note = workspace.store.get(values.note_id, "problem_note")
    record = {"id": identity or "annotation_" + content_hash([values.note_id, values.anchor.model_dump(), values.question, now()])[:20], "note_id": note["id"],
              "campaign_id": note["campaign_id"], "anchor": values.anchor.model_dump(), "kind": values.kind, "question": values.question, "tier": tier,
              "escalated_from_tier": escalated_from, "answer": answer, "proposed_edit": None, "proposed_revision_id": None, "visual_id": None,
              "status": "answered" if answer else "open", "run_id": run_id, "created_at": now(), "updated_at": now()}
    with workspace.lock:
        workspace.store.put("note_annotation", record, "note.annotated")
    return record


def ask(workspace, values, *, actor="researcher"):
    """The lookup and quick tiers, answered here and now; the lead and visual tiers are refused here (they go through the bus)."""
    note = workspace.store.get(values.note_id, "problem_note")
    revision = revision_of(workspace, note["id"], values.anchor.revision)
    tier = values.tier or classify(values.question, values.kind, revision.get("glossary") or [])
    escalated = None
    if tier == "lookup":
        found = lookup(revision, values.question)
        if found:
            return annotate(workspace, values, tier="lookup", answer={"text": found, "answered_by": "glossary"})
        tier, escalated = "quick", "lookup"
    if tier == "quick":
        try:
            answer = quick_answer(workspace, note, revision, values.question)
            return annotate(workspace, values, tier="quick", escalated_from=escalated, answer={**answer, "answered_by": "model"})
        except ValueError as exc:
            # Honest escalation: the next tier is the lead, through the bus; the annotation says why.
            record = annotate(workspace, values, tier="lead", escalated_from=escalated or "quick")
            record["escalation_reason"] = str(exc)
            with workspace.lock:
                workspace.store.put("note_annotation", record)
            return record
    raise ValueError(f"The {tier} tier runs a turn of the lead: send note.ask through the command bus")


def propose_edit(workspace, annotation_id, *, answer, proposed_edit=None, author="lead", run_id=None):
    with workspace.lock:
        record = workspace.store.get(annotation_id, "note_annotation")
        if record["status"] in {"accepted", "rejected", "resolved"}:
            raise ValueError("This annotation is closed")
        record.update(answer={"text": answer, "answered_by": author, "run_id": run_id}, proposed_edit=proposed_edit or None,
                      status="answered", updated_at=now())
        workspace.store.put("note_annotation", record, "note.edit_proposed" if proposed_edit else "note.answered")
        return record


def attach_visual(workspace, annotation_id, artifact_id):
    with workspace.lock:
        record = workspace.store.get(annotation_id, "note_annotation")
        record.update(visual_id=artifact_id, status="answered" if record["status"] == "open" else record["status"], updated_at=now())
        workspace.store.put("note_annotation", record, "note.answered")
        return record


# Adopt -------------------------------------------------------------------------------------------------------
def held_skips(workspace, note):
    from balsamic.notes.skips import held
    return held(workspace, note)


def markdown(revision, skipped=None):
    lines = ["# " + revision.get("title", "Problem note"), ""]
    for name, section in revision["sections"].items():
        if not (section.get("text") or section.get("items") or section.get("latex")):
            if name in (skipped or {}):   # left unwritten by agreement: the formulator reads why
                lines += ["## " + TITLES[name], "", "Skipped with the researcher's agreement: " + skipped[name]["reason"], ""]
            continue
        lines += ["## " + TITLES[name], ""]
        if section.get("text"):
            lines += [section["text"], ""]
        lines += ["- " + item for item in section.get("items", [])]
        lines += ["$$" + formula + "$$" for formula in section.get("latex", [])]
        lines.append("")
    if revision.get("glossary"):
        lines += ["## Glossary", ""] + [f"- **{entry['term']}**: {entry['definition']}" for entry in revision["glossary"]]
    return "\n".join(lines)


def adopt(workspace, identity, revision_number, *, actor):
    if actor != "researcher":
        raise ValueError("Only the researcher adopts the note")
    with workspace.lock:
        note = workspace.store.get(identity, "problem_note")
        if revision_number != note["current_revision"]:
            raise ValueError("Adopt the current revision; the note changed")
        revision = revision_of(workspace, identity, revision_number)
        if not (revision["sections"]["objective"].get("text") or "").strip():
            raise ValueError("The note needs an objective before it is adopted")
    imports = getattr(workspace, "problem_imports", None)
    if imports is None:
        raise ValueError("The formulator is not available in this workspace")
    from optimization_framework.problem_import.service import ImportCreate
    record = imports.create(ImportCreate(title=revision["title"][:200], statement=markdown(revision, held_skips(workspace, note)), notes=f"Adopted from problem note {identity} revision {revision_number}"))
    imports.start(record["id"])
    with workspace.lock:
        note = workspace.store.get(identity, "problem_note")
        note.update(adopted_revision=revision_number, import_id=record["id"], status="adopted", updated_at=now())
        workspace.store.put("problem_note", note, "problem_note.adopted")
        campaign = workspace.store.get(note["campaign_id"], "campaign")
        campaign["problem_note_id"] = identity
        workspace.store.put("campaign", campaign)
    return {"note_id": identity, "revision": revision_number, "import_id": record["id"]}


# Commands ----------------------------------------------------------------------------------------------------
class ReviseInput(Contract):
    note_id: str
    sections: dict = Field(default_factory=dict)
    glossary: list[dict] | None = None
    title: str | None = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=2000)
    source_message_id: str | None = None
    annotation_id: str | None = None


class AdoptInput(Contract):
    note_id: str
    revision: int = Field(ge=1)


def register(workspace):
    commands = workspace.commands

    def _own(command, identity):
        if identity == note_id(command.campaign_id):
            ensure_note(workspace, command.campaign_id, thread_id=(command.origin.thread_id if command.origin else None))
        note = workspace.store.get(identity, "problem_note")
        if note["campaign_id"] != command.campaign_id:
            raise ValueError("The note belongs to another campaign")
        return note

    def revise_command(command, actor):
        values = ReviseInput(**command.payload)
        _own(command, values.note_id)
        origin = command.origin.model_dump() if command.origin else {}
        record = revise(workspace, values.note_id, values.model_dump(exclude={"note_id", "reason", "source_message_id", "annotation_id"}),
                        author="lead" if actor != "researcher" else "researcher", reason=values.reason, source_message_id=values.source_message_id or origin.get("message_id"),
                        run_id=origin.get("run_id"), annotation_id=values.annotation_id)
        return {"note_id": values.note_id, "revision": record["revision"], "revision_id": record["id"]}

    def adopt_command(command, actor):
        values = AdoptInput(**command.payload)
        _own(command, values.note_id)
        return adopt(workspace, values.note_id, values.revision, actor=actor)

    def ask_command(command, actor):
        """The lead and visual tiers: a turn of the lead on the thread, told to answer as a proposed edit (or a visual)."""
        values = AskInput(**command.payload)
        note = _own(command, values.note_id)
        revision = revision_of(workspace, note["id"], values.anchor.revision)
        tier = values.tier or classify(values.question, values.kind, revision.get("glossary") or [])
        if tier in {"lookup", "quick"}:
            record = ask(workspace, values, actor=actor)
            return {"annotation_id": record["id"], "tier": record["tier"], "status": record["status"]}
        if not workspace.pi.owns(command.campaign_id):
            raise ValueError("Start the agent team first: this question needs a turn of the lead")
        origin = command.origin.model_dump() if command.origin else {}
        record = annotate(workspace, values, tier=tier, identity="annotation_" + content_hash(command.id)[:20])
        quote = values.anchor.quote or values.anchor.section
        prompt = (f"The researcher asks on the problem note (annotation {record['id']}, section {values.anchor.section}, revision {values.anchor.revision}; quoted: \"{quote[:300]}\"): "
                  f"{values.question}\n" + ("Answer with render_visual (give the annotation_id), then problem_note_propose with a short answer. "
                                             if tier == "visual" else "Answer with problem_note_propose: your answer, and, only if the note should change, the proposed_edit "
                                             "(a sections patch). Never rewrite the note silently.") + " Then yield_work.")
        from balsamic.threads.service import lead_agent, thread_of
        lead = lead_agent(workspace, command.campaign_id)
        workspace.pi._request(command.campaign_id, True)
        placement = {}
        if origin.get("thread_id"):
            thread = thread_of(workspace, origin["thread_id"])
            placement = {"thread_revision": thread.get("guidance_revision"), **({"leaf_entry_id": thread["agent_leaf"]} if thread.get("agent_leaf") else {"branch_from_entry_id": thread.get("fork_entry_id")})}
        run = workspace.pi.enqueue(lead, "pi_run_" + command.id, prompt, mode="steer", origin={**origin, "actor": "researcher", "annotation_id": record["id"]}, **placement)
        record.update(run_id=run["id"])
        with workspace.lock:
            workspace.store.put("note_annotation", record)
        return {"annotation_id": record["id"], "tier": tier, "status": record["status"], "run_id": run["id"]}

    def resolve_command(command, actor):
        if actor != "researcher":
            raise ValueError("Only the researcher accepts or rejects a proposed edit")
        values = ResolveInput(**command.payload)
        with workspace.lock:
            record = workspace.store.get(values.annotation_id, "note_annotation")
            if record["campaign_id"] != command.campaign_id:
                raise ValueError("The annotation belongs to another campaign")
            if record["status"] in {"accepted", "rejected", "resolved"}:
                raise ValueError("This annotation is closed")
            outcome = {"annotation_id": record["id"]}
            if values.choice == "accept":
                if not record.get("proposed_edit"):
                    raise ValueError("There is no proposed edit to accept")
                revision = revise(workspace, record["note_id"], record["proposed_edit"], author="researcher", reason="accepted the lead's proposed edit",
                                  annotation_id=record["id"])
                record["proposed_revision_id"] = revision["id"]
                outcome["revision"] = revision["revision"]
            record.update(status={"accept": "accepted", "reject": "rejected", "resolve": "resolved"}[values.choice], updated_at=now())
            workspace.store.put("note_annotation", record, "note.resolved")
        return outcome

    commands.register("problem_note.revise", revise_command, model=ReviseInput, delegable=True)
    commands.register("problem_note.adopt", adopt_command, model=AdoptInput)
    commands.register("note.ask", ask_command, model=AskInput)
    commands.register("note.resolve", resolve_command, model=ResolveInput)
