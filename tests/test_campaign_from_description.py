"""A campaign can start from the researcher's description alone, before its problem is defined."""
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from optimization_framework.agents.tools import INSTRUCTIONS, PiTools
from optimization_framework.api.app import create_app
from optimization_framework.contracts.commands import Command
from optimization_framework.contracts.requests import FORMULATE, CampaignInput, StudyInput, TaskInput
from optimization_framework.execution.service import Workspace
from optimization_framework.research.engine import _offline

DESCRIPTION = ("We are 10 people and two always-on agents sharing one LLM token budget of $500 a month.\n\n"
               "Each user has a daily cap; the vendor also limits each seat per 5 hours and per week.")


def creation(identity='create_described', **payload):
    return Command(id=identity, campaign_id='campaign_described', operation='campaign.create', expected_revision=0,
        payload={'name': 'Token sharing', 'description': DESCRIPTION, 'llm_budget_usd': 1, **payload})


def described(workspace):
    return workspace.commands.execute(creation())['outcome']['campaign']


def test_a_description_alone_creates_a_campaign_whose_notebook_opens_with_it(tmp_path):
    workspace = Workspace(tmp_path)
    receipt = workspace.commands.execute(creation())
    campaign = receipt['outcome']['campaign']
    cid = campaign['id']
    # No problem, so no task, and a study that holds no instance yet.
    assert workspace.current_tasks(cid) == [] and campaign['objective'] == FORMULATE
    study = workspace.store.get(campaign['active_study_id'], 'study')
    assert study['instance_ids'] == [] and study['goal'] == FORMULATE
    # The description is not kept on the campaign, which every state poll carries: it is the notebook's first cell.
    assert 'description' not in workspace.store.get(cid, 'campaign')
    view = workspace.formulation.view(cid)
    [cell] = view['cells']
    assert (cell['name'], cell['kind'], cell['status'], cell['author'], cell['revision']) == ('problem', 'markdown', 'agreed', 'researcher', 1)
    assert cell['source'].startswith('## The problem in words\n\nWe are 10 people') and 'budget of \\$500 a month' in cell['source']
    assert cell['reason'] == "The researcher's description"
    # The rest of the outline is still offered, without a second problem cell.
    assert [op['name'] for op in view['outline']][:2] == ['sets', 'parameters'] and 'problem' not in [op['name'] for op in view['outline']]
    assert [event['kind'] for event in workspace.store.events(cid)].count('formulation.updated') == 1
    # The same command again is the same receipt, and writes nothing more.
    events = len(workspace.store.events(cid))
    assert workspace.commands.execute(creation()) == receipt
    assert len(workspace.store.events(cid)) == events and len(workspace.store.list('formulation_cell', cid)) == 1


def test_a_campaign_needs_a_problem_or_a_description(tmp_path):
    with pytest.raises(ValidationError, match='Describe the problem'):
        CampaignInput(name='Nothing yet')
    with pytest.raises(ValidationError, match='Describe the problem'):
        CampaignInput(name='Blank words', description='   \n')
    workspace = Workspace(tmp_path)
    task = TaskInput(name='Quadratic', problem_id='bounded_continuous', configuration={})
    plain = workspace.create_campaign(CampaignInput(name='Chosen problem', tasks=[task]))
    assert workspace.formulation.summary(plain['id']) is None and plain['objective'].startswith('Develop an effective optimizer')
    # A chosen problem with a description keeps both: the task, and the description in the notebook.
    both = workspace.create_campaign(CampaignInput(name='Both', description='Minimise the quadratic.', tasks=[task]))
    assert len(workspace.current_tasks(both['id'])) == 1 and workspace.formulation.view(both['id'])['cells'][0]['name'] == 'problem'
    # A researcher's own objective is kept, problem or not.
    own = workspace.create_campaign(CampaignInput(name='Own objective', description='Share tokens.', objective='Find fair caps.'))
    assert own['objective'] == 'Find fair caps.'


