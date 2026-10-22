"""The agents in charge of pages: each does its page's work for whoever asked it, the researcher on the page or the lead."""
from optimization_framework.agents.models import DISCUSSION_ROLES, NOTICE
from optimization_framework.agents.tools import ROLE_OPERATIONS, PiTools
from optimization_framework.contracts.requests import TrialInput

from test_pi_agents import Client, activated


def submitted(w, run):
    run = w.store.get(run['id'], 'agent_run')
    run['status'] = 'submitted'; w.store.put('agent_run', run)
    return run


def call(w, agent, run, name, args, call_id):
    return PiTools(w.pi).call(agent['id'], run['id'], call_id, name, args)


def finish(w, agent, run, text='Done.'):
    w.pi._receive(agent['id'], {'runs': {run['id']: {'status': 'completed', 'result': {'text': text}}}, 'events': [], 'cursor': 0})


def guidance(w, c):
    return w.memory.state(c['id'])['guidance_revision']


def wakes(w, agent, prefix):
    return [run for run in w.store.list('agent_run') if run['agent_id'] == agent['id'] and run['id'].startswith(prefix)]


def test_each_page_agent_sees_only_the_commands_of_its_page_and_the_moderator_asks_only_reviewers(tmp_path, monkeypatch):
    w, c, lead = activated(tmp_path, monkeypatch)
    tools = PiTools(w.pi)
    manifests = {}
    for role in ROLE_OPERATIONS:
        run = submitted(w, w.pi.message(c['id'], {'message': 'Wait for my next message.', 'page_agent': role}, 'ask_' + role))
        agent = w.store.get(run['agent_id'], 'agent_session')
        assert agent['parent_agent_id'] == lead['id'] and agent['role'] == role
        manifest = {tool['name']: tool for tool in tools.manifest(agent['id'], run['id'])['tools']}
        assert set(manifest['campaign_command']['input_schema']['properties']['operation']['enum']) == ROLE_OPERATIONS[role]
        assert not {'report_write', 'page_agent_message'} & set(manifest)
        manifests[role] = (agent, run, manifest)
    moderator, run, manifest = manifests['discussion_moderator']
    assert "reviewer's own word" in tools.manifest(moderator['id'], run['id'])['instructions']
    assert manifest['delegate']['input_schema']['properties']['role']['enum'] == list(DISCUSSION_ROLES)
    assert 'delegate' not in manifests['experiment_operator'][2] and 'implementation_workspace_create' in manifests['implementation_lead'][2]
    # A command outside its page is refused before anything runs, and so is asking another page agent or a builder.
    operator, operator_run, _ = manifests['experiment_operator']
    refused = call(w, operator, operator_run, 'campaign_command', {'operation': 'hypothesis.create', 'payload': {}, 'request_key': 'h',
        'guidance_revision': guidance(w, c)}, 'hypothesis')
    assert refused['executed'] is False and 'hypothesis.create' in refused['error']
    refused = call(w, moderator, run, 'campaign_command', {'operation': 'trial.create', 'payload': {}, 'request_key': 't',
        'guidance_revision': guidance(w, c)}, 'trial')
    assert refused['executed'] is False
    for role in ('experiment_operator', 'implementation_builder'):
        assert call(w, moderator, run, 'delegate', {'role': role, 'objective': 'x', 'request_key': role}, role)['executed'] is False
    # The lead assigns no page agent as a one-off assistant: it writes to the standing one.
    lead_run = submitted(w, w.pi.message(c['id'], {'message': 'Hand the review to the moderator.'}, 'lead_request'))
    lead_tools = {tool['name']: tool for tool in tools.manifest(lead['id'], lead_run['id'])['tools']}
    assert not set(ROLE_OPERATIONS) & set(lead_tools['delegate']['input_schema']['properties']['role']['enum'])
    assert 'page_agent_message' in lead_tools


