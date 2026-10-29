import fs from 'node:fs';
import path from 'node:path';
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const FIXTURES = path.resolve(import.meta.dirname, '../src/fixtures');
const NOW = new Date('2026-10-25T12:30:00Z');
const WCAG = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'];

/** Serve `/Sentinel/snapshot/**` from a fixture set, or fail every request with `status`. */
async function serveSnapshot(page: Page, set: 'populated' | 'empty' | { status: number }) {
  await page.route('**/Sentinel/snapshot/**', async (route) => {
    if (typeof set !== 'string') {
      await route.fulfill({ status: set.status, body: 'unavailable' });
      return;
    }
    const rel = new URL(route.request().url()).pathname.split('/Sentinel/snapshot/')[1] ?? '';
    const file = path.join(FIXTURES, set, rel);
    if (!file.startsWith(path.join(FIXTURES, set)) || !fs.existsSync(file)) {
      await route.fulfill({ status: 404, body: 'not found' });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: fs.readFileSync(file, 'utf8') });
  });
}

async function axe(page: Page, include?: string) {
  let builder = new AxeBuilder({ page }).withTags(WCAG);
  if (include) builder = builder.include(include);
  const { violations } = await builder.analyze();
  expect(violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
}

const chainRows = (page: Page) => page.getByRole('list', { name: 'Chains' }).locator(':scope > [data-row-index]');

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(NOW);
});

test('smoke_populated_and_axe', async ({ page }, info) => {
  await serveSnapshot(page, 'populated');
  await page.goto('./');
  await expect(page.getByRole('heading', { level: 1, name: 'Sentinel Ops' })).toBeVisible();
  const overview = page.getByRole('region', { name: 'Overview' });
  await expect(overview.locator('button[aria-pressed]')).toHaveCount(7);
  await expect(chainRows(page)).toHaveCount(6);
  await expect(page.locator('[data-needs-action]')).toHaveCount(3);
  await expect(page.locator('html')).toHaveClass(info.project.name === 'dark' ? /\bdark\b/ : /^(?!.*\bdark\b)/);
  // Let the lazy chart chunk settle before scanning.
  await expect(page.locator('[data-trend-chart] svg')).toBeVisible();
  await axe(page);
});

test('smoke_drawer_deeplink_and_axe', async ({ page }) => {
  await serveSnapshot(page, 'populated');
  await page.goto('./');
  await page.getByRole('button', { name: 'Open details for deployfail/02: reverted by PR #55' }).click();
  const dialog = page.getByRole('dialog', { name: 'deployfail/02: reverted by PR #55' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('switch', { name: 'Show traces' }).click();
  await expect(page).toHaveURL(/\?chain=c_2c8f0e7a5b1d3f94&traces=1$/);
  await page.reload();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('list', { name: 'Trace spans' }).getByRole('listitem')).toHaveCount(7);
  await axe(page, '[role="dialog"]');
});

test('smoke_theme_persists_without_flash', async ({ page }) => {
  await serveSnapshot(page, 'populated');
  await page.goto('./');
  await page.getByRole('radio', { name: 'Dark theme' }).click();
  await expect(page.locator('html')).toHaveClass(/\bdark\b/);
  await page.reload({ waitUntil: 'domcontentloaded' });
  expect(await page.evaluate(() => document.documentElement.classList.contains('dark'))).toBe(true);
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).backgroundColor)).toBe('rgb(10, 14, 23)');
});

test('smoke_states', async ({ page }) => {
  await serveSnapshot(page, 'empty');
  await page.goto('./');
  await expect(page.getByText('No activity recorded yet')).toBeVisible();

  await page.unrouteAll();
  await serveSnapshot(page, { status: 503 });
  await page.reload();
  const alert = page.getByRole('alert');
  await expect(alert).toContainText('Server returned 503');
  await expect(alert.getByRole('button', { name: 'Retry' })).toBeVisible();

  await page.unrouteAll();
  await serveSnapshot(page, 'populated');
  await page.clock.setFixedTime(new Date('2026-10-25T14:00:00Z'));
  await page.reload();
  await expect(page.getByText(/Snapshot is 2 h old/)).toBeVisible();
});

test('smoke_no_horizontal_overflow', async ({ page }) => {
  await serveSnapshot(page, 'populated');
  await page.goto('./');
  await expect(chainRows(page)).toHaveCount(6);
  const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(await overflow()).toBeLessThanOrEqual(0);
  await page.getByRole('button', { name: /^Show stages for runtime\/04/ }).click();
  expect(await overflow()).toBeLessThanOrEqual(0);
  await page.getByRole('button', { name: 'Open details for deployfail/02: reverted by PR #55' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('switch', { name: 'Show raw event JSON' }).click();
  expect(await overflow()).toBeLessThanOrEqual(0);
});

test('smoke_no_fixtures_in_build', async ({ page, baseURL }) => {
  const outside: string[] = [];
  page.on('request', (req) => {
    const url = req.url();
    if (url.startsWith('data:')) return;
    if (!url.startsWith(baseURL ?? 'http://localhost:4173/Sentinel/')) outside.push(url);
  });
  await serveSnapshot(page, 'populated');
  await page.goto('./');
  await expect(chainRows(page)).toHaveCount(6);
  const res = await page.request.get('./src/fixtures/populated/meta.json');
  const body = await res.text();
  expect(res.status() === 404 || !body.includes('"ops.snapshot.v1"')).toBe(true);
  expect(outside).toEqual([]);
});
