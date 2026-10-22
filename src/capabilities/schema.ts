import type { ConnectionConfig } from "../config/env.js";
import type { EntityMeta, MetadataMap } from "../types/odata.js";
import { DEFAULT_PROFILE } from "./config.js";
import { targets, type Requirement } from "./requirements.js";

export type SchemaStatus =
  "published" | "absent_from_accessible_metadata" | "schema_incompatible" | "not_implemented" | "unknown";
export interface SchemaObservation {
  status: SchemaStatus;
  candidates: string[];
  entitySet?: string;
  missing: string[];
  unknown: string[];
  factConflict?: boolean;
}

function fieldProblems(req: Requirement, entity: EntityMeta): string[] {
  const properties = new Map(entity.properties.map((p) => [p.name, p.type]));
  const fields = Array.isArray(req.fields)
    ? req.fields.map((f) => [f, undefined] as const)
    : Object.entries(req.fields);
  const problems = fields
    .filter(([name, type]) => !properties.has(name) || (type && properties.get(name) !== type))
    .map(([name, type]) => (type ? `${name}:${type}` : name));
  for (const key of req.keys ?? []) if (!entity.keys.includes(key)) problems.push(`key:${key}`);
  for (const field of req.forbiddenFields ?? [])
    if (properties.has(field)) problems.push(`forbidden:${field}`);
  if (req.forbiddenFields && req.keys && entity.keys.length !== req.keys.length)
    problems.push("key:exact-set");
  return problems;
}

/** Схема доказывает лишь объявленный контракт, а не существование объекта в конфигураторе или права. */
export function inspectRequirement(
  req: Requirement,
  cfg: ConnectionConfig,
  metadata?: MetadataMap,
): SchemaObservation {
  const candidates = targets(req, cfg);
  if (!metadata) return { status: "unknown", candidates, missing: [], unknown: ["metadata_unavailable"] };
  const profile = cfg.capabilityProfile ?? DEFAULT_PROFILE;
  const fact = profile.unsupported.find((f) => candidates.includes(f.entitySet));
  const entities = candidates.map((c) => metadata.entities.get(c)).filter((e): e is EntityMeta => !!e);
  if (!entities.length)
    return {
      status: fact ? "not_implemented" : "absent_from_accessible_metadata",
      candidates,
      missing: [],
      unknown: fact ? [] : ["configuration_existence_and_publication_cause"],
    };
  if (req.kind === "function") {
    for (const entity of entities) {
      const fn = metadata.functions?.find(
        (f) => f.name === req.function?.name && f.bindingType === entity.entityType,
      );
      if (!fn) continue;
      const missingParams = (req.function?.parameters ?? [])
        .map((p) => p.split(" ")[0]!)
        .filter((p) => !fn.parameters.some((x) => x.name === p));
      const missingFields = fn.properties.length
        ? fieldProblems({ ...req, keys: [] }, { ...entity, properties: fn.properties, keys: [] })
        : [];
      const missing = [...missingParams.map((p) => `parameter:${p}`), ...missingFields];
      return {
        status: missing.length ? "schema_incompatible" : fn.properties.length ? "published" : "unknown",
        candidates,
        entitySet: entity.entitySet,
        missing,
        unknown: fn.properties.length ? [] : ["function_result_schema"],
        ...(fact ? { factConflict: true } : {}),
      };
    }
    return {
      status: "unknown",
      candidates,
      entitySet: entities[0]!.entitySet,
      missing: [],
      unknown: ["function_declaration_and_result_schema"],
      ...(fact ? { factConflict: true } : {}),
    };
  }
  const checks = entities.map((e) => ({ entity: e, missing: fieldProblems(req, e) }));
  const best = checks.find((c) => !c.missing.length) ?? checks[0]!;
  return {
    status: best.missing.length ? "schema_incompatible" : "published",
    candidates,
    entitySet: best.entity.entitySet,
    missing: best.missing,
    unknown: [],
    ...(fact ? { factConflict: true } : {}),
  };
}
