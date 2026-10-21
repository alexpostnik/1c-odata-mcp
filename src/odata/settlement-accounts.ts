import type { Connection } from "../context.js";
import { CATALOGS, REGISTERS, resolveEntity } from "../config/mapping.js";
import type { ODataEntity } from "../types/odata.js";
import { fetchAll } from "./pagination.js";
import { and, cmp, odataGuid, or, buildQuery } from "./query.js";
import { InputError } from "../errors.js";
import { counterpartyAncestors, EMPTY_GUID } from "./counterparty-hierarchy.js";

/**
 * Счета расчётов и авансов с контрагентом из регистра «Счета учёта расчётов с контрагентами».
 *
 * 1С при создании документа через OData эту настройку не применяет (проверено на БП 3.0: реализация
 * получает общие 62.01/62.02 даже при особой настройке договора, оплата — пустые счета). Поэтому
 * счета подбираем сами по политике MCP: договор → контрагент → текущие группы → организация,
 * с учётом вида расчётов договора. Native порядок вложенных групп не подтверждён.
 */

const norm = (g: unknown): string | undefined => {
  const s = typeof g === "string" ? g.toLowerCase() : "";
  return s && s !== EMPTY_GUID ? s : undefined;
};

export type SettlementSide = "customer" | "supplier";

export type SettlementKind =
  "РасчетыВВалютеРегламентированногоУчета" | "РасчетыВИностраннойВалюте" | "РасчетыВУсловныхЕдиницах";

/** Уровень записи регистра, давшей счёт. */
export type SettlementLevel = "договор" | "контрагент" | "группа" | "организация" | "общая";

export interface SettlementProvenance {
  level: SettlementLevel;
  organizationRef?: string | undefined;
  counterpartyRef?: string | undefined;
  contractRef?: string | undefined;
  groupDepth?: number | undefined;
  kind?: string | undefined;
}

export interface SettlementChoice {
  settle?: string | undefined;
  advance?: string | undefined;
  /** Уровень записи, из которой взят счёт расчётов (или авансов, если расчётов нет). */
  level: SettlementLevel;
  provenance: { settle?: SettlementProvenance | undefined; advance?: SettlementProvenance | undefined };
}

/** Ресурсы регистра для стороны расчётов. */
const RESOURCES: Record<SettlementSide, { settle: string; advance: string }> = {
  customer: { settle: "СчетУчетаРасчетовСПокупателем_Key", advance: "СчетУчетаАвансовПолученных_Key" },
  supplier: { settle: "СчетУчетаРасчетовСПоставщиком_Key", advance: "СчетУчетаАвансовВыданных_Key" },
};

/** Сторона расчётов по виду договора; прочие виды (комиссия, «Прочее») — не подбираем. */
export function settlementSideOf(contractKind: unknown): SettlementSide | undefined {
  if (contractKind === "СПокупателем") return "customer";
  if (contractKind === "СПоставщиком") return "supplier";
  return undefined;
}

/**
 * Вид расчётов по договору — тот же признак, которым 1С сохраняет настройку из формы договора
 * («в валюте регламентированного учёта», а не пустой).
 */
export function contractSettlementKind(
  contract: ODataEntity,
  currencyCode: string | undefined,
  regulatedCurrencyCode: string,
): SettlementKind {
  if (contract["РасчетыВУсловныхЕдиницах"] === true) return "РасчетыВУсловныхЕдиницах";
  if (currencyCode === undefined || currencyCode.trim() === regulatedCurrencyCode) {
    return "РасчетыВВалютеРегламентированногоУчета";
  }
  return "РасчетыВИностраннойВалюте";
}

/**
 * Выбирает счета из записей регистра: для каждого счёта — самая частная запись, где он заполнен.
 * Политика MCP: договор > контрагент > ближайшая группа > дальняя группа > организация > вид расчётов.
 * Пустое измерение — «для всех»;
 * запись с другим (непустым) значением измерения не подходит.
 */
export function pickSettlementAccounts(
  rows: readonly ODataEntity[],
  key: {
    orgKey?: string | undefined;
    counterpartyRef?: string | undefined;
    contractRef: string;
    kind: SettlementKind;
    groupRefs?: readonly string[];
  },
  side: SettlementSide,
): SettlementChoice | undefined {
  key = {
    ...key,
    orgKey: norm(key.orgKey),
    counterpartyRef: norm(key.counterpartyRef),
    contractRef: key.contractRef.toLowerCase(),
    groupRefs: key.groupRefs?.map((ref) => ref.toLowerCase()),
  };
  const scored: Array<{ row: ODataEntity; score: number; source: SettlementProvenance }> = [];
  for (const r of rows) {
    const ctr = norm(r["Договор_Key"]);
    const cp = norm(r["Контрагент_Key"]);
    const og = norm(r["Организация_Key"]);
    const kind = typeof r["ВидРасчетовПоДоговору"] === "string" ? r["ВидРасчетовПоДоговору"] : "";
    if (ctr && ctr !== key.contractRef) continue;
    const groupDepth = cp ? (key.groupRefs ?? []).indexOf(cp) : -1;
    if (cp && cp !== key.counterpartyRef && groupDepth < 0) continue;
    // A contract-specific row can belong only to its actual individual owner.
    if (ctr && groupDepth >= 0) continue;
    if (og && og !== key.orgKey) continue;
    if (kind && kind !== key.kind) continue;
    const subjectScore = cp === key.counterpartyRef && cp ? 1000 : groupDepth >= 0 ? 900 - groupDepth * 4 : 0;
    const level: SettlementLevel = ctr
      ? "договор"
      : groupDepth >= 0
        ? "группа"
        : cp
          ? "контрагент"
          : og
            ? "организация"
            : "общая";
    scored.push({
      row: r,
      score: (ctr ? 2000 : 0) + subjectScore + (og ? 2 : 0) + (kind ? 1 : 0),
      source: {
        level,
        organizationRef: og,
        counterpartyRef: cp,
        contractRef: ctr,
        kind: kind || undefined,
        ...(groupDepth >= 0 ? { groupDepth } : {}),
      },
    });
  }
  scored.sort((a, b) => b.score - a.score);
  const res = RESOURCES[side];
  const first = (field: string) => scored.find((s) => norm(s.row[field]));
  const settle = first(res.settle);
  const advance = first(res.advance);
  const from = settle ?? advance;
  if (!from) return undefined;
  return {
    settle: norm(settle?.row[res.settle]),
    advance: norm(advance?.row[res.advance]),
    level: from.source.level,
    provenance: { settle: settle?.source, advance: advance?.source },
  };
}

