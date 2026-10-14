import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const campaign = { id: 'token-sharing', version: 1, name: 'Token sharing', autonomy: 'delegated', objective: 'Share one token budget', compute_budget_seconds: 100, llm_budget_usd: 5 };
function cell(name: string, source: string, more: Record<string, unknown> = {}) {
  return { id: `id_${name}`, name, kind: 'markdown', source, status: 'proposed', revision: 1, notebook_revision: 1, author: 'lead', reason: 'First draft', updated_at: '2026-10-14T01:00:00+00:00', agreed_revision: null, ...more };
}
function workspace(cells: any[]) {
  const notebook = { notebook: cells.length ? { id: 'formulation_token-sharing', revision: 1 } : null, cells,
    outline: cells.length ? [] : [{ op: 'create', kind: 'markdown', name: 'problem', source: '## The problem in words', status: 'open' }],
    active_run: null as any, latest_run: null as any, limits: { cells: 200, source_characters: 32768, run_seconds: 90 } };
  const state: any = { workspace_id: 'formulation-workspace', campaign, campaigns: [campaign], tasks: [], trials: [], hypotheses: [], decisions: [], messages: [], events: [], algorithms: [],
    manager_issues: [], manager_commands: [], research_runs: [], budget: {}, settings: { llm_configured: true }, formulation: cells.length ? { revision: 1 } : null,
    agent_runtime: { configuration: { enabled: true, status: 'running', control_revision: 1, provider: { configured: true } },
      agents: [{ id: 'lead', role: 'lead', status: 'waiting', model: 'deepseek-v4-pro', control_revision: 0 }], questions: [], aliases: [] } };
  return { notebook, state, commands: [] as any[] };
}
// A one-pixel PNG, for the pictures a run drew.
const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
async function open(page: Page, lab: ReturnType<typeof workspace>, answer: (body: any, path: string) => any = () => undefined) {
  await page.addInitScript(() => {
    const streams: EventTarget[] = [];
    (window as any).__updateWorkspace = () => streams.forEach(stream => stream.dispatchEvent(new MessageEvent('update')));
    (window as any).EventSource = class extends EventTarget { onopen: any; constructor() { super(); streams.push(this); setTimeout(() => this.onopen?.({}), 1); } close() {} };
  });
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === 'PUT') { const body = request.postDataJSON(); lab.commands.push({ put: path, body }); return route.fulfill(await answer(body, path) || { json: { outcome: {}, run: null, queued: false, notebook: lab.notebook } }); }
    if (request.method() === 'POST') { const body = request.postDataJSON(); lab.commands.push(path.endsWith('/runs') ? { run: path } : body); return route.fulfill(await answer(body, path) || { json: { status: 'completed', outcome: {} } }); }
    if (path.endsWith('.png')) return route.fulfill({ contentType: 'image/png', body: pixel });
    if (path.endsWith('/history')) return route.fulfill({ json: { revisions: [cell('budget', 'Spend at most $B$.'), cell('budget', 'Spend at most $B$ a month.', { revision: 2, author: 'researcher', reason: '' })] } });
    if (path.endsWith('/formulation')) return route.fulfill({ json: lab.notebook });
    if (path.includes('/api/v1/commands/')) return route.fulfill({ status: 404, json: { detail: 'Not received' } });
    return route.fulfill({ json: lab.state });
  });
  await page.goto('/#formulation');
}

