import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const now = '2026-10-01T00:00:00Z';
function fixture(): any {
  const campaign = { id: 'race-campaign', name: 'Adaptive grating tests', objective: 'Compare methods efficiently',
    active_study_id: 'development-study', version: 2, autonomy: 'delegated', compute_budget_seconds: 200000, llm_budget_usd: 0 };
  const state = { workspace_id: 'race-workspace', campaign, campaigns: [campaign], tasks: [], trials: [], hypotheses: [],
    decisions: [], messages: [], events: [], research_runs: [], algorithms: [], settings: { llm_configured: false },
    studies: [{ id: 'development-study', goal: 'Find reliable algorithms', scope: 'exploratory', comparison: { cost_axis: 'worker_seconds' } }] };
  const race = { id: 'adaptive-race', revision: 3, status: 'running', stage: 'development',
    started_at: now, deadline_at: '2026-10-01T16:00:00Z', batch_deadline_at: '2026-10-01T04:00:00Z',
    elapsed_seconds: 900, total_seconds: 57600, batch_seconds: 14400, max_workers: 4, running_workers: 2,
    worker_seconds_spent: 2700, worker_seconds_cap: 230400, remaining_worker_seconds: 227700,
    configurations: [{ id: 'paper-ppo', algorithm: 'flrl_ppo', status: 'protected_learning', mean_score: 0.32, seeds: [17, 41, 73],
      maturity: { eligible: false, reasons: ['4 / 10 policy updates', 'Episode not complete'] }, trial_ids: ['trial_ppo_17'],
      endpoints: [{ seed: 17, rung_seconds: 900, score: 0.32, eligible: false, training_updates: 4 },
        { seed: 41, rung_seconds: 900, score: 0.2, eligible: false, censored: true }] },
      { id: 'motif', algorithm: 'motif_surgery', status: 'continuing', mean_score: 0.42, seeds: [17, 41, 73],
        maturity: { eligible: true, reasons: [] }, trial_ids: [], endpoints: [{ seed: 17, rung_seconds: 900, score: 0.42, eligible: true }] }],
    decisions: [{ id: 'decision_1', action: 'protect_learning', rationale: 'PPO needs training before screening.', created_at: now, configuration_ids: ['paper-ppo'] }],
    preflight: { numerical_checks: 'passed' }, confirmation: { status: 'not_started' } };
  return { state, race };
}
async function setup(page: Page, data: any, options: { failControl?: boolean; failRead?: boolean } = {}) {
  const writes: any[] = [], raceReads: string[] = [];
  let failure = options.failControl;
  await page.clock.install({ time: new Date(now) });
  await page.addInitScript(() => { (window as any).EventSource = class extends EventTarget { close() {} }; });
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === 'POST') {
      const body = request.postDataJSON(); writes.push(body);
      if (failure) { failure = false; return route.fulfill({ status: 503, json: { detail: 'Temporary network error' } }); }
      data.race.status = body.payload.action === 'pause' ? 'paused' : body.payload.action === 'stop' ? 'stopped' : 'running';
      data.race.revision++;
      return route.fulfill({ json: { status: 'completed', outcome: {} } });
    }
    if (path === '/api/state') return route.fulfill({ json: data.state });
    if (path.endsWith('/race')) {
      raceReads.push(path);
      return options.failRead ? route.fulfill({ status: 503, json: { detail: 'Protocol status temporarily unavailable' } })
        : route.fulfill({ json: { race: data.race } });
    }
    if (path.includes('/commands/')) return route.fulfill({ status: 404, json: { detail: 'Not received' } });
    if (path.endsWith('/assets')) return route.fulfill({ json: [] });
    if (path.endsWith('/study-templates')) return route.fulfill({ json: { templates: [] } });
    if (path.endsWith('/problems')) return route.fulfill({ json: { problems: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto('/#studies');
  return { writes, raceReads };
}

test('shows protocol clocks, activity protection and censored endpoints without starting work', async ({ page }) => {
  const data = fixture(), reads = await setup(page, data);
  const panel = page.getByRole('region', { name: 'Adaptive testing protocol' });
  await expect(panel.getByRole('heading', { name: 'Longer development runs' })).toBeVisible();
  await expect(panel).toContainText('15.0m / 16.0h');
  await expect(panel).toContainText('2 / 4');
  await expect(panel).toContainText('45.0m / 64.0h');
  await expect(panel).toContainText('Undertrained or unassessed');
  await expect(panel).toContainText('4 / 10 policy updates');
  await expect(panel).toContainText('Censored');
  await expect(panel).toContainText('Eligible time endpoint');
  await expect(panel.getByRole('link', { name: /Open run .*ppo_17/ })).toHaveAttribute('href', '#experiments/trial_ppo_17');
  await panel.getByText('Allocation decisions (1)', { exact: true }).click();
  await expect(panel).toContainText('PPO needs training before screening.');
  expect(reads.writes).toEqual([]);
  expect(reads.raceReads.every(path => path === '/api/v1/studies/development-study/race')).toBe(true);
});

test('polls lightweight protocol state independently and uses current revision for controls', async ({ page }) => {
  const data = fixture(), reads = await setup(page, data);
  const panel = page.getByRole('region', { name: 'Adaptive testing protocol' });
  await expect(panel).toContainText('2 / 4');
  data.race.running_workers = 4; data.race.revision = 8;
  await page.clock.fastForward(6000);
  await expect(panel).toContainText('4 / 4');
  await panel.getByRole('button', { name: 'Pause protocol', exact: true }).click();
  await expect(panel.getByRole('button', { name: 'Resume protocol', exact: true })).toBeVisible();
  expect(reads.writes[0]).toMatchObject({ operation: 'study.race.control', payload: { race_id: 'adaptive-race', action: 'pause', expected_revision: 8 } });
  await panel.getByRole('button', { name: 'Resume protocol', exact: true }).click();
  await expect(panel.getByRole('button', { name: 'Pause protocol', exact: true })).toBeVisible();
  expect(reads.writes[1].payload.expected_revision).toBe(9);
  await panel.getByRole('button', { name: 'Stop protocol', exact: true }).click();
  await expect(panel.getByRole('button', { name: 'Pause protocol', exact: true })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: 'Resume protocol', exact: true })).toHaveCount(0);
});

test('a failed control keeps its command identity on retry and preserves visible evidence', async ({ page }) => {
  const data = fixture(), reads = await setup(page, data, { failControl: true });
  const panel = page.getByRole('region', { name: 'Adaptive testing protocol' });
  await panel.getByRole('button', { name: 'Pause protocol', exact: true }).click();
  await expect(panel.getByRole('alert')).toContainText('Temporary network error');
  await expect(panel).toContainText('32.0%');
  await panel.getByRole('button', { name: 'Pause protocol', exact: true }).click();
  await expect(panel.getByRole('button', { name: 'Resume protocol', exact: true })).toBeVisible();
  expect(reads.writes).toHaveLength(2);
  expect(reads.writes[0].id).toBe(reads.writes[1].id);
});

test('a study without a protocol hides the monitor', async ({ page }) => {
  const data = fixture(); data.race = null;
  await setup(page, data);
  await expect(page.getByRole('heading', { name: 'Study history.' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Adaptive testing protocol' })).toHaveCount(0);
});

test('protocol status network errors have a retry control', async ({ page }) => {
  await setup(page, fixture(), { failRead: true });
  const panel = page.getByRole('region', { name: 'Adaptive testing protocol' });
  await expect(panel.getByRole('alert')).toContainText('Protocol status temporarily unavailable');
  await expect(panel.getByRole('button', { name: 'Retry protocol status' })).toBeEnabled();
});
