import { test, expect } from '@playwright/test';
import { emptyWorkspace } from './setupFixtures';

const models = [{ provider: 'openai-codex', id: 'gpt-6-sol', family: 'openai', thinking_levels: ['low', 'xhigh'] },
  { provider: 'deepseek', id: 'deepseek-v4-pro', family: 'deepseek', thinking_levels: ['off', 'high', 'max'] },
  { provider: 'deepseek', id: 'deepseek-v4-flash', family: 'deepseek', thinking_levels: ['off'] }];
const pro = { provider: 'deepseek', model: 'deepseek-v4-pro', effort: null };

test('tiers map one model each, from any family, and roles follow them', async ({ page }) => {
  const saves: any[] = [];
  let view: any = { mode: 'dev', revision: 0, saved: false, providers: {}, models, assignable: ['lead', 'methodology_specialist', 'problem_importer'],
    tiers: [{ id: 'strong', label: 'Strong', model: pro }, { id: 'medium', label: 'Medium', model: pro }],
    roles: { lead: 'strong', methodology_specialist: 'medium', problem_importer: 'strong' } };
  await emptyWorkspace(page, (path, method, body) => {
    if (path !== '/api/v1/model-tiers') return undefined;
    if (method === 'PUT') {
      saves.push(body);
      view = { ...view, ...body, revision: view.revision + 1, saved: true,
        applied: { updated: ['agent-1', 'agent-2'], blocked: [{ agent_id: 'lead-1', role: 'lead', reason: 'family' }] } };
    }
    return view;
  });
  await page.goto('/#models');
  const panel = page.locator('.model-tiers');
  await expect(panel.getByText('Tiers are not saved yet.')).toBeVisible();
  await panel.getByLabel('Strong model').selectOption('openai-codex/gpt-6-sol');
  await panel.getByLabel('Strong thinking level').selectOption('xhigh');
  await panel.getByLabel('Medium model').selectOption('deepseek/deepseek-v4-flash');
  await panel.getByRole('button', { name: 'Add tier' }).click();
  await panel.getByLabel('Tier 3 name').fill('Fast');
  await panel.getByLabel('Problem importer').selectOption('medium');
  await panel.getByLabel('Methodology specialist').selectOption('');
  await panel.getByRole('button', { name: 'Save tiers' }).click();
  await expect(panel.getByText('Saved. 2 agents switch at their next turn. 1 keep their model because their tier is now another family: Lead.')).toBeVisible();
  expect(saves[0]).toEqual({ expected_revision: 0, roles: { lead: 'strong', problem_importer: 'medium' }, tiers: [
    { id: 'strong', label: 'Strong', model: { provider: 'openai-codex', model: 'gpt-6-sol', effort: 'xhigh' } },
    { id: 'medium', label: 'Medium', model: { provider: 'deepseek', model: 'deepseek-v4-flash', effort: null } },
    { id: 'tier_3', label: 'Fast', model: { provider: 'deepseek', model: 'deepseek-v4-flash', effort: null } }] });
  await expect(panel.getByRole('row', { name: /Strong/ }).getByRole('cell').nth(3)).toHaveText('1');
});
