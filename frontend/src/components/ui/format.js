/** Presentation helpers shared across components. Non-component exports live
 *  here so component modules stay Fast-Refresh friendly. */

/** Risk label -> badge tone. One mapping used everywhere, so "high" is never
 *  amber in one table and red in another. */
export const RISK_TONE = { low: 'ok', medium: 'warn', high: 'danger' };

/** Head/tail truncation for hashes and addresses, keeping both ends legible -
 *  the ends are what an investigator actually compares. */
export function truncateMiddle(value, head = 10, tail = 6) {
  if (!value) return '';
  if (value.length <= head + tail + 1) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function relativeTime(iso) {
  if (!iso) return null;
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return null;
  const secs = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}
