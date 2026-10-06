import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

const now = '2026-10-02T02:10:00Z';
function fixture(): any {
  const campaigns = [{ id: 'campaign-a', name: 'Grating campaign', objective: 'Reliable methods', autonomy: 'delegated', compute_budget_seconds: 248400, llm_budget_usd: 0 },
    { id: 'campaign-b', name: 'Another campaign', objective: 'Other methods', autonomy: 'delegated', compute_budget_seconds: 7200, llm_budget_usd: 0 }];
  const state = { workspace_id: 'resource-workspace', campaign: campaigns[0], campaigns, tasks: [], trials: [], hypotheses: [], decisions: [], messages: [], events: [], research_runs: [], algorithms: [], settings: { llm_configured: false } };
  const snapshot = { sampled_at: now, campaign_id: 'campaign-a', host: { cpu: { logical_count: 32, capacity_cores: 32, utilization_percent: 2.5, load_average: [0.6, 0.7, 0.8] },
    memory: { total_bytes: 32 * 1024 ** 3, used_bytes: 14.4 * 1024 ** 3, available_bytes: 17.6 * 1024 ** 3 }, gpu: { status: 'not_used', devices: [] },
    processes: [{ pid: 1234, name: 'python', role: 'workspace_service', rss_bytes: 7.5 * 1024 ** 3, threads: 12 }] },
    workers: { configured_limit: 4, running_count: 0, queued_count: 0, jobs: [] },
    budget: { actual_seconds: 14734, allocated_seconds: 18934, limit_seconds: 248400, remaining_seconds: 229466, grants: [] },
    plans: [{ race_id: 'race-a', status: 'budget_exhausted', stage: 'numerical_checks', ended: true, deadline_at: '2026-10-01T22:39:17Z', elapsed_seconds: 57600, total_seconds: 57600,
      worker_seconds_spent: 1774.36, worker_seconds_cap: 230400, pending_jobs: 24, blocked_reason: 'RCWA convergence is unresolved. No optimizer trials were admitted.',
      memory_forecast: { fidelity: { rcwa_order_x: 22, rcwa_order_y: 11 }, predicted_bytes: 26.86 * 1024 ** 3, headroom_bytes: 4 * 1024 ** 3, available_bytes: 17.6 * 1024 ** 3,
        historical_available_bytes: 17.64 * 1024 ** 3, historical_fits: false, fits_now: false, observed_at: '2026-10-01T07:40:04Z', measured_peak_bytes: 6.2 * 1024 ** 3,
        measured_fidelity: { rcwa_order_x: 18, rcwa_order_y: 9 }, measured_at: '2026-10-01T07:37:46Z', safety_factor: 2, basis: 'Measured peak × harmonic-count squared × 2 safety factor' },
      memory_checks: [{ fidelity: { rcwa_order_x: 22, rcwa_order_y: 11 }, harmonic_count: 1035, single_matrix_bytes: 1035 ** 2 * 16,
        predicted_bytes: 26.86 * 1024 ** 3, headroom_bytes: 4 * 1024 ** 3, fits_now: false, basis: 'Estimated from completed order (18, 9)', expanded_grid_shape: { x: 23040, y: 5888 }, expanded_complex_grid_bytes: 23040 * 5888 * 16, fft_workspace_lower_bound_bytes: 5.102 * 1024 ** 3 }],
      phases: [{ name: 'numerical_checks', state: 'unresolved', jobs: 9, worker_seconds: 1774.36, threads: 1, concurrency: 1, estimate_basis: 'Serial protected validation' },
        { name: 'development', state: 'not_started', jobs: 24, worker_seconds: 21600, threads: 1, concurrency: 2, estimate_basis: '8 configurations × 3 seeds × 900 seconds' }],
      decisions: [{ action: 'pause', rationale: 'The next common fidelity did not fit the resource forecast.', created_at: '2026-10-01T07:40:04Z' }] }], warnings: [] };
  return { state, snapshot };
}
async function setup(page: Page, data: any, resource: (route: Route, campaign: string | null) => Promise<void> = async (route, campaign) => {
  const value = campaign ? data.snapshot : { ...data.snapshot, campaign_id: null, budget: null, plans: [] };
  await route.fulfill({ json: value });
}) {
  const writes: string[] = [], reads: string[] = [];
  await page.clock.install({ time: new Date(now) });
  await page.addInitScript(() => { (window as any).EventSource = class extends EventTarget { close() {} }; });
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== 'GET') { writes.push(request.method()); return route.fulfill({ status: 405, json: { detail: 'Read-only dashboard' } }); }
    if (url.pathname === '/api/state') {
      const id = url.searchParams.get('campaign_id');
      return route.fulfill({ json: id ? { ...data.state, campaign: data.state.campaigns.find((campaign: any) => campaign.id === id) } : data.state });
    }
    if (url.pathname === '/api/v1/resources') { reads.push(url.searchParams.get('campaign_id') || 'host'); return resource(route, url.searchParams.get('campaign_id')); }
    return route.fulfill({ json: {} });
  });
  await page.goto('/#resources');
  return { writes, reads };
}

