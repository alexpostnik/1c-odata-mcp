# Сборка MCPB-бандла (`.mcpb`)

`manifest.json` в корне репозитория описывает сервер в формате
[MCP Bundle](https://github.com/modelcontextprotocol/mcpb): `.mcpb` — это zip-архив с
собранным сервером и манифестом, который можно установить в Claude Desktop как расширение
(перетащить файл в настройки расширений) или приложить к GitHub Release. Основной способ
установки по-прежнему npm (`npx -y 1c-odata-mcp`).

> Раньше бандл публиковался на Smithery. С октября 2026 Smithery не даёт обновлять серверы
> («Updating servers has been removed»), поэтому туда больше не публикуем.

## Почему не `mcpb pack`

Официальный CLI `@anthropic-ai/mcpb` валидирует `manifest.json` по спецификации MCPB, где
элементы `tools` — только `{name, description}`. Наш манифест хранит у инструментов ещё и
`inputSchema` / `outputSchema` / аннотации — ровно то, что видит MCP-клиент, — и на них
держится тест согласованности документации с кодом. `mcpb pack` такие ключи отвергает
(«Unrecognized key(s)»), поэтому бандл собирается вручную через `zip`.

## Как собрать

1. Пересобрать список инструментов в `manifest.json` из живого сервера (имя, описание,
   `inputSchema`, `outputSchema`, аннотации — ровно то, что видит MCP-клиент) и
   синхронизировать `version` с `package.json`. Реальная база не нужна — сервер
   поднимается с фиктивным подключением в режиме только-чтения:

   ```bash
   npm run manifest
   ```

2. Прогнать тесты: `test/docs-consistency.test.ts` сверяет состав и схемы инструментов в
   манифесте с кодом, версию в `package.json`/`manifest.json`/CHANGELOG и число
   инструментов в README, `package.json` и `long_description` манифеста. Упал — поправить
   числа в текстах (`mcpb validate` тут не поможет: официальный валидатор не принимает
   `inputSchema` в `tools[]`):

   ```bash
   npm test
   ```

3. Собрать staging-папку (build + **чистый** `npm ci --omit=dev`, чтобы не
   тащить dev-зависимости и не трогать рабочий `node_modules`) и заzip'овать:

   ```bash
   STAGE=$(mktemp -d)
   npm run build
   cp -R dist "$STAGE/dist"
   cp package.json package-lock.json README.md LICENSE manifest.json "$STAGE/"
   (cd "$STAGE" && npm ci --omit=dev --ignore-scripts)
   rm -rf "$STAGE/dist/scripts"
   # В рантайме не нужны: типы, sourcemaps, исходники .ts и markdown пакетов —
   # бандл ужимается с ~6,4 до ~3,3 МБ.
   find "$STAGE" -type f \( -name "*.map" -o -name "*.d.ts" -o -name "*.d.mts" -o -name "*.d.cts" \) -delete
   find "$STAGE/node_modules" -type f \( -name "*.ts" -o -iname "*.md" -o -iname "CHANGELOG*" \) -delete
   (cd "$STAGE" && zip -r -X -q -9 /tmp/1c-odata-mcp.mcpb . -x ".*")
   ```

   Перед тем как отдавать бандл, стоит запустить `$STAGE/dist/index.js` MCP-клиентом (`tools/list` и
   пара вызовов) — облегчённый бандл должен отвечать так же, как исходный.
