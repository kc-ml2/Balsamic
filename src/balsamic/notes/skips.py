"""Skipping a section of the problem note when that is justified: the lead proposes, the researcher agrees.

A section other than the objective may stay unwritten on one of two grounds. "charter": the confirmed charter already states
it, and the proposal names the charter paths that do (each must hold a value in the confirmed revision). "not_applicable":
the section does not apply to this problem. The researcher is not left to judge this alone: each proposed skip carries the
lead's reason and one statement for the researcher to confirm, and the lead asks in the conversation about whatever the
records do not settle. Only the researcher's agreement skips a section. The note's text is unchanged, so no new adoption is
needed; a skip on the charter lapses when the confirmed charter no longer states what it cited.
"""
from typing import Literal

from pydantic import Field

from optimization_framework.contracts.base import Contract, content_hash
from optimization_framework.storage.sqlite import now

from balsamic.notes.service import SECTIONS, TITLES, revision_of

KIND = "note_skip"
SKIPPABLE = tuple(name for name in SECTIONS if name != "objective")
Section = Literal["knobs", "observables", "noise", "constraints", "confounders", "open_questions"]


class SkipItem(Contract):
    section: Section
    basis: Literal["charter", "not_applicable"]
    charter_refs: list[str] = Field(default_factory=list, max_length=12, description="Paths in the confirmed charter that state the section, e.g. variables, metrics, evaluator.deterministic")
    reason: str = Field(min_length=20, max_length=1200, description="Why the section need not be written, in the researcher's terms")
    confirm: str = Field(min_length=10, max_length=400, description="The one statement the researcher confirms by agreeing")


class ProposeInput(Contract):
    skips: list[SkipItem] = Field(min_length=1, max_length=len(SKIPPABLE))


class DecideInput(Contract):
    skip_id: str
    agree: list[Section] = Field(default_factory=list)
    decline: list[Section] = Field(default_factory=list)


class UnskipInput(Contract):
    note_id: str
    section: Section


def _written(section):
    section = section or {}
    return bool((section.get("text") or "").strip() or section.get("items") or section.get("latex"))


def _sections(workspace, note):
    if not note.get("current_revision"):
        return {}
    return revision_of(workspace, note["id"], note["current_revision"]).get("sections") or {}


def confirmed_charter(workspace, note):
    """(charter id, confirmed revision, its content) for the charter of the note's latest adoption, or None."""
    from balsamic.evolution.stages import _charter_of
    record = _charter_of(workspace.store, note)
    if not record or record.get("confirmed_revision") is None:
        return None
    try:
        content = workspace.store.get(f"{record['id']}_r{record['confirmed_revision']}", "problem_charter_revision")["charter"]
    except KeyError:
        return None
    return record["id"], record["confirmed_revision"], content


def resolve_ref(charter, path):
    """The value at a dotted path (list items by index), or None when the charter does not hold one there."""
    value = charter
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return None if value in (None, "", [], {}, False) else value


def lapsed(item, charter):
    """Why a skip on the charter no longer holds, or None while it does."""
    if item["basis"] != "charter":
        return None
    if charter is None:
        return "the charter is no longer confirmed"
    missing = [path for path in item.get("charter_refs") or [] if resolve_ref(charter[2], path) is None]
    return ("the confirmed charter no longer states " + ", ".join(missing)) if missing else None


def held(workspace, note):
    """The sections skipped with the researcher's agreement that still hold: {section: skip}."""
    skipped = note.get("skipped") or {}
    if not skipped:
        return {}
    charter = confirmed_charter(workspace, note)
    return {name: item for name, item in skipped.items() if lapsed(item, charter) is None}


def _check(workspace, note, item, sections, charter):
    if _written(sections.get(item.section)):
        raise ValueError(f"{TITLES[item.section]} is written; there is nothing to skip")
    if item.section in (note.get("skipped") or {}) and item.section in held(workspace, note):
        raise ValueError(f"{TITLES[item.section]} is already skipped")
    if item.basis == "charter":
        if charter is None:
            raise ValueError("A skip on the charter needs a confirmed charter; until then write the section, or skip it as not applicable with a reason")
        if not item.charter_refs:
            raise ValueError(f"Name the charter paths that state {TITLES[item.section]} (charter_refs)")
    if item.charter_refs:
        if charter is None:
            raise ValueError("There is no confirmed charter to cite; leave charter_refs empty")
        missing = [path for path in item.charter_refs if resolve_ref(charter[2], path) is None]
        if missing:
            raise ValueError("The confirmed charter holds nothing at " + ", ".join(missing) + "; cite paths that state the section (e.g. variables, objective, metrics, constraints, evaluator.deterministic)")
    if item.section == "open_questions" and charter is not None:
        unanswered = [q.get("question", "")[:120] for q in charter[2].get("questions") or [] if not q.get("answer")]
        if unanswered:
            raise ValueError("The charter still has unanswered questions; write them as open questions instead: " + "; ".join(unanswered))


