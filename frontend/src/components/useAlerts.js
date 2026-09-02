import { useEffect, useRef, useState } from 'react';
import { fetchRecentAlerts, openAlertSocket } from '../api/client.js';

/**
 * Subscribes to the alert stream, with a polling fallback when the socket will
 * not open, so a blocked WebSocket degrades to stale-but-present rather than to
 * nothing at all.
 */
export default function useAlerts(limit = 60, enabled = true) {
  const [alerts, setAlerts] = useState([]);
  const [status, setStatus] = useState('connecting');
  const socketRef = useRef(null);
  const seenRef = useRef(new Set());

  useEffect(() => {
    // Alert messages name case numbers, so the feed is only fetched for a
    // signed-in officer. Polling while signed out would just 401 in a loop.
    if (!enabled) {
      setStatus('unauthenticated');
      return undefined;
    }

    // De-duplication happens OUTSIDE the state updater. A setState updater must
    // be a pure function of the previous state: React StrictMode invokes it
    // twice to surface impurity, and mutating a Set inside it means the second
    // invocation sees the key already present and discards the alert.
    const add = (a) => {
      const key = a.stream_msg_id || a.id;
      if (key) {
        if (seenRef.current.has(key)) return;
        seenRef.current.add(key);
      }
      setAlerts((prev) => [a, ...prev].slice(0, limit));
    };

    socketRef.current = openAlertSocket({ onAlert: add, onStatus: setStatus });

    const poll = setInterval(async () => {
      if (socketRef.current && socketRef.current.readyState === WebSocket.OPEN) return;
      try {
        const data = await fetchRecentAlerts(limit);
        (data.alerts || []).forEach(add);
      } catch {
        /* leave the feed as-is */
      }
    }, 15000);

    return () => {
      clearInterval(poll);
      socketRef.current?.close();
    };
  }, [limit, enabled]);

  return { alerts, status };
}
