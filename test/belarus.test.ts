import { describe, expect, it, vi } from "vitest";
import { parseConfig } from "../src/config/env.js";
import { createServer } from "../src/mcp/server.js";
import { accountsByCode, resolveAccounts } from "../src/odata/accounting.js";

const baseEnv = { ODATA_BASE_URL: "https://example.invalid/odata/", ODATA_USERNAME: "u", ODATA_PASSWORD: "p" };
const set = "Catalog_Контрагенты";
const key = "11111111-1111-4111-8111-111111111111";
const metadata = (field = "УНП") => ({ entities: new Map([[set, { properties: ["Ref_Key", "Description", field, "IsFolder"].map(name => ({ name, type: name === "IsFolder" ? "Edm.Boolean" : "Edm.String" })) }]]) });
function fixture(field = "УНП") {
  const collection = vi.fn(async () => ({ value: [] as Record<string, unknown>[] }));
  const conn = {
    cfg: { name: "by", writable: true, counterpartyTaxPolicy: "by-required-unique", counterpartyTaxField: field },
    behavior: { pageSize: 100, maxRows: 1000, writeOperationMarker: false },
    getMetadata: vi.fn(async () => metadata(field)), available: async () => new Set([set]),
    client: { getCollection: collection, getEntity: vi.fn(async () => ({ Ref_Key: key, [field]: "123456789" })), prepareCreate: vi.fn(), create: vi.fn(async () => ({ Ref_Key: key })), patch: vi.fn(), operationSettled: vi.fn() },
  };
  const tools = (createServer({ db: () => conn } as never) as unknown as { _registeredTools: Record<string, { handler: (args: Record<string, unknown>, extra: object) => Promise<{ isError?: boolean; structuredContent?: Record<string, unknown> }> }> })._registeredTools;
  return { conn, collection, tools };
}

describe("Belarus counterparty policy", () => {
  it("parses per-database opt-in without changing legacy defaults", () => {
    const cfg = parseConfig({ ...baseEnv, ODATA_COUNTERPARTY_TAX_POLICY: "by-required-unique", ODATA_COUNTERPARTY_TAX_FIELD: "УНП" });
    expect(cfg.connections[0]).toMatchObject({ counterpartyTaxPolicy: "by-required-unique", counterpartyTaxField: "УНП" });
    expect(parseConfig(baseEnv).connections[0]).toMatchObject({ counterpartyTaxPolicy: "ru" });
    expect(() => parseConfig({ ...baseEnv, ODATA_COUNTERPARTY_TAX_POLICY: "unknown" })).toThrow();
  });
  it.each([undefined, "", " ", "000000000", "unknown", "12-345", " 123456789 "])("rejects missing, placeholder or noncanonical UNP %s before preview", async (inn) => {
    const { conn, tools } = fixture();
    const result = await tools["write.counterparty.create_counterparty"]!.handler({ name: "Synthetic", inn, confirm: false }, {});
    expect(result.isError).toBe(true);
    expect(conn.client.prepareCreate).not.toHaveBeenCalled();
    expect(conn.client.create).not.toHaveBeenCalled();
  });
  it("maps an alphanumeric personal UNP to the metadata field and checks with one bounded request", async () => {
    const { collection, tools } = fixture();
    const result = await tools["write.counterparty.create_counterparty"]!.handler({ name: "Synthetic", inn: "AB1234567", confirm: false }, {});
    expect(result.isError).not.toBe(true);
    expect(result.structuredContent?.payload).toMatchObject({ УНП: "AB1234567" });
    expect(result.structuredContent?.payload).not.toHaveProperty("ИНН");
    expect(collection).toHaveBeenCalledOnce();
    const url = decodeURIComponent(collection.mock.calls[0]![0] as string);
    expect(url).toContain("$top=2");
    expect(url).toContain("УНП eq 'AB1234567'");
    expect(url).not.toContain("DeletionMark");
  });
  it("fresh confirmation stops when a duplicate appears after preview, including deletion-marked cards", async () => {
    const { conn, collection, tools } = fixture();
    const args = { name: "Synthetic", inn: "123456789", confirm: false };
    const preview = await tools["write.counterparty.create_counterparty"]!.handler(args, {});
    collection.mockResolvedValue({ value: [{ Ref_Key: key, УНП: "123456789", DeletionMark: true }] });
    const result = await tools["write.counterparty.create_counterparty"]!.handler({ ...args, confirm: true, operationId: preview.structuredContent?.operationId }, {});
    expect(result.isError).toBe(true);
    expect(conn.client.create).not.toHaveBeenCalled();
  });
  it.each([{ value: [], "@odata.nextLink": "next" }, { value: [{ Ref_Key: key }] }, { value: [{ Ref_Key: key, УНП: "987654321" }] }, {}])("fails closed on incomplete or ignored uniqueness response", async (response) => {
    const { conn, collection, tools } = fixture();
    collection.mockResolvedValue(response as never);
    const result = await tools["write.counterparty.create_counterparty"]!.handler({ name: "Synthetic", inn: "123456789", confirm: false }, {});
    expect(result.isError).toBe(true);
    expect(conn.client.prepareCreate).not.toHaveBeenCalled();
  });
  it("does not let generic updates clear UNP and validates unchanged identity on name changes", async () => {
    const { conn, collection, tools } = fixture();
    const generic = tools["write.entity.update_entity"]!;
    expect((await generic.handler({ entitySet: set, ref: key, fields: { УНП: "" }, confirm: false }, {})).isError).toBe(true);
    expect((await generic.handler({ entitySet: set, ref: key, fields: { Description: "Renamed" }, confirm: false }, {})).isError).not.toBe(true);
    expect(conn.client.getEntity).toHaveBeenCalled();
    expect(collection).toHaveBeenCalledOnce();
  });
});

describe("configurable accounting", () => {
  it("resolves configured chart entity using one batched exact-code request", async () => {
    const getCollection = vi.fn(async () => ({ value: [{ Code: "41.2", Ref_Key: key }] }));
    const conn = { cfg: { accounting: { chart: "ChartOfAccounts_Test" } }, available: async () => new Set(["ChartOfAccounts_Test"]), client: { getCollection }, behavior: { pageSize: 100, maxRows: 1000 } };
    expect(await accountsByCode(conn as never, ["41.2", "41.2"])).toEqual(new Map([["41.2", key]]));
    expect(getCollection).toHaveBeenCalledOnce();
    expect(getCollection.mock.calls[0]![0]).toContain("ChartOfAccounts_Test?");
    await resolveAccounts(conn as never, ["41"]);
  });
  it("accepts explicit chart/register/prefix/default mappings and rejects malformed JSON", () => {
    const accounting = { chart: "ChartOfAccounts_Test", register: "AccountingRegister_Test", prefixes: { receivables: ["62"] }, defaults: { income: ["90.1"] } };
    expect(parseConfig({ ...baseEnv, ODATA_ACCOUNTING_CONFIG: JSON.stringify(accounting) }).connections[0]).toMatchObject({ accounting });
    expect(() => parseConfig({ ...baseEnv, ODATA_ACCOUNTING_CONFIG: "{" })).toThrow();
  });
});
