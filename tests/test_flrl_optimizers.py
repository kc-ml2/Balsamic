"""Physical gradients, mechanism invariants, and real worker continuation."""
from copy import deepcopy

import numpy as np
import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('meent')

from dqn_meent.fourier import FourierGeometry
from dqn_meent.flrl_optimizers import tangent_frame
from optimization_framework.optimizers.fourier_specs import NAMES
from dqn_meent.problem_2d import Meent2DProblem
from optimization_framework.contracts.problems import Observation
from optimization_framework.optimizers.registry import create, capability_reason
from optimization_framework.execution.worker import ExperimentWorker, read_journal, run
from optimization_framework.storage.artifacts import atomic_json
from test_meent_2d_problem import configuration


@pytest.fixture(autouse=True)
def one_thread():
    old = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(old)


def problem():
    return Meent2DProblem().resolve(configuration(), {'rcwa_order_x': 1, 'rcwa_order_y': 1})


def parameters(name):
    values = {'level_set_modes_x': 2, 'level_set_modes_y': 1}
    if name in {'flrl_ppo', 'flrl_ppo_polish', 'flrl_ppo_residual'}:
        values.update(episode_length=2, rollout_steps=2)
    return values


def step(optimizer, evaluator, instance):
    proposal = optimizer.propose()[0]
    measured = evaluator.evaluate_proposal(proposal.candidate, proposal.metadata)
    observation = Observation(id='observed', experiment_id='test', attempt_id='attempt', request_id='request',
        proposal_id=proposal.id, candidate=proposal.candidate, status='ok', objectives=measured.objectives,
        metadata=measured.metadata, evaluator_identity=instance.evaluation_identity)
    optimizer.observe([observation])
    return proposal, measured


def test_basis_matches_analytic_fourier_modes_and_symmetry():
    geometry = FourierGeometry(2, 1, 16, 8)
    x, y = np.meshgrid(np.linspace(0, 2*np.pi, 16), np.linspace(0, 2*np.pi, 8))
    # Independent closed-form modes in the authors' real-then-imaginary order.
    expected = [4*np.cos(2*x)*np.cos(y), 2*np.cos(2*x), 4*np.cos(x)*np.cos(y),
        2*np.cos(x), 2*np.cos(y), np.ones_like(x), 4*np.sin(2*x)*np.cos(y),
        2*np.sin(2*x), 4*np.sin(x)*np.cos(y), 2*np.sin(x)]
    for coefficient, field in zip(np.eye(10), expected):
        np.testing.assert_allclose(geometry.field(coefficient).reshape(8, 16), field, atol=1e-13)
    c = np.random.default_rng(9).normal(size=10)
    np.testing.assert_allclose(geometry.field(c).reshape(8,16), geometry.field(c).reshape(8,16)[::-1], atol=1e-13)
    assert geometry.mask(c) == geometry.mask(3*c)
    assert geometry.mask(np.zeros(10)) == [1]*128
    assert np.mean(geometry.field(geometry.normalize(c, 'rms'))**2) == pytest.approx(1.)
    assert FourierGeometry(8, 4, 256, 128).dimensions == 85


@pytest.mark.parametrize('material_map', ['index', 'permittivity'])
def test_meent_autograd_agrees_with_centered_finite_difference(material_map):
    instance = problem(); evaluator = Meent2DProblem().evaluator(instance)
    geometry = FourierGeometry(2, 1, 16, 8)
    c = geometry.normalize(np.random.default_rng(18).normal(size=10))
    request = geometry.gradient_request(c, 5., material_map)
    response = evaluator.relaxed_gradient(request)
    direction = np.random.default_rng(35).normal(size=10)
    direction /= np.linalg.norm(direction)
    delta = 1e-5
    plus = evaluator.relaxed_gradient({**request, 'coefficients': (c + delta*direction).tolist()})['mean']
    minus = evaluator.relaxed_gradient({**request, 'coefficients': (c - delta*direction).tolist()})['mean']
    assert np.dot(response['gradient'], direction) == pytest.approx((plus-minus)/(2*delta), rel=2e-3, abs=2e-5)


