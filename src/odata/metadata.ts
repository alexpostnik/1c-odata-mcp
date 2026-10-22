import { XMLParser } from "fast-xml-parser";
import type { ODataClient } from "./client.js";
import { logger } from "../logger.js";
import type {
  EntityClass,
  EntityMeta,
  MetadataMap,
  MetaNavigation,
  MetaProperty,
  MetaFunction,
} from "../types/odata.js";

/** Сопоставление префикса EntitySet → класс объекта 1С. */
const PREFIX_TO_CLASS: Array<[string, EntityClass]> = [
  ["Catalog_", "catalog"],
  ["Document_", "document"],
  ["DocumentJournal_", "documentJournal"],
  ["AccumulationRegister_", "accumulationRegister"],
  ["AccountingRegister_", "accountingRegister"],
  ["InformationRegister_", "informationRegister"],
  ["CalculationRegister_", "calculationRegister"],
  ["Enum_", "enum"],
  ["ChartOfAccounts_", "chartOfAccounts"],
  ["ChartOfCharacteristicTypes_", "chartOfCharacteristicTypes"],
  ["Constant_", "constant"],
  ["BusinessProcess_", "businessProcess"],
  ["Task_", "task"],
  ["ExchangePlan_", "exchangePlan"],
];

export function classify(entitySet: string): { class: EntityClass; shortName: string } {
  for (const [prefix, cls] of PREFIX_TO_CLASS) {
    if (entitySet.startsWith(prefix)) {
      return { class: cls, shortName: entitySet.slice(prefix.length) };
    }
  }
  return { class: "other", shortName: entitySet };
}

interface RawProp {
  "@_Name": string;
  "@_Type": string;
  "@_Nullable"?: string;
}
interface RawNav {
  "@_Name": string;
  "@_Relationship"?: string;
  "@_ToRole"?: string;
}
interface RawEntityType {
  "@_Name": string;
  Key?: { PropertyRef: { "@_Name": string } | Array<{ "@_Name": string }> };
  Property?: RawProp | RawProp[];
  NavigationProperty?: RawNav | RawNav[];
}
interface RawEntitySet {
  "@_Name": string;
  "@_EntityType": string;
}

const asArray = <T>(v: T | T[] | undefined): T[] => (v === undefined ? [] : Array.isArray(v) ? v : [v]);

/**
 * Загружает $metadata (EDMX/XML) и строит карту сущностей.
 * Результат кешируется вызывающей стороной — здесь только разбор.
 */
export async function loadMetadata(client: ODataClient): Promise<MetadataMap> {
  return parseMetadata(await client.getText("$metadata"));
}

/** Разбор текста $metadata — отдельно от загрузки, чтобы тесты проверяли код по снимку схемы. */
export function parseMetadata(xml: string): MetadataMap {
  const parser = new XMLParser({
    ignoreAttributes: false,
    attributeNamePrefix: "@_",
    removeNSPrefix: true,
  });
  const doc = parser.parse(xml) as Record<string, unknown>;

  const schema = findSchema(doc);
  const odataVersion = detectVersion(doc);
  if (schema?.EntityContainer === undefined) throw new Error("Ответ metadata не содержит EntityContainer");

  // EntityType по полному имени (Namespace.TypeName и просто TypeName).
  const typeByName = new Map<string, RawEntityType>();
  for (const t of asArray<RawEntityType>(schema?.EntityType as RawEntityType | RawEntityType[])) {
    typeByName.set(t["@_Name"], t);
  }

  const sets = asArray<RawEntitySet>(
    (schema?.EntityContainer as { EntitySet?: RawEntitySet | RawEntitySet[] })?.EntitySet,
  );

  const entities = new Map<string, EntityMeta>();
  for (const set of sets) {
    const entitySet = set["@_Name"];
    const typeName = stripNamespace(set["@_EntityType"]);
    const raw = typeByName.get(typeName);
    const { class: cls, shortName } = classify(entitySet);

    const properties: MetaProperty[] = asArray<RawProp>(raw?.Property).map((p) => ({
      name: p["@_Name"],
      type: p["@_Type"],
      nullable: p["@_Nullable"] !== "false",
    }));

    const navigations: MetaNavigation[] = asArray<RawNav>(raw?.NavigationProperty).map((n) => ({
      name: n["@_Name"],
      toType: stripNamespace(n["@_ToRole"] ?? ""),
      collection: false, // уточняется по Association; для карты достаточно имени
    }));

    const keyRefs = asArray(raw?.Key?.PropertyRef).map((k) => k["@_Name"]);

    entities.set(entitySet, {
      entitySet,
      entityType: typeName,
      class: cls,
      shortName,
      keys: keyRefs,
      properties,
      navigations,
    });
  }

  logger.info({ count: entities.size, odataVersion }, "metadata loaded");
  return { odataVersion, entities, functions: parseFunctions(schema) };
}

