import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createServer } from "../src/mcp/server.js";
import { fake1C } from "./helpers/fake-1c.js";
import { SNAPSHOT_REF as id } from "./helpers/metadata-snapshot.js";

/**
 * Все инструменты против «поддельной 1С» на снимке схемы БП 3.0 (test/helpers/fake-1c.ts):
 * чтение — как есть, запись — в режиме предпросмотра. Ни один запрос не должен обращаться к
 * полю, объекту или значению перечисления, которых нет в типовой конфигурации.
 *
 * Бюджет запросов: число обращений к 1С на каждый инструмент сверяется с
 * test/fixtures/request-budget.json. Больше нормы — тест падает (на облачной 1С каждый запрос —
 * около секунды). Если рост осознанный, обновите норму: UPDATE_REQUEST_BUDGET=1 npx vitest run test/fake-1c.test.ts
 */

const BUDGET_FILE = new URL("./fixtures/request-budget.json", import.meta.url);
const budget: Record<string, number> = existsSync(BUDGET_FILE)
  ? (JSON.parse(readFileSync(BUDGET_FILE, "utf8")) as Record<string, number>)
  : {};
const measured: Record<string, number> = {};

type Schema = {
  type?: string | string[];
  enum?: unknown[];
  properties?: Record<string, Schema>;
  required?: string[];
  items?: Schema;
  default?: unknown;
};
const manifest = JSON.parse(readFileSync(new URL("../manifest.json", import.meta.url), "utf8")) as {
  tools: Array<{ name: string; inputSchema: Schema }>;
};

const line = { nomenclatureRef: id, quantity: 1, price: 100, vatRate: "БезНДС" };
const SETTINGS_SCOPE = {
  organizationRef: id,
  counterpartyRef: id,
  contractRef: id,
  settlementKind: "РасчетыВВалютеРегламентированногоУчета",
};
// Запись регистра в подделке уже есть, версии у регистра нет — правка «одним пользователем».
const SETTINGS_UPDATE = { mode: "update", singleWriter: true, accounts: { customerSettlement: id } };

/** Аргументы, которые не подобрать по имени поля. Новый инструмент обычно сюда не нужен. */
const OVERRIDES: Record<string, Record<string, unknown>> = {
  "read.document.get_document": { entitySet: "Document_РеализацияТоваровУслуг", ref: id },
  "read.document.search_documents": { entitySet: "Document_РеализацияТоваровУслуг" },
  "read.accounting.get_document_postings": {
    documentEntity: "Document_РеализацияТоваровУслуг",
    documentRef: id,
  },
  "read.audit.get_document_history": { documentEntity: "Document_РеализацияТоваровУслуг", documentRef: id },
  "read.analytics.get_deal_history": { contractRef: id },
  "write.counterparty.update_counterparty": { ref: id, name: "Тест" },
  "write.catalog.update_nomenclature": { ref: id, name: "Тест" },
  "read.schema.describe_entity": { entitySet: "Catalog_Контрагенты" },
  "write.document.post_document": { entitySet: "Document_РеализацияТоваровУслуг", ref: id },
  "write.entity.mark_for_deletion": { entitySet: "Catalog_Контрагенты", ref: id },
  "write.entity.update_entity": {
    entitySet: "Catalog_Контрагенты",
    ref: id,
    fields: { Комментарий: "тест" },
  },
  "write.entity.create_folder": { entitySet: "Catalog_Контрагенты", name: "Тест" },
  "write.entity.move_to_folder": { entitySet: "Catalog_Контрагенты", ref: id, folder: id },
  "write.document.update_document_lines": {
    entitySet: "Document_РеализацияТоваровУслуг",
    ref: id,
    lines: [line],
  },
  "write.document.add_document_line": { entitySet: "Document_РеализацияТоваровУслуг", ref: id, line },
  "write.document.remove_document_line": {
    entitySet: "Document_РеализацияТоваровУслуг",
    ref: id,
    lineNumber: 1,
  },
  "write.document.copy_document": { entitySet: "Document_СчетНаОплатуПокупателю", ref: id },
  "write.warehouse.create_transfer": {
    fromWarehouse: "Synthetic",
    toWarehouse: "Synthetic",
    lines: [{ nomenclatureRef: id, quantity: 1, price: 100 }],
  },
  "write.warehouse.create_inventory": {
    lines: [{ nomenclatureRef: id, accountingQuantity: 0, factQuantity: 1, price: 100 }],
  },
  "write.sales.create_services_act": {
    counterpartyRef: id,
    nomenclatureGroupRef: id,
    lines: [{ name: "Тест", quantity: 1, price: 100, nomenclatureRef: id, vatRate: "БезНДС" }],
  },
  "write.money.create_bank_writeoff": { operationKind: "ОплатаПоставщику", amount: 1 },
  "write.money.create_cash_payment": { operationKind: "ОплатаПоставщику", amount: 1 },
  "write.document.apply_bank_settlement_settings": {
    entitySet: "Document_ПоступлениеНаРасчетныйСчет",
    ref: id,
    contractRef: id,
    side: "customer",
    expectedDataVersion: "AAAAAAAAAAA=",
  },
  "write.counterparty.set_group": {
    ref: id,
    subjectKind: "counterparty",
    parentRef: "00000000-0000-0000-0000-000000000000",
    expectedDataVersion: "AAAAAAAAAAA=",
  },
  "write.counterparty.create_group": { name: "Тест", parentRef: "00000000-0000-0000-0000-000000000000" },
  "read.accounting.get_settlement_settings": { ...SETTINGS_SCOPE, scope: "contract" },
  "read.accounting.get_contract_settlement_settings": SETTINGS_SCOPE,
  "write.accounting.set_settlement_settings": { ...SETTINGS_SCOPE, ...SETTINGS_UPDATE, scope: "contract" },
  "write.accounting.set_contract_settlement_settings": { ...SETTINGS_SCOPE, ...SETTINGS_UPDATE },
  "write.operation.status": { operationId: "00000000-0000-4000-8000-000000000000" },
};

