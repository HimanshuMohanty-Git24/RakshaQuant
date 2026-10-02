// Ctrl/⌘+K (§6.4): go to a screen, open a symbol or a decision by id, halt, toggle shortcuts.

import { Command } from "cmdk";
import { useState } from "react";
import { useNavigate } from "react-router";

import { SCREENS } from "../app/routes";
import { useSettings } from "../lib/settings";
import { useUi } from "../lib/ui";

const ID = /^[A-Za-z0-9_-]{8,64}$/;
const SYMBOL = /^[A-Z0-9&_.-]{1,20}$/;

const item =
  "flex h-7 cursor-pointer items-center justify-between rounded-sm px-2 text-sm text-fg-1 data-[selected=true]:bg-bg-2 data-[selected=true]:text-fg-0";
const group = "px-1 py-1 text-2xs text-fg-2 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1";

export function CommandPalette() {
  const { palette, setPalette, setHalt } = useUi();
  const { shortcuts, toggleShortcuts } = useSettings();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const text = query.trim();

  const run = (action: () => void) => {
    setPalette(false);
    setQuery("");
    action();
  };

  return (
    <Command.Dialog
      open={palette}
      onOpenChange={(open) => {
        setPalette(open);
        if (!open) setQuery("");
      }}
      label="Command palette"
      overlayClassName="fixed inset-0 bg-overlay"
      contentClassName="fixed left-1/2 top-[15%] w-[560px] max-w-[92vw] -translate-x-1/2 rounded border border-line-strong bg-bg-1 shadow-popover"
    >
      <Command.Input
        value={query}
        onValueChange={setQuery}
        placeholder="Go to a screen, a symbol (INFY) or a decision id…"
        className="h-9 w-full border-b bg-transparent px-3 text-sm text-fg-0 outline-none placeholder:text-fg-2"
      />
      <Command.List className="max-h-[360px] overflow-auto p-1">
        <Command.Empty className="px-2 py-3 text-xs text-fg-2">No match.</Command.Empty>
        {text && (ID.test(text) || SYMBOL.test(text)) && (
          <Command.Group heading="Open" className={group}>
            {ID.test(text) && (
              <Command.Item value={`decision ${text}`} onSelect={() => run(() => navigate(`/decisions/${text}`))} className={item}>
                Decision <span className="font-mono text-xs">{text}</span>
              </Command.Item>
            )}
            {SYMBOL.test(text.toUpperCase()) && (
              <Command.Item
                value={`symbol ${text}`}
                onSelect={() => run(() => navigate(`/market?symbol=${encodeURIComponent(text.toUpperCase())}`))}
                className={item}
              >
                Symbol <span className="font-mono text-xs">{text.toUpperCase()}</span>
              </Command.Item>
            )}
          </Command.Group>
        )}
        <Command.Group heading="Go to" className={group}>
          {SCREENS.map((s) => (
            <Command.Item key={s.path} value={`go ${s.label}`} onSelect={() => run(() => navigate(s.path))} className={item}>
              {s.label}
              {shortcuts && <kbd className="font-mono text-2xs text-fg-2">{s.key}</kbd>}
            </Command.Item>
          ))}
        </Command.Group>
        <Command.Group heading="Actions" className={group}>
          <Command.Item value="halt all books" onSelect={() => run(() => setHalt(true))} className={item}>
            Halt all books
          </Command.Item>
          <Command.Item value="toggle single-key shortcuts" onSelect={() => run(toggleShortcuts)} className={item}>
            {shortcuts ? "Turn single-key shortcuts off" : "Turn single-key shortcuts on"}
          </Command.Item>
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}
