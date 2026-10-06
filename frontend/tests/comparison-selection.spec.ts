import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

// The matched-cost report loads on request, including after a reload.
async function loadReport(page: Page) {
  await page.getByRole('button', { name: 'Load matched-cost report' }).click();
  await expect(page.getByRole('table', { name: 'Individual run results' })).toBeVisible();
}

async function comparisonWorkspace(page: Page, direction = 'maximize') {
  await page.addInitScript(() => { (window as any).EventSource = class extends EventTarget { close() {} }; });
  const campaign = { id: 'finalist-campaign', name: 'Optimizer selection', objective: 'Compare search procedures',
    active_study_id: 'development-study', autonomy: 'guided', version: 1, compute_budget_seconds: 3600, llm_budget_usd: 0 };
  const state: any = { workspace_id: 'finalist-workspace', campaign, campaigns: [campaign],
    tasks: [{ id: 'test-task', name: 'Scientific test problem', physics: {}, split: 'development', problem: { definition_id: 'test_problem' } }],
    studies: [{ id: campaign.active_study_id, goal: 'Development comparisons', scope: 'exploratory' }], hypotheses: [], trials: [], decisions: [],
    messages: [], events: [], research_runs: [], algorithms: [], settings: { llm_configured: false }, event_cursor: 1, finalist_selections: [] };
  const method = (patience: number) => ({ algorithm: 'hillclimb', parameters: { restart_patience: patience, neighborhood: 'permuted', accept_equal: false },
    training: {}, schedule: { steps: 1536 }, implementation: 'same-code-digest', runtime: { python: '3.12' }, initial_assets: [] });
  const trial = (id: string, methodId: string, seed: number, score: number | null, patience: number, cost: number | null) => ({ id, seed, method_id: methodId,
    algorithm: 'hillclimb', hypothesis_title: 'Restart hill climbing', method: method(patience), best_objective: score,
    scientific_complete: score !== null, status: score === null ? 'failed' : 'completed', finalist_eligible: score !== null,
    finalist_ineligible_reason: score === null ? 'Complete the optimizer experiment first.' : '',
    procedure_id: `procedure-${patience}`, procedure: { algorithm: 'hillclimb', algorithm_config: { restart_patience: patience },
      max_steps: 1536, schedule_steps: 1536, wall_seconds: 45, completion: { unit: 'evaluation_requests', count: 1536 } },
    question: `Try restart patience ${patience} with multiple seeds.`, curve: score === null ? [] : [{ cost: cost || 1, objective: score }],
    full_cost: { quantities: { worker_seconds: { total: cost }, evaluation_requests: { total: cost === null ? null : 1536 }, solver_executions: { total: cost === null ? null : 1536 } } } });
  const trials = [trial('trial-a1', '386c732e3a', 11, .91, 16, 10), trial('trial-b1', '0cb4fd1ed0', 21, .80, 32, 12),
    trial('trial-a2', '386c732e3a', 12, .89, 16, 11), trial('trial-missing', 'missing-method', 31, null, 64, null)];
  const report = { groups: [{ id: 'compatible-group', problem: { definition_id: 'test_problem', primary_objective: { name: 'quality', direction, units: 'score' } },
    common_observed_cost: 8, trials, method_summaries: [
      { method_id: '386c732e3a', mean: .82, observed_count: 2, trial_count: 2 },
      { method_id: '0cb4fd1ed0', mean: .88, observed_count: 1, trial_count: 1 },
      { method_id: 'missing-method', mean: null, observed_count: 0, trial_count: 1 }] }],
    actual_campaign_costs: { quantities: { worker_seconds: { total: 33 }, evaluation_requests: { total: 4608 }, model_calls: { total: 0 }, implementation_seconds: { total: 0 } } } };
  const commands: any[] = [];
  let reject = false;
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/state') return route.fulfill({ json: state });
    if (url.pathname.endsWith('/comparison')) return route.fulfill({ json: report });
    if (url.pathname === '/api/v1/commands' && route.request().method() === 'POST') {
      const request = route.request().postDataJSON();
      commands.push(request);
      expect(request.operation).toBe('finalist.set');
      if (reject) return route.fulfill({ status: 409, json: { detail: 'Finalist shortlist changed; refresh before replacing it.' } });
      expect(request.payload.expected_revision).toBe(state.finalist_selections[0]?.revision || 0);
      const ids = request.payload.trial_ids;
      state.finalist_selections = [{ id: 'shortlist', campaign_id: campaign.id, study_id: campaign.active_study_id,
        trial_ids: ids, prototype_trial_ids: ids.filter((id: string, index: number) => ids.findIndex((other: string) =>
          trials.find(t => t.id === id)?.procedure_id === trials.find(t => t.id === other)?.procedure_id) === index),
        revision: (state.finalist_selections[0]?.revision || 0) + 1 }];
      state.event_cursor++;
      return route.fulfill({ json: { outcome: { finalist_selection: state.finalist_selections[0] } } });
    }
    if (url.pathname.endsWith('/discovery')) return route.fulfill({ json: { sessions: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto('/#comparison');
  await loadReport(page);
  return { state, report, commands, rejectNext: () => { reject = true; } };
}

test('results sort numerically, compare parameter differences, and group at matched cost', async ({ page }) => {
  await comparisonWorkspace(page);
  const table = page.getByRole('table', { name: 'Individual run results' });
  const ids = () => table.locator('tbody tr').evaluateAll(rows => rows.map(row => row.getAttribute('data-trial-id')));
  expect(await ids()).toEqual(['trial-a1', 'trial-a2', 'trial-b1', 'trial-missing']);
  await page.getByRole('button', { name: 'Best observed endpoint' }).click();
  expect(await ids()).toEqual(['trial-b1', 'trial-a2', 'trial-a1', 'trial-missing']);
  await page.getByRole('button', { name: 'Full cost' }).click();
  expect(await ids()).toEqual(['trial-a1', 'trial-a2', 'trial-b1', 'trial-missing']);
  await page.getByRole('button', { name: 'Full cost' }).click();
  expect(await ids()).toEqual(['trial-b1', 'trial-a2', 'trial-a1', 'trial-missing']);
  await page.getByLabel('Compare run trial-a1', { exact: true }).check();
  await page.getByLabel('Compare run trial-b1', { exact: true }).check();
  await page.getByRole('button', { name: 'Inspect / compare selected' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading', { name: 'Compare method details' })).toBeVisible();
  const difference = dialog.locator('[data-diff-field="method.parameters.restart_patience"]');
  await expect(difference).toContainText('16');
  await expect(difference).toContainText('32');
  await expect(dialog.locator('[data-diff-field="method.runtime.python"]')).toHaveCount(0);
  await dialog.getByLabel('Show differing method and procedure fields only').uncheck();
  await expect(dialog.locator('[data-diff-field="method.runtime.python"]')).toContainText('3.12');
  await expect(dialog.locator('[data-diff-field="procedure.wall_seconds"]')).toContainText('45');
  await dialog.getByRole('button', { name: 'Close dialog' }).click();
  await page.getByLabel('Results table', { exact: true }).selectOption('methods');
  await page.getByRole('button', { name: 'Mean at matched cost' }).click();
  const groups = page.getByRole('table', { name: 'Method group results' });
  await expect(groups.locator('tbody tr').first()).toHaveAttribute('data-method-id', '0cb4fd1ed0');
  await expect(groups.locator('tbody tr[data-method-id="386c732e3a"]')).toContainText('0.82');
  await expect(groups.locator('tbody tr[data-method-id="386c732e3a"]')).toContainText('2 runs');
});

test('minimization sorts lowest observed result first with missing results last', async ({ page }) => {
  await comparisonWorkspace(page, 'minimize');
  const rows = page.getByRole('table', { name: 'Individual run results' }).locator('tbody tr');
  await expect(rows.first()).toHaveAttribute('data-trial-id', 'trial-b1');
  await expect(rows.last()).toHaveAttribute('data-trial-id', 'trial-missing');
});

test('run and method group finalists persist, deduplicate prototypes, and can be unmarked', async ({ page }) => {
  const { state, commands } = await comparisonWorkspace(page);
  await page.getByLabel('Compare run trial-a1', { exact: true }).check();
  await page.getByLabel('Compare run trial-b1', { exact: true }).check();
  await page.getByRole('button', { name: 'Mark selected as finalists' }).click();
  await expect(page.getByLabel('Finalist shortlist')).toContainText('2 finalist runs marked · 2 distinct confirmation procedures');
  expect(state.finalist_selections[0].trial_ids).toEqual(['trial-a1', 'trial-b1']);
  expect(commands[0].payload.expected_procedure_ids).toEqual({ 'trial-a1': 'procedure-16', 'trial-b1': 'procedure-32' });
  await page.reload();
  await loadReport(page);
  await expect(page.locator('tr[data-trial-id="trial-a1"]').getByRole('button', { name: 'Unmark finalist' })).toBeVisible();
  await page.locator('tr[data-trial-id="trial-a1"]').getByRole('button', { name: 'Unmark finalist' }).click();
  await expect(page.getByLabel('Finalist shortlist')).toContainText('1 finalist runs marked');
  await page.getByLabel('Results table', { exact: true }).selectOption('methods');
  await page.locator('tr[data-method-id="386c732e3a"]').getByRole('button', { name: 'Mark method group' }).click();
  await expect(page.getByLabel('Finalist shortlist')).toContainText('3 finalist runs marked · 2 distinct confirmation procedures');
  expect(new Set(state.finalist_selections[0].trial_ids)).toEqual(new Set(['trial-a1', 'trial-a2', 'trial-b1']));
  await page.locator('tr[data-method-id="386c732e3a"]').getByRole('button', { name: 'Unmark group' }).click();
  await expect(page.getByLabel('Finalist shortlist')).toContainText('1 finalist runs marked');
  await page.getByRole('button', { name: 'Clear finalist shortlist' }).click();
  await expect(page.getByLabel('Finalist shortlist')).toContainText('0 finalist runs marked');
  expect(commands).toHaveLength(5);
  expect(commands.every(command => command.operation === 'finalist.set')).toBe(true);
});

test('a rejected shortlist write keeps the existing saved selection visible', async ({ page }) => {
  const workspace = await comparisonWorkspace(page);
  workspace.rejectNext();
  await page.locator('tr[data-trial-id="trial-a1"]').getByRole('button', { name: 'Mark finalist' }).click();
  await expect(page.getByRole('alert')).toContainText('Finalist shortlist changed; refresh before replacing it.');
  await expect(page.getByLabel('Finalist shortlist')).toContainText('0 finalist runs marked');
  await expect(page.locator('tr[data-trial-id="trial-a1"]').getByRole('button', { name: 'Mark finalist' })).toBeEnabled();
  expect(workspace.state.finalist_selections).toHaveLength(0);
});

test('a method group with an ineligible replicate can still be marked and unmarked', async ({ page }) => {
  const workspace = await comparisonWorkspace(page);
  workspace.report.groups[0].trials.find(trial => trial.id === 'trial-a2')!.finalist_eligible = false;
  await page.reload();
  await loadReport(page);
  await page.getByLabel('Results table', { exact: true }).selectOption('methods');
  const group = page.locator('tr[data-method-id="386c732e3a"]');
  await group.getByRole('button', { name: 'Mark method group' }).click();
  await expect(page.getByLabel('Finalist shortlist')).toContainText('1 finalist runs marked');
  expect(workspace.state.finalist_selections[0].trial_ids).toEqual(['trial-a1']);
  await group.getByRole('button', { name: 'Unmark group' }).click();
  await expect(page.getByLabel('Finalist shortlist')).toContainText('0 finalist runs marked');
});

test('method details retain differing procedure source and typed parameter values', async ({ page }) => {
  const workspace = await comparisonWorkspace(page);
  const a: any = workspace.report.groups[0].trials[0];
  const b: any = workspace.report.groups[0].trials[1];
  a.procedure.scientific_source_hash = 'source-a';
  b.procedure.scientific_source_hash = 'source-b';
  b.method.parameters.restart_patience = '16';
  await page.reload();
  await loadReport(page);
  await page.getByLabel('Compare run trial-a1', { exact: true }).check();
  await page.getByLabel('Compare run trial-b1', { exact: true }).check();
  await page.getByRole('button', { name: 'Inspect / compare selected' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.locator('[data-diff-field="procedure.scientific_source_hash"]')).toContainText('source-a');
  await expect(dialog.locator('[data-diff-field="procedure.scientific_source_hash"]')).toContainText('source-b');
  const typed = dialog.locator('[data-diff-field="method.parameters.restart_patience"]');
  await expect(typed.locator('td').nth(0)).toHaveText('16');
  await expect(typed.locator('td').nth(1)).toHaveText('"16"');
});
