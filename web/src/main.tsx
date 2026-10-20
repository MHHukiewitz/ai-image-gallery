import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Missing #root element in index.html");
}

createRoot(container).render(
  <StrictMode>
    <h1>AI Image Gallery</h1>
  </StrictMode>,
);
