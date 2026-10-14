import { test, expect } from '@playwright/test';
import { emptyWorkspace, openNewCampaign } from './setupFixtures';

test('an installed problem starts from a chosen example and is edited through its declared fields', async ({ page }) => {
  const { commands } = await emptyWorkspace(page);
  await page.goto('/');
  const form = await openNewCampaign(page);
  // Nothing problem-specific is chosen until the researcher says how the problem is defined.
  await expect(form.getByRole('radio', { name: 'Not defined yet' })).toBeChecked();
  await expect(form.getByLabel('n_cells', { exact: true })).toHaveCount(0);
  await form.getByRole('radio', { name: 'An installed problem' }).check();
  const examples = form.getByLabel('Examples');
  await expect(examples.getByRole('button', { name: /1D binary grating/ })).toHaveAttribute('aria-pressed', 'false');
  await expect(form.getByLabel('Problem adapter')).toHaveValue('');

  await examples.getByRole('button', { name: /Octavian FLRL/ }).click();
  await expect(form.getByLabel('Campaign name')).toHaveValue('2D deflector — Octavian FLRL');
  await expect(form.getByLabel('Compute cap (seconds)', { exact: true })).toHaveValue('248400');
  await expect(form.getByLabel('Problem adapter')).toHaveValue('meent_2d_dual_polarization_deflector');
  await expect(form.getByLabel('rcwa_order_x', { exact: true })).toHaveValue('10');
  await form.getByLabel('thickness_nm', { exact: true }).fill('330');
  await form.getByRole('button', { name: 'Edit as JSON' }).click();
  await expect(form.getByLabel('Problem configuration (JSON)')).toHaveValue(/"thickness_nm": 330/);
  await form.getByRole('button', { name: 'Edit as fields' }).click();
  await form.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(page.getByText('Campaign created. Your workspace is ready.')).toBeVisible();

  expect(commands).toHaveLength(1);
  const payload = commands[0].payload;
  expect(commands[0].operation).toBe('campaign.create');
  expect(payload).toMatchObject({ name: '2D deflector — Octavian FLRL', objective: 'Reproduce the FLRL condition.', compute_budget_seconds: 248400,
    delegated_trial_seconds: 3600, autonomy: 'delegated' });
  expect(payload.tasks).toEqual([{ name: '1050 nm, 75°', split: 'development', problem_id: 'meent_2d_dual_polarization_deflector',
    configuration: { wavelength_nm: 1050, thickness_nm: 330, silicon_index_source: 'FLRL CSV' }, fidelity: { rcwa_order_x: 10, rcwa_order_y: 5 } }]);
});

test('an installed problem is chosen before its fields appear, and the page fits a phone screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await emptyWorkspace(page);
  await page.goto('/');
  const form = await openNewCampaign(page);
  await form.getByRole('radio', { name: 'An installed problem' }).check();
  await expect(form.getByLabel('wavelength_nm', { exact: true })).toHaveCount(0);
  await form.getByLabel('Problem adapter').selectOption('meent_grating');
  await expect(form.getByLabel('wavelength_nm', { exact: true })).toHaveValue('1100');
  await expect(form.getByLabel('material', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('declaring a problem starts from a declaration whose evaluator is built later', async ({ page }) => {
  const { commands } = await emptyWorkspace(page, path => path === '/api/v1/registered-recipes' ? { recipes: [] } : undefined);
  await page.goto('/');
  const form = await openNewCampaign(page);
  await form.getByLabel('Campaign name').fill('Declared');
  await form.getByRole('radio', { name: 'Declare it myself' }).check();
  await expect(form.getByLabel('Problem adapter')).toHaveValue('__commission__');
  await form.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(page.getByText('Campaign created. Your workspace is ready.')).toBeVisible();
  expect(commands[0].payload.tasks[0].evaluator_manifest).toMatchObject({ candidate_schema: { representation: 'continuous' } });
});
