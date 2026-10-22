import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { z } from "zod";

const name = z.string().regex(/^[\p{L}\p{N}_.: -]{1,220}$/u);
const stamp = z.iso.datetime();
export const schemaStatus = z.enum([
  "published",
  "absent_from_accessible_metadata",
  "schema_incompatible",
  "not_implemented",
  "unknown",
]);
const schemaObservation = z.object({
  status: schemaStatus,
  candidates: z.array(name),
  entitySet: name.optional(),
  missing: z.array(name),
  unknown: z.array(name),
  factConflict: z.boolean().optional(),
});
const operationObservation = z.object({
  entitySet: name,
  functionName: name.optional(),
  operation: z.enum(["read", "create", "update", "post", "unpost", "delete"]),
  status: z.enum(["observed_success", "observed_denied", "unknown"]),
  reason: z.enum([
    "request_succeeded",
    "explicit_1c_permission_denial",
    "authentication_or_service",
    "record_not_found",
    "not_found_ambiguous",
    "service_or_transport",
    "query_or_schema",
    "unknown",
  ]),
  at: stamp,
  lastSuccessAt: stamp.optional(),
  scope: z.enum(["collection_query", "one_record", "one_operation"]),
  expiresAt: stamp,
});
export type OperationObservation = z.infer<typeof operationObservation>;
export const eventSchema = z.object({
  at: stamp,
  kind: z.enum([
    "newly_required",
    "newly_missing",
    "restored",
    "changed",
    "observation_failed",
    "observation_restored",
  ]),
  requirement: name,
  before: z.string().max(100).optional(),
  after: z.string().max(100),
});
export type CapabilityEvent = z.infer<typeof eventSchema>;
const stateSchema = z.object({
  schemaVersion: z.literal(1),
  scope: z.string().regex(/^[a-f0-9]{64}$/),
  revision: z.string().max(100),
  fingerprints: z.record(name, z.string().regex(/^[a-f0-9]{64}$/)),
  lastAttemptAt: stamp.optional(),
  lastSuccessAt: stamp.optional(),
  metadataStatus: z.enum(["ok", "unknown"]),
  latest: z.record(name, schemaObservation),
  operations: z.array(operationObservation).max(5000),
  events: z.array(eventSchema).max(1000),
});
export type CapabilityState = z.infer<typeof stateSchema>;
export function emptyState(scope: string): CapabilityState {
  return {
    schemaVersion: 1,
    scope,
    revision: "unknown",
    fingerprints: {},
    metadataStatus: "unknown",
    latest: {},
    operations: [],
    events: [],
  };
}
export class CapabilityStore {
  readonly path: string;
  constructor(
    private readonly dir: string,
    readonly scope: string,
  ) {
    this.path = join(dir, `${scope}.json`);
  }
  async load(): Promise<{ state: CapabilityState; status: "new" | "loaded" | "unavailable" }> {
    try {
      const state = stateSchema.parse(JSON.parse(await readFile(this.path, "utf8")));
      if (state.scope !== this.scope) throw new Error("scope");
      return { state, status: "loaded" };
    } catch (e) {
      return {
        state: emptyState(this.scope),
        status: (e as NodeJS.ErrnoException).code === "ENOENT" ? "new" : "unavailable",
      };
    }
  }
  async save(state: CapabilityState): Promise<void> {
    await mkdir(this.dir, { recursive: true, mode: 0o700 });
    const temp = join(this.dir, `.${this.scope}-${randomUUID()}.tmp`);
    await writeFile(temp, JSON.stringify(state), { mode: 0o600, flag: "wx" });
    await rename(temp, this.path);
  }
}
