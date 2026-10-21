import { describe, expect, it } from "vitest";
import { createdAt, documentTitle, negativeCashHint, printNumber } from "../src/odata/names.js";
import { ageCounterparty } from "../src/odata/settlements-aging.js";

describe("documentTitle", () => {
  it("имя метаданных → слова", () => {
    expect(documentTitle("Document_ПоступлениеНаРасчетныйСчет")).toBe("Поступление на расчетный счет");
    expect(documentTitle("РеализацияТоваровУслуг")).toBe("Реализация товаров услуг");
    expect(documentTitle("Document_ОперацияБух")).toBe("Операция бух");
    expect(documentTitle("Document_СписаниеСРасчетногоСчета")).toBe("Списание с расчетного счета");
    expect(documentTitle("Document_ОтчетКомитентуОПродажах")).toBe("Отчет комитенту о продажах");
    expect(documentTitle("Document_СчетФактураВыданный")).toBe("Счет фактура выданный");
  });
});

describe("printNumber", () => {
  it("без префикса и ведущих нулей", () => {
    expect(printNumber("0000-000581")).toBe("581");
    expect(printNumber("БП-000012")).toBe("12");
    expect(printNumber("000000")).toBe("0");
    expect(printNumber("б/н")).toBe("б/н");
  });
});

describe("createdAt", () => {
  it("UUIDv1 — время, иначе undefined", () => {
    expect(createdAt("b92962b4-c598-11f1-9110-00155d97eb2d")).toMatch(/^2026-/);
    expect(createdAt("123e4567-e89b-42d3-a456-426614174000")).toBeUndefined();
    expect(createdAt(undefined)).toBeUndefined();
  });
});

describe("negativeCashHint", () => {
  it("по счёту и наличию аналитики", () => {
    expect(negativeCashHint("57.03", false)).toContain("продажи по картам");
    expect(negativeCashHint("55.03", true)).toContain("не указан банковский счёт");
    expect(negativeCashHint("51", false)).toContain("выписк");
  });
});

describe("документы с будущей датой", () => {
  it("не попадают в возраст и просрочку", () => {
    const a = ageCounterparty(
      [{ counterparty: "cp", contract: "c", document: "d", documentSet: "Document_X", cents: 1000 }],
      new Map([["d", { date: "2026-11-30" }]]),
      new Map([["c", 10]]),
      "2026-10-21",
    );
    expect(a.futureDatedCents).toBe(1000);
    expect(a.overdueCents + a.notDueCents + a.ageBuckets["0-30"]).toBe(0);
    expect(a.documents[0]!.status).toBe("future_dated");
  });
});
