import { describe, expect, it } from "vitest";
import { snapshotConnection, snapshotTools } from "./helpers/metadata-snapshot.js";
import { parseMetadata } from "../src/odata/metadata.js";

describe("проверка возможностей OData", () => {
  it("показывает требования и независимую диагностику без попытки записи", () => {
    const tools = snapshotTools(snapshotConnection());
    expect(tools["read.system.odata_requirements"]).toBeDefined();
    expect(tools["read.system.odata_capabilities"]).toBeDefined();
    expect(tools["read.system.odata_refresh"]).toBeDefined();
  });

  it("сохраняет объявленные функции и их типы результата отдельно от корня регистра", () => {
    const meta = parseMetadata(`<Edmx><DataServices><Schema Namespace="S">
      <ComplexType Name="BalanceResult"><Property Name="Amount" Type="Edm.Decimal"/></ComplexType>
      <EntityContainer><FunctionImport Name="Balance" ReturnType="Collection(S.BalanceResult)" IsBindable="true">
      <Parameter Name="bindingParameter" Type="S.AccountingRegister_Test"/>
      <Parameter Name="Period" Type="Edm.DateTime"/></FunctionImport></EntityContainer>
      </Schema></DataServices></Edmx>`);
    expect(meta).toHaveProperty("functions");
    expect(Reflect.get(meta, "functions")).toEqual([
      expect.objectContaining({
        name: "Balance",
        bindingType: "AccountingRegister_Test",
        properties: [expect.objectContaining({ name: "Amount", type: "Edm.Decimal" })],
      }),
    ]);
  });
});

import { afterEach, vi } from "vitest";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseConfig } from "../src/config/env.js";
import { Connection, ServerContext } from "../src/context.js";
import { createServer } from "../src/mcp/server.js";
import { REQUIREMENTS, assertToolRequirement } from "../src/capabilities/requirements.js";
import { inspectRequirement } from "../src/capabilities/schema.js";
import { classifyObservation } from "../src/capabilities/monitor.js";
import { fromHttpStatus, ODataError } from "../src/odata/errors.js";
import { CapabilityProfileSchema } from "../src/capabilities/config.js";

const dirs: string[] = [];
const monitors: Connection[] = [];
const xml = (
  fields = '<Property Name="Ref_Key" Type="Edm.Guid"/><Property Name="Description" Type="Edm.String"/><Property Name="ИНН" Type="Edm.String"/>',
  extra = "",
) =>
  `<Edmx><DataServices><Schema Namespace="S"><EntityType Name="Catalog_Организации"><Key><PropertyRef Name="Ref_Key"/></Key>${fields}</EntityType><EntityContainer><EntitySet Name="Catalog_Организации" EntityType="S.Catalog_Организации"/>${extra}</EntityContainer></Schema></DataServices></Edmx>`;
async function setup(env: Record<string, string> = {}, stateDir?: string) {
  const dir = stateDir ?? (await mkdtemp(join(tmpdir(), "odata-capabilities-test-")));
  if (!stateDir) dirs.push(dir);
  const config = parseConfig({
    ODATA_BASE_URL: "https://secret-host.invalid/private-ledger/",
    ODATA_USERNAME: "secret-principal",
    ODATA_PASSWORD: "secret-password",
    ODATA_RETRIES: "0",
    ODATA_CAPABILITY_STATE_DIR: dir,
    ODATA_CAPABILITY_PROBE_LIMIT: "0",
    ...env,
  });
  const conn = new Connection(config.connections[0]!, config.behavior);
  monitors.push(conn);
  let metadata = xml();
  let status = 200;
  let body = '{"value":[]}';
  const requests: { method: string; url: string }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    requests.push({ method: init?.method ?? "GET", url });
    if (url.endsWith("$metadata")) return new Response(metadata, { status: 200 });
    return new Response(body, { status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    config,
    conn,
    dir,
    requests,
    fetchMock,
    schema: (value: string) => {
      metadata = value;
    },
    response: (code: number, value: string) => {
      status = code;
      body = value;
    },
  };
}
afterEach(async () => {
  for (const conn of monitors.splice(0)) conn.capabilities.stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const dir of dirs.splice(0)) await rm(dir, { recursive: true, force: true });
});

