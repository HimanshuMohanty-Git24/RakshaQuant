import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { bootstrapToken } from "./lib/auth";
import "./index.css";

bootstrapToken();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
