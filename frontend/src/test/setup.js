import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

// React Testing Library does not unmount between tests on its own when
// globals are enabled this way. Without this, a component from one test is
// still in the document during the next, and queries match the wrong element.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// jsdom implements neither of these, and components that use them would throw
// rather than fail an assertion - which reads as a broken test rather than a
// missing browser API.
globalThis.ResizeObserver ||= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

globalThis.matchMedia ||= (query) => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {
    return false;
  },
});
