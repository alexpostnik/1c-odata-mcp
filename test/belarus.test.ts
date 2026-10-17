import { afterEach, describe, expect, it, vi } from "vitest";
import { parseConfig } from "../src/config/env.js";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Connection } from "../src/context.js";
import { createServer } from "../src/mcp/server.js";
import { accountPrefixes, accountingRegister, defaultAccountCodes } from "../src/config/accounting.js";
import { counterpartyTaxField } from "../src/odata/counterparty-policy.js";
import { accountsByCode, resolveAccounts, resolveDefaultAccounts } from "../src/odata/accounting.js";

const baseEnv = {
  ODATA_BASE_URL: "https://example.invalid/odata/",
  ODATA_USERNAME: "u",
  ODATA_PASSWORD: "p",
};
const set = "Catalog_Контрагенты";
const dirs: string[] = [];
afterEach(() => {
  vi.unstubAllGlobals();
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});
const key = "11111111-1111-4111-8111-111111111111";
const metadata = (field = "УНП") => ({
  entities: new Map([
    [
      set,
      {
        properties: ["Ref_Key", "Description", field, "IsFolder"].map((name) => ({
          name,
          type: name === "IsFolder" ? "Edm.Boolean" : "Edm.String",
        })),
      },
    ],
  ]),
});
function fixture(field = "УНП") {
  const collection = vi.fn(async (_path: string) => ({ value: [] as Record<string, unknown>[] }));
  const dir = mkdtempSync(join(tmpdir(), "unp-policy-"));
  dirs.push(dir);
  const parsed = parseConfig({
    ...baseEnv,
    ODATA_WRITABLE: "true",
    READ_ONLY: "false",
    ODATA_WRITE_JOURNAL_DIR: dir,
    ODATA_COUNTERPARTY_TAX_POLICY: "by-required-unique",
    ODATA_COUNTERPARTY_TAX_FIELD: field,
  });
  // Distinct synthetic URL isolates pending outcome claims across test fixtures.
  const conn = new Connection(
    {
      ...parsed.connections[0]!,
      baseUrl: `https://example.invalid/${dirs.length}-${dir.split("/").at(-1)}/`,
    },
    parsed.behavior,
  );
  vi.spyOn(conn.client, "getMetadata").mockResolvedValue(metadata(field) as never);
  vi.spyOn(conn.client, "getCollection").mockImplementation(collection as never);
  vi.spyOn(conn.client, "getEntity").mockResolvedValue({ Ref_Key: key, [field]: "123456789" });
  vi.spyOn(conn.client, "prepareCreate");
  vi.spyOn(conn.client, "create");
  vi.spyOn(conn.client, "patch");
  const post = vi.fn(async (_url: string | URL | Request, options?: RequestInit) => {
    const body = typeof options?.body === "string" ? JSON.parse(options.body) : {};
    return new Response(JSON.stringify({ Ref_Key: body.Ref_Key ?? key }), { status: 201 });
  });
  vi.stubGlobal("fetch", post);
  const tools = (
    createServer({ db: () => conn } as never) as unknown as {
      _registeredTools: Record<
        string,
        {
          handler: (
            args: Record<string, unknown>,
            extra: object,
          ) => Promise<{ isError?: boolean; structuredContent?: Record<string, unknown> }>;
        }
      >;
    }
  )._registeredTools;
  return { conn, collection, tools, post };
}

