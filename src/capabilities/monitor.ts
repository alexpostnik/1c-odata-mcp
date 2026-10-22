import { mutationDiagnostics } from "./diagnose.js";
import { join } from "node:path";
import { homedir } from "node:os";
import type { Behavior, ConnectionConfig } from "../config/env.js";
import type { ODataClient } from "../odata/client.js";
import { ODataError } from "../odata/errors.js";
import type { MetadataMap } from "../types/odata.js";
import { DEFAULT_PROFILE } from "./config.js";
import { REQUIREMENTS, fingerprint } from "./requirements.js";
import { inspectRequirement, type SchemaObservation } from "./schema.js";
import {
  CapabilityStore,
  emptyState,
  type CapabilityEvent,
  type CapabilityState,
  type OperationObservation,
} from "./store.js";

export interface RequestObservation {
  entitySet: string;
  functionName?: string;
  operation: OperationObservation["operation"];
  scope: OperationObservation["scope"];
  error?: ODataError;
}
const OBSERVATION_TTL_MS = 24 * 60 * 60 * 1000;
const entityName = /^[A-Za-z][\p{L}\p{N}_]{1,180}$/u;

/** HTTP 403/404, сеть и обычный 500 не доказывают отказ роли 1С. */
export function classifyObservation(error?: ODataError): Pick<OperationObservation, "status" | "reason"> {
  if (!error) return { status: "observed_success", reason: "request_succeeded" };
  if (error.permissionDenied) return { status: "observed_denied", reason: "explicit_1c_permission_denial" };
  if (error.kind === "auth") return { status: "unknown", reason: "authentication_or_service" };
  if (error.kind === "not_found") return { status: "unknown", reason: "not_found_ambiguous" };
  if (error.kind === "bad_request") return { status: "unknown", reason: "query_or_schema" };
  return { status: "unknown", reason: "service_or_transport" };
}

/** Пер-база/пользователь: сохраняет только имена контрактов, классификации и времена. */
export class CapabilityMonitor {
  private readonly store: CapabilityStore;
  private state: CapabilityState;
  private loaded?: Promise<void>;
  private saving: Promise<void> = Promise.resolve();
  private inFlight?: Promise<ReturnType<CapabilityMonitor["report"]>>;
  private metadata?: MetadataMap;
  private knownEntities = new Set<string>();
  private persistence: "new" | "loaded" | "saved" | "unavailable" = "new";
  private active = false;
  private cursor = 0;
  private timer?: ReturnType<typeof setInterval>;
  private stopped = false;
  private notify?: (events: CapabilityEvent[]) => void;

  constructor(
    private readonly cfg: ConnectionConfig,
    private readonly behavior: Behavior,
    private readonly client: ODataClient,
  ) {
    // Пароль исключён: смена пароля не меняет principal; URL/логин не попадают ни в файл, ни в отчёт.
    const url = new URL(cfg.baseUrl);
    url.username = "";
    url.password = "";
    url.hash = "";
    const scope = fingerprint([url.toString().replace(/\/$/, ""), cfg.username]);
    this.state = emptyState(scope);
    this.store = new CapabilityStore(
      behavior.capabilityStateDir ?? join(homedir(), ".1c-odata-mcp", "capabilities"),
      scope,
    );
    for (const req of Object.values(REQUIREMENTS.definitions))
      for (const target of req.target) this.knownEntities.add(target);
  }

  private load(): Promise<void> {
    this.loaded ??= this.store.load().then(({ state, status }) => {
      this.state = state;
      this.persistence = status;
    });
    return this.loaded;
  }
  private persist(): Promise<void> {
    const snapshot = structuredClone(this.state);
    this.saving = this.saving.then(async () => {
      try {
        await this.store.save(snapshot);
        this.persistence = "saved";
      } catch {
        this.persistence = "unavailable";
      }
    });
    return this.saving;
  }
  private append(events: CapabilityEvent[]): void {
    this.state.events = [...this.state.events, ...events].slice(-1000);
    if (events.length && !this.stopped) this.notify?.(events);
  }

