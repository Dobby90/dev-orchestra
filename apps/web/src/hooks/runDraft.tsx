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
  commitPrefix: string;
  instructions: string;
  budget: string;
  testScope: "full" | "affected";
  autoApprovePlan: boolean;
  agentSettings: Schema["AgentSettings"] | null;
  designImages: Schema["DesignImageUpload"][];
};
const empty: Draft = {
  project: "",
  name: "",
  text: "",
  mode: "text",
  base: "",
  branch: "",
  commitPrefix: "",
  instructions: "",
  budget: "",
  testScope: "affected",
  autoApprovePlan: false,
  agentSettings: null,
  designImages: [],
};
const Context = createContext<{
  draft: Draft;
  setDraft: Dispatch<SetStateAction<Draft>>;
  clear: () => void;
} | null>(null);
export function RunDraftProvider({ children }: { children: ReactNode }) {
  const [draft, setDraft] = useState(empty);
  useEffect(() => {
    if (
      !draft.name &&
      !draft.text &&
      !draft.instructions &&
      !draft.commitPrefix &&
      !draft.designImages.length
    )
      return;
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