test('equations are typeset, and what a cell says cannot run as a page', async ({ page }) => {
  const lab = workspace([
    cell('budget', ['## Monthly budget', '', 'The team spends at most $B$ tokens, about \\$1.02 a day each.', '', '$$\\sum_{i \\in I} \\sum_{t \\in T} x_{it} \\le B$$', '',
      '<script>window.cellExecuted = true</script>', '<img src="/unexpected" onerror="window.cellExecuted = true">', '', '$\\href{javascript:window.cellExecuted=true}{x}$'].join('\n')),
    cell('budget_code', 'm.budget = Constraint(expr=sum(m.x[i, t] for i in m.I for t in m.T) <= m.B)\nsecond_line = 1', { kind: 'code' }),
  ]);
  lab.state.messages = [{ id: 'cost', agent_id: 'lead', role: 'assistant', content: 'That run cost $1.02 and the next costs $3.50. I changed [budget](formulation/budget) and [budget_code](#formulation/budget_code).' }];
  await open(page, lab);
  const budget = page.getByRole('article', { name: 'Cell budget', exact: true });
  await expect(budget.getByRole('heading', { name: 'Monthly budget' })).toBeVisible();
  await expect(budget.locator('.katex-display')).toHaveCount(1);
  await expect(budget.locator('.katex').first()).toBeVisible();
  await expect(budget).toContainText('about $1.02 a day each');
  await expect(budget.locator('script, img, a')).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).cellExecuted)).toBeUndefined();
  // The conversation is not typeset: there a dollar sign is an amount.
  const chat = page.getByRole('complementary', { name: 'Lead agent conversation' });
  await expect(chat).toContainText('That run cost $1.02 and the next costs $3.50.');
  await expect(chat.locator('.katex')).toHaveCount(0);
  // A link to a cell stays in the dashboard, with or without its #.
  await expect(chat.getByRole('link', { name: 'budget', exact: true })).toHaveAttribute('href', '#formulation/budget');
  await expect(chat.getByRole('link', { name: 'budget_code' })).toHaveAttribute('href', '#formulation/budget_code');
  // Code is folded to its first line until asked for.
  const codeCell = page.getByRole('article', { name: 'Cell budget_code', exact: true });
  await expect(codeCell.locator('summary')).toContainText('m.budget = Constraint');
  await expect(codeCell.locator('pre')).toBeHidden();
  await page.getByLabel('Show code').check();
  await expect(codeCell.locator('pre')).toContainText('second_line = 1');
  await expect(page.getByRole('link', { name: 'Markdown' })).toHaveAttribute('href', '/api/campaigns/token-sharing/formulation/export');
  await expect(page.getByRole('link', { name: 'marimo notebook' })).toHaveAttribute('href', '/api/campaigns/token-sharing/formulation/export?format=py');
  // The agent-team panel is not stacked above the notebook.
  await expect(page.getByRole('region', { name: 'Agent team' })).toHaveCount(0);
});

test('agreeing names the revision that was read, and Discuss fills the composer without sending', async ({ page }) => {
  const lab = workspace([cell('budget', 'Spend at most $B$.', { revision: 3 }), cell('caps', 'Daily caps.', { status: 'agreed', author: 'researcher', agreed_revision: 1 })]);
  await open(page, lab);
  const budget = page.getByRole('article', { name: 'Cell budget', exact: true });
  await expect(page.getByRole('group', { name: 'Show cells' })).toContainText('Proposed1');
  await budget.getByRole('button', { name: 'Agree' }).click();
  await expect.poll(() => lab.commands.length).toBe(1);
  expect(lab.commands[0]).toMatchObject({ operation: 'formulation.edit', campaign_id: 'token-sharing', expected_revision: 1,
    payload: { operations: [{ op: 'update', cell: 'id_budget', expected_revision: 3, status: 'agreed' }] } });
  await expect(page.getByRole('article', { name: 'Cell caps', exact: true }).getByRole('button', { name: 'Withdraw agreement' })).toBeVisible();
  await budget.getByRole('button', { name: 'Discuss' }).click();
  const composer = page.getByRole('textbox', { name: 'Message to lead agent' });
  await expect(composer).toHaveValue('About cell budget (revision 3) of the formulation notebook: ');
  await expect(composer).toBeFocused();
  // A second update of the page does not insert the text again.
  await page.evaluate(() => (window as any).__updateWorkspace());
  await page.waitForTimeout(700);
  await expect(composer).toHaveValue('About cell budget (revision 3) of the formulation notebook: ');
  expect(lab.commands).toHaveLength(1);
});

