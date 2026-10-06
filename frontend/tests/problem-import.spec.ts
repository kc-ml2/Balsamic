import { test, expect } from '@playwright/test';
import { emptyWorkspace, flrl } from './setupFixtures';

const draft = { title: 'FLRL beam deflector', summary: 'A 2D dual-polarization deflector.', objective: 'Maximize mean TE/TM +1 transmission at 75 degrees.',
  instances: flrl.instances, assumptions: ['Normal incidence'], open_questions: ['Which silicon index table?'], evaluator_notes: '',
  citations: [{ source: 'documents/main.pdf.txt', location: 'page 3', quote: 'deflects 1050 nm light to 75°' }] };
const ready = { id: 'import_ready', title: 'Octavian FLRL', status: 'ready', created_at: '2026-10-06T08:00:00Z', model: { provider: 'deepseek', model: 'deepseek-v4-pro', effort: 'high' },
  billing: 'api', budget_usd: 2, usage: { calls: 12, input: 90000, output: 8000, charged_usd: 0.05, cost_usd: 0.05 }, documents: [{ name: 'main.pdf' }],
  code: { kind: 'git', source: 'https://github.com/org/flrl.git', commit: '7838e71313d71cee8e2db3b432f41f80b9106a95', files: 42 },
  draft, reply: 'Formulated the 2D condition; one open question.', activity: [{ at: '2026-10-06T08:01:00Z', kind: 'tool', text: 'Read documents/main.pdf.txt' }] };

test('an import uploads its material in order and its draft opens in the New campaign form', async ({ page }) => {
  const calls: string[] = [], created: any[] = [];
  const { commands } = await emptyWorkspace(page, (path, method, body) => {
    if (path.startsWith('/api/v1/problem-imports')) calls.push(`${method} ${path}${body?.kind ? ` ${body.kind}` : ''}`);
    if (path === '/api/v1/model-tiers') return { mode: 'dev', revision: 1, saved: true, providers: { deepseek: { billing: 'api' } }, models: [],
      tiers: [{ id: 'strong', label: 'Strong', model: { provider: 'openai-codex', model: 'gpt-6-sol', effort: 'xhigh' } },
              { id: 'medium', label: 'Medium', model: { provider: 'deepseek', model: 'deepseek-v4-flash', effort: null } }],
      roles: { problem_importer: 'medium' }, assignable: ['lead', 'problem_importer'] };
    if (path === '/api/v1/problem-imports' && method === 'POST') { created.push(body); return { id: 'import_new', status: 'collecting' }; }
    if (path === '/api/v1/problem-imports' && method === 'GET') return { imports: [{ ...ready, has_draft: true }] };
    if (path === '/api/v1/problem-imports/import_new/start') return { ...ready, id: 'import_new', status: 'running', draft: null, reply: null };
    if (path.startsWith('/api/v1/problem-imports/import_new')) return method === 'GET' ? { ...ready, id: 'import_new', status: 'running', draft: null, reply: null } : { id: 'import_new' };
    if (path === '/api/v1/problem-imports/import_ready') return ready;
    return undefined;
  });
  await page.goto('/#problem-import');
  await expect(page.getByRole('heading', { name: 'Formulate a problem from papers and code.' })).toBeVisible();
  await page.getByLabel('Documents').setInputFiles({ name: 'main.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4') });
  await page.getByLabel('Code base').selectOption('git');
  await page.getByLabel('Repository URL').fill('https://github.com/org/flrl.git');
  await expect(page.getByLabel('Model tier')).toHaveValue('medium');
  await expect(page.getByText('deepseek/deepseek-v4-flash. Tiers are set on the Models page.')).toBeVisible();
  await page.getByLabel('Model tier').selectOption('strong');
  await page.getByRole('button', { name: 'Start import' }).click();
  await expect(page.getByText('The importer is reading the material.')).toBeVisible();
  expect(created[0]).toMatchObject({ tier: 'strong', budget_usd: 2 });
  expect(calls.filter(call => !call.startsWith('GET'))).toEqual(['POST /api/v1/problem-imports', 'PUT /api/v1/problem-imports/import_new/documents/main.pdf',
    'POST /api/v1/problem-imports/import_new/code git', 'POST /api/v1/problem-imports/import_new/start']);

  await page.getByRole('button', { name: /Octavian FLRL/ }).click();
  await expect(page.getByRole('heading', { name: 'FLRL beam deflector' })).toBeVisible();
  await expect(page.getByText('Which silicon index table?')).toBeVisible();
  await expect(page.getByText('deflects 1050 nm light to 75°')).toBeVisible();
  await expect(page.getByText('$0.0500 of $2.00')).toBeVisible();
  await page.getByRole('button', { name: 'Use in a new campaign' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Campaign name')).toHaveValue('FLRL beam deflector');
  await expect(dialog.getByLabel('Research objective')).toHaveValue(draft.objective);
  await expect(dialog.getByLabel('Problem adapter')).toHaveValue('meent_2d_dual_polarization_deflector');
  await dialog.getByRole('button', { name: 'Create campaign', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  expect(commands[0].payload.tasks[0].fidelity).toEqual({ rcwa_order_x: 10, rcwa_order_y: 5 });
});
