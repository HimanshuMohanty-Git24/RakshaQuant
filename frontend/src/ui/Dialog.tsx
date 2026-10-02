// Radix dialogs with a focus trap (§6.4), and ConfirmTyped: a destructive control that only
// arms once the operator has typed the exact phrase (RESUME, FLATTEN) and given a reason.

import * as RadixDialog from "@radix-ui/react-dialog";
import { useId, useState, type ReactNode } from "react";

import { ToolButton } from "./Panel";

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 bg-overlay" />
        <RadixDialog.Content className="fixed left-1/2 top-[20%] w-[440px] max-w-[92vw] -translate-x-1/2 rounded border border-line-strong bg-bg-1 p-4 shadow-popover">
          <RadixDialog.Title className="text-sm font-medium text-fg-0">{title}</RadixDialog.Title>
          <RadixDialog.Description className="mt-1 text-xs text-fg-1">
            {description ?? " "}
          </RadixDialog.Description>
          <div className="mt-3">{children}</div>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

const field = "h-7 w-full rounded-sm border border-line-strong bg-bg-0 px-2 text-sm text-fg-0";

export function ConfirmTyped({
  open,
  onOpenChange,
  title,
  description,
  phrase,
  confirmLabel,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  phrase: "RESUME" | "FLATTEN";
  confirmLabel: string;
  /** Resolves on success; a rejection's message is shown in the dialog. */
  onConfirm: (reason: string) => Promise<unknown>;
}) {
  const [typed, setTyped] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const phraseId = useId();
  const reasonId = useId();
  const armed = typed === phrase && reason.trim().length >= 3 && !busy;

  const close = (next: boolean) => {
    if (!next) {
      setTyped("");
      setReason("");
      setError(null);
    }
    onOpenChange(next);
  };

  const submit = async () => {
    if (!armed) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(reason.trim());
      close(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "the request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={close} title={title} description={description}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        className="flex flex-col gap-2"
      >
        <label htmlFor={reasonId} className="text-2xs text-fg-2">
          Reason (recorded with the action)
        </label>
        <input id={reasonId} className={field} value={reason} onChange={(e) => setReason(e.target.value)} />
        <label htmlFor={phraseId} className="text-2xs text-fg-2">
          Type <span className="font-mono text-fg-0">{phrase}</span> to confirm
        </label>
        <input
          id={phraseId}
          className={`${field} font-mono`}
          value={typed}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setTyped(e.target.value)}
        />
        {error && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
        <div className="mt-2 flex justify-end gap-2">
          <ToolButton onClick={() => close(false)}>Cancel</ToolButton>
          <ToolButton type="submit" tone="crit" disabled={!armed}>
            {confirmLabel}
          </ToolButton>
        </div>
      </form>
    </Dialog>
  );
}
