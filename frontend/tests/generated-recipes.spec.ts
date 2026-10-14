import { test, expect } from '@playwright/test';
import { openNewCampaign } from './setupFixtures';
import { readFileSync, writeFileSync } from 'node:fs';

test('commissioned evaluator recipes work through study, diagnostic, and validation controls', async ({ page, request }, info) => {
  const base = process.env.COMMISSIONING_TEST_URL;
  test.skip(!base, 'Set COMMISSIONING_TEST_URL to the isolated supplied-code qualification workspace.');
  test.setTimeout(120000);
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  const manifest = { id: 'recipe_quadratic', name: 'Quadratic with declared analyses',
    candidate_schema: { representation: 'continuous', dimensions: 2, bounds: [[-3, 3], [-3, 3]] },
    primary_objective: { name: 'energy', direction: 'minimize', units: 'arbitrary energy units' },
    configuration_schema: { type: 'object', additionalProperties: false, required: ['center', 'offset'],
      properties: { center: { type: 'number', minimum: -2, maximum: 2 }, offset: { type: 'number' } } },
    configuration: { center: 1.5, offset: -2 },
    fidelity_schema: { type: 'object', additionalProperties: false, required: ['digits'],
      properties: { digits: { type: 'integer', minimum: 0, maximum: 12 } } }, fidelity: { digits: 8 },
    recipe_ids: ['unavailable_analysis:v1'], deterministic: true };
  await page.goto(`${base}/#problem`);
  const form = await openNewCampaign(page, { from: 'sidebar', path: 'Declare it myself' });
  const dialog = page.getByRole('dialog');
  await form.getByLabel('Campaign name').fill(`Generated recipe qualification ${Date.now()}`);
  await form.getByLabel('Compute cap (seconds)', { exact: true }).fill('120');
  await form.getByLabel('Implementation compute cap (seconds)', { exact: true }).fill('60');
  await form.getByLabel('Validation reserve (seconds)').fill('0');
  await form.getByLabel('API spending cap (USD)').fill('0');
  await form.getByLabel('Configuration 1', { exact: true }).fill('Declared quadratic');
  await form.getByLabel('Problem adapter').selectOption('__commission__');
  await form.getByText('Advanced declaration: configuration, fidelity, constraints and metrics', { exact: true }).click();
  await form.getByLabel('Problem manifest JSON').fill(JSON.stringify(manifest));
  await form.getByRole('button', { name: 'Apply declaration', exact: true }).click();
  await expect(form.getByLabel('Objective name', { exact: true })).toHaveValue('energy');
  await form.getByLabel('Reevaluate observed candidates', { exact: true }).check();
  await form.getByLabel('Compare declared fidelities', { exact: true }).check();
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/commands') && response.request().method() === 'POST'
    && response.request().postDataJSON()?.operation === 'campaign.create');
  await form.getByRole('button', { name: 'Create campaign', exact: true }).click();
  const campaign = (await (await created).json()).outcome.campaign;
  expect(campaign.id).toBeTruthy();
  await expect(form).not.toBeVisible();
  const state = async () => (await request.get(`${base}/api/v1/state?campaign_id=${campaign.id}`)).json();
  expect((await state()).tasks[0].evaluator_manifest.recipe_ids).toEqual([
    'unavailable_analysis:v1', 'candidate_reevaluation:v1', 'fidelity_comparison:v1']);

  await page.getByRole('button', { name: 'Build or reuse evaluator', exact: true }).click();
  await dialog.getByLabel('Evaluator specification', { exact: true }).fill('Sum squared distance to center plus offset, rounded to digits.');
  await dialog.getByLabel('Evaluator time allocation (seconds)').fill('30');
  await dialog.getByLabel('Evaluator maximum model calls').fill('1');
  await dialog.getByLabel('Reference 1 candidate').fill('0, 0');
  await dialog.getByLabel('Reference 1 expected energy').fill('2.5');
  await dialog.getByLabel('Reference 1 independent basis').fill('Analytical: 2*(0-1.5)^2-2 = 2.5');
  await dialog.getByRole('button', { name: 'Add independent reference' }).click();
  await dialog.getByLabel('Reference 2 candidate').fill('1.5, 1.5');
  await dialog.getByLabel('Reference 2 expected energy').fill('-2');
  await dialog.getByLabel('Reference 2 independent basis').fill('Analytical minimum: both squared distances vanish.');
  await dialog.getByText('Supply an existing evaluator package', { exact: true }).click();
  await dialog.getByLabel('Evaluator package JSON').fill(JSON.stringify({ kind: 'evaluator', contract: 'evaluator_v1',
    entrypoint: 'evaluator:create_evaluator', files: [{ path: 'evaluator.py',
      content: readFileSync('../examples/implementation-reference/continuous_evaluator.py', 'utf8') }] }));
  await dialog.getByRole('button', { name: 'Commission evaluator', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => (await state()).tasks[0].evaluator_readiness?.runnable, { timeout: 25000 }).toBe(true);
  const built = await state(), evaluatorId = built.tasks[0].evaluator_version_id;

  await page.goto(`${base}/#studies`);
  await page.getByRole('button', { name: 'Define a new study' }).click();
  await dialog.getByLabel('Study question').fill('Measure repeatability and declared fidelity agreement');
  await dialog.getByLabel('Require Compare declared fidelities', { exact: true }).check();
  await dialog.getByLabel('Fidelity settings sequence (JSON)', { exact: true }).fill('[{"digits":4},{"digits":8}]');
  await dialog.getByLabel('Absolute tolerance', { exact: true }).fill('0.0001');
  await dialog.getByLabel('Time cap per required check (seconds)', { exact: true }).fill('10');
  await dialog.getByRole('button', { name: 'Freeze and activate study' }).click();
  await expect(dialog).not.toBeVisible();
  const frozen = await state(), study = frozen.studies.find((item: any) => item.id === frozen.campaign.active_study_id);
  expect(study.validation_policy.required_recipe_parameters['fidelity_comparison:v1'].fidelities).toEqual([{ digits: 4 }, { digits: 8 }]);

  await page.goto(`${base}/#drafts`);
  await page.getByRole('button', { name: 'Design an experiment', exact: true }).click();
  await dialog.getByLabel('Optimization strategy').selectOption('coordinate');
  await dialog.getByLabel('Evaluation requests', { exact: true }).fill('4');
  await dialog.getByLabel('Time cap (seconds)', { exact: true }).fill('10');
  await dialog.getByText('Recovery and diagnostic schedules', { exact: true }).click();
  await dialog.getByRole('button', { name: 'Add diagnostic schedule', exact: true }).click();
  await dialog.getByLabel('Milestone counts', { exact: true }).fill('2');
  await dialog.getByLabel('Capture optimizer artifacts', { exact: true }).uncheck();
  await dialog.getByRole('button', { name: 'Add validation', exact: true }).click();
  await dialog.getByLabel('Validation recipe', { exact: true }).selectOption('candidate_reevaluation:v1');
  await dialog.getByLabel('Evaluations per candidate', { exact: true }).fill('2');
  await dialog.getByLabel('Validation time cap (seconds)', { exact: true }).fill('5');
  await dialog.getByRole('button', { name: 'Launch experiment', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => (await state()).trials.filter((trial: any) => trial.status === 'completed' && trial.latest_output_asset_ids?.length).length,
    { timeout: 30000 }).toBe(2);
  const searched = await state(), parent = searched.trials.find((trial: any) => !trial.recipe);
  const diagnostic = searched.trials.find((trial: any) => trial.recipe?.recipe_id === 'candidate_reevaluation:v1');
  expect(diagnostic.diagnostic_grant_id).toBeTruthy();
  expect(diagnostic.evaluator_version_id).toBe(evaluatorId);
  expect(diagnostic.validation_requirement_ids).toEqual([]);

  await page.goto(`${base}/#validation`);
  await page.getByLabel('Source experiment', { exact: true }).selectOption(parent.id);
  await expect(page.getByRole('option', { name: 'unavailable_analysis:v1 · unavailable', exact: true })).toBeDisabled();
  await page.getByLabel('Validation recipe', { exact: true }).selectOption('fidelity_comparison:v1');
  await page.getByLabel('Fidelity settings sequence (JSON)', { exact: true }).fill('[{"digits":4},{"digits":8}]');
  await page.getByLabel('Absolute tolerance', { exact: true }).fill('0.0001');
  await page.getByLabel('Validation time cap (seconds)', { exact: true }).fill('10');
  await page.getByRole('button', { name: 'Submit validation request', exact: true }).click();
  const assessments = async () => (await request.get(`${base}/api/v1/campaigns/${campaign.id}/validations`)).json();
  await expect.poll(async () => (await assessments()).filter((row: any) => row.measured_pass).length, { timeout: 25000 }).toBeGreaterThan(0);
  const final = await state(), checked = final.trials.find((trial: any) => trial.recipe?.recipe_id === 'fidelity_comparison:v1');
  expect(checked.evaluator_version_id).toBe(evaluatorId);
  expect(checked.execution_manifest).toEqual(parent.execution_manifest);
  expect(checked.result.scientific_complete).toBe(true);
  expect(checked.result.recipe_result.subjects.every((subject: any) => subject.verdict === 'passed')).toBe(true);
  await expect(page.getByText('Measured pass', { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: info.outputPath('commissioned-fidelity-evidence.png'), fullPage: true });
  const assets = await Promise.all(checked.latest_output_asset_ids.map(async (id: string) =>
    (await request.get(`${base}/api/v1/assets/${id}`)).json()));
  const evaluator = final.implementation_library.versions.find((item: any) => item.id === evaluatorId);
  const implementation = await (await request.get(`${base}/api/v1/assets/${parent.evaluator_cost_asset_ids[0]}`)).json();
  // The durable library can retain earlier commissions of this same version.
  // A result includes that declared upstream work once, not just its last report.
  const upstream = implementation.full_attributed_cost.quantities.evaluation_requests.total;
  expect(upstream).toBeGreaterThanOrEqual(evaluator.validation_report.costs.evaluation_requests);
  const expected = upstream + 4 + checked.result.evaluations;
  expect(assets.every((item: any) => item.full_attributed_cost.quantities.evaluation_requests.total === expected)).toBe(true);
  const catalog = await (await request.get(`${base}/api/v1/trials/${parent.id}/problem`)).json();
  expect(catalog.basis).toBe('captured_source');
  expect(catalog.definition.evaluator_version).toBe(evaluatorId);
  expect(browserErrors).toEqual([]);
  const reportPath = info.outputPath('generated-recipe-evidence.json');
  writeFileSync(reportPath, JSON.stringify({ evidence_kind: 'supplied_code_mocked_model_review', campaign: final.campaign,
    task: final.tasks[0], study, evaluator, implementation, catalog, parent, diagnostic, checked, assets, requirements: await assessments(),
    browser_errors: browserErrors }, null, 2));
  await info.attach('generated-recipe-evidence.json', { path: reportPath, contentType: 'application/json' });
});
