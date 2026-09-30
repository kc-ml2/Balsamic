import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';

export default defineConfig({
  testDir: './tests', testMatch: 'report-review.spec.ts', fullyParallel: true, reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:8794', viewport: { width: 1440, height: 1000 },
    launchOptions: { executablePath: process.env.CHROME_PATH || (existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : undefined) },
    trace: 'retain-on-failure' },
  webServer: { command: '../.venv/bin/python ../scripts/serve_report_review_fixture.py --port 8794',
    url: 'http://127.0.0.1:8794/api/health', reuseExistingServer: false },
});
