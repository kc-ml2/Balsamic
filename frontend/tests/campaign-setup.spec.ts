import { test, expect } from '@playwright/test';
import { emptyWorkspace } from './setupFixtures';

test('new campaigns start from a chosen example and edit the problem through its declared fields', async ({ page }) => {
  const { commands } = await emptyWorkspace(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Create campaign', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const start = dialog.getByRole('region', { name: 'Starting point' });
  await expect(start.getByRole('button', { name: /1D binary grating/ })).toHaveAttribute('aria-pressed', 'true');
  await expect(dialog.getByLabel('n_cells', { exact: true })).toHaveValue('64');
  await expect(dialog.getByLabel('material', { exact: true })).toHaveValue('constant');
  await expect(start.getByRole('button', { name: /Import from documents/ })).toBeVisible();

  await start.getByRole('button', { name: /Octavian FLRL/ }).click();
  await expect(dialog.getByLabel('Campaign name')).toHaveValue('2D deflector — Octavian FLRL');
  await expect(dialog.getByLabel('Compute cap (seconds)', { exact: true })).toHaveValue('248400');
  await expect(dialog.getByLabel('Problem adapter')).toHaveValue('meent_2d_dual_polarization_deflector');
  await expect(dialog.getByLabel('rcwa_order_x', { exact: true })).toHaveValue('10');
  await dialog.getByLabel('thickness_nm', { exact: true }).fill('330');
  await dialog.getByRole('button', { name: 'Edit as JSON' }).click();
  await expect(dialog.getByLabel('Problem configuration (JSON)')).toHaveValue(/"thickness_nm": 330/);
  await dialog.getByRole('button', { name: 'Edit as fields' }).click();
  await dialog.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(dialog).not.toBeVisible();

  const payload = commands[0].payload;
  expect(commands[0].operation).toBe('campaign.create');
  expect(payload).toMatchObject({ name: '2D deflector — Octavian FLRL', objective: 'Reproduce the FLRL condition.', compute_budget_seconds: 248400,
    delegated_trial_seconds: 3600, autonomy: 'delegated' });
  expect(payload.tasks).toEqual([{ name: '1050 nm, 75°', split: 'development', problem_id: 'meent_2d_dual_polarization_deflector',
    configuration: { wavelength_nm: 1050, thickness_nm: 330, silicon_index_source: 'FLRL CSV' }, fidelity: { rcwa_order_x: 10, rcwa_order_y: 5 } }]);
});

test('a blank problem uses the adapter defaults and the dialog fits a phone screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await emptyWorkspace(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Create campaign', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: /Blank problem/ }).click();
  await expect(dialog.getByLabel('Problem adapter')).toHaveValue('meent_grating');
  await expect(dialog.getByLabel('wavelength_nm', { exact: true })).toHaveValue('1100');
  await expect(dialog.getByLabel('material', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
