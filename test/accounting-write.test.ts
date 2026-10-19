import { describe, expect, it, vi } from "vitest";
import { parseConfig } from "../src/config/env.js";
import { createServer } from "../src/mcp/server.js";
import { Connection } from "../src/context.js";
const id = "11111111-1111-4111-8111-111111111111";
const line = { nomenclatureRef: id, quantity: 1, price: 1, vatRate: "БезНДС" };
function fixture(config: object, register = false) {
  const parsed = parseConfig({
    ODATA_BASE_URL: "https://example.invalid/odata/",
    ODATA_USERNAME: "u",
    ODATA_PASSWORD: "p",
    ODATA_ACCOUNTING_CONFIG: JSON.stringify(config),
  });
  const conn = new Connection(parsed.connections[0]!, parsed.behavior);
  const sets = [
    "ChartOfAccounts_Test",
    "Document_РеализацияТоваровУслуг",
    "Document_ПоступлениеНаРасчетныйСчет",
    "Catalog_Организации",
    ...(register ? ["InformationRegister_СчетаУчетаНоменклатуры"] : []),
  ];
  vi.spyOn(conn, "available").mockResolvedValue(new Set(sets));
  vi.spyOn(conn.client, "prepareCreate").mockResolvedValue(undefined as never);
  vi.spyOn(conn.client, "getEntity").mockResolvedValue({ Ref_Key: id, Description: "Synthetic" });
  const reads = vi.spyOn(conn.client, "getCollection").mockImplementation(async (path) => {
    if (path.startsWith("Catalog_Организации")) return { value: [{ Ref_Key: id, Description: "Synthetic" }] };
    if (path.startsWith("InformationRegister_"))
      return { value: [{ СчетДоходовОтРеализации_Key: id, СчетРасходовОтРеализации_Key: id }] };
    return {
      value: [
        { Ref_Key: id, Code: "62.1" },
        { Ref_Key: id, Code: "62.2" },
      ],
    };
  });
  const tools = (
    createServer({ db: () => conn } as never) as unknown as {
      _registeredTools: Record<
        string,
        {
          handler: (
            args: object,
            extra: object,
          ) => Promise<{ isError?: boolean; structuredContent?: { payload?: Record<string, unknown> } }>;
        }
      >;
    }
  )._registeredTools;
  const act = (args: object = {}) =>
    tools["write.sales.create_act"]!.handler(
      {
        organization: id,
        counterpartyRef: id,
        contractRef: id,
        lines: [line],
        sumIncludesVat: true,
        confirm: false,
        ...args,
      },
      {},
    );
  return { act, tools, reads };
}
const strict = { chart: "ChartOfAccounts_Test", strictDefaults: true, defaults: { receivable: ["62.1"] } };
describe("accounting creation flows", () => {
  it("does not require goods/VAT/overridden income or expense defaults for a service", async () => {
    const { act, reads } = fixture({ ...strict, operationKinds: { salesAct: "ПродажаКомиссия" } });
    const r = await act({ incomeAccount: id, expenseAccount: id });
    expect(r.isError, JSON.stringify(r)).not.toBe(true);
    expect(r.structuredContent?.payload).toMatchObject({
      ВидОперации: "ПродажаКомиссия",
      СчетУчетаРасчетовСКонтрагентом_Key: id,
    });
    expect(r.structuredContent?.payload?.Услуги).toMatchObject([
      { СчетДоходов_Key: id, СчетРасходов_Key: id },
    ]);
    expect(r.structuredContent?.payload?.Услуги).not.toMatchObject([{ СчетУчета_Key: id }]);
    expect(
      decodeURIComponent(reads.mock.calls.find(([path]) => path.startsWith("ChartOfAccounts_"))![0]),
    ).not.toContain("41");
  });
  it("uses register accounts before demanding fallback mappings", async () => {
    const { act } = fixture(strict, true);
    expect((await act()).isError).not.toBe(true);
  });
  it("rejects a missing actually needed mapping before preparing a write", async () => {
    const { act } = fixture(strict);
    expect((await act()).isError).toBe(true);
  });
  it("explicit operation kind wins over the base setting", async () => {
    const { act } = fixture({ ...strict, operationKinds: { salesAct: "Configured" } });
    expect(
      (await act({ operationKind: "Explicit", incomeAccount: id, expenseAccount: id })).structuredContent
        ?.payload?.ВидОперации,
    ).toBe("Explicit");
  });
  it("uses configured payment operation kind and settlement defaults", async () => {
    const { tools } = fixture({
      ...strict,
      defaults: { receivable: ["62.1"], receivableAdvance: ["62.2"] },
      operationKinds: { customerPayment: "ConfiguredPayment" },
    });
    const r = await tools["write.money.create_payment"]!.handler(
      {
        organization: id,
        counterpartyRef: id,
        contractRef: id,
        amount: 1,
        orgBankAccount: id,
        confirm: false,
      },
      {},
    );
    expect(r.isError, JSON.stringify(r)).not.toBe(true);
    expect(r.structuredContent?.payload?.ВидОперации).toBe("ConfiguredPayment");
    expect(r.structuredContent?.payload?.РасшифровкаПлатежа).toMatchObject([
      { СчетУчетаРасчетовСКонтрагентом_Key: id, СчетУчетаРасчетовПоАвансам_Key: id },
    ]);
  });
});
