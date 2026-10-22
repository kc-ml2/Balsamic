"""Authority, durable recovery and evidence isolation for the Pi replacement."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from balsamic.api.internal_pi import active_implementation_grant
from optimization_framework.agents.tools import PiTools
from optimization_framework.contracts.requests import CampaignInput, TaskInput, TrialInput
from optimization_framework.contracts.commands import Command
from optimization_framework.execution.service import Workspace
from optimization_framework.storage.sqlite import now


def test_resumed_library_job_can_dispatch_review_before_grant_projection():
    grant = {'id': 'grant_one', 'campaign_id': 'campaign_one', 'job_id': 'job_one',
             'status': 'failed', 'request': {'agent_parent_id': 'pi_one'}}
    job = {'status': 'reviewing', 'request': {'grant_id': 'grant_one', 'campaign_id': 'campaign_one',
           'workspace_id': 'workspace_one', 'agent_parent_id': 'pi_one'}}
    client = SimpleNamespace(job=lambda identity: job)
    workspace = SimpleNamespace(implementations=SimpleNamespace(client=client, workspace_id='workspace_one'))
    assert active_implementation_grant(workspace, grant, 'pi_one')
    assert not active_implementation_grant(workspace, grant, 'pi_other')
    job['status'] = 'failed'
    assert not active_implementation_grant(workspace, grant, 'pi_one')


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv('GRATING_LLM_ENABLED', 'true')
    monkeypatch.setenv('GRATING_LLM_DISABLED', 'false')
    monkeypatch.setenv('GRATING_PI_PROVIDER', 'openai-codex')
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name='Pi campaign', compute_budget_seconds=100,
        validation_reserve_seconds=10, implementation_compute_budget_seconds=30,
        tasks=[TaskInput(name='Quadratic', problem_id='bounded_continuous', configuration={})]))
    workspace.commands.execute(Command(id='activate', campaign_id=campaign['id'], expected_revision=1,
        operation='agent.activate', payload={'objective': 'Implement the selected method within the existing allocation'}))
    agent = workspace.store.list('agent_session')[0]
    # Starting the team starts no turn; the lead's first turn is a researcher's request.
    assert not workspace.store.list('agent_run')
    workspace.pi._request(campaign['id'], True)
    run = workspace.pi.enqueue(agent, 'pi_run_first_request', 'Implement the selected method within the existing allocation')
    run['status'] = 'submitted'; workspace.store.put('agent_run', run)
    return workspace, campaign, workspace.store.get(agent['id'], 'agent_session'), run


def invoke(prepared, name, args=None, call_id='call'):
    workspace, _, agent, run = prepared
    return PiTools(workspace.pi).call(agent['id'], run['id'], call_id, name, args or {})


def test_repeated_unchanged_pi_inspection_does_not_emit_progress(prepared):
    workspace, campaign, agent, _ = prepared
    remote = {'events': [], 'cursor': agent.get('event_cursor', 0), 'runs': {}}
    workspace.pi._receive(agent['id'], remote)
    first = sum(row['kind'] == 'agent.progress' for row in workspace.store.events(campaign['id']))
    workspace.pi._receive(agent['id'], remote)
    second = sum(row['kind'] == 'agent.progress' for row in workspace.store.events(campaign['id']))
    assert first == second


def test_activation_preserves_allocations_and_uses_subscription_models(prepared):
    w, c, a, r = prepared
    current = w.store.get(c['id'], 'campaign')
    for key in ('compute_budget_seconds','implementation_compute_budget_seconds','validation_reserve_seconds','llm_budget_usd'):
        assert current[key] == c[key]
    assert current['autonomy'] == 'delegated'
    assert a['model'] == 'gpt-6-astra'
    assert a['usage']['api_cost_usd'] == 0
    assert w.pi.configuration(c['id'])['lead_id'] == a['id'] and w.pi.owns(c['id'])
    assert invoke(prepared, 'campaign_inspect')['tasks'][0]['evaluator_readiness']['runnable']


def test_duplicate_tools_commit_once_and_survive_restarted_controller(prepared):
    w, c, a, r = prepared
    args = {'title':'Saved result','kind':'finding','content':{'value':7}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(prepared,'artifact_save',args), range(2)))
    assert results[0] == results[1]
    assert len(w.store.list('agent_artifact')) == len(w.store.list('agent_tool_receipt')) == 1
    restarted = Workspace(w.directory)
    assert PiTools(restarted.pi).call(a['id'],r['id'],'call','artifact_save',args) == results[0]
    with pytest.raises(ValueError, match='identity'):
        invoke(prepared,'artifact_save',{**args,'title':'Changed'})


def test_local_tool_receipt_failure_rolls_back_mutation(prepared,monkeypatch):
    w, *_ = prepared
    put = w.store.put_immutable
    def fail(kind,*args,**kwargs):
        if kind == 'agent_tool_receipt': raise RuntimeError('crash before receipt')
        return put(kind,*args,**kwargs)
    monkeypatch.setattr(w.store,'put_immutable',fail)
    with pytest.raises(RuntimeError): invoke(prepared,'artifact_save',{'title':'result','kind':'finding','content':1})
    assert w.store.list('agent_artifact') == []


def test_retrieval_crosses_legacy_sessions_but_not_campaigns(prepared):
    w,c,*_ = prepared
    w.store.put('agent_artifact',{'id':'old','campaign_id':c['id'],'content':'x'*300000,'title':'historical'})
    w.store.put('agent_artifact',{'id':'foreign','campaign_id':'other','content':'secret'})
    result = invoke(prepared,'evidence_search',{'query':'historical'},'search')
    assert result['records'][0]['id'] == 'old'
    result = invoke(prepared,'evidence_read',{'record_id':'old','max_bytes':2048},'read')
    assert len(json.dumps(result)) < 6000
    assert 'error' in invoke(prepared,'evidence_read',{'record_id':'foreign'},'foreign')


def test_agents_can_cite_owned_execution_receipts_without_cross_campaign_access(prepared):
    w, c, *_ = prepared
    receipt = invoke(prepared, 'evidence_read', {'record_id': 'activate'}, 'read_command')
    assert receipt['request']['operation'] == 'agent.activate'
    saved = invoke(prepared, 'artifact_save', {'title': 'Migration recorded', 'kind': 'checkpoint',
        'content': {'claim': 'The campaign was activated'}, 'evidence_ids': ['activate']}, 'cite_command')
    assert 'error' not in saved
    w.store.put('work_command', {'id': 'foreign_command', 'campaign_id': 'other', 'outcome': 'private'})
    rejected = invoke(prepared, 'artifact_save', {'title': 'Invalid citation', 'kind': 'checkpoint',
        'content': 'Unavailable', 'evidence_ids': ['foreign_command']}, 'foreign_command')
    assert rejected['executed'] is False
    assert len(w.store.list('agent_artifact', c['id'])) == 1


def test_specialists_cannot_grant_authority_and_answers_invalidate_stale_commands(prepared):
    w,c,a,r = prepared
    result = invoke(prepared,'delegate',{'role':'methodology_specialist','objective':'Inspect one mechanism','request_key':'one'})
    child = w.store.get(result['agent_id'],'agent_session'); run = w.store.get(result['run_id'],'agent_run')
    run['status']='submitted';w.store.put('agent_run',run)
    with pytest.raises(ValueError,match='not available'):
        PiTools(w.pi).call(child['id'],run['id'],'forbidden','campaign_command',{})
    q = invoke(prepared,'researcher_ask',{'question':'Which objective?', 'reason':'Conflicting scientific specifications', 'suggestion':'Keep the charter objective.', 'options':['Keep the charter objective','Use the paper objective']},'question')
    w.pi.message(c['id'],{'message':'Use the current objective','question_id':q['id']},'answer')
    assert w.memory.state(c['id'])['guidance_revision'] == 1
    assert w.store.get(q['id'],'agent_question')['status'] == 'answered'
    task = w.current_tasks(c['id'])[0]
    rejected = invoke(prepared,'campaign_command',{'operation':'trial.create','guidance_revision':0,'request_key':'stale',
        'payload':{'task_id':task['id'],'algorithm':'coordinate','max_steps':2,'wall_seconds':5}},'stale')
    assert 'guidance changed' in rejected['error']
    assert w.store.list('trial') == []


class Client:
    def __init__(self): self.runs={}; self.submissions=[]; self.fail=False
    def status(self): return {'configured':True}
    def inspect(self,agent_id,after=0): return {'runs':self.runs,'events':[],'cursor':0,'session_id':'exact'}
    def submit(self,agent,run):
        self.submissions.append(run['id'])
        if self.fail: self.fail=False;raise ValueError('Lost POST before delivery')
        self.runs[run['id']]={'status':'running'}
    def control(self,*args): return {}


def test_lost_post_to_existing_agent_is_retried_with_exact_id(prepared):
    w,c,a,r = prepared
    client=Client();client.fail=True;w.pi.client=client
    w.pi._sync(c['id']); w.pi._sync(c['id'])
    assert client.submissions == [r['id'], r['id']]
    assert len(w.store.list('agent_run')) == 1


def test_child_completion_wakes_parent_exactly_once_and_counts_usage_once(prepared):
    w,c,a,r = prepared
    child = w.pi.create_agent(c['id'],'methodology_specialist','Review','child',parent_id=a['id'])
    run = w.pi.enqueue(child,'child_run','work')
    remote={'runs':{run['id']:{'status':'completed','result':{'text':'Done with evidence'}}},'cursor':1,'session_id':'child_session',
        'events':[{'seq':1,'occurred_at':now(),'type':'assistant.message','text':'Done','usage':{'input':5,'output':2}}]}
    w.pi._receive(child['id'],remote);w.pi._receive(child['id'],remote)
    assert len([r for r in w.store.list('agent_run') if r['id'].startswith('pi_child_result_')])==1
    assert w.store.get(child['id'],'agent_session')['usage']['calls']==1


def test_boolean_tool_failure_event_does_not_break_sync_or_authentication(prepared):
    w,c,a,r = prepared
    client=Client();w.pi.client=client
    client.inspect=lambda *args: {'runs': {r['id']: {'status': 'running'}}, 'cursor': 1,
        'events': [{'seq': 1, 'occurred_at': now(), 'type': 'tool_execution_end',
                    'tool': 'evidence_read', 'error': True}]}
    w.pi._sync(c['id'])
    assert w.pi.configuration(c['id'])['provider']['configured']
    assert not w.pi.configuration(c['id']).get('sync_error')
    assert 'evidence_read failed' in w.store.get(a['id'],'agent_session')['activity']


def test_projection_failure_preserves_auth_status_and_reports_sync_error(prepared, monkeypatch):
    w,c,*_ = prepared
    w.pi.client=Client()
    def fail(*args): raise RuntimeError('Projection failed')
    monkeypatch.setattr(w.pi, '_receive', fail)
    w.pi._sync(c['id'])
    config=w.pi.configuration(c['id'])
    assert config['provider']['configured']
    assert config['sync_error']=='Projection failed'


def test_steering_acknowledgment_does_not_claim_child_finished(prepared):
    w,c,a,_ = prepared
    child=w.pi.create_agent(c['id'],'methodology_specialist','Review','child',parent_id=a['id'])
    w.pi.enqueue(child,'child_run','work')
    steer=w.pi.enqueue(child,'steering','Update',mode='steer')
    w.pi._receive(child['id'], {'runs': {steer['id']: {'status': 'completed', 'result': {'delivery': 'steering_queued'}}},
        'events': [], 'cursor': 0})
    assert not any(r['id'].startswith('pi_child_result_') for r in w.store.list('agent_run'))
    assert w.store.get(child['id'],'agent_session')['status']=='running'


def test_control_revision_pause_and_rollback_keep_evidence(prepared):
    w,c,a,r=prepared
    w.pi.control(c['id'],{'action':'pause','expected_control_revision':0},'pause')
    with pytest.raises(ValueError,match='control changed'):
        w.pi.control(c['id'],{'action':'stop','expected_control_revision':0},'stale')
    with pytest.raises(ValueError,match='not active'):
        invoke(prepared,'campaign_inspect')
    with pytest.raises(ValueError,match='settling'):
        w.pi.rollback(c['id'],{'reason':'Test rollback'})
    r['status']='paused';w.store.put('agent_run',r)
    w.pi.rollback(c['id'],{'reason':'Test rollback'})
    rolled_back = w.pi.configuration(c['id'])
    assert not w.pi.owns(c['id']) and not rolled_back['enabled'] and rolled_back['rollback_reason'] == 'Test rollback'


def test_implementation_workers_cannot_read_protected_or_peer_evidence(prepared):
    w,c,a,_=prepared
    child=w.pi.create_agent(c['id'],'implementation_builder','Build','builder',parent_id=a['id'],grant_id='grant')
    names=PiTools(w.pi).names(child)
    assert 'evidence_read' not in names and 'campaign_command' not in names
    assert {'workspace_run','submit_output'} <= names


def test_private_gateway_requires_token(prepared,monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from balsamic.app import create_app
    token=tmp_path/'token';token.write_text('a'*64);monkeypatch.setenv('GRATING_PI_TOKEN_FILE',str(token))
    app=create_app(tmp_path/'api',start_workers=False)
    with TestClient(app) as client:
        assert client.post('/api/internal/pi/manifest',json={'agent_id':'x','run_id':'x'}).status_code==401


def test_large_dimensions_and_frozen_mechanism_checks():
    from optimization_framework.implementations.models import ImplementationSpec, MechanismCheck
    from optimization_framework.implementations.validation import check_invariant
    spec=ImplementationSpec(name='H12',mechanism='Tangent covariance',acceptance_criteria=['Unit norm'],n_cells_min=32768,n_cells_max=32768)
    assert spec.n_cells_max==32768
    unit=MechanismCheck(name='Unit direction',pointer='/direction',assertion='unit_norm')
    check_invariant({'direction':[1,0]},unit)
    with pytest.raises(ValueError):check_invariant({'direction':[2,0]},unit)
    tangent=MechanismCheck(name='Tangent factor',pointer='/factor',reference_pointer='/direction',assertion='tangent')
    check_invariant({'direction':[1,0],'factor':[[0,1]]},tangent)
    with pytest.raises(ValueError):check_invariant({'direction':[1,0],'factor':[[1,0]]},tangent)
    rank_zero=MechanismCheck(name='Rank-zero ambient factor',pointer='/state/covariance/ambient_U',
        reference_pointer='/state/mean',assertion='tangent')
    empty={'state':{'mean':[1,0],'covariance':{'ambient_U':[],'U':[]}}}
    check_invariant(empty,rank_zero)
    with pytest.raises(ValueError,match='invariant failed'):
        check_invariant({'state':{'mean':[1,0],'covariance':{'ambient_U':[],'U':[[1]]}}},rank_zero)
    psd=MechanismCheck(name='PSD',pointer='/covariance',assertion='positive_semidefinite')
    check_invariant({'covariance':[[1,0],[0,0]]},psd)
    with pytest.raises(ValueError):check_invariant({'covariance':[[1,0],[0,-1]]},psd)


def test_every_execution_tool_has_an_authoritative_schema(prepared):
    from optimization_framework.agents.tools import OPERATIONS
    assert OPERATIONS <= prepared[0].commands.describe().keys()


def test_new_proposal_requires_a_separate_reviewer_of_its_exact_revision(prepared):
    from optimization_framework.campaigns.proposals import hypothesis_revision, readiness
    w,c,a,r=prepared
    hypothesis={'id':'idea','campaign_id':c['id'],'title':'Proposed update','mechanism':'One step', 'requires_concept_review':True}
    w.store.put('hypothesis',hypothesis)
    assert not readiness(w.store,hypothesis)['eligible']
    arguments={'hypothesis_id':'idea','hypothesis_hash':hypothesis_revision(hypothesis),'verdict':'test',
        'rationale':'Coherent mechanism; efficacy unmeasured','suggested_tests':['Matched seed baseline']}
    with pytest.raises(ValueError,match='not available'):
        invoke(prepared,'proposal_review',arguments)
    child=w.pi.create_agent(c['id'],'proposal_reviewer','Review independently','reviewer',parent_id=a['id'])
    run=w.pi.enqueue(child,'review','Review')
    run['status']='submitted';w.store.put('agent_run',run)
    result=PiTools(w.pi).call(child['id'],run['id'],'review','proposal_review',arguments)
    assert result['verdict']=='test'
    assert readiness(w.store,hypothesis)['eligible']
    changed={**hypothesis,'mechanism':'Different step'}
    assert not readiness(w.store,changed)['eligible']


def test_paused_undelivered_message_is_requeued_on_resume(prepared):
    w,c,a,r=prepared
    run=w.pi.enqueue(a,'not_delivered','Important direction')
    w.pi.control(c['id'],{'action':'pause','expected_control_revision':0},'pause')
    assert w.store.get(run['id'],'agent_run')['status']=='paused'
    w.pi.control(c['id'],{'action':'resume','expected_control_revision':1},'resume')
    resumed=w.store.get(w.store.get(run['id'],'agent_run')['superseded_by'],'agent_run')
    assert resumed['input']=='Important direction' and resumed['status']=='queued'


def test_agents_are_inspected_on_notice_or_sweep_not_on_every_pass(prepared):
    w,c,a,r = prepared
    r = w.store.get(r['id'],'agent_run'); r['status']='running'; w.store.put('agent_run',r)
    child = w.pi.create_agent(c['id'],'methodology_specialist','Review','child',parent_id=a['id'])
    client=Client(); inspected=[]; statuses=[]
    client.inspect=lambda agent_id, after=0: inspected.append(agent_id) or {'runs':{},'events':[],'cursor':0}
    client.status=lambda: statuses.append(1) or {'configured':True}
    w.pi.client=client
    w.pi._sync(c['id'])
    assert sorted(inspected) == sorted([a['id'], child['id']]) and len(statuses) == 1
    inspected.clear(); w.pi._sync(c['id'])
    assert inspected == [] and len(statuses) == 1
    assert not w.pi._due(c['id'])
    w.pi.notify([child['id']]); w.pi.threads[c['id']].join(5)
    assert inspected == [child['id']]


def test_lead_extension_needs_researcher_approval_and_the_answer_wakes_the_lead(prepared):
    w, c, a, r = prepared
    task = w.current_tasks(c['id'])[0]
    created = invoke(prepared, 'campaign_command', {'operation': 'trial.create', 'guidance_revision': 0, 'request_key': 'trial',
        'payload': {'task_id': task['id'], 'algorithm': 'coordinate', 'max_steps': 2, 'wall_seconds': 5}}, 'trial')
    trial = w.store.get(created['outcome']['trial_id'], 'trial')
    refused = invoke(prepared, 'campaign_command', {'operation': 'trial.control', 'guidance_revision': 0, 'request_key': 'extend',
        'payload': {'trial_id': trial['id'], 'action': 'extend', 'expected_control_revision': 0, 'wall_seconds': 9}}, 'extend')
    assert 'researcher approval' in refused['error']
    filed = invoke(prepared, 'campaign_command', {'operation': 'trial.extension_request', 'guidance_revision': 0, 'request_key': 'more_time',
        'payload': {'trial_id': trial['id'], 'additional_seconds': 4, 'rationale': 'The curve is still improving'}}, 'more_time')
    decision = w.store.get(filed['outcome']['decision_id'], 'decision')
    assert decision['requested_by'] == 'lead' and w.store.get(trial['id'], 'trial')['wall_seconds'] == 5
    w.commands.execute(Command(id='approve', campaign_id=c['id'], expected_revision=w.store.get(c['id'], 'campaign')['version'],
        operation='decision.resolve', payload={'decision_id': decision['id'], 'choice': '0', 'comment': 'Go ahead',
        'expected_resolution_revision': 0}))
    assert w.store.get(trial['id'], 'trial')['wall_seconds'] == 9
    w.pi._campaign_events(c['id'])
    woken = [run for run in w.store.list('agent_run') if run['id'].startswith('pi_events_')]
    assert woken and 'decision.resolved' in woken[-1]['input']


def test_large_command_outcome_returns_as_an_index_the_lead_pages(prepared):
    from optimization_framework.agents.tools import COMMAND_RESULT_BYTES
    from optimization_framework.contracts.base import content_hash
    w, c, *_ = prepared
    curve = [{'cost': float(index), 'objective': index / 1e5} for index in range(20000)]
    report = {'groups': [{'id': 'group_one', 'common_observed_cost': 19999., 'method_summaries': [{'method_id': 'method_one', 'mean': .19999}],
        'trials': [{'id': 'trial_one', 'seed': 1, 'method_id': 'method_one', 'algorithm': 'random', 'status': 'completed',
                    'best_objective': .19999, 'censored': False, 'method': {'runtime': 'package ' * 20000}, 'curve': curve}]}]}
    identity = 'pi_command_' + content_hash([c['id'], 'comparison.report', 'compare'])[:32]
    w.store.put('work_command', {'id': identity, 'campaign_id': c['id'], 'status': 'completed', 'outcome': report,
        'request': {'operation': 'comparison.report', 'payload': {}}})
    arguments = {'operation': 'comparison.report', 'payload': {}, 'request_key': 'compare', 'guidance_revision': 0}
    result = invoke(prepared, 'campaign_command', arguments, 'compare')
    assert len(json.dumps(result).encode()) <= COMMAND_RESULT_BYTES and result['outcome_bytes'] > 500000
    assert result['comparison'][0]['method_summaries'] == report['groups'][0]['method_summaries']
    assert result['comparison'][0]['trials'] == [{'id': 'trial_one', 'seed': 1, 'method_id': 'method_one', 'algorithm': 'random',
        'status': 'completed', 'best_objective': .19999, 'censored': False, 'end_cost': 19999.}]
    assert result['outcome_index']['fields'][0]['pointer'] == '/outcome/groups'
    point = invoke(prepared, 'evidence_read', {'record_id': identity, 'pointer': '/outcome/groups/0/trials/0/curve/19999'}, 'page')
    assert point == curve[-1]
    # A small receipt is returned as it was saved.
    w.store.put('work_command', {'id': 'pi_command_' + content_hash([c['id'], 'comparison.report', 'small'])[:32], 'campaign_id': c['id'],
        'status': 'completed', 'outcome': {'groups': []}, 'request': {'operation': 'comparison.report', 'payload': {}}})
    assert invoke(prepared, 'campaign_command', {**arguments, 'request_key': 'small'}, 'small')['outcome'] == {'groups': []}


def test_no_tool_hands_the_model_more_than_it_can_hold(prepared, monkeypatch):
    from optimization_framework.agents.tools import TOOL_RESULT_BYTES
    monkeypatch.setattr(PiTools, 'inspect', lambda self, agent: {'history': 'x' * (TOOL_RESULT_BYTES + 1)})
    result = invoke(prepared, 'campaign_inspect', call_id='too_large')
    assert result['executed'] is True and 'at most' in result['error'] and len(json.dumps(result)) < 2000


def test_inspection_lists_the_methods_a_task_can_run_with_their_parameters(prepared):
    methods = {method['id']: method for method in invoke(prepared, 'campaign_inspect')['methods']}
    task = prepared[0].current_tasks(prepared[1]['id'])[0]
    assert methods['coordinate']['task_ids'] == [task['id']]
    assert methods['coordinate']['parameters']['radius'] == {'type': 'number', 'default': .2, 'maximum': 1, 'exclusiveMinimum': 0}
    assert set(methods['random']['parameters']) == {'initial_design'}
    # Binary-only searches and the 2D Fourier methods cannot run on a continuous task.
    assert not {'hillclimb', 'dqn', 'flrl_ppo'} & set(methods)


def test_the_lead_reads_a_finding_of_another_campaign_only_once_it_is_referenced(prepared):
    w, c, *_ = prepared
    earlier = w.create_campaign(CampaignInput(name='Earlier campaign', tasks=[TaskInput(name='Quadratic', problem_id='bounded_continuous')]))
    source = w.commands.execute(Command(id='earlier_source', campaign_id=earlier['id'], expected_revision=1, operation='source.record',
        payload={'title': 'Earlier benchmark', 'url': 'https://example.org/benchmark', 'excerpt': 'A small benchmark.'}))['outcome']['source_id']
    finding = w.commands.execute(Command(id='earlier_finding', campaign_id=earlier['id'], expected_revision=1, operation='finding.record',
        payload={'content': 'A short screen did not rank the methods; seeds differed more than methods.', 'source_ids': [source],
            'publish': True, 'problem_scope': 'The bounded quadratic instance.', 'limitations': ['Two seeds.'],
            'study_ids': [earlier['active_study_id']]}))['outcome']
    assert invoke(prepared, 'campaign_inspect', call_id='before')['reference_evidence'] == []
    assert 'error' in invoke(prepared, 'evidence_read', {'record_id': finding['asset_id']}, 'unreferenced')
    w.commands.execute(Command(id='reference_finding', campaign_id=c['id'], expected_revision=w.store.get(c['id'], 'campaign')['version'],
        operation='asset.reuse', payload={'asset_id': finding['asset_id'], 'study_id': w.store.get(c['id'], 'campaign')['active_study_id'],
            'decision': 'reference', 'intended_use': 'manager_evidence', 'rationale': 'Plan the screen with the earlier lesson in view.'}))
    listed = invoke(prepared, 'campaign_inspect', call_id='after')['reference_evidence']
    assert [row['asset_id'] for row in listed] == [finding['asset_id']]
    assert listed[0]['content'].startswith('A short screen') and listed[0]['applicability']['limitations'] == ['Two seeds.']
    whole = invoke(prepared, 'evidence_read', {'record_id': finding['asset_id']}, 'referenced')
    assert whole['payload']['content'].startswith('A short screen') and whole['payload']['limitations'] == ['Two seeds.']
    # The reference opens that finding alone: its evidence stays in the earlier campaign.
    assert 'error' in invoke(prepared, 'evidence_read', {'record_id': source}, 'its_source')
    assert 'error' in invoke(prepared, 'evidence_read', {'record_id': finding['finding_id']}, 'its_note')


def test_the_lead_may_ask_for_a_journal_compaction_but_not_run_one(prepared):
    from optimization_framework.agents.tools import OPERATIONS
    assert 'journal.compaction_request' in OPERATIONS and 'journal.compact' not in OPERATIONS
    refused = invoke(prepared, 'campaign_command', {'operation': 'journal.compact', 'payload': {}, 'request_key': 'compact', 'guidance_revision': 0}, 'compact')
    assert refused['executed'] is False and not prepared[0].store.list('journal_compaction')
    asked = invoke(prepared, 'campaign_command', {'operation': 'journal.compaction_request', 'payload': {'rationale': 'The results are recorded.'},
        'request_key': 'ask', 'guidance_revision': 0}, 'ask')
    assert 'journals left to compact' in asked['error']   # no experiment has run in this campaign


def test_agents_read_committed_repository_files_and_their_own_experiments_only(prepared, tmp_path, monkeypatch):
    from optimization_framework.agents import files
    w, c, *_ = prepared
    repository = tmp_path / "checkout"
    for name, text in {"docs/guide.md": "first\nsecond needle\nthird\n", "src/module.py": "value = 1\n", "secrets.env": "KEY=needle\n",
                       "runs/journal.jsonl": "needle\n"}.items():
        (repository / name).parent.mkdir(parents=True, exist_ok=True)
        (repository / name).write_text(text)
    monkeypatch.setattr(files, "REPOSITORY", repository)
    monkeypatch.setattr(files, "tracked", lambda: frozenset({"docs/guide.md", "src/module.py"}))   # what git tracks: no credentials, no run data
    top = invoke(prepared, 'file_list', {'root': 'repository'}, 'top')
    assert [(entry['name'], entry['type']) for entry in top['entries']] == [('docs', 'directory'), ('src', 'directory')]
    assert invoke(prepared, 'file_list', {'root': 'repository', 'path': 'docs'}, 'docs')['entries'] == [{'name': 'guide.md', 'type': 'file', 'bytes': 26}]
    page = invoke(prepared, 'file_read', {'root': 'repository', 'path': 'docs/guide.md', 'lines': 2}, 'page')
    assert (page['content'], page['last_line'], page['next_first_line']) == ('first\nsecond needle', 2, 3)
    rest = invoke(prepared, 'file_read', {'root': 'repository', 'path': 'docs/guide.md', 'first_line': 3}, 'rest')
    assert (rest['content'], rest['next_first_line']) == ('third', None)
    for call, path in enumerate(['secrets.env', 'runs/journal.jsonl', '../outside', '/etc/passwd', 'docs']):
        assert 'error' in invoke(prepared, 'file_read', {'root': 'repository', 'path': path}, f'closed{call}')
    found = invoke(prepared, 'file_search', {'root': 'repository', 'query': 'needle'}, 'search')
    assert found['matches'] == [{'path': 'docs/guide.md', 'line': 2, 'text': 'second needle'}] and found['complete']
    assert invoke(prepared, 'file_search', {'root': 'repository', 'query': 'value', 'glob': '*.md'}, 'glob')['matches'] == []
    # An experiment's directory: its own campaign's only, without saved states.
    task = w.current_tasks(c['id'])[0]
    trial = invoke(prepared, 'campaign_command', {'operation': 'trial.create', 'guidance_revision': 0, 'request_key': 'trial',
        'payload': {'task_id': task['id'], 'algorithm': 'coordinate', 'max_steps': 2, 'wall_seconds': 5}}, 'trial')['outcome']['trial_id']
    directory = w.job_dir(trial)
    (directory / 'checkpoints').mkdir(exist_ok=True)
    (directory / 'checkpoints' / 'state.pkl').write_bytes(b'\x80')
    names = [entry['name'] for entry in invoke(prepared, 'file_list', {'root': 'experiment', 'trial_id': trial}, 'trial_files')['entries']]
    assert 'spec.json' in names and 'checkpoints' not in names
    assert '"coordinate"' in invoke(prepared, 'file_read', {'root': 'experiment', 'trial_id': trial, 'path': 'spec.json'}, 'spec')['content']
    assert 'error' in invoke(prepared, 'file_read', {'root': 'experiment', 'trial_id': trial, 'path': 'checkpoints/state.pkl'}, 'state')
    w.store.put('trial', {'id': 'trial_elsewhere', 'campaign_id': 'campaign_elsewhere', 'status': 'completed'})
    assert 'error' in invoke(prepared, 'file_list', {'root': 'experiment', 'trial_id': 'trial_elsewhere'}, 'elsewhere')


def test_an_agent_analyses_its_experiments_with_its_own_script_in_a_sandbox(prepared):
    import shutil, subprocess
    if not shutil.which('bwrap') or subprocess.run(['bwrap', '--unshare-all', '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/lib64', '/lib64', '--ro-bind', '/bin', '/bin', '/usr/bin/true'], capture_output=True).returncode:
        pytest.skip('This host cannot establish the sandbox namespaces')
    w, c, agent, _ = prepared
    task = w.current_tasks(c['id'])[0]
    trial = invoke(prepared, 'campaign_command', {'operation': 'trial.create', 'guidance_revision': 0, 'request_key': 'trial',
        'payload': {'task_id': task['id'], 'algorithm': 'coordinate', 'max_steps': 2, 'wall_seconds': 5}}, 'trial')['outcome']['trial_id']
    script = f'''import json, os, numpy
from optimization_framework.execution.metrics import scalar_rows
print("algorithm", json.load(open("/data/{trial}/spec.json"))["algorithm"], "| numpy", numpy.zeros(2).sum())
print("workspace", os.path.exists("{w.directory}"), "| credentials", [os.path.exists(os.path.join("{Path.home()}", name)) for name in (".config", ".ssh", ".pi")])
try:
    open("/data/{trial}/spec.json", "w")
except OSError as error:
    print("experiment files are read-only:", type(error).__name__)
open("result.txt", "w").write("kept")
'''
    assert invoke(prepared, 'workspace_write', {'path': 'look.py', 'content': script}, 'write')['saved']
    result = invoke(prepared, 'analysis_run', {'path': 'look.py', 'trial_ids': [trial], 'seconds': 60}, 'run')
    assert result['exit_code'] == 0, result
    assert 'algorithm coordinate | numpy 0.0' in result['stdout'] and 'read-only: OSError' in result['stdout']
    # Neither the workspace (database, other campaigns) nor the user's configuration and keys exist inside the sandbox.
    assert 'workspace False | credentials [False, False, False]' in result['stdout'] and result['mounted'] == ['/data/' + trial]
    assert invoke(prepared, 'workspace_read', {'path': 'result.txt'}, 'kept')['content'] == 'kept'
    assert w.store.get(agent['id'], 'agent_session')['analysis_seconds'] > 0
    invoke(prepared, 'workspace_write', {'path': 'slow.py', 'content': 'import time\ntime.sleep(30)\n'}, 'slow')
    assert invoke(prepared, 'analysis_run', {'path': 'slow.py', 'seconds': 1}, 'timeout')['status'] == 'timeout'
    # Another campaign's experiment cannot be mounted, and a script must be the agent's own file.
    w.store.put('trial', {'id': 'trial_elsewhere', 'campaign_id': 'campaign_elsewhere', 'status': 'completed'})
    assert 'error' in invoke(prepared, 'analysis_run', {'path': 'look.py', 'trial_ids': ['trial_elsewhere']}, 'foreign')
    assert 'error' in invoke(prepared, 'analysis_run', {'path': '../look.py'}, 'outside')


def test_what_only_the_researcher_grants_is_asked_for_and_applied_on_approval(prepared, monkeypatch):
    w, c, *_ = prepared
    cid = c['id']
    ask = lambda key, payload: invoke(prepared, 'campaign_command', {'operation': 'approval.request', 'request_key': key,
        'guidance_revision': w.memory.state(cid)['guidance_revision'], 'payload': payload}, key)
    approve = lambda key, decision, choice='0': w.commands.execute(Command(id=key, campaign_id=cid, expected_revision=w.store.get(cid, 'campaign')['version'],
        operation='decision.resolve', payload={'decision_id': decision, 'choice': choice, 'comment': 'Agreed', 'expected_resolution_revision': 0}))
    # A campaign limit: the lead cannot set it, and nothing changes until the researcher approves.
    limit = ask('limit', {'kind': 'campaign_limits', 'limits': {'delegated_trial_seconds': 7200, 'compute_budget_seconds': 400},
        'rationale': 'A full run takes two hours at the measured speed.'})
    decision = w.store.get(limit['outcome']['decision_id'], 'decision')
    assert decision['status'] == 'pending' and 'delegated_trial_seconds 60 to 7200' in decision['title']
    assert w.store.get(cid, 'campaign')['delegated_trial_seconds'] == 60
    approve('approve_limit', decision['id'])
    campaign = w.store.get(cid, 'campaign')
    assert (campaign['delegated_trial_seconds'], campaign['compute_budget_seconds']) == (7200, 400)
    assert w.store.list('campaign_budget_amendment', cid)[0]['limits'] == {'compute_budget_seconds': 400, 'delegated_trial_seconds': 7200}
    assert 'already has these limits' in ask('same', {'kind': 'campaign_limits', 'limits': {'compute_budget_seconds': 400}, 'rationale': 'Again.'})['error']
    assert 'error' in ask('too_long', {'kind': 'campaign_limits', 'limits': {'delegated_trial_seconds': 90000}, 'rationale': 'More than a day.'})
    # The worker ceiling: declined leaves it, approved changes it at once.
    before = w.max_workers
    ceiling = ask('ceiling', {'kind': 'worker_ceiling', 'workers': before + 3, 'rationale': 'Throughput was still rising at the ceiling.'})
    approve('decline_ceiling', ceiling['outcome']['decision_id'], '1')
    assert w.max_workers == before and not w.store.list('worker_ceiling_change', cid)
    ceiling = ask('ceiling_again', {'kind': 'worker_ceiling', 'workers': before + 3, 'rationale': 'Throughput was still rising at the ceiling.'})
    approve('approve_ceiling', ceiling['outcome']['decision_id'])
    assert w.max_workers == before + 3 and w.store.list('worker_ceiling_change', cid)[0]['previous'] == before
    direct = invoke(prepared, 'campaign_command', {'operation': 'workers.set', 'payload': {'workers': 1, 'rationale': 'x'},
        'request_key': 'direct', 'guidance_revision': 0}, 'direct')
    assert direct['executed'] is False and w.max_workers == before + 3   # the lead cannot set it itself
    # A waiver of a check that blocks work.
    monkeypatch.setenv('GRATING_RUNTIME_GATE_SECONDS', '100')
    task = w.current_tasks(cid)[0]
    with pytest.raises(ValueError, match='runtime'):
        w.create_trial(TrialInput(campaign_id=cid, task_id=task['id'], algorithm='coordinate', max_steps=2, wall_seconds=120))
    requirement = w.store.list('validation_requirement', cid)[0]
    assert 'error' in ask('bare', {'kind': 'waiver', 'requirement_id': requirement['id'], 'rationale': 'No evidence named.'})
    waiver = ask('waiver', {'kind': 'waiver', 'requirement_id': requirement['id'], 'evidence_ids': [task['id']], 'rationale': 'A rehearsal; speed is not at issue.'})
    assert not w.store.list('waiver', cid)
    approve('approve_waiver', waiver['outcome']['decision_id'])
    granted = w.store.list('waiver', cid)[0]
    assert (granted['requirement_id'], granted['authority'], granted['evidence_ids']) == (requirement['id'], 'researcher', [task['id']])
    assert granted['rationale'].startswith('A rehearsal; speed is not at issue.') and 'Agreed' in granted['rationale']


def test_a_message_that_arrives_as_the_lead_finishes_is_still_delivered(prepared):
    w, c, lead, first = prepared
    finish = lambda run: w.pi._receive(lead['id'], {'runs': {run['id']: {'status': 'completed', 'result': {'text': 'Done'}}}, 'events': [], 'cursor': 0})
    complete = lambda run: w.store.put('agent_run', {**w.store.get(run['id'], 'agent_run'), 'status': 'submitted', 'disposition': {'disposition': 'complete', 'summary': 'All done'}})
    # The lead's turn calls its request fulfilled while the researcher's next message already waits.
    message = w.pi.message(c['id'], {'message': 'One more thing before you stop.'}, 'late_message')
    complete(first); finish(first)
    assert w.pi.configuration(c['id'])['request_open'] and w.store.get(message['id'], 'agent_run')['status'] == 'queued'
    # With nothing waiting, the same disposition closes the request. The team stays available.
    complete(message); finish(message)
    config = w.pi.configuration(c['id'])
    assert config['status'] == 'running' and not config['request_open']
    assert w.store.get(lead['id'], 'agent_session')['status'] == 'waiting'
    # The next message is the next request and is dispatched.
    later = w.pi.message(c['id'], {'message': 'A follow-up'}, 'later')
    w.pi.client = Client()
    w.pi._sync(c['id'])
    assert w.pi.configuration(c['id'])['request_open'] and w.store.get(later['id'], 'agent_run')['status'] in {'submitted', 'running'}


def activated(tmp_path, monkeypatch, payload=None):
    monkeypatch.setenv('GRATING_LLM_ENABLED', 'true')
    monkeypatch.setenv('GRATING_LLM_DISABLED', 'false')
    monkeypatch.setenv('GRATING_PI_PROVIDER', 'openai-codex')
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name='Service campaign', compute_budget_seconds=100, validation_reserve_seconds=10,
        tasks=[TaskInput(name='Quadratic', problem_id='bounded_continuous', configuration={})]))
    workspace.commands.execute(Command(id='start_team', campaign_id=campaign['id'], expected_revision=1, operation='agent.activate', payload=payload or {}))
    return workspace, campaign, workspace.store.get(workspace.pi.configuration(campaign['id'])['lead_id'], 'agent_session')


def test_starting_the_team_starts_no_work_until_the_researcher_asks(tmp_path, monkeypatch):
    from optimization_framework.agents.models import NOTICE, STANDING_OBJECTIVE
    w, c, lead = activated(tmp_path, monkeypatch)
    client = Client(); w.pi.client = client
    config = w.pi.configuration(c['id'])
    assert config['status'] == 'running' and not config['request_open'] and config['objective'] == STANDING_OBJECTIVE
    assert lead['status'] == 'waiting' and 'first request' in lead['activity'] and lead['objective'] == STANDING_OBJECTIVE
    # Nothing is queued, and synchronizing submits nothing to the model.
    w.pi._sync(c['id'])
    assert not w.store.list('agent_run') and client.submissions == []
    assert w.pi.progress(c['id'])['headline'] == 'Lead agent is waiting for your request'
    # An experiment the researcher starts by hand finishes without waking the lead...
    task = w.current_tasks(c['id'])[0]
    trial = w.create_trial(TrialInput(campaign_id=c['id'], task_id=task['id'], algorithm='coordinate', max_steps=2, wall_seconds=5))
    w.store.put('trial', {**w.store.get(trial['id'], 'trial'), 'status': 'completed'}, 'trial.evidence_cataloged')
    w.pi._campaign_events(c['id'])
    assert not w.store.list('agent_run')
    # ...and so does pausing and resuming the team.
    w.pi.control(c['id'], {'action': 'pause', 'expected_control_revision': 0}, 'pause')
    w.pi.control(c['id'], {'action': 'resume', 'expected_control_revision': 1}, 'resume')
    assert not w.store.list('agent_run') and w.store.get(lead['id'], 'agent_session')['status'] == 'waiting'
    # The researcher's first message is the lead's first turn, delivered as written.
    run = w.pi.message(c['id'], {'message': 'Propose three hypotheses. Do not run anything yet.'}, 'first')
    w.pi._sync(c['id'])
    assert client.submissions == [run['id']] and run['input'] == 'Propose three hypotheses. Do not run anything yet.'
    assert w.pi.configuration(c['id'])['request_open'] and not run['input'].startswith(NOTICE)


def test_outcomes_wake_the_lead_only_for_an_open_request_and_a_profile_reports_once(tmp_path, monkeypatch):
    from optimization_framework.agents.models import NOTICE
    w, c, lead = activated(tmp_path, monkeypatch)
    task = w.current_tasks(c['id'])[0]
    woken = lambda: [run for run in w.store.list('agent_run') if run['id'].startswith('pi_events_')]
    def finished(event='trial.evidence_cataloged'):
        trial = w.create_trial(TrialInput(campaign_id=c['id'], task_id=task['id'], algorithm='coordinate', max_steps=2, wall_seconds=5))
        w.store.put('trial', {**w.store.get(trial['id'], 'trial'), 'status': 'completed'}, event)
        return trial
    w.pi.message(c['id'], {'message': 'Measure the runtime, then run one short experiment.'}, 'request')
    # The probes of a runtime profile do not wake the lead one by one; the profile's end does, once.
    probes = [finished(), finished()]
    profile = {'id': 'runtime_profile_test', 'campaign_id': c['id'], 'status': 'running', 'levels': [{'workers': 2, 'trial_ids': [probe['id'] for probe in probes]}]}
    w.store.put('runtime_profile', profile, 'runtime.profile_started')
    w.pi._campaign_events(c['id'])
    assert not woken()
    w.store.put('runtime_profile', {**profile, 'status': 'completed'}, 'runtime.profile_completed')
    w.pi._campaign_events(c['id'])
    assert len(woken()) == 1 and woken()[0]['input'].startswith(NOTICE) and 'runtime.profile_completed' in woken()[0]['input']
    # An experiment of the request wakes it; a change the researcher makes to the campaign does not.
    experiment = finished()
    w.pi._campaign_events(c['id'])
    assert len(woken()) == 2 and experiment['id'] in woken()[-1]['input']
    w.store.put('campaign', w.store.get(c['id'], 'campaign'), 'campaign.updated')
    w.pi._campaign_events(c['id'])
    assert len(woken()) == 2
    # Once the lead reports the request fulfilled, later outcomes are not its business until the researcher writes again.
    w.pi._request(c['id'], False)
    finished()
    w.pi._campaign_events(c['id'])
    assert len(woken()) == 2
    w.pi.message(c['id'], {'message': 'Summarise what ran.'}, 'second')
    w.pi._campaign_events(c['id'])
    assert len(woken()) == 2   # what happened meanwhile is read from the campaign state, not replayed as wake-ups


def test_a_silent_turn_is_asked_for_its_checkpoint_not_told_to_continue(prepared):
    from optimization_framework.agents.models import NOTICE
    w, c, lead, first = prepared
    w.pi._receive(lead['id'], {'runs': {first['id']: {'status': 'completed', 'result': {'text': 'Here is my answer.'}}}, 'events': [], 'cursor': 0})
    nudges = [run for run in w.store.list('agent_run') if run['id'].startswith('pi_continue_')]
    assert len(nudges) == 1 and nudges[0]['input'].startswith(NOTICE)
    assert 'yield_work(complete)' in nudges[0]['input'] and 'Start nothing the request did not ask for' in nudges[0]['input']
    assert 'Continue the authorized objective' not in nudges[0]['input'] and w.pi.configuration(c['id'])['request_open']


def test_instructions_make_the_lead_act_on_requests_and_saved_assignments_follow(prepared):
    from optimization_framework.agents.models import FORMER_OBJECTIVE, STANDING_OBJECTIVE
    from optimization_framework.agents.controller import PiController
    w, c, lead, run = prepared
    instructions = PiTools(w.pi).manifest(lead['id'], run['id'])['instructions']
    assert "at the researcher's request" in instructions and 'start one only when a request covers it' in instructions
    assert 'end-to-end' not in instructions and 'If useful independent work remains' not in instructions
    # A team started by an earlier version was told to continue the campaign by itself; its saved assignment is replaced.
    w.store.put('agent_session', {**w.store.get(lead['id'], 'agent_session'), 'objective': FORMER_OBJECTIVE})
    w.store.put('agent_campaign', {**w.pi.configuration(c['id']), 'objective': FORMER_OBJECTIVE})
    restarted = PiController(w)
    assert restarted.store.get(lead['id'], 'agent_session')['objective'] == STANDING_OBJECTIVE
    assert restarted.configuration(c['id'])['objective'] == STANDING_OBJECTIVE


def test_the_live_view_names_each_step_and_saves_nothing(prepared):
    w, c, a, r = prepared
    a['status'] = 'running'; w.store.put('agent_session', a)
    client = Client(); w.pi.client = client
    client.live = lambda ids: {a['id']: {'run_id': r['id'], 'phase': 'tool', 'started_at': now(), 'updated_at': now(), 'finished_at': None,
        'thinking': 'Compare H5 and H6 first.', 'text': '',
        'steps': [{'tool': 'campaign_inspect', 'args': {}, 'finished_at': now()},
                  {'tool': 'campaign_command', 'args': {'operation': 'trial.create', 'payload': {'title': 'H6 seed 0'}}},
                  {'tool': 'evidence_read', 'args': {'record_id': 'trial_9a3d71ea86eb4321'}, 'finished_at': now(), 'failed': True},
                  {'tool': 'yield_work', 'args': {}}]}}
    kinds = ('agent_session', 'agent_run', 'message', 'agent_tool_intent')
    before = (w.store.revision(*kinds), len(w.store.events(c['id'])))
    live = w.pi.live(c['id'])['agents']
    assert [(row['role'], row['phase'], row['thinking'], row['run_id']) for row in live] == [('lead', 'tool', 'Compare H5 and H6 first.', r['id'])]
    assert live[0]['steps'] == [{'label': "Reading the campaign's state", 'done': True, 'failed': False},
                                {'label': 'Starting an experiment: H6 seed 0', 'done': False, 'failed': False},
                                {'label': 'Reading experiment …eb4321', 'done': True, 'failed': True}]
    # Shown, not saved: the reasoning reaches no record and no event.
    assert (w.store.revision(*kinds), len(w.store.events(c['id']))) == before
    # A turn that has ended shows nothing, and an unreachable agent service leaves the agent listed without steps.
    client.live = lambda ids: {a['id']: {'run_id': r['id'], 'finished_at': now(), 'thinking': 'earlier', 'steps': [{'tool': 'campaign_inspect'}]}}
    assert [(row['steps'], row['thinking'], row['phase']) for row in w.pi.live(c['id'])['agents']] == [([], '', 'queued')]
    def unreachable(ids): raise ValueError('Pi service is unavailable')
    client.live = unreachable
    assert [row['phase'] for row in w.pi.live(c['id'])['agents']] == ['queued']
    # An idle team has nothing to show, and the harness is not asked.
    w.store.put('agent_session', {**w.store.get(a['id'], 'agent_session'), 'status': 'waiting'})
    assert w.pi.live(c['id']) == {'agents': [], 'reports': []}
    with pytest.raises(KeyError):
        w.pi.live('campaign_unknown')


def test_a_tool_call_shows_as_readable_activity(prepared):
    w, c, a, r = prepared
    w.pi._receive(a['id'], {'runs': {r['id']: {'status': 'running'}}, 'cursor': 1,
        'events': [{'seq': 1, 'occurred_at': now(), 'type': 'tool_execution_start', 'tool': 'campaign_inspect', 'summary': 'tool execution start'}]})
    assert w.store.get(a['id'], 'agent_session')['activity'] == "Reading the campaign's state"


def test_a_reply_records_the_kind_of_turn_it_came_from(prepared):
    from optimization_framework.agents.controller import PiController
    from optimization_framework.agents.models import NOTICE
    w, c, a, r = prepared
    w.store.put('agent_run', {**w.store.get(r['id'], 'agent_run'), 'disposition': {'disposition': 'waiting', 'summary': 'Three runs started.'}})
    w.pi._receive(a['id'], {'runs': {r['id']: {'status': 'completed', 'result': {'text': 'Started three runs.'}}}, 'events': [], 'cursor': 0})
    reply = w.store.get('message_' + r['id'], 'message')
    assert (reply['turn'], reply['disposition']) == ('request', 'waiting')
    notice = w.pi.enqueue(w.store.get(a['id'], 'agent_session'), 'pi_events_one', NOTICE + 'Work in this campaign has new outcomes.')
    w.store.put('agent_run', {**notice, 'status': 'submitted'})
    w.pi._receive(a['id'], {'runs': {notice['id']: {'status': 'completed', 'result': {'text': 'Still waiting on two runs.'}}}, 'events': [], 'cursor': 0})
    note = w.store.get('message_pi_events_one', 'message')
    assert (note['turn'], note['disposition']) == ('notice', None) and 'parts' not in note
    # A turn with lines written beside earlier tool calls keeps them apart, so the chat can fold them.
    later = w.pi.enqueue(w.store.get(a['id'], 'agent_session'), 'pi_events_two', NOTICE + 'Work in this campaign has new outcomes.')
    w.store.put('agent_run', {**later, 'status': 'submitted'})
    w.pi._receive(a['id'], {'runs': {later['id']: {'status': 'completed', 'result': {'text': 'Restarting the writer.\n\nThe writer restarted; see Reports.',
        'parts': ['Restarting the writer.', 'The writer restarted; see Reports.']}}}, 'events': [], 'cursor': 0})
    both = w.store.get('message_pi_events_two', 'message')
    assert both['parts'] == ['Restarting the writer.', 'The writer restarted; see Reports.'] and both['content'].endswith('see Reports.')
    # A reply saved before replies were marked is marked once, when the service starts.
    w.store.put('message', {key: value for key, value in reply.items() if key not in {'turn', 'disposition'}})
    assert 'turn' not in w.store.get(reply['id'], 'message')
    PiController(w)
    marked = w.store.get(reply['id'], 'message')
    assert (marked['turn'], marked['disposition'], marked['content']) == ('request', 'waiting', 'Started three runs.')


def test_the_lead_is_given_the_pages_and_a_receipt_names_the_page_of_what_it_created(prepared):
    from optimization_framework.agents.tools import INSTRUCTIONS, command_page
    pages = invoke(prepared, 'campaign_inspect')['pages']
    links = [page['link'] for page in pages]
    assert len(set(links)) == len(links) and {'#experiments', '#hypotheses', '#hypotheses/rounds', '#hypotheses/implementations', '#files/reports', '#evolution/frontier', '#monitor/agents'} <= set(links)
    assert all(page['name'] for page in pages) and all(page['shows'] for page in pages if '/' not in page['link'])
    assert 'campaign_inspect.pages' in INSTRUCTIONS and 'link the page instead' in INSTRUCTIONS
    assert command_page({'request': {'operation': 'trial.create'}, 'outcome': {'trial_id': 'trial_1'}}) == '#experiments/trial_1'
    assert command_page({'request': {'operation': 'hypothesis.create'}, 'outcome': {'hypothesis_id': 'h'}}) == '#hypotheses/h'
    assert command_page({'request': {'operation': 'draft.save'}, 'outcome': {'draft_id': 'd'}}) == '#experiments/drafts'
    assert command_page({'request': {'operation': 'study.race.decide'}, 'outcome': None}) == '#evolution/races'
    assert command_page({'request': {'operation': 'journal.compact'}, 'outcome': None}) is None
    # A real receipt carries it, small or large.
    tools = PiTools(prepared[0].pi)
    record = {'id': 'pi_command_x', 'status': 'completed', 'request': {'operation': 'comparison.report', 'payload': {}}, 'outcome': {'groups': []}}
    assert tools.receipt(record)['page'] == '#evolution/curves'
    assert tools.receipt({**record, 'outcome': {'groups': [], 'filler': 'x' * 60000}})['page'] == '#evolution/curves'
    assert 'page' not in tools.receipt({**record, 'request': {'operation': 'journal.compact', 'payload': {}}})


def test_resource_events_wake_the_resource_planner_which_plans_but_starts_nothing(tmp_path, monkeypatch):
    from optimization_framework.agents.models import NOTICE
    w, c, lead = activated(tmp_path, monkeypatch)
    kind = 'kind_' + '0' * 24
    w.store.event(c['id'], 'resources.unmeasured_kind', {'record_id': 'trial_x', 'kind': kind, 'label': 'Quadratic · coordinate'})
    w.pi._campaign_events(c['id'])
    planner = w.store.get(w.pi.page_agent(c['id'], 'resource_planner')['id'], 'agent_session')
    runs = [run for run in w.store.list('agent_run') if run['agent_id'] == planner['id']]
    assert len(runs) == 1 and runs[0]['input'].startswith(NOTICE) and 'resources.unmeasured_kind' in runs[0]['input']
    assert not [run for run in w.store.list('agent_run') if run['agent_id'] == lead['id']]   # the lead is not woken for it
    run = runs[0]; run['status'] = 'submitted'; w.store.put('agent_run', run)
    tools = PiTools(w.pi)
    manifest = tools.manifest(planner['id'], run['id'])
    names = {tool['name'] for tool in manifest['tools']}
    assert {'resource_inspect', 'campaign_command', 'experiment_progress', 'lead_ask'} <= names and 'page_agent_message' not in names
    assert 'balanced resource level' in manifest['instructions'] and 'resources.estimate' in manifest['instructions']
    assert tools.schema(planner, 'campaign_command')['properties']['operation']['enum'] == ['approval.request', 'resources.estimate', 'resources.plan', 'runtime.profile']
    # It reads each running run's memory and the GPU's processes, and records an estimate and a plan through its own commands.
    inspected = tools.call(planner['id'], run['id'], 'inspect', 'resource_inspect', {})
    assert {'running', 'gpu_processes', 'kinds', 'level'} <= set(inspected['admission'])
    revision = w.memory.state(c['id'])['guidance_revision']
    def command(key, operation, payload):
        return tools.call(planner['id'], run['id'], key, 'campaign_command', {'operation': operation, 'request_key': key,
            'guidance_revision': revision, 'payload': payload})
    command('estimate', 'resources.estimate', {'kind': kind, 'memory_bytes': 2e9, 'device_bytes': 3e9, 'rationale': 'Like the measured coordinate runs.'})
    assert w.admission.expected(kind)['basis'] == 'estimated' and w.admission.expected(kind)['memory_bytes'] == 3e9   # balanced: ×1.5
    command('plan', 'resources.plan', {'workers': 1, 'kinds': {kind: 1}, 'rationale': 'One GPU run at a time while the card is shared.'})
    assert w.admission.plan_of(c['id'])['workers'] == 1 and w.admission.plan_of(c['id'])['set_by'] == 'manager'
    refused = command('too_many', 'resources.plan', {'workers': 9, 'rationale': 'More.'})
    assert 'researcher' in str(refused)
    assert 'executed' in command('start', 'trial.create', {'task_id': w.current_tasks(c['id'])[0]['id']}) and not w.store.list('trial')
    # The researcher's level change reaches the campaigns that have a resource planner.
    w.admission.set_level('aggressive', authority='researcher', reason='The other GPU job ended')
    w.pi._campaign_events(c['id'])
    assert len([run for run in w.store.list('agent_run') if run['agent_id'] == planner['id']]) == 2


def test_an_implementation_agent_runs_its_script_with_the_servers_python_in_the_sandbox(prepared):
    import shutil
    if not shutil.which('bwrap'):
        pytest.skip('bubblewrap is not installed')
    w, c, lead, _ = prepared
    builder = w.pi.create_agent(c['id'], 'implementation_builder', 'Build', 'builder_sandbox', parent_id=lead['id'], grant_id='grant')
    run = w.pi.enqueue(builder, 'builder_run', 'Build it')
    run['status'] = 'submitted'; w.store.put('agent_run', run)
    tools = PiTools(w.pi)
    tools.call(builder['id'], run['id'], 'write', 'workspace_write', {'path': 'check.py', 'content': 'import json, sys\nprint(json.dumps(sys.version_info[:2]))\n'})
    # The environment's python is a link into its installation (with uv, through a link to the minor version); the sandbox holds them all.
    result = tools.call(builder['id'], run['id'], 'run', 'workspace_run', {'path': 'check.py'})
    assert result['exit_code'] == 0, result.get('stderr')
    assert json.loads(result['stdout']) == list(__import__('sys').version_info[:2])