def test_cache_preserves_hard_scores_and_counts_gradient_work_separately():
    instance = problem(); evaluator = Meent2DProblem().evaluator(instance)
    geometry = FourierGeometry(2, 1, 16, 8)
    c = geometry.normalize(np.random.default_rng(42).normal(size=10))
    mask = geometry.mask(c)
    first = evaluator.evaluate_proposal(mask, {})
    second = evaluator.evaluate_proposal(mask, {'flrl_gradient': geometry.gradient_request(c, 5)})
    third = evaluator.evaluate_proposal(mask, {})
    assert first.objectives == second.objectives == third.objectives
    assert first.solver_executions == second.solver_executions == 2
    assert not first.cache_hit and second.cache_hit and third.cache_hit
    assert third.solver_executions == 0 and 'flrl_gradient' not in third.metadata
    with pytest.raises(ValueError, match='do not produce'):
        evaluator.evaluate_proposal(mask, {'flrl_gradient': geometry.gradient_request(-c, 5)})
    restored = Meent2DProblem().evaluator(instance); restored.restore(evaluator.checkpoint())
    assert restored.evaluate_proposal(mask, {}).solver_executions == 0


@pytest.mark.parametrize('name', list(NAMES))
def test_real_meent_steps_and_exact_checkpoint_continuation(name):
    instance = problem(); evaluator = Meent2DProblem().evaluator(instance)
    optimizer = create(name, instance, parameters(name), seed=42, schedule_steps=40)
    for _ in range(3): step(optimizer, evaluator, instance)
    checkpoint = optimizer.checkpoint(); evaluator_state = evaluator.checkpoint()
    expected = []
    for _ in range(7):
        proposal, measured = step(optimizer, evaluator, instance)
        expected.append((proposal.model_dump(), measured.objectives, optimizer.inspect()))
    resumed = create(name, instance, parameters(name), seed=42, schedule_steps=40)
    resumed.restore(checkpoint)
    evaluator.restore(evaluator_state)
    for proposal, objectives, diagnostics in expected:
        actual, measured = step(resumed, evaluator, instance)
        assert actual.model_dump() == proposal
        assert measured.objectives == objectives
        assert resumed.inspect() == diagnostics
    assert optimizer.best_score >= 0
    if optimizer.ppo:
        assert optimizer.ppo.updates > 0
        assert optimizer.ppo.last_loss != 0
    if name in {'flrl_autograd_adam', 'flrl_ppo_polish', 'flrl_ppo_residual'}:
        assert optimizer.gradient_calls > 0


def test_tangent_frame_and_zero_gradient_ablation():
    instance = problem()
    config = {**parameters('flrl_ppo_residual'), 'gradient_radius': 0.}
    optimizer = create('flrl_ppo_residual', instance, config, seed=42, schedule_steps=20)
    evaluator = Meent2DProblem().evaluator(instance)
    for _ in range(4):
        proposal, measured = step(optimizer, evaluator, instance)
        assert 'flrl_gradient' not in proposal.metadata
        assert 'flrl_gradient' not in measured.metadata
    assert optimizer.gradient_calls == 0
    c = optimizer.geometry.normalize(np.random.default_rng(1).normal(size=10))
    frame = tangent_frame(c, np.zeros(10), c, 4)
    np.testing.assert_allclose(frame.T @ frame, np.eye(4), atol=1e-12)
    np.testing.assert_allclose(frame.T @ c, np.zeros(4), atol=1e-12)


