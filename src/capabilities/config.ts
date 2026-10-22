import { z } from "zod";

const identifier = z.string().regex(/^[\p{L}\p{N}_. -]{1,100}$/u);
const entity = z.string().regex(/^[A-Za-z][\p{L}\p{N}_]{1,180}$/u);
/** Только явные справочные факты; пароль, URL, свободные записи/тексты здесь не принимаются. */
export const CapabilityProfileSchema = z
  .object({
    schemaVersion: z.literal(1),
    revision: identifier,
    profile: z.enum(["generic", "belarus"]).default("generic"),
    configuration: identifier.default("unknown"),
    configurationVersion: identifier.default("unknown"),
    platformVersion: identifier.default("unknown"),
    source: identifier.default("operator-supplied"),
    observedAt: z.iso.datetime().optional(),
    unsupported: z
      .array(
        z
          .object({
            entitySet: entity,
            configuration: identifier,
            configurationVersion: identifier,
            source: identifier,
            observedAt: z.iso.datetime(),
          })
          .strict(),
      )
      .max(100)
      .default([]),
  })
  .strict()
  .superRefine((value, ctx) => {
    for (const fact of value.unsupported) {
      if (
        fact.configuration !== value.configuration ||
        fact.configurationVersion !== value.configurationVersion ||
        value.configuration === "unknown"
      )
        ctx.addIssue({
          code: "custom",
          message: "Факт отсутствия объекта должен относиться к указанной конфигурации и её версии",
        });
    }
  });
export type CapabilityProfile = z.infer<typeof CapabilityProfileSchema>;
export const DEFAULT_PROFILE = CapabilityProfileSchema.parse({ schemaVersion: 1, revision: "generic-1" });
export const JsonCapabilityProfileSchema = z.preprocess((value, ctx) => {
  if (typeof value !== "string") return value;
  if (!value.trim()) return undefined;
  try {
    return JSON.parse(value);
  } catch {
    ctx.addIssue({ code: "custom", message: "должна быть JSON-настройка профиля возможностей" });
    return z.NEVER;
  }
}, CapabilityProfileSchema.optional());