test('what is being typed survives the lead changing the same cell, and the conflict is shown', async ({ page }) => {
  const lab = workspace([cell('budget', 'Spend at most $B$.')]);
  await open(page, lab);
  const budget = page.getByRole('article', { name: 'Cell budget', exact: true });
  await budget.getByRole('button', { name: 'Edit' }).click();
  const editor = budget.getByRole('form', { name: 'Edit budget' });
  await editor.getByRole('textbox', { name: /Text/ }).fill('Spend at most $B$ tokens in a month.');
  await expect(editor.getByRole('region', { name: 'Preview' }).locator('.katex')).toHaveCount(1);
  // The lead saves another wording meanwhile.
  lab.notebook.cells[0] = cell('budget', 'Spend no more than $B$.', { revision: 2, notebook_revision: 2 });
  lab.notebook.notebook = { id: 'formulation_token-sharing', revision: 2 };
  lab.state.formulation = { revision: 2 };
  await page.evaluate(() => (window as any).__updateWorkspace());
  await expect(editor.getByRole('status')).toContainText('the lead agent changed this cell while you were writing');
  await expect(editor.getByRole('textbox', { name: /Text/ })).toHaveValue('Spend at most $B$ tokens in a month.');
  await editor.getByRole('button', { name: 'Keep my text and replace it' }).click();
  await editor.getByRole('button', { name: 'Save cell' }).click();
  await expect.poll(() => lab.commands.length).toBe(1);
  expect(lab.commands[0].payload.operations).toEqual([{ op: 'update', cell: 'id_budget', expected_revision: 2, source: 'Spend at most $B$ tokens in a month.' }]);
});

test('a refused edit keeps the text and says why', async ({ page }) => {
  const lab = workspace([cell('budget_code', 'B = 4_000_000', { kind: 'code' })]);
  await open(page, lab, () => ({ status: 409, json: { detail: '`B` is defined in both budget_code and caps. A name has one defining cell.' } }));
  await page.getByRole('button', { name: 'Add a cell here' }).click();
  const editor = page.getByRole('form', { name: 'New cell' });
  await editor.getByLabel('Kind').selectOption('code');
  await editor.getByLabel('Name').fill('caps');
  await editor.getByRole('textbox', { name: 'Python code' }).fill('B = 1');
  await editor.getByRole('button', { name: 'Add cell' }).click();
  await expect(page.getByRole('alert')).toContainText('`B` is defined in both budget_code and caps');
  await expect(editor.getByRole('textbox', { name: 'Python code' })).toHaveValue('B = 1');
  expect(lab.commands[0].payload.operations).toEqual([{ op: 'create', kind: 'code', source: 'B = 1', name: 'caps', after: 'id_budget_code' }]);
});

