import { test, expect } from '@playwright/test';
import { mockWorkspace } from './mocks/workspace';
import { THREAD_ID, CAMPAIGN_ID } from './mocks/fixtures';

test('the lead\'s turn streams as text, steps and a finished run; sending goes through the bus with the thread as origin', async ({ page }) => {
  const recorded = await mockWorkspace(page);
  await page.goto(`/#t/${THREAD_ID}/hypotheses`);
  await expect(page.getByTestId('thread-chat')).toContainText('Lead waiting');
  const push = (event: unknown) => page.evaluate(e => (window as any).__pushChat(e), event);
  await push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'pi_run_1' });
  await push({ type: 'TEXT_MESSAGE_START', messageId: 'live_pi_run_1', role: 'assistant' });
  await push({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'live_pi_run_1', delta: 'Looking at the runs' });
  await push({ type: 'TOOL_CALL_START', toolCallId: 'pi_run_1:0', toolCallName: 'Reading the campaign\'s state', parentMessageId: 'live_pi_run_1' });
  const live = page.getByTestId('live-turn');
  await expect(live).toContainText('Looking at the runs');
  await expect(live.locator('.step')).toHaveText(/Reading the campaign/);
  await push({ type: 'TOOL_CALL_END', toolCallId: 'pi_run_1:0', link: '#experiments/trial_1' });
  await expect(live.locator('.step.done a')).toHaveAttribute('href', `#t/${THREAD_ID}/experiments/trial_1`);
  await push({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'live_pi_run_1', delta: ' now.' });
  await push({ type: 'RUN_FINISHED', threadId: THREAD_ID, runId: 'pi_run_1' });
  await expect(live).toContainText('finished');
  // Sending a message: one agent.message command, origin thread set, X-Workspace-Id on the write.
  await page.getByTestId('composer').fill('Compare the two');
  await page.getByTestId('send').click();
  await expect.poll(() => recorded.commands.length).toBe(1);
  const command = recorded.commands[0] as any;
  expect(command.operation).toBe('agent.message');
  expect(command.campaign_id).toBe(CAMPAIGN_ID);
  expect(command.expected_revision).toBe(3);
  expect(command.payload).toMatchObject({ message: 'Compare the two', thread_id: THREAD_ID });
  expect(command.origin).toMatchObject({ thread_id: THREAD_ID, actor: 'researcher' });
  expect(command.id).toMatch(/^ui_[0-9a-f]{32}$/);
});

test('a failed turn is said to have failed with its work saved; an SSE frame refetches the pages it names', async ({ page }) => {
  await mockWorkspace(page);
  await page.goto(`/#t/${THREAD_ID}/inbox`);
  await page.evaluate(() => (window as any).__pushChat({ type: 'RUN_STARTED', threadId: 't', runId: 'r' }));
  await page.evaluate(() => (window as any).__pushChat({ type: 'RUN_ERROR', threadId: 't', runId: 'r', message: 'The provider refused' }));
  await expect(page.getByTestId('live-turn')).toContainText('The provider refused');
  // The inbox's first read gives the cursor a later read starts after; a frame before it lands has nothing to start after.
  await expect(page.getByText('Extend trial trial_1 by 120 s?').first()).toBeVisible();
  const requests: string[] = [];
  page.on('request', r => { if (r.url().includes('/api/v1/campaigns/demo/inbox')) requests.push(r.url()); });
  await page.evaluate(() => (window as any).__pushEvent({ last_event_id: 11, events: [{ id: 11, kind: 'decision.resolved', record_id: 'extension_1', campaign_id: 'demo', status: 'resolved' }] }));
  await expect.poll(() => requests.length).toBeGreaterThan(0);
  expect(requests.at(-1)).toContain('after=10');
});

test('a bar under the chat header says what the lead is doing; opened, it lists the agents and the runs', async ({ page }) => {
  await mockWorkspace(page);
  await page.goto(`/#t/${THREAD_ID}/inbox`);
  const bar = page.getByTestId('now-toggle');
  await expect(bar).toContainText('Lead waiting');
  await bar.click();
  const panel = page.getByTestId('now-panel');
  await expect(panel.getByTestId('now-agent').first()).toContainText('Lead');
  await expect(panel).toContainText('Runs:');
  await bar.click();
  await expect(panel).toHaveCount(0);
});
