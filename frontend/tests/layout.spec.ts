import { test, expect } from '@playwright/test';
import { mockWorkspace } from './mocks/workspace';
import * as f from './mocks/fixtures';

const { THREAD_ID, CAMPAIGN_ID } = f;

test('the overview page is first on the rail and holds the campaign at a glance and its removal', async ({ page }) => {
  await mockWorkspace(page);
  await page.goto(`/#t/${THREAD_ID}/overview`);
  const rail = page.locator('.rail a');
  await expect(rail.first()).toHaveText(/Overview/);
  const overview = page.getByTestId('overview');
  await expect(overview).toContainText('Demo campaign');
  await expect(overview.locator('.stat')).toHaveCount(5);
  await expect(overview.locator('svg.meter')).toHaveCount(2);
  await expect(overview.getByTestId('remove-campaign')).toBeVisible();
  await page.goto(`/#t/${THREAD_ID}/monitor/resources`);
  await expect(page.getByTestId('remove-campaign')).toHaveCount(0);
});

test('the resource level reads the admission level and sets it with one click', async ({ page }) => {
  let put: unknown = null;
  await mockWorkspace(page);
  await page.route('**/api/v1/resources/level', async route => { put = route.request().postDataJSON(); await route.fulfill({ json: { level: 'cautious', set_by: 'researcher' } }); });
  await page.goto(`/#t/${THREAD_ID}/monitor/resources`);
  const levels = page.getByTestId('resource-levels');
  await expect(levels).toHaveAttribute('data-level', 'balanced');
  await expect(page.getByTestId('level-balanced')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('level-cautious')).toContainText('Uses at most 60% of memory.');
  await page.getByTestId('level-cautious').click();
  await expect.poll(() => put).toEqual({ level: 'cautious', reason: 'set from the monitor' });
});

test('the page agent desk is docked under its page, opens with its conversation, and sends through the bus', async ({ page }) => {
  const recorded = await mockWorkspace(page);
  await page.goto(`/#t/${THREAD_ID}/hypotheses`);
  const desk = page.getByTestId('desk-discussion_moderator');
  await expect(desk).toHaveAttribute('data-mode', 'docked');
  await expect(desk).toContainText('Discussion moderator');
  await desk.getByTestId('desk-head').click();
  await expect(desk.getByTestId('desk-turn')).toHaveCount(2);
  await desk.getByTestId('desk-composer').fill('Review H02 again');
  await desk.getByTestId('desk-composer-send').click();
  await expect.poll(() => recorded.commands.length).toBeGreaterThan(0);
  expect(recorded.commands.at(-1)).toMatchObject({ operation: 'agent.message', payload: { page_agent: 'discussion_moderator', message: 'Review H02 again' } });
  await page.goto(`/#t/${THREAD_ID}/monitor/resources`);
  await expect(page.getByTestId('desk-resource_planner')).toBeVisible();
  await page.goto(`/#t/${THREAD_ID}/inbox`);
  await expect(page.locator('.desk')).toHaveCount(0);
});

test('the columns resize by dragging the gutters and the rail folds to initials; both survive a reload', async ({ page }) => {
  await mockWorkspace(page);
  await page.goto(`/#t/${THREAD_ID}/hypotheses`);
  const rail = page.locator('.rail');
  const before = (await rail.boundingBox())!.width;
  const gutter = page.getByTestId('gutter-rail');
  const box = (await gutter.boundingBox())!;
  await page.mouse.move(box.x + 2, box.y + 100); await page.mouse.down(); await page.mouse.move(box.x + 80, box.y + 100, { steps: 4 }); await page.mouse.up();
  await expect.poll(async () => (await rail.boundingBox())!.width).toBeGreaterThan(before + 40);
  const chat = page.getByTestId('thread-chat');
  const chatBefore = (await chat.boundingBox())!.width;
  const chatGutter = (await page.getByTestId('gutter-chat').boundingBox())!;
  await page.mouse.move(chatGutter.x + 2, chatGutter.y + 100); await page.mouse.down(); await page.mouse.move(chatGutter.x - 120, chatGutter.y + 100, { steps: 4 }); await page.mouse.up();
  await expect.poll(async () => (await chat.boundingBox())!.width).toBeLessThan(chatBefore - 60);
  await page.getByTestId('rail-toggle').click();
  await expect(rail).toHaveAttribute('data-collapsed', '1');
  await expect(page.getByTestId('rail-hypotheses').locator('span').first()).toHaveText('Hy');
  await expect(page.getByTestId('rail-hypotheses').locator('.count')).toBeHidden();
  await page.reload();
  await expect(page.locator('.rail')).toHaveAttribute('data-collapsed', '1');
  await page.getByTestId('rail-toggle').click();
  await expect(page.getByTestId('rail-hypotheses')).toHaveText(/Hypotheses/);
});