test('Run all shows what each code cell produced and marks what is out of date', async ({ page }) => {
  const output = (more: Record<string, unknown> = {}) => ({ status: 'ok', stdout: '', outputs: [], run_id: 'run_1', stale: false, elapsed_seconds: 0.42, ...more });
  const lab = workspace([
    cell('params', 'B = 4_000_000\nprint("budget", B)', { kind: 'code', output: output({ stdout: 'budget 4000000\n', stale: true }) }),
    cell('solve', 'solution = solve(B)', { kind: 'code', output: output({ outputs: [{ mime: 'text/markdown', text: '| user | cap |\n| --- | --- |\n| ana | 180000 |' },
      { mime: 'text/latex', text: '\\sum_i x_i \\le B' }, { mime: 'image/png', file: 'solve_3.png' }, { mime: 'text/plain', text: '[1, 1, 0]' }, { mime: 'text/markdown', text: '<script>window.outputExecuted = true</script>' }] }) }),
    cell('check', 'assert total <= B', { kind: 'code', output: output({ status: 'error', error: 'File "<cell check>", line 1\nAssertionError', elapsed_seconds: 0.01 }) }),
    cell('report', 'report()', { kind: 'code', output: output({ status: 'skipped', after: 'check' }) }),
    cell('untouched', 'later = 1', { kind: 'code' }),
  ]);
  lab.notebook.latest_run = { id: 'run_1', status: 'error', requested_by: 'lead', started_at: '2026-10-14T01:00:00+00:00', elapsed_seconds: 2.1 };
  await open(page, lab, (_body, path) => {
    if (!path.endsWith('/formulation/runs')) return undefined;
    lab.notebook.active_run = { id: 'run_2', status: 'running', requested_by: 'researcher', started_at: '2026-10-14T01:05:00+00:00' };
    return { json: lab.notebook.active_run };
  });
  const result = (name: string) => page.getByRole('region', { name: `Output of ${name}`, exact: true });
  await expect(result('params')).toContainText('budget 4000000');
  await expect(result('params')).toContainText('out of date');
  await expect(result('solve').getByRole('cell', { name: 'ana' })).toBeVisible();
  await expect(result('solve').locator('.katex-display')).toHaveCount(1);
  await expect(result('solve').getByRole('img', { name: 'Figure 3 of cell solve' })).toHaveAttribute('src', '/api/campaigns/token-sharing/formulation/runs/run_1/files/solve_3.png');
  await expect(result('solve')).toContainText('[1, 1, 0]');
  await expect(result('solve').locator('script')).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).outputExecuted)).toBeUndefined();
  await expect(result('check').getByRole('alert')).toContainText('AssertionError');
  await expect(result('report')).toContainText('not run: cell check failed first');
  await expect(result('untouched')).toHaveCount(0);
  await expect(page.getByText('Last run by the lead agent')).toContainText('a cell failed (2.1 s). Some cells changed since; their outputs are out of date.');
  // Running: the button waits, and the page reads the notebook again until the run is over.
  await page.getByRole('button', { name: 'Run all' }).click();
  await expect(page.getByRole('button', { name: 'Running…' })).toBeDisabled();
  expect(lab.commands).toEqual([{ run: '/api/campaigns/token-sharing/formulation/runs' }]);
  lab.notebook.cells[0] = cell('params', 'B = 4_000_000\nprint("budget", B)', { kind: 'code', output: output({ stdout: 'budget 4000000\n', run_id: 'run_2' }) });
  lab.notebook.cells[2] = cell('check', 'assert total <= B', { kind: 'code', output: output({ run_id: 'run_2' }) });
  lab.notebook.latest_run = { id: 'run_2', status: 'completed', requested_by: 'researcher', started_at: '2026-10-14T01:05:00+00:00', elapsed_seconds: 1.8 };
  lab.notebook.active_run = null;
  await expect(page.getByRole('button', { name: 'Run all' })).toBeEnabled();
  await expect(result('params')).not.toContainText('out of date');
  await expect(result('check').getByRole('alert')).toHaveCount(0);
  await expect(page.getByText('Last run by you')).toContainText('every cell ran (1.8 s).');
});

test('an earlier revision can be read and restored', async ({ page }) => {
  const lab = workspace([cell('budget', 'Spend at most $B$ a month.', { revision: 2, author: 'researcher', reason: '' })]);
  await open(page, lab);
  await page.getByRole('article', { name: 'Cell budget', exact: true }).getByRole('button', { name: 'History' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: /Revision 1/ })).toHaveAttribute('aria-pressed', 'true');
  await expect(dialog.getByRole('region', { name: 'Revision 1' })).toContainText('Spend at most');
  await dialog.getByRole('button', { name: 'Restore this revision' }).click();
  await expect.poll(() => lab.commands.length).toBe(1);
  expect(lab.commands[0].payload).toEqual({ operations: [{ op: 'update', cell: 'id_budget', expected_revision: 2, source: 'Spend at most $B$.' }], reason: 'Restored an earlier revision of budget' });
});

