import { test, expect } from '@playwright/test';
import { openNewCampaign } from './setupFixtures';
import { writeFile } from 'node:fs/promises';

const base = process.env.EXPERIMENT_CONTROLS_TEST_URL;
test.beforeEach(() => test.skip(!base, 'Requires the isolated experiment-controls HTTP fixture.'));

test('a reloaded browser reconciles accepted and unaccepted actions with the original identity', async ({ page, request }, info) => {
  test.setTimeout(90_000);
  const writes: Record<string, any[]> = {}, errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/v1/commands', async route => {
    const body = route.request().postDataJSON();
    (writes[body.operation] ||= []).push(body);
    if (body.operation === 'campaign.create' && writes[body.operation].length === 1) {
      const response = await route.fetch();
      expect(response.ok(), await response.text()).toBeTruthy();
      return route.abort('failed');
    }
    if (body.operation === 'campaign.update' && writes[body.operation].length === 1) return route.abort('failed');
    return route.continue();
  });
  await page.goto(base!);
  const form = await openNewCampaign(page, { from: 'sidebar', path: 'An installed problem' });
  const dialog = page.getByRole('dialog');
  await form.getByLabel('Campaign name').fill(`Reload recovery ${Date.now()}`);
  await form.getByLabel('Compute cap (seconds)', { exact: true }).fill('200');
  await form.getByLabel('Validation reserve (seconds)').fill('0');
  await form.getByLabel('API spending cap (USD)').fill('0');
  await form.getByLabel('Problem adapter').selectOption('bounded_continuous');
  await form.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(form.getByText('Failed to fetch', { exact: true })).toBeVisible();
  const creation = writes['campaign.create'][0];
  const state = async () => (await request.get(`${base}/api/state?campaign_id=${creation.campaign_id}`)).json();
  const identity = (await state()).workspace_id;
  const journal = () => page.evaluate((workspaceId: string) => Object.entries(localStorage)
    .filter(([key]) => key.startsWith(`optimization.commands.v1:${workspaceId}:`)).map(([, value]) => JSON.parse(value)), identity);
  expect((await journal())[0].request).toEqual(creation);
  // An action from another workspace sharing this browser origin is never offered here.
  await page.evaluate(() => localStorage.setItem('optimization.commands.v1:another-workspace:ui_other', JSON.stringify({
    workspace_id: 'another-workspace', signature: 'another request', created_at: new Date().toISOString(),
    request: { id: 'ui_other', operation: 'campaign.create', payload: { name: 'Another workspace only' } },
  })));
  await page.reload();
  const pending = page.getByRole('region', { name: 'Unconfirmed actions' });
  await expect(pending.getByRole('button', { name: 'Check result' })).toHaveCount(1);
  await expect(pending).not.toContainText('Another workspace only');
  await page.screenshot({ path: info.outputPath('reloaded-pending-action.png'), fullPage: true });
  await pending.getByRole('button', { name: 'Check result' }).click();
  await expect(pending.getByRole('status')).toContainText('was recorded');
  expect(writes['campaign.create']).toHaveLength(1);
  expect(await journal()).toEqual([]);
  await pending.getByRole('button', { name: 'Dismiss', exact: true }).click();

  await page.getByRole('button', { name: 'Problem workbench', exact: true }).click();
  await page.getByRole('button', { name: 'Revise charter' }).click();
  await dialog.getByLabel('Compute cap (seconds)', { exact: true }).fill('210');
  await dialog.getByRole('button', { name: 'Save new version' }).click();
  await expect(dialog.getByText('Failed to fetch', { exact: true })).toBeVisible();
  const amendment = writes['campaign.update'][0];
  let failedLookup = false;
  await page.route(`**/api/v1/commands/${amendment.id}`, async route => {
    if (!failedLookup) { failedLookup = true; return route.fulfill({ status: 503, json: { detail: 'Receipt lookup temporarily unavailable' } }); }
    return route.continue();
  });
  await page.reload();
  await pending.getByRole('button', { name: 'Check result' }).click();
  await expect(pending.getByText('Receipt lookup temporarily unavailable', { exact: true })).toBeVisible();
  expect((await journal())[0].request).toEqual(amendment);
  expect(writes['campaign.update']).toHaveLength(1);
  await pending.getByRole('button', { name: 'Check result' }).click();
  await expect(pending.getByRole('button', { name: 'Retry original action' })).toBeVisible();
  await pending.getByRole('button', { name: 'Retry original action' }).click();
  await expect(pending.getByRole('status')).toContainText('was recorded');
  expect(writes['campaign.update']).toHaveLength(2);
  expect(writes['campaign.update'][1]).toEqual(amendment);
  const final = await state();
  expect(final.campaign.version).toBe(2);
  expect(final.campaign.compute_budget_seconds).toBe(210);
  expect(final.trials).toHaveLength(0);
  expect(await journal()).toEqual([]);
  expect(errors).toEqual([]);
  await writeFile(info.outputPath('reload-recovery-evidence.json'), JSON.stringify({ workspace_id: identity, creation, amendment, writes, campaign: final.campaign, errors }, null, 2));
});

