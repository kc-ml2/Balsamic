import { test, expect } from '@playwright/test';
import { emptyWorkspace } from './setupFixtures';

const objective = 'Design a deflector. Px = 1050/sin(75 degrees) = 1087.0399894305872 nm and Py = 525 nm. '
  + 'The reference code is at commit 7838e71313d71cee8e2db3b432f41f80b9106a95 and uses MEENT 0.13.2. Validate convergence first.';
const campaign = { id: 'c1', name: 'FLRL deflector', objective, compute_budget_seconds: 3600, llm_budget_usd: 5, autonomy: 'guided', version: 1, active_study_id: 's1' };
const state = { workspace_id: 'w', campaigns: [campaign], campaign, hypotheses: [], trials: [], decisions: [], messages: [], events: [], research_runs: [], algorithms: [],
  settings: { llm_configured: true }, event_cursor: 1, studies: [{ id: 's1', goal: objective, scope: 'exploratory' }],
  tasks: [{ id: 't1', name: '1050 nm', physics: {}, split: 'development', problem: { definition_id: 'meent_2d', primary_objective: { name: 'mean', direction: 'maximize', units: 'fraction' } } }] };

test('the overview shows the charter once, readable, with exact values on hover', async ({ page }) => {
  await emptyWorkspace(page, path => path === '/api/state' ? state : undefined);
  await page.goto('/#overview');
  const heading = page.locator('.page-heading');
  await expect(page.getByRole('heading', { name: 'Active study' })).toHaveCount(0);
  await expect(heading.getByText('1087.04', { exact: true })).toHaveAttribute('title', '1087.0399894305872');
  await expect(heading.getByText(/commit/)).toHaveCount(0);
  await heading.getByRole('button', { name: 'Show full charter' }).click();
  await expect(heading.getByText('7838e71', { exact: true })).toHaveAttribute('title', '7838e71313d71cee8e2db3b432f41f80b9106a95');
  await expect(heading).toContainText('MEENT 0.13.2');
  await expect(page.getByText(objective)).toHaveCount(0);
});