describe("Belarus counterparty policy", () => {
  it("parses per-database opt-in without changing legacy defaults", () => {
    const cfg = parseConfig({
      ...baseEnv,
      ODATA_COUNTERPARTY_TAX_POLICY: "by-required-unique",
      ODATA_COUNTERPARTY_TAX_FIELD: "УНП",
    });
    expect(cfg.connections[0]).toMatchObject({
      counterpartyTaxPolicy: "by-required-unique",
      counterpartyTaxField: "УНП",
    });
    expect(parseConfig(baseEnv).connections[0]).toMatchObject({ counterpartyTaxPolicy: "ru" });
    expect(() => parseConfig({ ...baseEnv, ODATA_COUNTERPARTY_TAX_POLICY: "unknown" })).toThrow();
  });
  it.each([undefined, "", " ", "000000000", "unknown", "12-345", " 123456789 "])(
    "rejects missing, placeholder or noncanonical UNP %s before preview",
    async (inn) => {
      const { conn, tools } = fixture();
      const result = await tools["write.counterparty.create_counterparty"]!.handler(
        { name: "Synthetic", inn, confirm: false },
        {},
      );
      expect(result.isError).toBe(true);
      expect(conn.client.prepareCreate).not.toHaveBeenCalled();
      expect(conn.client.create).not.toHaveBeenCalled();
    },
  );
  it("maps an alphanumeric personal UNP to the metadata field and checks with one bounded request", async () => {
    const { collection, tools } = fixture();
    const result = await tools["write.counterparty.create_counterparty"]!.handler(
      { name: "Synthetic", inn: "AB1234567", confirm: false },
      {},
    );
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
    const { conn, collection, tools, post } = fixture();
    const args = { name: "Synthetic", inn: "123456789", confirm: false };
    const preview = await tools["write.counterparty.create_counterparty"]!.handler(args, {});
    collection.mockResolvedValue({ value: [{ Ref_Key: key, УНП: "123456789", DeletionMark: true }] });
    const result = await tools["write.counterparty.create_counterparty"]!.handler(
      { ...args, confirm: true, operationId: preview.structuredContent?.operationId },
      {},
    );
    expect(result.isError).toBe(true);
    expect(conn.client.create).toHaveBeenCalledOnce();
    expect(post).not.toHaveBeenCalled();
  });
  it("records a pre-POST duplicate refusal as rejected, preserving the reason", async () => {
    const { conn, collection, tools, post } = fixture();
    const tool = tools["write.counterparty.create_counterparty"]!;
    const args = { name: "Synthetic", inn: "123456789", confirm: false };
    const preview = await tool.handler(args, {});
    collection.mockResolvedValue({ value: [{ Ref_Key: key, УНП: "123456789" }] });
    const result = await tool.handler(
      { ...args, confirm: true, operationId: preview.structuredContent?.operationId },
      {},
    );
    expect(result.isError).toBe(true);
    expect(await conn.client.operationEntry(preview.structuredContent?.operationId as string)).toMatchObject({
      state: "rejected",
    });
    expect(post).not.toHaveBeenCalled();
  });
  it("permits self identity during update and rejects duplicate identity before PATCH", async () => {
    const { conn, collection, tools, post } = fixture();
    const tool = tools["write.entity.update_entity"]!;
    const args = { entitySet: set, ref: key, fields: { Description: "Renamed" }, confirm: false };
    collection.mockResolvedValue({ value: [{ Ref_Key: key.toUpperCase(), УНП: "123456789" }] });
    expect((await tool.handler(args, {})).isError).not.toBe(true);
    collection.mockResolvedValue({
      value: [{ Ref_Key: "22222222-2222-4222-8222-222222222222", УНП: "123456789" }],
    });
    expect((await tool.handler({ ...args, confirm: true }, {})).isError).toBe(true);
    expect(conn.client.patch).toHaveBeenCalledOnce();
    expect(post).not.toHaveBeenCalled();
  });
  it("exempts structural folders while refusing a card-to-folder policy bypass", async () => {
    const { conn, collection, tools, post } = fixture();
    // Exercise the transport directly: folders are the sole UNP exemption.
    await conn.client.request(`${set}?$format=json`, "POST", { Description: "Group", IsFolder: true });
    expect(conn.client.getCollection).not.toHaveBeenCalled();
    post.mockClear();
    const result = await tools["write.entity.update_entity"]!.handler(
      { entitySet: set, ref: key, fields: { IsFolder: true }, confirm: false },
      {},
    );
    expect(result.isError).toBe(true);
    expect(collection).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });
  it.each([null, "true", 1])("rejects a nonboolean folder flag %s", async (IsFolder) => {
    const { tools, post } = fixture();
    const result = await tools["write.entity.update_entity"]!.handler(
      { entitySet: set, ref: key, fields: { IsFolder }, confirm: false },
      {},
    );
    expect(result.isError).toBe(true);
    expect(post).not.toHaveBeenCalled();
  });
  it("does not include upstream error text or identifiers in uniqueness diagnostics", async () => {
    const { collection, tools } = fixture();
    collection.mockRejectedValue(new Error("Synthetic private backend detail"));
    const result = await tools["write.counterparty.create_counterparty"]!.handler(
      { name: "Synthetic", inn: "123456789", confirm: false },
      {},
    );
    expect(result.isError).toBe(true);
    expect(JSON.stringify(result)).not.toContain("Synthetic private backend detail");
    expect(JSON.stringify(result)).not.toContain("123456789");
  });
  it("maps read-side UNP search without selecting unpublished Russian-only fields", async () => {
    const { collection, tools } = fixture();
    collection.mockResolvedValue({ value: [{ Ref_Key: key, Description: "Synthetic", УНП: "AB1234567" }] });
    const result = await tools["read.counterparty.find_counterparty"]!.handler(
      { query: "AB1234567", limit: 20 },
      {},
    );
    expect(result.structuredContent?.rows).toMatchObject([{ inn: "AB1234567" }]);
    const url = decodeURIComponent(collection.mock.calls[0]![0]);
    expect(url).toContain("УНП eq 'AB1234567'");
    expect(url).not.toContain("КПП");
    expect(url).not.toContain("ИНН");
  });
  it("requires explicit mapping for ambiguous or incompatible tax metadata", async () => {
    const { conn } = fixture();
    conn.cfg.counterpartyTaxField = undefined;
    vi.mocked(conn.client.getMetadata).mockResolvedValue({
      entities: new Map([
        [
          set,
          {
            properties: [
              { name: "УНП", type: "Edm.String" },
              { name: "ИНН", type: "Edm.String" },
            ],
          },
        ],
      ]),
    } as never);
    await expect(counterpartyTaxField(conn)).rejects.toThrow("one published");
    vi.mocked(conn.client.getMetadata).mockResolvedValue({
      entities: new Map([[set, { properties: [{ name: "УНП", type: "Edm.Int64" }] }]]),
    } as never);
    await expect(counterpartyTaxField(conn)).rejects.toThrow("Edm.String");
    vi.mocked(conn.client.getMetadata).mockResolvedValue({
      entities: new Map([[set, { properties: [{ name: "ИНН", type: "Edm.String" }] }]]),
    } as never);
    await expect(counterpartyTaxField(conn)).resolves.toBe("ИНН");
  });
  it("replays a successful confirmation without another uniqueness read or POST", async () => {
    const { collection, tools, post } = fixture();
    const tool = tools["write.counterparty.create_counterparty"]!;
    const args = { name: "Synthetic", inn: "123456789", confirm: false };
    const preview = await tool.handler(args, {});
    const confirmedArgs = { ...args, confirm: true, operationId: preview.structuredContent?.operationId };
    expect((await tool.handler(confirmedArgs, {})).isError).not.toBe(true);
    expect(collection).toHaveBeenCalledTimes(2); // one preview + one immediately before POST
    expect((await tool.handler(confirmedArgs, {})).structuredContent).toMatchObject({ replayed: true });
    expect(collection).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenCalledOnce();
  });
  it("serializes distinct confirmed creates of the same UNP in one process", async () => {
    const { collection, tools, post } = fixture();
    const tool = tools["write.counterparty.create_counterparty"]!;
    const args = { name: "Synthetic", inn: "123456789", confirm: false };
    const a = await tool.handler(args, {});
    const b = await tool.handler(args, {});
    let created = false;
    collection.mockImplementation(async () => ({
      value: created ? [{ Ref_Key: key, УНП: "123456789" }] : [],
    }));
    post.mockImplementation(async (_url, options) => {
      await Promise.resolve();
      created = true;
      const body = typeof options?.body === "string" ? JSON.parse(options.body) : {};
      return new Response(JSON.stringify({ Ref_Key: body.Ref_Key }), { status: 201 });
    });
    const results = await Promise.all(
      [a, b].map((preview) =>
        tool.handler({ ...args, confirm: true, operationId: preview.structuredContent?.operationId }, {}),
      ),
    );
    expect(results.filter((result) => result.isError)).toHaveLength(1);
    expect(post).toHaveBeenCalledOnce();
  });
  it("retains an uncertain create claim and refuses a different operation until reconciliation", async () => {
    const { tools, post } = fixture();
    const tool = tools["write.counterparty.create_counterparty"]!;
    const args = { name: "Synthetic", inn: "123456789", confirm: false };
    const a = await tool.handler(args, {});
    const b = await tool.handler(args, {});
    post.mockRejectedValue(new TypeError("Synthetic lost response"));
    expect(
      (await tool.handler({ ...args, confirm: true, operationId: a.structuredContent?.operationId }, {}))
        .isError,
    ).toBe(true);
    expect(
      (await tool.handler({ ...args, confirm: true, operationId: b.structuredContent?.operationId }, {}))
        .isError,
    ).toBe(true);
    expect(post).toHaveBeenCalledOnce();
  });
  it.each([
    "",
    "{}",
    "[]",
    "null",
    JSON.stringify({ Ref_Key: "not-a-guid" }),
    JSON.stringify({ Ref_Key: key }),
  ])("retains an uncertain CREATE claim after an invalid semantic 201 response %s", async (responseBody) => {
    const { conn, tools, post } = fixture();
    const tool = tools["write.counterparty.create_counterparty"]!;
    const args = { name: "Synthetic", inn: "123456789", confirm: false };
    const a = await tool.handler(args, {});
    const b = await tool.handler(args, {});
    post.mockImplementation(async () => new Response(responseBody, { status: 201 }));
    const first = await tool.handler(
      { ...args, confirm: true, operationId: a.structuredContent?.operationId },
      {},
    );
    const second = await tool.handler(
      { ...args, confirm: true, operationId: b.structuredContent?.operationId },
      {},
    );
    expect(first.isError).toBe(true);
    expect(await conn.client.operationEntry(a.structuredContent?.operationId as string)).toMatchObject({
      state: "outcome_unknown",
    });
    expect(second.isError).toBe(true);
    expect(post).toHaveBeenCalledOnce();
  });
  it.each([{ Description: "Renamed", УНП: "123456789" }, { Description: "Renamed" }])(
    "PATCH %j cannot race or release a different uncertain CREATE's same-UNP claim",
    async (patchFields) => {
      const { conn, tools, post } = fixture();
      const tool = tools["write.counterparty.create_counterparty"]!;
      const args = { name: "Synthetic", inn: "123456789", confirm: false };
      const a = await tool.handler(args, {});
      const b = await tool.handler(args, {});
      let postCount = 0;
      post.mockImplementation(async (_url, options) => {
        if (options?.method === "POST") {
          postCount++;
          throw new TypeError("Synthetic lost response");
        }
        return new Response(JSON.stringify({ Ref_Key: key }), { status: 200 });
      });
      expect(
        (await tool.handler({ ...args, confirm: true, operationId: a.structuredContent?.operationId }, {}))
          .isError,
      ).toBe(true);
      const patch = conn.client.patch(`${set}(guid'${key}')`, { Description: "Renamed", УНП: "123456789" });
      await expect(patch).rejects.toThrow("unknown outcome");
      expect(post).toHaveBeenCalledOnce(); // denied before PATCH transport
      expect(
        (await tool.handler({ ...args, confirm: true, operationId: b.structuredContent?.operationId }, {}))
          .isError,
      ).toBe(true);
      expect(postCount).toBe(1);
    },
  );
  it.each([
    { value: [], "@odata.nextLink": "next" },
    { value: [{ Ref_Key: key }] },
    { value: [{ Ref_Key: key, УНП: "987654321" }] },
    {},
  ])("fails closed on incomplete or ignored uniqueness response", async (response) => {
    const { conn, collection, tools } = fixture();
    collection.mockResolvedValue(response as never);
    const result = await tools["write.counterparty.create_counterparty"]!.handler(
      { name: "Synthetic", inn: "123456789", confirm: false },
      {},
    );
    expect(result.isError).toBe(true);
    expect(conn.client.prepareCreate).not.toHaveBeenCalled();
  });
  it("does not let generic updates clear UNP and validates unchanged identity on name changes", async () => {
    const { conn, collection, tools } = fixture();
    const generic = tools["write.entity.update_entity"]!;
    expect(
      (await generic.handler({ entitySet: set, ref: key, fields: { УНП: "" }, confirm: false }, {})).isError,
    ).toBe(true);
    expect(
      (
        await generic.handler(
          { entitySet: set, ref: key, fields: { Description: "Renamed" }, confirm: false },
          {},
        )
      ).isError,
    ).not.toBe(true);
    expect(conn.client.getEntity).toHaveBeenCalled();
    expect(collection).toHaveBeenCalledOnce();
  });
});

