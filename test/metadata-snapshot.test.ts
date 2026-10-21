import { describe, expect, it, vi } from "vitest";
import { parseConfig } from "../src/config/env.js";
import { CATALOGS, DOCUMENTS, REGISTERS } from "../src/config/mapping.js";
import { Connection } from "../src/context.js";
import { createServer } from "../src/mcp/server.js";
import { bp30Enums, bp30Fields, bp30Metadata } from "./helpers/metadata-snapshot.js";

/**
 * Код против настоящей схемы БП 3.0, а не против рукописных заглушек: каждое поле, которое пишет
 * инструмент, есть у объекта, а вид операции — среди значений перечислений. Так ловятся поля,
 * которых нет в типовой конфигурации (например, Контрагент_Key у банковских документов — там
 * составной Контрагент + Контрагент_Type), и несуществующие виды операций.
 */

const id = "11111111-1111-4111-8111-111111111111";
const meta = bp30Metadata();
const operationKinds = new Set(
  [...bp30Enums()].filter(([name]) => name.startsWith("ВидыОпераций")).flatMap(([, v]) => [...v]),
);

/** Не опубликованы в базе, с которой снят снимок; инструменты без них отвечают понятной ошибкой. */
const NOT_IN_SNAPSHOT = new Set(["stock", "responsiblePersons"]);

describe("снимок схемы БП 3.0", () => {
  it("каждый объект из mapping.ts есть в снимке (хотя бы один из вариантов имени)", () => {
    const missing = Object.entries({ ...CATALOGS, ...DOCUMENTS, ...REGISTERS })
      .filter(([key, names]) => !NOT_IN_SNAPSHOT.has(key) && !names.some((n) => meta.entities.has(n)))
      .map(([key]) => key);
    expect(missing).toEqual([]);
  });

  it("проверка ловит поля и виды операций, которых нет в типовой конфигурации", () => {
    // Банковские документы: контрагент составной (Контрагент + Контрагент_Type), Контрагент_Key нет.
    expect(
      unknownFields("Document_ПоступлениеНаРасчетныйСчет", {
        Контрагент_Key: id,
        РасшифровкаПлатежа: [{ ДоговорКонтрагента_Key: id, СчетУчетаРасчетовПоАвансам_Key: id }],
      }),
    ).toEqual(["Контрагент_Key"]);
    expect(operationKinds.has("ОплатаПокупателя")).toBe(true);
    expect(operationKinds.has("ОплатаОтПокупателя")).toBe(false);
  });
});

function fixture() {
  const parsed = parseConfig({
    ODATA_BASE_URL: "https://example.invalid/odata/",
    ODATA_USERNAME: "u",
    ODATA_PASSWORD: "p",
  });
  const conn = new Connection(parsed.connections[0]!, parsed.behavior);
  vi.spyOn(conn, "available").mockResolvedValue(new Set(meta.entities.keys()));
  vi.spyOn(conn, "getMetadata").mockResolvedValue(meta);
  vi.spyOn(conn.client, "prepareCreate").mockResolvedValue(undefined as never);
  vi.spyOn(conn.client, "getEntity").mockResolvedValue({
    Ref_Key: id,
    Description: "Synthetic",
    Owner_Key: id,
    Организация_Key: id,
    ВидДоговора: "СПокупателем",
    ВалютаВзаиморасчетов_Key: id,
    Code: "643",
    Number: "0000-000001",
    Date: "2026-10-01T00:00:00",
    Posted: true,
    DeletionMark: false,
  });
  vi.spyOn(conn.client, "getCollection").mockImplementation(async (path: string) => ({
    value: path.startsWith("ChartOfAccounts_")
      ? ["41.01", "60.01", "60.02", "62.01", "62.02", "90.01.1", "90.02.1", "91.01", "91.02", "94"].map(
          (Code) => ({ Ref_Key: id, Code, Description: Code }),
        )
      : [{ Ref_Key: id, Description: "Synthetic", Code: "1", Owner_Key: id }],
  }));
  vi.spyOn(conn.client, "getText").mockResolvedValue("");
  const tools = (
    createServer({ db: () => conn } as never) as unknown as {
      _registeredTools: Record<
        string,
        {
          handler: (
            a: Record<string, unknown>,
            e: unknown,
          ) => Promise<{ isError?: boolean; structuredContent?: Record<string, unknown> }>;
        }
      >;
    }
  )._registeredTools;
  return tools;
}

