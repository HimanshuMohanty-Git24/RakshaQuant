import { Suspense } from "react";
import { NavLink, Outlet } from "react-router";

import { SCREENS } from "./routes";

// The application frame. M10.3 replaces this with the full shell (top bar, status bar,
// command palette, toasts).
export default function App() {
  return (
    <div className="flex h-full flex-col bg-bg-0 text-fg-0">
      <nav aria-label="Screens" className="flex gap-3 border-b px-3 py-1 text-xs text-fg-1">
        {SCREENS.map((s) => (
          <NavLink
            key={s.path}
            to={s.path}
            end={s.path === "/"}
            className={({ isActive }) => (isActive ? "text-fg-0" : "hover:text-fg-0")}
          >
            {s.label}
          </NavLink>
        ))}
      </nav>
      <main className="min-h-0 flex-1 overflow-auto">
        <Suspense fallback={null}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
