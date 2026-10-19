import { describe, expect, it } from "vitest";
import { parseConfig } from "../src/config/env.js";
import { defaultAccountCodes, operationKind } from "../src/config/accounting.js";

const env = { ODATA_BASE_URL: "https://example.invalid/odata/", ODATA_USERNAME: "u", ODATA_PASSWORD: "p" };
describe("accounting configuration", () => {
  it("keeps Russian account and operation defaults", () => {
    const cfg = parseConfig(env).connections[0]!;
    expect(defaultAccountCodes(cfg, "receivable")).toEqual(["62.01", "62"]);
    expect(operationKind(cfg, "salesAct")).toBe("Услуги");
    expect(operationKind(cfg, "customerPayment")).toBe("ОплатаПокупателя");
  });
  it("treats blank global config as unset", () => {
    expect(parseConfig({ ...env, ODATA_ACCOUNTING_CONFIG: "  " }).connections[0]!.accounting).toBeUndefined();
  });
  it("falls back from blank per-base config to global and overrides by base", () => {
    const multi = {
      ODATA_DB_BY_BASE_URL: env.ODATA_BASE_URL,
      ODATA_DB_BY_USERNAME: "u",
      ODATA_DB_BY_PASSWORD: "p",
      ODATA_ACCOUNTING_CONFIG: '{"defaults":{"receivable":["62.1"]}}',
    };
    expect(
      parseConfig({ ...multi, ODATA_DB_BY_ACCOUNTING_CONFIG: " " }).connections[0]!.accounting?.defaults
        ?.receivable,
    ).toEqual(["62.1"]);
    expect(
      parseConfig({ ...multi, ODATA_DB_BY_ACCOUNTING_CONFIG: '{"defaults":{"receivable":["62.3"]}}' })
        .connections[0]!.accounting?.defaults?.receivable,
    ).toEqual(["62.3"]);
  });
  it("requires only requested mappings in strict mode", () => {
    const cfg = parseConfig({
      ...env,
      ODATA_ACCOUNTING_CONFIG:
        '{"strictDefaults":true,"defaults":{"receivable":["62.1"]},"operationKinds":{"salesAct":"ПродажаКомиссия"}}',
    }).connections[0]!;
    expect(defaultAccountCodes(cfg, "receivable")).toEqual(["62.1"]);
    expect(() => defaultAccountCodes(cfg, "goods")).toThrow("goods");
    expect(operationKind(cfg, "salesAct")).toBe("ПродажаКомиссия");
    expect(operationKind(cfg, "salesAct", "ЯвныйВид")).toBe("ЯвныйВид");
  });
  it.each([
    '{"defaults":{"goods":[" "]}}',
    '{"chart":"invalid"}',
    '{"operationKinds":{"salesAct":" "}}',
    '{"unexpected":true}',
    "invalid",
  ])("rejects invalid config %s", (config) => {
    expect(() => parseConfig({ ...env, ODATA_ACCOUNTING_CONFIG: config })).toThrow();
  });
});