test('shows idle expired campaign and separates measured solver peak from blocked memory forecast', async ({ page }) => {
  const data = fixture();
  data.state.research_progress = { status: 'running', headline: 'Campaign manager is working', message: 'A lengthy agent task is in progress.', active: true,
    session: { id: 'discovery-running', status: 'running', control_revision: 1 }, task_counts: { total: 20, running: 2, completed: 18 },
    agents: [{ task_id: 'method-task', role: 'methodology_specialist', stage: 'implementation', status: 'running', active: true, activity: 'Building and checking candidate algorithms.' }] };
  const calls = await setup(page, data);
  const dashboard = page.getByLabel('Resource dashboard', { exact: true });
  await expect(dashboard.getByRole('heading', { name: 'Resources', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Campaign research progress' })).toHaveCount(0);
  expect((await dashboard.getByRole('heading', { name: 'Resources', exact: true }).boundingBox())!.y).toBeLessThan(200);
  await expect(dashboard).toContainText('No numerical jobs running');
  await expect(dashboard).toContainText('Deadline expired · no further work admitted');
  await expect(dashboard).toContainText('29.6m / 64.0h');
  await expect(dashboard).toContainText('Forecast, not measured usage');
  await expect(dashboard).toContainText('26.9 GiB + 4.0 GiB');
  await expect(dashboard).toContainText('Insufficient memory forecast');
  await expect(dashboard).toContainText('6.2 GiB at RCWA (18, 9)');
  await expect(dashboard).toContainText('It is not an isolated solver measurement or a measurement at order (22, 11)');
  await expect(dashboard).toContainText('23040 × 5888 · 2.0 GiB per complex array');
  await expect(page.getByRole('region', { name: 'Largest host memory consumers' })).toContainText('7.5 GiB');
  await expect(page.getByRole('region', { name: 'Campaign resource allowance' })).toContainText('1.2h');
  await expect(dashboard.getByRole('button')).toHaveCount(1);
  expect(calls.writes).toEqual([]);
});

test('a historical memory rejection can fit current host memory without suggesting an expired protocol resumed', async ({ page }) => {
  const data = fixture(); data.snapshot.host.memory.available_bytes = 31 * 1024 ** 3;
  data.snapshot.plans[0].memory_forecast.available_bytes = 31 * 1024 ** 3;
  data.snapshot.plans[0].memory_forecast.fits_now = true;
  await setup(page, data);
  const plan = page.getByRole('article', { name: 'Resource plan race-a' });
  await expect(plan).toContainText('Available now31.0 GiB');
  await expect(plan).toContainText('17.6 GiB was available when the decision was made · insufficient memory forecast');
  await expect(plan).toContainText('Fits current memory forecast · protocol admission still required');
  await expect(plan).toContainText('Deadline expired');
});

test('polling keeps a visible stale sample on failure and retry recovers without writing commands', async ({ page }) => {
  const data = fixture(); let failing = false;
  const calls = await setup(page, data, async (route) => {
    await route.fulfill(failing ? { status: 503, json: { detail: 'Resource sampling temporarily unavailable' } } : { json: data.snapshot });
  });
  const dashboard = page.getByLabel('Resource dashboard', { exact: true });
  await expect(dashboard).toContainText('26.9 GiB + 4.0 GiB');
  failing = true;
  await page.clock.fastForward(5000);
  await expect(dashboard.getByRole('alert')).toContainText('Resource sampling temporarily unavailable');
  await expect(dashboard).toContainText(/Stale sample · [\d.]+s old/);
  await expect(dashboard).toContainText('26.9 GiB + 4.0 GiB');
  failing = false; data.snapshot.sampled_at = '2026-10-02T02:10:05Z'; data.snapshot.host.cpu.utilization_percent = 48.5;
  await dashboard.getByRole('button', { name: 'Retry resource status' }).click();
  await expect(dashboard.getByRole('alert')).toHaveCount(0);
  await expect(dashboard).toContainText('48.5%');
  await expect(dashboard.getByRole('img', { name: 'Host CPU utilization history, 2 measured samples' })).toBeVisible();
  expect(calls.writes).toEqual([]);
});

test('campaign switch aborts a pending read and cannot show the previous campaign response', async ({ page }) => {
  const data = fixture(); let hold = false, release: () => void = () => {};
  const calls = await setup(page, data, async (route, campaign) => {
    if (campaign === 'campaign-a' && hold) { await new Promise<void>(resolve => { release = resolve; }); }
    const snapshot = campaign === 'campaign-b' ? { ...data.snapshot, campaign_id: campaign, plans: [{ ...data.snapshot.plans[0], race_id: 'race-b', blocked_reason: 'Another campaign has no numerical evidence yet.' }] } : data.snapshot;
    await route.fulfill({ json: snapshot }).catch(() => {});
  });
  const dashboard = page.getByLabel('Resource dashboard', { exact: true });
  await expect(dashboard).toContainText('race-a');
  hold = true; await page.clock.fastForward(5000);
  await expect.poll(() => calls.reads.filter(campaign => campaign === 'campaign-a').length).toBeGreaterThanOrEqual(2);
  await page.getByRole('combobox', { name: 'Active campaign' }).selectOption('campaign-b');
  await expect(dashboard).toContainText('race-b');
  release();
  await expect(dashboard).not.toContainText('race-a');
  await expect(dashboard).toContainText('Another campaign has no numerical evidence yet.');
  expect(calls.writes).toEqual([]);
});

test('slow resource requests do not overlap, and host monitoring works without a campaign', async ({ page }) => {
  const data = fixture(); data.state.campaign = null; data.state.campaigns = [];
  const pending: (() => void)[] = [];
  const calls = await setup(page, data, async route => {
    await new Promise<void>(resolve => { pending.push(resolve); });
    await route.fulfill({ json: { ...data.snapshot, campaign_id: null, budget: null, plans: [] } }).catch(() => {});
  });
  await expect(page.getByRole('button', { name: 'Refresh resources' })).toBeDisabled();
  const initialReads = calls.reads.length;
  await page.clock.fastForward(7000);
  expect(calls.reads).toHaveLength(initialReads);
  expect(calls.reads.every(campaign => campaign === 'host')).toBe(true);
  pending.forEach(resolve => resolve());
  const dashboard = page.getByLabel('Resource dashboard', { exact: true });
  await expect(dashboard).toContainText('No campaign selected. Host measurements remain available');
  await expect(page.getByRole('region', { name: 'Current host usage' })).toContainText('17.6 GiB available');
  expect(calls.writes).toEqual([]);
});

test('resource dashboard stays readable on a narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, fixture());
  await expect(page.getByRole('heading', { name: 'Resources', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Planned resource usage' })).toContainText('Insufficient memory forecast');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('live numeric timestamps and structured Fourier grids render without crashing', async ({ page }) => {
  const data = fixture();
  data.snapshot.plans[0].deadline_at = Date.parse(data.snapshot.plans[0].deadline_at) / 1000;
  data.snapshot.plans[0].memory_forecast.observed_at = 1790839804;
  data.snapshot.plans[0].memory_forecast.measured_at = 1790839666;
  data.snapshot.host.memory.effective_available_bytes = 9 * 1024 ** 3;
  data.snapshot.host.memory.swap_total_bytes = 16 * 1024 ** 3;
  data.snapshot.host.memory.swap_used_bytes = 3.5 * 1024 ** 3;
  const exceptions: string[] = []; page.on('pageerror', error => exceptions.push(error.message));
  await setup(page, data);
  const dashboard = page.getByLabel('Resource dashboard', { exact: true });
  await expect(dashboard).toContainText('Deadline expired');
  await expect(dashboard).toContainText('9.0 GiB available to new work');
  await expect(dashboard).toContainText('Swap: 3.5 GiB used / 16.0 GiB total');
  await expect(dashboard).toContainText('Known Fourier workspace floor 5.1 GiB');
  expect(exceptions).toEqual([]);
});

test('running and queued jobs show measurements, allowances, and GPU readings without creating work', async ({ page }) => {
  const data = fixture();
  data.snapshot.workers.running_count = 1; data.snapshot.workers.queued_count = 1;
  data.snapshot.workers.jobs = [{ id: 'trial-running', kind: 'trial', algorithm: 'meent_adam', seed: 17, status: 'running', pid: 5678, process_state: 'alive', rss_bytes: 3 * 1024 ** 3,
    peak_rss_bytes: 4 * 1024 ** 3, cpu_percent: 180, threads: 2, wall_seconds: 900, spent_seconds: 120, remaining_seconds: 780 },
    { id: 'trial-queued', kind: 'trial', status: 'queued', process_state: 'not_started', rss_bytes: null, peak_rss_bytes: null, cpu_percent: null, threads: null, wall_seconds: 900, spent_seconds: 0, remaining_seconds: 900 }];
  data.snapshot.host.gpu = { status: 'available', devices: [{ id: 'card1', vendor: 'AMD', utilization_percent: 0, memory_used_bytes: 2 * 1024 ** 3, memory_total_bytes: 32 * 1024 ** 3, memory_kind: 'Shared host allocations may be additional.' }] };
  const calls = await setup(page, data);
  const jobs = page.getByRole('region', { name: 'Current jobs', exact: true });
  await expect(jobs).toContainText('3.0 GiB / 4.0 GiB');
  await expect(jobs).toContainText('meent adam · seed 17');
  await expect(jobs).toContainText('180.0%');
  await expect(jobs).toContainText('15.0m allowance');
  await expect(jobs).toContainText('not started · no process');
  await expect(jobs.getByRole('link', { name: 'trial-running' })).toHaveAttribute('href', '#experiments/trial-running');
  await expect(page.getByLabel('Resource dashboard', { exact: true })).toContainText('2.0 GiB / 32.0 GiB driver memory');
  expect(calls.writes).toEqual([]);
});

test('a hanging refresh times out, preserves measurements, and allows retry', async ({ page }) => {
  const data = fixture(); let hold = false; const pending: (() => void)[] = [];
  await setup(page, data, async route => {
    if (hold) await new Promise<void>(resolve => { pending.push(resolve); });
    await route.fulfill({ json: data.snapshot }).catch(() => {});
  });
  const dashboard = page.getByLabel('Resource dashboard', { exact: true });
  await expect(dashboard).toContainText('26.9 GiB + 4.0 GiB');
  hold = true; await page.clock.fastForward(5000);
  await expect(dashboard.getByRole('button', { name: 'Refresh resources' })).toBeDisabled();
  await page.clock.fastForward(10000);
  await expect(dashboard.getByRole('alert')).toContainText('Resource sampling timed out after 10 seconds');
  await expect(dashboard).toContainText('26.9 GiB + 4.0 GiB');
  await expect(dashboard.getByRole('button', { name: 'Retry resource status' })).toBeEnabled();
  hold = false; pending.forEach(resolve => resolve());
  await dashboard.getByRole('button', { name: 'Retry resource status' }).click();
  await expect(dashboard.getByRole('alert')).toHaveCount(0);
});
