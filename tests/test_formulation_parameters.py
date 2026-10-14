"""Parameters of the formulation notebook: the numbers the researcher moves with controls and code reads as P."""
import json
import os
import subprocess
import sys
import threading
import time
import types

from fastapi.testclient import TestClient
import pytest

from optimization_framework.api.app import create_app
from optimization_framework.contracts.evaluators import EvaluatorManifest
from optimization_framework.formulation import kernel, service
from optimization_framework.formulation.parameters import schema, values
from optimization_framework.formulation.service import Refusal
from optimization_framework.implementations.models import check_parameters

from test_formulation import campaign_in, code, invoke, markdown, notebook, team   # noqa: F401  (fixtures)
from test_formulation_runs import fake_execution


def declare(name, value, kind='number', **more):
    return {'op': 'parameter.declare', 'name': name, 'label': more.pop('label', name), 'kind': kind, 'value': value, **more}


def budget(**more):
    return declare('B', 4_000_000, label='Monthly token allocation', minimum=0, maximum=10_000_000, step=100_000, unit='tokens', **more)


def test_parameters_and_the_code_that_reads_them_change_together_or_not_at_all(notebook):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    written = formulation.edit(cid, [budget(prominent=True), declare('seats', 12, 'integer', minimum=1),
                                     code('data', 'cap = P.B / P["seats"] / 30')])
    assert [item['name'] for item in written['parameters']] == ['B', 'seats']
    view = formulation.view(cid)
    held = {item['name']: item for item in view['parameters']}
    assert held['B']['used_by'] == ['data'] and held['B']['basis'] == 'given' and held['B']['revision'] == 1 and held['B']['prominent']
    assert view['limits']['parameters'] == 64
    events = len(workspace.store.events(cid))
    refused = [
        ([declare('rate', 2.0, minimum=0, maximum=1), markdown('note', 'A rate.')], 'rate is from 0 to 1; 2 is outside it'),
        ([declare('days', 30.5, 'integer')], 'whole number'),
        ([declare('flag', 1, 'boolean')], 'true or false'),
        ([declare('mode', 'fair', 'choice', choices=['fair'])], 'at least two different choices'),
        ([declare('B', 1)], 'Two parameters would be named B'),
        ([code('more', 'x = P.missing')], 'reads P.missing, which is not a parameter'),
        ([{'op': 'parameter.delete', 'parameter': 'B', 'expected_revision': 1}], 'reads P.B, which is not a parameter'),
        ([code('mine', 'P = 3')], 'P holds the notebook'),
        ([{'op': 'parameter.set', 'parameter': 'B', 'expected_revision': 0, 'value': 1}], 'revision 1, not 0'),
    ]
    for operations, message in refused:
        with pytest.raises(Refusal, match=message):
            formulation.edit(cid, operations)
    assert len(workspace.store.events(cid)) == events and formulation.view(cid)['notebook']['revision'] == 1
    # A rename and the code that reads the new name are one batch.
    formulation.edit(cid, [{'op': 'parameter.update', 'parameter': 'B', 'expected_revision': 1, 'name': 'allocation'},
                           {'op': 'update', 'cell': 'data', 'expected_revision': 1, 'source': 'cap = P.allocation / P["seats"] / 30'}])
    assert [item['name'] for item in formulation.view(cid)['parameters']] == ['allocation', 'seats']
    # A bound is removed by sending it as null.
    formulation.edit(cid, [{'op': 'parameter.update', 'parameter': 'allocation', 'expected_revision': 2, 'maximum': None, 'unit': 'tokens a month'}])
    held = formulation.view(cid)['parameters'][0]
    assert held['maximum'] is None and held['minimum'] == 0 and held['unit'] == 'tokens a month'
    with pytest.raises(Refusal, match='no parameter B'):
        formulation.set_values(cid, {'B': 1})


def test_a_notebook_that_defines_P_itself_stays_editable_until_it_declares_a_parameter(notebook):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    formulation.edit(cid, [code('old', 'P = {"B": 4}')])
    formulation.edit(cid, [code('more', 'total = P["B"] * 2')])
    head = workspace.store.get(formulation.notebook_id(cid), 'formulation_notebook')
    assert formulation._arguments(head, formulation._cells(cid, head)) is None
    with pytest.raises(Refusal, match='P holds the notebook'):
        formulation.edit(cid, [budget()])


