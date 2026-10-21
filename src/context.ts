import type { Behavior, ConnectionConfig, RuntimeConfig } from "./config/env.js";
import { ODataClient } from "./odata/client.js";
import { ODataError } from "./odata/errors.js";
import type { MetadataMap } from "./types/odata.js";

/**
 * Одно подключение к конкретной базе 1С: клиент + лениво-кешируемая карта
 * метаданных этой базы. У каждой базы свой кеш $metadata.
 */
export class Connection {
  readonly cfg: ConnectionConfig;
  readonly behavior: Behavior;
  readonly client: ODataClient;
  readonly regulatedCurrencySource: "database" | "global" | "default";

  constructor(cfg: ConnectionConfig, behavior: Behavior) {
    this.cfg = cfg;
    this.behavior = {
      ...behavior,
      regulatedCurrencyCode: cfg.regulatedCurrencyCode ?? behavior.regulatedCurrencyCode ?? "643",
    };
    this.regulatedCurrencySource = cfg.regulatedCurrencyCode
      ? "database"
      : (behavior.regulatedCurrencySource ?? (behavior.regulatedCurrencyCode ? "global" : "default"));
    this.client = new ODataClient(cfg, this.behavior);
  }

  getMetadata(): Promise<MetadataMap> {
    return this.client.getMetadata();
  }

  async available(): Promise<ReadonlySet<string>> {
    const meta = await this.getMetadata();
    return new Set(meta.entities.keys());
  }
}

/**
 * Реестр всех настроенных баз. Инструменты получают его и выбирают базу
 * методом db(name): без имени — база по умолчанию.
 */
export class ServerContext {
  readonly defaultName: string;
  readonly behavior: Behavior;
  private readonly map = new Map<string, Connection>();

  constructor(rc: RuntimeConfig) {
    this.defaultName = rc.defaultName;
    this.behavior = rc.behavior;
    for (const c of rc.connections) this.map.set(c.name, new Connection(c, rc.behavior));
  }

  /** Подключение по имени; без имени — база по умолчанию. Бросает понятную ошибку. */
  db(name?: string): Connection {
    const key = (name ?? this.defaultName).toLowerCase();
    const conn = this.map.get(key);
    if (!conn) {
      const known = [...this.map.keys()].join(", ");
      throw new ODataError({
        kind: "bad_request",
        message: `База "${name}" не настроена. Доступные базы: ${known}. См. list_databases.`,
      });
    }
    return conn;
  }

  /** Список баз для list_databases. */
  databases(): Array<{ name: string; label?: string; isDefault: boolean }> {
    return [...this.map.values()].map((c) => ({
      name: c.cfg.name,
      ...(c.cfg.label ? { label: c.cfg.label } : {}),
      isDefault: c.cfg.name === this.defaultName,
    }));
  }
}
