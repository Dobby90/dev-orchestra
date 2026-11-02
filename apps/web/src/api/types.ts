import type { components } from "./generated";
export type Schema = components["schemas"];
export type Run = Schema["RunSummary"];
export type RunDetail = Schema["RunDetail"];
export type State = Schema["State"];
export type Project = Schema["ProjectView"];
export type Policy = Schema["Policy"];
export type Event = Schema["EventEnvelope"];
export type Step = Schema["StepView"];
