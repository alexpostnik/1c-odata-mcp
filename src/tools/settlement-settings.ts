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
} from "../odata/saved-settlement-settings.js";

export function registerSettlementSettingsTools(server: McpServer, ctx: ServerContext): void {
  server.registerTool(
    "read.accounting.get_contract_settlement_settings",
    {
      title: "Прочитать сохранённые счета договора",
      description:
        "Читает точную настройку независимого регистра по организации, владельцу, договору и виду расчётов. Общие настройки не подставляются. Отсутствующая или несовместимая схема блокирует операцию.",
      inputSchema: { database: databaseField, ...savedSettlementScope },
      outputSchema: z.object({
        database: z.string(),
        entitySet: z.string(),
        recordPath: z.string(),
        found: z.boolean(),
        key: z.record(z.string(), z.string()),
        record: z.record(z.string(), z.unknown()).optional(),
        contractKind: z.unknown().optional(),
      }),
    },
    (args) =>
      guard("read.accounting.get_contract_settlement_settings", async () =>
        ok({
          database: ctx.db(args.database).cfg.name,
          ...(await readSavedSettlementSettings(ctx.db(args.database), args)),
        }),
      ),
  );
  server.registerTool(
    "write.accounting.set_contract_settlement_settings",
    {
      title: "Сохранить счета точного договора",
      description:
        "Создаёт отсутствующую настройку (create) либо меняет явно указанные ресурсы (update) с expectedDataVersion и If-Match. Сначала предпросмотр; подтверждение с теми же аргументами и operationId. Владелец и организация договора обязаны совпадать. Глобальные настройки и удаление недоступны. Договор Прочее не получает автоматическое толкование займа/расхода; сохранение ресурсов этого не меняет.",
      inputSchema: {
        database: databaseField,
        ...savedSettlementScope,
        mode: z.enum(["create", "update"]),
        accounts: savedSettlementAccounts,
        expectedDataVersion: z.string().optional(),
        confirm: z.boolean().default(false),
      },
      outputSchema: patchResultSchema,
    },
    (args) =>
      guard("write.accounting.set_contract_settlement_settings", async () =>
        ok(await saveSettlementSettings(ctx.db(args.database), args, args.confirm)),
      ),
  );
}
