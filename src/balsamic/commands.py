"""Balsamic's additions to the command bus. Each feature module registers its own operations here."""
from balsamic import housekeeping
from optimization_framework.contracts.base import Contract
from optimization_framework.storage.sqlite import now
from pydantic import Field
from typing import Literal


class HypothesisEditInput(Contract):
    """Fields of a recorded proposal the lead or the moderator may correct. Text fields any time; the search lane and its
    parameters only before the proposal has run, because trials are bound to them."""
    hypothesis_id: str
    title: str | None = Field(default=None, min_length=1, max_length=300)
    short_title: str | None = Field(default=None, min_length=1, max_length=40)
    mechanism: str | None = Field(default=None, max_length=20000)
    rationale: str | None = Field(default=None, max_length=20000)
    algorithm: str | None = Field(default=None, min_length=1, max_length=100)
    algorithm_config: dict | None = None
    reason: str = Field(default="", max_length=2000)


class StagePinInput(Contract):
    """Pin the campaign's stage badge, or clear the pin so it follows the records again."""
    stage: Literal["formulate", "generate", "optimize", "report"] | None = None
    reason: str = Field(default="", max_length=2000)


def register(workspace):
    commands = workspace.commands
    # The Pi controller syncs with the harness when the harness notifies and on every maintenance tick (the legacy
    # campaign manager used to drive it): queued turns are submitted, finished ones received.
    previous_tick = workspace.on_manager_tick

    def sync_agents():
        if previous_tick:
            previous_tick()
        for campaign in workspace.store.list("campaign"):
            if workspace.pi.owns(campaign["id"]):
                workspace.pi.tick(campaign["id"])
        housekeeping.tick(workspace)
    workspace.on_manager_tick = sync_agents

    def thread_revision(command):
        origin = command.origin
        thread_id = origin.thread_id if origin else None
        return workspace.memory.thread_revision(thread_id) if thread_id else 0

    commands.thread_revision = thread_revision

    def stage_pin(command, actor):
        if actor != "researcher":
            raise ValueError("Only the researcher pins the stage")
        values = StagePinInput(**command.payload)
        campaign = workspace.store.get(command.campaign_id, "campaign")
        campaign["stage_pin"] = values.stage
        campaign["stage_pin_reason"] = values.reason
        workspace.store.put("campaign", campaign, "campaign.stage_pinned")
        return {"campaign_id": campaign["id"], "stage_pin": values.stage}

    commands.register("campaign.stage_pin", stage_pin, model=StagePinInput)

    def hypothesis_edit(command, actor):
        from optimization_framework.execution.service import algorithms
        values = HypothesisEditInput(**command.payload)
        hypothesis = workspace.store.get(values.hypothesis_id, "hypothesis")
        if hypothesis["campaign_id"] != command.campaign_id:
            raise ValueError("The proposal belongs to another campaign")
        patch = {k: v for k, v in values.model_dump(exclude={"hypothesis_id", "reason", "schema_version"}).items() if v is not None}
        if not patch:
            raise ValueError("Nothing to change: give at least one field")
        ran = [t["id"] for t in workspace.store.list("trial", command.campaign_id) if t.get("hypothesis_id") == hypothesis["id"]]
        if ran and ({"algorithm", "algorithm_config"} & set(patch)):
            raise ValueError(f"The search lane is bound to {len(ran)} trial(s) of this proposal; revise it as a new proposal (parent_links) instead")
        if "algorithm" in patch:
            installed = {item["id"] for item in algorithms()}
            if patch["algorithm"] not in installed and patch["algorithm"] != "custom":
                raise ValueError("Unknown algorithm; campaign_inspect lists the installed ones (or use custom)")
            hypothesis["executable"] = patch["algorithm"] in installed
            hypothesis["algorithm_chosen"] = True
        if "short_title" in patch:
            hypothesis.setdefault("housekeeping", {})["label"] = {"status": "done", "by": actor, "at": now()}
        before = {k: hypothesis.get(k) for k in patch}
        hypothesis.update(patch)
        hypothesis["edits"] = (hypothesis.get("edits") or [])[-20:] + [{"at": now(), "by": actor, "command_id": command.id, "fields": sorted(patch), "before": before, "reason": values.reason}]
        workspace.store.put("hypothesis", hypothesis, "hypothesis.edited")
        return {"hypothesis_id": hypothesis["id"], "fields": sorted(patch)}

    commands.register("hypothesis.edit", hypothesis_edit, model=HypothesisEditInput, delegable=True)
    # The lead runs the workspace's operations on request; the page agents keep their own lists.
    workspace.lead_operations = {"hypothesis.allocate", "hypothesis.edit", "intervention.batch", "research.generate", "hypothesis.nominate", "thread.pin", "problem_note.revise", "library.apply"}
    from balsamic.threads.service import register as register_threads
    register_threads(workspace)
    from balsamic.panels.service import register as register_panels
    register_panels(workspace)
    from balsamic.evolution.generate import register as register_generate
    register_generate(workspace)
    from balsamic.notes.service import register as register_notes
    register_notes(workspace)
    from balsamic.notes.skips import register as register_skips
    register_skips(workspace)
    from balsamic.evolution.interventions import register as register_interventions
    register_interventions(workspace)
    from balsamic.library.service import register as register_library
    register_library(workspace)
    from balsamic.autopilot.settings import register as register_autopilot
    from balsamic.autopilot.rounds import register as register_rounds
    register_autopilot(workspace)
    register_rounds(workspace)
    from balsamic.evolution.gate import register as register_gate
    register_gate(workspace)   # last: it wraps the handlers that start work
    from balsamic.lead.tools import register as register_tools
    from balsamic.lead.instructions import lead_instructions
    if "thread_pin" not in __import__("optimization_framework.agents.tools", fromlist=["EXTRA_TOOLS"]).EXTRA_TOOLS:
        register_tools()   # process-wide; handlers act on the workspace of the tools that call them
    workspace.lead_instructions = lead_instructions(workspace)
