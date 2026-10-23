import type { Connection } from "../context.js";
import { resolveEntity } from "../config/mapping.js";

/**
 * Требуемый объект отсутствует в доступной OData-схеме. guard() превращает ошибку
 * в подсказку по «Составу», имени объекта, конфигурации и правам.
 */
export class NotPublishedError extends Error {
  readonly missing: Array<{ label: string; candidates: readonly string[] }>;
  constructor(missing: Array<{ label: string; candidates: readonly string[] }>) {
    super("Требуемые объекты отсутствуют в доступной OData-схеме");
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
    "Эти объекты 1С отсутствуют в доступной OData-схеме, поэтому операция недоступна. " +
    "Добавьте их в 1С: «Все функции → Обработки → Настройка стандартного интерфейса OData → вкладка Состав», затем сохраните:\n" +
    lines +
    "\nВозможны также другое имя объекта, его отсутствие в этой конфигурации или ограничения прав."
  );
}
