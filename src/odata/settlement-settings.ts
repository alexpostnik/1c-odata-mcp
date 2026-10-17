import { z } from "zod";
import type { MetadataMap, EntityMeta } from "../types/odata.js";

const field = z.string().min(1);
/** Caller-supplied roles, never inferred from field names or bank numbers. */
export const settlementSettingsMappingSchema = z
  .object({
    entitySet: field,
    dimensions: z
      .object({ organization: field, counterparty: field, contract: field, settlementKind: field })
      .strict(),
    resources: z.object({ settlementAccount: field, advanceAccount: field.optional() }).strict(),
  })
  .strict();
export type SettlementSettingsMapping = z.infer<typeof settlementSettingsMappingSchema>;
const fieldSchema = z.object({ name: z.string(), type: z.string(), nullable: z.boolean() });
const candidateSchema = z.object({
  entitySet: z.string(),
  keys: z.array(z.string()),
  fields: z.array(fieldSchema),
  roles: z.literal("unknown"),
  relations: z.array(z.object({ name: z.string(), toType: z.string(), collection: z.boolean() })),
});
export const settlementSettingsResultSchema = z
  .object({
    database: z.string(),
    status: z.enum([
      "mapping_required",
      "mapping_fields_verified",
      "invalid_mapping",
      "not_published_or_different_name_or_unknown",
    ]),
    candidates: z.array(candidateSchema),
    count: z.number(),
    matchingCount: z.number(),
    truncated: z.boolean(),
    mapping: z
      .object({
        entitySet: z.string(),
        dimensions: settlementSettingsMappingSchema.shape.dimensions,
        resources: settlementSettingsMappingSchema.shape.resources,
        roles: z.literal("caller_supplied_unverified"),
        genericWriteSupport: z.literal("unknown"),
        missingFields: z.array(z.string()),
        duplicateFields: z.array(z.string()),
        fields: z.array(fieldSchema),
      })
      .optional(),
    limitations: z.array(z.string()),
  })
  .passthrough();
type DiscoveryResult = z.infer<typeof settlementSettingsResultSchema>;

/** Enumerates only schema facts. EDM keys do not prove business dimension/resource roles. */
export function discoverSettlementSettings(
  metadata: MetadataMap,
  database: string,
  options: { search?: string; limit?: number; mapping?: SettlementSettingsMapping },
): DiscoveryResult {
  const limit = options.limit ?? 50;
  const needle = options.search?.toLocaleLowerCase();
  const isRegister = (entity: EntityMeta) => entity.class === "informationRegister";
  const matching = [...metadata.entities.values()]
    .filter(
      (entity) =>
        isRegister(entity) &&
        (!needle ||
          [entity.entitySet, ...entity.properties.map((p) => p.name)].some((name) =>
            name.toLocaleLowerCase().includes(needle),
          )),
    )
    .sort((a, b) => a.entitySet.localeCompare(b.entitySet));
  const candidates = matching.slice(0, limit).map((entity) => ({
    entitySet: entity.entitySet,
    keys: [...entity.keys],
    fields: entity.properties.map((p) => ({ name: p.name, type: p.type, nullable: p.nullable })),
    roles: "unknown" as const,
    relations: entity.navigations.map((n) => ({ ...n })),
  }));
  const result: DiscoveryResult = {
    database,
    status: matching.length ? "mapping_required" : "not_published_or_different_name_or_unknown",
    candidates,
    count: candidates.length,
    matchingCount: matching.length,
    truncated: matching.length > candidates.length,
    limitations: [
      "Only published metadata is inspected. No matching entity can mean unpublished data, a different technical name, an incomplete search, or unknown visibility; it does not establish absence of 1C functionality.",
      "EDM keys, property types and navigation targets are reported exactly. Business dimension/resource roles, selection precedence and account analytics need configuration documentation and explicit caller evidence.",
      "Caller mappings validate field presence and distinctness only; they do not prove semantic roles or reference targets. No bank-number-to-ledger-account mapping or contract subkonto is inferred.",
      "Generic compound-key information-register read/write support is unknown; this diagnostic sends no business-data reads or writes and does not establish an access path.",
    ],
  };
  const mapping = options.mapping;
  if (!mapping) return result;
  const entity = metadata.entities.get(mapping.entitySet);
  if (!entity || !isRegister(entity))
    return { ...result, status: "not_published_or_different_name_or_unknown" };
  const mappedNames = [...Object.values(mapping.dimensions), ...Object.values(mapping.resources)];
  const names = new Set(entity.properties.map((p) => p.name));
  const missingFields = mappedNames.filter((name) => !names.has(name));
  const duplicateFields = [
    ...new Set(mappedNames.filter((name, index) => mappedNames.indexOf(name) !== index)),
  ];
  return {
    ...result,
    status: missingFields.length || duplicateFields.length ? "invalid_mapping" : "mapping_fields_verified",
    mapping: {
      ...mapping,
      roles: "caller_supplied_unverified",
      genericWriteSupport: "unknown",
      missingFields,
      duplicateFields,
      fields: entity.properties.map((p) => ({ name: p.name, type: p.type, nullable: p.nullable })),
    },
  };
}