  /** Вызывается только для уже произошедшего обычного запроса, автоматической записи нет. */
  async observe(input: RequestObservation): Promise<void> {
    if (!this.active || !entityName.test(input.entitySet) || !this.knownEntities.has(input.entitySet)) return;
    if (input.functionName && !Object.values(REQUIREMENTS.definitions).some((req) => req.function?.name === input.functionName)) return;
    await this.load();
    const at = new Date().toISOString();
    const observation: OperationObservation = {
      ...classifyObservation(input.error),
      entitySet: input.entitySet,
      ...(input.functionName ? { functionName: input.functionName } : {}),
      operation: input.operation,
      scope: input.scope,
      at,
      expiresAt: new Date(Date.now() + OBSERVATION_TTL_MS).toISOString(),
    };
    if (input.error?.kind === "not_found" && input.scope === "one_record")
      observation.reason = "record_not_found";
    const previous = this.state.operations.find(
      (o) => o.entitySet === input.entitySet && o.operation === input.operation && o.functionName === input.functionName,
    );
    this.state.operations = [
      ...this.state.operations.filter(
        (o) => o.entitySet !== input.entitySet || o.operation !== input.operation || o.functionName !== input.functionName,
      ),
      observation,
    ].slice(-5000);
    observation.lastSuccessAt = observation.status === "observed_success" ? at : previous?.lastSuccessAt;
    if (previous && previous.status !== observation.status)
      this.append([
        {
          at,
          kind:
            observation.status === "observed_success"
              ? "restored"
              : observation.status === "observed_denied"
                ? "newly_missing"
                : "observation_failed",
          requirement: `${input.entitySet}:${input.operation}`,
          before: previous.status,
          after: observation.status,
        },
      ]);
    else if (!previous && observation.status !== "observed_success")
      this.append([
        {
          at,
          kind: observation.status === "observed_denied" ? "newly_missing" : "observation_failed",
          requirement: `${input.entitySet}:${input.operation}`,
          after: observation.status,
        },
      ]);
    await this.persist();
  }

  async status() {
    await this.load();
    await this.saving;
    return this.report();
  }
  summary() {
    return {
      metadataStatus: this.state.metadataStatus,
      lastAttemptAt: this.state.lastAttemptAt ?? null,
      lastSuccessAt: this.state.lastSuccessAt ?? null,
      missing: Object.values(this.state.latest).filter((o) => o.status !== "published").length,
      events: this.state.events.length,
      persistence: this.persistence,
      diagnosticsTool: "read.system.odata_capabilities",
    };
  }

  report() {
    const known = Object.fromEntries(
      Object.entries(REQUIREMENTS.definitions).map(([id, req]) => [
        id,
        this.state.latest[id] ?? inspectRequirement(req, this.cfg),
      ]),
    );
    const current =
      this.state.metadataStatus === "ok"
        ? known
        : Object.fromEntries(
            Object.entries(known).map(([id, o]) => [
              id,
              { ...o, status: "unknown" as const, unknown: ["current_metadata_unavailable"] },
            ]),
          );
    const operations = this.state.operations.map((o) => ({
      ...o,
      effectiveStatus: Date.parse(o.expiresAt) > Date.now() ? o.status : "unknown",
      expired: Date.parse(o.expiresAt) <= Date.now(),
    }));
    const requirements = Object.fromEntries(
      Object.entries(current).map(([id, schema]) => [
        id,
        {
          ...schema,
          operations: ((REQUIREMENTS.definitions[id]?.operations as string[] | undefined) ?? []).map(
            (operation) => ({
              operation,
              evidence: operations.find(
                (o) => o.entitySet === schema.entitySet && o.operation === operation && o.functionName === REQUIREMENTS.definitions[id]?.function?.name,
              ) ?? { effectiveStatus: "unknown" },
            }),
          ),
        },
      ]),
    );
    return {
      requirementsRevision: REQUIREMENTS.revision,
      sourceVersion: REQUIREMENTS.sourceVersion,
      sourceRef: REQUIREMENTS.sourceRef,
      scope: this.state.scope,
      profile: this.cfg.capabilityProfile ?? DEFAULT_PROFILE,
      summary: this.summary(),
      localWritePolicy: {
        readOnly: this.behavior.readOnly,
        writable: this.cfg.writable,
        serverRights: "unknown",
      },
      requirements,
      lastSuccessfulSchema: this.state.latest,
      observationProvenance: this.state.provenance ?? null,
      probeCoverage: this.state.probeCoverage ? { ...this.state.probeCoverage, periodicWorstCaseMs: this.state.probeCoverage.periodicBudget ? Math.ceil(this.state.probeCoverage.candidateCount / this.state.probeCoverage.periodicBudget) * (this.behavior.capabilityIntervalMs ?? 900_000) : null, note: "Последний проход; checked означает попытку ограниченного запроса, а не наличие прав на строки. При меньшем бюджете объекты проверяются по кругу; к интервалу добавляется время запросов." } : null,
      operations,
      events: this.state.events,
      tools: REQUIREMENTS.tools.map((t) => ({
        name: t.name,
        required: t.required.map((id) => ({ id, status: current[id]?.status ?? "unknown" })),
        conditional: t.conditional,
        optional: t.optional,
        dynamic: t.dynamic,
        mutationFieldModel: t.mutationFieldModel,
        mutationSchema: mutationDiagnostics(
          t,
          this.cfg,
          this.state.metadataStatus === "ok" ? this.metadata : undefined,
        ),
        invocationStatus: t.localOnly ? "local" : "unknown",
        note: "Условные поля и динамические объекты проверяются для выбранных аргументов; схема и успешный GET не доказывают права записи.",
      })),
      administratorRequest: this.adminRequest(current),
      limits: {
        initialProbeLimit: 64,
        probeKind: "top_zero_query_only",
        probeLimit: this.behavior.capabilityProbeLimit ?? 64,
        intervalMs: this.behavior.capabilityIntervalMs ?? 900_000,
        observationTtlMs: OBSERVATION_TTL_MS,
        recordDataStored: false,
        allRowPermissionsProven: false,
        historyLimit: 1000,
      },
    };
  }

