import { useCallback, useState } from "react";
import { createPortal } from "react-dom";
import { CircleCheck, CircleX } from "lucide-react";

import { ToastCtx as Ctx } from "../lib/toast";

export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const push = useCallback((text, tone = "ok") => {
    const id = Math.random().toString(36).slice(2);
    setItems((xs) => [...xs, { id, text, tone }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 4200);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      {createPortal(
        <div className="toasts" role="status" aria-live="polite">
          {items.map((t) => (
            <div key={t.id} className={`toast ${t.tone}`}>
              {t.tone === "bad" ? <CircleX size={16} /> : <CircleCheck size={16} />}
              {t.text}
            </div>
          ))}
        </div>,
        document.body
      )}
    </Ctx.Provider>
  );
}
