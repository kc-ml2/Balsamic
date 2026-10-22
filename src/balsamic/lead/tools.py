"""Tools the service layer gives the lead: thread pins, lab events, the problem note, visuals."""
from optimization_framework.agents.tools import obj, register_tool, S
from optimization_framework.contracts.base import content_hash
from optimization_framework.storage.sqlite import now

from balsamic.panels.schema import TOOL_INPUT

VISUAL_BYTES = 256 * 1024
SECTIONS = ("objective", "knobs", "observables", "noise", "constraints", "confounders", "open_questions")


def thread_of_run(tools, run):
    thread_id = (run.get("origin") or {}).get("thread_id")
    if not thread_id:
        return None
    try:
        return tools.store.get(thread_id, "thread")
    except KeyError:
        return None


def register(workspace=None):
    # Handlers are process-wide: they act on the workspace of the tools that call them, never on a captured one.
    def thread_pin(tools, agent, run, args, identity):
        thread = thread_of_run(tools, run)
        if thread is None:
            raise ValueError("This turn has no thread; pins belong to a conversation thread")
        tools.store.get_entry(args["record_id"])
        key = "excluded_ids" if args.get("exclude") else "pinned_ids"
        other = "pinned_ids" if key == "excluded_ids" else "excluded_ids"
        thread[key] = [*(rid for rid in thread.get(key) or [] if rid != args["record_id"]), args["record_id"]][-50:]
        thread[other] = [rid for rid in thread.get(other) or [] if rid != args["record_id"]]
        thread["updated_at"] = now()
        tools.store.put("thread", thread, "thread.pinned")
        return {"thread_id": thread["id"], "pinned_ids": thread["pinned_ids"], "excluded_ids": thread["excluded_ids"]}

    def lab_events(tools, agent, run, args, identity):
        thread = thread_of_run(tools, run)
        rows = [m for m in tools.store.list("message", agent["campaign_id"]) if m.get("origin") == "lab_event"
                and (thread is None or m.get("thread_id") == thread["id"])]
        return {"events": [{"id": m["id"], "at": m["created_at"], "text": m["content"], "command_id": m.get("command_id")} for m in rows[-int(args.get("limit", 20)):]]}

    def render_visual(tools, agent, run, args, identity):
        html = args["html"]
        if len(html.encode()) > VISUAL_BYTES:
            raise ValueError(f"A visual is at most {VISUAL_BYTES} bytes; this one is {len(html.encode())}")
        lowered = html.lower()
        if "<script src" in lowered or "http://" in lowered or "https://" in lowered:
            raise ValueError("A visual is self-contained: no external scripts, styles or images (it renders in a sandbox without network)")
        thread = thread_of_run(tools, run)
        record = {"id": "visual_" + content_hash(identity)[:24], "campaign_id": agent["campaign_id"], "kind": "visual", "title": args["title"][:200],
                  "content": {"html": html, "note_id": args.get("note_id"), "annotation_id": args.get("annotation_id")}, "agent_id": agent["id"], "run_id": run["id"],
                  "thread_id": thread["id"] if thread else None, "evidence_ids": args.get("evidence_ids") or [], "created_at": now()}
        tools.store.put_immutable("agent_artifact", record, "artifact.saved")
        if args.get("annotation_id"):
            from balsamic.notes.service import attach_visual
            attach_visual(tools.workspace, args["annotation_id"], record["id"])
        return {"artifact_id": record["id"], "link": f"#note?sel={record['id']}" if args.get("note_id") else f"#files/notebook?sel={record['id']}",
                "note": "Shown in a sandboxed frame; refer to it by its artifact_id."}

    def problem_note_draft(tools, agent, run, args, identity):
        from balsamic.notes.service import ensure_note, revise
        thread = thread_of_run(tools, run)
        note = ensure_note(tools.workspace, agent["campaign_id"], thread_id=thread["id"] if thread else None)
        revision = revise(tools.workspace, note["id"], {"sections": args.get("sections") or {}, "glossary": args.get("glossary"), "title": args.get("title")},
                          author="lead", source_message_id=(run.get("origin") or {}).get("message_id"), run_id=run["id"], reason=args.get("reason") or "lead draft")
        return {"note_id": note["id"], "revision": revision["revision"], "link": "#note", "sections": list(revision["sections"])}

    def problem_note_propose(tools, agent, run, args, identity):
        from balsamic.notes.service import propose_edit
        annotation = propose_edit(tools.workspace, args["annotation_id"], answer=args["answer"], proposed_edit=args.get("proposed_edit"), author="lead", run_id=run["id"])
        return {"annotation_id": annotation["id"], "status": annotation["status"], "link": f"#note?sel={annotation['id']}",
                "note": "The researcher accepts or rejects the proposed edit; the note is not changed until then."}

    def merge_note_draft(tools, agent, run, args, identity):
        thread = thread_of_run(tools, run)
        if thread is None or not (thread.get("merge_draft") or {}).get("run_id"):
            raise ValueError("No merge note was asked for in this thread")
        draft = {**thread["merge_draft"], "text": args["text"][:20000], "written_at": now(), "run_id": run["id"]}
        thread["merge_draft"] = draft
        thread["updated_at"] = now()
        tools.store.put("thread", thread, "thread.merge_draft_written")
        return {"thread_id": thread["id"], "into_thread_id": draft["into_thread_id"], "note": "The researcher edits the note and merges; nothing is merged by this call."}

    def library_search(tools, agent, run, args, identity):
        from balsamic.library.service import search
        result = search(tools.workspace, campaign_id=agent["campaign_id"], q=args.get("query", ""), limit=int(args.get("limit", 10)))
        return {"signature": result["signature"], "entries": [{"entry_id": row["entry"]["id"], "title": row["entry"]["strategy"].get("title"), "algorithm": row["entry"]["strategy"].get("algorithm"),
                 "best_score": row["entry"]["evidence"].get("best_score"), "origin_campaign_id": row["entry"]["origin_campaign_id"], "match": row["match"]} for row in result["entries"]],
                "note": "Apply one with campaign_command library.apply {entry_id, note}: a new root strategy inspired by it, reviewed like any other."}

    register_tool("library_search", "Strategies that won earlier campaigns of this problem (the library), ranked by how well their problem signature matches this campaign's.",
                  obj({"query": S, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}), library_search)
    register_tool("merge_note_draft", "Write the merge note the researcher asked for: what this branch concluded, as the request the target conversation reads. The researcher edits it and merges.",
                  obj({"text": S}, ["text"]), merge_note_draft)
    register_tool("thread_pin", "Keep a record in this thread's context (pinned) or keep it out (exclude). The researcher sees pins in the context drawer.",
                  obj({"record_id": S, "exclude": {"type": "boolean"}}, ["record_id"]), thread_pin)
    register_tool("lab_events", "What other threads and the lab did since: commands that completed elsewhere, as one line each.",
                  obj({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}), lab_events)
    def panel_render(tools, agent, run, args, identity):
        from balsamic.panels import service
        thread = thread_of_run(tools, run)
        values = {k: v for k, v in args.items() if k != "panel_id"}
        record = service.create(tools.workspace, agent["campaign_id"], values, author="lead", agent_id=agent["id"], run_id=run["id"],
                                thread_id=thread["id"] if thread else None, panel_id=args.get("panel_id"))
        return {"panel_id": record["id"], "revision": record["revision"], "link": record["page"] + f"?sel={record['id']}",
                "note": "Shown on that page and, when made in a conversation, as a card in it. Revise it by passing panel_id."}

    register_tool("panel_render", "Compose a panel for a page from the catalog: markdown, kv (bound values), table (records of a kind), number and select "
                  "fields bound to a charter path or question, chart (a trial's curve), button (a command the researcher issues), link, section. "
                  "Values resolve from the records when read; fields write through the charter service. Use it for inputs or views the fixed pages lack.",
                  TOOL_INPUT, panel_render)
    register_tool("render_visual", "Save a small self-contained HTML/SVG visual (no network, at most 256 KiB) for the researcher; for a note question give the annotation_id.",
                  obj({"title": S, "html": S, "note_id": S, "annotation_id": S, "evidence_ids": {"type": "array", "items": S, "maxItems": 50}}, ["title", "html"]), render_visual)
    register_tool("problem_note_draft", "Write or revise the campaign's problem note. sections: any of " + ", ".join(SECTIONS) +
                  ", each {text, items[], latex[]}; glossary: [{term, definition}]. Each call is a new revision; the researcher sees the diff.",
                  obj({"title": S, "sections": {"type": "object"}, "glossary": {"type": "array", "items": {"type": "object"}}, "reason": S}, ["sections"]), problem_note_draft)
    def problem_note_skip(tools, agent, run, args, identity):
        from balsamic.notes import skips
        thread = thread_of_run(tools, run)
        record = skips.propose(tools.workspace, agent["campaign_id"], skips.ProposeInput(**args), key=identity, run_id=run["id"], thread_id=thread["id"] if thread else None)
        return {"skip_id": record["id"], "sections": [item["section"] for item in record["items"]], "link": "#note",
                "note": "Nothing is skipped until the researcher agrees on the card or the note page. In your reply, say in a line per section why it "
                        "need not be written and what they confirm by agreeing, and ask about anything the records do not settle."}

    register_tool("problem_note_skip", "Propose leaving sections of the problem note unwritten, for the researcher to agree to. Each skip: section (not the "
                  "objective), basis charter (the confirmed charter already states it; charter_refs are its paths, e.g. variables, objective, metrics, "
                  "constraints, evaluator.deterministic, quantities.<name>) or not_applicable (the section does not apply to this problem), reason (why, "
                  "in the researcher's terms), confirm (the one statement the researcher confirms by agreeing). Refused when the section is written, the "
                  "cited paths are empty, or, for open questions, the charter still has unanswered ones.",
                  obj({"skips": {"type": "array", "minItems": 1, "maxItems": 6, "items": obj({
                      "section": {"enum": ["knobs", "observables", "noise", "constraints", "confounders", "open_questions"]},
                      "basis": {"enum": ["charter", "not_applicable"]}, "charter_refs": {"type": "array", "items": S, "maxItems": 12},
                      "reason": S, "confirm": S}, ["section", "basis", "reason", "confirm"])}}, ["skips"]), problem_note_skip)
    register_tool("problem_note_propose", "Answer a question the researcher asked on the note (annotation_id) and, when the note should change, propose the edit; the researcher accepts it.",
                  obj({"annotation_id": S, "answer": S, "proposed_edit": {"type": "object"}}, ["annotation_id", "answer"]), problem_note_propose)
