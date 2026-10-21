import { z } from "zod";
import type { Connection } from "../context.js";
import { CATALOGS, resolveEntity } from "../config/mapping.js";
import { InputError } from "../errors.js";
import type { ODataEntity } from "../types/odata.js";
import { odataGuid } from "./query.js";

export const EMPTY_GUID = "00000000-0000-0000-0000-000000000000";
export const exactRef = z
  .string()
  .uuid()
  .transform((v) => v.toLowerCase())
  .refine((v) => v !== EMPTY_GUID, "Пустая ссылка (для всех) запрещена.");
export const MAX_GROUP_DEPTH = 32;

export async function counterpartyCatalog(conn: Connection): Promise<string> {
  const metadata = await conn.getMetadata();
  const set = resolveEntity(CATALOGS.counterparties, new Set(metadata.entities.keys()));
  const types = new Map(metadata.entities.get(set ?? "")?.properties.map((p) => [p.name, p.type]));
  if (
    !set ||
    types.get("Parent_Key") !== "Edm.Guid" ||
    types.get("IsFolder") !== "Edm.Boolean" ||
    types.get("DeletionMark") !== "Edm.Boolean" ||
    types.get("Ref_Key") !== "Edm.Guid"
  )
    throw new InputError(
      "Не опубликована совместимая иерархия контрагентов (Ref_Key, Parent_Key, IsFolder, DeletionMark); подбор по группам недоступен.",
    );
  return set;
}

export async function readCounterparty(
  conn: Connection,
  set: string,
  ref: string,
  isFolder: boolean,
): Promise<ODataEntity> {
  const row = await conn.client.getEntity(`${set}(${odataGuid(ref)})?$format=json`);
  if (
    String(row.Ref_Key).toLowerCase() !== ref.toLowerCase() ||
    row.DeletionMark !== false ||
    row.IsFolder !== isFolder
  )
    throw new InputError(
      "Контрагент/группа отсутствует, помечен на удаление или IsFolder не соответствует выбранной области.",
    );
  return row;
}

/** Nearest group first; never use a partial or unverifiable ancestor chain. */
export async function counterpartyAncestors(
  conn: Connection,
  ref: string,
  isFolder = false,
): Promise<{ entitySet: string; subject: ODataEntity; groupRefs: string[] }> {
  const entitySet = await counterpartyCatalog(conn);
  const subject = await readCounterparty(conn, entitySet, ref, isFolder);
  const seen = new Set([ref.toLowerCase()]);
  const groupRefs: string[] = [];
  let row = subject;
  for (;;) {
    const parsed = z.string().uuid().safeParse(row.Parent_Key);
    if (!parsed.success)
      throw new InputError("Родитель контрагента/группы неизвестен; подбор по неполной иерархии запрещён.");
    const parent = parsed.data.toLowerCase();
    if (parent === EMPTY_GUID) return { entitySet, subject, groupRefs };
    if (seen.has(parent)) throw new InputError("Цикл в иерархии контрагентов; подбор по группам запрещён.");
    if (groupRefs.length >= MAX_GROUP_DEPTH)
      throw new InputError(
        `Иерархия контрагентов глубже ${MAX_GROUP_DEPTH}; подбор по неполной цепочке запрещён.`,
      );
    seen.add(parent);
    row = await readCounterparty(conn, entitySet, parent, true);
    groupRefs.push(parent);
  }
}
