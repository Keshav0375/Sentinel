import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';
import { installMatchMedia, resetMedia } from './helpers';

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