export interface SettlementLookup {
  /** Регистр «Счета учёта расчётов с контрагентами» опубликован в OData. */
  published: boolean;
  choice?: SettlementChoice | undefined;
  side?: SettlementSide | undefined;
  /** Вид расчётов договора; undefined — справочник договоров не опубликован. */
  kind?: SettlementKind | undefined;
}

/**
 * Читает договор (сторона, вид расчётов) и, если регистр опубликован, подбирает по нему счета.
 * Вид расчётов нужен и без регистра: от него зависит, можно ли заполнить сумму взаиморасчётов.
 */
export async function settlementAccountsFor(
  conn: Connection,
  p: { orgKey?: string | undefined; contractRef: string; side?: SettlementSide | undefined },
  mutation?: { contract: ODataEntity },
): Promise<SettlementLookup> {
  const available = await conn.available();
  const reg = resolveEntity(REGISTERS.settlementAccounts, available);
  const ctrSet = resolveEntity(CATALOGS.contracts, available);
  if (!ctrSet) {
    if (mutation) throw new InputError("Договоры не опубликованы; вид расчётов неизвестен.");
    return { published: !!reg, side: p.side };
  }
  // Mutation callers supply the exact full contract they already validated. Do not reread
  // a partial snapshot that could disagree with its owner, organization or currency.
  const contract =
    mutation?.contract ??
    (await conn.client.getEntity(
      `${ctrSet}(${odataGuid(p.contractRef)})${buildQuery({
        select: [
          "Ref_Key",
          "Owner_Key",
          "Организация_Key",
          "ВидДоговора",
          "ВалютаВзаиморасчетов_Key",
          "РасчетыВУсловныхЕдиницах",
        ],
      })}`,
    ));
  const side = p.side ?? settlementSideOf(contract["ВидДоговора"]);
  const currencyCode = await currencyCodeOf(conn, norm(contract["ВалютаВзаиморасчетов_Key"]));
  if (mutation && (!currencyCode?.trim() || typeof contract["РасчетыВУсловныхЕдиницах"] !== "boolean"))
    throw new InputError(
      "Код валюты или признак расчётов в условных единицах неизвестен; применение счетов запрещено.",
    );
  const kind = contractSettlementKind(contract, currencyCode, conn.behavior.regulatedCurrencyCode ?? "643");
  if (!reg || !side) return { published: !!reg, side, kind };
  const orgKey = p.orgKey ?? norm(contract["Организация_Key"]);
  const counterpartyRef = norm(contract["Owner_Key"]);
  if (mutation && !counterpartyRef)
    throw new InputError("Владелец договора неизвестен: иерархию контрагента нельзя проверить.");
  // Existing creation keeps its original contract/individual/organization lookup and call count.
  // Only the explicitly requested mutation follows the current group hierarchy.
  const groupRefs = mutation ? (await counterpartyAncestors(conn, counterpartyRef!)).groupRefs : [];
  const anyOf = (field: string, ref: string | undefined) =>
    ref
      ? or(cmp(field, "eq", odataGuid(ref)), cmp(field, "eq", odataGuid(EMPTY_GUID)))
      : cmp(field, "eq", odataGuid(EMPTY_GUID));
  const { rows, truncated } = await fetchAll(
    conn.client,
    reg,
    {
      filter: and(
        `(${anyOf("Договор_Key", p.contractRef)})`,
        `(${or(...[...(counterpartyRef ? [counterpartyRef] : []), ...groupRefs, EMPTY_GUID].map((ref) => cmp("Контрагент_Key", "eq", odataGuid(ref))))})`,
        `(${anyOf("Организация_Key", orgKey)})`,
      ),
    },
    conn.behavior.pageSize,
    500,
  );
  if (mutation && truncated)
    throw new InputError("Список настроек счетов усечён лимитом 500; выбор из неполных данных запрещён.");
  return {
    published: true,
    side,
    kind,
    choice: pickSettlementAccounts(
      rows,
      { orgKey, counterpartyRef, contractRef: p.contractRef, kind, groupRefs },
      side,
    ),
  };
}

async function currencyCodeOf(
  conn: Connection,
  currencyRef: string | undefined,
): Promise<string | undefined> {
  if (!currencyRef) return undefined;
  const set = resolveEntity(CATALOGS.currencies, await conn.available());
  if (!set) return undefined;
  try {
    const cur = await conn.client.getEntity(
      `${set}(${odataGuid(currencyRef)})${buildQuery({ select: ["Code"] })}`,
    );
    return typeof cur["Code"] === "string" ? cur["Code"] : undefined;
  } catch {
    return undefined;
  }
}
