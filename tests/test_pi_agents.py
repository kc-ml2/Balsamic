"""Authority, durable recovery and evidence isolation for the Pi replacement."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import pytest

from optimization_framework.agents.api import active_implementation_grant
from optimization_framework.agents.tools import PiTools
from optimization_framework.contracts.requests import CampaignInput, TaskInput
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
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name='Pi campaign', compute_budget_seconds=100,
        validation_reserve_seconds=10, implementation_compute_budget_seconds=30,
        tasks=[TaskInput(name='Quadratic', problem_id='bounded_continuous', configuration={})]))
    workspace.commands.execute(Command(id='activate', campaign_id=campaign['id'], expected_revision=1,
        operation='agent.activate', payload={'objective': 'Implement the selected method within the existing allocation'}))
    agent = workspace.store.list('agent_session')[0]
    run = workspace.store.list('agent_run')[0]
    run['status'] = 'submitted'; workspace.store.put('agent_run', run)
    return workspace, campaign, agent, run


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


def test_migration_preserves_allocations_and_uses_subscription_models(prepared):
    w, c, a, r = prepared
    current = w.store.get(c['id'], 'campaign')
    for key in ('compute_budget_seconds','implementation_compute_budget_seconds','validation_reserve_seconds','llm_budget_usd'):
        assert current[key] == c[key]
    assert current['autonomy'] == 'delegated'
    assert a['model'] == 'gpt-6-astra'
    assert a['usage']['api_cost_usd'] == 0
    assert len(w.store.list('agent_migration')) == 1
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
    q = invoke(prepared,'researcher_ask',{'question':'Which objective?', 'reason':'Conflicting scientific specifications'},'question')
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
    assert not w.pi.owns(c['id']) and w.store.list('agent_migration')


def test_implementation_workers_cannot_read_protected_or_peer_evidence(prepared):
    w,c,a,_=prepared
    child=w.pi.create_agent(c['id'],'implementation_builder','Build','builder',parent_id=a['id'],grant_id='grant')
    names=PiTools(w.pi).names(child)
    assert 'evidence_read' not in names and 'campaign_command' not in names
    assert {'workspace_run','submit_output'} <= names


def test_private_gateway_requires_token(prepared,monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from optimization_framework.api.app import create_app
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
    from optimization_framework.research.discovery.proposals import hypothesis_revision, readiness
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
