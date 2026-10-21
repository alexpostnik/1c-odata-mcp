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
const id = (n: number) => `${n}`.repeat(8) + "-1111-4111-8111-111111111111";
const ORG = id(1),
  CP = id(2),
  CTR = id(3),
  ACCOUNT = id(4),
  GROUP = id(5),
  CUR = id(6),
  DOC = id(7),
  OLD = id(8);
const EMPTY = "00000000-0000-0000-0000-000000000000";
const SET = "Document_ПоступлениеНаРасчетныйСчет",
  REG = "InformationRegister_СчетаУчетаРасчетовСКонтрагентами";
const CONTRACT = "ДоговорКонтрагента_Key",
  ACCOUNT_FIELD = "СчетУчетаРасчетовСКонтрагентом_Key",
  ROWS = "РасшифровкаПлатежа";
const TOOL = "write.document.apply_bank_settlement_settings";
const dirs: string[] = [];
afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all(dirs.splice(0).map((d) => rm(d, { recursive: true, force: true })));
});
const data = (r: CallToolResult) => r.structuredContent as Record<string, unknown>;
async function fixture(entitySet = SET) {
  const dir = await mkdtemp(join(tmpdir(), "bank-settings-"));
  dirs.push(dir);
  const cfg = parseConfig({
    ODATA_BASE_URL: "https://example.invalid/odata/",
    ODATA_USERNAME: "synthetic",
    ODATA_PASSWORD: "synthetic",
    READ_ONLY: "false",
    ODATA_WRITABLE: "true",
    WRITE_JOURNAL_DIR: dir,
  });
  const conn = new Connection(
    { ...cfg.connections[0]!, writable: true },
    { ...cfg.behavior, writeJournalDir: dir, regulatedCurrencyCode: "933" },
  );
  let doc: ODataEntity = {
    Ref_Key: DOC,
    DataVersion: "v1",
    Posted: false,
    DeletionMark: false,
    Организация_Key: ORG,
    Контрагент_Key: CP,
    ВалютаДокумента_Key: CUR,
    ВидОперации: "РасчетыПоКредитамИЗаймам",
    [CONTRACT]: OLD,
    [ACCOUNT_FIELD]: OLD,
    СуммаДокумента: 150,
    Date: "2010-03-22T00:00:00",
    НазначениеПлатежа: "Synthetic loan",
    СубконтоДт1: "cost-analytic",
    [ROWS]: [
      {
        LineNumber: "1",
        [CONTRACT]: OLD,
        [ACCOUNT_FIELD]: OLD,
        СчетУчетаРасчетовПоАвансам_Key: OLD,
        СуммаПлатежа: 150,
        СуммаНДС: 0,
        Субконто: "preserved",
      },
    ],
  };
  let contract: ODataEntity = {
    Ref_Key: CTR,
    DeletionMark: false,
    IsFolder: false,
    Owner_Key: CP,
    Организация_Key: ORG,
    ВалютаВзаиморасчетов_Key: CUR,
    ВидДоговора: "Прочее",
    РасчетыВУсловныхЕдиницах: false,
  };
  let account: ODataEntity = {
    Ref_Key: ACCOUNT,
    Code: "67.2",
    Description: "Synthetic account",
    DeletionMark: false,
    IsFolder: false,
    ЗапретитьИспользоватьВПроводках: false,
  };
  let settings: ODataEntity[] = [
    {
      Организация_Key: EMPTY,
      Контрагент_Key: GROUP,
      Договор_Key: EMPTY,
      ВидРасчетовПоДоговору: "РасчетыВВалютеРегламентированногоУчета",
      СчетУчетаРасчетовСПоставщиком_Key: ACCOUNT,
    },
  ];
  const meta = (
    name: string,
    properties: Record<string, string>,
    cls: EntityMeta["class"] = "catalog",
  ): EntityMeta => ({
    entitySet: name,
    entityType: name,
    class: cls,
    shortName: name,
    keys: ["Ref_Key"],
    properties: Object.entries(properties).map(([name, type]) => ({ name, type, nullable: true })),
    navigations: [],
  });
  const infer = (r: ODataEntity) =>
    Object.fromEntries(
      Object.entries(r).map(([k, v]) => [
        k,
        k.endsWith("_Key")
          ? "Edm.Guid"
          : typeof v === "boolean"
            ? "Edm.Boolean"
            : typeof v === "number"
              ? "Edm.Double"
              : "Edm.String",
      ]),
    );
  const entities = new Map<string, EntityMeta>([
    [
      entitySet,
      meta(
        entitySet,
        { ...infer(doc), [ROWS]: `Collection(StandardODATA.${entitySet}_${ROWS}_RowType)` },
        "document",
      ),
    ],
    [`${entitySet}_${ROWS}`, meta(`${entitySet}_${ROWS}`, infer((doc[ROWS] as ODataEntity[])[0]!))],
    [
      "Catalog_Контрагенты",
      meta("Catalog_Контрагенты", {
        Ref_Key: "Edm.Guid",
        Parent_Key: "Edm.Guid",
        DeletionMark: "Edm.Boolean",
        IsFolder: "Edm.Boolean",
      }),
    ],
    ["Catalog_ДоговорыКонтрагентов", meta("Catalog_ДоговорыКонтрагентов", infer(contract))],
    ["Catalog_Валюты", meta("Catalog_Валюты", { Ref_Key: "Edm.Guid", Code: "Edm.String" })],
    ["ChartOfAccounts_Хозрасчетный", meta("ChartOfAccounts_Хозрасчетный", infer(account), "chartOfAccounts")],
    [REG, meta(REG, infer(settings[0]!), "informationRegister")],
  ]);
  vi.spyOn(conn, "getMetadata").mockResolvedValue({ entities, odataVersion: "3.0" });
  let failure: "write" | "readback" | undefined;
  const writes: Array<{ path: string; method: string; body: unknown; headers: unknown }> = [];
  vi.spyOn(conn.client, "request").mockImplementation(async (path, method = "GET", body, headers) => {
    if (method !== "GET") {
      writes.push({ path, method, body, headers });
      if (failure === "write") throw new ODataError({ kind: "timeout", message: "Synthetic timeout" });
      if (path.includes("/Unpost")) doc = { ...doc, Posted: false, DataVersion: "v2" };
      else if (path.includes("/Post")) doc = { ...doc, Posted: true, DataVersion: "v2" };
      else doc = { ...doc, ...(body as object), DataVersion: "v2" };
      return {} as never;
    }
    if (path.startsWith(entitySet)) {
      if (writes.length && failure === "readback")
        throw new ODataError({ kind: "auth", message: "Synthetic readback failure" });
      return structuredClone(doc) as never;
    }
    if (path.startsWith("Catalog_Договоры")) return contract as never;
    if (path.startsWith("Catalog_Валюты")) return { Code: "933" } as never;
    if (path.startsWith("Catalog_Контрагенты"))
      return (
        path.includes(GROUP)
          ? { Ref_Key: GROUP, IsFolder: true, DeletionMark: false, Parent_Key: EMPTY }
          : { Ref_Key: CP, IsFolder: false, DeletionMark: false, Parent_Key: GROUP }
      ) as never;
    if (path.startsWith(REG)) return { value: settings } as never;
    if (path.startsWith("ChartOfAccounts_"))
      return (path.includes("guid'") && !path.includes("$filter") ? account : { value: [account] }) as never;
    throw new Error(`Unexpected synthetic path ${path}`);
  });
  const tools = (
    createServer({ db: () => conn } as never) as unknown as {
      _registeredTools: Record<string, { handler: (args: object, extra: object) => Promise<CallToolResult> }>;
    }
  )._registeredTools;
  const run = async (extra: object = {}, tool = TOOL) => {
    expect(tools[tool], "bounded existing-bank tool is registered").toBeDefined();
    return tools[tool]!.handler(
      {
        database: "default",
        entitySet,
        ref: DOC,
        contractRef: CTR,
        side: "supplier",
        expectedDataVersion: "v1",
        confirm: false,
        ...extra,
      },
      {},
    );
  };
  return {
    run,
    conn,
    writes,
    entities,
    doc: () => structuredClone(doc),
    setDoc: (v: object) => (doc = { ...doc, ...v }),
    setContract: (v: object) => (contract = { ...contract, ...v }),
    setAccount: (v: object) => (account = { ...account, ...v }),
    setSettings: (v: ODataEntity[]) => (settings = v),
    settings: () => settings,
    fail: (v: typeof failure) => (failure = v),
  };
}

