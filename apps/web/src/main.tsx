import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles/app.css";
try {
  document.documentElement.classList.toggle(
    "dark",
    localStorage.getItem("devorchestra-theme") !== "light",
  );
} catch {
  /* Dark is the default. */
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
