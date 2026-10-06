import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

test.skip(({ baseURL }) => baseURL !== 'http://127.0.0.1:8794', 'Requires the isolated service in playwright.reports.config.ts.');

async function select(page: Page, phrase: string, section = 'one') {
  await page.evaluate(({ phrase, section }) => {
    const parent = document.querySelector(`#report-content #${section}`)!;
    const walker = document.createTreeWalker(parent, NodeFilter.SHOW_TEXT, { acceptNode(node) {
      return node.parentElement!.closest('svg,script,style,input,button,label') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    }});
    const nodes: { node: Node; start: number; end: number }[] = [];
    let current, text = '';
    while ((current = walker.nextNode())) { const start = text.length; text += current.textContent; nodes.push({ node: current, start, end: text.length }); }
    const start = text.indexOf(phrase), end = start + phrase.length;
    if (start < 0) throw Error(`Missing phrase: ${phrase}`);
    const first = nodes.find(item => item.start <= start && start < item.end)!, last = nodes.find(item => item.start < end && end <= item.end)!;
    const range = document.createRange(); range.setStart(first.node, start - first.start); range.setEnd(last.node, end - last.start);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
  }, { phrase, section });
  await expect(page.locator('#review-selection')).toContainText('characters selected');
}

test('cross-element Unicode marks, overlap, remarks, undo, persistence and submission', async ({ page, request }) => {
  const report = await (await request.post('/_fixture/report')).json();
  await page.goto('/reports/' + report.id);
  const originalText = await page.locator('#report-content').textContent();
  const originalFigure = await page.locator('figure').innerHTML();
  await select(page, '😀 very strong phrase and');
  await page.getByRole('button', { name: 'Very good', exact: false }).click();
  expect(await page.locator('mark[data-tier=good]').allTextContents()).toEqual(['😀 ', 'very strong phrase', ' and']);
  await page.locator('.review-card').getByRole('button', { name: 'Add remark' }).click();
  await page.getByLabel('Remark', { exact: true }).fill('Keep this wording. <script>bad()</script>');
  await page.getByRole('button', { name: 'Save remark' }).click();
  await select(page, 'weak claim'); await page.keyboard.press('3');
  await select(page, 'claim'); await page.keyboard.press('4');
  await expect(page.locator('mark[data-tier=poor]')).toHaveText('weak ');
  await expect(page.locator('mark[data-tier=bad]')).toHaveText('claim');
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect(page.locator('mark[data-tier=poor]')).toHaveText('weak claim');
  await page.getByLabel('Overall remarks').fill('Keep the scope modest.');
  await page.reload();
  await expect(page.locator('.review-remark').first()).toContainText('Keep this wording. <script>bad()</script>');
  await expect(page.locator('mark[data-tier=poor]')).toHaveText('weak claim');
  expect((await (await request.get('/api/reports/' + report.id)).json()).feedback).toBeNull();
  await page.getByRole('button', { name: 'Submit feedback', exact: true }).click();
  await expect(page.locator('#review-receipt')).toContainText('Submitted ·');
  await expect(page.locator('#review-save-status')).toContainText('Submitted review');
  await expect(page.getByRole('button', { name: 'Revise draft with writer' })).toBeEnabled();
  const saved = await (await request.get('/api/reports/' + report.id)).json();
  expect(saved.feedback.annotations).toHaveLength(2);
  expect(saved.feedback.annotations[0].context).toContain('A 😀 very strong phrase');
  expect(saved.jobs).toEqual([]);
  expect(await page.locator('#report-content').textContent()).toBe(originalText);
  expect(await page.locator('figure').innerHTML()).toBe(originalFigure);
  await page.screenshot({ path: test.info().outputPath('review.png'), fullPage: true });
});