def test_the_lead_changes_a_value_the_researcher_set_only_with_a_reason(notebook):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    lead = {'author': 'lead', 'author_id': 'lead_1'}
    with pytest.raises(Refusal, match='quote where the researcher gave it'):
        formulation.edit(cid, [budget(basis='given')], **lead)
    formulation.edit(cid, [budget(basis='given', origin='"one LLM token budget of 4M a month"'), declare('absence', .1, minimum=0, maximum=1)],
                     **lead, reason='From the description')
    held = {item['name']: item for item in formulation.view(cid)['parameters']}
    assert held['absence']['basis'] == 'assumed' and held['absence']['value_author'] == 'lead'
    # The researcher moves a control: the value is theirs, and that move is no revision worth keeping.
    formulation.set_values(cid, {'B': 5_000_000})
    snapshots = len(workspace.store.list('formulation_parameter_revision', cid))
    with pytest.raises(Refusal, match='revision 2, not 1'):
        formulation.edit(cid, [{'op': 'parameter.set', 'parameter': 'B', 'expected_revision': 1, 'value': 3_000_000}], **lead, reason='Lower')
    with pytest.raises(Refusal, match='Give the reason'):
        formulation.edit(cid, [{'op': 'parameter.set', 'parameter': 'B', 'expected_revision': 2, 'value': 3_000_000}], **lead)
    formulation.edit(cid, [{'op': 'parameter.set', 'parameter': 'B', 'expected_revision': 2, 'value': 3_000_000}], **lead,
                     reason='The vendor quote is 3M')
    changed = {item['name']: item for item in formulation.view(cid)['parameters']}['B']
    assert (changed['value'], changed['value_author'], changed['basis'], changed['confirmed_value']) == (3_000_000, 'lead', 'assumed', 5_000_000)
    assert len(workspace.store.list('formulation_parameter_revision', cid)) == snapshots + 1
    # Setting the shown value confirms it.
    formulation.set_values(cid, {'B': 3_000_000})
    confirmed = {item['name']: item for item in formulation.view(cid)['parameters']}['B']
    assert confirmed['value_author'] == 'researcher' and confirmed['confirmed_value'] == 3_000_000


def test_moving_a_control_dates_the_outputs_from_its_first_reader_and_runs_with_the_new_value(notebook, monkeypatch):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    calls = fake_execution(monkeypatch)
    formulation.edit(cid, [budget(), code('fixed', 'days = 30'), code('cap', 'cap = P.B / days'), code('report', 'cap * 2')])
    formulation.run(cid)
    assert calls[-1]['parameters'] == {'B': 4_000_000}
    formulation.set_values(cid, {'B': 6_000_000})
    stale = {cell['name']: cell['output']['stale'] for cell in formulation.view(cid)['cells']}
    assert stale == {'fixed': False, 'cap': True, 'report': True}
    formulation.run(cid)
    assert calls[-1]['parameters'] == {'B': 6_000_000} and not any(cell['output']['stale'] for cell in formulation.view(cid)['cells'])
    # The scratchpad tries another value, and keeps neither it nor its outputs.
    formulation.scratch(cid, 'cap', parameters={'B': 1_000_000})
    assert calls[-1]['parameters'] == {'B': 1_000_000} and values(formulation.view(cid)['parameters']) == {'B': 6_000_000}
    with pytest.raises(Refusal, match='no parameter seats'):
        formulation.scratch(cid, 'cap', parameters={'seats': 3})
    with pytest.raises(Refusal, match='outside it'):
        formulation.scratch(cid, 'cap', parameters={'B': -1})


def test_the_researchers_controls_save_and_run_without_a_command_record(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'api', start_workers=False)
    workspace = app.state.workspace
    cid = campaign_in(workspace)['id']
    formulation = workspace.formulation
    formulation.edit(cid, [budget(), code('cap', 'cap = P.B / 30')])
    calls = fake_execution(monkeypatch)
    with TestClient(app) as client:
        answer = client.put(f'/api/campaigns/{cid}/formulation/values', json={'values': {'B': 2_000_000}})
        assert answer.status_code == 200 and answer.json()['run']['status'] == 'completed' and answer.json()['queued'] is False
        assert calls[-1]['parameters'] == {'B': 2_000_000} and answer.json()['notebook']['parameters'][0]['value'] == 2_000_000
        assert not workspace.store.list('work_command', cid) and not workspace.store.list('manager_issue', cid)
        refused = client.put(f'/api/campaigns/{cid}/formulation/values', json={'values': {'B': -5}})
        assert refused.status_code == 409 and 'outside it' in refused.json()['detail'] and not workspace.store.list('manager_issue', cid)
        assert client.put(f'/api/campaigns/{cid}/formulation/values', json={'values': {'B': [1]}}).status_code == 422
        # A run asked for with a wait answers finished; without one it answers running.
        assert client.post(f'/api/campaigns/{cid}/formulation/runs?wait=5').json()['status'] == 'completed'


