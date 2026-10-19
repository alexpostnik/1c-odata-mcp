import { z } from "zod";
import type { ConnectionConfig } from "./env.js";
import { ACCOUNT_PREFIX, REGISTERS } from "./mapping.js";
import { InputError } from "../errors.js";

const codes = z.array(z.string().trim().min(1).max(100)).min(1).max(50);
export const AccountingSchema = z
  .object({
    chart: z
      .string()
      .regex(/^ChartOfAccounts_[\p{L}\p{N}_]+$/u)
      .optional(),
    register: z
      .string()
      .regex(/^AccountingRegister_[\p{L}\p{N}_]+$/u)
      .optional(),
    strictDefaults: z.boolean().optional(),
    operationKinds: z
      .object({
        salesAct: z.string().trim().min(1).max(100).optional(),
        customerPayment: z.string().trim().min(1).max(100).optional(),
      })
      .strict()
      .optional(),
    prefixes: z.object({ receivables: codes.optional(), inventory: codes.optional() }).strict().optional(),
    defaults: z
      .object({
        goods: codes.optional(),
        incomingVat: codes.optional(),
        outgoingVat: codes.optional(),
        income: codes.optional(),
        expense: codes.optional(),
        receivable: codes.optional(),
        receivableAdvance: codes.optional(),
        payable: codes.optional(),
        payableAdvance: codes.optional(),
      })
      .strict()
      .optional(),
  })
  .strict();
export type AccountingConfig = z.infer<typeof AccountingSchema>;
const RU_DEFAULTS = {
  goods: ["41.01", "41"],
  incomingVat: ["19.03", "19"],
  outgoingVat: ["90.03"],
  income: ["90.01.1", "90.01"],
  expense: ["90.02.1", "90.02"],
  receivable: ["62.01", "62"],
  receivableAdvance: ["62.02"],
  payable: ["60.01", "60"],
  payableAdvance: ["60.02"],
} as const;
export type AccountDefault = keyof typeof RU_DEFAULTS;
export function accountingChart(cfg: ConnectionConfig): readonly string[] {
  return [cfg?.accounting?.chart ?? "ChartOfAccounts_Хозрасчетный"];
}
export function accountingRegister(cfg: ConnectionConfig): readonly string[] {
  return cfg?.accounting?.register ? [cfg.accounting.register] : REGISTERS.accounting;
}
export function accountPrefixes(
  cfg: ConnectionConfig,
  group: "receivables" | "inventory",
): readonly string[] {
  const configured = cfg?.accounting?.prefixes?.[group];
  if (configured) return configured;
  if (cfg?.accounting?.strictDefaults)
    throw new InputError(`Configure accounting.prefixes.${group} for this database.`);
  return ACCOUNT_PREFIX[group];
}
export function defaultAccountCodes(cfg: ConnectionConfig, group: AccountDefault): readonly string[] {
  const configured = cfg?.accounting?.defaults?.[group];
  if (configured) return configured;
  if (cfg?.accounting?.strictDefaults)
    throw new InputError(
      `Configure accounting.defaults.${group} for this database; Russian account defaults are disabled by strictDefaults.`,
    );
  return RU_DEFAULTS[group];
}

/** Явное значение инструмента важнее настройки базы; иначе сохранены прежние значения БП РФ. */
export function operationKind(
  cfg: ConnectionConfig,
  group: "salesAct" | "customerPayment",
  explicit?: string,
): string {
  return (
    explicit?.trim() ||
    cfg.accounting?.operationKinds?.[group] ||
    (group === "salesAct" ? "Услуги" : "ОплатаПокупателя")
  );
}
