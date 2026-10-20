import { z } from "zod";
import type { Connection } from "../context.js";
import { CATALOGS, REGISTERS, resolveEntity } from "../config/mapping.js";
import { InputError } from "../errors.js";
import type { ODataEntity } from "../types/odata.js";
import { accountsByRef } from "./accounting.js";
import { ODataError } from "./errors.js";
import { odataGuid, odataString } from "./query.js";
import { contractSettlementKind } from "./settlement-accounts.js";

const EMPTY_GUID = "00000000-0000-0000-0000-000000000000";
const exactRef = z
  .string()
  .uuid()
  .transform((v) => v.toLowerCase())
  .refine((v) => v !== EMPTY_GUID, "Пустая ссылка (для всех) запрещена.");
export const savedSettlementScope = {
  organizationRef: exactRef,
  counterpartyRef: exactRef,
  contractRef: exactRef,
  settlementKind: z.enum([
    "РасчетыВВалютеРегламентированногоУчета",
    "РасчетыВИностраннойВалюте",
    "РасчетыВУсловныхЕдиницах",
  ]),
};
export const savedSettlementAccounts = z
  .object({
    customerSettlement: exactRef.optional(),
    customerAdvance: exactRef.optional(),
    supplierSettlement: exactRef.optional(),
    supplierAdvance: exactRef.optional(),
  })
  .strict()
  .refine((v) => Object.values(v).some((ref) => ref !== undefined), "Укажите хотя бы один ресурс счетов.");
const scopeSchema = z.object(savedSettlementScope);
const writeSchema = scopeSchema.extend({
  mode: z.enum(["create", "update"]),
  accounts: savedSettlementAccounts,
  expectedDataVersion: z
    .string()
    .min(1)
    .max(200)
    .refine((v) => v !== "*" && !/[\r\n]/.test(v))
    .optional(),
});
const RESOURCE_FIELDS = {
  customerSettlement: "СчетУчетаРасчетовСПокупателем_Key",
  customerAdvance: "СчетУчетаАвансовПолученных_Key",
  supplierSettlement: "СчетУчетаРасчетовСПоставщиком_Key",
  supplierAdvance: "СчетУчетаАвансовВыданных_Key",
} as const;
const KEY_TYPES: Record<string, string> = {
  Организация_Key: "Edm.Guid",
  Контрагент_Key: "Edm.Guid",
  Договор_Key: "Edm.Guid",
  ВидРасчетовПоДоговору: "Edm.String",
};

export interface SavedSettlementSnapshot {
  entitySet: string;
  recordPath: string;
  key: Record<string, string>;
  found: boolean;
  record?: ODataEntity;
  contractKind: unknown;
}

/** Reads only the exact contract record; inherited organization/global defaults are never returned. */
export async function readSavedSettlementSettings(
  conn: Connection,
  input: unknown,
): Promise<SavedSettlementSnapshot> {
  const p = scopeSchema.parse(input);
  const metadata = await conn.getMetadata();
  const entitySet = resolveEntity(REGISTERS.settlementAccounts, new Set(metadata.entities.keys()));
  if (!entitySet)
    throw new InputError(
      "Регистр сохранённых счетов расчётов отсутствует в доступной OData-схеме; проверьте имя, состав публикации и права. Настройка не читается и не записывается.",
    );
  const meta = metadata.entities.get(entitySet)!;
  const props = new Map(meta.properties.map((v) => [v.name, v.type]));
  if (
    meta.keys.length !== 4 ||
    new Set(meta.keys).size !== 4 ||
    meta.keys.some((name) => !KEY_TYPES[name]) ||
    Object.entries(KEY_TYPES).some(([name, type]) => props.get(name) !== type) ||
    ["RecordSet", "Recorder", "Recorder_Key", "Recorder_Type", "Period"].some((name) => props.has(name))
  ) {
    throw new InputError(
      "Неподдерживаемая схема регистра: нужен независимый непериодический регистр с точным ключом организация/контрагент/договор/вид расчётов.",
    );
  }
  const contracts = resolveEntity(CATALOGS.contracts, new Set(metadata.entities.keys()));
  if (!contracts) throw new InputError("Договоры не опубликованы: нельзя проверить владельца и организацию.");
  const contract = await conn.client.getEntity(`${contracts}(${odataGuid(p.contractRef)})?$format=json`);
  if (
    contract.DeletionMark === true ||
    contract.IsFolder === true ||
    String(contract.Owner_Key).toLowerCase() !== p.counterpartyRef ||
    String(contract.Организация_Key).toLowerCase() !== p.organizationRef
  ) {
    throw new InputError(
      "Договор не принадлежит указанным контрагенту и организации либо недоступен для настройки.",
    );
  }
  const currencies = resolveEntity(CATALOGS.currencies, new Set(metadata.entities.keys()));
  if (
    !currencies ||
    typeof contract.ВалютаВзаиморасчетов_Key !== "string" ||
    contract.ВалютаВзаиморасчетов_Key === EMPTY_GUID
  )
    throw new InputError("Не установлена валюта договора: вид расчётов нельзя проверить.");
  const currency = await conn.client.getEntity(
    `${currencies}(${odataGuid(contract.ВалютаВзаиморасчетов_Key)})?$format=json&$select=Code`,
  );
  if (typeof currency.Code !== "string" || !currency.Code.trim())
    throw new InputError("Код валюты договора неизвестен: вид расчётов нельзя проверить.");
  if (
    p.settlementKind !==
    contractSettlementKind(contract, currency.Code, conn.behavior.regulatedCurrencyCode ?? "643")
  )
    throw new InputError("Вид расчётов не совпадает с валютой и условиями договора.");
  const key = {
    Организация_Key: p.organizationRef,
    Контрагент_Key: p.counterpartyRef,
    Договор_Key: p.contractRef,
    ВидРасчетовПоДоговору: p.settlementKind,
  };
  const recordPath = `${entitySet}(${meta.keys.map((name) => `${name}=${KEY_TYPES[name] === "Edm.Guid" ? odataGuid(key[name as keyof typeof key]) : odataString(key[name as keyof typeof key])}`).join(",")})`;
  let record: ODataEntity | undefined;
  try {
    record = await conn.client.getEntity(`${recordPath}?$format=json`);
  } catch (e) {
    if (!(e instanceof ODataError && e.kind === "not_found")) throw e;
  }
  if (record) assertRecordValues(record, key);
  return {
    entitySet,
    recordPath,
    key,
    found: !!record,
    ...(record ? { record } : {}),
    contractKind: contract.ВидДоговора,
  };
}

