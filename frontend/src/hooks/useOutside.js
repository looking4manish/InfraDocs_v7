import { useEffect, useRef } from "react";

// Calls onOut when a mousedown lands outside the returned ref's element.
export function useOutside(onOut) {
  const ref = useRef(null);
  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) onOut(); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [onOut]);
  return ref;
}
