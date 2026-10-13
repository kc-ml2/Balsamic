"""The formulation notebook: one way to change it, for the researcher and the agents, and what each may do."""
from fastapi.testclient import TestClient
import pytest

from optimization_framework.agents.tools import PiTools
from optimization_framework.api.app import create_app
from optimization_framework.contracts.commands import Command
from optimization_framework.contracts.requests import CampaignInput, TaskInput
from optimization_framework.execution.service import Workspace
from optimization_framework.formulation.rules import check, definitions
from optimization_framework.formulation.service import Refusal


def campaign_in(workspace, name='Token sharing'):
    return workspace.create_campaign(CampaignInput(name=name, compute_budget_seconds=100, validation_reserve_seconds=10,
        tasks=[TaskInput(name='Placeholder', problem_id='bounded_continuous', configuration={})]))


@pytest.fixture
def notebook(tmp_path):
    workspace = Workspace(tmp_path)
    return workspace, campaign_in(workspace)


@pytest.fixture
def team(tmp_path, monkeypatch):
    """A campaign whose lead agent has one active turn, as in tests/test_pi_agents.py."""
    monkeypatch.setenv('GRATING_LLM_ENABLED', 'true')
    monkeypatch.setenv('GRATING_LLM_DISABLED', 'false')
    monkeypatch.setenv('GRATING_PI_PROVIDER', 'openai-codex')
    workspace = Workspace(tmp_path)
    campaign = campaign_in(workspace)
    workspace.commands.execute(Command(id='activate', campaign_id=campaign['id'], expected_revision=1,
        operation='agent.activate', payload={'objective': 'Formulate the problem with the researcher'}))
    lead = workspace.store.list('agent_session')[0]
    workspace.pi._request(campaign['id'], True)
    run = workspace.pi.enqueue(lead, 'pi_run_first_request', 'Write the sets and the budget constraint')
    run['status'] = 'submitted'; workspace.store.put('agent_run', run)
    return workspace, campaign, workspace.store.get(lead['id'], 'agent_session'), run


def invoke(team, name, args=None, call_id='call'):
    workspace, _, agent, run = team
    return PiTools(workspace.pi).call(agent['id'], run['id'], call_id, name, args or {})


def markdown(name, source, **more):
    return {'op': 'create', 'kind': 'markdown', 'name': name, 'source': source, **more}


def code(name, source, **more):
    return {'op': 'create', 'kind': 'code', 'name': name, 'source': source, **more}


def test_a_batch_is_applied_together_or_not_at_all(notebook):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    assert formulation.summary(cid) is None and formulation.view(cid)['outline']
    first = formulation.edit(cid, [markdown('budget', 'Total tokens: $\\sum_i x_i \\le B$'), code('budget_code', 'B = 4_000_000')])
    assert first['notebook_revision'] == 1 and [cell['name'] for cell in first['cells']] == ['budget', 'budget_code']
    events = len(workspace.store.events(cid))
    # The second operation breaks a rule, so the first is not kept either.
    with pytest.raises(Refusal, match='defined in both budget_code and again'):
        formulation.edit(cid, [markdown('absence', 'Absent users free their share.'), code('again', 'B = 1')])
    view = formulation.view(cid)
    assert [cell['name'] for cell in view['cells']] == ['budget', 'budget_code'] and view['notebook']['revision'] == 1
    assert len(workspace.store.events(cid)) == events and len(workspace.store.list('formulation_cell_revision', cid)) == 2
    # A whole batch is one revision of the notebook and one event, whatever its size.
    formulation.edit(cid, [markdown('absence', 'Absent users free their share.', after='budget'), code('caps', 'caps = [B / 12] * 12'),
        {'op': 'move', 'cell': 'budget_code', 'before': 'budget'}])
    view = formulation.view(cid)
    assert [cell['name'] for cell in view['cells']] == ['budget_code', 'budget', 'absence', 'caps']
    assert view['notebook']['revision'] == 2 and view['notebook']['counts'] == {'cells': 4, 'proposed': 4, 'agreed': 0, 'open': 0}
    assert len(workspace.store.events(cid)) == events + 1 and not view['outline']
    # A move changes the order only: the cell keeps its revision.
    assert next(cell for cell in view['cells'] if cell['name'] == 'budget_code')['revision'] == 1