function stripNamespace(name: string): string {
  const idx = name.lastIndexOf(".");
  return idx >= 0 ? name.slice(idx + 1) : name;
}

function findSchema(doc: Record<string, unknown>):
  | {
      EntityType?: RawEntityType | RawEntityType[];
      [key: string]: unknown;
      EntityContainer?: { EntitySet?: RawEntitySet | RawEntitySet[] };
    }
  | undefined {
  const edmx = (doc.Edmx ?? doc["edmx:Edmx"]) as Record<string, unknown> | undefined;
  const services = edmx?.DataServices ?? edmx?.["edmx:DataServices"];
  const schema = (services as Record<string, unknown> | undefined)?.Schema;
  return Array.isArray(schema) ? schema[0] : (schema as never);
}

function detectVersion(doc: Record<string, unknown>): string {
  // Реальная версия OData — в DataServices/@m:DataServiceVersion (напр. "3.0").
  // Атрибут Version у Edmx — это версия EDMX-обёртки ("1.0"), не путать.
  const edmx = (doc.Edmx ?? doc["edmx:Edmx"]) as Record<string, unknown> | undefined;
  const services = (edmx?.DataServices ?? edmx?.["edmx:DataServices"]) as Record<string, string> | undefined;
  return services?.["@_DataServiceVersion"] ?? services?.["@_m:DataServiceVersion"] ?? "3.0";
}

/** Функции v3 / bound actions: поля результата принадлежат ReturnType, а не корню регистра. */
function parseFunctions(schema: Record<string, unknown> | undefined): MetaFunction[] {
  if (!schema) return [];
  const types = new Map(
    asArray(schema.ComplexType as RawEntityType | RawEntityType[])
      .concat(asArray(schema.EntityType as RawEntityType | RawEntityType[]))
      .map((t) => [t["@_Name"], t]),
  );
  const container = schema.EntityContainer as Record<string, unknown> | undefined;
  const declarations = [
    ...asArray(container?.FunctionImport as Record<string, unknown> | Record<string, unknown>[]),
    ...asArray(schema.Function as Record<string, unknown> | Record<string, unknown>[]),
  ];
  return declarations.map((fn) => {
    const params = asArray(fn.Parameter as RawProp | RawProp[]).map((p) => ({
      name: p["@_Name"],
      type: p["@_Type"],
    }));
    const returnType = typeof fn["@_ReturnType"] === "string" ? fn["@_ReturnType"] : undefined;
    const result = returnType && types.get(stripNamespace(returnType.replace(/^Collection\((.*)\)$/, "$1")));
    const binding =
      params.find((p) => /binding/i.test(p.name)) ?? (fn["@_IsBindable"] === "true" ? params[0] : undefined);
    return {
      name: String(fn["@_Name"]),
      ...(binding ? { bindingType: stripNamespace(binding.type.replace(/^Collection\((.*)\)$/, "$1")) } : {}),
      parameters: params,
      returnType,
      properties: result
        ? asArray(result.Property).map((p) => ({
            name: p["@_Name"],
            type: p["@_Type"],
            nullable: p["@_Nullable"] !== "false",
          }))
        : [],
    };
  });
}