def test_a_request_on_a_page_is_the_page_agents_own_and_does_not_reach_the_lead(tmp_path, monkeypatch):
    w, c, lead = activated(tmp_path, monkeypatch)
    first = w.pi.message(c['id'], {'message': 'Why did the last run fail?', 'page_agent': 'experiment_operator'}, 'first')
    operator = w.store.get(first['agent_id'], 'agent_session')
    second = w.pi.message(c['id'], {'message': 'And the one before?', 'page_agent': 'experiment_operator'}, 'second')
    assert second['agent_id'] == operator['id'] and len([a for a in w.store.list('agent_session') if a['role'] == 'experiment_operator']) == 1
    assert operator['request_open'] and operator['requested_by'] == 'researcher'
    assert not w.pi.configuration(c['id'])['request_open']   # the lead is not asked
    assert w.store.get('message_first', 'message')['agent_id'] == operator['id']
    run = submitted(w, first)
    call(w, operator, run, 'yield_work', {'disposition': 'complete', 'summary': 'It ran out of GPU memory.', 'report': 'Run 41 ran out of GPU memory at 8 workers.'}, 'done')
    finish(w, operator, run, 'It ran out of GPU memory.')
    w.pi._receive(operator['id'], {'runs': {second['id']: {'status': 'completed', 'result': {'text': 'Same cause.'}}}, 'events': [], 'cursor': 0})
    operator = w.store.get(operator['id'], 'agent_session')
    assert operator['status'] == 'waiting' and not wakes(w, lead, 'pi_child_result_')


def test_the_leads_request_to_a_page_agent_holds_the_lead_and_its_result_comes_back(tmp_path, monkeypatch):
    w, c, lead = activated(tmp_path, monkeypatch)
    lead_run = submitted(w, w.pi.message(c['id'], {'message': 'Have two proposals reviewed.'}, 'request'))
    sent = call(w, lead, lead_run, 'page_agent_message', {'page_agent': 'discussion_moderator', 'message': 'Run one review round on H1 and H2.'}, 'ask')
    assert sent['page'] == '#hypotheses'
    moderator = w.store.get(sent['agent_id'], 'agent_session')
    assert moderator['requested_by'] == lead['id'] and moderator['request_open']
    note = w.store.get('message_' + [r for r in w.store.list('agent_tool_receipt') if r['tool'] == 'page_agent_message'][0]['id'], 'message')
    assert (note['role'], note['agent_id'], note['content']) == ('lead', moderator['id'], 'Run one review round on H1 and H2.')
    held = call(w, lead, lead_run, 'yield_work', {'disposition': 'complete', 'summary': 'Done'}, 'too_early')
    assert 'Outstanding assignments' in held['error']
    # The moderator asks a reviewer; the reviewer's answer goes to the moderator, not to the lead.
    run = submitted(w, w.store.get(sent['run_id'], 'agent_run'))
    asked = call(w, moderator, run, 'delegate', {'role': 'proposal_reviewer', 'objective': 'Review H1.', 'request_key': 'h1'}, 'review')
    reviewer = w.store.get(asked['agent_id'], 'agent_session')
    assert reviewer['parent_agent_id'] == moderator['id']
    assert 'Outstanding assignments' in call(w, moderator, run, 'yield_work', {'disposition': 'complete', 'summary': 'x'}, 'early')['error']
    call(w, moderator, run, 'yield_work', {'disposition': 'waiting', 'summary': 'A reviewer is reading H1.'}, 'waiting')
    finish(w, moderator, run, 'A reviewer is reading H1.')
    finish(w, reviewer, submitted(w, w.store.get(asked['run_id'], 'agent_run')), 'H1: test.')
    woken = wakes(w, moderator, 'pi_child_result_')
    assert len(woken) == 1 and not wakes(w, lead, 'pi_child_result_')
    # When the moderator ends the request, its result comes back to the lead, which may now complete.
    run = submitted(w, woken[0])
    call(w, moderator, run, 'yield_work', {'disposition': 'complete', 'summary': 'H1 stands.', 'report': 'H1: test. H2 was revised.'}, 'done')
    finish(w, moderator, run, 'H1 stands; H2 was revised.')
    assert len(wakes(w, lead, 'pi_child_result_')) == 1 and wakes(w, lead, 'pi_child_result_')[0]['input'].startswith(NOTICE)
    assert not w.store.get(moderator['id'], 'agent_session')['request_open']
    assert call(w, lead, lead_run, 'yield_work', {'disposition': 'complete', 'summary': 'Reviewed', 'report': 'H1 stands.'}, 'complete') == {'saved': True, 'disposition': 'complete', 'summary': 'Reviewed', 'report': 'H1 stands.'}


