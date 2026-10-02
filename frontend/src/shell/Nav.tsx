import { NavLink } from "react-router";

import { SCREENS } from "../app/routes";
import { cx } from "../lib/cx";
import { useSettings } from "../lib/settings";
import { Icon } from "./icons";

/** 56 px of icons, or 184 px with labels (§6.5). */
export function Nav() {
  const { navExpanded, toggleNav, shortcuts } = useSettings();
  return (
    <nav
      aria-label="Screens"
      className={cx("flex shrink-0 flex-col border-r bg-bg-1 py-1", navExpanded ? "w-[184px]" : "w-14")}
    >
      {SCREENS.map((s) => (
        <NavLink
          key={s.path}
          to={s.path}
          end={s.path === "/"}
          title={navExpanded ? undefined : s.label}
          aria-label={navExpanded ? undefined : s.label}
          className={({ isActive }) =>
            cx(
              "mx-1 flex h-8 items-center gap-2 rounded-sm px-3 text-sm",
              isActive ? "bg-bg-2 text-fg-0" : "text-fg-1 hover:bg-bg-2 hover:text-fg-0",
              !navExpanded && "justify-center px-0",
            )
          }
        >
          <Icon name={s.icon} />
          {navExpanded && <span className="flex-1">{s.label}</span>}
          {navExpanded && shortcuts && <kbd className="font-mono text-2xs text-fg-2">{s.key}</kbd>}
        </NavLink>
      ))}
      <button
        type="button"
        onClick={toggleNav}
        className="mx-1 mt-auto h-7 rounded-sm text-2xs text-fg-2 hover:bg-bg-2 hover:text-fg-0"
        aria-label={navExpanded ? "Collapse navigation" : "Expand navigation"}
      >
        {navExpanded ? "« collapse" : "»"}
      </button>
    </nav>
  );
}