def test_a_move_during_a_run_runs_once_more_with_the_latest_value(notebook, monkeypatch):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    formulation.edit(cid, [budget(), code('cap', 'cap = P.B / 30')])
    release, seen = threading.Event(), []
    def held(directory, items, *, scratch=None, seconds=90., parameters=None, font_list=None):
        seen.append(parameters)
        release.wait(10)
        return {'status': 'completed', 'elapsed_seconds': .1, 'cells': {items[0]['id']: {'status': 'ok', 'stdout': '', 'outputs': []}}}
    monkeypatch.setattr(service, 'execute', held)
    first = formulation.run(cid, background=True)
    for value in (1_000_000, 2_000_000, 3_000_000):
        formulation.set_values(cid, {'B': value})
        assert formulation.run(cid, background=True, queue=True) is None
    release.set()
    workspace.research_threads[first['id']].join(timeout=10)
    deadline = time.monotonic() + 10
    while formulation.view(cid)['active_run'] or len(seen) < 2:
        assert time.monotonic() < deadline
        time.sleep(.02)
    # Three moves during the run are one more run, with the last value.
    assert seen == [{'B': 4_000_000}, {'B': 3_000_000}]
    assert not formulation.view(cid)['cells'][0]['output']['stale']


def test_agents_read_parameters_and_notice_the_researchers_moves(team, monkeypatch):
    from optimization_framework.agents.tools import PiTools
    workspace, campaign, lead, _ = team
    cid, tools = campaign['id'], PiTools(workspace.pi)
    calls = fake_execution(monkeypatch)
    written = invoke(team, 'notebook_edit', {'operations': [budget(basis='assumed', description='Assumed until the invoice is read.'),
                                                            code('cap', 'cap = P.B / 30')], 'reason': 'First parameters'}, 'write')
    assert written['parameters'][0]['name'] == 'B' and written['parameters'][0]['value_author'] == 'lead'
    outline = invoke(team, 'notebook_read', {}, 'outline')
    assert outline['parameters'][0]['used_by'] == ['cap'] and 'parameters_changed_since_your_last_read' not in outline
    workspace.formulation.set_values(cid, {'B': 4_500_000})
    assert tools.inspect(lead)['formulation']['parameters_changed_since_your_last_read'] == [{'name': 'B', 'value': 4_500_000, 'set_by': 'researcher'}]
    assert invoke(team, 'notebook_read', {}, 'again')['parameters_changed_since_your_last_read'] == ['B']
    assert 'parameters_changed_since_your_last_read' not in tools.inspect(lead)['formulation']
    tried = invoke(team, 'notebook_scratch', {'code': 'cap', 'parameters': {'B': 900_000}}, 'try')
    assert tried['status'] == 'ok' and calls[-1]['parameters'] == {'B': 900_000}
    refused = invoke(team, 'notebook_edit', {'operations': [{'op': 'parameter.set', 'parameter': 'B', 'expected_revision': 2, 'value': 1}],
                                             'reason': ''}, 'overwrite')
    assert refused['executed'] is False and 'Give the reason' in refused['error']


def test_the_kernel_gives_parameters_as_a_read_only_P(tmp_path):
    items = [{'id': 'c0', 'name': 'c0', 'source': 'P.B / 2'}, {'id': 'c1', 'name': 'c1', 'source': 'P["mode"], len(P), "B" in P'},
             {'id': 'c2', 'name': 'c2', 'source': 'P.B = 1'}]
    (tmp_path / 'input.json').write_text(json.dumps({'cells': items, 'scratch': None, 'parameters': {'B': 4, 'mode': 'fair'}}))
    subprocess.run([sys.executable, '-u', kernel.__file__], cwd=tmp_path, env={**os.environ, 'MPLBACKEND': 'Agg'}, check=True, timeout=60)
    result = json.loads((tmp_path / 'result.json').read_text())['cells']
    assert result['c0']['outputs'] == [{'mime': 'text/plain', 'text': '2.0'}]
    assert result['c1']['outputs'] == [{'mime': 'text/plain', 'text': "('fair', 2, True)"}]
    assert result['c2']['status'] == 'error' and 'set with the controls' in result['c2']['error']