def test_an_experiment_a_page_agent_started_wakes_that_agent_and_does_not_hold_the_lead(tmp_path, monkeypatch):
    w, c, lead = activated(tmp_path, monkeypatch)
    lead_run = submitted(w, w.pi.message(c['id'], {'message': 'Summarise the proposals.'}, 'request'))
    run = submitted(w, w.pi.message(c['id'], {'message': 'Start one short run.', 'page_agent': 'experiment_operator'}, 'run_one'))
    operator = w.store.get(run['agent_id'], 'agent_session')
    task = w.current_tasks(c['id'])[0]
    created = call(w, operator, run, 'campaign_command', {'operation': 'trial.create', 'guidance_revision': guidance(w, c), 'request_key': 'one',
        'payload': {'task_id': task['id'], 'algorithm': 'coordinate', 'max_steps': 2, 'wall_seconds': 5}}, 'start')
    trial_id = created['outcome']['trial_id']
    assert created['page'] == f'#experiments/{trial_id}' and w.pi.starter(c['id'], trial_id)['id'] == operator['id']
    # The run is the operator's: the lead, asked for something else, completes while it runs; the operator waits for it.
    assert 'Outstanding' in call(w, operator, run, 'yield_work', {'disposition': 'complete', 'summary': 'x'}, 'early')['error']
    call(w, operator, run, 'yield_work', {'disposition': 'waiting', 'summary': 'One run is going.'}, 'waiting')
    finish(w, operator, run, 'Started one run.')
    assert w.store.get(operator['id'], 'agent_session')['request_open']
    assert call(w, lead, lead_run, 'yield_work', {'disposition': 'complete', 'summary': 'Summarised', 'report': 'Two proposals.'}, 'lead_done')['saved']
    w.store.put('trial', {**w.store.get(trial_id, 'trial'), 'status': 'completed'}, 'trial.evidence_cataloged')
    w.pi._campaign_events(c['id'])
    woken = wakes(w, operator, 'pi_events_')
    assert len(woken) == 1 and trial_id in woken[0]['input'] and not wakes(w, lead, 'pi_events_')
    # The lead still hears of work no page agent started.
    w.pi._request(c['id'], True)
    other = w.create_trial(TrialInput(campaign_id=c['id'], task_id=task['id'], algorithm='coordinate', max_steps=2, wall_seconds=5))
    w.store.put('trial', {**w.store.get(other['id'], 'trial'), 'status': 'completed'}, 'trial.evidence_cataloged')
    w.pi._campaign_events(c['id'])
    assert len(wakes(w, lead, 'pi_events_')) == 1 and len(wakes(w, operator, 'pi_events_')) == 1


def test_page_agents_do_not_take_the_places_of_assistants_at_work(tmp_path, monkeypatch):
    w, c, lead = activated(tmp_path, monkeypatch)
    client = Client(); w.pi.client = client
    config = w.pi.configuration(c['id']); config['max_subagents'] = 1; w.store.put('agent_campaign', config)
    helper = w.pi.create_agent(c['id'], 'literature_specialist', 'Read two papers.', 'agent_helper', parent_id=lead['id'])
    first = w.pi.enqueue(helper, 'assignment_helper', 'Read two papers.')
    asked = w.pi.message(c['id'], {'message': 'What is running?', 'page_agent': 'experiment_operator'}, 'ask')
    w.pi._sync(c['id'])
    assert {first['id'], asked['id']} <= set(client.submissions)


def test_a_stopped_page_agent_takes_the_next_request(tmp_path, monkeypatch):
    w, c, lead = activated(tmp_path, monkeypatch)
    run = w.pi.message(c['id'], {'message': 'Check the evaluator.', 'page_agent': 'implementation_lead'}, 'first')
    agent = w.store.get(run['agent_id'], 'agent_session')
    w.pi.control(c['id'], {'agent_id': agent['id'], 'action': 'stop', 'expected_control_revision': agent['control_revision']}, 'stop')
    assert w.store.get(agent['id'], 'agent_session')['status'] == 'stopped'
    again = w.pi.message(c['id'], {'message': 'Try again.', 'page_agent': 'implementation_lead'}, 'again')
    assert w.store.get(agent['id'], 'agent_session')['status'] == 'queued' and again['status'] == 'queued'