describe("версионные требования", () => {
  it("каждый зарегистрированный инструмент имеет явную запись, неизвестная регистрация отклоняется", () => {
    expect(REQUIREMENTS.tools.map((t) => t.name).sort()).toEqual(
      Object.keys(snapshotTools(snapshotConnection())).sort(),
    );
    expect(() => assertToolRequirement("read.system.future_tool")).toThrow("не описаны требования");
    for (const tool of REQUIREMENTS.tools)
      for (const id of tool.required)
        expect(REQUIREMENTS.definitions[id], `${tool.name}: ${id}`).toBeDefined();
    expect(new Set(REQUIREMENTS.tools.map((t) => t.name)).size).toBe(REQUIREMENTS.tools.length);
  });
  it("различает отсутствие в схеме, отсутствие в конфигурации по явному факту и конфликт факта", async () => {
    const { conn } = await setup();
    const req = REQUIREMENTS.definitions.organizations!;
    expect(inspectRequirement(req, conn.cfg, { entities: new Map(), odataVersion: "3.0" }).status).toBe(
      "absent_from_accessible_metadata",
    );
    const profile = CapabilityProfileSchema.parse({
      schemaVersion: 1,
      revision: "by-1",
      profile: "belarus",
      configuration: "BY",
      configurationVersion: "2.1",
      unsupported: [
        {
          entitySet: "Catalog_Организации",
          configuration: "BY",
          configurationVersion: "2.1",
          source: "support-123",
          observedAt: "2026-10-01T00:00:00.000Z",
        },
      ],
    });
    const cfg = { ...conn.cfg, capabilityProfile: profile };
    expect(inspectRequirement(req, cfg, { entities: new Map(), odataVersion: "3.0" }).status).toBe(
      "not_implemented",
    );
    expect(inspectRequirement(req, cfg, parseMetadata(xml()))).toMatchObject({
      status: "published",
      factConflict: true,
    });
    expect(CapabilityProfileSchema.safeParse({ ...profile, configurationVersion: "2.2" }).success).toBe(
      false,
    );
  });
  it("проверяет типы, ключи и выбирает совместимую альтернативу", async () => {
    const { conn } = await setup();
    const meta = parseMetadata(xml());
    expect(
      inspectRequirement(
        { target: ["Catalog_Организации"], fields: { Ref_Key: "Edm.String" }, keys: ["Other"] },
        conn.cfg,
        meta,
      ),
    ).toMatchObject({ status: "schema_incompatible", missing: ["Ref_Key:Edm.String", "key:Other"] });
    const e = meta.entities.get("Catalog_Организации")!;
    meta.entities.set("Catalog_Second", {
      ...e,
      entitySet: "Catalog_Second",
      properties: [...e.properties, { name: "Extra", type: "Edm.String", nullable: true }],
    });
    expect(
      inspectRequirement(
        { target: ["Catalog_Организации", "Catalog_Second"], fields: ["Extra"] },
        conn.cfg,
        meta,
      ),
    ).toMatchObject({ status: "published", entitySet: "Catalog_Second" });
  });
  it("учитывает настройку плана счетов и неизвестную схему функции", async () => {
    const { conn } = await setup({ ODATA_ACCOUNTING_CONFIG: '{"register":"AccountingRegister_Custom"}' });
    const meta = parseMetadata(xml());
    const e = meta.entities.values().next().value!;
    meta.entities.set("AccountingRegister_Custom", { ...e, entitySet: "AccountingRegister_Custom" });
    expect(inspectRequirement(REQUIREMENTS.definitions["accounting.balance"]!, conn.cfg, meta)).toMatchObject(
      { candidates: ["AccountingRegister_Custom"], status: "unknown" },
    );
  });
});