def test_a_name_has_one_defining_cell_and_private_names_are_a_cells_own():
    assert definitions('import numpy as np\nfrom math import pi as PI\nx, (y, *z) = 1, (2, 3)\nfor i in range(3):\n    total = i\n'
                       'def f(a):\n    inner = a\n    return inner\nclass Model: limit = 3\nwith open("f") as handle: pass\n') == \
        {'np', 'PI', 'x', 'y', 'z', 'i', 'total', 'f', 'Model', 'handle'}
    # A comprehension's variable stays inside it; a walrus inside one does not.
    assert definitions('squares = [k * k for k in range(4)]') == {'squares'}
    assert definitions('seen = [(last := k) for k in range(4)]') == {'seen', 'last'}
    assert definitions('_scratch = 1\ntry:\n    value = 1\nexcept ValueError as error:\n    value = 0') == {'value'}
    with pytest.raises(ValueError, match='instead of \\*'):
        definitions('from math import *')
    with pytest.raises(ValueError, match='line 1'):
        definitions('x = (')
    cell = lambda name, source, kind='code': {'name': name, 'kind': kind, 'source': source}
    assert check([cell('a', 'rows = [k for k in range(2)]\n_tmp = 1'), cell('b', 'cols = [k for k in range(3)]\n_tmp = 2'),
                  cell('words', 'rows = not code', 'markdown')]) == {'rows': 'a', 'cols': 'b'}
    with pytest.raises(ValueError, match='`rows` is defined in both a and c'):
        check([cell('a', 'rows = 1'), cell('c', 'import rows')])


def test_an_edit_names_the_revision_it_read(notebook):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    formulation.edit(cid, [markdown('budget', 'First wording')])
    formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'source': 'Second wording'}])
    with pytest.raises(Refusal, match='revision 2, not 1'):
        formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'source': 'Written over a stale read'}])
    with pytest.raises(Refusal, match='revision 2, not 1'):
        formulation.edit(cid, [{'op': 'delete', 'cell': 'budget', 'expected_revision': 1}])
    with pytest.raises(Refusal, match='no cell missing'):
        formulation.edit(cid, [{'op': 'update', 'cell': 'missing', 'expected_revision': 1, 'status': 'open'}])
    with pytest.raises(Refusal, match='named budget'):
        formulation.edit(cid, [markdown('budget', 'A second cell of that name')])
    cell = formulation.view(cid)['cells'][0]
    assert cell['source'] == 'Second wording' and cell['revision'] == 2
    # Saving what is already there changes nothing and is no revision.
    assert formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 2, 'source': 'Second wording'}])['cells'] == []
    assert formulation.view(cid)['notebook']['revision'] == 2
    # A deleted cell leaves the notebook and frees its name, and its history stays.
    formulation.edit(cid, [{'op': 'delete', 'cell': cell['id'], 'expected_revision': 2}, markdown('budget', 'A new cell of that name')])
    assert [row['revision'] for row in formulation.history(cid, cell['id'])] == [1, 2, 3]
    assert formulation.history(cid, cell['id'])[-1]['deleted'] and len(formulation.view(cid)['cells']) == 1


def test_only_the_researcher_agrees_and_an_agents_change_proposes_the_cell_again(notebook):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    lead = {'author': 'lead', 'author_id': 'lead_1'}
    formulation.edit(cid, [markdown('budget', 'Spend at most $B$.')], **lead, reason='First draft')
    with pytest.raises(Refusal, match='Only the researcher'):
        formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'status': 'agreed'}], **lead)
    with pytest.raises(Refusal, match='Only the researcher'):
        formulation.edit(cid, [markdown('caps', 'Daily caps.', status='agreed')], **lead)
    formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'status': 'agreed'}])
    agreed = formulation.view(cid)['cells'][0]
    assert agreed['status'] == 'agreed' and agreed['agreed_revision'] == 2 and agreed['author'] == 'researcher'
    with pytest.raises(Refusal, match='Give the reason'):
        formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 2, 'source': 'Spend at most $B$ per month.'}], **lead)
    formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 2, 'source': 'Spend at most $B$ per month.'}],
                     **lead, reason='The period was missing')
    changed = formulation.view(cid)['cells'][0]
    assert changed['status'] == 'proposed' and changed['agreed_revision'] == 2 and changed['reason'] == 'The period was missing'
    # The researcher's own change of an agreed cell leaves it agreed.
    formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 3, 'status': 'agreed'}])
    formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 4, 'source': 'Spend at most $B$ tokens per month.'}])
    assert formulation.view(cid)['cells'][0]['status'] == 'agreed' and formulation.view(cid)['cells'][0]['agreed_revision'] == 5


