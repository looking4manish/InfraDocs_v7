import { useActionRunner } from "../hooks/useActionRunner";
import { ACTION_META } from "../lib/actions";
import { Btn } from "./ui";

export default function ActionButton({ action, fire, label, target, sm = true, iconOnly, primary,
  disabled, disabledReason, invalidateKeys }) {
  const r = useActionRunner(invalidateKeys);
  const meta = ACTION_META[action] || { label: action };
  const text = label || meta.label;
  return (
    <>
      <Btn sm={sm} iconOnly={iconOnly} label={text} icon={meta.icon} primary={primary}
        danger={action === "stop" || action === "down"} busy={r.busy}
        disabled={disabled || r.busy} title={disabled ? disabledReason : undefined}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); r.run({ action, fire, target, label: text }); }}>
        {text}
      </Btn>
      {r.ui}
    </>
  );
}