describe("наблюдения, сохранение и изменения", () => {
  it("refresh использует только свежую metadata и ограниченные GET, не выдаёт права записи", async () => {
    const { conn, requests } = await setup({
      ODATA_CAPABILITY_PROBE_LIMIT: "3",
      READ_ONLY: "false",
      ODATA_WRITABLE: "true",
    });
    await conn.getMetadata();
    const report = await conn.capabilities.refresh();
    expect(requests.filter((r) => r.url.endsWith("$metadata"))).toHaveLength(2);
    expect(requests.every((r) => r.method === "GET")).toBe(true);
    expect(requests.some((r) => r.url.includes("$top=0"))).toBe(true);
    expect(report.operations).toEqual([
      expect.objectContaining({
        operation: "read",
        scope: "collection_query",
        effectiveStatus: "observed_success",
      }),
    ]);
    expect(report.localWritePolicy).toMatchObject({
      readOnly: false,
      writable: true,
      serverRights: "unknown",
    });
  });
  it("сохраняет missing/restored после рестарта и не хранит URL, логины, ключи и тела", async () => {
    const first = await setup();
    await first.conn.capabilities.refresh();
    await first.conn.client.request("Catalog_Организации(guid'secret-record-key')?$filter=secret-filter");
    first.schema(xml('<Property Name="Ref_Key" Type="Edm.Guid"/>'));
    await first.conn.capabilities.refresh();
    expect((await first.conn.capabilities.status()).events).toContainEqual(
      expect.objectContaining({ kind: "newly_missing", requirement: "organizations" }),
    );
    const second = await setup({}, first.dir);
    const restored = await second.conn.capabilities.status();
    expect(restored.events).toContainEqual(
      expect.objectContaining({ kind: "newly_missing", requirement: "organizations" }),
    );
    await second.conn.capabilities.refresh();
    expect((await second.conn.capabilities.status()).events).toContainEqual(
      expect.objectContaining({ kind: "restored", requirement: "organizations" }),
    );
    const stored = await readFile(join(first.dir, (await readdir(first.dir))[0]!), "utf8");
    for (const secret of [
      "secret-host",
      "private-ledger",
      "secret-principal",
      "secret-password",
      "secret-record-key",
      "secret-filter",
    ])
      expect(stored).not.toContain(secret);
  });
  it("новое требование/инструмент обнаруживается после смены версии сохранённого inventory", async () => {
    const first = await setup();
    await first.conn.capabilities.refresh();
    const path = join(first.dir, (await readdir(first.dir))[0]!);
    const state = JSON.parse(await readFile(path, "utf8"));
    delete state.fingerprints["tool:read.system.odata_refresh"];
    delete state.fingerprints.organizations;
    state.revision = "older-version";
    await writeFile(path, JSON.stringify(state));
    const second = await setup({}, first.dir);
    const result = await second.conn.capabilities.refresh();
    expect(result.events).toContainEqual(
      expect.objectContaining({ kind: "newly_required", requirement: "tool:read.system.odata_refresh" }),
    );
    expect(result.events).toContainEqual(
      expect.objectContaining({ kind: "newly_required", requirement: "organizations" }),
    );
  });
  it("разделяет principal и базу; смена пароля не создаёт новый principal", async () => {
    const first = await setup();
    const firstReport = await first.conn.capabilities.refresh();
    const same = await setup({ ODATA_PASSWORD: "new-password" }, first.dir);
    expect((await same.conn.capabilities.status()).scope).toBe(firstReport.scope);
    const other = await setup({ ODATA_USERNAME: "another-user" }, first.dir);
    expect((await other.conn.capabilities.status()).scope).not.toBe(firstReport.scope);
    expect((await other.conn.capabilities.status()).events).toEqual([]);
    const otherBase = await setup({ ODATA_BASE_URL: "https://secret-host.invalid/other-base/" }, first.dir);
    expect((await otherBase.conn.capabilities.status()).scope).not.toBe(firstReport.scope);
  });
  it("текущий сбой не затирает последнюю успешную схему и не приписывает удаление объектов", async () => {
    const f = await setup();
    const before = await f.conn.capabilities.refresh();
    f.schema("<html>maintenance</html>");
    const after = await f.conn.capabilities.refresh();
    expect(after.summary).toMatchObject({
      metadataStatus: "unknown",
      lastSuccessAt: before.summary.lastSuccessAt,
    });
    expect(after.requirements.organizations?.status).toBe("unknown");
    expect(after.lastSuccessfulSchema.organizations?.status).toBe("published");
    expect(after.events.filter((e) => e.requirement === "organizations")).toEqual([]);
    f.schema(xml());
    expect((await f.conn.capabilities.refresh()).events).toContainEqual(
      expect.objectContaining({ kind: "observation_restored" }),
    );
  });
  it("404 записи не означает отсутствие объекта, 403 обслуживания не означает отказ роли", async () => {
    const f = await setup();
    await f.conn.capabilities.refresh();
    f.response(404, '{"odata.error":{"message":{"value":"Экземпляр сущности не найден"}}}');
    await expect(f.conn.client.request("Catalog_Организации(guid'secret-record-key')")).rejects.toThrow();
    expect((await f.conn.capabilities.status()).operations).toContainEqual(
      expect.objectContaining({ reason: "record_not_found", status: "unknown" }),
    );
    f.response(403, "maintenance");
    await expect(f.conn.client.request("Catalog_Организации?$top=0")).rejects.toThrow();
    expect((await f.conn.capabilities.status()).operations).toContainEqual(
      expect.objectContaining({ reason: "authentication_or_service", status: "unknown" }),
    );
    expect((await f.conn.capabilities.status()).requirements.organizations?.status).toBe("published");
  });
  it("наблюдает обычную запись без пробных мутаций и ограничивает её свидетельство по времени", async () => {
    const f = await setup({ READ_ONLY: "false", ODATA_WRITABLE: "true" });
    await f.conn.capabilities.refresh();
    f.response(200, '{"Ref_Key":"secret-created-key","Description":"secret-record-value"}');
    await f.conn.client.request("Catalog_Организации", "POST", { Description: "secret-input" });
    expect((await f.conn.capabilities.status()).operations).toContainEqual(
      expect.objectContaining({
        operation: "create",
        effectiveStatus: "observed_success",
        scope: "one_operation",
      }),
    );
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + 25 * 60 * 60 * 1000);
    expect((await f.conn.capabilities.status()).operations).toContainEqual(
      expect.objectContaining({ operation: "create", effectiveStatus: "unknown", expired: true }),
    );
    expect(await readFile(join(f.dir, (await readdir(f.dir))[0]!), "utf8")).not.toMatch(
      /secret-created-key|secret-record-value|secret-input/,
    );
  });
  it("явный отказ1С отличается от обычного500,403 и сетевой ошибки", () => {
    const denied = fromHttpStatus(
      500,
      "secret-url",
      '{"odata.error":{"message":{"value":"Нарушение прав доступа!"}}}',
    );
    expect(classifyObservation(denied)).toMatchObject({ status: "observed_denied" });
    for (const e of [
      fromHttpStatus(500, "x", "maintenance"),
      fromHttpStatus(403, "x", "maintenance"),
      new ODataError({ kind: "network", message: "secret-host" }),
    ])
      expect(classifyObservation(e).status).toBe("unknown");
  });
  it("недоступное или повреждённое хранилище не ломает чтение и явно помечено", async () => {
    const f = await setup();
    const scope = (await f.conn.capabilities.status()).scope;
    await writeFile(join(f.dir, `${scope}.json`), "broken");
    const second = await setup({}, f.dir);
    expect((await second.conn.capabilities.status()).summary.persistence).toBe("unavailable");
    const invalidDir = join(f.dir, "a-file");
    await writeFile(invalidDir, "occupied");
    const third = await setup({}, invalidDir);
    expect((await third.conn.capabilities.refresh()).summary.persistence).toBe("unavailable");
    await expect(third.conn.client.request("Catalog_Организации?$top=0")).resolves.toBeDefined();
  });
});

