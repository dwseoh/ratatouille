import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import "./cook.css";
import Cook from "./screens/Cook.tsx";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Cook />
  </StrictMode>,
);
