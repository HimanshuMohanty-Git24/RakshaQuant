const SHORTCUTS: [string, string][] = [
  ["j / k", "Move down / up through cycles"],
  ["Enter", "Open the focused span's detail"],
  ["/", "Filter spans in the trace explorer"],
  [":", "Focus the command input"],
  ["Esc", "Close the drawer / help"],
  ["g then t", "Jump to the trace explorer"],
  ["g then f", "Jump to the live feed"],
  ["?", "Toggle this cheatsheet"],
];

const COMMANDS: [string, string][] = [
  [":start", "Start a live trading run"],
  [":demo", "Start a synthetic demo run"],
  [":stop", "Stop the active run"],
  [":help", "Show this help"],
];

export function ShortcutHelp({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-md border bg-panel p-5 shadow-drawer"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-amber">Keyboard & Commands</h3>
          <button
            onClick={onClose}
            className="rounded-sm border px-2 py-1 text-2xs text-muted hover:text-ink"
          >
            Esc ✕
          </button>
        </div>
        <div className="grid grid-cols-2 gap-6">
          <div>
            <div className="mb-2 text-2xs uppercase tracking-wider text-muted">Navigation</div>
            <ul className="space-y-1.5">
              {SHORTCUTS.map(([k, d]) => (
                <li key={k} className="flex items-center justify-between text-xs">
                  <kbd className="rounded-sm border bg-canvas px-1.5 py-0.5 text-2xs text-amber">
                    {k}
                  </kbd>
                  <span className="ml-2 text-right text-muted">{d}</span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <div className="mb-2 text-2xs uppercase tracking-wider text-muted">Commands</div>
            <ul className="space-y-1.5">
              {COMMANDS.map(([k, d]) => (
                <li key={k} className="flex items-center justify-between text-xs">
                  <kbd className="rounded-sm border bg-canvas px-1.5 py-0.5 text-2xs text-cost">
                    {k}
                  </kbd>
                  <span className="ml-2 text-right text-muted">{d}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
