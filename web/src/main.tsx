import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import "./index.css";
import { initObservability } from "./lib/observability";

initObservability();

const container = document.getElementById("root");
if (!container) {
  throw new Error("Missing #root element in index.html");
}

/**
 * Mocks must be up before the first request, so rendering waits for the worker. A browser that
 * can't register a service worker shouldn't leave a blank page, so failure is logged and the
 * app renders against the real `/api`.
 */
async function enableMocking(): Promise<void> {
  if (import.meta.env.VITE_USE_MOCKS !== "true") return;
  try {
    const { startMocking } = await import("./mocks/browser");
    await startMocking();
  } catch (error) {
    console.error("The API mocks failed to start, so /api requests go to the real backend.", error);
  }
}

void enableMocking().then(() => {
  createRoot(container).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