  private adminRequest(observations: Record<string, SchemaObservation>): string {
    const lines = Object.entries(observations)
      .filter(([, o]) => o.status !== "published" || o.factConflict)
      .map(([id, o]) => {
        const req = REQUIREMENTS.definitions[id];
        return `• ${id}: ${o.candidates.join(" или ")}; операции: ${((req?.operations as string[] | undefined) ?? []).join(", ") || "metadata"}${req?.function ? `; функция ${req.function.name}(${req.function.parameters.join(", ")})` : ""}; поля/ключи: ${[...(Array.isArray(req?.fields) ? req.fields : Object.keys(req?.fields ?? {})), ...(req?.keys ?? []).map((k) => `key:${k}`)].join(", ") || "см. требования инструмента"}; состояние: ${o.status}${o.missing.length ? `; несовместимо: ${o.missing.join(", ")}` : ""}${o.factConflict ? "; факт администратора противоречит текущей схеме" : ""}`;
      });
    for (const o of this.state.operations.filter(
      (x) => x.status === "observed_denied" && Date.parse(x.expiresAt) > Date.now(),
    ))
      lines.push(
        `• ${o.entitySet}: операция ${o.operation}; наблюдён явный отказ роли 1С (${o.at}), только для одного запроса.`,
      );
    return (
      "Установка/обновление MCP не настраивает публикацию и роли 1С. Просьба проверить существование перечисленных объектов в указанной конфигурации/версии, затем их публикацию в стандартном OData и отдельно права пользователя на нужные операции. Если объект не реализован, подтвердите конфигурацию, версию и источник факта. Право изменять состав публикации — административное и отличается от права читать/создавать/изменять/проводить данные. При наличии административных прав откройте «Настройка стандартного интерфейса OData → Состав», проверьте точные имена и роли; иначе передайте запрос администратору/поддержке. MCP публикацию не меняет.\n" +
      lines.join("\n")
    );
  }