def test_a_campaign_without_a_problem_is_shown_exported_and_removed(tmp_path):
    app = create_app(tmp_path / 'api', start_workers=False)
    workspace = app.state.workspace
    cid = described(workspace)['id']
    with TestClient(app) as client:
        state = client.get(f'/api/state?campaign_id={cid}').json()
        assert state['campaign']['id'] == cid and state['tasks'] == [] and state['formulation']['counts']['agreed'] == 1
        assert client.get('/api/v1/problems').status_code == 200
        assert client.get(f'/api/campaigns/{cid}/export').status_code == 200
        assert '\\$500' in client.get(f'/api/campaigns/{cid}/formulation/export').text
    workspace.memory.sync(cid, force=True)
    document = workspace.store.get(workspace.memory.state(cid)['context_id'], 'context_revision')['document']
    assert 'No problem is defined yet' in document
    workspace.removal.remove(cid, 'summary')
    kept = workspace.removal.directory / cid
    assert 'No problem was defined' in (kept / 'summary.md').read_text()
    assert 'budget of \\$500 a month' in (kept / 'formulation.md').read_text()


def test_a_problem_is_attached_later_with_a_linked_study(tmp_path):
    workspace = Workspace(tmp_path)
    campaign = described(workspace)
    cid, first_study = campaign['id'], campaign['active_study_id']
    workspace.commands.execute(Command(id='attach', campaign_id=cid, operation='campaign.update', expected_revision=1,
        payload={'tasks': [{'name': 'Quadratic', 'problem_id': 'bounded_continuous', 'configuration': {}}]}))
    revised = workspace.store.get(cid, 'campaign')
    study = workspace.store.get(revised['active_study_id'], 'study')
    assert len(workspace.current_tasks(cid)) == 1 and study['parent_study_id'] == first_study and len(study['instance_ids']) == 1
    # The notebook stays as it was.
    assert [cell['name'] for cell in workspace.formulation.view(cid)['cells']] == ['problem']


def test_nothing_is_proposed_or_probed_without_a_problem(tmp_path):
    workspace = Workspace(tmp_path)
    cid = described(workspace)['id']
    for mode in ('generate', 'probe'):
        with pytest.raises(ValueError, match='no problem defined yet'):
            workspace.commands.execute(Command(id='start_' + mode, campaign_id=cid, operation='research.start', expected_revision=1,
                payload={'mode': mode, 'message': 'Propose methods'}))
    with pytest.raises(ValueError, match='at least one problem instance'):
        workspace.create_study(cid, StudyInput(goal='Compare methods'))
    # Without a task, the curated strategies of the legacy grating are not offered as ideas.
    assert _offline({'mode': 'generate'}, {'hypotheses': [], 'tasks': [], 'trials': []})['hypotheses'] == []


@pytest.fixture
def lead_of_a_described_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv('GRATING_LLM_ENABLED', 'true')
    monkeypatch.setenv('GRATING_LLM_DISABLED', 'false')
    monkeypatch.setenv('GRATING_PI_PROVIDER', 'openai-codex')
    workspace = Workspace(tmp_path)
    return workspace, described(workspace)


def test_the_review_is_the_researchers_request_and_the_lead_is_told_no_problem_is_defined(lead_of_a_described_campaign):
    workspace, campaign = lead_of_a_described_campaign
    cid = campaign['id']
    # Creating the campaign and activating its team start nothing.
    workspace.commands.execute(Command(id='activate', campaign_id=cid, expected_revision=1, operation='agent.activate', payload={}))
    assert workspace.store.list('agent_run', cid) == []
    request = 'Review the problem description in cell problem.'
    workspace.commands.execute(Command(id='review', campaign_id=cid, expected_revision=1, operation='agent.message',
        payload={'message': request}))
    [run] = workspace.store.list('agent_run', cid)
    assert request in str(run['input'])
    lead = workspace.store.list('agent_session', cid)[0]
    seen = PiTools(workspace.pi).inspect(lead)
    assert seen['tasks'] == [] and seen['problem']['defined'] is False and 'cell problem' in seen['problem']['note']
    assert 'a campaign with no task has no problem defined yet' in INSTRUCTIONS.lower()
