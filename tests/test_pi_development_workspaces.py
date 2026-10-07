"""Durable native Pi coding workspace handoff and validation authority."""
import json
from pathlib import Path

import pytest

from optimization_framework.agents.development import DevelopmentWorkspaces
from optimization_framework.contracts.commands import Command
from optimization_framework.contracts.requests import CampaignInput, TaskInput
from optimization_framework.execution.service import Workspace


class Driver:
    def __init__(self, root):
        self.directory = root
        self.cpu = 0
        self.prepared = 0
        self.started = 0
        self.paused = 0

    def root(self, record):
        return self.directory / record['id']

    def name(self, record):
        return 'grating-' + record['id']

    def capability(self):
        return {'available': True}

    def prepare(self, record, brief, evidence):
        self.prepared += 1
        root = self.root(record)
        for name in ('incoming/commands', 'incoming/results', 'outgoing/events'):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / 'brief.md').write_text(brief)

    def start(self, record):
        self.started += 1
        return self.inspect(record)

    def inspect(self, record):
        return {'running': True, 'paused': bool(self.paused), 'port': 32768, 'pid': 1000}

    def cpu_seconds(self, state):
        return self.cpu

    def pause(self, record):
        self.paused += 1

    def stop(self, record):
        self.paused += 1

    def snapshot(self, record, commit, manifest_path):
        assert manifest_path == 'implementation-manifest.json'
        return ({'contract': 'optimizer_v1', 'entrypoint': 'optimizer:create_optimizer',
                 'files': [{'path': 'optimizer.py', 'content': 'def create_optimizer(*args): pass\n'}]},
                {'contract': 'optimizer_v1', 'entrypoint': 'optimizer:create_optimizer',
                 'files': ['optimizer.py'], 'dependencies': {}, 'test_summary': 'Development tests passed'})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('GRATING_DEVELOPMENT_ENABLED', 'true')
    monkeypatch.setenv('GRATING_PI_PROVIDER', 'openai-codex')
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name='Development campaign', compute_budget_seconds=100,
        validation_reserve_seconds=10, implementation_compute_budget_seconds=30,
        tasks=[TaskInput(name='Quadratic', problem_id='bounded_continuous', configuration={})]))
    workspace.commands.execute(Command(id='activate-development', campaign_id=campaign['id'], expected_revision=1,
        operation='agent.activate', payload={'objective': 'Implement an optimizer'}))
    hypothesis = {'id': 'hypothesis_dev', 'campaign_id': campaign['id'], 'title': 'Test method',
                  'status': 'proposed', 'mechanism': 'A test implementation', 'algorithm': 'custom',
                  'algorithm_config': {}}
    workspace.store.put('hypothesis', hypothesis)
    driver = Driver(tmp_path / 'development')
    development = DevelopmentWorkspaces(workspace.pi, driver=driver)
    workspace.pi.development = development
    return workspace, campaign, development, driver


def outgoing(driver, record, identity, kind, payload):
    path = driver.root(record) / 'outgoing/events' / (identity + '.json')
    path.write_text(json.dumps({'type': kind, 'payload': payload}))


def test_persistent_workspace_receipts_and_replay(setup):
    workspace, campaign, development, driver = setup
    cid = campaign['id']
    request = {'hypothesis_id': 'hypothesis_dev', 'objective': 'Implement this method',
               'evidence_ids': [], 'request_key': 'request-one'}
    first = development.create(cid, request)
    second = development.create(cid, request)
    assert first['id'] == second['id']
    assert len(workspace.store.list('development_workspace', cid)) == 1
    development.sync(cid)
    assert driver.prepared == 1
    command_file = list((driver.root(first) / 'incoming/commands').glob('*.json'))
    assert len(command_file) == 1
    original_command = json.loads(command_file[0].read_text())
    assert original_command['operation'] == 'message'
    outgoing(driver, first, 'ready-event', 'ready', {'session_id': 'pi.jsonl', 'tools': ['bash', 'read', 'edit', 'write']})
    development.sync(cid)
    saved = development.get(cid, first['id'])
    assert saved['status'] == 'idle'
    assert saved['active_tools'] == ['bash', 'read', 'edit', 'write']
    assert development.events(cid, first['id'])['cursor'] == 2

    outgoing(driver, first, 'receipt-event', 'receipt', {'command_id': original_command['id'], 'status': 'accepted'})
    development.sync(cid)
    assert workspace.store.get(original_command['id'], 'development_command')['status'] == 'accepted'
    cursor = development.events(cid, first['id'])['cursor']
    outgoing(driver, first, 'receipt-event', 'receipt', {'command_id': original_command['id'], 'status': 'accepted'})
    development.sync(cid)
    assert development.events(cid, first['id'])['cursor'] == cursor
    restarted = DevelopmentWorkspaces(workspace.pi, driver=driver)
    assert restarted.get(cid, first['id'])['session_id'] == 'pi.jsonl'
    assert driver.prepared == 1


