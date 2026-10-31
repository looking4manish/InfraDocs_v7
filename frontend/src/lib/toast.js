import { createContext, useContext } from "react";

export const ToastCtx = createContext(() => {});

export function useToast() {
  return useContext(ToastCtx);
}