test('the next formulation step is called out on every page until the charter is confirmed', async ({ page }) => {
  const step = { kind: 'confirm', title: 'Confirm the charter', text: 'No experiment can start until you confirm the charter.', page: '#note/charter', blocking: true, proposals: 5 };
  await mockWorkspace(page, { '/api/v1/campaigns': { campaigns: [{ ...f.campaignRow, next_step: step }] } });
  await page.goto(`/#t/${THREAD_ID}/hypotheses`);
  const callout = page.getByTestId('next-step');
  await expect(callout).toContainText('5 proposals wait for this: confirm the charter');
  await expect(callout).toHaveClass(/attention/);
  await callout.getByTestId('next-step-open').click();
  await expect(page).toHaveURL(new RegExp(`#t/${THREAD_ID}/note/charter`));
  await expect(page.getByTestId('next-step')).toBeVisible();
  await expect(page.getByTestId('next-step-open')).toHaveCount(0);
  await mockWorkspace(page);
  await page.goto(`/#t/${THREAD_ID}/overview`);
  await page.reload();
  await expect(page.getByTestId('next-step')).toHaveCount(0);
});

test('an evaluator that cannot start in this lab is called out with what to install, and the overview says so', async ({ page }) => {
  const error = 'ModuleNotFoundError: The 2D MEENT solver runs on PyTorch, which is not installed; install it with scripts/install-meent';
  const step = { kind: 'evaluator', title: "Install the evaluator's software", text: `The evaluator of meent_2d_dual_polarization_deflector cannot start in this lab: ${error}. No experiment or runtime probe can run on it until that is fixed.`,
    page: '', blocking: false, proposals: 0, missing: [], error, problem_id: 'meent_2d_dual_polarization_deflector' };
  await mockWorkspace(page, { '/api/v1/campaigns': { campaigns: [{ ...f.campaignRow, next_step: step }] } });
  await page.goto(`/#t/${THREAD_ID}/overview`);
  const callout = page.getByTestId('next-step');
  await expect(callout).toHaveAttribute('data-kind', 'evaluator');
  await expect(callout).toContainText("Next: install the evaluator's software");
  await expect(callout).toContainText('scripts/install-meent');
  await expect(callout.getByTestId('next-step-open')).toHaveCount(0);
  await expect(page.getByTestId('evaluator-blocked')).toHaveText(error);
  await expect(page.getByTestId('overview')).toContainText('defined');
});

test('status pills are inert and controls are buttons: the monitor and context controls carry the button class', async ({ page }) => {
  await mockWorkspace(page);
  await page.goto(`/#t/${THREAD_ID}/hypotheses`);
  await expect(page.getByTestId('monitor-pill')).toHaveClass(/\bbtn\b/);
  await expect(page.getByTestId('context-chip')).toHaveClass(/\bbtn\b/);
  await expect(page.getByTestId('autopilot-toggle')).toHaveClass(/\bbtn\b/);
  await expect(page.getByTestId('stage')).not.toHaveClass(/\bbtn\b/);
  const cursor = await page.getByTestId('stage').evaluate(el => getComputedStyle(el.closest('.pill')!).cursor);
  expect(cursor).toBe('default');
});
