/// <reference types="vitest/config" />
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

/**
 * Preload the body font so it downloads with the JS instead of after first render. The file name
 * is content-hashed, so the tag is written from the bundle (build only; dev needs no preload).
 */
function preloadFont(file: RegExp): Plugin {
  let base = '/';
  return {
    name: 'sentinel-preload-font',
    apply: 'build',
    configResolved(config) {
      base = config.base;
    },
    transformIndexHtml: {
      order: 'post',
      handler(_html, ctx) {
        const asset = Object.keys(ctx.bundle ?? {}).find((name) => file.test(name));
        if (!asset) throw new Error(`font preload: no bundle asset matches ${file}`);
        return [
          {
            tag: 'link',
            attrs: { rel: 'preload', as: 'font', type: 'font/woff2', crossorigin: true, href: `${base}${asset}` },
            injectTo: 'head',
          },
        ];
      },
    },
  };
}

export default defineConfig({
  base: '/Sentinel/',
  plugins: [react(), tailwindcss(), preloadFont(/^assets\/inter-latin-wght-normal-[\w-]+\.woff2$/)],
  build: { target: 'es2022', sourcemap: false },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    // The UI renders times in the viewer's zone; pin it so assertions are deterministic.
    env: { TZ: 'UTC' },
  },
});