/** Creates an absent exact record or CAS-patches explicit resources. Never performs upsert or key edits. */
export async function saveSettlementSettings(
  conn: Connection,
  input: unknown,
  confirm: boolean,
): Promise<Record<string, unknown>> {
  const p = writeSchema.parse(input);
  const snapshot = await readSavedSettlementSettings(conn, p);
  if (p.mode === "create" && snapshot.found)
    throw new InputError(
      "Точная настройка уже существует. Для изменения нужен mode=update и её текущий DataVersion.",
    );
  if (
    p.mode === "update" &&
    (!snapshot.record || !p.expectedDataVersion || snapshot.record.DataVersion !== p.expectedDataVersion)
  )
    throw new InputError(
      "Изменение требует существующую точную настройку и совпадающий expectedDataVersion (DataVersion из чтения).",
    );
  if (p.mode === "create" && p.expectedDataVersion !== undefined)
    throw new InputError("Для создания отсутствующей настройки expectedDataVersion не используется.");
  const props = new Map(
    (await conn.getMetadata()).entities.get(snapshot.entitySet)!.properties.map((v) => [v.name, v.type]),
  );
  const patch: Record<string, string> = {};
  for (const [name, ref] of Object.entries(p.accounts)) {
    if (ref === undefined) continue;
    const field = RESOURCE_FIELDS[name as keyof typeof RESOURCE_FIELDS];
    if (props.get(field) !== "Edm.Guid")
      throw new InputError(`Ресурс ${field} не опубликован как Edm.Guid; запись запрещена.`);
    patch[field] = ref;
  }
  await accountsByRef(conn, Object.values(patch));
  const method = p.mode === "create" ? "POST" : "PATCH";
  const payload = p.mode === "create" ? { ...snapshot.key, ...patch } : patch;
  const path = `${p.mode === "create" ? snapshot.entitySet : snapshot.recordPath}?$format=json`;
  if (p.mode === "update" && props.get("DataVersion") !== "Edm.String")
    throw new InputError("В схеме нет DataVersion: безопасное изменение настройки недоступно.");
  const headers: Record<string, string> = p.mode === "update" ? { "If-Match": p.expectedDataVersion! } : {};
  const operation = { method, path, payload, headers };
  const notes = [
    "Изменяется только точная сохранённая настройка. Применение счетов к документу отдельно не проверено.",
  ];
  if (snapshot.contractKind === "Прочее")
    notes.push(
      "Договор «Прочее»: автоматический подбор счетов поставщика/покупателя не применяется. Ресурсы указаны явно; это не настройка расхода или займа.",
    );
  if (!confirm) {
    await conn.client.prepareCreate(snapshot.entitySet, operation, { withRefKey: false });
    return { dryRun: true, database: conn.cfg.name, ...snapshot, ...operation, notes };
  }
  const result = await conn.client.writeRecordOnce(snapshot.entitySet, operation, async () => {
    await conn.client.request(path, method, payload, headers);
    // A failed verification cannot turn an already successful mutation into a rejection.
    try {
      const record = await conn.client.getEntity(`${snapshot.recordPath}?$format=json`);
      assertRecordValues(record, { ...snapshot.key, ...patch });
      if (snapshot.record)
        assertRecordValues(
          record,
          Object.fromEntries(
            Object.entries(snapshot.record).filter(
              ([name]) => props.has(name) && name !== "DataVersion" && !(name in patch),
            ),
          ),
        );
      return { ...record, RecordPath: snapshot.recordPath };
    } catch (cause) {
      throw new ODataError({
        kind: "unknown",
        message:
          "После запроса записи контрольное чтение настройки не подтвердило результат; требуется ручная сверка, повторная запись не разрешена.",
        cause,
      });
    }
  });
  return {
    updated: true,
    verified: true,
    database: conn.cfg.name,
    entitySet: snapshot.entitySet,
    recordPath: snapshot.recordPath,
    record: result,
    notes,
  };
}

function assertRecordValues(record: ODataEntity, expected: Record<string, unknown>): void {
  for (const [name, value] of Object.entries(expected)) {
    const actual = record[name];
    const equal =
      typeof value === "string" && name.endsWith("_Key")
        ? typeof actual === "string" && actual.toLowerCase() === value.toLowerCase()
        : JSON.stringify(actual) === JSON.stringify(value);
    if (!equal)
      throw new ODataError({
        kind: "unknown",
        message: `Повторное чтение настройки не подтвердило поле ${name}; результат требует ручной сверки, повторная запись не разрешена.`,
      });
  }
}
