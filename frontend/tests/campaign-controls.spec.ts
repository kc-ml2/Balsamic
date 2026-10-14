import { test, expect } from '@playwright/test';
import { openNewCampaign } from './setupFixtures';
import { writeFile } from 'node:fs/promises';

test('campaign and context commands survive lost replies, refreshes and stale researcher edits', async ({ page, request }, info) => {
  const base = process.env.CAMPAIGN_CONTROLS_TEST_URL;
  test.skip(!base, 'Requires the isolated campaign-controls HTTP fixture.');
  test.setTimeout(120_000);
  const writes: Record<string, any[]> = {};
  const lookups: string[] = [];
  page.on('request', req => {
    if (req.method() === 'GET' && new URL(req.url()).pathname.startsWith('/api/v1/commands/')) lookups.push(req.url().split('/').at(-1)!);
  });
  const lose = new Set(['campaign.create', 'campaign.update', 'context.edit']);
  const revisions: number[] = [];
  let owner = '';
  page.on('response', async response => {
    if (new URL(response.url()).pathname === '/api/state' && response.ok()) {
      try { const state = await response.json(); if (state.campaign?.id === owner) revisions.push(state.campaign.version); } catch { /* Page may close. */ }
    }
  });
  await page.route('**/api/v1/commands', async route => {
    const body = route.request().postDataJSON();
    (writes[body.operation] ||= []).push(body);
    if (lose.delete(body.operation)) {
      const accepted = await route.fetch();
      expect(accepted.ok(), await accepted.text()).toBeTruthy();
      return route.abort('failed');
    }
    return route.continue();
  });
  await page.goto(base!);
  const form = await openNewCampaign(page, { from: 'sidebar', path: 'An installed problem' });
  const dialog = page.getByRole('dialog');
  const name = `Campaign controls ${Date.now()}`;
  await form.getByLabel('Campaign name').fill(name);
  await form.getByLabel('Compute cap (seconds)', { exact: true }).fill('200');
  await form.getByLabel('Validation reserve (seconds)').fill('0');
  await form.getByLabel('API spending cap (USD)').fill('0');
  await form.getByLabel('Problem adapter').selectOption('bounded_continuous');
  await form.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(form.getByText('Failed to fetch', { exact: true })).toBeVisible();
  const createdCommand = writes['campaign.create'][0];
  owner = createdCommand.campaign_id;
  async function state() {
    const response = await request.get(`${base}/api/v1/state?campaign_id=${owner}`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }
  expect((await state()).campaign.id).toBe(owner);
  await expect.poll(() => revisions.includes(1), { timeout: 15_000 }).toBe(true);
  await form.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(form).not.toBeVisible();
  expect(writes['campaign.create']).toHaveLength(1);
  expect(lookups).toContain(createdCommand.id);
  expect((await state()).campaigns.filter((campaign: any) => campaign.name === name)).toHaveLength(1);

  await page.getByRole('button', { name: 'Problem workbench', exact: true }).click();
  await page.getByRole('button', { name: 'Revise charter' }).click();
  await dialog.getByLabel('Compute cap (seconds)', { exact: true }).fill('210');
  await dialog.getByRole('button', { name: 'Save new version' }).click();
  await expect(dialog.getByText('Failed to fetch', { exact: true })).toBeVisible();
  await expect.poll(() => revisions.includes(2), { timeout: 15_000 }).toBe(true);
  await dialog.getByRole('button', { name: 'Save new version' }).click();
  await expect(dialog).not.toBeVisible();
  expect(writes['campaign.update']).toHaveLength(1);
  expect(lookups).toContain(writes['campaign.update'][0].id);
  expect((await state()).campaign.version).toBe(2);

  await page.getByRole('button', { name: 'Campaign memory', exact: true }).click();
  const guidance = page.getByLabel('Campaign guidance (Markdown)');
  await guidance.fill('Preserve the selected baseline and continue within the authorized cap.');
  await page.getByRole('button', { name: 'Save new memory revision' }).click();
  await expect(page.getByText('Failed to fetch', { exact: true })).toBeVisible();
  const editedCommand = writes['context.edit'][0];
  const externalUpdate = await request.post(`${base}/api/v1/commands`, { data: {
    id: `concurrent_${crypto.randomUUID()}`, campaign_id: owner, operation: 'campaign.update', expected_revision: 2,
    payload: { compute_budget_seconds: 220, rationale: 'Concurrent researcher authorization while the acknowledgement is unavailable' },
  } });
  expect(externalUpdate.ok(), await externalUpdate.text()).toBeTruthy();
  await expect.poll(() => revisions.includes(3), { timeout: 15_000 }).toBe(true);
  await page.getByRole('button', { name: 'Save new memory revision' }).click();
  await expect(page.getByText('Failed to fetch', { exact: true })).not.toBeVisible();
  expect(writes['context.edit']).toHaveLength(1);
  expect(lookups).toContain(editedCommand.id);
  expect((await state()).manager_context.guidance_revision).toBe(1);

  // A separate session changes guidance after this editor starts writing.
  await guidance.fill('A stale unsaved direction that must remain editable.');
  const beforeConflict = await state();
  const newer = await request.post(`${base}/api/v1/commands`, { data: {
    id: `guidance_${crypto.randomUUID()}`, campaign_id: owner, operation: 'context.edit', expected_revision: 3,
    payload: { content: 'Current researcher guidance from another session.', expected_revision: beforeConflict.manager_context.revision },
  } });
  expect(newer.ok(), await newer.text()).toBeTruthy();
  await page.getByRole('button', { name: 'Save new memory revision' }).click();
  await expect(page.getByText('Campaign memory changed; inspect the latest revision before saving', { exact: true })).toBeVisible();
  await expect(guidance).toHaveValue('A stale unsaved direction that must remain editable.');
  await expect.poll(async () => (await state()).manager_issues.filter((issue: any) => issue.status === 'pending').length).toBe(1);
  await page.getByRole('button', { name: 'Reload current guidance' }).click();
  await expect(guidance).toHaveValue('Current researcher guidance from another session.', { timeout: 15_000 });
  const manager = page.getByRole('complementary', { name: 'Research conversation' });
  if (!await manager.isVisible()) await page.getByRole('button', { name: 'Campaign manager', exact: true }).click();
  await manager.getByRole('button', { name: 'Defer', exact: true }).click();
  await expect(manager.getByRole('button', { name: 'Defer', exact: true })).not.toBeVisible();
  const final = await state();
  expect(final.manager_issues.every((issue: any) => issue.status === 'deferred')).toBe(true);
  expect(final.trials).toHaveLength(0);
  expect(final.research_runs).toHaveLength(0);
  const accepted = [];
  for (const operation of ['campaign.create', 'campaign.update', 'context.edit', 'issue.resolve']) {
    const command = writes[operation][0];
    const response = await request.get(`${base}/api/v1/commands/${command.id}`);
    expect(response.ok()).toBeTruthy();
    accepted.push(await response.json());
  }
  await page.screenshot({ path: info.outputPath('campaign-context.png'), fullPage: true });
  await writeFile(info.outputPath('campaign-controls-evidence.json'), JSON.stringify({ campaign_id: owner, commands: accepted,
    campaign: final.campaign, context: final.manager_context, issues: final.manager_issues, writes, lookups, observed_revisions: revisions,
    numerical_jobs: 0, model_calls: 0 }, null, 2));
});