test('experiment controls recover replies after reload and keep the frozen procedure', async ({ page, request }, info) => {
  test.setTimeout(90_000);
  const response = await request.post(`${base}/api/campaigns`, { data: { name: `Experiment controls ${Date.now()}`,
    compute_budget_seconds: 150, validation_reserve_seconds: 0, tasks: [{ name: 'Quadratic', problem_id: 'bounded_continuous' }] } });
  expect(response.ok(), await response.text()).toBeTruthy();
  const campaign = await response.json();
  const state = async () => (await request.get(`${base}/api/state?campaign_id=${campaign.id}`)).json();
  const initial = await state();
  const queued = await request.post(`${base}/api/trials`, { data: { campaign_id: campaign.id, task_id: initial.tasks[0].id,
    algorithm: 'coordinate', max_steps: 1_000_000, wall_seconds: 20, question: 'Exercise bounded controls and recovery' } });
  expect(queued.ok(), await queued.text()).toBeTruthy();
  const trial = await queued.json(), writes: any[] = [];
  const methodName = initial.algorithms.find((a: any) => a.id === 'coordinate').name;
  await page.route('**/api/v1/commands', async route => {
    const body = route.request().postDataJSON();
    if (body.operation === 'trial.control') {
      writes.push(body);
      const accepted = await route.fetch();
      expect(accepted.ok(), await accepted.text()).toBeTruthy();
      return route.abort('failed');
    }
    return route.continue();
  });
  try {
    await page.goto(`${base}/#experiments`);
    await page.getByLabel('Active campaign').selectOption(campaign.id);
    await page.getByRole('button', { name: methodName, exact: true }).click();
    let dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Pause', exact: true }).click();
    await expect(dialog.getByText('Failed to fetch', { exact: true })).toBeVisible();
    await page.reload();
    const pending = page.getByRole('region', { name: 'Unconfirmed actions' });
    await pending.getByRole('button', { name: 'Check result' }).click();
    await expect(pending.getByRole('status')).toContainText('was recorded');
    await expect.poll(async () => (await state()).trials[0].status).toBe('paused');
    expect(writes).toHaveLength(1);
    await pending.getByRole('button', { name: 'Dismiss', exact: true }).click();
    await page.getByRole('button', { name: methodName, exact: true }).click();
    await dialog.getByRole('button', { name: 'Extend allocation' }).click();
    dialog = page.getByRole('dialog');
    await dialog.getByLabel('New total evaluation limit').fill('1000003');
    await dialog.getByLabel('New total time cap (seconds)').fill('25');
    await dialog.getByRole('button', { name: 'Update trial', exact: true }).click();
    await expect(dialog.getByText('Failed to fetch', { exact: true })).toBeVisible();
    // Another session advances control state before the browser checks its reply.
    const stop = await request.post(`${base}/api/trials/${trial.id}/control`, { data: { action: 'stop' }, headers: { 'Idempotency-Key': 'final-stop' } });
    expect(stop.ok(), await stop.text()).toBeTruthy();
    await page.reload();
    await pending.getByRole('button', { name: 'Check result' }).click();
    await expect(pending.getByRole('status')).toContainText('was recorded');
    await expect.poll(async () => (await state()).trials[0].status).toBe('stopped');
    expect(writes).toHaveLength(2);
    const final = (await state()).trials[0];
    expect(final.control_revision).toBe(3);
    expect(final.max_steps).toBe(1_000_003);
    expect(final.experiment_spec).toEqual(trial.experiment_spec);
    expect(final.schedule_steps).toBe(trial.schedule_steps);
    const accepted = await Promise.all(writes.map(async body => (await request.get(`${base}/api/v1/commands/${body.id}`)).json()));
    expect(accepted.map(item => item.outcome.control_revision)).toEqual([1, 2]);
    expect(accepted[1].outcome.trial.status).toBe('queued');
    await page.screenshot({ path: info.outputPath('experiment-controls.png'), fullPage: true });
    await writeFile(info.outputPath('experiment-controls-evidence.json'), JSON.stringify({ campaign_id: campaign.id, original: trial, final, commands: accepted }, null, 2));
  } finally {
    await request.post(`${base}/api/trials/${trial.id}/control`, { data: { action: 'stop' }, headers: { 'Idempotency-Key': 'test-cleanup' } });
  }
});