def test_a_round_the_moderator_runs_is_shown_with_the_request_that_started_it(tmp_path, monkeypatch):
    from optimization_framework.agents.discussions import view
    w, c, lead = activated(tmp_path, monkeypatch)
    lead_run = submitted(w, w.pi.message(c['id'], {'message': 'Have H1 reviewed.'}, 'request'))
    sent = call(w, lead, lead_run, 'page_agent_message', {'page_agent': 'discussion_moderator', 'message': 'Review H1 with one reviewer.'}, 'ask')
    moderator = w.store.get(sent['agent_id'], 'agent_session')
    run = submitted(w, w.store.get(sent['run_id'], 'agent_run'))
    asked = call(w, moderator, run, 'delegate', {'role': 'proposal_reviewer', 'objective': 'Review H1.', 'request_key': 'h1'}, 'review')
    saved = call(w, moderator, run, 'artifact_save', {'title': 'Round plan', 'kind': 'analysis', 'content': 'One reviewer on H1.'}, 'plan')
    on_page = w.pi.message(c['id'], {'message': 'Add a skeptic.', 'page_agent': 'discussion_moderator'}, 'on_page')
    second = submitted(w, on_page)
    call(w, moderator, second, 'delegate', {'role': 'skeptical_domain_analyst', 'objective': 'Challenge H1.', 'request_key': 'skeptic'}, 'skeptic')
    rounds = view(w, c['id'])['rounds']
    assert [(r['number'], r['owner'], r['trigger']['kind']) for r in rounds] == [(2, 'discussion_moderator', 'request'), (1, 'discussion_moderator', 'lead')]
    first = rounds[1]
    assert first['trigger']['text'] == 'Review H1 with one reviewer.' and [a['agent_id'] for a in first['assistants']] == [asked['agent_id']]
    assert [doc['id'] for doc in first['owner_documents']] == [saved['artifact_id']]
    assert rounds[0]['trigger']['text'] == 'Add a skeptic.'


def test_the_researcher_can_set_a_question_aside_without_starting_a_turn(tmp_path, monkeypatch):
    from optimization_framework.contracts.commands import Command
    w, c, lead = activated(tmp_path, monkeypatch)
    lead_run = submitted(w, w.pi.message(c['id'], {'message': 'Decide the gate path.'}, 'request'))
    asked = call(w, lead, lead_run, 'researcher_ask', {'question': 'Which path?', 'reason': 'The gate refuses long runs.', 'suggestion': 'Approve the runtime profile first.'}, 'ask')
    assert 'Unresolved questions' in call(w, lead, lead_run, 'yield_work', {'disposition': 'complete', 'summary': 'x'}, 'early')['error']
    runs = len(w.store.list('agent_run'))
    w.commands.execute(Command(id='dismiss', campaign_id=c['id'], expected_revision=w.store.get(c['id'], 'campaign')['version'],
        operation='agent.question_dismiss', payload={'question_id': asked['id']}))
    assert w.store.get(asked['id'], 'agent_question')['status'] == 'dismissed' and len(w.store.list('agent_run')) == runs
    assert not w.pi.view(c['id'])['questions']
    assert call(w, lead, lead_run, 'yield_work', {'disposition': 'complete', 'summary': 'Set aside', 'report': 'The question was set aside.'}, 'done')['saved']


def test_a_request_is_not_called_complete_without_its_reply_and_the_reply_is_the_message(tmp_path, monkeypatch):
    w, c, lead = activated(tmp_path, monkeypatch)
    run = submitted(w, w.pi.message(c['id'], {'message': 'Run a tiny screen and tell me how it went.', 'page_agent': 'experiment_operator'}, 'op'))
    operator = w.store.get(run['agent_id'], 'agent_session')
    assert call(w, operator, run, 'yield_work', {'disposition': 'waiting', 'summary': 'Runs going.'}, 'wait')['saved']   # waiting needs no reply
    refused = call(w, operator, run, 'yield_work', {'disposition': 'complete', 'summary': 'Screen done.'}, 'bare')
    assert refused['executed'] is False and 'report' in refused['error']
    call(w, operator, run, 'yield_work', {'disposition': 'complete', 'summary': 'Screen done.',
        'report': 'Random reached 0.028%, coordinate search 0.0017% after 30 evaluations each.'}, 'done')
    w.pi._receive(operator['id'], {'runs': {run['id']: {'status': 'completed', 'result': {'text': 'Both finished. Now I can report.',
        'parts': ['Starting both runs.', 'Both finished. Now I can report.']}}}, 'events': [], 'cursor': 0})
    message = w.store.get('message_' + run['id'], 'message')
    assert message['content'] == 'Random reached 0.028%, coordinate search 0.0017% after 30 evaluations each.'
    assert message['parts'] == ['Starting both runs.', 'Both finished. Now I can report.', message['content']]
