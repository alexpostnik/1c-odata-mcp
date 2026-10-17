import type { ConnectionConfig } from "../config/env.js";
import type { MetadataMap, ODataEntity } from "../types/odata.js";
import { InputError } from "../errors.js";
import { buildQuery, cmp, odataString, odataGuid } from "./query.js";

export const COUNTERPARTY_SET = "Catalog_Контрагенты";
const PLACEHOLDERS = new Set(["unknown", "undefined", "null", "none", "na", "test", "нет", "неизвестно"]);
interface PolicySource {
  cfg: ConnectionConfig;
  getMetadata(): Promise<MetadataMap>;
  client: {
    getCollection(path: string): Promise<unknown>;
    getEntity(path: string): Promise<ODataEntity>;
  };
}
export function requiresUniqueUnp(cfg: ConnectionConfig): boolean {
  return cfg?.counterpartyTaxPolicy === "by-required-unique";
}
/** Resolve only a published string property. An ambiguous schema needs an explicit mapping. */
export async function counterpartyTaxField(
  source: Pick<PolicySource, "cfg" | "getMetadata">,
): Promise<string> {
  if (!requiresUniqueUnp(source.cfg)) return "ИНН";
  const props = (await source.getMetadata()).entities.get(COUNTERPARTY_SET)?.properties ?? [];
  const candidates = source.cfg.counterpartyTaxField
    ? [source.cfg.counterpartyTaxField]
    : ["УНП", "ИНН"].filter((name) => props.some((p) => p.name === name));
  if (candidates.length !== 1 || !props.some((p) => p.name === candidates[0] && p.type === "Edm.String")) {
    throw new InputError(
      "UNP requires one published Edm.String property; configure COUNTERPARTY_TAX_FIELD from metadata.",
    );
  }
  return candidates[0]!;
}
export function validateUnp(value: unknown): string {
  if (
    typeof value !== "string" ||
    !value ||
    value !== value.trim() ||
    !/^[\p{L}\p{N}]+$/u.test(value) ||
    /^0+$/.test(value) ||
    PLACEHOLDERS.has(value.toLowerCase())
  ) {
    throw new InputError(
      "A confirmed nonblank alphanumeric UNP is required; whitespace, zero and placeholder values are forbidden.",
    );
  }
  return value;
}
/** One exact filtered read; no full-catalog fallback and no deletion-mark exclusion. */
export async function checkCounterpartyPolicy(
  source: PolicySource,
  fields: Record<string, unknown>,
  ref?: string,
): Promise<string | undefined> {
  if (!requiresUniqueUnp(source.cfg)) return;
  const field = await counterpartyTaxField(source);
  const existing = ref
    ? await source.client.getEntity(
        `${COUNTERPARTY_SET}(${odataGuid(ref)})${buildQuery({ select: ["Ref_Key", field, "IsFolder"] })}`,
      )
    : {};
  const folder = Object.hasOwn(fields, "IsFolder") ? fields.IsFolder : (existing.IsFolder ?? false);
  if (typeof folder !== "boolean") throw new InputError("Counterparty IsFolder must be boolean.");
  if (folder) {
    if (ref && existing.IsFolder !== true)
      throw new InputError("A counterparty card cannot bypass UNP policy by converting to a folder.");
    return;
  }
  if (
    Object.hasOwn(fields, "Description") &&
    (typeof fields.Description !== "string" || !fields.Description.trim())
  )
    throw new InputError("Counterparty Description must be nonblank.");
  if (Object.hasOwn(fields, "ИНН") && field !== "ИНН")
    throw new InputError("Use the configured UNP property for generic counterparty writes.");
  const unp = validateUnp(Object.hasOwn(fields, field) ? fields[field] : existing[field]);
  let response: unknown;
  try {
    response = await source.client.getCollection(
      `${COUNTERPARTY_SET}${buildQuery({ filter: cmp(field, "eq", odataString(unp)), select: ["Ref_Key", field], top: 2 })}`,
    );
  } catch {
    throw new InputError("UNP uniqueness could not be verified; no write sent.");
  }
  if (!response || typeof response !== "object" || Array.isArray(response))
    throw new InputError("UNP uniqueness response is incomplete; no write sent.");
  const page = response as Record<string, unknown>;
  const rows = page.value;
  if (
    !Array.isArray(rows) ||
    ["__next", "next_link", "@odata.nextLink", "odata.nextLink"].some((k) => page[k])
  )
    throw new InputError("UNP uniqueness response is incomplete; no write sent.");
  const normalizeRef = (v: unknown) => (typeof v === "string" ? v.replace(/[{}]/g, "").toLowerCase() : "");
  for (const row of rows) {
    if (!row || typeof row !== "object" || row[field] !== unp || !normalizeRef(row.Ref_Key))
      throw new InputError("UNP uniqueness filter or response cannot be verified; no write sent.");
    if (!ref || normalizeRef(row.Ref_Key) !== normalizeRef(ref))
      throw new InputError("UNP already exists in this database; no write sent.");
  }
  if (rows.length > 1)
    throw new InputError("UNP uniqueness response contains repeated matches; no write sent.");
  return unp;
}

// Serialize policy check + transport across aliases to the same URL in this process.
const tails = new Map<string, Promise<void>>();
export async function withCounterpartyLock<T>(target: string, run: () => Promise<T>): Promise<T> {
  const previous = tails.get(target) ?? Promise.resolve();
  let release!: () => void;
  const next = new Promise<void>((resolve) => {
    release = resolve;
  });
  tails.set(target, next);
  await previous;
  try {
    return await run();
  } finally {
    release();
    if (tails.get(target) === next) tails.delete(target);
  }
}