describe("existing bank inherited settlement", () => {
  it("applies all-org group supplier account with provenance while preserving header and row analytics", async () => {
    const f = await fixture(),
      before = f.doc(),
      p = await f.run();
    expect(p.isError).toBeFalsy();
    expect(data(p).resolution).toMatchObject({
      choice: { settle: ACCOUNT, provenance: { settle: { level: "группа", counterpartyRef: GROUP } } },
    });
    const r = await f.run({ confirm: true, operationId: data(p).operationId });
    expect(r.isError).toBeFalsy();
    expect(f.writes).toHaveLength(1);
    expect(f.writes[0]).toMatchObject({ method: "PATCH", headers: { "If-Match": "v1" } });
    expect(f.doc()).toEqual({
      ...before,
      DataVersion: "v2",
      [CONTRACT]: CTR,
      [ACCOUNT_FIELD]: ACCOUNT,
      [ROWS]: [{ ...(before[ROWS] as object[])[0], [CONTRACT]: CTR, [ACCOUNT_FIELD]: ACCOUNT }],
    });
    expect((await f.run({ confirm: true, operationId: data(p).operationId })).isError).toBeFalsy();
    expect(f.writes).toHaveLength(1);
  });
  it("uses individual override before all-org group", async () => {
    const f = await fixture();
    f.setSettings([
      ...f.settings(),
      { ...f.settings()[0], Контрагент_Key: CP, СчетУчетаРасчетовСПоставщиком_Key: OLD },
    ]);
    f.setAccount({ Ref_Key: OLD });
    expect(data(await f.run()).resolution).toMatchObject({ choice: { settle: OLD, level: "контрагент" } });
  });
  it.each([{ side: undefined }, { expectedDataVersion: "stale" }, { entitySet: "Catalog_Контрагенты" }])(
    "rejects unsupported or unbound inputs %j",
    async (v) => {
      const f = await fixture();
      expect((await f.run(v)).isError).toBe(true);
      expect(f.writes).toEqual([]);
    },
  );
  it.each([
    { Posted: true },
    { DeletionMark: true },
    { Контрагент_Key: OLD },
    { Организация_Key: OLD },
    { ВалютаДокумента_Key: OLD },
    { [ROWS]: [{}, {}] },
  ])("rejects unsafe document %j", async (v) => {
    const f = await fixture();
    f.setDoc(v);
    expect((await f.run()).isError).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it.each([
    { DeletionMark: true },
    { IsFolder: true },
    { Owner_Key: OLD },
    { Организация_Key: OLD },
    { ВалютаВзаиморасчетов_Key: OLD },
  ])("rejects unsuitable contract %j", async (v) => {
    const f = await fixture();
    f.setContract(v);
    expect((await f.run()).isError).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it.each([
    { DeletionMark: true },
    { IsFolder: true },
    { ЗапретитьИспользоватьВПроводках: true },
    { ЗапретитьИспользоватьВПроводках: undefined },
  ])("rejects unusable account %j", async (v) => {
    const f = await fixture();
    f.setAccount(v);
    expect((await f.run()).isError).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it.each(["settings", "doc"])("rejects changed %s between preview and confirmation", async (which) => {
    const f = await fixture(),
      p = await f.run();
    if (which === "doc") f.setDoc({ DataVersion: "v2" });
    else f.setSettings([{ ...f.settings()[0], Контрагент_Key: CP }]);
    expect((await f.run({ confirm: true, operationId: data(p).operationId })).isError).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it.each(["write", "readback"] as const)("holds unknown %s without re-sending PATCH", async (stage) => {
    const f = await fixture(),
      p = await f.run();
    f.fail(stage);
    const args = { confirm: true, operationId: data(p).operationId };
    expect((await f.run(args)).isError).toBe(true);
    expect((await f.run(args)).isError).toBe(true);
    expect(f.writes).toHaveLength(1);
    expect((await f.conn.client.operationEntry(data(p).operationId as string))?.state).toBe(
      "outcome_unknown",
    );
  });
  it("supports explicit supplier side for other writeoff without clearing expense analytic", async () => {
    const f = await fixture("Document_СписаниеСРасчетногоСчета");
    f.setDoc({ ВидОперации: "ПрочееСписание" });
    const p = await f.run();
    expect(p.isError).toBeFalsy();
    expect(data(p).payload).not.toHaveProperty("СубконтоДт1");
    const before = f.doc();
    expect((await f.run({ confirm: true, operationId: data(p).operationId })).isError).toBeFalsy();
    expect(f.doc().СубконтоДт1).toBe(before.СубконтоДт1);
    expect(f.doc().СуммаДокумента).toBe(before.СуммаДокумента);
  });
});

describe("mutation HTTP retry boundary", () => {
  it("does not retry a bodyless Post after network failure", async () => {
    const f = await fixture();
    vi.mocked(f.conn.client.request).mockRestore();
    const fetchMock = vi.fn(async () => {
      throw new Error("Synthetic connection loss");
    });
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(f.conn.client.action(`Document_Synthetic(guid'${DOC}')/Post`)).rejects.toBeInstanceOf(
        ODataError,
      );
      expect(fetchMock).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
describe("version guarded separate Post/Unpost", () => {
  it.each([true, false])("previews and executes post=%s once with readback", async (post) => {
    const f = await fixture();
    f.setDoc({ Posted: !post });
    const p = await f.run({ post }, "write.document.post_document");
    expect(p.isError).toBeFalsy();
    expect(f.writes).toEqual([]);
    const args = { post, confirm: true, operationId: data(p).operationId };
    expect((await f.run(args, "write.document.post_document")).isError).toBeFalsy();
    expect((await f.run(args, "write.document.post_document")).isError).toBeFalsy();
    expect(f.writes).toHaveLength(1);
    expect(f.doc().Posted).toBe(post);
  });
  it("rejects changed version before action", async () => {
    const f = await fixture(),
      p = await f.run({ post: true }, "write.document.post_document");
    f.setDoc({ DataVersion: "changed" });
    expect(
      (
        await f.run(
          { post: true, confirm: true, operationId: data(p).operationId },
          "write.document.post_document",
        )
      ).isError,
    ).toBe(true);
    expect(f.writes).toEqual([]);
  });
  it.each(
    [true, false].flatMap((post) => (["write", "readback"] as const).map((stage) => ({ post, stage }))),
  )("holds unknown post=$post $stage and status cannot clear hold", async ({ post, stage }) => {
    const f = await fixture();
    f.setDoc({ Posted: !post });
    const p = await f.run({ post }, "write.document.post_document");
    f.fail(stage);
    const args = { post, confirm: true, operationId: data(p).operationId };
    expect((await f.run(args, "write.document.post_document")).isError).toBe(true);
    expect((await f.run(args, "write.document.post_document")).isError).toBe(true);
    expect(f.writes).toHaveLength(1);
    const status = await f.run({ operationId: data(p).operationId }, "write.operation.status");
    expect(data(status).status).toBe("unverifiable");
    expect((await f.conn.client.operationEntry(data(p).operationId as string))?.state).toBe(
      "outcome_unknown",
    );
  });
});