test('self-contained HTML works offline and coworkers can return reviewed HTML', async ({ page, request, browser }) => {
  const report = await (await request.post('/_fixture/report')).json();
  await page.goto('/reports/' + report.id);
  await select(page, 'very strong phrase'); await page.keyboard.press('1');
  await page.getByText('Share & export', { exact: true }).click();
  const shared = page.waitForEvent('download'); await page.getByRole('button', { name: 'Download review HTML', exact: true }).click();
  const file = test.info().outputPath('shared.html'); await (await shared).saveAs(file);
  const offline = await browser.newContext({ offline: true }); const coworker = await offline.newPage();
  const errors: string[] = []; coworker.on('pageerror', error => errors.push(error.message));
  await coworker.goto(pathToFileURL(file).href);
  await expect(coworker.locator('mark[data-tier=good]')).toHaveText('very strong phrase');
  await select(coworker, 'weak claim'); await coworker.keyboard.press('4');
  await coworker.getByLabel('Overall remarks').fill('Coworker: remove the unsupported claim.');
  await coworker.getByLabel('Writing focus', { exact: true }).fill('Explain practical cost rather than general superiority.');
  const returned = coworker.waitForEvent('download'); await coworker.getByRole('button', { name: 'Download reviewed HTML', exact: true }).click();
  const reviewed = test.info().outputPath('reviewed.html'); await (await returned).saveAs(reviewed);
  await offline.close();
  await page.locator('#review-import').setInputFiles(reviewed);
  await expect(page.locator('mark[data-tier=bad]')).toHaveText('weak claim');
  await expect(page.getByLabel('Overall remarks')).toHaveValue('Coworker: remove the unsupported claim.');
  await page.getByRole('button', { name: 'Submit feedback', exact: true }).click();
  await expect(page.locator('#review-receipt')).toContainText('Submitted ·');
  const data = await (await request.get('/api/reports/' + report.id)).json();
  expect(data.feedback.annotations.map((mark: { tier: string }) => mark.tier).sort()).toEqual(['bad', 'good']);
  expect(data.feedback.focus).toBe('Explain practical cost rather than general superiority.');
  expect(errors).toEqual([]);
});

test('rough notes produce a reviewed draft with editable focus and portable writing notes', async ({ page, browser }) => {
  await page.goto('/#reports');
  await page.getByLabel('Your observations and intended focus').fill('Wall time matters. Keep the conclusion narrow.');
  await page.getByRole('button', { name: 'Write a draft', exact: true }).click();
  const progress = page.getByRole('article', { name: 'Writing progress' }).first();
  await expect(progress).toContainText('Draft ready', { timeout: 15000 });
  await progress.getByRole('link', { name: 'Open draft', exact: false }).click();
  await expect(page.getByLabel('Writing focus', { exact: true })).toHaveValue('Practical cost under limited evidence');
  await page.getByText('Writing notes & evidence limits', { exact: true }).click();
  await expect(page.locator('#review-editorial-details')).toContainText('Matched-budget replication');
  const artifact = page.waitForEvent('download'); await page.getByRole('link', { name: 'Download full writing record' }).click();
  const artifactPath = test.info().outputPath('writing-record.json'); await (await artifact).saveAs(artifactPath);
  const record = JSON.parse(await readFile(artifactPath, 'utf8'));
  expect(record.stages.some((stage: { role: string }) => stage.role === 'report_scientific_reviewer')).toBe(true);
  expect(record.stages.some((stage: { role: string }) => stage.role === 'report_reader_reviewer')).toBe(true);
  expect(record.snapshot.content_hash).toBeTruthy();
  await page.getByLabel('Writing focus', { exact: true }).fill('Make the practical recommendation explicit.');
  await page.getByRole('button', { name: 'Submit feedback', exact: true }).click();
  await expect(page.locator('#review-save-status')).toContainText('Submitted review');
  await page.getByText('Share & export', { exact: true }).click();
  const shared = page.waitForEvent('download'); await page.getByRole('button', { name: 'Download review HTML', exact: true }).click();
  const path = test.info().outputPath('staged-draft.html'); await (await shared).saveAs(path);
  const offline = await browser.newContext({ offline: true }), copy = await offline.newPage();
  await copy.goto(pathToFileURL(path).href);
  await expect(copy.getByLabel('Writing focus', { exact: true })).toHaveValue('Make the practical recommendation explicit.');
  await expect(copy.locator('#review-editorial-details')).toContainText('Exploratory evidence only');
  await expect(copy.getByRole('link', { name: 'Download full writing record' })).toHaveCount(0);
  await expect(copy.locator('#report-content')).toContainText('The campaign is exploratory.');
  await offline.close();
});

test('a fundamental focus question can be answered without replaying the brief', async ({ page }) => {
  await page.goto('/#reports');
  await page.getByLabel('Your observations and intended focus').fill('Two incompatible purposes; ask me about the focus.');
  await page.getByRole('button', { name: 'Write a draft', exact: true }).click();
  const progress = page.getByRole('article', { name: 'Writing progress' }).first();
  await expect(progress).toContainText('A question about your focus', { timeout: 15000 });
  await page.reload();
  await progress.getByLabel('Emphasize cost or learning dynamics?').fill('Cost for deployment.');
  await progress.getByRole('button', { name: 'Continue writing' }).click();
  await expect(progress).toContainText('Draft ready', { timeout: 15000 });
  await expect(progress).toContainText('9 of at most 14 model calls');
});

