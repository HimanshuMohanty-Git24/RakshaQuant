import * as RadixTabs from "@radix-ui/react-tabs";
import type { ReactNode } from "react";

export interface TabSpec {
  value: string;
  label: ReactNode;
  content: ReactNode;
}

/** Radix tabs (§6.4): keyboard arrows move between tabs; content mounts on demand. */
export function Tabs({
  tabs,
  value,
  onValueChange,
  label,
}: {
  tabs: TabSpec[];
  value?: string;
  onValueChange?: (value: string) => void;
  label: string;
}) {
  return (
    <RadixTabs.Root
      value={value}
      defaultValue={value === undefined ? tabs[0]?.value : undefined}
      onValueChange={onValueChange}
      className="flex min-h-0 flex-1 flex-col"
    >
      <RadixTabs.List aria-label={label} className="flex h-7 shrink-0 items-end gap-3 border-b px-2">
        {tabs.map((t) => (
          <RadixTabs.Trigger
            key={t.value}
            value={t.value}
            className="h-7 border-b-2 border-transparent text-xs text-fg-2 hover:text-fg-0 data-[state=active]:border-accent data-[state=active]:text-fg-0"
          >
            {t.label}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {tabs.map((t) => (
        <RadixTabs.Content key={t.value} value={t.value} className="min-h-0 flex-1 outline-none">
          {t.content}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  );
}