def test_the_researchers_edit_is_a_command_that_wakes_nobody(team):
    workspace, campaign, lead, _ = team
    cid = campaign['id']
    guidance = workspace.memory.state(cid)['guidance_revision']
    runs = len(workspace.store.list('agent_run', cid))
    command = lambda identity, operations, **more: Command(id=identity, campaign_id=cid, expected_revision=1, operation='formulation.edit',
        payload={'operations': operations, **more})
    receipt = workspace.commands.execute(command('edit_1', [markdown('budget', 'Spend at most $B$.')], reason='Started'))
    assert receipt['outcome']['notebook_revision'] == 1 and receipt['actor'] == 'researcher'
    # The same command again is the same receipt, not a second cell.
    assert workspace.commands.execute(command('edit_1', [markdown('budget', 'Spend at most $B$.')], reason='Started')) == receipt
    workspace.pi._campaign_events(cid)
    assert workspace.memory.state(cid)['guidance_revision'] == guidance and len(workspace.store.list('agent_run', cid)) == runs
    # An agent does not reach the notebook through the command service.
    state = workspace.memory.state(cid)
    with pytest.raises(ValueError, match='researcher authorization'):
        workspace.commands.execute(Command(id='edit_2', campaign_id=cid, expected_revision=1, operation='formulation.edit',
            expected_guidance_revision=state['guidance_revision'], expected_authority_hash=workspace.commands.authority_hash(workspace.store.get(cid, 'campaign')),
            payload={'operations': [markdown('caps', 'Daily caps.')]}), actor='manager')
    assert 'formulation.edit' in workspace.commands.describe() and len(workspace.formulation.view(cid)['cells']) == 1


def test_the_lead_writes_the_notebook_and_other_agents_read_it(team):
    workspace, campaign, lead, run = team
    cid, tools = campaign['id'], PiTools(workspace.pi)
    assert {'notebook_read', 'notebook_edit'} <= tools.names(lead)
    assert 'notebook_read' in tools.names({'role': 'methodology_specialist'}) and 'notebook_edit' not in tools.names({'role': 'methodology_specialist'})
    assert not {'notebook_read', 'notebook_edit'} & tools.names({'role': 'implementation_builder', 'grant_id': 'protected'})
    assert invoke(team, 'notebook_read')['notebook'] is None
    written = invoke(team, 'notebook_edit', {'operations': [markdown('budget', 'Spend at most \\(B\\).'), code('budget_code', 'B = 4_000_000')],
                                             'reason': 'First draft'}, 'write')
    assert written['notebook_revision'] == 1 and 'does not render' in written['warnings'][0]
    # A repeated call is answered from its receipt; nothing is written twice.
    assert invoke(team, 'notebook_edit', {'operations': [markdown('budget', 'Spend at most \\(B\\).'), code('budget_code', 'B = 4_000_000')],
                                          'reason': 'First draft'}, 'write') == written
    assert workspace.formulation.view(cid)['notebook']['revision'] == 1
    # An agent cannot send agreement, and a stale revision changes nothing.
    refused = invoke(team, 'notebook_edit', {'operations': [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'status': 'agreed'}], 'reason': ''}, 'agree')
    assert refused['executed'] is False and "'agreed' is not one of" in refused['error']
    workspace.formulation.edit(cid, [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'source': 'Spend at most $B$ a month.', 'status': 'agreed'}])
    stale = invoke(team, 'notebook_edit', {'operations': [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'source': 'Overwritten'},
                                                           markdown('caps', 'Daily caps.')], 'reason': 'Reworded'}, 'stale')
    assert stale['executed'] is False and 'revision 2, not 1' in stale['error']
    assert [cell['name'] for cell in workspace.formulation.view(cid)['cells']] == ['budget', 'budget_code']
    # The lead sees what the researcher changed until it reads that cell in full.
    assert tools.inspect(lead)['formulation']['changed_since_your_last_read'] == [{'name': 'budget', 'by': 'researcher', 'status': 'agreed'}]
    outline = invoke(team, 'notebook_read', {}, 'outline')
    assert outline['changed_since_your_last_read'] == ['budget'] and 'source' not in outline['cells'][0]
    full = invoke(team, 'notebook_read', {'cells': ['budget']}, 'full')
    assert full['cells'][0]['source'] == 'Spend at most $B$ a month.' and full['cells'][0]['revision'] == 2
    assert invoke(team, 'notebook_read', {}, 'outline_again')['changed_since_your_last_read'] == []
    assert invoke(team, 'notebook_read', {'cells': ['nowhere']}, 'missing')['executed'] is False
    # The notebook's records are evidence the lead can cite when its turn ends.
    assert tools.record(lead, workspace.formulation.notebook_id(cid))['revision'] == 2