def test_parameters_become_the_configuration_of_a_declared_problem(notebook):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    formulation.edit(cid, [budget(description='Tokens per month.'), declare('carry', False, 'boolean', label='Carry over unused tokens'),
                           declare('policy', 'fair', 'choice', choices=['fair', 'greedy'], label='Policy'), declare('note', 'draft', 'text')])
    held = formulation.view(cid)['parameters']
    configuration_schema = schema(held)
    check_parameters(values(held), configuration_schema)
    assert configuration_schema['properties']['B'] == {'type': 'number', 'title': 'Monthly token allocation', 'default': 4_000_000,
                                                       'description': 'Tokens per month.', 'minimum': 0, 'maximum': 10_000_000}
    manifest = EvaluatorManifest(id='token_sharing', name='Token sharing', configuration_schema=configuration_schema, configuration=values(held),
        candidate_schema={'representation': 'continuous', 'dimensions': 2, 'bounds': [[0, 1], [0, 1]]},
        primary_objective={'name': 'value', 'direction': 'maximize'})
    assert manifest.configuration == {'B': 4_000_000, 'carry': False, 'policy': 'fair', 'note': 'draft'}


def test_both_exports_carry_the_parameters(notebook, monkeypatch):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    formulation.edit(cid, [budget(), declare('seats', 12, 'integer', minimum=1), declare('carry', False, 'boolean', label='Carry over'),
                           declare('policy', 'fair', 'choice', choices=['fair', 'greedy']), markdown('parameters', 'What the numbers mean.'),
                           code('cap', 'cap = P.B / P["seats"]\npolicy = P.policy')])
    text = formulation.export_markdown(cid)
    assert '| `B` | Monthly token allocation | 4000000 tokens | 0 to 1e+07 | given |' in text and '| `policy` | policy | fair | fair or greedy |' in text
    exported = formulation.export_marimo(cid)
    compile(exported, 'formulation.py', 'exec')
    assert 'parameters = mo.ui.dictionary({' in exported and "mo.ui.slider(start=0.0, stop=10000000.0, step=100000.0" in exported
    assert "mo.ui.number(start=1.0, step=1, value=12" in exported and "mo.ui.switch(value=False" in exported
    assert "mo.ui.dropdown(options=['fair', 'greedy'], value='fair'" in exported and 'def cell_parameters(mo):' in exported
    assert 'def cell_cap(P):' in exported
    # Run it as marimo would, with controls that hold their values.
    functions, shown = [], []
    class App:
        def __init__(self, **options): pass
        def cell(self, function=None, **options):
            if function is None:
                return lambda inner: self.cell(inner)
            functions.append(function); return function
    control = lambda **options: types.SimpleNamespace(value=options.get('value'))
    ui = types.SimpleNamespace(slider=control, number=control, switch=control, dropdown=control, text=control,
                               dictionary=lambda elements, **options: types.SimpleNamespace(value={key: item.value for key, item in elements.items()}))
    stub = types.ModuleType('marimo')
    stub.App, stub.md, stub.ui, stub.output = App, (lambda text: ('md', text)), ui, types.SimpleNamespace(append=shown.append)
    monkeypatch.setitem(sys.modules, 'marimo', stub)
    exec(compile(exported, 'formulation.py', 'exec'), {'__name__': 'exported'})
    import ast, inspect
    returns = [[element.id for element in node.body[-1].value.elts] if isinstance(node.body[-1], ast.Return) and node.body[-1].value else []
               for node in ast.parse(exported).body if isinstance(node, ast.FunctionDef)]
    found = {}
    for function, returned in zip(functions, returns):
        result = function(*[found[name] for name in inspect.signature(function).parameters])
        found.update(dict(zip(returned, result or ())))
    assert found['P'].B == 4_000_000 and found['P']['seats'] == 12 and found['cap'] == 4_000_000 / 12 and found['policy'] == 'fair'
