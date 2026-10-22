import { test, expect } from '@playwright/test';
import { mockWorkspace } from './mocks/workspace';
import * as f from './mocks/fixtures';
import { THREAD_ID, CAMPAIGN_ID } from './mocks/fixtures';

const stuck = { id: 'issue_dep', code: 'dependency_incomplete', status: 'pending', revision: 1, options: ['Discuss with the lead', 'Defer'],
  created_at: '2026-10-22T20:18:34Z', affected: 'trial_b',
  message: 'trial_b is to start after trial_a, which ended without completing (wall budget exhausted), so it cannot start. Start them anyway in their order, or stop them.',
  actions: [{ id: 'start_anyway', label: 'Start them anyway', description: 'Each waiting run starts once the run before it has ended.' },
            { id: 'stop', label: 'Stop them', description: 'The waiting runs are stopped.' }] };
const asked = { id: 'question_1', status: 'pending', created_at: '2026-10-22T21:04:58Z', question: 'Two GPU runs no longer fit before 22:04. Which pair?',
  suggestion: 'Run the seed 101 pair now.', options: ['Run the seed 101 pair', 'Run both pairs past 22:04'] };

test('an issue offers what can be done about it, and a question comes with the lead\'s suggestion and one-click answers', async ({ page }) => {
  const recorded = await mockWorkspace(page, { [`/api/v1/campaigns/${CAMPAIGN_ID}/inbox`]: { ...f.inbox, issues: [stuck], questions: [asked] } });
  await page.goto(`/#t/${THREAD_ID}/inbox`);
  await expect(page.getByTestId('issue-action')).toHaveText(['Start them anyway', 'Stop them']);
  await expect(page.getByTestId('issue')).toContainText('Mark as handled');
  await page.getByTestId('issue-action').first().click();
  await expect.poll(() => recorded.commands.find(c => c.operation === 'issue.resolve')?.payload).toMatchObject({ issue_id: 'issue_dep', choice: 'action', action: 'start_anyway' });
  await expect(page.getByTestId('question-suggestion')).toContainText('Run the seed 101 pair now.');
  await page.getByTestId('question-option').first().click();
  await expect.poll(() => recorded.commands.find(c => c.operation === 'agent.message')?.payload).toMatchObject({ message: 'Run the seed 101 pair', question_id: 'question_1' });
});
