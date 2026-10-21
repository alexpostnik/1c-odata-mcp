import type { Connection } from "../context.js";
import { fetchAll } from "./pagination.js";
import { CATALOGS } from "../config/mapping.js";
import { requireEntity } from "./publication.js";
import { InputError } from "../errors.js";
import { cached, lookupWithRefresh } from "./cache.js";

export interface Organization {
  ref: string;
  name: string;
  inn?: string;
}

const orgSet = (conn: Connection): Promise<string> =>
  requireEntity(conn, CATALOGS.organizations, "Справочник «Организации»");

async function loadOrganizations(conn: Connection): Promise<Organization[]> {
  const set = await orgSet(conn);
  const { rows } = await fetchAll(
    conn.client,
    set,
    { select: ["Ref_Key", "Description", "ИНН"], orderby: "Description" },
    conn.behavior.pageSize,
    conn.behavior.maxRows,
  );
  return rows.map((r) => ({
    ref: String(r["Ref_Key"] ?? ""),
    name: String(r["Description"] ?? ""),
    inn: r["ИНН"] ? String(r["ИНН"]) : undefined,
  }));
}

const ORGS_KEY = "organizations";
const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Все организации (юрлица) базы — из кэша подключения (cache.ts). */
export async function listOrganizations(conn: Connection): Promise<Organization[]> {
  return cached(conn, ORGS_KEY, () => loadOrganizations(conn));
}

/**
 * Резолвит организацию в Ref_Key: по части названия (без учёта регистра), по Ref_Key или по ИНН.
 * Бросает понятную ошибку, если ничего не найдено или найдено неоднозначно.
 */
export async function resolveOrganization(conn: Connection, query: string): Promise<Organization> {
  const q = query
    .trim()
    .replace(/^\{|\}$/g, "")
    .toLowerCase();
  // Ссылка из list_organizations или ИНН — точное совпадение; иначе — часть названия.
  const match = GUID.test(q)
    ? (o: Organization) => o.ref.toLowerCase() === q
    : /^\d{10,12}$/.test(q)
      ? (o: Organization) => o.inn === q
      : (o: Organization) => o.name.toLowerCase().includes(q);
  const found = await lookupWithRefresh(
    conn,
    ORGS_KEY,
    () => loadOrganizations(conn),
    (orgs) => orgs.filter(match),
    (r) => r.length === 0,
  );
  if (found.length === 0) {
    throw new InputError(`Организация "${query}" не найдена. Список — в list_organizations.`);
  }
  // Точное совпадение названия снимает неоднозначность («Ромашка» и «Ромашка-Юг»).
  const exact = found.filter((o) => o.name.toLowerCase() === q);
  if (found.length > 1 && exact.length !== 1) {
    const names = found.map((o) => o.name).join(", ");
    throw new InputError(`Под "${query}" подходит несколько организаций: ${names}. Уточните название.`);
  }
  return exact.length === 1 ? exact[0]! : found[0]!;
}

/**
 * Резолвит организацию по названию; без названия — авто-выбор, если в базе
 * ровно одна. Общий хелпер для всех инструментов с необязательным `organization`.
 */
export async function resolveOrgOrDefault(
  conn: Connection,
  organization: string | undefined,
): Promise<Organization> {
  if (organization) return resolveOrganization(conn, organization);
  const orgs = await listOrganizations(conn);
  if (orgs.length === 1) return orgs[0]!;
  throw new InputError(
    `В базе несколько организаций — укажите organization. Доступные: ${orgs.map((o) => o.name).join(", ")}`,
  );
}
