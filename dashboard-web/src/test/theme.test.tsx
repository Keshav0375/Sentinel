import fs from 'node:fs';
import path from 'node:path';
import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createFixtureClient } from '../data/fixtures-loader';
import { THEME_KEY } from '../state/theme';
import { renderApp, setPrefersDark } from './helpers';

const html = document.documentElement;

function inlineThemeScript(): string {
  const index = fs.readFileSync(path.resolve(import.meta.dirname, '../../index.html'), 'utf8');
  const m = /<script id="theme-init">([\s\S]*?)<\/script>/.exec(index);
  if (!m?.[1]) throw new Error('theme-init script missing from index.html');
  // It must run before any stylesheet or module script, i.e. right after <meta charset>.
  expect(index.indexOf('<script id="theme-init">')).toBeLessThan(index.indexOf('<meta name="viewport"'));
  return m[1];
}

describe('theme', () => {
  it('test_inline_script_matches_theme_key', () => {
    const body = inlineThemeScript();
    expect(body).toContain(`'${THEME_KEY}'`);

    localStorage.setItem(THEME_KEY, 'dark');
    new Function(body)();
    expect(html.classList.contains('dark')).toBe(true);
    expect(html.dataset.theme).toBe('dark');
    expect(html.style.colorScheme).toBe('dark');

    localStorage.clear();
    html.className = '';
    setPrefersDark(true);
    new Function(body)();
    expect(html.classList.contains('dark')).toBe(true);
    expect(html.dataset.theme).toBe('system');

    html.className = '';
    setPrefersDark(false);
    localStorage.setItem(THEME_KEY, 'nonsense');
    new Function(body)();
    expect(html.classList.contains('dark')).toBe(false);
    expect(html.dataset.theme).toBe('system');
    expect(html.style.colorScheme).toBe('light');
  });

  it('test_theme_toggle_persists', async () => {
    const { user } = renderApp(createFixtureClient('populated'));
    const group = screen.getByRole('radiogroup', { name: 'Theme' });
    expect(group).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'System theme' })).toHaveAttribute('aria-checked', 'true');

    await user.click(screen.getByRole('radio', { name: 'Dark theme' }));
    expect(localStorage.getItem(THEME_KEY)).toBe('dark');
    expect(html.classList.contains('dark')).toBe(true);

    await user.click(screen.getByRole('radio', { name: 'Light theme' }));
    expect(localStorage.getItem(THEME_KEY)).toBe('light');
    expect(html.classList.contains('dark')).toBe(false);

    setPrefersDark(true);
    await user.click(screen.getByRole('radio', { name: 'System theme' }));
    expect(localStorage.getItem(THEME_KEY)).toBeNull();
    expect(html.classList.contains('dark')).toBe(true);
    expect(html.dataset.theme).toBe('system');

    setPrefersDark(false);
    await waitFor(() => expect(html.classList.contains('dark')).toBe(false));
  });
});