const line = { nomenclatureRef: id, quantity: 1, price: 100, vatRate: "БезНДС" };
const stockLine = { nomenclatureRef: id, quantity: 1, price: 100 };
const CASES: Array<[string, Record<string, unknown>]> = [
  ["write.sales.create_invoice", { counterpartyRef: id, contractRef: id, lines: [line] }],
  ["write.sales.create_shipment", { counterpartyRef: id, contractRef: id, lines: [line] }],
  ["write.sales.create_act", { counterpartyRef: id, contractRef: id, lines: [line] }],
  ["write.purchase.create_purchase", { counterpartyRef: id, contractRef: id, lines: [line] }],
  ["write.purchase.create_supplier_invoice", { counterpartyRef: id, contractRef: id, lines: [line] }],
  ["write.warehouse.create_return_from_customer", { counterpartyRef: id, contractRef: id, lines: [line] }],
  ["write.warehouse.create_return_to_supplier", { counterpartyRef: id, contractRef: id, lines: [line] }],
  ["write.warehouse.create_surplus", { lines: [stockLine] }],
  ["write.warehouse.create_writeoff", { lines: [stockLine] }],
  [
    "write.warehouse.create_inventory",
    { lines: [{ nomenclatureRef: id, accountingQuantity: 0, factQuantity: 1, price: 100 }] },
  ],
  ["write.money.create_payment", { counterpartyRef: id, contractRef: id, amount: 1 }],
  [
    "write.money.create_bank_writeoff",
    { operationKind: "ОплатаПоставщику", amount: 1, counterpartyRef: id, contractRef: id },
  ],
  [
    "write.money.create_cash_receipt",
    { operationKind: "ОплатаПокупателя", amount: 1, counterpartyRef: id, contractRef: id },
  ],
  [
    "write.money.create_cash_payment",
    { operationKind: "ОплатаПоставщику", amount: 1, counterpartyRef: id, contractRef: id },
  ],
  [
    "write.money.create_payout_order",
    { counterpartyRef: id, amount: 1, number: "1", purposeText: "тест", recipientText: "тест" },
  ],
  ["write.counterparty.create_counterparty", { name: "Тест" }],
  ["write.catalog.create_nomenclature", { name: "Тест" }],
  ["write.catalog.create_contract", { counterpartyRef: id, kind: "СПокупателем" }],
];

/** Поля payload, которых нет в схеме: у шапки — в самом объекте, у строк — в табличной части. */
function unknownFields(entitySet: string, payload: Record<string, unknown>): string[] {
  const fields = bp30Fields(entitySet);
  if (!fields) return [`объект ${entitySet} отсутствует в снимке`];
  const bad: string[] = [];
  for (const [key, value] of Object.entries(payload)) {
    if (Array.isArray(value) && value.length && typeof value[0] === "object") {
      const section = bp30Fields(`${entitySet}_${key}`);
      if (!section) bad.push(`табличная часть ${key}`);
      else
        for (const row of value as Record<string, unknown>[])
          for (const k of Object.keys(row)) if (!section.has(k)) bad.push(`${key}.${k}`);
    } else if (!fields.has(key)) bad.push(key);
  }
  return [...new Set(bad)];
}

describe("инструменты создания пишут только поля типовой схемы", () => {
  it.each(CASES)("%s", async (name, args) => {
    const tools = fixture();
    const r = await tools[name]!.handler({ ...args, confirm: false }, {});
    expect(r.isError, JSON.stringify(r)).not.toBe(true);
    const sc = r.structuredContent ?? {};
    const entitySet = String(sc["entitySet"] ?? sc["willCreate"] ?? "");
    const payload = sc["payload"] as Record<string, unknown> | undefined;
    expect(payload, JSON.stringify(sc).slice(0, 300)).toBeDefined();
    expect(unknownFields(entitySet, payload!)).toEqual([]);
    if (typeof payload!["ВидОперации"] === "string")
      expect(operationKinds.has(payload!["ВидОперации"])).toBe(true);
  });
});
