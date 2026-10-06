import { test, expect } from '@playwright/test';

test('diagnostic controls freeze a registered policy adapter and execute independent child evidence', async ({ page, request }, testInfo) => {
  const base = process.env.OPTIMIZATION_TEST_URL;
  test.skip(!base, 'Set OPTIMIZATION_TEST_URL to an isolated workspace with model calls disabled.');
  test.setTimeout(90000);
  const campaign = await (await request.post(`${base}/api/campaigns`, { data: {
    name: `Registered diagnostic ${Date.now()}`, compute_budget_seconds: 120, validation_reserve_seconds: 0,
    tasks: [{ name: 'Small grating', problem_id: 'meent_grating', configuration: { n_cells: 4, fourier_order: 1 } }],
  } })).json();
  await page.goto(`${base}/#drafts`);
  await page.getByLabel('Active campaign').selectOption(campaign.id);
  await page.getByRole('button', { name: 'Design an experiment', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Optimization strategy').selectOption('dqn');
  await dialog.getByLabel('Algorithm parameters (JSON)').fill(JSON.stringify({ horizon: 3, hidden_sizes: [4, 4],
    learning_starts: 5, buffer_size: 8, batch_size: 2 }));
  await dialog.getByLabel('Evaluation requests', { exact: true }).fill('3');
  await dialog.getByLabel('Time cap (seconds)', { exact: true }).fill('10');
  await dialog.getByLabel('Search schedule horizon', { exact: true }).fill('2');
  await dialog.getByLabel('Scientific completion', { exact: true }).selectOption('optimizer_decisions');
  await dialog.getByLabel('Required optimizer decisions', { exact: true }).fill('2');
  await dialog.getByText('Recovery and diagnostic schedules', { exact: true }).click();
  await dialog.getByLabel('Checkpoint every observations', { exact: true }).fill('2');
  await dialog.getByRole('button', { name: 'Add diagnostic schedule', exact: true }).click();
  await dialog.getByLabel('Milestone counter', { exact: true }).selectOption('optimizer_decisions');
  await dialog.getByLabel('Milestone counts', { exact: true }).fill('1, 2');
  await dialog.getByRole('button', { name: 'Add artifact inference', exact: true }).click();
  await expect(dialog.getByLabel('Inference adapter', { exact: true })).toHaveValue('dqn_policy:v1');
  await dialog.getByLabel('Episode decisions', { exact: true }).fill('1');
  await dialog.getByLabel('Inference time cap (seconds)', { exact: true }).fill('5');
  await dialog.getByLabel('Inference seed rule', { exact: true }).selectOption('affine');
  await dialog.getByLabel('Seed offset', { exact: true }).fill('100');
  await dialog.getByLabel('Milestone multiplier', { exact: true }).fill('2');
  const inference = dialog.getByRole('group', { name: 'Artifact inference 1', exact: true });
  await inference.getByRole('button', { name: 'Add validation', exact: true }).click();
  await inference.getByLabel('Validation recipe', { exact: true }).selectOption('reevaluate:v1');
  await inference.getByLabel('Fourier orders', { exact: true }).fill('2');
  await inference.getByLabel('Validation time cap (seconds)', { exact: true }).fill('5');
  await expect(dialog.getByText('Reserved diagnostic time: 20 seconds, in addition to the parent experiment.')).toBeVisible();
  await dialog.getByRole('button', { name: 'Save draft', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Freeze and launch', exact: true })).toBeEnabled();
  await inference.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('registered-diagnostic-form.png'), fullPage: true });
  await dialog.getByRole('button', { name: 'Freeze and launch', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  let final: any;
  await expect.poll(async () => {
    final = await (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
    return final.trials.filter((trial: any) => trial.status === 'completed').length;
  }, { timeout: 40000 }).toBe(5);
  const parent = final.trials.find((trial: any) => !trial.parent_trial_id);
  const children = final.trials.filter((trial: any) => trial.parent_trial_id === parent.id);
  expect(parent.result.scientific_complete).toBe(true);
  expect(parent.diagnostics[0].rollouts[0].adapter_id).toBe('dqn_policy:v1');
  expect(children.map((trial: any) => trial.seed).sort()).toEqual([102, 104]);
  for (const child of children) {
    expect(child.algorithm).toBe('artifact_inference');
    expect(child.inference_adapter.adaptation).toBe('forbidden');
    expect(child.source_hash).toBe(parent.source_hash);
    expect(child.result.evaluations).toBe(2);
    expect(child.result.diagnostics.updates).toBe(0);
  }
  const compared = await (await request.get(`${base}/api/v1/campaigns/${campaign.id}/comparison`)).json();
  expect(compared.groups.flatMap((group: any) => group.trials.map((trial: any) => trial.id))).toEqual([parent.id]);
});

test('production references import and freeze their exact versions without activating research', async ({ page, request }, testInfo) => {
  const base = process.env.OPTIMIZATION_TEST_URL;
  test.skip(!base, 'Set OPTIMIZATION_TEST_URL to an isolated workspace with model calls disabled.');
  test.setTimeout(90000);
  const catalog = await (await request.get(`${base}/api/v1/study-templates`)).json();
  const production = catalog.templates.find((entry: any) => entry.template.id === 'meent-production-replication:v1');
  const manifest = production.reference_sets[0].manifest_preview;
  const campaign = await (await request.post(`${base}/api/campaigns`, { data: {
    name: `Production declaration ${Date.now()}`, compute_budget_seconds: 691200, validation_reserve_seconds: 0,
    tasks: [{ name: 'Declared replication condition', problem_id: 'meent_grating', configuration: manifest.configuration }],
  } })).json();
  await page.goto(`${base}/#studies`);
  await page.getByLabel('Active campaign').selectOption(campaign.id);
  await page.getByRole('button', { name: 'Use a study template', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Study template', { exact: true }).selectOption(production.template.id);
  await expect(dialog.getByRole('button', { name: 'Freeze study template', exact: true })).toBeDisabled();
  await expect(dialog.getByText('Choose an available asset for every required input before freezing this study.')).toBeVisible();
  await dialog.getByRole('button', { name: 'Import preserved references', exact: true }).click();
  const inputs: Record<string, string> = {};
  for (const solution of manifest.solutions) {
    const input = dialog.getByLabel(`Input: ${solution.title}`, { exact: true });
    await expect(input).not.toHaveValue('');
    inputs[solution.slot] = await input.inputValue();
  }
  await expect(dialog.getByRole('button', { name: 'Freeze study template', exact: true })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('production-reference-selection.png'), fullPage: true });
  await dialog.getByRole('button', { name: 'Freeze study template', exact: true }).click();
  await expect(dialog).not.toBeVisible({ timeout: 60000 });
  const state = await (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
  expect(state.trials).toHaveLength(0);
  expect(state.budget.allocated_seconds).toBe(0);
  expect(state.study_executions[0].status).toBe('frozen');
  const frozen = await (await request.get(`${base}/api/v1/study-executions/${state.study_executions[0].id}`)).json();
  expect(frozen.cells).toHaveLength(60);
  for (const [slot, assetId] of Object.entries(inputs)) {
    expect(frozen.input_bindings[slot].asset_id).toBe(assetId);
    const asset = await (await request.get(`${base}/api/v1/assets/${assetId}`)).json();
    expect(frozen.input_bindings[slot].asset_digest).toBe(asset.asset.content_hash);
    expect(asset.full_attributed_cost.quantities.worker_seconds.total).toBeNull();
  }
});

test('a declared reference input executes with required validation and stays separate from selection', async ({ page, request }, testInfo) => {
  const base = process.env.OPTIMIZATION_TEST_URL;
  test.skip(!base, 'Set OPTIMIZATION_TEST_URL to an isolated workspace with model calls disabled.');
  test.setTimeout(120000);
  const campaign = await (await request.post(`${base}/api/campaigns`, { data: {
    name: `Reference workflow ${Date.now()}`, compute_budget_seconds: 120, validation_reserve_seconds: 0,
    tasks: [{ name: 'Reference quadratic', problem_id: 'bounded_continuous' }],
  } })).json();
  const read = async () => (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
  const initial = await read();
  const parent = await (await request.post(`${base}/api/trials`, { data: {
    campaign_id: campaign.id, task_id: initial.tasks[0].id, algorithm: 'random', max_steps: 1, wall_seconds: 5,
  } })).json();
  let source: any;
  await expect.poll(async () => {
    const assets = await (await request.get(`${base}/api/v1/assets?campaign_id=${campaign.id}`)).json();
    source = assets.find((asset: any) => asset.kind === 'solution' && asset.producer_id === parent.id);
    return !!source;
  }, { timeout: 20000 }).toBe(true);
  const sourceDetail = await (await request.get(`${base}/api/v1/assets/${source.id}`)).json();
  // Only the short declarative catalog entry is a test fixture. Commands,
  // input validation, actual workers, checks, selection and reports are real.
  await page.route('**/api/v1/study-templates*', async route => {
    const response = await route.fetch();
    const body = await response.json();
    const entry = structuredClone(body.templates.find((entry: any) => entry.template.id === 'continuous-workflow-qualification:v1'));
    entry.template.id = 'browser-reference-qualification:v1';
    entry.template.name = 'Reference input workflow qualification';
    entry.template.methods.reference = { procedure: { algorithm: 'evaluate_asset', max_steps: 1, wall_seconds: 5,
      input_binding: { kind: 'declared_asset:v1', slot: 'reference' } } };
    entry.template.input_requirements = { reference: { title: 'Prior quadratic solution', asset_kind: 'solution' } };
    entry.template.groups.unshift({ id: 'references', scope: 'reference', reference_role: 'historical_control', slots: ['reference'], seeds: [0], priority: 100 });
    entry.template.validation_policies = { ...entry.template.validation_policies,
      reference: { required_recipes: ['analytic_fixtures:v1'], validation_wall_seconds: 5 } };
    entry.input_candidates = { reference: [source] };
    entry.reference_sets = [];
    body.templates.push(entry);
    await route.fulfill({ response, json: body });
  });
  await page.goto(`${base}/#studies`);
  await page.getByLabel('Active campaign').selectOption(campaign.id);
  await page.getByRole('button', { name: 'Use a study template', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Study template', { exact: true }).selectOption('browser-reference-qualification:v1');
  await expect(dialog.getByRole('button', { name: 'Freeze study template', exact: true })).toBeDisabled();
  await dialog.getByLabel('Input: Prior quadratic solution', { exact: true }).selectOption(source.id);
  await dialog.getByRole('button', { name: 'Freeze study template', exact: true }).click();
  await expect(dialog).not.toBeVisible({ timeout: 20000 });
  await page.getByRole('button', { name: 'Activate study execution', exact: true }).click();
  await expect.poll(async () => (await read()).study_executions[0].status, { timeout: 90000 }).toBe('complete');
  await page.getByRole('button', { name: 'Release completed evidence', exact: true }).click();
  await expect(page.getByText(/Released as completed roster/)).toBeVisible({ timeout: 20000 });
  const state = await read();
  const execution = await (await request.get(`${base}/api/v1/study-executions/${state.study_executions[0].id}`)).json();
  const assessmentResponse = await request.get(`${base}/api/v1/confirmations/${execution.execution.protocol_id}`);
  expect(assessmentResponse.ok()).toBe(true);
  const assessment = await assessmentResponse.json();
  const evidence = assessment.report.evidence.analysis_evidence;
  expect(evidence.references).toHaveLength(1);
  expect(evidence.references[0].reference_role).toBe('historical_control');
  expect(evidence.references[0].result.evaluations).toBe(1);
  expect(evidence.references[0].result.best_candidate).toEqual(sourceDetail.asset.payload.candidate);
  expect(evidence.references[0].validation[0].measured_pass).toBe(true);
  expect(evidence.nomination.evidence.experiments.some((row: any) => row.trial_id === evidence.references[0].trial_id)).toBe(false);
  await page.screenshot({ path: testInfo.outputPath('reference-input-report.png'), fullPage: true });
});

for (const domain of ['bounded_continuous', 'meent_grating']) {
  test(`frozen study template executes and releases ${domain} qualification`, async ({ page, request }, testInfo) => {
    const base = process.env.OPTIMIZATION_TEST_URL;
    test.skip(!base, 'Set OPTIMIZATION_TEST_URL to an isolated workspace with model calls disabled.');
    test.setTimeout(120000);
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const campaign = await (await request.post(`${base}/api/campaigns`, { data: {
      name: `Staged ${domain} ${Date.now()}`, compute_budget_seconds: 60, validation_reserve_seconds: 0,
      tasks: [{ name: 'Template problem', problem_id: domain, configuration: domain === 'meent_grating' ? { n_cells: 4, fourier_order: 1 } : {} }],
    } })).json();
    await page.goto(`${base}/#studies`);
    await page.getByLabel('Active campaign').selectOption(campaign.id);
    await page.getByRole('button', { name: 'Use a study template', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Study template', { exact: true }).selectOption(domain === 'meent_grating' ? 'meent-workflow-qualification:v1' : 'continuous-workflow-qualification:v1');
    await dialog.getByRole('button', { name: 'Freeze study template', exact: true }).click();
    await expect(dialog).not.toBeVisible({ timeout: 20000 });
    const read = async () => (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
    const frozen = await read();
    expect(frozen.trials).toHaveLength(0);
    expect(frozen.budget.allocated_seconds).toBe(0);
    expect(frozen.study_executions[0].status).toBe('frozen');
    await page.getByRole('button', { name: 'Activate study execution', exact: true }).click();
    await expect(page.getByText(/Overall deadline:/)).toBeVisible();
    await expect.poll(async () => (await read()).study_executions[0].status, { timeout: 90000 }).toBe('complete');
    const final = await read();
    const detail = await (await request.get(`${base}/api/v1/study-executions/${final.study_executions[0].id}`)).json();
    expect(detail.cells.filter((cell: any) => cell.canonical_cell_id !== cell.id)).toHaveLength(2);
    expect(final.trials.filter((trial: any) => !trial.parent_trial_id)).toHaveLength(domain === 'meent_grating' ? 8 : 6);
    // The view runs captured evidence analysis after the roster becomes complete.
    await expect(page.getByRole('button', { name: 'Release completed evidence', exact: true })).toBeEnabled({ timeout: 20000 });
    await page.getByRole('button', { name: 'Release completed evidence', exact: true }).click();
    await expect(page.getByText(/Released as completed roster/)).toBeVisible({ timeout: 20000 });
    await page.screenshot({ path: testInfo.outputPath(`${domain}-staged-study.png`), fullPage: true });
    expect(errors).toEqual([]);
  });
}

test('a missing implementation remains designable and its revised draft launches once', async ({ page, request }, testInfo) => {
  const base = process.env.OPTIMIZATION_TEST_URL;
  test.skip(!base, 'Set OPTIMIZATION_TEST_URL to an isolated workspace with model calls disabled.');
  const campaign = await (await request.post(`${base}/api/campaigns`, { data: {
    name: `Draft workflow ${Date.now()}`, compute_budget_seconds: 60, validation_reserve_seconds: 0,
    tasks: [{ name: 'Draft quadratic', problem_id: 'bounded_continuous' }],
  } })).json();
  const hypothesis = await (await request.post(`${base}/api/hypotheses`, { data: {
    campaign_id: campaign.id, title: `Missing-code proposal ${Date.now()}`, mechanism: 'A proposed specialized optimizer',
    rationale: 'Keep the design while implementation is unresolved.', algorithm: '',
  } })).json();
  await page.goto(`${base}/#hypotheses`);
  await page.getByLabel('Active campaign').selectOption(campaign.id);
  await page.getByRole('button', { name: new RegExp(hypothesis.title) }).click();
  let dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: 'Design experiment', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Design experiment', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByLabel('Evaluation requests', { exact: true }).fill('4');
  await dialog.getByLabel('Time cap (seconds)', { exact: true }).fill('10');
  await dialog.getByRole('button', { name: 'Save draft', exact: true }).click();
  await expect(dialog.getByText('Draft saved · revision 1', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Freeze and launch', exact: true })).toBeDisabled();
  const first = await (await request.get(`${base}/api/state?campaign_id=${campaign.id}`)).json();
  expect(first.trials).toHaveLength(0);
  expect(first.drafts).toHaveLength(1);
  expect(first.budget.allocated_seconds).toBe(0);
  await dialog.getByLabel('Optimization strategy').selectOption('coordinate');
  await dialog.getByText('Recovery and diagnostic schedules', { exact: true }).click();
  await dialog.getByLabel('Checkpoint every observations', { exact: true }).fill('2');
  await dialog.getByRole('button', { name: 'Add diagnostic schedule', exact: true }).click();
  await dialog.getByLabel('Milestone counts', { exact: true }).fill('2');
  await dialog.getByLabel('Capture optimizer artifacts', { exact: true }).uncheck();
  await dialog.getByRole('button', { name: 'Add validation', exact: true }).click();
  await dialog.getByLabel('Validation time cap (seconds)', { exact: true }).fill('5');
  await dialog.getByRole('button', { name: 'Save draft revision', exact: true }).click();
  await expect(dialog.getByText('Draft saved · revision 2', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Freeze and launch', exact: true })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('resolved-experiment-draft.png'), fullPage: true });
  await dialog.getByRole('button', { name: 'Freeze and launch', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => {
    const state = await (await request.get(`${base}/api/state?campaign_id=${campaign.id}`)).json();
    return state.trials.filter((trial: any) => trial.status === 'completed').length;
  }, { timeout: 20000 }).toBe(2);
  const final = await (await request.get(`${base}/api/state?campaign_id=${campaign.id}`)).json();
  expect(final.draft_launches).toHaveLength(1);
  const parent = final.trials.find((trial: any) => !trial.parent_trial_id);
  expect(parent.progress.scientific_complete).toBe(true);
  expect(parent.recovery.every_observations).toBe(2);
  expect(final.trials.find((trial: any) => trial.parent_trial_id).progress.recipe_result.verdict).toBe('passed');
  await page.getByRole('button', { name: 'Experiment drafts', exact: true }).click();
  await page.getByRole('button', { name: first.drafts[0].title, exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Freeze and launch', exact: true })).toBeDisabled();
});

for (const domain of ['bounded_continuous', 'meent_grating']) {
  test(`shared browser workflow runs ${domain} with raw objective and completion`, async ({ page, request }) => {
    const base = process.env.OPTIMIZATION_TEST_URL;
    test.skip(!base, 'Set OPTIMIZATION_TEST_URL to an isolated workspace with model calls disabled.');
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base!);
    await page.getByRole('button', { name: 'New campaign', exact: true }).click();
    const dialog = page.getByRole('dialog');
    const name = `${domain} browser qualification ${Date.now()}`;
    await dialog.getByLabel('Campaign name').fill(name);
    await dialog.getByLabel('Problem adapter').selectOption(domain);
    await dialog.getByLabel('Compute cap (seconds)', { exact: true }).fill('60');
    await dialog.getByLabel('Validation reserve (seconds)').fill('0');
    await dialog.getByRole('button', { name: 'Edit as JSON' }).click();
    await dialog.getByLabel('Problem configuration (JSON)').fill(JSON.stringify(domain === 'meent_grating'
      ? { n_cells: 4, fourier_order: 1 } : { dimensions: 2, function: 'quadratic', bounds: [[-3, 3], [-3, 3]] }));
    await dialog.getByRole('button', { name: 'Create campaign', exact: true }).click();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Design an experiment', exact: true }).click();
    await dialog.getByLabel('Optimization strategy').selectOption(domain === 'meent_grating' ? 'hillclimb' : 'coordinate');
    if (domain === 'bounded_continuous') await expect(dialog.getByLabel('Optimization strategy').locator('option[value="dqn"]')).toHaveCount(0);
    await dialog.getByLabel('Evaluation requests', { exact: true }).fill('12');
    await dialog.getByLabel('Time cap (seconds)', { exact: true }).fill('10');
    await dialog.getByRole('button', { name: 'Launch experiment' }).click();
    await expect(dialog).not.toBeVisible();
    const campaignId = await page.getByLabel('Active campaign').inputValue();
    let trial: Record<string, any> = {};
    await expect.poll(async () => {
      const state = await (await request.get(`${base}/api/state?campaign_id=${campaignId}`)).json();
      trial = state.trials[0] || {};
      return trial.status;
    }, { timeout: 15000 }).toBe('completed');
    expect(trial.progress.scientific_complete).toBe(true);
    expect(trial.progress.evaluations).toBe(12);
    expect(trial.problem.primary_objective.direction).toBe(domain === 'meent_grating' ? 'maximize' : 'minimize');
    await page.getByRole('button', { name: domain === 'meent_grating' ? 'Restart hill climbing' : 'Coordinate search', exact: true }).click();
    await expect(dialog.getByText('Scientific procedure complete', { exact: true })).toBeVisible();
    await expect(dialog.getByRole('heading', { name: 'Recent observations' })).toBeVisible();
    if (domain === 'bounded_continuous') await expect(dialog.getByRole('columnheader', { name: 'Coordinate', exact: true })).toBeVisible();
    else await expect(dialog.getByRole('img')).toBeVisible();
    await dialog.getByRole('button', { name: 'Close dialog' }).click();
    await page.getByRole('button', { name: 'Compare results', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Compare at full upstream cost.' })).toBeVisible();
    await page.getByLabel('Comparison cost').selectOption('evaluation_requests');
    await expect(page.getByRole('img', { name: /by evaluation_requests/ })).toBeVisible();
    await page.getByRole('button', { name: 'Validation', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Validation and waivers.' })).toBeVisible();
    await page.getByLabel('Validation time cap (seconds)').fill('10');
    if (domain === 'meent_grating') {
      await page.getByLabel('Fourier orders', { exact: true }).fill('1, 2');
      await page.getByLabel('Absolute tolerance', { exact: true }).fill('0.1');
      await page.getByLabel('Next action').selectOption('validation.require');
    }
    await page.getByRole('button', { name: 'Submit validation request' }).click();
    await expect(page.getByRole('button', { name: 'Run frozen check', exact: true })).toBeVisible();
    if (domain === 'meent_grating') {
      await page.getByRole('button', { name: 'Record waiver', exact: true }).click();
      await dialog.getByLabel('Waiver rationale').fill('Browser qualification of the explicit waiver path; no measured convergence claim.');
      await dialog.getByLabel('Supporting evidence record IDs').fill(trial.id);
      await dialog.getByRole('button', { name: 'Save waiver' }).click();
      await expect(dialog).not.toBeVisible();
    }
    await expect.poll(async () => {
      const rows = await (await request.get(`${base}/api/v1/campaigns/${campaignId}/validations`)).json();
      return rows[0]?.status;
    }, { timeout: 15000 }).toBe(domain === 'meent_grating' ? 'waived' : 'passed');
    await page.getByRole('button', { name: 'Research assets', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Reuse when it helps this problem.' })).toBeVisible();
    const assetButton = page.locator('table .table-link').first();
    await expect(assetButton).toBeVisible();
    await assetButton.click();
    await dialog.getByLabel('Asset decision').selectOption('decline');
    await dialog.getByLabel('Applicability rationale').fill('Preserve this evidence but start the next optimizer independently.');
    await dialog.getByRole('button', { name: 'Record asset decision' }).click();
    await expect(dialog).not.toBeVisible();
    await page.getByRole('button', { name: 'Studies', exact: true }).click();
    await page.getByRole('button', { name: 'Define a new study' }).click();
    await dialog.getByLabel('Study question').fill('A linked exploratory study for the same problem');
    await dialog.getByRole('button', { name: 'Freeze and activate study' }).click();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByRole('heading', { name: 'A linked exploratory study for the same problem' })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath(`${domain}.png`), fullPage: true });
    expect(errors).toEqual([]);
  });
}

test('a frozen confirmation roster schedules, validates and releases through the browser', async ({ page, request }) => {
  const base = process.env.OPTIMIZATION_TEST_URL;
  test.skip(!base, 'Set OPTIMIZATION_TEST_URL to an isolated workspace with model calls disabled.');
  const response = await request.post(`${base}/api/campaigns`, { data: {
    name: `Confirmation qualification ${Date.now()}`, compute_budget_seconds: 100, validation_reserve_seconds: 0,
    tasks: [{ name: 'Quadratic', problem_id: 'bounded_continuous', configuration: {} }],
  } });
  expect(response.ok()).toBeTruthy();
  const campaign = await response.json();
  const state = await (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
  const prototypeResponse = await request.post(`${base}/api/v1/commands`, { data: {
    id: `prototype_${Date.now()}`, campaign_id: campaign.id, expected_revision: campaign.version, operation: 'trial.create',
    payload: { task_id: state.tasks[0].id, algorithm: 'coordinate', max_steps: 3, wall_seconds: 5 },
  } });
  expect(prototypeResponse.ok()).toBeTruthy();
  const prototype = (await prototypeResponse.json()).outcome.trial_id;
  await page.goto(base!);
  await page.getByLabel('Active campaign').selectOption(campaign.id);
  await page.getByRole('button', { name: 'Studies', exact: true }).click();
  await page.getByRole('button', { name: 'Define a new study' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Study question').fill('Confirm the frozen procedure with independent seeds');
  await dialog.getByLabel('Study scope').selectOption('confirmation');
  await dialog.getByLabel('Fresh seeds').fill('17, 18');
  await dialog.getByLabel('Prototype experiments', { exact: true }).selectOption(prototype);
  await dialog.getByLabel('Require Known-value evaluator fixtures').check();
  await dialog.getByLabel('Time cap per required check (seconds)').fill('5');
  await dialog.getByRole('button', { name: 'Freeze and activate study' }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole('button', { name: 'Schedule missing cells' }).click();
  await expect(page.getByRole('button', { name: 'Schedule missing cells' })).toBeDisabled();
  let protocol = '';
  await expect.poll(async () => {
    const current = await (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
    protocol = current.studies.find((study: Record<string, any>) => study.id === current.campaign.active_study_id).confirmation.id;
    const assessment = await (await request.get(`${base}/api/v1/confirmations/${protocol}`)).json();
    return assessment.cells.filter((cell: Record<string, any>) => cell.scientific_complete).length;
  }, { timeout: 15000 }).toBe(2);
  await expect(page.getByRole('button', { name: 'Release completed evidence' })).toBeDisabled();
  await page.getByRole('button', { name: 'Run required checks' }).click();
  await expect.poll(async () => (await (await request.get(`${base}/api/v1/confirmations/${protocol}`)).json()).complete, { timeout: 15000 }).toBe(true);
  await page.getByRole('button', { name: 'Release completed evidence' }).click();
  await expect(page.getByText(/Released as completed roster/)).toBeVisible();
  await page.getByText('Evidence report · descriptive', { exact: true }).click();
  await expect(page.getByText(/"evidence_hash"/)).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('confirmation.png'), fullPage: true });
});

test('development nomination and isolated diagnostics feed a frozen claim report', async ({ page, request }) => {
  const base = process.env.OPTIMIZATION_TEST_URL;
  test.skip(!base, 'Set OPTIMIZATION_TEST_URL to an isolated workspace with model calls disabled.');
  test.setTimeout(60000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const response = await request.post(`${base}/api/campaigns`, { data: {
    name: `Selection qualification ${Date.now()}`, compute_budget_seconds: 200, validation_reserve_seconds: 0,
    tasks: [{ name: 'Quadratic selection', problem_id: 'bounded_continuous', configuration: {} }],
  } });
  expect(response.ok()).toBeTruthy();
  const campaign = await response.json();
  const read = async () => (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
  await page.goto(base!);
  await page.getByLabel('Active campaign').selectOption(campaign.id);
  await page.getByRole('button', { name: 'Studies', exact: true }).click();
  await page.getByRole('button', { name: 'Define a new study' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Study question').fill('Select a procedure on two declared development seeds');
  await dialog.getByLabel('Development selection rule').selectOption('framework|median_objective:v1');
  await dialog.getByLabel('Seeds', { exact: true }).fill('10, 11');
  await dialog.getByRole('button', { name: 'Freeze and activate study' }).click();
  await expect(dialog).not.toBeVisible();
  const state = await read();
  for (const algorithm of ['coordinate', 'random']) for (const seed of [10, 11]) {
    const created = await request.post(`${base}/api/v1/commands`, { data: {
      id: `selection_${campaign.id}_${algorithm}_${seed}`, campaign_id: campaign.id, expected_revision: state.campaign.version,
      operation: 'trial.create', payload: { task_id: state.tasks[0].id, algorithm, seed, max_steps: 6, wall_seconds: 5,
        diagnostics: algorithm === 'coordinate' ? [{ at_counts: [3], export_optimizer: false,
          recipes: [{ recipe_id: 'analytic_fixtures:v1', wall_seconds: 5 }] }] : [] },
    } });
    expect(created.ok(), await created.text()).toBeTruthy();
  }
  await expect.poll(async () => {
    const current = await read();
    return current.trials.length === 6 && current.trials.every((trial: Record<string, any>) => trial.status === 'completed' && trial.asset_capture_attempt === trial.attempt);
  }, { timeout: 20000 }).toBe(true);
  await expect(page.getByText('2 eligible methods.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Freeze method nomination' }).click();
  await expect(page.getByText(/Frozen nomination:/)).toBeVisible();
  const selected = await read(), nomination = selected.nominations[0];
  expect(nomination.result.selected_method_ids).toHaveLength(1);
  const prototype = selected.trials.find((trial: Record<string, any>) => Object.values(nomination.prototypes).includes(trial.id));
  const control = selected.trials.find((trial: Record<string, any>) => !trial.recipe && trial.algorithm !== prototype.algorithm);
  await page.getByRole('button', { name: 'Define a new study' }).click();
  await dialog.getByLabel('Study question').fill('Confirm the nominated procedure against its declared control');
  await dialog.getByLabel('Study scope').selectOption('confirmation');
  await dialog.getByLabel('Fresh seeds').fill('100, 101');
  await dialog.getByLabel('Frozen development nomination').selectOption(nomination.id);
  await dialog.getByLabel('Prototype experiments', { exact: true }).selectOption(control.id);
  await dialog.getByLabel('Confirmation analysis rule').selectOption('framework|paired_improvement:v1');
  await dialog.getByLabel('Minimum Wins', { exact: true }).fill('1');
  await dialog.getByRole('button', { name: 'Freeze and activate study' }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole('button', { name: 'Schedule missing cells' }).click();
  let protocol = '';
  await expect.poll(async () => {
    const current = await read();
    protocol = current.studies.find((study: Record<string, any>) => study.id === current.campaign.active_study_id).confirmation.id;
    return (await (await request.get(`${base}/api/v1/confirmations/${protocol}`)).json()).complete;
  }, { timeout: 20000 }).toBe(true);
  await page.getByRole('button', { name: 'Release completed evidence' }).click();
  await expect(page.getByText('Evidence report · protocol conclusion', { exact: true })).toBeVisible({ timeout: 20000 });
  const assessment = await (await request.get(`${base}/api/v1/confirmations/${protocol}`)).json();
  expect(assessment.report.evidence.analysis_evidence.nomination.id).toBe(nomination.id);
  expect(assessment.report.evidence.analysis_evidence.nomination_review.supported).toBe(true);
  await page.screenshot({ path: test.info().outputPath('nomination-and-confirmation.png'), fullPage: true });
  await page.getByRole('button', { name: 'Experiments', exact: true }).click();
  await page.getByRole('button', { name: 'Coordinate search', exact: true }).first().click();
  await expect(dialog.getByRole('heading', { name: 'Declared diagnostic milestones' })).toBeVisible();
  await expect(dialog.getByText('3 evaluation requests', { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('diagnostic-milestone.png'), fullPage: true });
  expect(errors).toEqual([]);
});