test('saved feedback survives a lost reply and status recovery preserves newer researcher intent', async ({ page, request }, info) => {
  const created = await request.post(`${base}/api/campaigns`, { data: { name: `Feedback recovery ${Date.now()}`,
    tasks: [{ name: 'Quadratic', problem_id: 'bounded_continuous' }] } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const campaign = await created.json();
  const proposed = await request.post(`${base}/api/hypotheses`, { data: { campaign_id: campaign.id, title: 'Feedback recovery idea',
    mechanism: 'A bounded restart rule', rationale: 'Test the stopping assumption', algorithm: 'coordinate' } });
  expect(proposed.ok(), await proposed.text()).toBeTruthy();
  const hypothesis = await proposed.json(), writes: any[] = [], errors: string[] = [];
  const state = async () => (await request.get(`${base}/api/state?campaign_id=${campaign.id}`)).json();
  const initial = await state();
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/v1/commands', async route => {
    const body = route.request().postDataJSON();
    if (['hypothesis.review', 'hypothesis.status'].includes(body.operation)) {
      writes.push(body);
      const accepted = await route.fetch();
      expect(accepted.ok(), await accepted.text()).toBeTruthy();
      return route.abort('failed');
    }
    return route.continue();
  });
  await page.goto(`${base}/#hypotheses`);
  await page.getByLabel('Active campaign').selectOption(campaign.id);
  const openIdea = () => page.getByRole('button', { name: /H\d+.*Feedback recovery idea/ }).click();
  await openIdea();
  const dialog = page.getByRole('dialog'), pending = page.getByRole('region', { name: 'Unconfirmed actions' });
  await dialog.getByLabel('Your feedback', { exact: true }).fill('Keep the original baseline and explain the restart trigger.');
  await dialog.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Failed to fetch');
  await page.reload();
  await pending.getByRole('button', { name: 'Check result' }).click();
  await expect(pending.getByRole('status')).toContainText('was recorded');
  await pending.getByRole('button', { name: 'Dismiss', exact: true }).click();
  await openIdea();
  await expect(dialog.getByText('Keep the original baseline and explain the restart trigger.', { exact: true })).toHaveCount(1);
  await expect(dialog.getByLabel('Your feedback', { exact: true })).toHaveValue('');
  await expect(dialog.getByRole('button', { name: 'Revise with my feedback', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Archive', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Failed to fetch');
  const revived = await request.post(`${base}/api/hypotheses/${hypothesis.id}/status`, { data: { status: 'proposed' },
    headers: { 'Idempotency-Key': 'newer-revive', 'X-Status-Revision': '1' } });
  expect(revived.ok(), await revived.text()).toBeTruthy();
  await page.reload();
  await pending.getByRole('button', { name: 'Check result' }).click();
  await expect(pending.getByRole('status')).toContainText('was recorded');
  await openIdea();
  await expect(dialog.getByRole('button', { name: 'Archive', exact: true })).toBeVisible();
  const final = await state(), idea = final.hypotheses.find((item: any) => item.id === hypothesis.id);
  expect(idea.reviews).toHaveLength(1);
  expect(idea.status_revision).toBe(2);
  expect(idea.status).toBe('proposed');
  expect(final.manager_context.guidance_revision).toBe(initial.manager_context.guidance_revision + 3);
  expect(final.trials).toHaveLength(0);
  expect(final.research_runs).toHaveLength(0);
  expect(writes.map(item => item.operation)).toEqual(['hypothesis.review', 'hypothesis.status']);
  expect(errors).toEqual([]);
  const commands = await Promise.all(writes.map(async body => (await request.get(`${base}/api/v1/commands/${body.id}`)).json()));
  await page.screenshot({ path: info.outputPath('feedback-recovery.png'), fullPage: true });
  await writeFile(info.outputPath('feedback-recovery-evidence.json'), JSON.stringify({ campaign_id: campaign.id, hypothesis: idea, commands,
    guidance_revision: final.manager_context.guidance_revision, new_model_runs: final.research_runs.length, errors }, null, 2));
});