def test_the_page_reads_the_notebook_and_a_conflict_is_no_campaign_issue(tmp_path):
    app = create_app(tmp_path / 'api', start_workers=False)
    workspace = app.state.workspace
    cid = campaign_in(workspace)['id']
    with TestClient(app) as client:
        send = lambda identity, operations: client.post('/api/v1/commands', json={'id': identity, 'campaign_id': cid, 'expected_revision': 1,
            'operation': 'formulation.edit', 'payload': {'operations': operations}})
        assert client.get(f'/api/campaigns/{cid}/formulation').json()['notebook'] is None
        assert client.get(f'/api/state?campaign_id={cid}').json()['formulation'] is None
        assert send('ui_1', [markdown('budget', 'Spend at most $B$.')]).json()['outcome']['notebook_revision'] == 1
        assert send('ui_2', [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'status': 'agreed'}]).status_code == 200
        conflict = send('ui_3', [{'op': 'update', 'cell': 'budget', 'expected_revision': 1, 'source': 'Typed over a stale read'}])
        assert conflict.status_code == 409 and 'revision 2, not 1' in conflict.json()['detail']
        assert not workspace.store.list('manager_issue', cid)
        # A malformed operation is the form's ordinary answer, and writes nothing.
        assert send('ui_4', [{'op': 'move', 'cell': 'budget'}]).status_code == 409
        view = client.get(f'/api/campaigns/{cid}/formulation').json()
        assert view['notebook']['revision'] == 2 and view['cells'][0]['status'] == 'agreed'
        assert client.get(f'/api/state?campaign_id={cid}').json()['formulation']['counts']['agreed'] == 1
        history = client.get(f"/api/campaigns/{cid}/formulation/cells/{view['cells'][0]['id']}/history").json()['revisions']
        assert [row['status'] for row in history] == ['proposed', 'agreed']
        assert client.get(f'/api/campaigns/{cid}/formulation/cells/unknown/history').status_code == 404
        exported = client.get(f'/api/campaigns/{cid}/formulation/export')
        assert exported.headers['content-type'].startswith('text/markdown') and 'Spend at most $B$.' in exported.text
        assert client.get(f'/api/campaigns/{cid}/formulation/export?format=pdf').status_code == 400
        assert client.get('/api/campaigns/campaign_unknown/formulation').status_code == 404


def test_removing_a_campaign_keeps_its_formulation(notebook):
    workspace, campaign = notebook
    cid = campaign['id']
    workspace.formulation.edit(cid, [markdown('budget', 'Spend at most $B$.'), code('budget_code', 'B = 4_000_000')])
    removed = workspace.removal.remove(cid, 'summary')
    kept = (workspace.removal.directory / cid / 'formulation.md').read_text()
    assert 'Spend at most $B$.' in kept and '```python\nB = 4_000_000\n```' in kept and removed['removed_campaign_id'] == cid
    assert not workspace.store.list('formulation_cell', cid) and not workspace.store.list('formulation_notebook', cid)
