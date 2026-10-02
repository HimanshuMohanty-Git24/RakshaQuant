import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";

import App from "./app/App";
import { INSPECTOR, SCREENS } from "./app/routes";
import "./index.css";
import { bootstrapToken } from "./lib/auth";

bootstrapToken(); // read #token= from the launch URL, then strip it from the address bar

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, retry: 1, refetchOnWindowFocus: false },
  },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      ...SCREENS.map((s) => (s.path === "/" ? { index: true, element: s.element } : s)),
      INSPECTOR,
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