test('an empty notebook starts from the outline, and a waiting question is announced', async ({ page }) => {
  const lab = workspace([]);
  lab.state.agent_runtime.questions = [{ id: 'question', status: 'pending', question: 'Is the monthly allocation a hard cap?', reason: 'It decides the objective.' }];
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, lab);
  await expect(page.getByRole('status').filter({ hasText: 'waiting for your answer' })).toContainText('Is the monthly allocation a hard cap?');
  await page.getByRole('button', { name: 'Start from the standard outline' }).click();
  await expect.poll(() => lab.commands.length).toBe(1);
  expect(lab.commands[0].payload).toEqual({ operations: lab.notebook.outline, reason: 'Standard outline' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});

test('long code, wide tables and long equations stay inside a phone screen', async ({ page }) => {
  const lab = workspace([
    cell('parameters', ['| Symbol | Meaning | Value or range | Source |', '| --- | --- | --- | --- |', '| $p_k$ | selling price of one loaf of kind $k$ | between 3 and 9 | to be given by the owner |'].join('\n')),
    cell('objective', 'Maximise profit.\n\n$$\\max \\ \\sum_{k \\in K} \\left( p_k \\cdot \\min(x_k,\\, d_k) - c_k \\cdot x_k \\right) + \\sum_{k \\in K} \\sum_{t \\in T} r_{kt} \\, y_{kt}$$'),
    cell('oven_code', 'oven_row = np.array([oven_hours_per_loaf[kind] for kind in kinds_of_bread_baked_each_morning])   # one entry per kind', { kind: 'code' }),
  ]);
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, lab);
  await expect(page.getByRole('article', { name: 'Cell objective', exact: true }).locator('.katex-display')).toHaveCount(1);
  const fits = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  expect(await fits()).toBe(true);
  await page.getByLabel('Show code').check();
  await expect(page.getByRole('article', { name: 'Cell oven_code', exact: true }).locator('pre')).toBeVisible();
  expect(await fits()).toBe(true);
});

test('a link to a cell opens the notebook at that cell', async ({ page }) => {
  const lab = workspace(Array.from({ length: 30 }, (_, index) => cell(`c_${index}`, `Constraint ${index}.\n\n$$x_{${index}} \\le B$$`)));
  await open(page, lab);
  await expect(page.getByRole('article', { name: 'Cell c_0', exact: true })).toBeVisible();
  await page.evaluate(() => { location.hash = '#formulation/c_27'; });
  const target = page.getByRole('article', { name: 'Cell c_27', exact: true });
  await expect(target).toBeInViewport();
  await expect(target).toBeFocused();
});

