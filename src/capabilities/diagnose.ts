import type { ConnectionConfig } from "../config/env.js";
import type { MetadataMap } from "../types/odata.js";
import { inspectRequirement } from "./schema.js";
import { REQUIREMENTS, type ToolRequirement } from "./requirements.js";

const literalField = /^[\p{L}_][\p{L}\p{N}_]*$/u;

/** Описания условий — документация, не исполняемые выражения и не разрешения записи. */
export function diagnoseDynamic(
  tool: ToolRequirement | undefined,
  cfg: ConnectionConfig,
  metadata: MetadataMap,
  entitySet: string,
  fields: string[] = [],
) {
  const dependencies = tool?.dynamic.filter((d) => d.argument) ?? [];
  const declaredFields = dependencies.flatMap((d) => d.fields ?? []);
  const simpleFields = declaredFields.filter((f) => literalField.test(f));
  const unresolvedFields = declaredFields.filter((f) => !literalField.test(f));
  const keys = [...new Set(dependencies.flatMap((d) => d.keys ?? []))];
  const result = inspectRequirement(
    { target: [entitySet], fields: [...new Set([...simpleFields, ...fields])], keys },
    cfg,
    metadata,
  );
  const entity = metadata.entities.get(entitySet);
  const restrictions = dependencies.flatMap((d) => d.restrictedCandidates ?? []);
  if (restrictions.length && !restrictions.includes(entitySet)) {
    result.status = "schema_incompatible";
    result.missing.push("tool:restricted-target");
  }
  const functions = dependencies
    .flatMap((d) => (d.functions ?? []) as Array<{ name: string; method?: string }>)
    .map((fn) => {
      const advertised = metadata.functions?.find(
        (f) => f.name === fn.name && f.bindingType === entity?.entityType,
      );
      return {
        name: fn.name,
        method: fn.method,
        declaration: advertised ? "advertised" : "unknown",
        permission: "unknown",
      };
    });
  return {
    ...result,
    functions,
    unresolvedFields,
    conditional: dependencies.flatMap((d) => d.conditionalFields ?? []),
    rights: "unknown",
    note: "Проверена выбранная схема. Условия, результатные поля функций и права остаются неизвестны без соответствующего наблюдения. Запросов записи нет.",
  };
}

/** Условные поля видимы даже если текущий набор аргументов ещё не выбран. */
export function mutationDiagnostics(tool: ToolRequirement, cfg: ConnectionConfig, metadata?: MetadataMap) {
  const model = tool.mutationFieldModel;
  if (!model) return undefined;
  const target = tool.required
    .map((id) => REQUIREMENTS.definitions[id])
    .find((req) => req && req.operations && (req.operations as string[]).includes("create"));
  if (!target) return { model, status: "unknown" };
  const schema = inspectRequirement(target, cfg, metadata);
  const checkFields = (fields: string[], suffix = "") =>
    inspectRequirement({ target: schema.candidates.map((name) => name + suffix), fields }, cfg, metadata);
  return {
    model,
    alwaysSent: schema,
    conditional: Object.entries(model.conditionalSent ?? {}).map(([field, when]) => ({
      field,
      when,
      applicability: "unknown",
      schema: checkFields([field]),
    })),
    rows: Object.entries(model.rows ?? {}).map(([name, row]) => ({
      name,
      applicability: model.alwaysSent.includes(name) ? "required" : "unknown",
      alwaysSent: checkFields(row.alwaysSent, `_${name}`),
      conditional: Object.entries(row.conditionalSent ?? {}).map(([field, when]) => ({
        field,
        when,
        applicability: "unknown",
        schema: checkFields([field], `_${name}`),
      })),
    })),
    note: "Необязательный реквизит нужен лишь когда его отправляет текущий вызов. Несовместимое условное поле не блокирует другие инструменты. Тип составной ссылки не заменяется автоматически на _Key.",
  };
}
