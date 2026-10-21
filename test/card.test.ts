import { describe, expect, it } from "vitest";
import { cardEntries, netBalance } from "../src/odata/account-card.js";
import { reconciliationAccounts, reconciliationLines } from "../src/tools/card.js";
import { PROMPTS, renderPrompt } from "../src/mcp/prompts.js";

const ACC62_1 = "62-01";
const ACC62_2 = "62-02";
const ACC51 = "51";
const CP = "11111111-1111-1111-1111-111111111111";
const OTHER = "22222222-2222-2222-2222-222222222222";
const CONTRACT = "33333333-3333-3333-3333-333333333333";

const T = (s: string) => `StandardODATA.${s}`;
const move = (o: {
  period: string;
  recorder: string;
  line?: number;
  dr: string;
  cr: string;
  sum: number;
  drDims?: Array<[string, string]>;
  crDims?: Array<[string, string]>;
  active?: boolean;
}) => {
  const r: Record<string, unknown> = {
    Period: o.period,
    Recorder: o.recorder,
    Recorder_Type: T("Document_ПоступлениеНаРасчетныйСчет"),
    LineNumber: String(o.line ?? 1),
    Active: o.active ?? true,
    AccountDr_Key: o.dr,
    AccountCr_Key: o.cr,
    Сумма: o.sum,
  };
  (o.drDims ?? []).forEach(([ref, set], i) => {
    r[`ExtDimensionDr${i + 1}`] = ref;
    r[`ExtDimensionDr${i + 1}_Type`] = T(set);
  });
  (o.crDims ?? []).forEach(([ref, set], i) => {
    r[`ExtDimensionCr${i + 1}`] = ref;
    r[`ExtDimensionCr${i + 1}_Type`] = T(set);
  });
  return r;
};

const cpDims: Array<[string, string]> = [
  [CP, "Catalog_Контрагенты"],
  [CONTRACT, "Catalog_ДоговорыКонтрагентов"],
];

describe("cardEntries", () => {
  it("сторона счёта, корреспонденция, неактивные пропускаются, сортировка по времени", () => {
    const rows = [
      move({
        period: "2026-08-10T10:00:00",
        recorder: "b",
        dr: ACC51,
        cr: ACC62_1,
        sum: 300,
        crDims: cpDims,
      }),
      move({
        period: "2026-08-01T10:00:00",
        recorder: "a",
        dr: ACC62_1,
        cr: "90",
        sum: 1000.5,
        drDims: cpDims,
      }),
      move({ period: "2026-08-02T10:00:00", recorder: "c", dr: ACC62_1, cr: "90", sum: 7, active: false }),
    ];
    const e = cardEntries(rows, new Set([ACC62_1]));
    expect(e.map((x) => [x.side, x.cents, x.corrAccount])).toEqual([
      ["debit", 100050, "90"],
      ["credit", 30000, ACC51],
    ]);
    expect(e[0]!.dims).toEqual([
      { ref: CP, set: "Catalog_Контрагенты" },
      { ref: CONTRACT, set: "Catalog_ДоговорыКонтрагентов" },
    ]);
  });

  it("отбор по субконто — по своей стороне проводки", () => {
    const rows = [
      move({ period: "2026-08-01T10:00:00", recorder: "a", dr: ACC62_1, cr: "90", sum: 10, drDims: cpDims }),
      move({
        period: "2026-08-01T11:00:00",
        recorder: "b",
        dr: ACC62_1,
        cr: "90",
        sum: 20,
        drDims: [[OTHER, "Catalog_Контрагенты"]],
      }),
    ];
    expect(cardEntries(rows, new Set([ACC62_1]), [CP]).map((x) => x.recorder)).toEqual(["a"]);
    expect(cardEntries(rows, new Set([ACC62_1]), [CP, CONTRACT])).toHaveLength(1);
  });

  it("проводка внутри набора счетов (зачёт аванса) даёт две строки", () => {
    const rows = [
      move({
        period: "2026-08-05T10:00:00",
        recorder: "z",
        dr: ACC62_2,
        cr: ACC62_1,
        sum: 50,
        drDims: cpDims,
        crDims: cpDims,
      }),
    ];
    expect(cardEntries(rows, new Set([ACC62_1, ACC62_2])).map((x) => x.side)).toEqual(["debit", "credit"]);
  });
});

describe("netBalance", () => {
  it("сальдо Дт − Кт по строкам с нужной аналитикой", () => {
    const rows = [
      { ExtDimension1: CP, ExtDimension2: CONTRACT, СуммаBalanceDr: 100, СуммаBalanceCr: 0 },
      { ExtDimension1: CP, СуммаBalanceDr: 0, СуммаBalanceCr: 30 },
      { ExtDimension1: OTHER, СуммаBalanceDr: 999, СуммаBalanceCr: 0 },
    ];
    expect(netBalance(rows, [CP])).toBe(7000);
    expect(netBalance(rows, [CP, CONTRACT])).toBe(10000);
    expect(netBalance(rows)).toBe(106900);
  });
});

describe("акт сверки", () => {
  it("строки по документам, без внутреннего зачёта аванса", () => {
    const rows = [
      move({
        period: "2026-08-01T10:00:00",
        recorder: "sale",
        dr: ACC62_1,
        cr: "90",
        sum: 1000,
        drDims: cpDims,
      }),
      move({
        period: "2026-08-01T10:00:00",
        recorder: "sale",
        line: 2,
        dr: ACC62_1,
        cr: "90",
        sum: 500,
        drDims: cpDims,
      }),
      move({
        period: "2026-08-03T10:00:00",
        recorder: "pay",
        dr: ACC51,
        cr: ACC62_2,
        sum: 1200,
        crDims: cpDims,
      }),
      move({
        period: "2026-08-03T10:00:01",
        recorder: "pay",
        line: 2,
        dr: ACC62_2,
        cr: ACC62_1,
        sum: 1200,
        drDims: cpDims,
        crDims: cpDims,
      }),
    ];
    const lines = reconciliationLines(cardEntries(rows, new Set([ACC62_1, ACC62_2]), [CP]));
    expect(lines.map((l) => [l.recorder, l.debit, l.credit])).toEqual([
      ["sale", 150000, 0],
      ["pay", 0, 120000],
    ]);
    expect(lines[0]!.contract?.ref).toBe(CONTRACT);
  });

  it("счета расчётов: без буквенных субсчетов НДС с авансов", () => {
    const a = (code: string) => ({ key: code, code, description: code });
    expect(
      reconciliationAccounts([a("60.01"), a("62.02"), a("76.09"), a("76.АВ"), a("76.ВА")]).map((x) => x.code),
    ).toEqual(["60.01", "62.02", "76.09"]);
  });
});

describe("сценарии", () => {
  it("не заданный необязательный аргумент — понятная подпись, заданный — значение", () => {
    const brief = PROMPTS.find((p) => p.name === "morning_brief")!;
    expect(renderPrompt(brief, {})).toContain("База: основная. Организация: все организации базы.");
    expect(renderPrompt(brief, { database: "ooo" })).toContain("База: ooo.");
  });
});
