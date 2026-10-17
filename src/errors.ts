/**
 * Ошибка данных, которые передал вызывающий: не найден склад, договор, счёт
 * плана счетов, банковский счёт организации; неоднозначный запрос.
 *
 * Отличается от сбоя сервера тем, что чинится на стороне вызывающего — уточнить
 * название, взять Ref_Key из справочника. Поэтому guard отдаёт её текст как есть,
 * без префикса «Внутренняя ошибка инструмента»: это не поломка сервера, а
 * подсказка модели, что именно поправить в аргументах.
 */
export class InputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InputError";
  }
}

/** Validation completed before any write request was sent. Safe to mark the operation rejected. */
export class BeforeWriteError extends InputError {}
