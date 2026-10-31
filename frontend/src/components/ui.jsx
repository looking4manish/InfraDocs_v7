// Shared primitives that wear the V7 theme classes (theme.css).
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { X, Loader2 } from "lucide-react";

export function Page({ children, className = "" }) {
  return <div className={`page ${className}`}>{children}</div>;
}

export function Head({ eyebrow, title, sub, children, right }) {
  return (
    <div className="head">
      <div>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {right}
      {children && <div className="act">{children}</div>}
    </div>
  );
}

export function Card({ className = "", children, ...rest }) {
  return <div className={`card ${className}`} {...rest}>{children}</div>;
}

export function CardHead({ title, meta, children, flush }) {
  return (
    <div className="card-h" style={flush ? { padding: "0 0 10px" } : undefined}>
      <h2>{title}</h2>
      {meta != null && <span className="meta">{meta}</span>}
      {children}
    </div>
  );
}

export function Chip({ tone = "idle", dot = true, children, title }) {
  return (
    <span className={`chip ${tone}`} title={title}>
      {dot && <span className="d" />}
      {children}
    </span>
  );
}

export function Tag({ children, sans, title }) {
  return <span className={`tag${sans ? " sans" : ""}`} title={title}>{children}</span>;
}

export function Orb({ sev = "ok" }) {
  return <span className={`orb ${sev}`} />;
}

export function Btn({ icon: Icon, children, primary, sm, danger, ghost, iconOnly, busy, className = "", label, ...rest }) {
  const cls = ["btn", primary && "primary", sm && "sm", danger && "danger", ghost && "ghost", iconOnly && "icon", className]
    .filter(Boolean).join(" ");
  const sz = sm || iconOnly ? 14 : 15;
  return (
    <button type="button" className={cls} aria-label={iconOnly ? label : undefined} title={iconOnly ? label : undefined} {...rest}>
      {busy ? <Loader2 size={sz} className="spin" /> : Icon && <Icon size={sz} />}
      {!iconOnly && children}
    </button>
  );
}

export function Seg({ options, value, onChange }) {
  return (
    <div className="seg" role="tablist">
      {options.map((o) => (
        <button key={o.value} type="button" role="tab" aria-selected={o.value === value}
          className={o.value === value ? "on" : ""} onClick={() => onChange(o.value)}>
          {o.label}
          {o.count != null && <span className="c">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function SearchBox({ value, onChange, placeholder, icon: Icon }) {
  return (
    <label className="search">
      {Icon && <Icon size={15} />}
      <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} aria-label={placeholder} />
    </label>
  );
}

export function Empty({ icon: Icon, title, children, action }) {
  return (
    <div className="empty">
      {Icon && <span className="ei"><Icon size={22} /></span>}
      <div className="t">{title}</div>
      {children && <div className="d">{children}</div>}
      {action}
    </div>
  );
}

export function Skel({ h = 16, w = "100%", style }) {
  return <div className="skel" style={{ height: h, width: w, ...style }} />;
}

export function SkelCard({ h = 160 }) {
  return <div className="card" style={{ padding: 20 }}><Skel h={h - 40} /></div>;
}

export function Modal({ title, icon: Icon, onClose, children, footer, wide }) {
  useEffect(() => {
    const k = (e) => { if (e.key === "Escape") onClose?.(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return createPortal(
    <div className="modal-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose?.(); }}>
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="mh">
          {Icon && <span className="app-icon" style={{ width: 38, height: 38, borderRadius: 11 }}><Icon size={18} /></span>}
          <h2>{title}</h2>
          <Btn className="x" iconOnly icon={X} label="Close" onClick={onClose} />
        </div>
        <div className="mb">{children}</div>
        {footer && <div className="mf">{footer}</div>}
      </div>
    </div>,
    document.body
  );
}

// Typed confirmation: the user types the item's id before an irreversible action.
export function ConfirmModal({ title, icon, children, confirmWord, actionLabel, danger, busy, error, onConfirm, onClose }) {
  const [typed, setTyped] = useState("");
  const ok = !confirmWord || typed === confirmWord;
  return (
    <Modal title={title} icon={icon} onClose={onClose}
      footer={<>
        <Btn onClick={onClose}>Cancel</Btn>
        <Btn primary={!danger} danger={danger} busy={busy} disabled={!ok || busy} onClick={onConfirm}>{actionLabel}</Btn>
      </>}>
      {children}
      {confirmWord && (
        <div className="field">
          <label htmlFor="confirm-word">Type <span className="mono" style={{ color: "var(--fg)" }}>{confirmWord}</span> to confirm</label>
          <input id="confirm-word" className="input" value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus autoComplete="off" />
        </div>
      )}
      {error && <div className="formerr">{error}</div>}
    </Modal>
  );
}
