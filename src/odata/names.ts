import { uuidTimestamp } from "./uuid.js";

/**
 * Представления для людей: имя вида документа, номер «как на печати», время создания объекта
 * по ссылке и подсказка по отрицательному остатку денег. Чистые функции без запросов к 1С.
 */

/**
 * «Document_ПоступлениеНаРасчетныйСчет» → «Поступление на расчетный счет». Синонимов видов
 * документов OData не публикует — разбиваем имя метаданных по заглавным буквам.
 */
export function documentTitle(entitySetOrName: string): string {
  const name = entitySetOrName.replace(/^(Document|Catalog)_/, "");
  const words = name.split(/(?<=[а-яёa-z0-9])(?=[А-ЯЁA-Z])/u);
  return words.map((w, i) => (i === 0 || /^[А-ЯЁA-Z]{2,}$/u.test(w) ? w : w.toLowerCase())).join(" ");
}

/**
 * Номер документа как в печатной форме 1С: без префикса (организации, базы) и ведущих нулей —
 * «0000-000581» → «581», «БП-000012» → «12». Номер без цифр в конце оставляем как есть.
 */
export function printNumber(number: string): string {
  const tail = number.trim().split("-").pop() ?? "";
  const stripped = tail.replace(/^0+(?=\d)/, "");
  return /\d$/.test(stripped) ? stripped : number.trim();
}

/** Время создания объекта по Ref_Key (UUIDv1, как выдаёт 1С), UTC ISO; иначе undefined. */
export function createdAt(ref: string | undefined): string | undefined {
  if (!ref) return undefined;
  try {
    const t = uuidTimestamp(ref);
    return t.kind === "v1" ? t.timestampIso : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Типичная причина отрицательного остатка денег по счёту. withoutAnalytics — минус у строки без
 * банковского счёта / кассы: сумма есть, но не разнесена по счетам, а не «денег не хватает».
 */
export function negativeCashHint(accountCode: string, withoutAnalytics: boolean): string {
  if (withoutAnalytics) {
    return "в части документов не указан банковский счёт или касса — суммы не разнесены по счетам, по счёту в целом остаток может быть положительным";
  }
  const top = accountCode.split(".")[0];
  if (accountCode.startsWith("57.03")) {
    return "оплаты по картам поступили в банк, а продажи по картам не отражены (операции по платёжным картам, отчёты о розничных продажах)";
  }
  if (top === "57") return "перевод в пути списан, а поступление на другой счёт не отражено";
  if (top === "50") return "расход из кассы проведён раньше прихода или не оформлен приходный ордер";
  if (top === "55")
    return "списание со спецсчёта или депозита без поступления на него — проверьте документы размещения";
  return "не загружена часть выписки или есть лишнее списание — сверьте с выпиской банка";
}
