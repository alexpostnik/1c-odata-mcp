import { readFileSync } from "node:fs";
import { parseMetadata } from "../../src/odata/metadata.js";
import type { MetadataMap } from "../../src/types/odata.js";

/**
 * Снимок $metadata типовой Бухгалтерии 3.0 (облако, октябрь 2026): только объекты, которые использует
 * коннектор, их табличные части и все перечисления. Данных базы в нём нет — только схема.
 * Обновить: снять $metadata с базы и отфильтровать по именам объектов из src (см. CONTRIBUTING).
 */
export const BP30_METADATA_XML = readFileSync(
  new URL("../fixtures/metadata/bp30.xml", import.meta.url),
  "utf8",
);

let parsed: MetadataMap | undefined;
export function bp30Metadata(): MetadataMap {
  return (parsed ??= parseMetadata(BP30_METADATA_XML));
}

/** Значения перечислений снимка: имя перечисления → множество значений. */
export function bp30Enums(): Map<string, Set<string>> {
  const enums = new Map<string, Set<string>>();
  for (const m of BP30_METADATA_XML.matchAll(/<EnumType Name="([^"]+)"[\s\S]*?<\/EnumType>/g))
    enums.set(m[1]!, new Set([...m[0].matchAll(/<Member Name="([^"]+)"/g)].map((x) => x[1]!)));
  return enums;
}

/** Поля сущности снимка: имя → тип Edm. */
export function bp30Fields(entitySet: string): Map<string, string> | undefined {
  const meta = bp30Metadata().entities.get(entitySet);
  return meta && new Map(meta.properties.map((p) => [p.name, p.type]));
}
