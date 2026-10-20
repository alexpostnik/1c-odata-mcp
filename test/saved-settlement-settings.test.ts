import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Connection } from "../src/context.js";
import { parseConfig } from "../src/config/env.js";
import { createServer } from "../src/mcp/server.js";
import { ODataError } from "../src/odata/errors.js";
import type { EntityMeta, ODataEntity } from "../src/types/odata.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

const ORG = "11111111-1111-4111-8111-111111111111";
const CP = "22222222-2222-4222-8222-222222222222";
const CTR = "33333333-3333-4333-8333-333333333333";
const ACCOUNT = "44444444-4444-4444-8444-444444444444";
const OTHER = "55555555-5555-4555-8555-555555555555";
const CURRENCY = "66666666-6666-4666-8666-666666666666";
const EMPTY = "00000000-0000-0000-0000-000000000000";
const REG = "InformationRegister_СчетаУчетаРасчетовСКонтрагентами";
const KIND = "РасчетыВВалютеРегламентированногоУчета";
const key = { Организация_Key: ORG, Контрагент_Key: CP, Договор_Key: CTR, ВидРасчетовПоДоговору: KIND };
const dimensions = Object.keys(key);
const resources = [
  "СчетУчетаРасчетовСПокупателем_Key",
  "СчетУчетаАвансовПолученных_Key",
  "СчетУчетаРасчетовСПоставщиком_Key",
  "СчетУчетаАвансовВыданных_Key",
];
const dirs: string[] = [];
afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all(dirs.splice(0).map((p) => rm(p, { recursive: true, force: true })));
});

async function fixture(existing?: ODataEntity, options: { readOnly?: boolean; writable?: boolean } = {}) {
  const dir = await mkdtemp(join(tmpdir(), "settings-test-"));
  dirs.push(dir);
  const cfg = parseConfig({
    ODATA_BASE_URL: "https://example.invalid/odata/",
    ODATA_USERNAME: "synthetic",
    ODATA_PASSWORD: "synthetic",
    READ_ONLY: String(options.readOnly ?? false),
    ODATA_WRITABLE: String(options.writable ?? true),
    WRITE_JOURNAL_DIR: dir,
    ODATA_REGULATED_CURRENCY_CODE: "933",
  });
  const conn = new Connection(
    { ...cfg.connections[0]!, writable: options.writable ?? true },
    { ...cfg.behavior, regulatedCurrencyCode: "933", writeJournalDir: dir },
  );
  const props = [...dimensions, ...resources, "DataVersion", "СчетПрочихРасчетов_Key"].map((name) => ({
    name,
    type: name === "ВидРасчетовПоДоговору" || name === "DataVersion" ? "Edm.String" : "Edm.Guid",
    nullable: true,
  }));
  const meta: EntityMeta = {
    entitySet: REG,
    entityType: REG,
    class: "informationRegister",
    shortName: "СчетаУчетаРасчетовСКонтрагентами",
    keys: [...dimensions],
    properties: props,
    navigations: [],
  };
  const entities = new Map([
    [REG, meta],
    ...["Catalog_ДоговорыКонтрагентов", "Catalog_Валюты", "ChartOfAccounts_Хозрасчетный"].map(
      (name) => [name, { ...meta, entitySet: name }] as const,
    ),
  ]);
  vi.spyOn(conn, "getMetadata").mockImplementation(async () => ({ entities, odataVersion: "3.0" }));
  let row = existing ? { ...existing } : undefined;
  let contract = {
    Ref_Key: CTR,
    Owner_Key: CP,
    Организация_Key: ORG,
    ВидДоговора: "Прочее",
    ВалютаВзаиморасчетов_Key: CURRENCY,
    РасчетыВУсловныхЕдиницах: false,
  };
  let corruptReadback = false;
  const writes: Array<{ path: string; method: string; body: unknown; headers: unknown }> = [];
  vi.spyOn(conn.client, "request").mockImplementation(
    async (path: string, method = "GET", body?: unknown, headers?: unknown) => {
      if (method !== "GET") {
        writes.push({ path, method, body, headers });
        row = { ...row, ...(body as object), DataVersion: "v2" };
        if (corruptReadback) row[resources[2]!] = OTHER;
        return {} as never;
      }
      if (path.startsWith("Catalog_Договоры")) return contract as never;
      if (path.startsWith("Catalog_Валюты")) return { Code: "933" } as never;
      if (path.startsWith("ChartOfAccounts_"))
        return { value: [{ Ref_Key: ACCOUNT, Code: "66.2", Description: "Synthetic" }] } as never;
      if (path.startsWith(REG)) {
        if (!row) throw new ODataError({ kind: "not_found", message: "Absent" });
        return { ...row } as never;
      }
      throw new Error(`Unexpected synthetic path ${path}`);
    },
  );
  const tools = (
    createServer({ db: () => conn } as never) as unknown as {
      _registeredTools: Record<string, { handler: (args: object, extra: object) => Promise<CallToolResult> }>;
    }
  )._registeredTools;
  const run = async (args: object = {}, read = false) => {
    const tool =
      tools[
        read
          ? "read.accounting.get_contract_settlement_settings"
          : "write.accounting.set_contract_settlement_settings"
      ];
    expect(tool, "dedicated exact saved-settings tool must be registered").toBeDefined();
    return tool!.handler(
      {
        database: "default",
        organizationRef: ORG,
        counterpartyRef: CP,
        contractRef: CTR,
        settlementKind: KIND,
        mode: "create",
        accounts: { supplierSettlement: ACCOUNT },
        confirm: false,
        ...args,
      },
      {},
    );
  };
  return {
    run,
    conn,
    meta,
    entities,
    writes,
    setRow: (r: ODataEntity | undefined) => {
      row = r;
    },
    setContract: (r: Partial<typeof contract>) => {
      contract = { ...contract, ...r };
    },
    corrupt: () => {
      corruptReadback = true;
    },
  };
}
const data = (r: CallToolResult) => r.structuredContent as Record<string, unknown>;
const saved = () => ({
  ...key,
  DataVersion: "v1",
  [resources[0]!]: OTHER,
  [resources[2]!]: OTHER,
  СчетПрочихРасчетов_Key: OTHER,
});

