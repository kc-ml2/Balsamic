import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const campaign = { id: 'campaign-test', name: 'Browser test campaign', objective: 'Compare optimization mechanisms under a common physical objective.', compute_budget_seconds: 600, llm_budget_usd: 5, validation_reserve_seconds: 30, autonomy: 'guided', version: 1 };
const task = { id: 'task-test', name: 'Test configuration', physics: { n_cells: 16, wavelength_nm: 1100, deflection_angle_deg: 50, thickness_nm: 325, material: 'constant', fourier_order: 5 }, split: 'development', campaign_id: campaign.id, charter_version: 1, created_at: '2026-01-01T00:00:00Z', exposed: false };
const algorithms = [{ id: 'random', name: 'Uniform random', description: 'Reference baseline.', parameters: {} }, { id: 'hillclimb', name: 'Restart hill climbing', description: 'Restarting local search.', parameters: {} }];
const blank = { campaigns: [], campaign: null, tasks: [], algorithms, hypotheses: [], decisions: [], events: [], trials: [], messages: [], research_runs: [], settings: { llm_configured: false, model: 'gpt-6-sol', provider: { provider: 'codex', billing_mode: 'subscription', model: 'gpt-6-sol', enabled: false, configured: false } } };
const base = { ...blank, campaigns: [campaign], campaign, tasks: [task], budget: { spent_seconds: 4, llm_spent_usd: .001 } };