test('stale submissions and mismatched imports retain both reviewers work', async ({ page, request, browser }) => {
  const report = await (await request.post('/_fixture/report')).json();
  await page.goto('/reports/' + report.id);
  const otherContext = await browser.newContext(); const other = await otherContext.newPage();
  await other.goto('http://127.0.0.1:8794/reports/' + report.id);
  await select(page, 'very strong phrase'); await page.keyboard.press('1');
  await select(other, 'weak claim'); await other.keyboard.press('4');
  await page.getByRole('button', { name: 'Submit feedback', exact: true }).click();
  await expect(page.locator('#review-receipt')).toContainText('Submitted ·');
  await other.getByRole('button', { name: 'Submit feedback', exact: true }).click();
  await expect(other.getByRole('alert')).toContainText('another tab or device');
  await expect(other.locator('mark[data-tier=bad]')).toHaveText('weak claim');
  await other.getByRole('button', { name: 'Load submitted review' }).click();
  await expect(other.locator('mark[data-tier=good]')).toHaveText('very strong phrase');
  await other.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect(other.locator('mark[data-tier=bad]')).toHaveText('weak claim');
  await page.locator('#review-import').setInputFiles({ name: 'wrong.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ report_id: 'other', source_hash: report.source_hash, annotations: [], comment: '' })) });
  await expect(page.getByRole('alert')).toContainText('another report version');
  await expect(page.locator('mark[data-tier=good]')).toHaveText('very strong phrase');
  await otherContext.close();
});

test('clean exports preserve wording and figures; print hides review controls', async ({ page, request }) => {
  const report = await (await request.post('/_fixture/report')).json();
  await page.goto('/reports/' + report.id);
  await select(page, 'weak claim'); await page.keyboard.press('4');
  await page.getByText('Share & export', { exact: true }).click();
  const clean = page.waitForEvent('download'); await page.getByRole('button', { name: 'Clean HTML', exact: true }).click();
  const cleanPath = test.info().outputPath('clean.html'); await (await clean).saveAs(cleanPath);
  const cleanText = await readFile(cleanPath, 'utf8'); expect(cleanText).toContain('weak claim'); expect(cleanText).toContain('<svg'); expect(cleanText).not.toContain('data-review-id'); expect(cleanText).not.toContain('review-header');
  const md = page.waitForEvent('download'); await page.getByRole('button', { name: 'Markdown', exact: true }).click();
  const mdPath = test.info().outputPath('report.md'); await (await md).saveAs(mdPath);
  const markdown = await readFile(mdPath, 'utf8'); expect(markdown).toContain('**very strong phrase**'); expect(markdown).toContain('| Annealing | 97.81% |'); expect(markdown).toContain('<svg');
  await page.emulateMedia({ media: 'print' });
  await expect(page.locator('#review-header')).toBeHidden(); await expect(page.locator('#review-sidebar')).toBeHidden();
  expect(await page.locator('mark').evaluate(element => getComputedStyle(element).backgroundColor)).toBe('rgba(0, 0, 0, 0)');
  await page.pdf({ path: test.info().outputPath('report.pdf'), printBackground: true });
});

test('mobile review keeps controls reachable and fine clears only the selected span', async ({ page, request }) => {
  const report = await (await request.post('/_fixture/report')).json();
  await page.setViewportSize({ width: 390, height: 844 }); await page.goto('/reports/' + report.id);
  await page.getByRole('button', { name: 'Remarks', exact: false }).click();
  await select(page, 'very strong phrase'); await page.keyboard.press('1');
  await select(page, 'strong'); await page.getByRole('button', { name: 'Fine', exact: false }).click();
  expect(await page.locator('mark[data-tier=good]').allTextContents()).toEqual(['very ', ' phrase']);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('mobile.png'), fullPage: true });
});

test('a selection across sections and figure artwork submits exact surrounding prose', async ({ page, request }) => {
  const report = await (await request.post('/_fixture/report')).json();
  await page.goto('/reports/' + report.id);
  const original = await page.locator('#report-content').textContent();
  await page.evaluate(() => {
    const first = document.querySelector('#one p')!.lastChild!, last = document.querySelector('#two p')!.firstChild!;
    const range = document.createRange(); range.setStart(first, first.textContent!.indexOf('weak')); range.setEnd(last, 'Only three seeds'.length);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
  });
  await expect(page.locator('#review-selection')).toContainText('characters selected');
  await page.getByRole('button', { name: 'Poor', exact: false }).click();
  await expect(page.locator('.review-card')).toHaveCount(2);
  await expect(page.locator('svg mark')).toHaveCount(0);
  await page.getByRole('button', { name: 'Submit feedback', exact: true }).click();
  await expect(page.locator('#review-save-status')).toContainText('Submitted review');
  const data = await (await request.get('/api/reports/' + report.id)).json();
  expect(data.feedback.annotations.map((mark: { section_id: string }) => mark.section_id)).toEqual(['one', 'two']);
  expect(await page.locator('#report-content').textContent()).toBe(original);
});
