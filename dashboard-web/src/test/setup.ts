import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { afterEach, vi } from 'vitest';
import { installMatchMedia, resetMedia } from './helpers';

// The shared CI runner is several times slower than a laptop: the drawer's entity fetch plus the
// Radix open transition outlasted the 1 s default in CI. findBy*/waitFor still fail fast on a real
// regression, just not on a busy runner.
configure({ asyncUtilTimeout: 5000 });

installMatchMedia();

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = () => {};
window.scrollTo = (() => {}) as typeof window.scrollTo;

afterEach(() => {
  cleanup();
  localStorage.clear();
  document.documentElement.className = '';
  delete document.documentElement.dataset.theme;
  document.documentElement.style.colorScheme = '';
  window.history.replaceState(null, '', '/Sentinel/');
  resetMedia();
  vi.useRealTimers();
});
