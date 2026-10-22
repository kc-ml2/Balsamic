"""The problem-definition gate: proposals, allocations, interventions and autopilot wait until every section of the note is written,
the note is adopted at its current revision, the charter is confirmed and the tasks are installed."""
import pytest

from optimization_framework.storage.sqlite import now

from support import QUADRATIC, activate_team, command
from balsamic.autopilot import rounds
from balsamic.evolution.stages import definition, next_step

MAIN = "thread_main_demo"
FULL = {"objective": {"text": "Maximise efficiency"}, "knobs": {"text": "period 300-600 nm, fill factor 0.2-0.8"}, "observables": {"text": "first-order efficiency"},
        "noise": {"text": "fabrication tolerance 5 nm"}, "constraints": {"text": "period below the wavelength"}, "confounders": {"text": "solver mesh"},
        "open_questions": {"text": "none remaining"}}


@pytest.fixture
def formulating(client):
    return command(client, "campaign.create", "demo", {"name": "Demo", "objective": "Design a deflector with higher efficiency"}, 0)["outcome"]["campaign"]


@pytest.fixture
def team(client, workspace, formulating, monkeypatch):
    return activate_team(client, workspace, formulating, monkeypatch)


def fake_formulator(workspace, monkeypatch):
    monkeypatch.setattr(workspace.problem_imports, "create", lambda values: {"id": "import_1", "status": "collecting"})
    monkeypatch.setattr(workspace.problem_imports, "start", lambda import_id: None)
    monkeypatch.setattr(workspace.problem_imports, "sync", lambda import_id: {"id": import_id, "status": "running", "charter_id": None, "error": None, "reply": None})


def test_the_gate_names_the_missing_sections_and_refuses_proposals_allocations_and_autopilot(client, workspace, formulating, team):
    campaign = lambda: workspace.store.get("demo", "campaign")
    command(client, "problem_note.revise", "demo", {"note_id": "note_demo", "sections": {"objective": {"text": "Maximise efficiency"}, "knobs": {"text": "period"}}}, formulating["version"])
    state = definition(workspace, campaign())
    assert state["ready"] is False and state["step"]["kind"] == "sections" and state["missing"] == ["Observables", "Noise", "Constraints", "Confounders", "Open questions"]
    row = client.get("/api/v1/campaigns").json()["campaigns"][0]
    assert row["next_step"]["kind"] == "sections" and "Observables" in row["next_step"]["text"] and row["next_step"]["page"] == "#note"
    # Every mode of work is refused with the step and its page: the researcher's switch, the lead's generation, an allocation, a batch.
    refused = command(client, "autopilot.set", "demo", {"enabled": True, "compute_cap_seconds": 600, "rationale": "overnight"}, campaign()["version"], expect=409)
    assert "waits for the problem definition: complete the problem note (#note)" in refused["detail"] and "Observables" in refused["detail"]
    assert (campaign()["autopilot"] or {}).get("enabled", False) is False
    for operation, payload in (("research.generate", {"operation": "diversify", "count": 2}), ("hypothesis.allocate", {"hypothesis_id": "nope", "worker_seconds": 10, "rationale": "x"}),
                               ("intervention.batch", {"items": [{"operation": "research.generate", "payload": {"operation": "diversify", "count": 1}}], "rationale": "x"})):
        refused = command(client, operation, "demo", payload, campaign()["version"], expect=409, origin={"thread_id": MAIN})
        assert "waits for the problem definition" in refused["detail"], operation
    # The lead reads the same gate in campaign_inspect.
    from optimization_framework.agents.tools import PiTools
    inspected = PiTools(workspace.pi).inspect(team)
    assert inspected["definition"]["ready"] is False and inspected["definition"]["missing"][0] == "Observables"
    assert "Gate now: closed" in workspace.lead_instructions(team) and "Observables" in workspace.lead_instructions(team)


