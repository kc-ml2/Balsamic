import { test, expect } from '@playwright/test';
import { openNewCampaign } from './setupFixtures';
import { readFileSync, writeFileSync } from 'node:fs';

test('contract-only evaluator use requires a study waiver and cannot become confirmation', async ({ page, request }, info) => {
  const base = process.env.COMMISSIONING_TEST_URL;
  test.skip(!base, 'Set COMMISSIONING_TEST_URL to the supplied-code qualification workspace.');
  test.setTimeout(90000);
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  const manifest = { id: 'exploratory_quadratic', name: 'Exploratory quadratic',
    candidate_schema: { representation: 'continuous', dimensions: 2, bounds: [[-3, 3], [-3, 3]] },
    primary_objective: { name: 'energy', direction: 'minimize', units: 'arbitrary energy units' },
    configuration_schema: { type: 'object', additionalProperties: false,
      properties: { center: { type: 'number' }, offset: { type: 'number' } } },
    configuration: { center: 1.5, offset: -2 },
    fidelity_schema: { type: 'object', additionalProperties: false, properties: { digits: { type: 'integer' } } }, fidelity: { digits: 8 } };
  const campaign = await (await request.post(`${base}/api/campaigns`, { data: {
    name: `Exploratory evaluator ${Date.now()}`, compute_budget_seconds: 60, validation_reserve_seconds: 0,
    implementation_compute_budget_seconds: 60,
    tasks: [{ name: 'Unverified quadratic', problem_id: manifest.id, evaluator_manifest: manifest }],
  } })).json();
  expect(campaign.id).toBeTruthy();
  const state = async () => (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
  await page.goto(`${base}/#problem`);
  await page.getByLabel('Active campaign').selectOption(campaign.id);
  await page.getByRole('button', { name: 'Build or reuse evaluator', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Evaluator specification', { exact: true }).fill('Sum squared distance to center plus offset, rounded to digits.');
  await dialog.getByLabel('Evaluator validation mode').selectOption('contract_only');
  await expect(dialog.getByLabel('Reference 1 expected energy')).toHaveCount(0);
  await dialog.getByLabel('Probe 1 candidate').fill('0, 0');
  await dialog.getByLabel('Evaluator time allocation (seconds)').fill('30');
  await dialog.getByLabel('Evaluator maximum model calls').fill('1');
  await dialog.getByText('Supply an existing evaluator package', { exact: true }).click();
  await dialog.getByLabel('Evaluator package JSON').fill(JSON.stringify({ kind: 'evaluator', contract: 'evaluator_v1',
    entrypoint: 'evaluator:create_evaluator', files: [{ path: 'evaluator.py',
      content: readFileSync('../examples/implementation-reference/continuous_evaluator.py', 'utf8') }] }));
  await dialog.getByRole('button', { name: 'Commission evaluator', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => (await state()).tasks[0].evaluator_readiness?.state, { timeout: 25000 }).toBe('numerical_evidence_required');
  const built = await state(), evaluatorId = built.tasks[0].evaluator_version_id;
  const version = built.implementation_library.versions.find((item: any) => item.id === evaluatorId);
  expect(version.status).toBe('contract_validated');
  expect(version.validation_report.numerical_status).toBe('unverified');
  expect(built.tasks[0].evaluator_readiness.runnable).toBe(false);
  await expect(page.getByRole('button', { name: 'Build or reuse evaluator', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Review evaluator evidence' }).click();
  await expect(page.getByRole('button', { name: 'Run frozen check', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Record waiver', exact: true })).toHaveCount(0);

  await page.goto(`${base}/#studies`);
  await page.getByRole('button', { name: 'Define a new study' }).click();
  await dialog.getByLabel('Study question').fill('Explore the harness without claiming numerical correctness');
  await dialog.getByLabel('Allow scoped evaluator waivers for exploratory use').check();
  await dialog.getByRole('button', { name: 'Freeze and activate study' }).click();
  await expect(dialog).not.toBeVisible();
  const scoped = await state(), studyId = scoped.campaign.active_study_id;
  expect(scoped.tasks[0].evaluator_readiness.waiver_allowed).toBe(true);
  expect(scoped.tasks[0].evaluator_readiness.runnable).toBe(false);
  await page.goto(`${base}/#drafts`);
  await page.getByRole('button', { name: 'Design an experiment', exact: true }).click();
  await dialog.getByLabel('Experiment title').fill('Exploratory evaluator experiment');
  await dialog.getByLabel('Optimization strategy').selectOption('coordinate');
  await dialog.getByLabel('Evaluation requests', { exact: true }).fill('4');
  await dialog.getByLabel('Time cap (seconds)', { exact: true }).fill('10');
  await expect(dialog.getByRole('button', { name: 'Launch experiment', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Save draft', exact: true }).click();
  await expect(dialog.getByText('Draft saved · revision 1', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Freeze and launch', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Close dialog' }).click();
  await page.goto(`${base}/#validation`);
  await page.getByRole('button', { name: 'Record waiver', exact: true }).click();
  await expect(dialog.getByLabel('Supporting evidence record IDs')).toHaveValue(/evaluator_evidence_/);
  await dialog.getByLabel('Waiver rationale').fill('Bounded exploratory harness exercise using contract evidence; numerical correctness and confirmation remain unestablished.');
  await dialog.getByRole('button', { name: 'Save waiver', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => (await state()).tasks[0].evaluator_readiness?.state).toBe('ready_with_waiver');
  const authorized = await state(), eligibility = authorized.tasks[0].evaluator_readiness.eligibility;
  await page.screenshot({ path: info.outputPath('scoped-evaluator-waiver.png'), fullPage: true });
  await page.goto(`${base}/#drafts`);
  await page.getByRole('button', { name: 'Exploratory evaluator experiment', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Freeze and launch', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Freeze and launch', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => (await state()).trials[0]?.result?.scientific_complete, { timeout: 20000 }).toBe(true);
  const finished = await state(), trial = finished.trials[0];
  expect(trial.evaluator_eligibility).toEqual(eligibility);
  expect(trial.experiment_spec.schedule.evaluator_eligibility).toEqual(eligibility);
  expect(trial.study_id).toBe(studyId);
  const confirmation = await request.post(`${base}/api/v1/commands`, { data: {
    id: `forbidden-confirmation-${Date.now()}`, campaign_id: campaign.id, expected_revision: finished.campaign.version,
    operation: 'study.create', payload: { goal: 'Cannot confirm using exploratory authorization', scope: 'confirmation',
      confirmation_kind: 'seed_replication', prototype_trial_ids: [trial.id], seeds: [100, 101] },
  } });
  expect(confirmation.status()).toBe(409);
  const rejected = await confirmation.json();
  expect(JSON.stringify(rejected)).toContain('cannot support confirmation');
  await page.goto(`${base}/#validation`);
  await page.getByRole('button', { name: 'Revoke waiver', exact: true }).click();
  await dialog.getByLabel('Revocation rationale').fill('Require independent evidence before another attempt.');
  await dialog.getByRole('button', { name: 'Revoke waiver', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => (await state()).tasks[0].evaluator_readiness?.runnable).toBe(false);
  const final = await state();
  expect(final.trials).toHaveLength(1);
  expect(final.trials[0].evaluator_eligibility).toEqual(eligibility);
  expect(final.trials[0].result.scientific_complete).toBe(true);
  expect(final.campaign.active_study_id).toBe(studyId);
  const assessments = await (await request.get(`${base}/api/v1/campaigns/${campaign.id}/validations`)).json();
  const assessment = assessments.find((item: any) => item.requirement.id === eligibility.requirement_id);
  expect(assessment.measured_pass).toBe(false);
  expect(assessment.revoked_waiver_ids).toContain(eligibility.waiver_id);
  expect(browserErrors).toEqual([]);
  const reportPath = info.outputPath('eligibility-evidence.json');
  writeFileSync(reportPath, JSON.stringify({ evidence_kind: 'supplied_code_mocked_model_review', campaign: final.campaign,
    task: final.tasks[0], trial: final.trials[0], evaluator: version, assessment,
    rejected_confirmation: rejected, browser_errors: browserErrors }, null, 2));
  await info.attach('eligibility-evidence.json', { path: reportPath, contentType: 'application/json' });
});

// This requires scripts/serve_commissioning_fixture.py on isolated service ports.
// Only model review is mocked; browser, HTTP, isolation and numerical work are real.
test('commissioned optimizer and evaluator preserve the draft, binding, and upstream evidence', async ({ page, request }, info) => {
  const base = process.env.COMMISSIONING_TEST_URL;
  test.skip(!base, 'Set COMMISSIONING_TEST_URL to the supplied-code qualification workspace.');
  test.setTimeout(120000);
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  const manifest = { id: 'commissioned_quadratic', name: 'Commissioned quadratic',
    candidate_schema: { representation: 'continuous', dimensions: 2, bounds: [[-3, 3], [-3, 3]] },
    primary_objective: { name: 'energy', direction: 'minimize', units: 'arbitrary energy units' },
    configuration_schema: { type: 'object', additionalProperties: false, required: ['center', 'offset'],
      properties: { center: { type: 'number', minimum: -2, maximum: 2 }, offset: { type: 'number' } } },
    configuration: { center: 1.5, offset: -2 }, fidelity_schema: { type: 'object', additionalProperties: false,
      required: ['digits'], properties: { digits: { type: 'integer', minimum: 0, maximum: 12 } } }, fidelity: { digits: 8 } };
  const evaluatorPackage = { kind: 'evaluator', contract: 'evaluator_v1', entrypoint: 'evaluator:create_evaluator', files: [
    { path: 'evaluator.py', content: readFileSync('../examples/implementation-reference/continuous_evaluator.py', 'utf8') }] };
  const optimizerPackage = { contract: 'optimizer_v1', entrypoint: 'optimizer:create_optimizer', files: [
    { path: 'optimizer.py', content: readFileSync('../examples/implementation-reference/continuous_optimizer_v1.py', 'utf8') }] };
  await page.goto(`${base}/#problem`);
  const form = await openNewCampaign(page, { from: 'sidebar', path: 'Declare it myself' });
  const dialog = page.getByRole('dialog');
  await form.getByLabel('Campaign name').fill(`Executable commissioning ${Date.now()}`);
  await form.getByLabel('Compute cap (seconds)', { exact: true }).fill('120');
  await form.getByLabel('Implementation compute cap (seconds)', { exact: true }).fill('120');
  await form.getByLabel('Validation reserve (seconds)').fill('0');
  await form.getByLabel('API spending cap (USD)').fill('0');
  await form.getByLabel('Configuration 1', { exact: true }).fill('Independent quadratic');
  await form.getByLabel('Problem adapter').selectOption('__commission__');
  await form.getByText('Advanced declaration: configuration, fidelity, constraints and metrics', { exact: true }).click();
  await form.getByLabel('Problem manifest JSON').fill(JSON.stringify(manifest));
  await form.getByRole('button', { name: 'Apply declaration', exact: true }).click();
  await expect(form.getByLabel('Objective name', { exact: true })).toHaveValue('energy');
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/commands') && response.request().method() === 'POST'
    && response.request().postDataJSON()?.operation === 'campaign.create');
  await form.getByRole('button', { name: 'Create campaign', exact: true }).click();
  const campaign = (await (await created).json()).outcome.campaign;
  expect(campaign.id).toBeTruthy();
  await expect(form).not.toBeVisible();
  const state = async () => (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
  const original = await state();
  const task = original.tasks[0], study = original.studies.find((item: any) => item.id === campaign.active_study_id);

  await page.goto(`${base}/#hypotheses`);
  await page.getByRole('button', { name: 'Add idea', exact: true }).click();
  await dialog.getByLabel('Strategy title').fill('Commissioned coordinate search');
  await dialog.getByLabel('Proposed mechanism').fill('Sample one coordinate of the best observed incumbent.');
  await dialog.getByLabel('Rationale and cheapest useful test').fill('Check a specialized bounded search on the declared quadratic.');
  await dialog.getByLabel('Runnable implementation').selectOption('');
  await dialog.getByRole('button', { name: 'Save hypothesis', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole('button', { name: /Commissioned coordinate search.*No campaign implementation/ }).click();
  await dialog.getByRole('button', { name: 'Design experiment', exact: true }).click();
  await dialog.getByLabel('Experiment title').fill('Both executables required');
  await dialog.getByLabel('Evaluation requests', { exact: true }).fill('4');
  await dialog.getByLabel('Time cap (seconds)', { exact: true }).fill('10');
  await expect(dialog.getByRole('button', { name: 'Launch experiment', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Save draft', exact: true }).click();
  await expect(dialog.getByText('Draft saved · revision 1', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Freeze and launch', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Close dialog' }).click();

  await page.goto(`${base}/#problem`);
  await page.getByRole('button', { name: 'Build or reuse evaluator', exact: true }).click();
  await dialog.getByLabel('Evaluator specification', { exact: true }).fill('Sum squared distance to center plus offset, rounded to digits.');
  await dialog.getByLabel('Evaluator time allocation (seconds)').fill('40');
  await dialog.getByLabel('Evaluator maximum model calls').fill('1');
  await dialog.getByLabel('Reference 1 candidate').fill('0, 0');
  await dialog.getByLabel('Reference 1 expected energy').fill('2.5');
  await dialog.getByLabel('Reference 1 independent basis').fill('Analytical: 2*(0-1.5)^2-2 = 2.5');
  await dialog.getByRole('button', { name: 'Add independent reference' }).click();
  await dialog.getByLabel('Reference 2 candidate').fill('1.5, 1.5');
  await dialog.getByLabel('Reference 2 expected energy').fill('-2');
  await dialog.getByLabel('Reference 2 independent basis').fill('Analytical minimum: both squared distances vanish.');
  await dialog.getByText('Supply an existing evaluator package', { exact: true }).click();
  await dialog.getByLabel('Evaluator package JSON').fill(JSON.stringify(evaluatorPackage));
  await dialog.getByRole('button', { name: 'Commission evaluator', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => (await state()).tasks[0].evaluator_readiness?.runnable, { timeout: 25000 }).toBe(true);
  const evaluated = await state(), evaluatorId = evaluated.tasks[0].evaluator_version_id;
  expect(evaluated.studies.find((item: any) => item.id === study.id)).toEqual(study);

  await page.goto(`${base}/#hypotheses`);
  await page.getByRole('button', { name: /Commissioned coordinate search.*No campaign implementation/ }).click();
  await dialog.getByRole('button', { name: 'Request implementation', exact: true }).click();
  await expect(dialog.getByLabel('Required evaluator capabilities')).toHaveValue('continuous, scalar_objective');
  await expect(dialog.getByText(`Correctness checks will use evaluator ${evaluatorId}.`)).toBeVisible();
  await dialog.getByLabel('Implementation time allocation (seconds)').fill('40');
  await dialog.getByLabel('Maximum model calls', { exact: true }).fill('1');
  await dialog.getByText('Independent behavioral checks', { exact: true }).click();
  await dialog.getByLabel('Optimizer behavior checks (JSON)').fill(JSON.stringify([{ name: 'Best incumbent', n_cells: 2,
    assertion: 'one_coordinate_from_incumbent', efficiencies: [-1, -3, -2, 1] }]));
  await dialog.getByText('Import an existing package instead of building the first candidate', { exact: true }).click();
  await dialog.getByLabel('Package JSON', { exact: true }).fill(JSON.stringify(optimizerPackage));
  await dialog.getByRole('button', { name: 'Commission implementation', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => (await state()).hypotheses.find((h: any) => h.title === 'Commissioned coordinate search')?.implementation_readiness?.runnable,
    { timeout: 25000 }).toBe(true);
  const built = await state(), hypothesis = built.hypotheses.find((h: any) => h.title === 'Commissioned coordinate search');
  await page.goto(`${base}/#implementations`);
  await expect(page.getByRole('heading', { name: 'Implementation versions', exact: true })).toBeVisible();
  await expect(page.getByText(evaluatorId, { exact: true })).toBeVisible();
  await expect(page.getByText(hypothesis.implementation_version_id, { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('both-executable-kinds.png'), fullPage: true });

  await page.goto(`${base}/#problem`);
  await page.getByRole('button', { name: 'Revise charter' }).click();
  await dialog.getByLabel('Compute cap (seconds)', { exact: true }).fill('180');
  await dialog.getByRole('button', { name: 'Save new version', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const amended = await state();
  expect(amended.tasks[0].id).toBe(task.id);
  expect(amended.tasks[0].evaluator_version_id).toBe(evaluatorId);
  expect(amended.campaign.active_study_id).toBe(study.id);
  await page.goto(`${base}/#drafts`);
  await page.getByRole('button', { name: 'Both executables required', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Freeze and launch', exact: true })).toBeEnabled();
  await page.screenshot({ path: info.outputPath('resolved-commissioning-draft.png'), fullPage: true });
  await dialog.getByRole('button', { name: 'Freeze and launch', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  let final: any;
  await expect.poll(async () => {
    final = await state();
    return final.trials.find((trial: any) => trial.hypothesis_id === hypothesis.id)?.latest_output_asset_ids?.length || 0;
  }, { timeout: 25000 }).toBeGreaterThan(0);
  const trial = final.trials.find((item: any) => item.hypothesis_id === hypothesis.id);
  expect(trial.status).toBe('completed');
  expect(trial.evaluator_version_id).toBe(evaluatorId);
  expect(trial.implementation_version_id).toBe(hypothesis.implementation_version_id);
  expect(trial.result.evaluations).toBe(4);
  expect(trial.result.scientific_complete).toBe(true);
  expect(final.studies.find((item: any) => item.id === study.id)).toEqual(study);
  expect(browserErrors).toEqual([]);
  const reportPath = info.outputPath('commissioning-evidence.json');
  writeFileSync(reportPath, JSON.stringify({ evidence_kind: 'supplied_code_mocked_model_review',
    campaign: final.campaign, original_study: study, task: final.tasks[0], jobs: final.implementation_jobs,
    trial, browser_errors: browserErrors }, null, 2));
  await info.attach('commissioning-evidence.json', { path: reportPath, contentType: 'application/json' });
});
