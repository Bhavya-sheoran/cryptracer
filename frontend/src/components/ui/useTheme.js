import { useCallback, useEffect, useState } from 'react';

const KEY = 'sih183-theme';

/**
 * Light/dark theme, persisted per browser and applied to the document root.
 *
 * Light is the default because findings get screenshotted into briefs and
 * reports, where a dark capture reads badly - the same reason Etherscan and the
 * compliance tools default light. Dark is a real option, not an inversion:
 * tokens.css redefines the palette rather than filtering it.
 */
export default function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      const stored = localStorage.getItem(KEY);
      if (stored === 'light' || stored === 'dark') return stored;
    } catch {
      /* storage unavailable (private mode) - fall through to the OS preference */
    }
    // No explicit choice yet: follow the operating system. An investigator who
    // runs their desktop dark should not be handed a white screen first.
    try {
      if (window.matchMedia?.('(prefers-color-scheme: dark)').matches) return 'dark';
    } catch {
      /* matchMedia unavailable - light is a safe default */
    }
    return 'light';
  });

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      /* not persisting is acceptable; the session still works */
    }
  }, [theme]);

  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), []);
  return { theme, toggle };
}
