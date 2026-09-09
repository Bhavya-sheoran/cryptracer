import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * Origin handling, which changed when Caddy put the whole stack behind one
 * https origin.
 *
 * Both constants are evaluated at module load, so each test resets the module
 * registry and re-imports rather than trying to mutate them after the fact.
 */
async function loadClient({ protocol = 'http:', host = 'localhost:5174', env = {} } = {}) {
  vi.resetModules();
  vi.stubGlobal('window', { location: { protocol, host } });
  vi.stubEnv('VITE_API_BASE', env.VITE_API_BASE ?? '');
  vi.stubEnv('VITE_WS_URL', env.VITE_WS_URL ?? '');
  return import('./client.js');
}

describe('API origin', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: async () => ({}) })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('calls the API on a relative path by default', async () => {
    const client = await loadClient();
    await client.fetchReadiness().catch(() => {});

    const [url] = globalThis.fetch.mock.calls[0];
    expect(url).toMatch(/^\/api\/v1\//);
    expect(url).not.toMatch(/^https?:\/\//);
  });

  it('never hardcodes http, which would be blocked as mixed content under https', async () => {
    const client = await loadClient({ protocol: 'https:', host: 'localhost:8443' });
    await client.fetchReadiness().catch(() => {});

    const [url] = globalThis.fetch.mock.calls[0];
    expect(url).not.toMatch(/^http:/);
  });

  it('honours an explicit base for a backend on another host', async () => {
    const client = await loadClient({ env: { VITE_API_BASE: 'https://api.example.gov.in' } });
    await client.fetchReadiness().catch(() => {});

    const [url] = globalThis.fetch.mock.calls[0];
    expect(url).toMatch(/^https:\/\/api\.example\.gov\.in\/api\/v1\//);
  });
});

describe('alert socket URL', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('uses ws:// when the page is plain http', async () => {
    const client = await loadClient({ protocol: 'http:', host: 'localhost:5174' });
    expect(client.WS_URL).toBe('ws://localhost:5174/api/v1/ws/alerts');
  });

  it('uses wss:// when the page is https', async () => {
    // A ws:// socket on an https page is refused by the browser, so this has
    // to follow the page rather than be configured.
    const client = await loadClient({ protocol: 'https:', host: 'localhost:8443' });
    expect(client.WS_URL).toBe('wss://localhost:8443/api/v1/ws/alerts');
  });

  it('follows the host it is served from, not a hardcoded one', async () => {
    const client = await loadClient({ protocol: 'https:', host: 'chaintrace.example.gov.in' });
    expect(client.WS_URL).toContain('chaintrace.example.gov.in');
  });

  it('honours an explicit override', async () => {
    const client = await loadClient({ env: { VITE_WS_URL: 'wss://elsewhere/api/v1/ws/alerts' } });
    expect(client.WS_URL).toBe('wss://elsewhere/api/v1/ws/alerts');
  });
});
