"""Skipping a section of the problem note: the lead proposes each skip with its basis, reason and what the researcher confirms;
only the researcher's agreement skips it; the note is not reopened; a skip on the charter lapses when the charter stops stating it."""
import pytest

from optimization_framework.problem_charter.service import KIND as CHARTER_KIND, REVISION
from optimization_framework.storage.sqlite import now

from support import QUADRATIC, activate_team, command, submitted, tool_call
from balsamic.evolution.stages import definition, next_step
from balsamic.notes import service as notes, skips

MAIN = "thread_main_demo"
from optimization_framework.contracts.problem_charter import ProblemCharter

QUESTION = {"id": "spec", "question": "Which benchmark defines the problem?", "options": ["installed", "custom"]}
CHARTER = ProblemCharter.model_validate({
    "title": "Deflector", "variables": [{"name": "design", "description": "Material of every cell", "representation": "binary", "shape": ["grid_y", "grid_x"], "units": "1", "meaning": "1 = silicon"}],
    "objective": {"name": "mean_plus1_transmission", "direction": "maximize", "units": "fraction of incident power", "definition": "(TE + TM) / 2"},
    "metrics": [{"name": "te_plus1_transmission", "units": "fraction of incident power"}],
    "evaluator": {"kind": "installed", "problem_id": "meent_2d_dual_polarization_deflector", "deterministic": True},
    "quantities": {"thickness_nm": {"description": "Layer thickness", "value": 325, "units": "nm"}, "grid_x": {"description": "Cells in x", "kind": "integer", "value": 4, "units": "1"},
                   "grid_y": {"description": "Cells in y", "kind": "integer", "value": 2, "units": "1"}},
    "questions": [QUESTION]}).model_dump(mode="json")


@pytest.fixture
def formulating(client):
    return command(client, "campaign.create", "demo", {"name": "Demo", "objective": "Design a deflector with higher efficiency"}, 0)["outcome"]["campaign"]


@pytest.fixture
def team(client, workspace, formulating, monkeypatch):
    return activate_team(client, workspace, formulating, monkeypatch)


def confirm(workspace, revision, charter):
    """The formulator's charter of the note's adoption, confirmed at this revision."""
    workspace.store.put_immutable(REVISION, {"id": f"charter_x_r{revision}", "charter_id": "charter_x", "revision": revision, "charter": charter, "created_at": now()})
    workspace.store.put(CHARTER_KIND, {"id": "charter_x", "campaign_ids": ["demo"], "status": "confirmed", "revision": revision, "confirmed_revision": revision, "charter": charter, "created_at": now(), "updated_at": now()})


@pytest.fixture
def adopted(client, workspace, formulating, team, monkeypatch):
    """The researcher's case: a note adopted with its objective alone, the charter confirmed, the tasks installed."""
    monkeypatch.setattr(workspace.problem_imports, "create", lambda values: {"id": "import_1", "status": "collecting"})
    monkeypatch.setattr(workspace.problem_imports, "start", lambda import_id: None)
    monkeypatch.setattr(workspace.problem_imports, "sync", lambda import_id: {"id": import_id, "status": "ready", "charter_id": None, "error": None, "reply": None})
    client.get("/api/v1/campaigns/demo/problem-note")
    command(client, "problem_note.adopt", "demo", {"note_id": "note_demo", "revision": 1}, formulating["version"])
    note = workspace.store.get("note_demo", "problem_note"); note["charter_id"] = "charter_x"; workspace.store.put("problem_note", note)
    confirm(workspace, 1, CHARTER)
    command(client, "campaign.update", "demo", {"tasks": [QUADRATIC]}, workspace.store.get("demo", "campaign")["version"])
    return workspace.store.get("demo", "campaign")


def lead_turn(client, workspace):
    campaign = workspace.store.get("demo", "campaign")
    receipt = command(client, "agent.message", "demo", {"message": "Which of the empty sections can be skipped?", "thread_id": MAIN}, campaign["version"], origin={"thread_id": MAIN})
    return submitted(workspace, "pi_run_" + receipt["id"])