async function mockWorkspace(page: Page, initial: Record<string, any>, failures: { research?: number; review?: number } = {}) {
  const state: Record<string, any> = { workspace_id: 'workspace-browser-fixture', ...structuredClone(initial) };
  const receipts = new Map<string, any>();
  const writes: Array<{ url: string; method: string; body: any }> = [];
  await page.route('**/api/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname;
    if (path === '/api/events') return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'retry: 60000\n\n' });
    if (req.method() !== 'GET') {
      const body = req.postDataJSON(); writes.push({ url: path, method: req.method(), body });
      if (path === '/api/v1/commands') {
        const payload = body.payload;
        if (receipts.has(body.id)) return route.fulfill({ json: receipts.get(body.id) });
        function accept(outcome: any) {
          const record = structuredClone({ id: body.id, request: body, actor: 'researcher', status: 'completed', outcome });
          receipts.set(body.id, record);
          return route.fulfill({ json: record });
        }
        if (body.operation === 'campaign.create') {
          state.campaign = { ...campaign, ...payload, id: body.campaign_id, version: 1 };
          state.campaigns = [state.campaign]; state.tasks = payload.tasks.map((t: any, index: number) => ({ ...t, id: `task-new-${index}` }));
          return accept({ campaign: state.campaign });
        }
        if (body.operation === 'campaign.update') {
          state.campaign = { ...state.campaign, ...payload, version: state.campaign.version + 1 };
          return accept({ campaign: state.campaign });
        }
        if (body.operation === 'context.edit') {
          state.manager_context = { ...state.manager_context, guidance: payload.content, revision: state.manager_context.revision + 1 };
          return accept({ context_id: state.manager_context.id });
        }
        if (body.operation === 'issue.resolve') {
          const issue = state.manager_issues.find((i: any) => i.id === payload.issue_id);
          Object.assign(issue, { status: payload.choice, revision: issue.revision + 1 });
          return accept({ issue_id: issue.id });
        }
        if (body.operation === 'trial.control') {
          const trial = state.trials.find((t: any) => t.id === payload.trial_id);
          trial.status = payload.action === 'pause' ? 'paused' : payload.action === 'resume' ? 'running' : 'stopped';
          trial.control_revision = (trial.control_revision || 0) + 1;
          return accept({ trial_id: trial.id, trial });
        }
        if (body.operation === 'hypothesis.review') {
          if (failures.review) { failures.review -= 1; return route.fulfill({ status: 503, json: { detail: 'Could not persist feedback' } }); }
          const h = state.hypotheses.find((h: any) => h.id === payload.hypothesis_id);
          h.reviews.push({ id: `review-${h.reviews.length + 1}`, author: 'researcher', text: payload.text, created_at: '2026-01-02T00:00:00Z' });
          return accept({ hypothesis_id: h.id, hypothesis: h });
        }
        if (body.operation === 'hypothesis.status') {
          const h = state.hypotheses.find((h: any) => h.id === payload.hypothesis_id);
          Object.assign(h, { status: payload.status, status_revision: (h.status_revision || 0) + 1 });
          return accept({ hypothesis_id: h.id, hypothesis: h });
        }
        if (body.operation === 'research.start') {
          if (failures.research) { failures.research -= 1; return route.fulfill({ status: 503, json: { detail: 'Provider unavailable' } }); }
          const run = { id: `research-${writes.length}`, status: 'running', request: payload, control_revision: 0, created_at: '2026-01-02T00:00:00Z' };
          state.research_runs.push(run);
          return accept({ manager_command_id: `manager-${body.id}`, effect_id: `research-${body.id}` });
        }
        if (body.operation === 'research.control') {
          const run = state.research_runs.find((r: any) => r.id === payload.run_id);
          Object.assign(run, { status: payload.action === 'stop' ? 'interrupted' : 'running', control_revision: (run.control_revision || 0) + 1 });
          return accept({ run_id: run.id, research_run: run });
        }
        if (body.operation === 'decision.resolve') {
          const decision = state.decisions.find((d: any) => d.id === payload.decision_id);
          Object.assign(decision, { choice: payload.choice, comment: payload.comment, status: 'resolved', resolution_revision: (decision.resolution_revision || 0) + 1 });
          return accept({ decision_id: decision.id, decision });
        }
        if (body.operation === 'literature.search' || body.operation === 'source.ingest') {
          state.sources ||= [];
          state.sources.push({ id: `source-${body.id}`, title: 'Source fixture', url: 'https://example.org/paper', verification: 'fixture' });
          return accept({ effect_id: `source-${body.id}` });
        }
      }
      if (path === '/api/research') {
        if (failures.research) { failures.research -= 1; return route.fulfill({ status: 503, json: { detail: 'Provider unavailable' } }); }
        const run = { id: `research-${writes.length}`, status: 'running', request: body, created_at: '2026-01-02T00:00:00Z' };
        state.research_runs.push(run);
        return route.fulfill({ json: run });
      }
      if (path === '/api/campaigns') { state.campaign = { ...campaign, ...body }; state.campaigns = [state.campaign]; state.tasks = body.tasks.map((t: any) => ({ ...t, id: 'task-new' })); }
      if (path.endsWith('/control')) { const trial = state.trials.find((t: any) => path.includes(t.id)); if (trial) trial.status = body.action === 'pause' ? 'paused' : body.action === 'resume' ? 'running' : 'stopped'; }
      if (path.endsWith('/resolve')) { const d = state.decisions.find((d: any) => path.includes(d.id)); if (d) Object.assign(d, { ...body, status: 'resolved' }); }
      if (path.endsWith('/manager/context')) state.manager_context = { ...state.manager_context, guidance: body.content, revision: state.manager_context.revision + 1 };
      if (path.includes('/manager/issues/') && path.endsWith('/resolve')) { const issue = state.manager_issues.find((i: any) => path.includes(i.id)); if (issue) issue.status = body.choice; }
      return route.fulfill({ json: path === '/api/campaigns' ? state.campaign : { id: 'new-record', ...body } });
    }
    if (path.startsWith('/api/v1/commands/')) return receipts.has(path.split('/').at(-1)!)
      ? route.fulfill({ json: receipts.get(path.split('/').at(-1)!) }) : route.fulfill({ status: 404, json: { detail: 'Workspace record not found' } });
    if (path === '/api/v1/problems') return route.fulfill({ json: { problems: [{ id: 'meent_grating', name: 'MEENT binary grating', configuration_schema: { properties: {} } }, { id: 'bounded_continuous', name: 'Bounded continuous benchmarks', configuration_schema: { properties: { dimensions: { default: 2 }, function: { default: 'quadratic' } } } }] } });
    if (path === '/api/state') return route.fulfill({ json: state });
    if (path === '/api/implementations') return route.fulfill({ json: state.implementation_library || { versions: [] } });
    if (path.endsWith('/manager/context/history')) return route.fulfill({ json: [state.manager_context] });
    if (path.endsWith('/metrics')) return route.fulfill({ json: [] });
    if (path.endsWith('/analysis')) return route.fulfill({ json: { groups: [], paired_comparisons: [], thresholds: [], aggregate_comparisons: [] } });
    return route.fulfill({ json: {} });
  });
  return { state, writes };
}