def test_the_gate_opens_after_sections_adoption_confirmation_and_tasks_and_closes_when_the_note_changes(client, workspace, formulating, team, monkeypatch):
    campaign = lambda: workspace.store.get("demo", "campaign")
    command(client, "problem_note.revise", "demo", {"note_id": "note_demo", "sections": FULL}, formulating["version"])
    assert next_step(workspace, campaign())["kind"] == "adopt"
    fake_formulator(workspace, monkeypatch)
    current = workspace.store.get("note_demo", "problem_note")["current_revision"]
    command(client, "problem_note.adopt", "demo", {"note_id": "note_demo", "revision": current}, campaign()["version"])
    assert next_step(workspace, campaign())["kind"] == "confirm"
    from optimization_framework.problem_charter.service import KIND
    workspace.store.put(KIND, {"id": "charter_x", "campaign_id": "demo", "status": "proposed", "revision": 1, "charter": {}, "created_at": now()})
    note = workspace.store.get("note_demo", "problem_note"); note["charter_id"] = "charter_x"; workspace.store.put("problem_note", note)
    assert next_step(workspace, campaign())["kind"] == "confirm"
    workspace.store.put(KIND, {"id": "charter_x", "campaign_id": "demo", "status": "confirmed", "revision": 1, "charter": {}, "created_at": now()})
    assert next_step(workspace, campaign())["kind"] == "tasks"
    command(client, "campaign.update", "demo", {"tasks": [QUADRATIC]}, campaign()["version"])
    assert definition(workspace, campaign()) == {"ready": True, "step": None, "missing": []}
    assert "Gate now: open" in workspace.lead_instructions(team)
    settings = command(client, "autopilot.set", "demo", {"enabled": True, "compute_cap_seconds": 600, "rationale": "overnight"}, campaign()["version"])["outcome"]["autopilot"]
    assert settings["enabled"] is True and rounds.round_state(workspace, "demo")["can_start"] is True
    # A revision after adoption closes the gate again: no round starts until the note is adopted again and the charter follows.
    command(client, "problem_note.revise", "demo", {"note_id": "note_demo", "sections": {"noise": {"text": "tolerance 10 nm"}}}, campaign()["version"])
    step = next_step(workspace, campaign())
    assert step["kind"] == "adopt" and step["title"] == "Adopt the problem note again"
    state = rounds.round_state(workspace, "demo")
    assert state["can_start"] is False and state["reason"].startswith("the problem definition is incomplete: adopt the problem note again")


def test_a_campaign_defined_by_its_tasks_without_a_note_passes_the_gate(client, workspace, campaign):
    assert definition(workspace, workspace.store.get("demo", "campaign")) == {"ready": True, "step": None, "missing": []}


def test_an_evaluator_that_cannot_start_here_is_the_next_step_and_what_the_lead_and_autopilot_are_told(client, workspace, campaign, monkeypatch):
    """The 2D deflector's first probe failed on a missing PyTorch; the check finds that before anything is queued and says what to install."""
    from optimization_framework.evaluation import preflight
    monkeypatch.setattr(preflight, "_results", {})
    monkeypatch.setattr(preflight, "_running", {})
    missing = "ModuleNotFoundError: The 2D MEENT solver runs on PyTorch, which is not installed; install it with scripts/install-meent"
    monkeypatch.setattr(preflight, "_probe", lambda problem, device: {"ok": False, "error": missing})
    team = activate_team(client, workspace, campaign, monkeypatch)
    assert "Evaluator now: cannot start in this lab" in workspace.lead_instructions(team) and missing in workspace.lead_instructions(team)
    step = next_step(workspace, workspace.store.get("demo", "campaign"))
    assert step["kind"] == "evaluator" and step["error"] == missing and step["page"] == "" and "No experiment or runtime probe" in step["text"]
    assert client.get("/api/v1/campaigns").json()["campaigns"][0]["next_step"]["kind"] == "evaluator"
    assert definition(workspace, workspace.store.get("demo", "campaign"))["ready"], "the problem is defined; only its evaluator is missing"
    tasks = [t for t in workspace.store.list("task", "demo")]
    refused = command(client, "trial.create", "demo", {"task_id": tasks[0]["id"], "algorithm": "random", "max_steps": 3, "wall_seconds": 5},
                      workspace.store.get("demo", "campaign")["version"], expect=409)
    assert "cannot start in this lab" in refused["detail"] and workspace.store.list("trial", "demo") == []
    command(client, "autopilot.set", "demo", {"enabled": True, "compute_cap_seconds": 300, "rationale": "overnight"}, workspace.store.get("demo", "campaign")["version"])
    assert rounds.round_state(workspace, "demo")["reason"] == "the evaluator cannot start in this lab: " + missing
    # Installed: the next check passes, and the step is gone.
    monkeypatch.setattr(preflight, "_results", {})
    monkeypatch.setattr(preflight, "_probe", lambda problem, device: {"ok": True, "error": None})
    assert workspace.evaluators.readiness(tasks[0])["runnable"] and next_step(workspace, workspace.store.get("demo", "campaign")) is None