def propose(workspace, campaign_id, values: ProposeInput, *, key, run_id=None, thread_id=None, author="lead"):
    """The lead's proposal: each skip checked against the note and the confirmed charter, shown as a card in the conversation.
    key makes a repeated tool call return the same proposal."""
    from balsamic.notes.service import ensure_note
    if len({item.section for item in values.skips}) != len(values.skips):
        raise ValueError("Each section appears once in a proposal")
    identity = "note_skip_" + content_hash(key)[:20]
    with workspace.lock:
        try:
            return workspace.store.get(identity, KIND)
        except KeyError:
            pass
        note = ensure_note(workspace, campaign_id, thread_id=thread_id)
        sections, charter = _sections(workspace, note), confirmed_charter(workspace, note)
        for item in values.skips:
            _check(workspace, note, item, sections, charter)
        # A newer proposal for a section replaces the open one.
        for old in workspace.store.list(KIND, campaign_id):
            if old["status"] != "open":
                continue
            items = [{**entry, "status": "superseded"} if entry["status"] == "proposed" and entry["section"] in {i.section for i in values.skips} else entry for entry in old["items"]]
            if items != old["items"]:
                workspace.store.put(KIND, {**old, "items": items, "status": _status(items), "updated_at": now()}, "note_skip.superseded")
        record = {"id": identity, "campaign_id": campaign_id, "note_id": note["id"], "author": author, "run_id": run_id, "thread_id": thread_id,
                  "charter_id": charter[0] if charter else None, "charter_revision": charter[1] if charter else None,
                  "items": [{**item.model_dump(), "status": "proposed"} for item in values.skips], "status": "open", "created_at": now(), "updated_at": now()}
        workspace.store.put(KIND, record, "note_skip.proposed")
        if thread_id:
            titles = ", ".join(TITLES[item.section] for item in values.skips)
            workspace.store.put("message", {"id": "message_" + identity, "campaign_id": campaign_id, "role": "assistant", "origin": "lab_event", "kind": "note_skip",
                                            "skip_id": identity, "thread_id": thread_id, "run_id": run_id, "created_at": now(),
                                            "content": f"The {author} proposes skipping {titles} in the problem note"}, "message.created")
        return record


def _status(items):
    return "open" if any(entry["status"] == "proposed" for entry in items) else "decided"


def decide(workspace, campaign_id, values: DecideInput, *, actor):
    """The researcher agrees to some proposed skips and declines others; an agreed skip is recorded on the note."""
    if actor != "researcher":
        raise ValueError("Only the researcher agrees to skip a section of the problem note")
    if set(values.agree) & set(values.decline):
        raise ValueError("A section is either agreed or declined")
    if not values.agree and not values.decline:
        raise ValueError("Agree to or decline at least one proposed skip")
    with workspace.lock:
        record = workspace.store.get(values.skip_id, KIND)
        if record["campaign_id"] != campaign_id:
            raise ValueError("The proposal belongs to another campaign")
        note = workspace.store.get(record["note_id"], "problem_note")
        sections, charter = _sections(workspace, note), confirmed_charter(workspace, note)
        open_items = {entry["section"]: entry for entry in record["items"] if entry["status"] == "proposed"}
        for name in [*values.agree, *values.decline]:
            if name not in open_items:
                raise ValueError(f"{TITLES[name]} is not an open skip in this proposal")
        skipped = dict(note.get("skipped") or {})
        for name in values.agree:
            entry = open_items[name]
            if _written(sections.get(name)):
                raise ValueError(f"{TITLES[name]} was written meanwhile; there is nothing to skip")
            reason = lapsed(entry, charter)
            if reason:
                raise ValueError(f"The skip of {TITLES[name]} no longer holds: {reason}; ask the lead again")
            skipped[name] = {key: entry[key] for key in ("section", "basis", "charter_refs", "reason", "confirm")} | {
                "skip_id": record["id"], "agreed_at": now(), "charter_id": charter[0] if charter else None, "charter_revision": charter[1] if charter else None}
        decisions = {**{name: "agreed" for name in values.agree}, **{name: "declined" for name in values.decline}}
        items = [{**entry, "status": decisions.get(entry["section"], entry["status"]), **({"decided_at": now()} if entry["section"] in decisions else {})} for entry in record["items"]]
        workspace.store.put(KIND, {**record, "items": items, "status": _status(items), "updated_at": now()}, "note_skip.decided")
        if values.agree:
            note.update(skipped=skipped, updated_at=now())
            workspace.store.put("problem_note", note, "problem_note.skipped")
        return {"skip_id": record["id"], "agreed": list(values.agree), "declined": list(values.decline), "skipped": sorted(skipped)}


def unskip(workspace, campaign_id, values: UnskipInput, *, actor):
    """The researcher takes a skip back: the section is to be written again before the gate opens."""
    if actor != "researcher":
        raise ValueError("Only the researcher takes back a skip")
    with workspace.lock:
        note = workspace.store.get(values.note_id, "problem_note")
        if note["campaign_id"] != campaign_id:
            raise ValueError("The note belongs to another campaign")
        skipped = dict(note.get("skipped") or {})
        if values.section not in skipped:
            raise ValueError(f"{TITLES[values.section]} is not skipped")
        skipped.pop(values.section)
        note.update(skipped=skipped, updated_at=now())
        workspace.store.put("problem_note", note, "problem_note.unskipped")
        return {"note_id": note["id"], "section": values.section, "skipped": sorted(skipped)}


def view(workspace, note):
    """What the pages show: the skips that hold (and lapsed ones with why), and the open proposals."""
    charter = confirmed_charter(workspace, note)
    skipped = {name: {**item, "lapsed": lapsed(item, charter)} for name, item in (note.get("skipped") or {}).items()}
    proposals = [row for row in workspace.store.list(KIND, note["campaign_id"]) if row["note_id"] == note["id"]]
    return {"skipped": skipped, "proposals": sorted(proposals, key=lambda row: row["created_at"])}


def register(workspace):
    commands = workspace.commands
    commands.register("problem_note.skip", lambda command, actor: decide(workspace, command.campaign_id, DecideInput(**command.payload), actor=actor), model=DecideInput)
    commands.register("problem_note.unskip", lambda command, actor: unskip(workspace, command.campaign_id, UnskipInput(**command.payload), actor=actor), model=UnskipInput)