/** Значение аргумента по имени и схеме поля. */
function valueFor(name: string, s: Schema): unknown {
  if (s.enum?.length) return s.enum[0];
  if (name === "lines") return [line];
  if (name === "line") return line;
  if (/Ref$|^ref$/.test(name)) return id;
  if (name === "month") return "2026-09";
  if (/^(from|date|asOf|incomingDate)$/.test(name)) return "2026-09-01";
  if (name === "to") return "2026-09-30";
  if (name === "accountNumber") return "40702810000000000001";
  if (name === "bik") return "044525225";
  if (name === "accounts") return { customerSettlement: id };
  if (name === "expectedDataVersion") return "AAAAAAAAAAA=";
  if (name === "fields") return { Комментарий: "тест" };
  const type = Array.isArray(s.type) ? s.type[0] : s.type;
  if (type === "number" || type === "integer") return 1;
  if (type === "boolean") return false;
  if (type === "array") return [];
  if (type === "object") return {};
  return name === "account" ? "62" : "Тест";
}

function argsFor(tool: { name: string; inputSchema: Schema }): Record<string, unknown> {
  const props = tool.inputSchema.properties ?? {};
  const args: Record<string, unknown> = {};
  for (const name of tool.inputSchema.required ?? []) args[name] = valueFor(name, props[name] ?? {});
  Object.assign(args, OVERRIDES[tool.name]);
  if (tool.name.startsWith("write.") && "confirm" in props) args.confirm = false;
  return args;
}

/**
 * Инструменты, которым синтетических данных подделки мало для успешного ответа (ошибка — по
 * бизнес-логике, а не из-за схемы). Нарушения схемы проверяются и для них.
 */
