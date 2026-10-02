import { useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { post } from "../api/client";
import { Dialog } from "../ui/Dialog";
import { ToolButton } from "../ui/Panel";
import { useToasts } from "../ui/Toast";

/** HALT stops new entries at once; it is always allowed (even read-only) and needs no typed
 *  phrase - only a reason, which is recorded with the action. */
export function HaltDialog({
  open,
  onOpenChange,
  book,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  book?: string;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const id = useId();
  const push = useToasts((s) => s.push);
  const client = useQueryClient();

  const close = (next: boolean) => {
    if (!next) {
      setReason("");
      setError(null);
    }
    onOpenChange(next);
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await post("/api/risk/halt", { reason: reason.trim(), book: book ?? null });
      push({ level: "warn", title: `HALT ${result.outcome.replace("_", " ")}`, body: result.detail });
      void client.invalidateQueries();
      close(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "the request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={book ? `Halt book ${book}` : "Halt all books"}
      description="Blocks every new entry now. Exits and stops keep working. Resuming needs the typed phrase."
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (reason.trim().length >= 3 && !busy) void submit();
        }}
        className="flex flex-col gap-2"
      >
        <label htmlFor={id} className="text-2xs text-fg-2">
          Reason (recorded with the action)
        </label>
        <input
          id={id}
          autoFocus
          className="h-7 w-full rounded-sm border border-line-strong bg-bg-0 px-2 text-sm text-fg-0"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        {error && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
        <div className="mt-2 flex justify-end gap-2">
          <ToolButton onClick={() => close(false)}>Cancel</ToolButton>
          <ToolButton type="submit" tone="crit" disabled={reason.trim().length < 3 || busy}>
            Halt
          </ToolButton>
        </div>
      </form>
    </Dialog>
  );
}
