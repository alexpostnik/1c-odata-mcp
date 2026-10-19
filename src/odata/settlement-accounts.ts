import type { Connection } from "../context.js";
import { CATALOGS, REGISTERS, resolveEntity } from "../config/mapping.js";
import type { ODataEntity } from "../types/odata.js";
import { fetchAll } from "./pagination.js";
import { and, cmp, odataGuid, or, buildQuery } from "./query.js";

/**
 * Счета расчётов и авансов с контрагентом из регистра «Счета учёта расчётов с контрагентами».
 *
 * 1С при создании документа через OData эту настройку не применяет (проверено на БП 3.0: реализация
 * получает общие 62.01/62.02 даже при особой настройке договора, оплата — пустые счета). Поэтому
 * счета подбираем сами, как форма документа: самая частная запись регистра по договору → контрагенту →
 * организации, с учётом вида расчётов договора.
 */

const EMPTY_GUID = "00000000-0000-0000-0000-000000000000";
const norm = (g: unknown): string | undefined => {
  const s = typeof g === "string" ? g : "";
  return s && s !== EMPTY_GUID ? s : undefined;
};

export type SettlementSide = "customer" | "supplier";

export type SettlementKind =
  "РасчетыВВалютеРегламентированногоУчета" | "РасчетыВИностраннойВалюте" | "РасчетыВУсловныхЕдиницах";

/** Уровень записи регистра, давшей счёт. */
export type SettlementLevel = "договор" | "контрагент" | "организация" | "общая";

export interface SettlementChoice {
  settle?: string | undefined;
  advance?: string | undefined;
  /** Уровень записи, из которой взят счёт расчётов (или авансов, если расчётов нет). */
  level: SettlementLevel;
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
 * Частность: договор > контрагент > организация > вид расчётов. Пустое измерение — «для всех»;
 * запись с другим (непустым) значением измерения не подходит.
 */
export function pickSettlementAccounts(
  rows: readonly ODataEntity[],
  key: {
    orgKey?: string | undefined;
    counterpartyRef?: string | undefined;
    contractRef: string;
    kind: SettlementKind;
  },
  side: SettlementSide,
): SettlementChoice | undefined {
  const scored: Array<{ row: ODataEntity; score: number }> = [];
  for (const r of rows) {
    const ctr = norm(r["Договор_Key"]);
    const cp = norm(r["Контрагент_Key"]);
    const og = norm(r["Организация_Key"]);
    const kind = typeof r["ВидРасчетовПоДоговору"] === "string" ? r["ВидРасчетовПоДоговору"] : "";
    if (ctr && ctr !== key.contractRef) continue;
    if (cp && cp !== key.counterpartyRef) continue;
    if (og && og !== key.orgKey) continue;
    if (kind && kind !== key.kind) continue;
    scored.push({ row: r, score: (ctr ? 8 : 0) + (cp ? 4 : 0) + (og ? 2 : 0) + (kind ? 1 : 0) });
  }
  scored.sort((a, b) => b.score - a.score);
  const res = RESOURCES[side];
  const first = (field: string) => scored.find((s) => norm(s.row[field]));
  const settle = first(res.settle);
  const advance = first(res.advance);
  const from = settle ?? advance;
  if (!from) return undefined;
  const r = from.row;
  const level: SettlementLevel = norm(r["Договор_Key"])
    ? "договор"
    : norm(r["Контрагент_Key"])
      ? "контрагент"
      : norm(r["Организация_Key"])
        ? "организация"
        : "общая";
  return { settle: norm(settle?.row[res.settle]), advance: norm(advance?.row[res.advance]), level };
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
): Promise<SettlementLookup> {
  const available = await conn.available();
  const reg = resolveEntity(REGISTERS.settlementAccounts, available);
  const ctrSet = resolveEntity(CATALOGS.contracts, available);
  if (!ctrSet) return { published: !!reg, side: p.side };
  const contract = await conn.client.getEntity(
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
  );
  const side = p.side ?? settlementSideOf(contract["ВидДоговора"]);
  const currencyCode = await currencyCodeOf(conn, norm(contract["ВалютаВзаиморасчетов_Key"]));
  const kind = contractSettlementKind(contract, currencyCode, conn.behavior.regulatedCurrencyCode ?? "643");
  if (!reg || !side) return { published: !!reg, side, kind };
  const orgKey = p.orgKey ?? norm(contract["Организация_Key"]);
  const counterpartyRef = norm(contract["Owner_Key"]);
  const anyOf = (field: string, ref: string | undefined) =>
    ref
      ? or(cmp(field, "eq", odataGuid(ref)), cmp(field, "eq", odataGuid(EMPTY_GUID)))
      : cmp(field, "eq", odataGuid(EMPTY_GUID));
  const { rows } = await fetchAll(
    conn.client,
    reg,
    {
      filter: and(
        `(${anyOf("Договор_Key", p.contractRef)})`,
        `(${anyOf("Контрагент_Key", counterpartyRef)})`,
        `(${anyOf("Организация_Key", orgKey)})`,
      ),
    },
    conn.behavior.pageSize,
    500,
  );
  return {
    published: true,
    side,
    kind,
    choice: pickSettlementAccounts(rows, { orgKey, counterpartyRef, contractRef: p.contractRef, kind }, side),
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
