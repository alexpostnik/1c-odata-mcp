import { describe, expect, it } from "vitest";
import { snapshotConnection, snapshotTools } from "./helpers/metadata-snapshot.js";
import { parseMetadata } from "../src/odata/metadata.js";
import { balanceByAccounts } from "../src/odata/accounting.js";

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
    vi.setSystemTime(Date.now() + 49 * 60 * 60 * 1000);
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
    await ctx.db().capabilities.start();
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
    f.schema(
      xml().replace(
        "</EntityContainer>",
        `<EntitySet Name="${register}" EntityType="S.${register}"/></EntityContainer>`,
      ),
    );
    await f.conn.capabilities.refresh();
    await f.conn.client.request(`${register}?$top=0`);
    const before = await f.conn.capabilities.status();
    expect(before.requirements["accounting.balance"]?.operations[0]?.evidence.effectiveStatus).toBe(
      "unknown",
    );
    await f.conn.client.request(`${register}/Balance(Period=datetime'2026-01-01T00:00:00')?$top=0`);
    const after = await f.conn.capabilities.status();
    expect(after.requirements["accounting.balance"]?.operations[0]?.evidence.effectiveStatus).toBe(
      "observed_success",
    );
    expect(after.operations).toContainEqual(expect.objectContaining({ functionName: "Balance" }));
  });
});

describe("полнота автоматической проверки и provenance", () => {
  it("по умолчанию startup и периодика проходят все подходящие статические объекты, а не три", async () => {
    const f = await setup({ ODATA_CAPABILITY_PROBE_LIMIT: "64", ODATA_CAPABILITY_INTERVAL_MS: "60000" });
    const { bp30Metadata } = await import("./helpers/metadata-snapshot.js");
    vi.spyOn(f.conn.client, "getMetadata").mockResolvedValue(bp30Metadata());
    const requests = vi.spyOn(f.conn.client, "request").mockResolvedValue({ value: [] });
    vi.useFakeTimers();
    await f.conn.capabilities.start();
    const first = await f.conn.capabilities.status();
    expect(requests.mock.calls.length).toBeGreaterThan(3);
    expect(requests.mock.calls.length).toBeLessThanOrEqual(64);
    expect(first).toHaveProperty("probeCoverage");
    const initial = requests.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(requests.mock.calls.length).toBe(initial * 2);
  });
  it("у последней успешной схемы есть своя версия требований и профиль источника", async () => {
    const f = await setup();
    const report = await f.conn.capabilities.refresh();
    expect(report).toHaveProperty("observationProvenance");
    expect(Reflect.get(report, "observationProvenance")).toMatchObject({
      requirementsRevision: REQUIREMENTS.revision,
      sourceRef: REQUIREMENTS.sourceRef,
      profile: { configurationVersion: "unknown" },
    });
  });
});

describe("изменения формы контракта", () => {
  it("изменение типа используемого поля видно даже когда имя осталось", async () => {
    const f = await setup();
    await f.conn.capabilities.refresh();
    f.schema(
      xml(
        '<Property Name="Ref_Key" Type="Edm.Guid"/><Property Name="Description" Type="Edm.Int32"/><Property Name="ИНН" Type="Edm.String"/>',
      ),
    );
    const report = await f.conn.capabilities.refresh();
    expect(report.events).toContainEqual(
      expect.objectContaining({ kind: "changed", requirement: "organizations" }),
    );
    expect(report.requirements.organizations).toHaveProperty("observedFields.Description", "Edm.Int32");
  });
  it("отозванное объявление функции становится unknown и не исчезает из истории", async () => {
    const f = await setup();
    const fnXml = `<Edmx><DataServices><Schema Namespace="S"><EntityType Name="Register"/><ComplexType Name="B"><Property Name="Account_Key" Type="Edm.Guid"/><Property Name="СуммаBalanceDr" Type="Edm.Decimal"/><Property Name="СуммаBalanceCr" Type="Edm.Decimal"/></ComplexType><EntityContainer><EntitySet Name="AccountingRegister_Хозрасчетный" EntityType="S.Register"/><FunctionImport Name="Balance" ReturnType="Collection(S.B)" IsBindable="true"><Parameter Name="bindingParameter" Type="S.Register"/><Parameter Name="Period" Type="Edm.DateTime"/></FunctionImport></EntityContainer></Schema></DataServices></Edmx>`;
    f.schema(fnXml);
    await f.conn.capabilities.refresh();
    f.schema(fnXml.replace(/<FunctionImport[\s\S]*?<\/FunctionImport>/, ""));
    const report = await f.conn.capabilities.refresh();
    expect(report.requirements["accounting.balance"]?.status).toBe("unknown");
    expect(report.events).toContainEqual(
      expect.objectContaining({ kind: "newly_missing", requirement: "accounting.balance" }),
    );
  });
});

