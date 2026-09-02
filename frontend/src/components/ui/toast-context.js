import { createContext, useContext } from 'react';

/** Kept out of Toaster.jsx so that file exports components only - React Fast
 *  Refresh cannot preserve state for a module that also exports non-components. */
export const ToastContext = createContext(() => {});

export function useToast() {
  return useContext(ToastContext);
}
