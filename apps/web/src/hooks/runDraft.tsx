import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { Schema } from "../api/types";
type Draft = {
  project: string;
  name: string;
  text: string;
  mode: "text" | "file";
  base: string;
  branch: string;
  instructions: string;
  budget: string;
  testScope: "full" | "affected";
  agentSettings: Schema["AgentSettings"] | null;
};
const empty: Draft = {
  project: "",
  name: "",
  text: "",
  mode: "text",
  base: "",
  branch: "",
  instructions: "",
  budget: "",
  testScope: "affected",
  agentSettings: null,
};
const Context = createContext<{
  draft: Draft;
  setDraft: Dispatch<SetStateAction<Draft>>;
  clear: () => void;
} | null>(null);
export function RunDraftProvider({ children }: { children: ReactNode }) {
  const [draft, setDraft] = useState(empty);
  useEffect(() => {
    if (!draft.name && !draft.text && !draft.instructions) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [draft]);
  return (
    <Context.Provider value={{ draft, setDraft, clear: () => setDraft(empty) }}>
      {children}
    </Context.Provider>
  );
}
export function useDraftField<K extends keyof Draft>(
  key: K,
  initial?: Draft[K],
): [Draft[K], (value: Draft[K]) => void] {
  const context = useContext(Context);
  const [fallback, setFallback] = useState(initial ?? empty[key]);
  return context
    ? [
        context.draft[key],
        (value) =>
          context.setDraft((previous) => ({ ...previous, [key]: value })),
      ]
    : [fallback, setFallback];
}
export function useClearDraft() {
  return useContext(Context)?.clear ?? (() => {});
}