def skip(section, basis, refs=(), reason="The confirmed charter already states this in full.", confirm="Nothing beyond the charter belongs here."):
    return {"section": section, "basis": basis, "charter_refs": list(refs), "reason": reason, "confirm": confirm}


def test_the_lead_proposes_skips_the_researcher_agrees_and_the_gate_opens_without_a_new_adoption(client, workspace, team, adopted):
    campaign = lambda: workspace.store.get("demo", "campaign")
    assert definition(workspace, campaign())["missing"] == ["Knobs", "Observables", "Noise", "Constraints", "Confounders", "Open questions"]
    run = lead_turn(client, workspace)
    # A skip needs a basis that holds: never the objective, never an empty charter path, never open questions the charter has not answered.
    assert "objective" in str(tool_call(workspace, team, run, "problem_note_skip", {"skips": [skip("objective", "not_applicable")]}, call_id="s0"))
    assert "holds nothing at tables" in str(tool_call(workspace, team, run, "problem_note_skip", {"skips": [skip("knobs", "charter", ["tables"])]}, call_id="s1"))
    assert "Name the charter paths" in str(tool_call(workspace, team, run, "problem_note_skip", {"skips": [skip("knobs", "charter")]}, call_id="s2"))
    assert "unanswered questions" in str(tool_call(workspace, team, run, "problem_note_skip", {"skips": [skip("open_questions", "charter", ["questions"])]}, call_id="s3"))
    first = {"skips": [
        skip("knobs", "charter", ["variables", "quantities.thickness_nm"]), skip("observables", "charter", ["objective", "metrics"]),
        skip("noise", "charter", ["evaluator.deterministic"], reason="The evaluator is a deterministic RCWA solve: one design gives one score.", confirm="Fabrication tolerance is not studied in this campaign."),
        skip("constraints", "not_applicable", reason="Every binary pattern on the grid is allowed; there is no fabrication rule.", confirm="No minimum feature size applies.")]}
    proposed = tool_call(workspace, team, run, "problem_note_skip", first, call_id="s4")
    assert proposed["sections"] == ["knobs", "observables", "noise", "constraints"], proposed
    assert tool_call(workspace, team, run, "problem_note_skip", first, call_id="s4")["skip_id"] == proposed["skip_id"], "a repeated call returns the same proposal"
    assert len(workspace.store.list(skips.KIND, "demo")) == 1
    card = [m for m in client.get(f"/api/v1/threads/{MAIN}/messages").json()["messages"] if m.get("kind") == "note_skip"]
    assert len(card) == 1 and card[0]["skip_id"] == proposed["skip_id"] and "Knobs, Observables, Noise, Constraints" in card[0]["content"]
    # Nothing is skipped yet; the step says a proposal waits, and the lead's briefing says so too.
    step = next_step(workspace, campaign())
    assert step["kind"] == "sections" and step["proposed_skips"] == ["Knobs", "Observables", "Noise", "Constraints"] and "agree or decline" in step["text"]
    assert "Your skip proposal waits for the researcher" in workspace.lead_instructions(team)
    with pytest.raises(ValueError, match="Only the researcher"):
        skips.decide(workspace, "demo", skips.DecideInput(skip_id=proposed["skip_id"], agree=["knobs"]), actor="lead")
    decided = command(client, "problem_note.skip", "demo", {"skip_id": proposed["skip_id"], "agree": ["knobs", "observables", "noise"], "decline": ["constraints"]}, campaign()["version"])["outcome"]
    assert decided["skipped"] == ["knobs", "noise", "observables"]
    state = definition(workspace, campaign())
    assert state["missing"] == ["Constraints", "Confounders", "Open questions"] and state["step"]["skipped"] == ["Knobs", "Observables", "Noise"] and state["step"]["proposed_skips"] == []
    note = workspace.store.get("note_demo", "problem_note")
    assert note["current_revision"] == 1 and note["status"] == "adopted", "a skip changes no text, so the note is not reopened"
    assert note["skipped"]["noise"]["confirm"] == "Fabrication tolerance is not studied in this campaign." and note["skipped"]["noise"]["charter_revision"] == 1
    view = client.get("/api/v1/campaigns/demo/problem-note").json()["skips"]
    assert set(view["skipped"]) == {"knobs", "observables", "noise"} and view["proposals"][0]["status"] == "decided"
    assert [i["status"] for i in view["proposals"][0]["items"]] == ["agreed", "agreed", "agreed", "declined"]
    # The charter's question is answered and confirmed again; the rest is proposed and agreed, and the gate opens.
    confirm(workspace, 2, {**CHARTER, "questions": [{**CHARTER["questions"][0], "answer": "installed"}]})
    second = tool_call(workspace, team, run, "problem_note_skip", {"skips": [
        skip("constraints", "not_applicable", reason="Every binary pattern on the grid is allowed, as you said in the chat.", confirm="No minimum feature size applies."),
        skip("confounders", "not_applicable", reason="One instance at one fixed fidelity; every method is scored the same way.", confirm="No method comparison crosses fidelities or instances."),
        skip("open_questions", "charter", ["questions"], reason="The charter's one question is answered.", confirm="Nothing about the problem is still open.")]}, call_id="s5")
    command(client, "problem_note.skip", "demo", {"skip_id": second["skip_id"], "agree": ["constraints", "confounders", "open_questions"]}, campaign()["version"])
    assert definition(workspace, campaign()) == {"ready": True, "step": None, "missing": []}
    assert "Gate now: open" in workspace.lead_instructions(team)


