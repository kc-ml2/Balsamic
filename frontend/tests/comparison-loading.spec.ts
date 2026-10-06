import { test, expect } from '@playwright/test';

test('comparison reads load on request, show progress, coalesce refreshes, and discard a previous cost axis', async ({ page }) => {
  await page.clock.install();
  await page.addInitScript(() => {
    (window as any).EventSource = class extends EventTarget { close() {} };
  });
  const campaign = { id: 'comparison-campaign', name: 'Optimizer comparison', objective: 'Compare mechanisms at matched cost.',
    active_study_id: 'comparison-study', autonomy: 'guided', version: 1, compute_budget_seconds: 3600, llm_budget_usd: 0 };
  const state = { workspace_id: 'comparison-workspace', campaign, campaigns: [campaign],
    tasks: [{ id: 'comparison-task', name: 'Test problem', physics: {}, split: 'development', problem: { definition_id: 'test_problem' } }],
    studies: [{ id: campaign.active_study_id, goal: 'Initial comparison' }], hypotheses: [], trials: [], decisions: [],
    messages: [], events: [], research_runs: [], algorithms: [], settings: { llm_configured: false }, event_cursor: 1 };
  let reads = 0, stateReads = 0;
  const requests: string[] = [];
  const releases = new Map<number, () => void>();
  const report = (algorithm: string) => ({ groups: [{ id: 'comparison-group',
    problem: { definition_id: 'test_problem', primary_objective: { name: 'quality', direction: 'maximize', units: 'score' } },
    common_observed_cost: 1, trials: [{ id: 'comparison-trial', algorithm, seed: 1, method_id: 'test-method', best_objective: 0.8,
      scientific_complete: true, curve: [{ cost: 1, objective: 0.8 }], full_cost: { quantities: {
        worker_seconds: { total: 1 }, evaluation_requests: { total: 10 }, solver_executions: { total: 10 } } } }] }],
    actual_campaign_costs: { quantities: { worker_seconds: { total: 1 }, evaluation_requests: { total: 10 },
      model_calls: { total: 0 }, implementation_seconds: { total: 0 } } } });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    expect(route.request().method()).toBe('GET');
    if (url.pathname === '/api/state') { stateReads++; return route.fulfill({ json: state }); }
    if (url.pathname.endsWith('/comparison')) {
      const index = ++reads;
      requests.push(url.searchParams.get('cost_axis')!);
      await new Promise<void>(resolve => releases.set(index, resolve));
      if (index === 4) return route.fulfill({ status: 503, json: { detail: 'Comparison service temporarily unavailable' } });
      return route.fulfill({ json: report(index < 3 ? `Worker report ${index}` : 'Request report') });
    }
    if (url.pathname.endsWith('/discovery')) return route.fulfill({ json: { sessions: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto('/#comparison');
  // The detailed report reads full provenance, so it loads only on request
  // and workspace updates do not re-read it.
  const initialStateReads = stateReads;
  state.event_cursor++;
  await page.clock.fastForward(6000);
  await expect.poll(() => stateReads).toBeGreaterThan(initialStateReads);
  expect(reads).toBe(0);
  await page.getByRole('button', { name: 'Load matched-cost report' }).click();
  await expect.poll(() => releases.has(1)).toBe(true);
  await expect(page.getByRole('status').filter({ hasText: 'Loading comparison results…' })).toBeVisible();
  await expect(page.getByText('No comparable experiments yet')).not.toBeVisible();

  // Refreshes requested during a slow read produce one follow-up read rather
  // than overlapping requests.
  const refresh = page.getByRole('button', { name: 'Refresh matched-cost report' });
  await refresh.click();
  await refresh.click();
  expect(reads).toBe(1);
  releases.get(1)!();
  await expect.poll(() => releases.has(2)).toBe(true);
  await expect(page.getByRole('cell', { name: /Worker report 1/ })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Updating comparison results…' })).toBeVisible();

  await page.getByLabel('Comparison cost', { exact: true }).selectOption('evaluation_requests');
  await expect(page.getByRole('cell', { name: /Worker report/ })).not.toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Loading comparison results…' })).toBeVisible();
  expect(reads).toBe(2);
  releases.get(2)!();
  await expect.poll(() => releases.has(3)).toBe(true);
  await expect(page.getByRole('cell', { name: /Worker report/ })).not.toBeVisible();
  releases.get(3)!();
  await expect(page.getByRole('cell', { name: /Request report/ })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: /comparison results/ })).not.toBeVisible();
  expect(requests).toEqual(['worker_seconds', 'worker_seconds', 'evaluation_requests']);

  // A refresh keeps the last usable report visible even if it fails.
  await refresh.click();
  await expect.poll(() => releases.has(4)).toBe(true);
  await expect(page.getByRole('cell', { name: /Request report/ })).toBeVisible();
  releases.get(4)!();
  await expect(page.getByText('Comparison service temporarily unavailable')).toBeVisible();
  await expect(page.getByRole('cell', { name: /Request report/ })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: /comparison results/ })).not.toBeVisible();
});
