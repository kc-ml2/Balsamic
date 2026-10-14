import { test, expect } from '@playwright/test';
import { emptyWorkspace, openNewCampaign } from './setupFixtures';

const description = 'We are 10 people and two always-on agents sharing one token budget of $500 a month.';
const seeded = (cid: string) => ({ notebook: { id: `formulation_${cid}`, revision: 1 }, outline: [{ op: 'create', name: 'sets' }], active_run: null, latest_run: null,
  limits: { cells: 200, source_characters: 32768, run_seconds: 90 },
  cells: [{ id: 'fcell_problem', name: 'problem', kind: 'markdown', status: 'agreed', revision: 1, notebook_revision: 1, author: 'researcher',
    source: `## The problem in words\n\n${description.replace('$', '\\$')}`, updated_at: '2026-10-14T10:00:00Z', agreed_revision: 1 }] });

test('a campaign starts from a description, kept while it is written, and the lead agent is asked to review it', async ({ page }) => {
  const { commands, state } = await emptyWorkspace(page, path => /^\/api\/campaigns\/[^/]+\/formulation$/.test(path) ? seeded(path.split('/')[3]) : undefined);
  state.agent_runtime = { configuration: { enabled: false }, lead_model: { provider: 'deepseek', model: 'deepseek-v4-pro' }, agents: [], questions: [] };
  await page.goto('/');
  let form = await openNewCampaign(page);
  await expect(page).toHaveURL(/#new-campaign$/);
  await form.getByLabel('Campaign name').fill('Token sharing');
  await form.getByLabel('Describe the problem').fill(description);
  // Nothing typed is lost to a reload before the campaign exists.
  await page.reload();
  form = page.getByRole('form', { name: 'New campaign' });
  await expect(form.getByLabel('Describe the problem')).toHaveValue(description);
  await expect(form.getByLabel('Campaign name')).toHaveValue('Token sharing');
  await expect(form.getByLabel('Compute cap (seconds)', { exact: true })).toBeHidden();
  await form.getByRole('button', { name: 'Create and review' }).click();

  await expect(page).toHaveURL(/#formulation$/);
  await expect(page.getByText('The lead agent was asked to review your description.')).toBeVisible();
  expect(commands.map(command => command.operation)).toEqual(['campaign.create', 'agent.activate', 'agent.message']);
  const created = commands[0].campaign_id;
  expect(commands[0].payload).toMatchObject({ name: 'Token sharing', description, llm_budget_usd: 5 });
  expect(commands[0].payload).not.toHaveProperty('tasks');
  expect(commands[0].payload).not.toHaveProperty('objective');
  expect(commands[2].payload).toMatchObject({ mode: 'steer', message: expect.stringContaining('Review the problem description in cell problem') });
  expect(commands.slice(1).every(command => command.campaign_id === created && command.expected_revision === 1)).toBe(true);
  // The draft is gone once the campaign exists.
  await page.goto('/#new-campaign');
  await expect(page.getByRole('form', { name: 'New campaign' }).getByLabel('Describe the problem')).toHaveValue('');
});

test('without a model only the campaign is created, and the Formulation page offers the review again', async ({ page }) => {
  const { commands, state } = await emptyWorkspace(page, path => /^\/api\/campaigns\/[^/]+\/formulation$/.test(path) ? seeded(path.split('/')[3]) : undefined);
  state.agent_runtime = { configuration: { enabled: false }, lead_model: null, agents: [], questions: [] };
  await page.goto('/');
  const form = await openNewCampaign(page);
  await form.getByLabel('Campaign name').fill('Token sharing');
  await form.getByLabel('API spending cap (USD)').fill('0');
  await form.getByRole('button', { name: 'Create and review' }).click();
  await expect(form.getByRole('alert')).toContainText('Describe the problem first');
  await form.getByLabel('Describe the problem').fill(description);
  await form.getByRole('button', { name: 'Create and review' }).click();
  await expect(form.getByRole('alert')).toContainText('API spending cap above 0');
  expect(commands).toHaveLength(0);
  await form.getByLabel('API spending cap (USD)').fill('1');
  await form.getByRole('button', { name: 'Create and review' }).click();
  await expect(page.getByText('No model is available for a lead agent yet')).toBeVisible();
  expect(commands.map(command => command.operation)).toEqual(['campaign.create']);
  const callout = page.getByRole('note').filter({ hasText: 'Your description is cell' });
  await expect(callout).toContainText('No model is available for a lead agent on this server');
  await expect(callout.getByRole('button', { name: 'Ask the lead agent to review it' })).toBeDisabled();
  await expect(page.getByRole('article', { name: 'Cell problem' })).toContainText('$500 a month');
  // A campaign without a problem shows that it is not defined, not the grating's views.
  await page.goto('/#problem');
  await expect(page.getByText('The problem is not defined yet.')).toBeVisible();
  await expect(page.getByText(/η/)).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Attach a problem' }).first()).toBeVisible();
});

test('documents are attached after the campaign exists, before the review; a refused one stops the review until the researcher decides', async ({ page }) => {
  const calls: string[] = [], attached: string[] = [];
  let refuse = true;
  const { commands, state } = await emptyWorkspace(page, (path, method) => {
    if (/\/formulation$/.test(path)) return seeded(path.split('/')[3]);
    const document = path.match(/\/materials\/(.+)$/);
    if (method === 'PUT' && document) {
      const name = decodeURIComponent(document[1]);
      calls.push(`PUT ${name}`);
      attached.push(name);
      return { name, characters: 120, read: { root: 'materials', path: `${name}.txt` } };
    }
    if (method === 'GET' && path.endsWith('/materials')) return { materials: attached.map((name, index) => ({ id: `material_${index}`, name, characters: 120 })) };
    if (method === 'POST' && path === '/api/v1/commands') calls.push('command');
    return undefined;
  });
  state.agent_runtime = { configuration: { enabled: false }, lead_model: { provider: 'deepseek', model: 'deepseek-v4-pro' }, agents: [], questions: [] };
  await page.goto('/');
  await page.route('**/materials/scan.pdf', route => {
    if (!refuse) return route.fallback();
    calls.push('PUT scan.pdf (refused)');
    return route.fulfill({ status: 409, json: { detail: 'scan.pdf has no extractable text' } });
  });
  const form = await openNewCampaign(page);
  await form.getByLabel('Campaign name').fill('Token sharing');
  await form.getByLabel('Describe the problem').fill(description);
  await form.getByLabel('Documents (optional)').setInputFiles([
    { name: 'notes.md', mimeType: 'text/markdown', buffer: Buffer.from('# Notes\n\nFour million tokens a month.') },
    { name: 'scan.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 image only') }]);
  await expect(form.getByRole('list', { name: 'Documents to attach' })).toContainText('notes.md');
  await form.getByRole('button', { name: 'Create and review' }).click();
  await expect(page).toHaveURL(/#formulation$/);
  // The refused document stops the review: nothing is asked of the lead agent until the researcher decides.
  await expect(page.getByRole('alert').filter({ hasText: 'A document was not attached.' })).toContainText('scan.pdf has no extractable text');
  expect(commands.map(command => command.operation)).toEqual(['campaign.create']);
  expect(calls.filter(call => call.startsWith('PUT'))).toEqual(['PUT notes.md', 'PUT scan.pdf (refused)']);
  await expect(page.getByText('notes.md', { exact: true })).toBeVisible();
  // Try again: this time it is attached, and the review is asked for from the page.
  refuse = false;
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'was not attached' })).toHaveCount(0);
  await expect(page.getByText('scan.pdf', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Ask the lead agent to review it' }).click();
  await expect.poll(() => commands.map(command => command.operation)).toEqual(['campaign.create', 'agent.activate', 'agent.message']);
});