describe("ежедневное расписание по решению владельца", () => {
  it("по умолчанию проверяет раз в24часа и сохраняет ограниченный срок свидетельств", async () => {
    const f = await setup();
    expect(f.config.behavior.capabilityIntervalMs).toBe(86400000);
    expect((await f.conn.capabilities.refresh()).limits.observationTtlMs).toBeGreaterThan(86400000);
  });
  it("повторное подключение с тем же перечнем в пределах суток использует датированный отчёт", async () => {
    const first = await setup();
    await first.conn.capabilities.refresh();
    const firstReport = await first.conn.capabilities.status();
    const second = await setup({}, first.dir);
    await second.conn.capabilities.start();
    const reused = await second.conn.capabilities.status();
    expect(second.requests.length).toBe(0);
    expect(reused.summary.lastSuccessAt).toBe(firstReport.summary.lastSuccessAt);
  });
});

describe("повторное подключение и обновление требований", () => {
  it("назначает проверку на остаток суток после повторного подключения", async () => {
    vi.useFakeTimers();
    const baseTime = Date.parse("2026-10-01T00:00:00.000Z");
    vi.setSystemTime(baseTime);
    const first = await setup();
    await first.conn.capabilities.refresh();
    vi.setSystemTime(baseTime + 12 * 60 * 60 * 1000);
    const second = await setup({}, first.dir);
    await second.conn.capabilities.start();
    expect(second.requests.length).toBe(0);
    await vi.advanceTimersByTimeAsync(12 * 60 * 60 * 1000 - 1);
    expect(second.requests.length).toBe(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(second.requests.some((r) => r.url.endsWith("$metadata"))).toBe(true);
    await second.conn.capabilities.status();
  });
  it("изменённая существующая запись перечня обходит суточную задержку", async () => {
    const first = await setup();
    await first.conn.capabilities.refresh();
    const path = join(first.dir, (await readdir(first.dir))[0]!);
    const state = JSON.parse(await readFile(path, "utf8"));
    state.fingerprints["tool:read.system.list_organizations"] = "0".repeat(64);
    await writeFile(path, JSON.stringify(state));
    const second = await setup({}, first.dir);
    await second.conn.capabilities.start();
    const report = await second.conn.capabilities.status();
    expect(second.requests.some((r) => r.url.endsWith("$metadata"))).toBe(true);
    expect(report.events).toContainEqual(
      expect.objectContaining({ kind: "newly_required", requirement: "tool:read.system.list_organizations" }),
    );
  });
  it("одновременные refresh разделяют один запрос metadata", async () => {
    const f = await setup();
    await f.conn.capabilities.status();
    let release!: (value: ReturnType<typeof parseMetadata>) => void;
    const pending = new Promise<ReturnType<typeof parseMetadata>>((resolve) => {
      release = resolve;
    });
    const metadata = vi.spyOn(f.conn.client, "getMetadata").mockReturnValue(pending);
    const a = f.conn.capabilities.refresh();
    const b = f.conn.capabilities.refresh();
    await Promise.resolve();
    await Promise.resolve();
    expect(metadata).toHaveBeenCalledTimes(1);
    release(parseMetadata(xml()));
    const [one, two] = await Promise.all([a, b]);
    expect(one.summary.lastSuccessAt).toBe(two.summary.lastSuccessAt);
  });
});

describe("API диагностики и настройка профиля", () => {
  it("три API дают filtered inventory, сохранённый статус и принудительный read-only refresh", async () => {
    const f = await setup();
    const tools = snapshotTools(f.conn);
    const requirement = await tools["read.system.odata_requirements"]!.handler(
      { tool: "read.system.list_organizations" },
      {},
    );
    expect(requirement.isError).not.toBe(true);
    expect(requirement.structuredContent?.tools).toEqual([
      expect.objectContaining({ name: "read.system.list_organizations" }),
    ]);
    const before = await tools["read.system.odata_capabilities"]!.handler({}, {});
    expect(before.structuredContent?.summary).toMatchObject({
      metadataStatus: "unknown",
      lastSuccessAt: null,
    });
    expect(f.requests).toEqual([]);
    const refresh = await tools["read.system.odata_refresh"]!.handler({}, {});
    expect(refresh.isError).not.toBe(true);
    expect(refresh.structuredContent?.summary).toMatchObject({ metadataStatus: "ok" });
    expect(f.requests.every((r) => r.method === "GET")).toBe(true);
    const after = await tools["read.system.odata_capabilities"]!.handler({}, {});
    expect(after.structuredContent?.summary).toMatchObject({ metadataStatus: "ok" });
    const invalid = await tools["read.system.odata_requirements"]!.handler({ tool: "not-a-tool" }, {});
    expect(invalid.isError).toBe(true);
  });
  it("неверный JSON профиля отклоняется, пустое значение использует generic", () => {
    const base = {
      ODATA_BASE_URL: "https://example.invalid/odata/",
      ODATA_USERNAME: "u",
      ODATA_PASSWORD: "p",
    };
    expect(() => parseConfig({ ...base, ODATA_CAPABILITY_PROFILE: "{invalid" })).toThrow(
      "JSON-настройка профиля",
    );
    expect(
      parseConfig({ ...base, ODATA_CAPABILITY_PROFILE: " " }).connections[0]!.capabilityProfile,
    ).toBeUndefined();
    expect(parseConfig(base).behavior.capabilityProbeLimit).toBe(64);
  });
});

const registerName = "AccountingRegister_Хозрасчетный";
const functionXml = (parameterType = "Edm.DateTime") =>
  `<Edmx><DataServices><Schema Namespace="S"><EntityType Name="Register"/><ComplexType Name="B"><Property Name="Account_Key" Type="Edm.Guid"/><Property Name="СуммаBalanceDr" Type="Edm.Decimal"/><Property Name="СуммаBalanceCr" Type="Edm.Decimal"/></ComplexType><EntityContainer><EntitySet Name="${registerName}" EntityType="S.Register"/><FunctionImport Name="Balance" ReturnType="Collection(S.B)" IsBindable="true"><Parameter Name="bindingParameter" Type="S.Register"/><Parameter Name="Period" Type="${parameterType}"/></FunctionImport></EntityContainer></Schema></DataServices></Edmx>`;

describe("регрессии независимой проверки", () => {
  it.each(["register", "chart"])(
    "R1: изменение accounting.%s обновляет тот же principal сразу",
    async (key) => {
      const first = await setup();
      const old = await first.conn.capabilities.refresh();
      const target = key === "register" ? "AccountingRegister_Custom" : "ChartOfAccounts_Custom";
      const second = await setup({ ODATA_ACCOUNTING_CONFIG: JSON.stringify({ [key]: target }) }, first.dir);
      second.schema(xml("", `<EntitySet Name="${target}" EntityType="S.Catalog_Организации"/>`));
      await second.conn.capabilities.start();
      const report = await second.conn.capabilities.status();
      expect(second.requests.some((r) => r.url.endsWith("$metadata"))).toBe(true);
      expect(report.scope).toBe(old.scope);
      expect(report.requirements[`accounting.${key}`]?.entitySet).toBe(target);
      expect(report.events).toContainEqual(
        expect.objectContaining({ kind: "newly_required", requirement: `accounting.${key}` }),
      );
    },
  );
  it.each(["mcpVersion", "sourceVersion", "sourceRef", "requirementsRevision", "revision"])(
    "R2: изменение %s обходит суточный cache при прежних content hashes",
    async (key) => {
      const first = await setup();
      await first.conn.capabilities.refresh();
      const path = join(first.dir, (await readdir(first.dir))[0]!);
      const state = JSON.parse(await readFile(path, "utf8"));
      if (key === "revision") state.revision = "older";
      else state.provenance[key] = "older";
      await writeFile(path, JSON.stringify(state));
      const second = await setup({}, first.dir);
      await second.conn.capabilities.start();
      expect(second.requests.some((r) => r.url.endsWith("$metadata"))).toBe(true);
      const report = await second.conn.capabilities.status();
      expect(report.observationProvenance).toMatchObject({
        mcpVersion: "0.10.0",
        requirementsRevision: REQUIREMENTS.revision,
        sourceVersion: REQUIREMENTS.sourceVersion,
        sourceRef: REQUIREMENTS.sourceRef,
      });
    },
  );
  it("R3/R5: parameterless Balance отдельно от root, точный отказ и восстановление видны администратору и в drift", async () => {
    const f = await setup();
    f.schema(functionXml());
    await f.conn.capabilities.refresh();
    await balanceByAccounts(f.conn, null);
    let report = await f.conn.capabilities.status();
    expect(report.requirements["accounting.balance"]?.operations[0]?.evidence.effectiveStatus).toBe(
      "observed_success",
    );
    expect(report.requirements["accounting.register"]?.operations[0]?.evidence.effectiveStatus).toBe(
      "unknown",
    );
    f.response(500, '{"odata.error":{"message":{"value":"Нарушение прав доступа!"}}}');
    await expect(f.conn.client.request(`${registerName}/Balance`)).rejects.toThrow();
    report = await f.conn.capabilities.status();
    expect(report.requirements["accounting.balance"]?.status).toBe("published");
    expect(report.administratorRequest).toContain(
      `${registerName}; функция Balance: операция read; наблюдён явный отказ`,
    );
    expect(report.events).toContainEqual(
      expect.objectContaining({ kind: "newly_missing", requirement: `${registerName}:Balance:read` }),
    );
    f.response(200, '{"value":[]}');
    await f.conn.client.request(`${registerName}/Balance(Period=datetime'2026-01-01T00:00:00')?$top=0`);
    expect((await f.conn.capabilities.status()).events).toContainEqual(
      expect.objectContaining({ kind: "restored", requirement: `${registerName}:Balance:read` }),
    );
    await f.conn.client.request(`${registerName}?$top=0`);
    expect((await f.conn.capabilities.status()).operations).toHaveLength(2);
  });
  it("R5: опубликованный параметризованный Balance даёт точный запрос и identity отказа", async () => {
    const f = await setup();
    f.schema(functionXml());
    await f.conn.capabilities.refresh();
    f.response(500, '{"odata.error":{"message":{"value":"Нарушение прав доступа!"}}}');
    await expect(
      f.conn.client.request(`${registerName}/Balance(Period=datetime'2026-01-01T00:00:00')?$top=0`),
    ).rejects.toThrow();
    const report = await f.conn.capabilities.status();
    expect(report.administratorRequest).toContain(
      `${registerName}; функция Balance: операция read; наблюдён явный отказ`,
    );
    expect(report.events).toContainEqual(
      expect.objectContaining({ kind: "newly_missing", requirement: `${registerName}:Balance:read` }),
    );
  });
  it("R3/R5: bound Post/Unpost сохраняют точную identity отдельно от GET и Balance", async () => {
    const f = await setup({ READ_ONLY: "false", ODATA_WRITABLE: "true" });
    f.schema(xml("", '<EntitySet Name="Document_Test" EntityType="S.Catalog_Организации"/>'));
    await f.conn.capabilities.refresh();
    f.response(500, '{"odata.error":{"message":{"value":"Нарушение прав доступа!"}}}');
    for (const action of ["Post", "Unpost"])
      await expect(
        f.conn.client.request(`Document_Test(guid'synthetic-key')/${action}`, "POST"),
      ).rejects.toThrow();
    const report = await f.conn.capabilities.status();
    expect(report.administratorRequest).toContain("Document_Test: операция post");
    expect(report.administratorRequest).toContain("Document_Test: операция unpost");
    expect(report.events.map((e) => e.requirement)).toEqual(
      expect.arrayContaining(["Document_Test:post", "Document_Test:unpost"]),
    );
    expect(report.operations.every((o) => o.functionName === undefined && o.operation !== "read")).toBe(true);
  });
  it("R4: известный тип Period проверяется, неизвестный остаётся явно unknown", async () => {
    const f = await setup();
    const req = REQUIREMENTS.definitions["accounting.balance"]!;
    expect(req.function?.parameters).toEqual([{ name: "Period", type: "Edm.DateTime", optional: true }]);
    const incompatible = inspectRequirement(req, f.conn.cfg, parseMetadata(functionXml("Edm.Int32")));
    expect(incompatible).toMatchObject({
      status: "schema_incompatible",
      missing: ["parameter:Period:Edm.DateTime"],
    });
    const unknown = inspectRequirement(
      {
        ...req,
        function: { name: "Balance", parameters: [{ name: "Period", type: "unknown" }] },
      } as typeof req,
      f.conn.cfg,
      parseMetadata(functionXml()),
    );
    expect(unknown).toMatchObject({ status: "unknown", unknown: ["parameter_type:Period"] });
  });
  it.each([
    ["write.catalog.update_nomenclature", { name: "sample", fullName: "sample", article: "sample" }],
    [
      "write.counterparty.update_counterparty",
      { name: "sample", inn: "sample", kpp: "sample", fullName: "sample" },
    ],
  ] as const)(
    "R6: %s описывает реальные optional preview fields и отдельно их schema",
    async (name, args) => {
      const f = await setup();
      f.schema(
        xml(
          '<Property Name="Ref_Key" Type="Edm.Guid"/>',
          '<EntitySet Name="Catalog_Номенклатура" EntityType="S.Catalog_Организации"/><EntitySet Name="Catalog_Контрагенты" EntityType="S.Catalog_Организации"/>',
        ),
      );
      const preview = await snapshotTools(f.conn)[name]!.handler(
        { ref: "11111111-1111-4111-8111-111111111111", confirm: false, ...args },
        {},
      );
      expect(preview.isError).not.toBe(true);
      const fields = Object.keys(preview.structuredContent!.fields as object);
      const record = REQUIREMENTS.tools.find((t) => t.name === name)!;
      expect(Object.keys(record.mutationFieldModel?.conditionalSent ?? {}).sort()).toEqual(
        expect.arrayContaining(fields.sort()),
      );
      expect(record.mutationFieldModel?.alwaysSent).toEqual([]);
      await f.conn.capabilities.refresh();
      const diagnostic = (await f.conn.capabilities.status()).tools.find(
        (t) => t.name === name,
      )!.mutationSchema;
      expect(diagnostic?.alwaysSent?.status).toBe("published");
      for (const field of fields)
        expect(diagnostic?.conditional).toContainEqual(
          expect.objectContaining({
            field,
            applicability: "unknown",
            schema: expect.objectContaining({ status: "schema_incompatible", missing: [field] }),
          }),
        );
      expect(f.requests.every((r) => r.method === "GET")).toBe(true);
    },
  );
  it("R6: контакты и дополнительные реквизиты отражают поля безопасного preview без обязательности для простого имени", async () => {
    const conn = snapshotConnection();
    const available = await conn.available();
    vi.spyOn(conn, "available").mockResolvedValue(
      new Set([...available, "ChartOfCharacteristicTypes_ДополнительныеРеквизитыИСведения"]),
    );
    vi.spyOn(conn.client, "getCollection").mockImplementation(async (path: string) => {
      if (path.startsWith("Catalog_ВидыКонтактнойИнформации"))
        return {
          value: path.includes("Parent_Key")
            ? [
                { Ref_Key: "11111111-1111-4111-8111-111111111111", Description: "Телефон", Тип: "Телефон" },
                {
                  Ref_Key: "22222222-2222-4222-8222-222222222222",
                  Description: "Email",
                  Тип: "АдресЭлектроннойПочты",
                },
                {
                  Ref_Key: "33333333-3333-4333-8333-333333333333",
                  Description: "Юридический адрес",
                  Тип: "Адрес",
                },
              ]
            : [{ Ref_Key: "44444444-4444-4444-8444-444444444444" }],
        };
      return { value: [{ Ref_Key: "55555555-5555-4555-8555-555555555555" }] };
    });
    vi.spyOn(conn.client, "getEntity").mockResolvedValue({ КонтактнаяИнформация: [] });
    const preview = await snapshotTools(conn)["write.counterparty.update_counterparty"]!.handler(
      {
        ref: "11111111-1111-4111-8111-111111111111",
        phone: "sample",
        email: "sample",
        address: "sample",
        ogrn: "sample",
        confirm: false,
      },
      {},
    );
    expect(preview.isError).not.toBe(true);
    const fields = preview.structuredContent!.fields as Record<string, Array<Record<string, unknown>>>;
    const model = REQUIREMENTS.tools.find(
      (t) => t.name === "write.counterparty.update_counterparty",
    )!.mutationFieldModel;
    for (const [section, rows] of Object.entries(fields)) {
      expect(model?.conditionalSent).toHaveProperty(section);
      const rowModel = model?.rows?.[section];
      const declared = [...(rowModel?.alwaysSent ?? []), ...Object.keys(rowModel?.conditionalSent ?? {})];
      for (const row of rows) expect(declared).toEqual(expect.arrayContaining(Object.keys(row)));
    }
    expect(Object.keys(fields).sort()).toEqual(["ДополнительныеРеквизиты", "КонтактнаяИнформация"].sort());
  });
});
