import { z } from "zod";
import type { Connection } from "../context.js";
import { CATALOGS, REGISTERS, resolveEntity } from "../config/mapping.js";
import { InputError } from "../errors.js";
import type { ODataEntity } from "../types/odata.js";
import { accountsByRef } from "./accounting.js";
import { ODataError } from "./errors.js";
import { odataGuid, odataString } from "./query.js";
import { contractSettlementKind } from "./settlement-accounts.js";

import { EMPTY_GUID, exactRef, counterpartyCatalog, readCounterparty } from "./counterparty-hierarchy.js";
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
export const generalSettlementScope = {
  ...savedSettlementScope,
  scope: z.enum(["contract", "counterparty", "group"]),
  contractRef: exactRef.optional(),
};
const scopeSchema = z.object(generalSettlementScope).superRefine((p, ctx) => {
  if ((p.scope === "contract") !== (p.contractRef !== undefined))
    ctx.addIssue({
      code: "custom",
      message: "Для scope=contract нужен договор; для counterparty/group договор должен отсутствовать.",
    });
});
const writeSchema = scopeSchema.safeExtend({
  mode: z.enum(["create", "update"]),
  accounts: savedSettlementAccounts,
  singleWriter: z.boolean().optional(),
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
  contractKind?: unknown;
  scope: "contract" | "counterparty" | "group";
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
  const organizations = resolveEntity(CATALOGS.organizations, new Set(metadata.entities.keys()));
  if (!organizations)
    throw new InputError("Организации не опубликованы: точную организацию нельзя проверить.");
  const org = await conn.client.getEntity(`${organizations}(${odataGuid(p.organizationRef)})?$format=json`);
  if (
    String(org.Ref_Key).toLowerCase() !== p.organizationRef ||
    org.DeletionMark !== false ||
    org.IsFolder === true
  )
    throw new InputError("Указанная организация отсутствует или помечена на удаление.");
  const counterparties = await counterpartyCatalog(conn);
  await readCounterparty(conn, counterparties, p.counterpartyRef, p.scope === "group");
  let contractKind: unknown;
  if (p.scope === "contract") {
    const contracts = resolveEntity(CATALOGS.contracts, new Set(metadata.entities.keys()));
    if (!contracts)
      throw new InputError("Договоры не опубликованы: нельзя проверить владельца и организацию.");
    const contract = await conn.client.getEntity(`${contracts}(${odataGuid(p.contractRef!)})?$format=json`);
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
    contractKind = contract.ВидДоговора;
  }
  const key = {
    Организация_Key: p.organizationRef,
    Контрагент_Key: p.counterpartyRef,
    Договор_Key: p.contractRef ?? EMPTY_GUID,
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
    contractKind,
    scope: p.scope,
  };
}

/** Creates an absent exact record or patches explicit resources; versioned updates always use CAS. */
export async function saveSettlementSettings(
  conn: Connection,
  input: unknown,
  confirm: boolean,
): Promise<Record<string, unknown>> {
  const p = writeSchema.parse(input);
  const snapshot = await readSavedSettlementSettings(conn, p);
  const props = new Map(
    (await conn.getMetadata()).entities.get(snapshot.entitySet)!.properties.map((v) => [v.name, v.type]),
  );
  if (p.mode === "create" && snapshot.found)
    throw new InputError("Точная настройка уже существует. Для изменения нужен mode=update.");
  if (p.mode === "create" && p.expectedDataVersion !== undefined)
    throw new InputError("Для создания отсутствующей настройки expectedDataVersion не используется.");
  const concurrency = updateConcurrency(p, snapshot, props);
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
  const headers: Record<string, string> =
    concurrency === "dataVersion" ? { "If-Match": p.expectedDataVersion! } : {};
  // Bind the observed record to the preview without sending it as PATCH data. This is a
  // stale-preview check, not an atomic lock: versionless writes require a sole operator.
  const operation = {
    method,
    path,
    payload,
    headers,
    ...(concurrency === "singleWriter" ? { expectedRecord: publishedRecord(snapshot.record!, props) } : {}),
  };
  const notes = [
    "Изменяется только точная сохранённая настройка. Применение счетов к документу отдельно не проверено.",
  ];
  if (concurrency === "singleWriter")
    notes.push(
      "DataVersion отсутствует в metadata: If-Match не отправляется, серверная проверка версии недоступна. singleWriter=true подтверждает, что никто параллельно не меняет эти настройки. Предпросмотр связан с прочитанной записью; изменение после последнего чтения серверной блокировкой не защищено.",
    );
  if (snapshot.contractKind === "Прочее")
    notes.push(
      "Договор «Прочее»: автоматический подбор счетов поставщика/покупателя не применяется. Ресурсы указаны явно; это не настройка расхода или займа.",
    );
  if (!confirm) {
    await conn.client.prepareCreate(snapshot.entitySet, operation, { withRefKey: false });
    return { dryRun: true, database: conn.cfg.name, ...snapshot, ...operation, concurrency, notes };
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
    concurrency,
    notes,
  };
}

function updateConcurrency(
  input: z.infer<typeof writeSchema>,
  snapshot: SavedSettlementSnapshot,
  props: Map<string, string>,
): "create" | "dataVersion" | "singleWriter" {
  if (input.mode === "create") return "create";
  if (!snapshot.record)
    throw new InputError(
      "Изменение требует существующую точную настройку; отсутствующая запись не создаётся.",
    );
  if (props.has("DataVersion")) {
    if (props.get("DataVersion") !== "Edm.String")
      throw new InputError("DataVersion опубликован с неподдерживаемым типом; изменение запрещено.");
    if (!input.expectedDataVersion || snapshot.record.DataVersion !== input.expectedDataVersion)
      throw new InputError(
        "Изменение требует совпадающий expectedDataVersion (DataVersion из чтения); singleWriter не отменяет проверку версии.",
      );
    return "dataVersion";
  }
  if ("DataVersion" in snapshot.record || input.expectedDataVersion !== undefined)
    throw new InputError(
      "В metadata нет DataVersion, но версия передана или возвращена в записи. Не подставляйте вымышленную версию; проверьте схему и чтение.",
    );
  if (input.singleWriter !== true)
    throw new InputError(
      "В metadata нет DataVersion. Для последовательного изменения подтвердите singleWriter=true: никто параллельно не меняет эти настройки.",
    );
  return "singleWriter";
}

function publishedRecord(record: ODataEntity, props: Map<string, string>): ODataEntity {
  return Object.fromEntries(Object.entries(record).filter(([name]) => props.has(name)));
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
