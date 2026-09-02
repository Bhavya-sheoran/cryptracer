const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8001';
const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8001/api/v1/ws/alerts';

/** Bearer token for the signed-in officer. Kept in module scope, not localStorage:
 *  a demo token in localStorage survives the tab and is readable by any script
 *  that gets injected. Sign-in is cheap; persistence is not worth that. */
let authToken = null;

export function setAuthToken(token) {
  authToken = token;
}

export function getAuthToken() {
  return authToken;
}

function authHeaders(extra) {
  const headers = { ...(extra || {}) };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  return headers;
}

/**
 * Thin fetch wrapper. Throws on non-2xx so callers handle one failure path.
 * FastAPI puts its message in `detail`, so surface that rather than a bare code -
 * "EIP-55 checksum mismatch" is far more useful to an investigator than "422".
 */
async function request(path, options) {
  const opts = { ...(options || {}) };
  opts.headers = authHeaders(opts.headers);
  const res = await fetch(`${API_BASE}${path}`, opts);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      // non-JSON error body; keep the status line
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export function apiGet(path) {
  return request(path, undefined);
}

export function apiPost(path, body) {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function fetchReadiness() {
  return apiGet('/api/v1/health/ready');
}

/** Chain detection + checksum check. Never creates a case. */
export function validateAddress(address) {
  return apiPost('/api/v1/wallets/validate', { address });
}

/** File a complaint: opens a case, traces, builds the graph, clusters. */
export function submitWallet(payload) {
  return apiPost('/api/v1/wallets', payload);
}

/** Trace + attribution + risk in one call. */
export function analyseWallet(address, depth) {
  const params = new URLSearchParams({ address });
  if (depth) params.set('depth', String(depth));
  return apiGet(`/api/v1/wallet?${params.toString()}`);
}

export function fetchRankedExchanges(limit = 20) {
  return apiGet(`/api/v1/exchanges/ranked?limit=${limit}`);
}

/** Wallets named in more than one case - the fraud-ring signal. */
export function fetchMultiReported() {
  return apiGet('/api/v1/wallets/multi-reported');
}

export { API_BASE };


// --- authentication --------------------------------------------------------
export async function login(username, password) {
  // OAuth2 password flow: form-encoded, not JSON.
  const body = new URLSearchParams({ username, password });
  const res = await fetch(`${API_BASE}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!res.ok) {
    let detail = 'Sign-in failed';
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* keep the default */
    }
    throw new Error(detail);
  }
  const data = await res.json();
  setAuthToken(data.access_token);
  return data;
}

export function seedDemoUsers() {
  return apiPost('/api/v1/auth/seed-demo-users', {});
}

// --- cases -----------------------------------------------------------------
export function fetchCases(limit = 25) {
  return apiGet(`/api/v1/cases?limit=${limit}`);
}

export function fetchCase(caseId) {
  return apiGet(`/api/v1/cases/${caseId}`);
}

export function addCaseNote(caseId, body) {
  return apiPost(`/api/v1/cases/${caseId}/notes`, { body });
}

export async function uploadEvidence(caseId, file) {
  const form = new FormData();
  form.append('file', file);
  // No Content-Type header: the browser must set the multipart boundary itself.
  const res = await fetch(`${API_BASE}/api/v1/cases/${caseId}/evidence`, {
    method: 'POST',
    headers: authHeaders(),
    body: form,
  });
  if (!res.ok) {
    let detail = `Upload failed (${res.status})`;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* keep the default */
    }
    throw new Error(detail);
  }
  return res.json();
}

export function generateReport(caseId) {
  return apiPost(`/api/v1/cases/${caseId}/report`, {});
}

export function reportDownloadUrl(caseId, reportId) {
  return `${API_BASE}/api/v1/cases/${caseId}/report/${reportId}/download`;
}

export function verifyReport(caseId, reportId) {
  return apiGet(`/api/v1/cases/${caseId}/report/${reportId}/verify`);
}

// --- freeze requests -------------------------------------------------------
export function fetchFreezeRequests(caseId) {
  const q = caseId ? `?case_id=${caseId}` : '';
  return apiGet(`/api/v1/freeze-requests${q}`);
}

export function createFreezeRequest(payload) {
  return apiPost('/api/v1/freeze-requests', payload);
}

export function submitFreezeRequest(id) {
  return apiPost(`/api/v1/freeze-requests/${id}/submit`, {});
}

export function approveFreezeRequest(id) {
  return apiPost(`/api/v1/freeze-requests/${id}/approve`, {});
}

export function rejectFreezeRequest(id, reason) {
  return apiPost(`/api/v1/freeze-requests/${id}/reject`, { reason });
}

// --- STR drafts ------------------------------------------------------------
export function createStrDraft(caseId) {
  return apiPost('/api/v1/str-drafts', { case_id: caseId });
}

export function fetchStrDrafts(caseId) {
  const q = caseId ? `?case_id=${caseId}` : '';
  return apiGet(`/api/v1/str-drafts${q}`);
}

export function approveStrDraft(id) {
  return apiPost(`/api/v1/str-drafts/${id}/approve`, {});
}

// --- alerts ----------------------------------------------------------------
export function fetchRecentAlerts(limit = 25) {
  return apiGet(`/api/v1/alerts/recent?limit=${limit}`);
}

/** Open the alert WebSocket. Returns the socket so the caller can close it.
 *
 *  The token travels as a query parameter because a browser WebSocket cannot
 *  set an Authorization header. The server verifies it exactly as it verifies
 *  the HTTP one and closes the socket before accepting if it fails.
 */
export function openAlertSocket({ onAlert, onHello, onStatus }) {
  if (!authToken) {
    onStatus?.('unauthenticated');
    return null;
  }
  let socket;
  try {
    socket = new WebSocket(`${WS_URL}?token=${encodeURIComponent(authToken)}`);
  } catch {
    onStatus?.('error');
    return null;
  }
  socket.onopen = () => onStatus?.('open');
  socket.onclose = () => onStatus?.('closed');
  socket.onerror = () => onStatus?.('error');
  socket.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.type === 'alert') onAlert?.(msg);
    else if (msg.type === 'hello') onHello?.(msg);
  };
  return socket;
}

export { WS_URL };
