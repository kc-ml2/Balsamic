"""Running the formulation notebook's code: what a cell shows, what is kept of a run, and what the sandbox does not reach."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from optimization_framework.api.app import create_app
from optimization_framework.execution.service import Workspace
from optimization_framework.formulation import kernel, runner, service
from optimization_framework.formulation.rules import check
from optimization_framework.formulation.service import Refusal

from test_formulation import campaign_in, code, invoke, markdown, notebook, team   # noqa: F401  (fixtures)


def cells(*sources):
    return [{'id': f'cell_{index}', 'name': f'c{index}', 'source': source} for index, source in enumerate(sources)]


def run_kernel(directory, items, scratch=None, wait=True):
    (directory / 'input.json').write_text(json.dumps({'cells': items, 'scratch': scratch}))
    process = subprocess.Popen([sys.executable, '-u', kernel.__file__], cwd=directory, env={**os.environ, 'MPLBACKEND': 'Agg'},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if wait:
        assert process.wait(timeout=60) == 0, process.stderr.read().decode()
        return json.loads((directory / 'result.json').read_text())
    return process


def test_a_cell_shows_its_last_value_what_it_printed_and_what_it_drew(tmp_path):
    result = run_kernel(tmp_path, cells(
        "import os\nB = 4_000_000\nprint('budget', B)\nos.write(1, b'printed by a library\\n')\nB / 4",
        "nb.md('Total $B$')\nnb.table([[1, 2], [3, 4]], ['x', 'y'])\nnb.tex('x \\\\le B')\n_local = 1",
        "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nNone",
        "def share():\n    return B / 0\nshare()",
        "never = 1"), scratch='B')
    first, shown, drawn, broken, after = (result['cells'][f'cell_{index}'] for index in range(5))
    # What a library writes from C lands in the same place and order as what Python prints.
    assert first['status'] == 'ok' and first['stdout'] == 'budget 4000000\nprinted by a library\n'
    assert first['outputs'] == [{'mime': 'text/plain', 'text': '1000000.0'}]
    assert shown['outputs'] == [{'mime': 'text/markdown', 'text': 'Total $B$'},
        {'mime': 'text/markdown', 'text': '| x | y |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |'}, {'mime': 'text/latex', 'text': 'x \\le B'}]
    assert drawn['outputs'] == [{'mime': 'image/png', 'file': 'c2_1.png'}] and (tmp_path / 'c2_1.png').read_bytes().startswith(runner.PNG)
    # A failure names the cell's own lines, not the runner's; the cells after it are not run, nor is the scratch code.
    assert broken['status'] == 'error' and 'ZeroDivisionError' in broken['error'] and '<cell c3>", line 2, in share' in broken['error']
    assert 'kernel.py' not in broken['error']
    assert after == {'status': 'skipped', 'stdout': '', 'outputs': [], 'after': 'c3'} and result['scratch']['status'] == 'skipped'
    assert result['finished'] is True and result['running'] is None


def test_a_value_is_shown_in_its_richest_form_and_the_scratch_code_sees_the_cells(tmp_path):
    result = run_kernel(tmp_path, cells("import sympy\nx = sympy.Symbol('x')\nsympy.Integral(x ** 2, x)", "limit = 12\n'text'"), scratch='limit * 2')
    assert result['cells']['cell_0']['outputs'][0]['mime'] == 'text/latex' and '\\int' in result['cells']['cell_0']['outputs'][0]['text']
    assert result['cells']['cell_1']['outputs'] == [{'mime': 'text/plain', 'text': "'text'"}]
    assert result['scratch']['status'] == 'ok' and result['scratch']['outputs'] == [{'mime': 'text/plain', 'text': '24'}]
    broken = run_kernel(tmp_path, cells('x = (', 'import sys\nsys.exit(3)'))
    assert broken['cells']['cell_0']['status'] == 'error' and 'SyntaxError' in broken['cells']['cell_0']['error']
    exited = run_kernel(tmp_path, cells('import sys\nsys.exit(3)', 'after = 1'))
    assert exited['cells']['cell_0']['status'] == 'error' and exited['cells']['cell_1']['status'] == 'skipped'


def test_a_run_stopped_at_its_limit_keeps_the_cells_that_finished(tmp_path, monkeypatch):
    process = run_kernel(tmp_path, cells("print('done')\n1", 'while True:\n    pass', 'never = 1'), wait=False)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not ((tmp_path / 'result.json').exists() and json.loads((tmp_path / 'result.json').read_text())['running'] == 'cell_1'):
        time.sleep(.05)
    process.send_signal(signal.SIGKILL); process.wait()
    stopped = json.loads((tmp_path / 'result.json').read_text())
    assert stopped['cells']['cell_0']['stdout'] == 'done\n' and stopped['running'] == 'cell_1' and not stopped['finished']
    # The runner reads that partial result as: one cell done, one stopped by the limit, one not reached.
    monkeypatch.setattr(runner, 'sandbox_command', lambda directory, read_only=(): [])
    monkeypatch.setattr(runner, 'run_sandboxed', lambda command, seconds: ({'status': 'timeout', 'seconds': seconds}, {'stdout': '', 'stderr': ''}, seconds))
    monkeypatch.setattr(Path, 'write_text', lambda self, text: len(text) if self.name == 'input.json' else Path.write_bytes(self, text.encode()))
    result = runner.execute(tmp_path, cells('a', 'b', 'c'), seconds=1)
    assert result['status'] == 'timeout' and [result['cells'][f'cell_{index}']['status'] for index in range(3)] == ['ok', 'timeout', 'not_run']
    assert 'limit of a run' in result['cells']['cell_1']['error']


def test_nothing_a_cell_left_behind_is_trusted(tmp_path, monkeypatch):
    secret = tmp_path / 'secret.txt'; secret.write_text('host file')
    run = tmp_path / 'run'; run.mkdir()
    (run / 'real.png').write_bytes(runner.PNG + b'picture')
    (run / 'linked.png').symlink_to(secret)
    (run / 'text.png').write_text('not a picture')
    (run / 'big.png').write_bytes(runner.PNG + b'0' * (kernel.FIGURE_BYTES + 1))
    (run / 'result.json').write_text(json.dumps({'finished': True, 'running': None, 'extra': 'ignored', 'cells': {'cell_0': {
        'status': 'celebrating', 'stdout': 'x' * 50000, 'error': 7, 'elapsed_seconds': 'soon', 'injected': {'html': '<script>'},
        'outputs': [{'mime': 'image/png', 'file': name} for name in ('real.png', 'linked.png', 'text.png', 'big.png', '../secret.txt', 'missing.png')]
                   + [{'mime': 'text/html', 'text': '<script>alert(1)</script>'}, {'mime': 'text/plain', 'text': 'kept'}, 'nonsense']}}}))
    monkeypatch.setattr(runner, 'sandbox_command', lambda directory, read_only=(): [])
    monkeypatch.setattr(runner, 'run_sandboxed', lambda command, seconds: ({'exit_code': 0}, {'stdout': '', 'stderr': ''}, .5))
    record = runner.execute(run, cells('1'))['cells']['cell_0']
    assert record == {'status': 'error', 'stdout': 'x' * kernel.OUTPUT_CHARACTERS,
                      'outputs': [{'mime': 'image/png', 'file': 'real.png'}, {'mime': 'text/plain', 'text': 'kept'}]}
    assert runner.picture(run, 'real.png') == run / 'real.png' and runner.picture(run, 'linked.png') is None and runner.picture(run, '../secret.txt') is None
    # A result that is itself a link, or is no result at all, is a failed run.
    (run / 'result.json').unlink(); (run / 'result.json').symlink_to(secret)
    assert runner.execute(run, cells('1')) == {'status': 'failed', 'cells': {'cell_0': {'status': 'not_run', 'stdout': '', 'outputs': []}},
                                              'elapsed_seconds': .5, 'stderr': ''}


def fake_execution(monkeypatch, results=None):
    """Runs without a sandbox: each code cell 'prints' its own source."""
    calls = []
    def execute(directory, items, *, scratch=None, seconds=90., parameters=None, font_list=None):
        calls.append({'directory': Path(directory), 'cells': [item['name'] for item in items], 'scratch': scratch, 'seconds': seconds, 'parameters': parameters})
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / 'figure_1.png').write_bytes(runner.PNG + b'picture')
        result = {'status': 'completed', 'elapsed_seconds': 2.0, 'cells': {item['id']: {'status': 'ok', 'stdout': item['source'] + '\n',
                  'outputs': [{'mime': 'image/png', 'file': 'figure_1.png'}], 'elapsed_seconds': .1} for item in items}}
        if scratch is not None:
            result['scratch'] = {'status': 'ok', 'stdout': '', 'outputs': [{'mime': 'text/plain', 'text': 'scratch saw ' + ', '.join(calls[-1]['cells'])}]}
        return (results or (lambda value: value))(result)
    monkeypatch.setattr(service, 'execute', execute)
    return calls


def test_an_output_is_stale_once_its_cell_or_one_above_it_changed(notebook, monkeypatch):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    calls = fake_execution(monkeypatch)
    with pytest.raises(Refusal, match='no code cell'):
        formulation.run(cid)
    formulation.edit(cid, [code('params', 'B = 4'), markdown('words', 'Spend at most $B$.'), code('caps', 'caps = [B / 2] * 2'), code('report', 'sum(caps)')])
    first = formulation.run(cid)
    assert first['status'] == 'completed' and calls[0]['cells'] == ['params', 'caps', 'report'] and calls[0]['seconds'] == 90
    view = formulation.view(cid)
    shown = {cell['name']: cell.get('output') for cell in view['cells']}
    assert shown['words'] is None and shown['caps']['stdout'] == 'caps = [B / 2] * 2\n' and not any(shown[name]['stale'] for name in ('params', 'caps', 'report'))
    assert view['latest_run']['id'] == first['id'] and view['active_run'] is None and view['notebook']['run_seconds'] == {'agents': 0.0, 'researcher': 2.0}
    # Text does not run: changing it leaves every output current. Changing a code cell stales it and the cells below it.
    formulation.edit(cid, [{'op': 'update', 'cell': 'words', 'expected_revision': 1, 'source': 'Spend at most $B$ a month.'}])
    assert not any(cell['output']['stale'] for cell in formulation.view(cid)['cells'] if cell['kind'] == 'code')
    formulation.edit(cid, [{'op': 'update', 'cell': 'caps', 'expected_revision': 1, 'source': 'caps = [B / 4] * 4'}])
    assert {cell['name']: cell['output']['stale'] for cell in formulation.view(cid)['cells'] if cell['kind'] == 'code'} == {'params': False, 'caps': True, 'report': True}
    # A new run replaces the old one: its outputs and its files are all that is kept.
    second = formulation.run(cid)
    assert not any(cell['output']['stale'] for cell in formulation.view(cid)['cells'] if cell['kind'] == 'code')
    assert workspace.store.get(first['id'], 'formulation_run')['cells'] == {} and not calls[0]['directory'].exists() and calls[1]['directory'].exists()
    assert formulation.figure(cid, second['id'], 'figure_1.png') == calls[1]['directory'] / 'figure_1.png'
    for owner, run_id, name in ((cid, first['id'], 'figure_1.png'), (cid, second['id'], 'absent.png'), ('campaign_other', second['id'], 'figure_1.png')):
        with pytest.raises(KeyError):
            formulation.figure(owner, run_id, name)


def test_one_run_at_a_time_and_a_lost_run_releases_the_notebook(notebook, monkeypatch):
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    formulation.edit(cid, [code('params', 'B = 4')])
    release = []
    def held(directory, items, *, scratch=None, seconds=90., parameters=None, font_list=None):
        while not release:
            time.sleep(.01)
        return {'status': 'completed', 'elapsed_seconds': 1.0, 'cells': {items[0]['id']: {'status': 'ok', 'stdout': '', 'outputs': []}}}
    monkeypatch.setattr(service, 'execute', held)
    started = formulation.run(cid, background=True)
    assert started['status'] == 'running' and formulation.view(cid)['active_run']['id'] == started['id']
    with pytest.raises(Refusal, match='in progress'):
        formulation.run(cid)
    # The campaign cannot be removed under a run.
    assert any('formulation notebook' in blocker for blocker in workspace.removal.plan(cid)['blockers'])
    release.append(True)
    workspace.research_threads[started['id']].join(timeout=10)
    assert formulation.view(cid)['active_run'] is None and formulation.view(cid)['latest_run']['status'] == 'completed'
    # A run whose service stopped is released at the next start, and a new run can begin.
    release.clear()
    lost = formulation.run(cid, background=True)
    formulation.recover()
    assert workspace.store.get(lost['id'], 'formulation_run')['status'] == 'interrupted' and formulation.view(cid)['active_run'] is None
    release.append(True)
    workspace.research_threads[lost['id']].join(timeout=10)
    # The lost run's late result is not taken for the notebook's output.
    assert workspace.store.get(lost['id'], 'formulation_run')['status'] == 'interrupted' and formulation.view(cid)['latest_run']['id'] == started['id']
    assert formulation.run(cid)['status'] == 'completed'


def test_the_lead_runs_the_notebook_and_any_agent_tries_code_against_it(team, monkeypatch):
    from optimization_framework.agents.tools import PiTools
    workspace, campaign, lead, _ = team
    cid, tools = campaign['id'], PiTools(workspace.pi)
    calls = fake_execution(monkeypatch)
    assert {'notebook_run', 'notebook_scratch'} <= tools.names(lead)
    assert 'notebook_scratch' in tools.names({'role': 'results_analyst'}) and 'notebook_run' not in tools.names({'role': 'results_analyst'})
    assert not {'notebook_run', 'notebook_scratch'} & tools.names({'role': 'implementation_builder', 'grant_id': 'protected'})
    # Before the notebook has cells there is nothing to run, and the scratchpad runs the given code alone.
    assert 'no code cell' in invoke(team, 'notebook_run', {}, 'empty')['error']
    assert invoke(team, 'notebook_scratch', {'code': '1 + 1'}, 'first')['outputs'] == [{'mime': 'text/plain', 'text': 'scratch saw '}]
    invoke(team, 'notebook_edit', {'operations': [code('params', 'B = 4'), code('caps', 'caps = [B / 2] * 2')], 'reason': 'Toy instance'}, 'write')
    tried = invoke(team, 'notebook_scratch', {'code': 'sum(caps)', 'seconds': 20}, 'try')
    assert tried['status'] == 'ok' and tried['outputs'] == [{'mime': 'text/plain', 'text': 'scratch saw params, caps'}] and calls[-1]['seconds'] == 20
    assert tried['kept'] == 'Nothing was saved to the notebook.' and not calls[-1]['directory'].exists() and workspace.formulation.view(cid)['latest_run'] is None
    ran = invoke(team, 'notebook_run', {}, 'run')
    assert ran['status'] == 'completed' and [(cell['name'], cell['status'], cell['stdout']) for cell in ran['cells']] == [('params', 'ok', 'B = 4\n'), ('caps', 'ok', 'caps = [B / 2] * 2\n')]
    # A picture is named to the agent, not sent to it.
    assert ran['cells'][0]['outputs'] == [{'mime': 'image/png', 'note': 'A figure; the researcher sees it on the Formulation page.'}]
    assert ran['agent_run_seconds_left'] == 3600 - 6 and workspace.formulation.view(cid)['latest_run']['requested_by'] == 'lead'
    # Reading a cell gives its latest output with its source.
    assert invoke(team, 'notebook_read', {'cells': ['caps']}, 'read')['cells'][0]['output']['stdout'] == 'caps = [B / 2] * 2\n'
    # The agents' share of run time is limited; the researcher's own runs are not.
    head = workspace.store.get(workspace.formulation.notebook_id(cid), 'formulation_notebook')
    workspace.store.put('formulation_notebook', {**head, 'run_seconds': {'agents': 3600.0, 'researcher': 0.0}})
    assert 'Run all' in invoke(team, 'notebook_run', {}, 'spent')['error'] and 'Run all' in invoke(team, 'notebook_scratch', {'code': '1'}, 'spent_scratch')['error']
    assert workspace.formulation.run(cid)['status'] == 'completed'
    # nb belongs to the notebook, so no cell defines it.
    with pytest.raises(ValueError, match="nb is the notebook's own name"):
        check([{'name': 'shadow', 'kind': 'code', 'source': 'nb = 1'}])


def test_the_page_starts_a_run_and_fetches_its_pictures(tmp_path, monkeypatch):
    fake_execution(monkeypatch)
    app = create_app(tmp_path / 'api', start_workers=False)
    workspace = app.state.workspace
    cid = campaign_in(workspace)['id']
    with TestClient(app) as client:
        assert client.post(f'/api/campaigns/{cid}/formulation/runs').status_code == 409   # nothing to run, and no campaign issue
        assert not workspace.store.list('manager_issue', cid)
        workspace.formulation.edit(cid, [code('params', 'B = 4')])
        started = client.post(f'/api/campaigns/{cid}/formulation/runs').json()
        assert started['status'] == 'running' and started['seconds'] == 90
        workspace.research_threads[started['id']].join(timeout=10)
        view = client.get(f'/api/campaigns/{cid}/formulation').json()
        assert view['active_run'] is None and view['latest_run']['status'] == 'completed' and view['cells'][0]['output']['stale'] is False
        assert client.get(f'/api/state?campaign_id={cid}').json()['formulation']['latest_run_id'] == started['id']
        picture = client.get(f"/api/campaigns/{cid}/formulation/runs/{started['id']}/files/figure_1.png")
        assert picture.status_code == 200 and picture.headers['content-type'] == 'image/png' and picture.headers['x-content-type-options'] == 'nosniff'
        assert client.get(f"/api/campaigns/{cid}/formulation/runs/{started['id']}/files/input.json").status_code == 404
        assert client.get(f"/api/campaigns/{cid}/formulation/runs/unknown/files/figure_1.png").status_code == 404


def test_cells_run_in_the_sandbox_with_the_solver_and_without_the_host(notebook):
    if not shutil.which('bwrap') or subprocess.run(['bwrap', '--unshare-all', '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/lib64', '/lib64', '--ro-bind', '/bin', '/bin', '/usr/bin/true'], capture_output=True).returncode:
        pytest.skip('This host cannot establish the sandbox namespaces')
    pytest.importorskip('scipy')
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    formulation.edit(cid, [
        code('data', 'import numpy as np\nfrom scipy.optimize import milp, LinearConstraint, Bounds\nvalue = np.array([5.0, 4.0, 3.0])\ncost = np.array([2.0, 3.0, 1.0])\nB = 5.0'),
        markdown('budget', 'Spend at most $B$: $$\\sum_i c_i x_i \\le B$$'),
        code('solve', 'solution = milp(-value, constraints=LinearConstraint(cost[None, :], ub=B), integrality=np.ones(3), bounds=Bounds(0, 1))\n'
                      'chosen = [int(round(v)) for v in solution.x]\nnb.table([[i, c] for i, c in enumerate(chosen)], ["item", "chosen"])\nchosen'),
        code('host', f'import os, socket\nprint("workspace", os.path.exists({str(workspace.directory)!r}), "| home", '
                     f'[os.path.exists(os.path.join({str(Path.home())!r}, name)) for name in (".config", ".ssh", ".pi")])\n'
                     'try:\n    socket.create_connection(("127.0.0.1", 8765), timeout=2)\n    print("network reachable")\nexcept OSError as error:\n    print("no network:", type(error).__name__)'),
    ])
    run = formulation.run(cid)
    outcome = {cell['name']: cell.get('output') for cell in formulation.view(cid)['cells']}
    assert run['status'] == 'completed', run
    assert outcome['solve']['outputs'][-1] == {'mime': 'text/plain', 'text': '[1, 1, 0]'} and outcome['solve']['outputs'][0]['text'].startswith('| item | chosen |')
    # Neither the workspace, nor the user's configuration and keys, nor any network exists inside the sandbox.
    assert 'workspace False | home [False, False, False]' in outcome['host']['stdout'] and 'no network:' in outcome['host']['stdout']
    assert formulation.view(cid)['notebook']['run_seconds']['researcher'] > 0
    # The scratchpad sees what the cells defined and leaves the notebook as it was.
    tried = formulation.scratch(cid, 'float(value @ np.array(chosen))')
    assert tried['outputs'] == [{'mime': 'text/plain', 'text': '9.0'}] and formulation.view(cid)['latest_run']['id'] == run['id']
    assert not list((workspace.directory / 'campaigns' / cid / 'formulation' / 'scratch').iterdir())
    # A parameter reaches the sandbox as P, and a run that draws reads the font list the server built, read-only.
    formulation.edit(cid, [{'op': 'parameter.declare', 'name': 'B', 'label': 'Budget', 'kind': 'number', 'value': 5.0, 'minimum': 0, 'maximum': 10},
                           {'op': 'update', 'cell': 'data', 'expected_revision': 1, 'source': 'import numpy as np\nfrom scipy.optimize import milp, '
                            'LinearConstraint, Bounds\nvalue = np.array([5.0, 4.0, 3.0])\ncost = np.array([2.0, 3.0, 1.0])\nB = P.B'},
                           code('figure', 'import os as _os, matplotlib.pyplot as plt\nplt.bar(range(3), chosen)\n'
                                          'open(_os.path.expanduser("~/.cache/matplotlib/probe"), "w").close()\n'
                                          'try:\n    open("/opt/matplotlib-fonts/probe", "w")\nexcept OSError as error:\n    print("font list read-only:", type(error).__name__)')])
    formulation.set_values(cid, {'B': 3.0})
    drawn = formulation.run(cid)
    outcome = {cell['name']: cell.get('output') for cell in formulation.view(cid)['cells']}
    assert drawn['status'] == 'completed' and drawn['parameters'] == {'B': 3.0}, drawn
    assert outcome['solve']['outputs'][-1] == {'mime': 'text/plain', 'text': '[1, 0, 1]'}
    assert outcome['figure']['outputs'][0]['mime'] == 'image/png' and 'Fontconfig' not in outcome['figure']['stdout']
    assert 'font list read-only: OSError' in outcome['figure']['stdout']
    # A cell that never ends is stopped at the limit; the cells before it keep their output.
    formulation.edit(cid, [code('endless', 'while True:\n    pass', after='data')])
    monkeypatched = service.RUN_SECONDS
    try:
        service.RUN_SECONDS = 2.
        stopped = formulation.run(cid)
    finally:
        service.RUN_SECONDS = monkeypatched
    assert stopped['status'] == 'timeout'
    statuses = {cell['name']: cell['output']['status'] for cell in formulation.view(cid)['cells'] if cell['kind'] == 'code'}
    assert statuses == {'data': 'ok', 'endless': 'timeout', 'solve': 'not_run', 'host': 'not_run', 'figure': 'not_run'}


def test_a_pyomo_component_is_shown_as_the_equation_generated_from_it(tmp_path):
    pytest.importorskip('pyomo'); pytest.importorskip('highspy')
    result = run_kernel(tmp_path, cells(
        "import pyomo.environ as pyo\nm = pyo.ConcreteModel()\nm.I = pyo.Set(initialize=['ana', 'bo'])\nm.T = pyo.RangeSet(1, 2)\n"
        "m.B = pyo.Param(initialize=6.0, mutable=True)\nm.v = pyo.Param(m.I, initialize={'ana': 3, 'bo': 2}, mutable=True)\n"
        "m.x = pyo.Var(m.I, m.T, domain=pyo.NonNegativeIntegers, bounds=(0, 2))",
        "m.budget = pyo.Constraint(rule=lambda m: sum(m.x[i, t] for i in m.I for t in m.T) <= m.B)\nnb.show(m.budget)",
        "m.daily = pyo.Constraint(m.T, rule=lambda m, t: sum(m.x[i, t] for i in m.I) <= m.B / 2)\nm.daily",
        "m.value = pyo.Objective(rule=lambda m: sum(m.v[i] * m.x[i, t] for i in m.I for t in m.T), sense=pyo.maximize)\nnb.show(m.value, symbols={'x': 'q'})",
        "solved = pyo.SolverFactory('appsi_highs').solve(m)\nstr(solved.solver.termination_condition), pyo.value(m.value)",
        "nb.show(m)\nnb.show(m.x['ana', 1])"))
    shown = [[' '.join(output['text'].split()) for output in result['cells'][f'cell_{index}']['outputs']] for index in range(6)]
    # Each set keeps its own letter as index, and a parameter its symbol: the equation reads as it would be written by hand.
    assert shown[1] == ['\\sum_{ i \\in I } \\sum_{ t \\in T } x_{i,t} \\leq B']
    assert shown[2] == ['\\sum_{ i \\in I } x_{i,t} \\leq \\frac{B}{2} \\qquad \\forall t \\in T']
    assert shown[3] == ['\\max \\sum_{ i \\in I } \\sum_{ t \\in T } v_{i} q_{i,t}']
    assert shown[4] == ["('optimal', 16.0)"]
    # A whole model is one aligned block, without the commands the page cannot typeset.
    assert shown[5][0].startswith('\\begin{aligned}') and '\\label' not in shown[5][0] and '\\mathds' not in shown[5][0] and '\\mathbb{Z}' in shown[5][0]
    # What the printer cannot turn into an equation is shown as Pyomo lists it.
    assert result['cells']['cell_5']['outputs'][1]['mime'] == 'text/plain' and 'NonNegativeIntegers' in shown[5][1]


def test_the_notebook_exports_as_a_marimo_file_that_runs(notebook, tmp_path, monkeypatch):
    import types
    workspace, campaign = notebook
    cid, formulation = campaign['id'], workspace.formulation
    formulation.edit(cid, [
        markdown('budget', '## Budget\n\nSpend at most $B$: $$\\sum_i x_i \\le B$$'),
        code('data', 'import math\nB = 4\nshares = [B / 2] * 2'),
        markdown('class', 'A cell named like a keyword, with """ inside.'),
        code('total', 'nb.table([[1, 2]], ["a", "b"])\nnb.md("**done**")\n_scratch = 1\ntotal = math.fsum(shares)\ntotal'),
        code('shares_check', 'ok = total == B'),
    ])
    exported = formulation.export_marimo(cid)
    compile(exported, 'formulation.py', 'exec')
    assert '__generated_with = "0.25.1"' in exported and exported.count('@app.cell') == 7   # five cells, the marimo import and the nb helper
    assert 'def cell_class(mo):' in exported and 'def total(' not in exported and 'def cell_total(math, nb, shares):' in exported
    assert "return (B, math, shares,)" in exported and "return (total,)" in exported
    # Run it as marimo does: each cell is called with the names it asks for and returns the names it defines.
    shown, functions = [], []
    class App:
        def __init__(self, **options): pass
        def cell(self, function=None, **options):
            if function is None:
                return lambda inner: self.cell(inner)
            functions.append(function); return function
        def run(self): pass
    stub = types.ModuleType('marimo')
    stub.App, stub.md, stub.output = App, (lambda text: ('md', text)), types.SimpleNamespace(append=shown.append)
    monkeypatch.setitem(sys.modules, 'marimo', stub)
    exec(compile(exported, 'formulation.py', 'exec'), {'__name__': 'exported'})
    import ast, inspect
    names = [[element.id for element in ast.walk(node.value) if isinstance(element, ast.Name)] if node.value else []
             for node in ast.walk(ast.parse(exported)) if isinstance(node, ast.Return)]
    values = {}
    for function, returned in zip(functions, names):
        result = function(*[values[name] for name in inspect.signature(function).parameters])
        values.update(dict(zip(returned, result or ())))
    assert values['total'] == 4.0 and values['ok'] is True and '_scratch' not in values
    assert ('md', '| a | b |\n| --- | --- |\n| 1 | 2 |') in shown and ('md', '**done**') in shown
    # Text that holds three quotes cannot sit in a triple-quoted string, so it is written as an ordinary one.
    assert """mo.md('A cell named like a keyword, with \"\"\" inside.')""" in exported and 'mo.md(r\"\"\"\n    ## Budget' in exported
    # The page offers both exports.
    app = create_app(tmp_path / 'api', start_workers=False)
    other = campaign_in(app.state.workspace)['id']
    app.state.workspace.formulation.edit(other, [code('data', 'B = 4')])
    with TestClient(app) as client:
        python = client.get(f'/api/campaigns/{other}/formulation/export?format=py')
        assert python.headers['content-type'].startswith('text/x-python') and 'def data():' in python.text and python.headers['content-disposition'].endswith('.py"')
        assert client.get(f'/api/campaigns/{other}/formulation/export?format=docx').status_code == 400
