"""Installed Fourier level-set methods and their explicit execution parameters (data only; the solvers live in dqn_meent)."""

PROBLEM_ID = 'meent_2d_dual_polarization_deflector'
NAMES = {
    'flrl_lsf_random': 'Fourier level-set random search',
    'flrl_lsf_es': 'Fourier level-set Gaussian evolution strategy',
    'flrl_ppo': 'FLRL PPO campaign adaptation',
    'flrl_autograd_adam': 'FLRL autograd Adam campaign adaptation',
    'flrl_ppo_polish': 'PPO with two-step Adam polishing',
    'flrl_ppo_residual': 'PPO residual control of bounded Adam',
}
INTEGER = {'type': 'integer', 'minimum': 1}
POSITIVE = {'type': 'number', 'exclusiveMinimum': 0}
COMMON = {
    'level_set_modes_x': {**INTEGER, 'maximum': 16, 'default': 8},
    'level_set_modes_y': {'type': 'integer', 'minimum': 0, 'maximum': 8, 'default': 4},
    'comparison_cost_axis': {'enum': ['full_worker_seconds']},
}
LEARNER = {
    'episode_length': {**INTEGER, 'maximum': 4096}, 'rollout_steps': {**INTEGER, 'minimum': 2, 'maximum': 4096},
    'batch_size': {**INTEGER, 'minimum': 2, 'maximum': 4096},
    'reward_scaling': POSITIVE, 'ppo_learning_rate': POSITIVE,
    'ppo_gamma': {'type': 'number', 'minimum': 0, 'maximum': 1},
    'ppo_gae_lambda': {'type': 'number', 'minimum': 0, 'maximum': 1},
    'ppo_clip_range': {**POSITIVE, 'maximum': 1}, 'ppo_n_epochs': {**INTEGER, 'maximum': 20},
}
PROPERTIES = {
    'flrl_lsf_random': {**COMMON, 'sampling': {'enum': ['seeded_gaussian_normalized']}},
    'flrl_lsf_es': {**COMMON, 'population_size': {**INTEGER, 'minimum': 2, 'maximum': 256},
        'elite_count': {**INTEGER, 'minimum': 2}, 'sigma': POSITIVE,
        'coefficient_dimensions': INTEGER, 'search_family': {'enum': ['evolution_strategy']}},
    'flrl_ppo': {**COMMON, **LEARNER, 'action_scaling': POSITIVE,
        'stacked_observations': {**INTEGER, 'maximum': 16}, 'parallel_environments': {'enum': [1]},
        'rcwa_order_x': INTEGER, 'rcwa_order_y': INTEGER, 'reference_total_timesteps': INTEGER},
    'flrl_autograd_adam': {**COMMON, 'optimizer': {'enum': ['Adam']}, 'reference_epochs': INTEGER,
        'reference_samples': INTEGER, 'initial_lr': POSITIVE, 'final_lr': POSITIVE,
        'initial_beta': {**POSITIVE, 'maximum': 100}, 'final_beta': {**POSITIVE, 'maximum': 100}},
    'flrl_ppo_polish': {**COMMON, **LEARNER, 'action_radius': POSITIVE,
        'level_set_mode_pairs': {'enum': [[[2, 1], [4, 2], [6, 3], [8, 4]]],
            'description': 'Planned comparison grid; level_set_modes_x/y select one fixed basis for this run.'},
        **{name: {'type': 'string'} for name in ['selection', 'reward', 'policy_action_likelihood', 'local_kernel_stationarity']}},
    'flrl_ppo_residual': {**COMMON, **LEARNER, 'gradient_radius': {'type': 'number', 'minimum': 0},
        'residual_radius': {'type': 'number', 'minimum': 0}, 'adam_learning_rate': POSITIVE,
        'tangent_dimensions': {**INTEGER, 'minimum': 2, 'maximum': 32}, 'restart_patience': INTEGER,
        'relaxation_stage_steps': INTEGER, 'initial_beta': {**POSITIVE, 'maximum': 100},
        'final_beta': {**POSITIVE, 'maximum': 100},
        **{name: {'type': 'string'} for name in ['search_representation', 'relaxed_update', 'policy_action',
            'policy_training', 'gradient_separation', 'archive']}},
}
METHODS = [{'id': key, 'name': value, 'description': 'Fourier coefficients map to y-symmetric binary 2D gratings. '
    'Physical scores and optional autograd requests use the campaign MEENT evaluator; all solver calls are counted.',
    'problem_ids': [PROBLEM_ID], 'representations': ['binary'], 'constraints': False, 'parameters': {}}
    for key, value in NAMES.items()]


def validate(name, instance, parameters):
    import math
    if any(type(value) in (int, float) and not math.isfinite(value) for value in parameters.values()):
        raise ValueError('Fourier parameters must be finite')
    nx, ny = parameters.get('level_set_modes_x', 8), parameters.get('level_set_modes_y', 4)
    dimensions = (2 * nx + 1) * (ny + 1)
    if dimensions * instance.candidate_schema.dimensions > 16_000_000:
        raise ValueError('Fourier basis exceeds the supported memory bound')
    if parameters.get('coefficient_dimensions', dimensions) != dimensions:
        raise ValueError('Coefficient dimensions disagree with Fourier modes')
    if parameters.get('elite_count', 4) > parameters.get('population_size', 16):
        raise ValueError('ES elite count must not exceed population size')
    if 'tangent_dimensions' in parameters and parameters['tangent_dimensions'] >= dimensions:
        raise ValueError('Tangent frame needs fewer axes than coefficient dimensions')
    for axis in ('x', 'y'):
        key = 'rcwa_order_' + axis
        if key in parameters and parameters[key] != instance.fidelity[key]:
            raise ValueError('Optimizer RCWA settings disagree with the frozen evaluator fidelity')