def test_questions_submissions_and_independent_validation_grant(setup, monkeypatch):
    workspace, campaign, development, driver = setup
    cid = campaign['id']
    record = development.create(cid, {'hypothesis_id': 'hypothesis_dev', 'objective': 'Implement',
                                       'request_key': 'create'})
    development.sync(cid)
    outgoing(driver, record, 'checkpoint', 'checkpoint', {'summary': 'Built the search module',
        'next_steps': 'Test seed replay', 'question': 'Should the seed be configurable?'})
    development.sync(cid)
    question = development.public(development.get(cid, record['id']))['questions'][0]
    answer = {'message': 'Yes, expose the seed.', 'mode': 'steer', 'request_key': 'answer', 'question_id': question['id']}
    development.message(cid, record['id'], answer)
    development.message(cid, record['id'], answer)
    assert workspace.store.get(question['id'], 'development_question')['status'] == 'answered'
    assert len([c for c in workspace.store.list('development_command', cid) if c['request_key'] == 'answer']) == 1

    commit = 'a' * 40
    outgoing(driver, record, 'submission-event', 'submission', {'commit': commit,
        'manifest_path': 'implementation-manifest.json', 'notes': 'Ready for independent checks'})
    development.sync(cid)
    submission = development.public(development.get(cid, record['id']))['submissions'][0]
    assert submission['commit'] == commit
    assert 'package' not in submission
    captured = {}
    def reserve(hypothesis_id, spec, **options):
        captured.update(hypothesis_id=hypothesis_id, spec=spec, **options)
        grant = {'id': 'grant_dev', 'campaign_id': cid, 'status': 'dispatching', 'compute_seconds': 0,
                 'request': {'compute_seconds': options['compute_seconds']}}
        workspace.store.put('implementation_grant', grant)
        return grant
    monkeypatch.setattr(workspace.implementations, 'reserve_commission', reserve)
    monkeypatch.setattr('optimization_framework.agents.capabilities.implementation_execution',
                        lambda: {'available': True})
    # The implementation module imports the capability function at invocation time.
    frozen = development.freeze_envelope(cid, record['id'], {'request_key': 'freeze',
        'spec': {'name': 'Test method', 'mechanism': 'Seeded search',
                 'acceptance_criteria': ['Seeded replay'], 'dependencies': {}}})
    assert frozen['checks'] == {'behavior': 0, 'mechanism': 0, 'diagnostic': 0}
    assert development.freeze_envelope(cid, record['id'], {'request_key': 'freeze',
        'spec': {'name': 'Test method', 'mechanism': 'Seeded search',
                 'acceptance_criteria': ['Seeded replay'], 'dependencies': {}}})['id'] == frozen['id']
    assert 'spec' not in development.public(development.get(cid, record['id']))['validation_envelopes'][0]
    result = development.validate(cid, record['id'], {'submission_id': submission['id'],
        'envelope_id': frozen['id'],
        'compute_seconds': 20, 'request_key': 'validate'})
    assert result['grant_id'] == 'grant_dev'
    assert captured['accounting_mode'] == 'execution_v1'
    assert captured['package']['files'][0]['content'].startswith('def create_optimizer')

    grant = workspace.store.get('grant_dev', 'implementation_grant')
    grant.update(status='failed', error='Independent check failed', attempts=[{'report': {'checks': [
        {'name': 'successful private reference', 'passed': True, 'detail': 'withheld expected design'},
        {'name': 'seed replay', 'passed': False, 'detail': 'replay diverged'}]},
        'review': {'findings': ['RNG state missing from checkpoint']}}])
    workspace.store.put('implementation_grant', grant)
    development.validation_feedback(development.get(cid, record['id']))
    saved = workspace.store.get(submission['id'], 'development_submission')
    assert saved['status'] == 'needs_revision'
    assert 'replay diverged' in json.dumps(saved['validation_outcome'])
    assert 'withheld expected design' not in json.dumps(saved['validation_outcome'])


def test_optional_cpu_allowance_pauses_without_losing_session(setup):
    workspace, campaign, development, driver = setup
    cid = campaign['id']
    record = development.create(cid, {'hypothesis_id': 'hypothesis_dev', 'objective': 'Implement',
        'request_key': 'limit', 'cpu_budget_seconds': 10})
    development.sync(cid)
    driver.cpu = 8
    development.sync(cid)
    assert development.get(cid, record['id'])['budget_checkpoint_requested']
    driver.cpu = 10
    development.sync(cid)
    saved = development.get(cid, record['id'])
    assert saved['status'] == 'paused'
    assert saved['usage']['container_cpu_seconds'] == 10
    assert driver.paused == 1
    with pytest.raises(ValueError, match='exhausted'):
        development.control(cid, record['id'], {'action': 'resume', 'request_key': 'too-soon',
                                                'expected_revision': saved['control_revision']})
