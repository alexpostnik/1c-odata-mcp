import type { Connection } from "../context.js";
import { resolveEntity } from "../config/mapping.js";

/**
 * Ошибка «объект не опубликован в OData». Возникает, когда инструмент требует
 * объект 1С, которого нет в «Составе OData». guard() превращает её в вежливый
 * ответ со списком того, что нужно добавить в настройку — вместо технической ошибки.
 */
export class NotPublishedError extends Error {
  readonly missing: Array<{ label: string; candidates: readonly string[] }>;
  constructor(missing: Array<{ label: string; candidates: readonly string[] }>) {
    super("Требуемые объекты не видны в доступной схеме OData");
    this.name = "NotPublishedError";
    this.missing = missing;
  }
}

/**
 * Возвращает имя опубликованного EntitySet среди кандидатов или бросает
 * NotPublishedError с человекочитаемым ярлыком.
 */
export async function requireEntity(
  conn: Connection,
  candidates: readonly string[],
  label: string,
): Promise<string> {
  const set = resolveEntity(candidates, await conn.available());
  if (!set) throw new NotPublishedError([{ label, candidates }]);
  return set;
}

/** Проверяет, что конкретный EntitySet опубликован (для имён, заданных пользователем). */
export function ensurePublished(available: ReadonlySet<string>, entitySet: string, label?: string): void {
  if (!available.has(entitySet)) {
    throw new NotPublishedError([{ label: label ?? entitySet, candidates: [entitySet] }]);
  }
}

/** Собирает текст вежливого фоллбэка по списку недостающих объектов. */
export function publicationHelp(missing: NotPublishedError["missing"]): string {
  const lines = missing.map((m) => `  • ${m.label} (${m.candidates.join(" или ")})`).join("\n");
  return (
    "Эти объекты не видны в доступной схеме OData; причина и наличие в конфигурации пока неизвестны. " +
    "Установка MCP не меняет публикацию и роли 1С. Проверьте точные имена и версию конфигурации с администратором/поддержкой; " +
    "если объекты реализованы и у вас есть административные права, проверьте «Все функции → Обработки → Настройка стандартного интерфейса OData → вкладка Состав». " +
    "Права на публикацию и на чтение/запись данных различаются. Подробности: read.system.odata_capabilities.\n" +
    lines
  );
}
