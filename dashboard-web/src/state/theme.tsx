// Same key and resolution as the blocking <script id="theme-init"> in index.html, which
// applies the theme before first paint; this provider takes over once React mounts.
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

export const THEME_KEY = 'sentinel-ops-theme';
export type ThemePref = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

const DARK_QUERY = '(prefers-color-scheme: dark)';

export function readThemePref(): ThemePref {
  try {
    const p = window.localStorage.getItem(THEME_KEY);
    return p === 'light' || p === 'dark' ? p : 'system';
  } catch {
    return 'system';
  }
}

function systemPrefersDark(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(DARK_QUERY).matches;
}

export function resolveTheme(pref: ThemePref): ResolvedTheme {
  if (pref === 'system') return systemPrefersDark() ? 'dark' : 'light';
  return pref;
}

export function applyTheme(pref: ThemePref): ResolvedTheme {
  const resolved = resolveTheme(pref);
  const el = document.documentElement;
  el.classList.toggle('dark', resolved === 'dark');
  el.style.colorScheme = resolved;
  el.dataset.theme = pref;
  return resolved;
}

interface ThemeContextValue {
  pref: ThemePref;
  resolved: ResolvedTheme;
  setPref: (pref: ThemePref) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPref] = useState<ThemePref>(readThemePref);
  const [resolved, setResolved] = useState<ResolvedTheme>(() => resolveTheme(pref));

  useEffect(() => {
    setResolved(applyTheme(pref));
    try {
      if (pref === 'system') window.localStorage.removeItem(THEME_KEY);
      else window.localStorage.setItem(THEME_KEY, pref);
    } catch {
      // Storage blocked (private mode): the choice lasts for this page only.
    }
    if (pref !== 'system' || typeof window.matchMedia !== 'function') return;
    const mql = window.matchMedia(DARK_QUERY);
    const onChange = () => setResolved(applyTheme('system'));
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [pref]);

  const value = useMemo(() => ({ pref, resolved, setPref }), [pref, resolved]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used inside <ThemeProvider>');
  return ctx;
}