def test_residual_cached_return_keeps_signed_reward_and_best_archive():
    instance = problem()
    optimizer = create('flrl_ppo_residual', instance, {**parameters('flrl_ppo_residual'),
        'gradient_radius': 0., 'episode_length': 20}, seed=42, schedule_steps=20)
    evaluator = Meent2DProblem().evaluator(instance)
    step(optimizer, evaluator, instance)
    a, a_score = optimizer.c.copy(), optimizer.current_score
    b = optimizer.geometry.normalize(np.random.default_rng(99).normal(size=10))
    optimizer.next_c = b
    _, b_result = step(optimizer, evaluator, instance)
    reward_ab = optimizer.last_reward
    best = optimizer.best_score
    optimizer.next_c = a
    _, returned = step(optimizer, evaluator, instance)
    assert returned.cache_hit and returned.solver_executions == 0
    assert optimizer.last_reward == pytest.approx(-reward_ab)
    assert optimizer.last_reward == pytest.approx(100*(a_score-b_result.objectives['mean_plus1_transmission']))
    assert optimizer.best_score == best
    optimizer.next_c = a.copy()
    step(optimizer, evaluator, instance)
    assert optimizer.last_reward == 0


def test_polish_resets_moments_and_rolls_back_without_improvement():
    instance = problem()
    optimizer = create('flrl_ppo_polish', instance, parameters('flrl_ppo_polish'), seed=42, schedule_steps=20)
    def constant_observation():
        proposal = optimizer.propose()[0]
        metrics = dict(mean_plus1_transmission=.3, te_plus1_transmission=.4, tm_plus1_transmission=.2)
        metadata = {'flrl_gradient': {'gradient': [0.]*optimizer.d}} if 'flrl_gradient' in proposal.metadata else {}
        optimizer.observe([Observation(id='o', experiment_id='t', attempt_id='a', request_id='r',
            proposal_id=proposal.id, candidate=proposal.candidate, status='ok', objectives=metrics,
            metadata=metadata, evaluator_identity=instance.evaluation_identity)])
    constant_observation()
    original = optimizer.c.copy()
    for _ in range(3): constant_observation()
    np.testing.assert_array_equal(optimizer.c, original)
    assert optimizer.rollback_count == 1 and optimizer.last_reward == 0 and optimizer.adam_step == 2
    proposal = optimizer.propose()[0]
    assert optimizer.adam_step == 0 and not optimizer.m.any() and not optimizer.v.any()
    assert proposal.metadata['flrl_gradient']['beta'] == 5


@pytest.mark.parametrize('name', list(NAMES))
def test_worker_accounts_physical_calls_and_only_archives_binary_scores(tmp_path, name):
    instance = problem()
    spec = {'id': 'test_flrl', 'campaign_id': 'test', 'study_id': 'study', 'seed': 42, 'algorithm': name,
        'algorithm_config': parameters(name), 'problem': instance.model_dump(mode='json'),
        'max_steps': 8, 'wall_seconds': 30, 'schedule_steps': 20, 'recovery': {'every_observations': 2}}
    atomic_json(tmp_path / 'spec.json', spec)
    result = run(tmp_path)
    assert result['status'] == 'completed', result.get('reason')
    records = read_journal(tmp_path / 'observations.jsonl')
    requests = read_journal(tmp_path / 'requests.jsonl')
    assert result['solver_calls'] == sum(r['costs']['solver_executions'] for r in records)
    for record, request in zip(records, requests):
        assert set(record['candidate']) <= {0, 1}
        gradient = 'flrl_gradient' in request['proposal_metadata']
        assert gradient == ('flrl_gradient' in record['metadata'])
        assert record['costs']['solver_executions'] == (0 if record['costs']['cache_hits'] else 2) + 2*gradient
    assert result['best_objective'] == max(r['objectives']['mean_plus1_transmission'] for r in records)
    restored = ExperimentWorker(tmp_path)
    assert restored.optimizer.inspect()['decisions'] == result['diagnostics']['decisions']


def test_methods_reject_other_problems_and_conflicting_fidelity():
    from optimization_framework.evaluation.registry import problems
    other = problems.resolve('bounded_continuous', {'dimensions': 2})
    assert '2D MEENT' in capability_reason('flrl_ppo', other)
    with pytest.raises(ValueError, match='fidelity'):
        create('flrl_ppo', problem(), {**parameters('flrl_ppo'), 'rcwa_order_x': 10}, seed=42, schedule_steps=20)