describe("configurable accounting", () => {
  it("resolves configured chart entity using one batched exact-code request", async () => {
    const getCollection = vi.fn(async (_path: string) => ({ value: [{ Code: "41.2", Ref_Key: key }] }));
    const conn = {
      cfg: { accounting: { chart: "ChartOfAccounts_Test" } },
      available: async () => new Set(["ChartOfAccounts_Test"]),
      client: { getCollection },
      behavior: { pageSize: 100, maxRows: 1000 },
    };
    expect(await accountsByCode(conn as never, ["41.2", "41.2"])).toEqual(new Map([["41.2", key]]));
    expect(getCollection).toHaveBeenCalledOnce();
    expect(getCollection.mock.calls[0]![0]).toContain("ChartOfAccounts_Test?");
    await resolveAccounts(conn as never, ["41"]);
  });
  it("fails closed when configured Belarus fallback accounts do not exist", async () => {
    const cfg = parseConfig({
      ...baseEnv,
      ODATA_COUNTERPARTY_TAX_POLICY: "by-required-unique",
      ODATA_ACCOUNTING_CONFIG: JSON.stringify({ defaults: { income: ["90.7"] } }),
    }).connections[0]!;
    const conn = {
      cfg,
      available: async () => new Set(["ChartOfAccounts_Хозрасчетный"]),
      client: { getCollection: async () => ({ value: [] }) },
    };
    await expect(resolveDefaultAccounts(conn as never, ["income"])).rejects.toThrow("income");
  });
  it("uses configured accounting groups and refuses missing Belarus defaults", () => {
    const cfg = parseConfig({
      ...baseEnv,
      ODATA_COUNTERPARTY_TAX_POLICY: "by-required-unique",
      ODATA_ACCOUNTING_CONFIG: JSON.stringify({
        register: "AccountingRegister_Test",
        prefixes: { receivables: ["62.7"] },
        defaults: { goods: ["41.7"] },
      }),
    }).connections[0]!;
    expect(accountingRegister(cfg)).toEqual(["AccountingRegister_Test"]);
    expect(accountPrefixes(cfg, "receivables")).toEqual(["62.7"]);
    expect(defaultAccountCodes(cfg, "goods")).toEqual(["41.7"]);
    expect(() => accountPrefixes(cfg, "inventory")).toThrow("Configure");
    expect(() => defaultAccountCodes(cfg, "income")).toThrow("Russian account defaults");
    const legacy = parseConfig(baseEnv).connections[0]!;
    expect(accountPrefixes(legacy, "inventory")).toEqual(["41", "10", "43"]);
    expect(defaultAccountCodes(legacy, "income")).toEqual(["90.01.1", "90.01"]);
  });
  it("allows per-database policy and accounting overrides of global configuration", () => {
    const cfg = parseConfig({
      ODATA_DB_A_BASE_URL: baseEnv.ODATA_BASE_URL,
      ODATA_DB_A_USERNAME: "u",
      ODATA_DB_A_PASSWORD: "p",
      ODATA_DB_B_BASE_URL: baseEnv.ODATA_BASE_URL,
      ODATA_DB_B_USERNAME: "u",
      ODATA_DB_B_PASSWORD: "p",
      ODATA_COUNTERPARTY_TAX_POLICY: "by-required-unique",
      ODATA_DB_B_COUNTERPARTY_TAX_POLICY: "ru",
      ODATA_ACCOUNTING_CONFIG: JSON.stringify({ chart: "ChartOfAccounts_Global" }),
      ODATA_DB_B_ACCOUNTING_CONFIG: JSON.stringify({ chart: "ChartOfAccounts_Local" }),
    });
    expect(cfg.connections.find((c) => c.name === "a")).toMatchObject({
      counterpartyTaxPolicy: "by-required-unique",
      accounting: { chart: "ChartOfAccounts_Global" },
    });
    expect(cfg.connections.find((c) => c.name === "b")).toMatchObject({
      counterpartyTaxPolicy: "ru",
      accounting: { chart: "ChartOfAccounts_Local" },
    });
  });
  it("accepts explicit chart/register/prefix/default mappings and rejects malformed JSON", () => {
    const accounting = {
      chart: "ChartOfAccounts_Test",
      register: "AccountingRegister_Test",
      prefixes: { receivables: ["62"] },
      defaults: { income: ["90.1"] },
    };
    expect(
      parseConfig({ ...baseEnv, ODATA_ACCOUNTING_CONFIG: JSON.stringify(accounting) }).connections[0],
    ).toMatchObject({ accounting });
    expect(() => parseConfig({ ...baseEnv, ODATA_ACCOUNTING_CONFIG: "{" })).toThrow();
  });
});

describe("shared metadata cache", () => {
  it("uses one metadata request across tool and transport consumers, and retries failed loads", async () => {
    const cfg = parseConfig(baseEnv);
    const conn = new Connection(cfg.connections[0]!, cfg.behavior);
    const xml = `<Edmx><DataServices><Schema><EntityType Name="Counterparties"><Property Name="УНП" Type="Edm.String"/></EntityType><EntityContainer><EntitySet Name="Catalog_Контрагенты" EntityType="StandardODATA.Counterparties"/></EntityContainer></Schema></DataServices></Edmx>`;
    const getText = vi
      .spyOn(conn.client, "getText")
      .mockRejectedValueOnce(new Error("Synthetic unavailable metadata"))
      .mockResolvedValue(xml);
    await expect(conn.getMetadata()).rejects.toThrow("unavailable");
    const [toolMetadata, transportMetadata] = await Promise.all([
      conn.getMetadata(),
      conn.client.getMetadata(),
    ]);
    expect(toolMetadata).toBe(transportMetadata);
    expect(toolMetadata.entities.has(set)).toBe(true);
    expect(getText).toHaveBeenCalledTimes(2); // one failed attempt + one shared successful load
  });
});