  async refresh(probeEntities?: string[], fullPass = false) {
    this.active = true;
    if (this.inFlight) return this.inFlight;
    this.inFlight = this.scan(probeEntities, fullPass).finally(() => {
      this.inFlight = undefined;
    });
    return this.inFlight;
  }
  private async scan(probeEntities?: string[], fullPass = false) {
    await this.load();
    const at = new Date().toISOString();
    const previousAttemptAt = this.state.lastAttemptAt;
    this.state.lastAttemptAt = at;
    const oldMetadataStatus = this.state.metadataStatus;
    const events: CapabilityEvent[] = [];
    const signatures = Object.fromEntries(
      [
        ...Object.entries(REQUIREMENTS.definitions),
        ...REQUIREMENTS.tools.map((t) => [`tool:${t.name}`, t] as const),
      ].map(([id, req]) => [id, fingerprint(req)]),
    );
    if (this.state.revision !== "unknown")
      for (const [id, signature] of Object.entries(signatures))
        if (this.state.fingerprints[id] !== signature)
          events.push({ at, kind: "newly_required", requirement: id, after: REQUIREMENTS.revision });
    try {
      this.metadata = await this.client.getMetadata(true);
      this.knownEntities = new Set(this.metadata.entities.keys());
      const latest = Object.fromEntries(
        Object.entries(REQUIREMENTS.definitions).map(([id, req]) => [
          id,
          inspectRequirement(req, this.cfg, this.metadata),
        ]),
      );
      for (const [id, now] of Object.entries(latest)) {
        const before = this.state.latest[id];
        if (!before && now.status !== "published")
          events.push({ at, kind: "newly_missing", requirement: id, after: now.status });
        if (before && fingerprint(before) !== fingerprint(now))
          events.push({
            at,
            kind:
              now.status === "published" && before.status !== "published"
                ? "restored"
                : before.status === "published" && now.status !== "published"
                  ? "newly_missing"
                  : "changed",
            requirement: id,
            before: before.status,
            after: now.status,
          });
      }
      this.state.latest = latest;
      this.state.metadataStatus = "ok";
      this.state.lastSuccessAt = at;
      this.state.provenance = { requirementsRevision: REQUIREMENTS.revision, sourceVersion: REQUIREMENTS.sourceVersion, sourceRef: REQUIREMENTS.sourceRef, profile: this.cfg.capabilityProfile ?? DEFAULT_PROFILE };
      if (
        oldMetadataStatus === "unknown" &&
        this.state.lastSuccessAt &&
        Object.keys(this.state.fingerprints).length
      )
        events.push({ at, kind: "observation_restored", requirement: "metadata", after: "ok" });
      const limit = fullPass ? 64 : (this.behavior.capabilityProbeLimit ?? 64);
      const candidates = [
        ...new Set(
          Object.entries(latest)
            .filter(([id]) =>
              (REQUIREMENTS.definitions[id]?.operations as string[] | undefined)?.includes("read"),
            )
            .map(([, value]) => value)
            .filter((o) => o.status === "published")
            .map((o) => o.entitySet!),
        ),
      ].filter((name) => !name.startsWith("Enum_") && this.metadata!.entities.get(name)?.keys.length);
      const selected =
        probeEntities ??
        Array.from(
          { length: Math.min(limit, candidates.length) },
          (_, n) => candidates[(this.cursor + n) % candidates.length]!,
        );
      if (
        selected.length > 64 ||
        selected.some((name) => !entityName.test(name) || !this.metadata!.entities.has(name))
      )
        throw new Error("invalid_probe_selection");
      const checked: string[] = [];
      for (const entity of selected) {
        if (this.stopped) break;
        const key = this.metadata.entities.get(entity)?.keys[0];
        if (!key || !entityName.test(key)) continue;
        checked.push(entity);
        try {
          await this.client.request(`${entity}?$top=0&$select=${encodeURIComponent(key)}&$format=json`, "GET", undefined, {}, { singleAttempt: true });
        } catch {
          /* Клиент записывает лишь классификацию; один отказ не останавливает остальные. */
        }
      }
      this.state.probeCoverage = { at, candidateCount: candidates.length, checked, fullPass, periodicBudget: this.behavior.capabilityProbeLimit ?? 64 };
      this.cursor = candidates.length ? (this.cursor + selected.length) % candidates.length : 0;
    } catch {
      this.state.metadataStatus = "unknown";
      if (oldMetadataStatus === "ok" || !previousAttemptAt)
        events.push({
          at,
          kind: "observation_failed",
          requirement: "metadata",
          before: oldMetadataStatus,
          after: "unknown",
        });
    }
    this.state.revision = REQUIREMENTS.revision;
    this.state.fingerprints = signatures;
    this.append(events);
    await this.persist();
    return this.report();
  }

  start(notify?: (events: CapabilityEvent[]) => void): void {
    if (this.timer) return;
    this.stopped = false;
    this.notify = notify;
    void this.refresh(undefined, true);
    this.timer = setInterval(() => {
      if (!this.stopped) void this.refresh();
    }, this.behavior.capabilityIntervalMs ?? 900_000);
    this.timer.unref();
  }
  stop(): void {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }
}