def test_a_skip_on_the_charter_lapses_when_the_charter_stops_stating_it_and_the_researcher_can_take_one_back(client, workspace, team, adopted):
    campaign = lambda: workspace.store.get("demo", "campaign")
    run = lead_turn(client, workspace)
    proposed = tool_call(workspace, team, run, "problem_note_skip", {"skips": [skip("observables", "charter", ["metrics"]), skip("noise", "not_applicable", reason="A deterministic solver gives one score per design.")]}, call_id="s1")
    command(client, "problem_note.skip", "demo", {"skip_id": proposed["skip_id"], "agree": ["observables", "noise"]}, campaign()["version"])
    assert "Observables" not in definition(workspace, campaign())["missing"]
    confirm(workspace, 2, {**CHARTER, "metrics": []})
    assert "Observables" in definition(workspace, campaign())["missing"]
    assert client.get("/api/v1/campaigns/demo/problem-note").json()["skips"]["skipped"]["observables"]["lapsed"] == "the confirmed charter no longer states metrics"
    command(client, "problem_note.unskip", "demo", {"note_id": "note_demo", "section": "noise"}, campaign()["version"])
    assert "Noise" in definition(workspace, campaign())["missing"]
    refused = command(client, "problem_note.unskip", "demo", {"note_id": "note_demo", "section": "noise"}, campaign()["version"], expect=409)
    assert "not skipped" in refused["detail"]


def test_a_written_section_cannot_be_skipped_and_a_skipped_one_is_named_to_the_formulator(client, workspace, formulating, team):
    client.get("/api/v1/campaigns/demo/problem-note")
    command(client, "problem_note.revise", "demo", {"note_id": "note_demo", "sections": {"knobs": {"text": "binary cells"}}}, formulating["version"])
    run = lead_turn(client, workspace)
    assert "Knobs is written" in str(tool_call(workspace, team, run, "problem_note_skip", {"skips": [skip("knobs", "not_applicable")]}, call_id="s1"))
    assert "needs a confirmed charter" in str(tool_call(workspace, team, run, "problem_note_skip", {"skips": [skip("noise", "charter", ["evaluator"])]}, call_id="s2"))
    proposed = tool_call(workspace, team, run, "problem_note_skip", {"skips": [skip("noise", "not_applicable", reason="A deterministic solver gives one score per design.")]}, call_id="s3")
    command(client, "problem_note.skip", "demo", {"skip_id": proposed["skip_id"], "agree": ["noise"]}, workspace.store.get("demo", "campaign")["version"])
    note = workspace.store.get("note_demo", "problem_note")
    text = notes.markdown(notes.revision_of(workspace, "note_demo", note["current_revision"]), skips.held(workspace, note))
    assert "## Noise\n\nSkipped with the researcher's agreement: A deterministic solver gives one score per design." in text
