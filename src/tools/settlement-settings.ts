import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { ServerContext } from "../context.js";
import { patchResultSchema } from "../schemas/output.js";
import { databaseField, guard, ok } from "./_shared.js";
import {
  readSavedSettlementSettings,
  saveSettlementSettings,
  savedSettlementAccounts,
  savedSettlementScope,
  generalSettlementScope,
} from "../odata/saved-settlement-settings.js";

export function registerSettlementSettingsTools(server: McpServer, ctx: ServerContext): void {
  for (const legacy of [true, false]) {
    const readName = legacy
      ? "read.accounting.get_contract_settlement_settings"
      : "read.accounting.get_settlement_settings";
    const writeName = legacy
      ? "write.accounting.set_contract_settlement_settings"
      : "write.accounting.set_settlement_settings";
    const scope = legacy ? savedSettlementScope : generalSettlementScope;
    server.registerTool(
      readName,
      {
        title: "Прочитать точную настройку счетов расчётов",
        description:
          "Читает точную настройку независимого регистра. scope=contract требует договор; counterparty/group означает контрагента или группу в том же поле Контрагент_Key без договора. Организация и вид расчётов обязательны. Унаследованные настройки не подставляются. Отсутствующая или несовместимая схема блокирует операцию. Старое имя get_contract_settlement_settings поддерживает только договор.",
        inputSchema: { database: databaseField, ...scope },
        outputSchema: z.object({
          database: z.string(),
          entitySet: z.string(),
          recordPath: z.string(),
          found: z.boolean(),
          key: z.record(z.string(), z.string()),
          record: z.record(z.string(), z.unknown()).optional(),
          contractKind: z.unknown().optional(),
          scope: z.enum(["contract", "counterparty", "group"]),
        }),
      },
      (args: { database?: string; confirm?: boolean } & Record<string, unknown>) =>
        guard(readName, async () =>
          ok({
            database: ctx.db(args.database).cfg.name,
            ...(await readSavedSettlementSettings(
              ctx.db(args.database),
              legacy ? { ...args, scope: "contract" } : args,
            )),
          }),
        ),
    );
    server.registerTool(
      writeName,
      {
        title: "Сохранить точную настройку счетов расчётов",
        description:
          "Создаёт отсутствующую настройку (create) либо меняет явно указанные ресурсы (update) с expectedDataVersion и If-Match. scope=contract требует договор; counterparty/group сохраняет настройку точного контрагента/группы без договора. Проверяет организацию, IsFolder и отсутствие удаления; для договора также владельца, организацию и валюту. Сначала предпросмотр; подтверждение с теми же аргументами и operationId. Глобальные настройки и удаление недоступны. Старое имя set_contract_settlement_settings поддерживает только договор. Прочее не получает толкование займа/расхода.",
        inputSchema: {
          database: databaseField,
          ...scope,
          mode: z.enum(["create", "update"]),
          accounts: savedSettlementAccounts,
          expectedDataVersion: z.string().optional(),
          confirm: z.boolean().default(false),
        },
        outputSchema: patchResultSchema,
      },
      (args: { database?: string; confirm?: boolean } & Record<string, unknown>) =>
        guard(writeName, async () =>
          ok(
            await saveSettlementSettings(
              ctx.db(args.database),
              legacy ? { ...args, scope: "contract" } : args,
              args.confirm ?? false,
            ),
          ),
        ),
    );
  }
}