test('a new researcher can create a charter with editable configurations and explicit budgets', async ({ page }) => {
  const { writes } = await mockWorkspace(page, blank);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'A better way to find the next design.' })).toBeVisible();
  await expect(page.getByText('No campaign yet')).toBeAttached();
  await page.screenshot({ path: test.info().outputPath('empty-workspace.png'), fullPage: true });
  await page.getByRole('button', { name: 'Create campaign', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Campaign name').fill('My inverse design study');
  await dialog.getByLabel('Compute cap (seconds)', { exact: true }).fill('240');
  await dialog.getByLabel('Validation reserve (seconds)').fill('24');
  await dialog.getByRole('button', { name: 'Add configuration' }).click();
  await dialog.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByText('Campaign created. Your workspace is ready.')).toBeVisible();
  expect(writes[0].body).toMatchObject({ operation: 'campaign.create', expected_revision: 0 });
  expect(writes[0].body.campaign_id).toBeTruthy();
  expect(writes[0].body.payload.tasks).toHaveLength(2);
  expect(writes[0].body.payload.compute_budget_seconds).toBe(240);
  expect(writes[0].body.payload.validation_reserve_seconds).toBe(24);
});

test('charter revision sends only permitted task fields and preserves explicit physics', async ({ page }) => {
  const { writes } = await mockWorkspace(page, base);
  await page.goto('/#problem');
  await page.getByRole('button', { name: 'Revise charter' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Save new version' }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  expect(writes[0].url).toBe('/api/v1/commands');
  expect(writes[0].body).toMatchObject({ operation: 'campaign.update', expected_revision: campaign.version });
  // A legacy task without a problem id keeps the server's default adapter; the form no longer assumes one.
  expect(Object.keys(writes[0].body.payload.tasks[0]).sort()).toEqual(['configuration', 'id', 'name', 'split']);
  expect(writes[0].body.payload.tasks[0].configuration).toEqual(task.physics);
});

test('experiment controls call the backend and update the visible lifecycle', async ({ page }) => {
  const trial = { id: 'trial-test', campaign_id: campaign.id, task_id: task.id, algorithm: 'random', status: 'running', seed: 7, max_steps: 100, wall_seconds: 60, priority: 0, created_at: '2026-01-01T00:00:00Z', progress: { step: 3, best_efficiency: .2, best_design: [1, 0, 1, 0], elapsed_seconds: 1, solver_calls: 3, cache_hits: 0 } };
  const { writes } = await mockWorkspace(page, { ...base, trials: [trial] });
  await page.goto('/#experiments');
  await page.getByRole('button', { name: 'Uniform random', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('20.00%')).toBeVisible();
  await dialog.getByRole('button', { name: 'Pause', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Resume', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Resume', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Pause', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect(dialog.getByText('stopped', { exact: true })).toBeVisible();
  expect(writes.map(w => w.body.operation)).toEqual(['trial.control', 'trial.control', 'trial.control']);
  expect(writes.map(w => w.body.payload.action)).toEqual(['pause', 'resume', 'stop']);
  expect(writes.map(w => w.body.payload.expected_control_revision)).toEqual([0, 1, 2]);
});

test('decisions require an explicit selection and retain researcher rationale', async ({ page }) => {
  const decision = { id: 'decision-test', title: 'Surrogate startup needs a longer probe', context: 'The initial design set used the short allocation; guided proposals have not been tested.', options: [{ id: 'extend', label: 'Extend through guided proposals', description: 'Allocate 30 additional seconds.' }, { id: 'defer', label: 'Defer this strategy' }], recommendation: 'Extend through guided proposals', status: 'pending' };
  const { writes } = await mockWorkspace(page, { ...base, decisions: [decision] });
  await page.goto('/#decisions');
  await expect(page.getByRole('button', { name: 'Record decision' })).toBeDisabled();
  await page.getByRole('radio', { name: 'Extend through guided proposals' }).check();
  await page.getByLabel('Your reasoning (optional)').fill('We need to observe the proposed mechanism before evaluating it.');
  await page.getByRole('button', { name: 'Record decision' }).click();
  await expect(page.getByRole('heading', { name: 'Room to keep exploring' })).toBeVisible();
  expect(writes[0].body).toMatchObject({ operation: 'decision.resolve', expected_revision: 1, payload: { decision_id: decision.id, expected_resolution_revision: 0,
    choice: 'extend', comment: 'We need to observe the proposed mechanism before evaluating it.' } });
});

test('mobile navigation and dialog escape work without a horizontal page overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockWorkspace(page, blank);
  await page.goto('/');
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('button', { name: 'Hypotheses', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Competing ideas. Visible reasoning.' })).toBeVisible();
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('button', { name: 'New campaign', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('deferred Codex setup keeps experiments available and distinguishes the API budget', async ({ page }) => {
  const { writes } = await mockWorkspace(page, { ...base, budget: { ...base.budget, subscription_calls: 4 } });
  await page.goto('/#problem');
  const panel = page.getByRole('complementary', { name: 'Research conversation' });
  if (!(await panel.isVisible())) await page.getByRole('button', { name: 'Campaign manager', exact: true }).click();
  await expect(panel.getByRole('link', { name: 'Codex · GPT6-sol · Models', exact: true })).toBeVisible();
  await expect(panel.getByText('Model configuration deferred', { exact: true })).toBeVisible();
  await expect(panel.getByText(/requests are saved until model access is available/)).toBeVisible();
  await expect(page.getByText('API spending', { exact: true })).toBeVisible();
  await expect(page.getByText('Subscription usage', { exact: true })).toBeVisible();
  await expect(page.getByText('4 calls', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Revise charter' }).click();
  await expect(page.getByRole('dialog').getByLabel('API spending cap (USD)')).toBeVisible();
  expect(writes).toEqual([]);
});

test('research notebook separates subscription calls from historical API estimates', async ({ page }) => {
  await mockWorkspace(page, {
    ...base,
    settings: { llm_configured: true, model: 'gpt-6-sol', provider: { provider: 'codex', billing_mode: 'subscription', model: 'gpt-6-sol', enabled: true, configured: true } },
    research_runs: [
      { id: 'subscription-run', mode: 'review', status: 'completed', model: 'gpt-6-sol', request: { message: 'Subscription review' }, usage: { billing_mode: 'subscription', cost_usd: null, api_cost_usd: 0, calls: 2, subscription_calls: 2, input_tokens: 100, output_tokens: 50 } },
      { id: 'old-api-run', mode: 'discuss', status: 'completed', model: 'gpt-6-luna', request: { message: 'Earlier API discussion' }, usage: { cost_usd: .025, calls: 1 } },
    ],
  });
  await page.goto('/#notebook');
  await page.getByRole('button', { name: 'Agent runs', exact: true }).click();
  const subscription = page.locator('.run-list article').filter({ hasText: 'Subscription review' });
  await expect(subscription.getByText('2 subscription calls', { exact: true })).toBeVisible();
  await expect(subscription.getByText('100 input tokens', { exact: true })).toBeVisible();
  await expect(subscription.getByText('Uses subscription allowance', { exact: true })).toBeVisible();
  await expect(subscription.locator('.meta-row')).not.toContainText('$');
  await expect(page.getByText('$0.0250 estimated API cost', { exact: true })).toBeVisible();
});

const feedbackIdea = {
  id: 'hypothesis-feedback', title: 'Adaptive block hill climbing', mechanism: 'Mutate adjacent cells and restart after stagnation.',
  rationale: 'Use local structure to improve proposal efficiency.', assumptions: [], risks: [], sources: [], parent_ids: [],
  algorithm: 'hillclimb', algorithm_config: {}, status: 'proposed', origin: 'researcher', created_at: '2026-01-01T00:00:00Z',
  implementation_readiness: { state: 'ready', runnable: true, reason: 'Built-in implementation is available.' },
  reviews: [{ id: 'review-original', author: 'researcher', text: 'Preserve block mutations.', created_at: '2026-01-01T00:00:00Z' }],
};
const configuredFeedback = {
  ...base, hypotheses: [feedbackIdea],
  settings: { llm_configured: true, model: 'gpt-6-sol', provider: { provider: 'codex', billing_mode: 'subscription', model: 'gpt-6-sol', enabled: true, configured: true } },
};

async function openFeedbackIdea(page: Page) {
  await page.goto('/#hypotheses');
  await page.getByRole('button', { name: /H01.*Adaptive block hill climbing/ }).click();
  return page.getByRole('dialog');
}

test('saving feedback records a comment without requesting a critique or revision', async ({ page }) => {
  const { writes } = await mockWorkspace(page, configuredFeedback);
  const dialog = await openFeedbackIdea(page);
  await dialog.getByLabel('Your feedback', { exact: true }).fill('Replace the fixed restart schedule with a stagnation rule.');
  await dialog.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect(dialog.getByText('Comment saved. Agents have not been asked to revise the idea.')).toBeVisible();
  await expect(dialog.getByLabel('Your feedback', { exact: true })).toHaveValue('');
  await expect(dialog.getByText('Replace the fixed restart schedule with a stagnation rule.', { exact: true })).toBeVisible();
  expect(writes.map(w => [w.url, w.body.operation])).toEqual([['/api/v1/commands', 'hypothesis.review']]);
});

test('revision saves the draft then targets its idea and all saved researcher feedback', async ({ page }) => {
  const { writes, state } = await mockWorkspace(page, configuredFeedback);
  const dialog = await openFeedbackIdea(page);
  await dialog.getByLabel('Your feedback', { exact: true }).fill('Trigger restart when progress stalls.');
  await dialog.getByRole('button', { name: 'Revise with my feedback', exact: true }).click();
  await expect(dialog.getByText(/Agents are working/)).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Revise with my feedback', exact: true })).toBeDisabled();
  await expect(dialog.getByRole('button', { name: 'Ask agents for critique', exact: true })).toBeDisabled();
  expect(writes.map(w => w.url)).toEqual(['/api/v1/commands', '/api/v1/commands']);
  expect(writes[0].body.operation).toBe('hypothesis.review');
  expect(writes[1].body).toMatchObject({ campaign_id: campaign.id, operation: 'research.start', payload: { hypothesis_id: feedbackIdea.id, mode: 'evolve', feedback_review_ids: ['review-original', 'review-2'] } });
  expect(state.hypotheses[0].title).toBe(feedbackIdea.title);
  expect(state.trials).toEqual([]);

  const child = { ...feedbackIdea, id: 'hypothesis-child', title: 'Stagnation-triggered block search', parent_ids: [feedbackIdea.id], reviews: [], change_summary: 'Restarts now follow a stall detector.', feedback_response: 'Kept block moves and replaced the fixed restart schedule.', revision_context: { hypothesis_id: feedbackIdea.id, reviews: structuredClone(state.hypotheses[0].reviews) } };
  state.hypotheses.push(child);
  Object.assign(state.research_runs[0], { status: 'completed', result: { hypotheses: [child] } });
  await page.reload();
  await page.getByRole('button', { name: /H01.*Adaptive block hill climbing/ }).click();
  await dialog.getByRole('button', { name: 'Open revision: Stagnation-triggered block search' }).click();
  await expect(dialog.getByRole('heading', { name: 'Stagnation-triggered block search', exact: true })).toBeVisible();
  await expect(dialog.getByText('Restarts now follow a stall detector.')).toBeVisible();
  await expect(dialog.getByText('Kept block moves and replaced the fixed restart schedule.')).toBeVisible();
  await dialog.getByText('Feedback used for this revision', { exact: true }).click();
  await expect(dialog.getByText('Trigger restart when progress stalls.', { exact: true })).toBeVisible();
  await expect(dialog.getByLabel('Your feedback', { exact: true })).toHaveValue('');
});

test('disabled provider keeps comments available and explains why agent actions are disabled', async ({ page }) => {
  const { writes } = await mockWorkspace(page, { ...base, hypotheses: [feedbackIdea] });
  const dialog = await openFeedbackIdea(page);
  await dialog.getByLabel('Your feedback', { exact: true }).fill('Use a smaller initial block size.');
  await expect(dialog.getByRole('button', { name: 'Revise with my feedback', exact: true })).toBeDisabled();
  await expect(dialog.getByRole('button', { name: 'Ask agents for critique', exact: true })).toBeDisabled();
  await expect(dialog.getByText(/Configure and enable the research provider/)).toBeVisible();
  await dialog.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect(dialog.getByText('Use a smaller initial block size.', { exact: true })).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].body.operation).toBe('hypothesis.review');
});

test('failed revision submission retains saved feedback and retry does not save it twice', async ({ page }) => {
  const { writes, state } = await mockWorkspace(page, configuredFeedback, { research: 1 });
  const dialog = await openFeedbackIdea(page);
  await dialog.getByLabel('Your feedback', { exact: true }).fill('Explain the restart threshold choice.');
  await dialog.getByRole('button', { name: 'Revise with my feedback', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Your feedback is saved, but the revision request could not start.');
  await expect(dialog.getByLabel('Your feedback', { exact: true })).toHaveValue('');
  await expect(dialog.getByText('Explain the restart threshold choice.', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Revise with my feedback', exact: true }).click();
  await expect(dialog.getByText(/Agents are working/)).toBeVisible();
  await expect.poll(() => writes.filter(w => w.body.operation === 'research.start').length).toBe(2);
  expect(writes.filter(w => w.body.operation === 'hypothesis.review')).toHaveLength(1);
  expect(writes.filter(w => w.body.operation === 'research.start').map(w => w.body.payload.feedback_review_ids)).toEqual([['review-original', 'review-2'], ['review-original', 'review-2']]);
  expect(writes.filter(w => w.body.operation === 'research.start')[1].body.id).toBe(writes.filter(w => w.body.operation === 'research.start')[0].body.id);
  expect(state.hypotheses[0].reviews).toHaveLength(2);
});

test('a failed feedback save preserves the draft and never starts a revision', async ({ page }) => {
  const { writes } = await mockWorkspace(page, configuredFeedback, { review: 1 });
  const dialog = await openFeedbackIdea(page);
  await dialog.getByLabel('Your feedback', { exact: true }).fill('Preserve the original mutation distribution.');
  await dialog.getByRole('button', { name: 'Revise with my feedback', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('No revision was requested.');
  await expect(dialog.getByLabel('Your feedback', { exact: true })).toHaveValue('Preserve the original mutation distribution.');
  expect(writes.map(w => [w.url, w.body.operation])).toEqual([['/api/v1/commands', 'hypothesis.review']]);
});

test('critique assesses saved feedback without saving a draft or requesting a child', async ({ page }) => {
  const { writes, state } = await mockWorkspace(page, configuredFeedback);
  const dialog = await openFeedbackIdea(page);
  await dialog.getByLabel('Your feedback', { exact: true }).fill('This is still a draft.');
  await dialog.getByRole('button', { name: 'Ask agents for critique', exact: true }).click();
  await expect(dialog.getByText(/Agents are working/)).toBeVisible();
  await expect(dialog.getByLabel('Your feedback', { exact: true })).toHaveValue('This is still a draft.');
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({ operation: 'research.start', payload: { mode: 'review', hypothesis_id: feedbackIdea.id } });
  expect(writes[0].body.payload.feedback_review_ids).toBeUndefined();
  expect(state.hypotheses).toHaveLength(1);
  state.hypotheses[0].reviews.push({ id: 'review-agent', author: 'agent', role: 'skeptical_reviewer', text: 'The stall threshold may depend on problem size.', created_at: '2026-01-02T00:00:00Z' });
  Object.assign(state.research_runs[0], { status: 'completed', result: { hypotheses: [] } });
  await page.reload();
  await page.getByRole('button', { name: /H01.*Adaptive block hill climbing/ }).click();
  await expect(dialog.getByText('The stall threshold may depend on problem size.')).toBeVisible();
  await expect(dialog.getByText('Critique completed. Read the agent assessments above.')).toBeVisible();
});

test('archived ideas and unsuccessful agent runs expose actionable reasons', async ({ page }) => {
  await mockWorkspace(page, { ...configuredFeedback, hypotheses: [{ ...feedbackIdea, status: 'archived' }], research_runs: [{ id: 'failed-revision', status: 'failed', request: { mode: 'evolve', hypothesis_id: feedbackIdea.id }, error: 'Model returned no valid revision.', created_at: '2026-01-02T00:00:00Z' }] });
  const dialog = await openFeedbackIdea(page);
  await expect(dialog.getByRole('button', { name: 'Revise with my feedback', exact: true })).toBeDisabled();
  await expect(dialog.getByText('Revive this idea before requesting a revision.')).toBeVisible();
  await expect(dialog.getByRole('alert')).toContainText('Model returned no valid revision.');
  await expect(dialog.getByText(/This request has not produced a linked revision/)).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Ask agents for critique', exact: true })).toBeEnabled();
});

test('reference code is visible while campaign execution still requires integration', async ({ page }) => {
  const reference = { id: 'reference-ppo', name: 'Authors’ Fourier PPO', repository_url: 'https://github.com/jLabKAIST/flrl',
    revision: '7838e71313d71cee8e2db3b432f41f80b9106a95', entrypoints: ['main.py'],
    integration_notes: 'Reuse the authors’ PPO loop; adapt evaluation accounting and validate checkpoints.' };
  const { writes } = await mockWorkspace(page, { ...base,
    hypotheses: [{ ...feedbackIdea, algorithm: 'flrl_ppo', implementation_readiness: { state: 'reference_available',
      runnable: false, reason: 'Reference code is available. Campaign integration is required.', references: [reference] } }],
  });
  const dossier = await openFeedbackIdea(page);
  await expect(dossier.getByText('Reference code available · needs integration', { exact: true })).toBeVisible();
  await expect(dossier.getByRole('link', { name: 'Authors’ repository' })).toHaveAttribute('href', reference.repository_url);
  await expect(dossier).toContainText('7838e71313d7');
  await expect(dossier).toContainText('Entry points: main.py');
  await expect(dossier.getByRole('link', { name: 'Download captured reference source' })).toHaveAttribute('href', '/api/v1/implementation-references/reference-ppo');
  await dossier.getByRole('button', { name: 'Design experiment', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Launch experiment', exact: true })).toBeDisabled();
  expect(writes).toHaveLength(0);
});

test('missing implementation permits design and commissions separate bounded work', async ({ page }) => {
  const { writes } = await mockWorkspace(page, { ...base,
    campaign: { ...campaign, implementation_compute_budget_seconds: 180 },
    hypotheses: [{ ...feedbackIdea, algorithm: 'fourier', implementation_readiness: { state: 'missing', runnable: false, reason: 'This proposal has no executable implementation.' } }],
  });
  const dossier = await openFeedbackIdea(page);
  await expect(dossier.getByText('No campaign implementation', { exact: true })).toBeVisible();
  await expect(dossier.getByRole('button', { name: 'Design experiment', exact: true })).toBeEnabled();
  await dossier.getByRole('button', { name: 'Design experiment', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Save draft', exact: true })).toBeEnabled();
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Launch experiment', exact: true })).toBeDisabled();
  // This older server fixture has no inference catalog. That failure must not
  // remove the saved-design controls or force another implementation workflow.
  await expect(page.getByRole('dialog').getByText('The diagnostic catalog is unavailable. Saved declarations can still be reviewed.')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Close dialog' }).click();
  await openFeedbackIdea(page);
  await expect(dossier.getByText('This proposal has no executable implementation.', { exact: true })).toBeVisible();
  await dossier.getByRole('button', { name: 'Request implementation', exact: true }).click();
  const form = page.getByRole('dialog');
  await expect(form.getByRole('heading', { name: 'Commission an implementation', exact: true })).toBeVisible();
  await form.getByLabel('Implementation time allocation (seconds)').fill('90');
  await form.getByLabel('Maximum model calls').fill('3');
  await form.getByRole('button', { name: 'Commission implementation', exact: true }).click();
  await expect(form).not.toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].url).toBe('/api/v1/commands');
  expect(writes[0].body).toMatchObject({ operation: 'implementation.commission', payload: {
    hypothesis_id: feedbackIdea.id, compute_seconds: 90, max_calls: 3, api_budget_usd: 0,
    spec: { mechanism: feedbackIdea.mechanism, capabilities: ['binary_forward'], problem_id: 'meent_grating' } } });
  expect(writes[0].body.id).toBeTruthy();
});

test('campaign memory edits and historical restoration create new revisions', async ({ page }) => {
  const memory = { id: 'context-one', revision: 3, guidance: '## Constraints\nPreserve the baselines.',
    document: '# Campaign context\nPreserve the baselines.', source_ids: ['campaign-test'], event_cursor: 19 };
  const { writes } = await mockWorkspace(page, { ...base, manager_context: memory });
  await page.goto('/#memory');
  await expect(page.getByRole('heading', { name: 'What the manager remembers.' })).toBeVisible();
  await page.getByRole('button', { name: 'View revision history' }).click();
  await page.getByLabel('Earlier revision').selectOption('context-one');
  await page.getByLabel('Campaign guidance (Markdown)').fill('## Constraints\nPreserve baselines and defer gradient work.');
  await page.getByRole('button', { name: 'Save new memory revision' }).click();
  await expect(page.getByText('Revision 4', { exact: true })).toBeVisible();
  expect(writes[0].body).toMatchObject({ operation: 'context.edit', payload: { expected_revision: 3, content: '## Constraints\nPreserve baselines and defer gradient work.' } });
  await page.getByRole('button', { name: 'Restore this guidance as a new revision' }).click();
  await expect(page.getByLabel('Campaign guidance (Markdown)')).toHaveValue(memory.guidance);
  await page.getByRole('button', { name: 'Save new memory revision' }).click();
  await expect(page.getByText('Revision 5', { exact: true })).toBeVisible();
  expect(writes[1].body.payload.expected_revision).toBe(4);
});

test('implementation exceptions can be discussed or deferred through the campaign manager', async ({ page }) => {
  const { writes } = await mockWorkspace(page, { ...base, manager_issues: [{ id: 'issue-gradient', revision: 1, status: 'pending', message: 'The evaluator cannot provide RCWA gradients.' }] });
  await page.goto('/');
  const manager = page.getByRole('complementary', { name: 'Research conversation' });
  await manager.getByRole('button', { name: 'Discuss', exact: true }).click();
  await expect(manager.getByLabel('Message to campaign manager')).toHaveValue('Resolve issue issue-gradient: The evaluator cannot provide RCWA gradients.');
  await manager.getByRole('button', { name: 'Defer', exact: true }).click();
  await expect(manager.getByText('The evaluator cannot provide RCWA gradients.', { exact: true })).not.toBeVisible();
  expect(writes[0]).toMatchObject({ url: '/api/v1/commands', body: { operation: 'issue.resolve', payload: { issue_id: 'issue-gradient', expected_revision: 1, choice: 'deferred' } } });
});

test('library attaches a specific reusable version without launching an experiment', async ({ page }) => {
  const version = { id: 'impl_reference', name: 'Reference implementation', status: 'validated',
    spec: { mechanism: 'Seeded one-bit proposals.', n_cells_min: 2, n_cells_max: 16, capabilities: ['binary_forward'] },
    validation_report: { passed: true, kind: 'implementation_correctness' } };
  const { writes } = await mockWorkspace(page, { ...base, campaign: { ...campaign, active_study_id: 'study-test' }, hypotheses: [feedbackIdea],
    implementation_library: { versions: [version], candidates: { [version.id]: { eligible: true, reason: 'Current correctness evidence matches this study' } } } });
  await page.goto('/#implementations');
  await expect(page.getByRole('heading', { name: 'Reference implementation', exact: true })).toBeVisible();
  await page.getByLabel('Attach a version to an idea').selectOption(feedbackIdea.id);
  await page.getByLabel('Reason for reuse or decline').fill('The checked behavior fits this proposal and its current study.');
  await page.getByRole('button', { name: 'Use this version for the selected idea' }).click();
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ url: '/api/v1/commands', body: { operation: 'implementation.reuse',
    payload: { hypothesis_id: feedbackIdea.id, version_id: version.id, study_id: 'study-test', decision: 'reuse',
      rationale: 'The checked behavior fits this proposal and its current study.' } } });
});
