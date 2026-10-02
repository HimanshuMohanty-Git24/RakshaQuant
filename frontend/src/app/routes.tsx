import { lazy } from "react";

// Every screen is its own chunk: the initial route stays inside the 250 KB budget (§6.7).
const CommandCenter = lazy(() => import("../screens/CommandCenter"));
const DecisionInspector = lazy(() => import("../screens/DecisionInspector"));
const Decisions = lazy(() => import("../screens/Decisions"));
const Blotter = lazy(() => import("../screens/Blotter"));
const RiskCenter = lazy(() => import("../screens/RiskCenter"));
const AIDesk = lazy(() => import("../screens/AIDesk"));
const Experiment = lazy(() => import("../screens/Experiment"));
const Market = lazy(() => import("../screens/Market"));
const System = lazy(() => import("../screens/System"));

export interface Screen {
  path: string;
  label: string;
  key: string; // single-key shortcut (off by default, §6.1)
  element: JSX.Element;
}

export const SCREENS: Screen[] = [
  { path: "/", label: "Command", key: "1", element: <CommandCenter /> },
  { path: "/decisions", label: "Decisions", key: "2", element: <Decisions /> },
  { path: "/blotter", label: "Blotter", key: "3", element: <Blotter /> },
  { path: "/risk", label: "Risk", key: "4", element: <RiskCenter /> },
  { path: "/ai", label: "AI", key: "5", element: <AIDesk /> },
  { path: "/experiment", label: "Experiment", key: "6", element: <Experiment /> },
  { path: "/market", label: "Market", key: "7", element: <Market /> },
  { path: "/system", label: "System", key: "8", element: <System /> },
];

export const INSPECTOR = { path: "/decisions/:id", element: <DecisionInspector /> };