describe("exact contract saved settlement settings", () => {
  it("reads exact compound-key row without inherited defaults", async () => {
    const f = await fixture(saved());
    const r = await f.run({}, true);
    expect(r.isError).toBeFalsy();
    expect(data(r).found).toBe(true);
    expect(data(r).record).toMatchObject(saved());
    expect(f.writes).toEqual([]);
  });
  it("previews and creates once, using all dimensions and no Ref_Key; confirms by readback", async () => {
    const f = await fixture();
    const preview = await f.run();
    expect(preview.isError).toBeFalsy();
    expect(data(preview)).toMatchObject({ dryRun: true, method: "POST" });
    expect(f.writes).toEqual([]);
    const operationId = data(preview).operationId;
    const result = await f.run({ confirm: true, operationId });
    expect(result.isError).toBeFalsy();
    expect(data(result).verified).toBe(true);
    expect(f.writes).toEqual([
      {
        path: `${REG}?$format=json`,
        method: "POST",
        body: { ...key, [resources[2]!]: ACCOUNT },
        headers: {},
      },
    ]);
    const replay = await f.run({ confirm: true, operationId });
    expect(data(replay).replayed).toBe(true);
    expect(f.writes).toHaveLength(1);
  });
  it("updates only explicit resources with exact compound path and If-Match, preserving other resources", async () => {
    const f = await fixture(saved());
    const args = { mode: "update", expectedDataVersion: "v1" };
    const p = await f.run(args);
    const r = await f.run({ ...args, confirm: true, operationId: data(p).operationId });
    expect(r.isError).toBeFalsy();
    expect(f.writes).toHaveLength(1);
    expect(f.writes[0]).toMatchObject({
      method: "PATCH",
      body: { [resources[2]!]: ACCOUNT },
      headers: { "If-Match": "v1" },
    });
    for (const name of dimensions) expect(f.writes[0]!.path).toContain(`${name}=`);
    expect(data(r).record).toMatchObject({ [resources[0]!]: OTHER, СчетПрочихРасчетов_Key: OTHER });
  });
  it.each(["RecordSet", "Recorder_Key", "Period"])(
    "blocks unsupported schema %s before any write",
    async (field) => {
      const f = await fixture();
      f.meta.properties.push({ name: field, type: "Edm.String", nullable: true });
      const r = await f.run();
      expect(r.isError).toBe(true);
      expect(f.writes).toEqual([]);
    },
  );
  it("blocks missing register and extra compound key instead of broad writes", async () => {
    const f = await fixture();
    f.entities.delete(REG);
    expect((await f.run()).isError).toBe(true);
    const g = await fixture();
    g.meta.keys.push("Unknown");
    expect((await g.run()).isError).toBe(true);
    expect(g.writes).toEqual([]);
  });
  it.each([
    { organizationRef: EMPTY },
    { counterpartyRef: EMPTY },
    { contractRef: EMPTY },
    { accounts: {} },
    { accounts: { arbitrary: ACCOUNT } },
  ])("rejects wildcard or unknown resource arguments %j", async (args) => {
    const f = await fixture();
    expect((await f.run(args)).isError).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it.each([{ Owner_Key: OTHER }, { Организация_Key: OTHER }])(
    "rejects contract scope mismatch %j",
    async (contract) => {
      const f = await fixture();
      f.setContract(contract);
      expect((await f.run()).isError).toBe(true);
      expect(f.writes).toEqual([]);
    },
  );
  it("blocks create over existing row and update without an exact current version", async () => {
    const f = await fixture(saved());
    expect((await f.run()).isError).toBe(true);
    expect((await f.run({ mode: "update" })).isError).toBe(true);
    expect((await f.run({ mode: "update", expectedDataVersion: "stale" })).isError).toBe(true);
    f.setRow({ ...key });
    expect((await f.run({ mode: "update", expectedDataVersion: "v1" })).isError).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it("blocks changed confirmation arguments and stale version between preview and confirmation", async () => {
    const f = await fixture(saved());
    const p = await f.run({ mode: "update", expectedDataVersion: "v1" });
    f.setRow({ ...saved(), DataVersion: "v2" });
    expect(
      (
        await f.run({
          mode: "update",
          expectedDataVersion: "v1",
          operationId: data(p).operationId,
          confirm: true,
        })
      ).isError,
    ).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it("marks readback mismatch uncertain and prevents re-send with the same operationId", async () => {
    const f = await fixture();
    const p = await f.run();
    f.corrupt();
    const args = { confirm: true, operationId: data(p).operationId };
    expect((await f.run(args)).isError).toBe(true);
    expect((await f.run(args)).isError).toBe(true);
    expect(f.writes).toHaveLength(1);
    const entry = await f.conn.client.operationEntry(data(p).operationId as string);
    expect(entry?.state).toBe("outcome_unknown");
  });
  it.each([{ readOnly: true }, { writable: false }])("honors existing write gates %j", async (options) => {
    const f = await fixture(undefined, options);
    const p = await f.run();
    expect(p.isError).toBeFalsy();
    expect((await f.run({ confirm: true, operationId: data(p).operationId })).isError).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it("blocks duplicate dimensions and unpublished CAS types", async () => {
    const f = await fixture(saved());
    f.meta.keys[1] = f.meta.keys[0]!;
    expect((await f.run({ mode: "update", expectedDataVersion: "v1" })).isError).toBe(true);
    const g = await fixture(saved());
    g.meta.properties = g.meta.properties.filter((p) => p.name !== "DataVersion");
    expect((await g.run({ mode: "update", expectedDataVersion: "v1" })).isError).toBe(true);
    expect(g.writes).toEqual([]);
  });
  it("does not send a write without preview or after changing confirmation input", async () => {
    const f = await fixture();
    expect((await f.run({ confirm: true })).isError).toBe(true);
    const p = await f.run();
    const r = await f.run({
      confirm: true,
      operationId: data(p).operationId,
      accounts: { customerSettlement: ACCOUNT },
    });
    expect(r.isError).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it("blocks missing resources and account references not found in configured chart", async () => {
    const f = await fixture();
    f.meta.properties = f.meta.properties.filter((p) => p.name !== resources[2]);
    expect((await f.run()).isError).toBe(true);
    expect(f.writes).toEqual([]);
    const g = await fixture();
    expect((await g.run({ accounts: { supplierSettlement: OTHER } })).isError).toBe(true);
    expect(g.writes).toEqual([]);
  });
  it("reports uncertain register operations without document/comment reconciliation", async () => {
    const f = await fixture();
    const p = await f.run();
    f.corrupt();
    const operationId = data(p).operationId;
    await f.run({ confirm: true, operationId });
    const tools = (
      createServer({ db: () => f.conn } as never) as unknown as {
        _registeredTools: Record<
          string,
          { handler: (args: object, extra: object) => Promise<CallToolResult> }
        >;
      }
    )._registeredTools;
    const status = await tools["write.operation.status"]!.handler({ database: "default", operationId }, {});
    expect(data(status).status).toBe("unverifiable");
    expect((await f.conn.client.operationEntry(operationId as string))?.state).toBe("outcome_unknown");
  });
  it("sends If-Match through fetch and never retries a failed PATCH body", async () => {
    const f = await fixture();
    vi.mocked(f.conn.client.request).mockRestore();
    const fetchMock = vi.fn(async (_url: unknown, _init: unknown) => new Response("", { status: 412 }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(
        f.conn.client.request("Synthetic(key='x')", "PATCH", { value: 1 }, { "If-Match": "v1" }),
      ).rejects.toBeInstanceOf(ODataError);
      expect(fetchMock).toHaveBeenCalledOnce();
      expect(fetchMock.mock.calls[0]![1]).toMatchObject({
        method: "PATCH",
        headers: { "If-Match": "v1" },
        body: JSON.stringify({ value: 1 }),
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
