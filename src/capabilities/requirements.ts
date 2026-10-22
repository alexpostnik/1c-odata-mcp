import inventory from "./requirements.json" with { type: "json" };
import { createHash } from "node:crypto";
import type { ConnectionConfig } from "../config/env.js";

export interface Requirement {
  target: string[];
  kind?: string;
  fields: string[] | Record<string, string>;
  keys?: string[];
  forbiddenFields?: string[];
  suffixAlternatives?: string[];
  configurationOverride?: string;
  function?: { name: string; parameters: string[] };
  notes?: string;
  [key: string]: unknown;
}
export interface Dependency {
  requirement?: string;
  target?: string[];
  argument?: string;
  fields?: string[];
  keys?: string[];
  operations?: string[];
  candidateGroups?: string[][];
  restrictedCandidates?: string[];
  defaultCandidates?: string[];
  when?: string;
  [key: string]: unknown;
}
export interface ToolRequirement {
  name: string;
  required: string[];
  conditional: Dependency[];
  optional: Array<string | Dependency>;
  dynamic: Dependency[];
  operations: string[];
  localOnly?: boolean;
  mutationFieldModel?: {
    alwaysSent: string[];
    conditionalSent?: Record<string, string>;
    rows?: Record<string, { alwaysSent: string[]; conditionalSent?: Record<string, string> }>;
  };
  [key: string]: unknown;
}
export const REQUIREMENTS = inventory as unknown as {
  schemaVersion: number;
  revision: string;
  sourceVersion: string;
  sourceRef: string;
  definitions: Record<string, Requirement>;
  tools: ToolRequirement[];
  rules: string[];
};

export const fingerprint = (value: unknown): string =>
  createHash("sha256").update(JSON.stringify(value)).digest("hex");

/** Учитывает те же настройки плана счетов, что используют сами инструменты. */
export function targets(req: Requirement, cfg: ConnectionConfig): string[] {
  const base =
    req.configurationOverride === "accounting.chart"
      ? [cfg.accounting?.chart ?? req.target[0]!]
      : req.configurationOverride === "accounting.register"
        ? [cfg.accounting?.register ?? req.target[0]!]
        : req.target;
  return req.suffixAlternatives ? base.flatMap((b) => req.suffixAlternatives!.map((s) => b + s)) : base;
}

/** Новая регистрация обязана попасть в versioned inventory, даже если ей не нужен OData. */
export function assertToolRequirement(name: string): void {
  if (!REQUIREMENTS.tools.some((t) => t.name === name))
    throw new Error(`Для ${name} не описаны требования OData в capabilities/requirements.json`);
}