describe("автоматический запуск и остановка", () => {
  it("initialized запускает полный bounded проход, периодика перечитывает схему, close останавливает таймер", async () => {
    const f = await setup({ ODATA_CAPABILITY_INTERVAL_MS: "60000", ODATA_CAPABILITY_PROBE_LIMIT: "1" });
    const ctx = new ServerContext(f.config);
    monitors.push(ctx.db());
    vi.useFakeTimers();
    const server = createServer(ctx);
    const logging = vi.spyOn(server, "sendLoggingMessage").mockResolvedValue();
    server.server.oninitialized?.();
    await ctx.db().capabilities.refresh();
    expect(f.requests.some((r) => r.url.includes("$top=0"))).toBe(true);
    expect(logging).toHaveBeenCalled();
    f.schema(xml('<Property Name="Ref_Key" Type="Edm.Guid"/>'));
    await vi.advanceTimersByTimeAsync(60000);
    expect((await ctx.db().capabilities.status()).requirements.organizations?.status).toBe(
      "schema_incompatible",
    );
    server.server.onclose?.();
    const count = f.requests.length;
    await vi.advanceTimersByTimeAsync(120000);
    expect(f.requests.length).toBe(count);
  });
});

describe("динамический контракт", () => {
  it("для post_document разрешает точный объект и объявленные действия без вызова POST", async () => {
    const conn = snapshotConnection();
    const tools = snapshotTools(conn);
    const result = await tools["read.system.odata_requirements"]!.handler(
      { tool: "write.document.post_document", entitySet: "Document_СписаниеСРасчетногоСчета" },
      {},
    );
    expect(result.structuredContent?.dynamic).toMatchObject({
      functions: [expect.objectContaining({ name: "Post" }), expect.objectContaining({ name: "Unpost" })],
    });
  });
  it("показывает несовместимые условные поля составного контрагента без блокировки независимых инструментов", async () => {
    const f = await setup();
    await f.conn.capabilities.refresh();
    const report = await f.conn.capabilities.status();
    const tool = report.tools.find((t) => t.name === "write.money.create_bank_writeoff")!;
    expect(tool).toHaveProperty("mutationFieldModel");
    expect(report.tools.find((t) => t.name === "read.system.list_databases")?.invocationStatus).toBe("local");
  });
});

describe("точность свидетельств функций", () => {
  it("успех корневого GET не доказывает вызов Balance; нормальный вызов учитывается отдельно", async () => {
    const f = await setup({ ODATA_CAPABILITY_PROBE_LIMIT: "0" });
    const register = "AccountingRegister_Хозрасчетный";
    f.schema(xml().replace('</EntityContainer>', `<EntitySet Name="${register}" EntityType="S.${register}"/></EntityContainer>`));
    await f.conn.capabilities.refresh();
    await f.conn.client.request(`${register}?$top=0`);
    const before = await f.conn.capabilities.status();
    expect(before.requirements["accounting.balance"]?.operations[0]?.evidence.effectiveStatus).toBe("unknown");
    await f.conn.client.request(`${register}/Balance(Period=datetime'2026-01-01T00:00:00')?$top=0`);
    const after = await f.conn.capabilities.status();
    expect(after.requirements["accounting.balance"]?.operations[0]?.evidence.effectiveStatus).toBe("observed_success");
    expect(after.operations).toContainEqual(expect.objectContaining({ functionName: "Balance" }));
  });
});