const EXPECTED_ERRORS: Record<string, string> = {};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("все инструменты против поддельной 1С на снимке схемы БП 3.0", () => {
  it.each(manifest.tools.map((t) => [t.name, t] as const))("%s", async (name, tool) => {
    const fake = fake1C({ READ_ONLY: "true" });
    const tools = (
      createServer(fake.ctx) as unknown as {
        _registeredTools: Record<
          string,
          {
            inputSchema?: { parse: (a: unknown) => Record<string, unknown> };
            handler: (
              a: Record<string, unknown>,
              e: unknown,
            ) => Promise<{ isError?: boolean; content?: { text?: string }[] }>;
          }
        >;
      }
    )._registeredTools;
    // Как SDK: аргументы проходят схему инструмента (значения по умолчанию, проверка типов).
    const registered = tools[name]!;
    const args = registered.inputSchema ? registered.inputSchema.parse(argsFor(tool)) : argsFor(tool);
    const r = await registered.handler(args, {});
    expect(fake.violations, "запросы к полям, которых нет в схеме БП 3.0").toEqual([]);
    if (!EXPECTED_ERRORS[name]) expect(r.isError, r.content?.[0]?.text).not.toBe(true);

    const count = fake.requests.length;
    measured[name] = count;
    if (process.env.UPDATE_REQUEST_BUDGET !== "1" && budget[name] !== undefined)
      expect(
        count,
        `${name} делает ${count} запросов к 1С вместо ${budget[name]}. Если так задумано — обновите норму (см. шапку теста).`,
      ).toBeLessThanOrEqual(budget[name]!);
  });

  it("у каждого инструмента есть норма запросов", () => {
    if (process.env.UPDATE_REQUEST_BUDGET === "1") {
      writeFileSync(BUDGET_FILE, `${JSON.stringify(measured, Object.keys(measured).sort(), 2)}\n`);
      return;
    }
    expect(manifest.tools.map((t) => t.name).filter((n) => budget[n] === undefined)).toEqual([]);
  });
});

describe("самопроверка: поддельная 1С отвечает на ошибки так же, как настоящая", () => {
  const expectRejected = async (fake: ReturnType<typeof fake1C>, run: () => Promise<unknown>) => {
    await expect(run()).rejects.toThrow();
    expect(fake.violations.length).toBe(1);
    return fake.violations[0]!;
  };

  it("поле, которого нет у объекта, в $select / $filter", async () => {
    const fake = fake1C({ READ_ONLY: "true" });
    const v = await expectRejected(fake, () =>
      fake.conn.client.getCollection(
        "Document_ПоступлениеНаРасчетныйСчет?$format=json&$select=Контрагент_Key",
      ),
    );
    expect(v).toContain("Контрагент_Key");
    const f2 = fake1C({ READ_ONLY: "true" });
    await expectRejected(f2, () =>
      f2.conn.client.getCollection(
        `Catalog_Контрагенты?$format=json&$filter=${encodeURIComponent("Выдуманное eq 'x'")}`,
      ),
    );
  });

  it("запись поля, которого нет в документе или в строке табличной части", async () => {
    // Ошибка из первой версии PR #43: у банковских документов контрагент составной, Контрагент_Key нет.
    const fake = fake1C({ READ_ONLY: "false", ODATA_WRITABLE: "true" });
    const v = await expectRejected(fake, () =>
      fake.conn.client.request(`Document_ПоступлениеНаРасчетныйСчет(guid'${id}')?$format=json`, "PATCH", {
        Контрагент_Key: id,
      }),
    );
    expect(v).toContain("Контрагент_Key");
    const f2 = fake1C({ READ_ONLY: "false", ODATA_WRITABLE: "true" });
    await expectRejected(f2, () =>
      f2.conn.client.request(`Document_ПоступлениеНаРасчетныйСчет(guid'${id}')?$format=json`, "PATCH", {
        РасшифровкаПлатежа: [{ Выдуманное: 1 }],
      }),
    );
  });

  it("вид операции, которого нет в перечислениях", async () => {
    const fake = fake1C({ READ_ONLY: "false", ODATA_WRITABLE: "true" });
    const v = await expectRejected(fake, () =>
      fake.conn.client.request(`Document_ПоступлениеНаРасчетныйСчет(guid'${id}')?$format=json`, "PATCH", {
        ВидОперации: "ОплатаОтПокупателя",
      }),
    );
    expect(v).toContain("ОплатаОтПокупателя");
  });

  it("объект, который не опубликован", async () => {
    const fake = fake1C({ READ_ONLY: "true" });
    await expect(fake.conn.client.getCollection("Catalog_Выдуманный?$format=json")).rejects.toThrow(
      /не найден/,
    );
  });
});
