import { test, expect } from '@playwright/test';

test('real API supports browser campaign, simultaneous baseline trials, validation, and charter history', async ({ page, request }) => {
  test.setTimeout(90000);
  const baseURL = process.env.GRATING_LIVE_TEST_URL;
  test.skip(!baseURL, 'Set GRATING_LIVE_TEST_URL to an isolated local service with model calls disabled.');
  const clientErrors: string[] = [];
  page.on('pageerror', error => clientErrors.push(error.message));
  await page.goto(baseURL!);
  await page.getByRole('button', { name: 'New campaign', exact: true }).click();
  const campaignName = `Browser integration ${Date.now()}`;
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Campaign name').fill(campaignName);
  await dialog.getByLabel('Compute cap (seconds)', { exact: true }).fill('180');
  await dialog.getByLabel('Validation reserve (seconds)').fill('30');
  await dialog.getByLabel('Problem adapter').selectOption('meent_grating');
  await dialog.getByRole('button', { name: 'Edit as JSON' }).click();
  await dialog.getByLabel('Problem configuration (JSON)').fill(JSON.stringify({ n_cells: 8, wavelength_nm: 1100, deflection_angle_deg: 50, thickness_nm: 325, material: 'constant', fourier_order: 3 }));
  await dialog.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByLabel('Active campaign')).toContainText(campaignName);
  const stateResponse = await request.get(`${baseURL}/api/state`);
  const state = await stateResponse.json();
  const campaign = state.campaigns.find((c: { name: string }) => c.name === campaignName);
  expect(campaign).toBeTruthy();
  for (const algorithm of ['random', 'hillclimb']) {
    await page.getByRole('button', { name: 'Design an experiment', exact: true }).click();
    await dialog.getByLabel('Optimization strategy').selectOption(algorithm);
    await dialog.getByLabel('Evaluation requests', { exact: true }).fill('100000');
    await dialog.getByLabel('Time cap (seconds)', { exact: true }).fill('15');
    await dialog.getByRole('button', { name: 'Launch experiment' }).click();
    await expect(dialog).not.toBeVisible();
  }
  const readTrials = async () => (await (await request.get(`${baseURL}/api/state?campaign_id=${campaign.id}`)).json()).trials;
  await expect.poll(async () => (await readTrials()).filter((t: { status: string }) => t.status === 'running').length, { timeout: 10000 }).toBe(2);
  const beforeReload = await readTrials();
  const ids = beforeReload.map((t: { id: string }) => t.id).sort();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('heading', { name: 'Follow the evidence.' })).toBeVisible();
  await expect.poll(async () => (await readTrials()).every((t: { id: string; progress: { step: number } }) => t.progress.step > (beforeReload.find((b: { id: string }) => b.id === t.id).progress.step || 0)), { timeout: 7000 }).toBe(true);
  expect((await readTrials()).map((t: { id: string }) => t.id).sort()).toEqual(ids);
  for (const name of ['Uniform random', 'Restart hill climbing']) {
    await page.getByRole('button', { name, exact: true }).click();
    await dialog.getByRole('button', { name: 'Stop', exact: true }).click();
    await dialog.getByRole('button', { name: 'Close dialog' }).click();
  }
  await expect.poll(async () => (await readTrials()).filter((t: { status: string }) => t.status === 'stopped').length, { timeout: 10000 }).toBe(2);
  await page.getByRole('button', { name: 'Uniform random', exact: true }).click();
  await expect(dialog.getByText('stopped', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Check physical convergence', exact: true }).click();
  await dialog.getByLabel('Fourier orders').fill('5, 7');
  await dialog.getByLabel('Number of archived designs').fill('1');
  await dialog.getByLabel('Validation time cap (seconds)').fill('10');
  await dialog.getByRole('button', { name: 'Run validation' }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(async () => {
    const r = await request.get(`${baseURL}/api/state?campaign_id=${campaign.id}`);
    return (await r.json()).trials.find((t: { recipe?: { recipe_id: string } }) => t.recipe?.recipe_id === 'fourier_convergence:v1')?.status;
  }, { timeout: 30000 }).toBe('completed');
  await page.getByRole('button', { name: 'Compare results', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Compare at full upstream cost.' })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('live-comparison.png'), fullPage: true });
  await page.getByRole('button', { name: 'Problem workbench', exact: true }).click();
  await page.getByRole('button', { name: 'Revision history' }).click();
  await expect(dialog.getByRole('heading', { name: 'Charter revision history' })).toBeVisible();
  await expect(dialog.getByLabel('Earlier version')).toContainText('Version 1');
  await expect(dialog.locator('section').filter({ has: page.getByRole('heading', { name: 'Problem definitions', exact: true }) })).toContainText('"n_cells": 8');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Research notebook', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Export research report' })).toHaveAttribute('href', `/api/campaigns/${campaign.id}/export`);
  expect(clientErrors).toEqual([]);
});
