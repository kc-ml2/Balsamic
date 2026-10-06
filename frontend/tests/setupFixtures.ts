import type { Page } from '@playwright/test';

// Shared mocks for the New campaign form and the problem importer.
export const adapters = [
  { id: 'meent_grating', name: 'MEENT binary grating', configuration_schema: { type: 'object', properties: {
    n_cells: { type: 'integer', minimum: 2, default: 64 }, wavelength_nm: { type: 'number', default: 1100 } } },
    fidelity_schema: { type: 'object', properties: { fourier_order: { type: 'integer', minimum: 1 } } } },
  { id: 'meent_2d_dual_polarization_deflector', name: 'FLRL 2D silicon/air dual-polarization beam deflector',
    configuration_schema: { type: 'object', required: ['wavelength_nm', 'thickness_nm', 'silicon_index_source'], properties: {
      wavelength_nm: { type: 'number', exclusiveMinimum: 0 }, thickness_nm: { type: 'number', exclusiveMinimum: 0 }, silicon_index_source: { type: 'string' } } },
    fidelity_schema: { type: 'object', properties: { rcwa_order_x: { type: 'integer', minimum: 1 }, rcwa_order_y: { type: 'integer', minimum: 1 } } } },
];
export const flrl = { id: 'flrl_2d_deflector_1050nm_75deg', name: '2D dual-polarization beam deflector · Octavian FLRL', source: 'installed', order: 20,
  summary: 'Periodic 2D silicon/air metasurface.', instances: [{ name: '1050 nm, 75°', problem_id: 'meent_2d_dual_polarization_deflector', split: 'development',
    configuration: { wavelength_nm: 1050, thickness_nm: 325, silicon_index_source: 'FLRL CSV' }, fidelity: { rcwa_order_x: 10, rcwa_order_y: 5 } }],
  campaign: { name: '2D deflector — Octavian FLRL', objective: 'Reproduce the FLRL condition.', compute_budget_seconds: 248400,
    delegated_trial_seconds: 3600, autonomy: 'delegated' } };
const grating = { id: 'meent_grating_1100nm_50deg', name: '1D binary grating · 1100 nm, 50° deflector', source: 'installed', order: 10,
  summary: '64-cell grating.', instances: [{ name: '1100 nm · 50° deflector', problem_id: 'meent_grating', configuration: { n_cells: 64, wavelength_nm: 1100, material: 'constant' } }],
  campaign: { name: 'Optimizer research' } };

export async function emptyWorkspace(page: Page, extra: (path: string, method: string, body: any) => any = () => undefined) {
  await page.addInitScript(() => { (window as any).EventSource = class extends EventTarget { close() {} }; });
  const commands: any[] = [];
  const state: any = { workspace_id: 'setup-workspace', campaigns: [], campaign: null, tasks: [], hypotheses: [], trials: [], decisions: [],
    messages: [], events: [], research_runs: [], algorithms: [], settings: { llm_configured: false }, event_cursor: 1 };
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
    let body: any;
    try { body = method === 'GET' ? undefined : request.postDataJSON(); } catch { body = undefined; }  // raw uploads
    const custom = extra(path, method, body);
    if (custom !== undefined) return route.fulfill({ json: custom });
    if (path === '/api/state') return route.fulfill({ json: state });
    if (path === '/api/v1/problems') return route.fulfill({ json: { problems: adapters } });
    if (path === '/api/v1/problem-examples') return route.fulfill({ json: { examples: [grating, flrl] } });
    if (path === '/api/v1/commands' && method === 'POST') {
      commands.push(body);
      const campaign = { id: body.campaign_id, version: 1, ...body.payload };
      Object.assign(state, { campaign, campaigns: [campaign], tasks: body.payload.tasks.map((task: any, i: number) => ({ ...task, id: `task-${i}` })) });
      return route.fulfill({ json: { id: body.id, request: body, actor: 'researcher', status: 'completed', outcome: { campaign } } });
    }
    return route.fulfill({ json: {} });
  });
  return { commands };
}