test('parameters are controls: a move saves the value and runs the notebook, and the outputs come back in place', async ({ page }) => {
  const output = (text: string, run: string) => ({ status: 'ok', stdout: text, outputs: [], run_id: run, stale: false, elapsed_seconds: .2 });
  const lab = workspace([cell('cap_code', 'cap = P.B / P.seats / 30', { kind: 'code', output: output('cap 11111\n', 'frun_1') })]);
  const parameter = (more: Record<string, unknown>) => ({ revision: 1, value_author: 'researcher', used_by: ['cap_code'], ...more });
  (lab.notebook as any).parameters = [
    parameter({ id: 'p1', name: 'B', label: 'Monthly allocation', kind: 'number', value: 4_000_000, minimum: 0, maximum: 10_000_000, step: 100_000, unit: 'tokens',
      symbol: 'B', basis: 'given', prominent: true, description: 'Tokens the team may use in a month.' }),
    parameter({ id: 'p2', name: 'seats', label: 'Seats', kind: 'integer', value: 12, minimum: 1, basis: 'given', prominent: true }),
    parameter({ id: 'p3', name: 'carry', label: 'Carry over unused tokens', kind: 'boolean', value: false, basis: 'assumed', value_author: 'lead', used_by: [] }),
    parameter({ id: 'p4', name: 'policy', label: 'Policy', kind: 'choice', value: 'fair', choices: ['fair', 'greedy'], basis: 'to_measure' }),
  ];
  const puts = () => lab.commands.filter(entry => entry.put);
  await open(page, lab, (body, path) => {
    if (!path.endsWith('/values')) return undefined;
    const held = (lab.notebook as any).parameters;
    for (const [name, value] of Object.entries(body.values)) Object.assign(held.find((item: any) => item.name === name), { value, value_author: 'researcher', revision: 2 });
    const B = held[0].value, seats = held[1].value;
    lab.notebook.cells[0].output = output(`cap ${Math.round(B / seats / 30)}\n`, `frun_${puts().length + 1}`);
    return { json: { outcome: {}, run: body.run ? { status: 'completed', elapsed_seconds: .3 } : null, queued: false, notebook: lab.notebook } };
  });
  const panel = page.getByRole('region', { name: 'Parameters' });
  await expect(panel).toContainText('2 given · 1 assumed · 1 to be measured · 1 not used yet');
  // What matters most comes first; the rest is folded.
  await expect(panel.getByLabel('Monthly allocation (tokens)', { exact: true })).toHaveValue('4000000');
  await expect(panel.getByLabel('Policy', { exact: true })).toBeHidden();
  await expect(panel.getByText('2 more parameters')).toBeVisible();
  await expect(panel.locator('.knob-symbol .katex')).toHaveCount(1);
  // A slider moved by one step is one request, and the cell's output changes where it is.
  await panel.getByLabel('Monthly allocation (tokens), slider').focus();
  await page.keyboard.press('ArrowRight');
  await expect.poll(() => puts().length).toBe(1);
  expect(puts()[0]).toEqual({ put: '/api/campaigns/token-sharing/formulation/values', body: { values: { B: 4_100_000 }, run: true, wait: 3 } });
  await expect(page.getByRole('article', { name: 'Cell cap_code' })).toContainText('cap 11389');
  await expect(panel.getByRole('status')).toContainText('Saved and ran in 0.3 s');
  // A typed number is sent when it is entered.
  await panel.getByLabel('Seats', { exact: true }).fill('10');
  await panel.getByLabel('Seats', { exact: true }).press('Enter');
  await expect.poll(() => puts().length).toBe(2);
  expect(puts()[1].body.values).toEqual({ seats: 10 });
  await expect(page.getByRole('article', { name: 'Cell cap_code' })).toContainText('cap 13667');
  // A value the lead agent set is marked until the researcher confirms it; confirming does not run the notebook.
  await panel.getByText('2 more parameters').click();
  await expect(panel.getByText('set by the lead agent')).toBeVisible();
  await expect(panel.getByText('not used by any cell yet')).toBeVisible();
  await panel.getByRole('button', { name: 'Confirm' }).click();
  await expect.poll(() => puts().length).toBe(3);
  expect(puts()[2].body).toEqual({ values: { carry: false }, run: false, wait: 3 });
  await expect(panel.getByText('set by the lead agent')).toHaveCount(0);
  // The researcher adds a parameter without the agent.
  await panel.getByRole('button', { name: 'Add a parameter' }).click();
  const form = page.getByRole('form', { name: 'New parameter' });
  await form.getByLabel('Name in code').fill('absence');
  await form.getByLabel('Label').fill('Share of people absent on a day');
  await form.getByLabel('Value').fill('0.1');
  await form.getByLabel('Minimum').fill('0');
  await form.getByLabel('Maximum').fill('1');
  await form.getByLabel('Source').selectOption('assumed');
  await form.getByRole('button', { name: 'Add' }).click();
  await expect.poll(() => lab.commands.filter(entry => entry.operation === 'formulation.edit').length).toBe(1);
  const declared = lab.commands.find(entry => entry.operation === 'formulation.edit');
  expect(declared.payload).toEqual({ operations: [{ op: 'parameter.declare', name: 'absence', label: 'Share of people absent on a day', kind: 'number',
    basis: 'assumed', prominent: false, minimum: 0, maximum: 1, value: 0.1 }], reason: 'Added a parameter' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
