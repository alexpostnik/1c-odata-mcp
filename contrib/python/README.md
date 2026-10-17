# Optional Python recovery toolkit

This is an optional Python distribution alongside the native TypeScript MCP server. Installing the native server does not install or start this toolkit. Python tool names and queue receipts belong to the Python connector; they are not wire-compatible adapters for the TypeScript write journal.

Requires Python 3.11+ and MCP Python SDK **2.2.0**. Install in a separate environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install './contrib/python[test]'
cd contrib/python
python -m pytest
python -m build
```

| Module | Capabilities | Prerequisites and boundaries |
|---|---|---|
| `onec_odata_mcp.client`, `.metadata`, `.server` | CSDL discovery, enums and composite references, bounded reads, accounting registers and chart lookup, preview/confirmation, create/update/post, movement reconciliation, multi-target routing | Explicit HTTPS OData targets and verified metadata. Writes default off, require TEST target, matching write URL, target confirmation and durable receipt ledger. Built-in credential provider uses macOS Keychain; Python callers can inject a credential store. |
| `onec_odata_mcp.counterparty` | Required card name/UNP, alphanumeric identifiers, full uniqueness evidence, fresh pre-POST check, durable target/UNP claims across aliases | Python connector currently maps UNP to `ИНН` on `Catalog_Контрагенты`. It does not discover registration identity. Processes must share the same coordination ledger; independent external writers require target-side uniqueness. |
| `onec_odata_mcp.queue_manifest`, `.queue_checkpoint` | Offline deterministic plans, bounded concurrency, stable operation IDs, locked checkpoints, claim continuity, exact preview/result/readback validation and resume guards | Private input/output files. Uncertain outcomes remain reserved until addressed reconciliation; generating a plan does not grant write authority. |
| `onec_odata_mcp.hr_source`, `.person_mapping`, `.person_repair` | Typed source provenance, prerequisite catalog lineage, create-only HR documents, conservative name preflight, atomic audited person-batch recovery | Source adapter evidence, exact target references and independently accepted artifacts. Helpers preserve unknown states and do not perform live verification. |
| `onec_odata_mcp.cash_revenue`, `.profile_preview` | Exact cash-symbol/text/money checks, RKO drafts, explicit cleanup periods, accounting classification previews and integer proportional splitting | Explicit target references, depositor facts and independently reviewed policy. No company policy, rate or chart mapping is bundled; example policy uses symbolic accounts and an illustrative ratio. Drafts cannot directly execute writes. |
| `onec_bank_graph.reader`, `.scripts` | Lossless statement graphs, historical/modern source adapters, schema validation, exact decimal controls, typed identities/links, bounded search/cursors/get/aggregation and source-readiness evidence | Operator-supplied archive and independently reviewed source units/epochs. Never combines unlike units or guesses missing values. No bank records or source archive is distributed. |
| `onec_bank_graph.mcp_adapter` | Read-only graph tools and resources; synchronous/background readiness with isolated failure state | Inject into an SDK 2.2.0 MCPServer using `register_bank_graph_tools(server, archive_path)`. |
| `onec_bank_graph.bank_statement_rules` | Bank-purpose classification, tax/period recognition, ambiguous/mixed amount guards | Default output requires a decision. Callers explicitly supply per-rule target policy and review reference; no organization-specific accounting assignment is bundled. |
| `agent_bridge` | Read-only typed legacy source requests, bounded pages, native identity/addressing, resumable catalog/constants/document scans, SQLite evidence ledgers, OData comparison | Separately installed Windows/1C 7.7 adapter implementing the documented request/response shapes. This package does not install Windows services or execute COM locally. No legacy write implementation. |
| `onec_mcp_runtime` | Loopback bearer-authenticated HTTP transport, token checks, privacy-safe telemetry, optional macOS launch helpers | Operator-selected private token/configuration paths. Helpers execute only when explicitly invoked; no automatic deployment. |

## Offline commands

```sh
onec83-queue-plan --help
onec83-queue-checkpoint --help
onec83-profile-preview --help
onec-bank-graph contract --archive /path/to/private/archive
onec-bank-rules --help
```

Import examples:

```python
from onec_odata_mcp.queue_manifest import compile_manifest, dry_run_request
from onec_odata_mcp.profile_preview import preview_batch
from onec_bank_graph.reader import Reader
from agent_bridge.core import Bridge
```

Build a graph from synthetic fixtures without an archive or network access:

```sh
python onec_bank_graph/scripts/statement_graph.py build \
  onec_bank_graph/assets/synthetic-reviewed.json \
  --controls onec_bank_graph/assets/synthetic-controls.json \
  --output /tmp/synthetic-graph.json --acceptance-output /tmp/synthetic-acceptance.json
```

The graph builder writes new files exclusively. Fixture accounts, names, identifiers and amounts are synthetic. Belarus BYR/BYN unit-epoch handling is explicit and does not convert amounts automatically.

## Configuration and recovery

The Python connector accepts `ONEC83_ODATA_BASE_URL`, `ONEC83_ODATA_USER`, `ONEC83_KEYCHAIN_SERVICE`, or a multi-target `ONEC83_TARGETS_JSON` object. The URL must end in `/odata/standard.odata/`. Ordinary writes additionally require `ONEC83_ENABLE_WRITE=true`, `ONEC83_TARGET_KIND=test` and `ONEC83_WRITE_BASE_URL` exactly matching the read target. Password environment variables are rejected by this connector. Do not commit configuration, credentials, ledgers, previews or exported business records.

Use `onec-python-mcp --help` for transport, target and ledger arguments. `onec-legacy-bridge` reads an operator-owned configuration selected by `ONEC_BRIDGE_CONFIG`; see `agent_bridge.config.load_config` for its validation contract. The bridge is read-only even for TEST.

Receipt recovery is evidence-based: an HTTP timeout after dispatch may mean the write succeeded. Preserve the ledger and source claim, read the addressed target and its movements, and reconcile the exact operation before resuming. Never delete a ledger to force a retry. Local protocol claims cannot provide global exactly-once semantics against unrelated writers.

Policy preview requires `--policy` and a separately retained `--reviewed-contract` containing canonical SHA256 hashes of selected profiles and rules. The example pair in `examples/` is synthetic. A digest proves byte consistency; independent review and authorization remain operator responsibilities. Context-specific enum dispatch mappings are empty by default and can be supplied explicitly to `MetadataModel(enum_context_mappings=...)`.

## Compatibility

The Python connector and optional macOS helpers are a separate implementation. Native TypeScript additions and the Python contribution share concepts and synthetic tests, not runtime journal formats. Platform/configuration metadata, Windows adapter deployment, production write policy and accounting mappings need installation-specific validation before any migration. Offline tests do not establish live 1C compatibility.

Contract-account settings may live in a separate information register keyed by organization, counterparty, agreement and settlement kind. The Python connector does **not** implement compound-key information-register writes. Its accounting-register record-set read is keyed by recorder and recorder type. Generic entity Create/Patch requires the supported Ref_Key contract. Inspect actual published metadata and confirm every dimension, resource and account mapping before designing a settings-register write; an absent entity cannot be repaired by guessing a name or putting account fields on an agreement.

The actual Windows 7.7 source-side adapter is included under [tools/agent-bridge/windows](tools/agent-bridge/windows/README.md), together with its sibling metadata reader and local interactive broker/client/status code. Installation and private configuration remain explicit; no administrative bootstrap is executed or bundled. The source distribution includes these scripts and the `windows-tests` synthetic harnesses. Run `python -m pytest windows-tests`; PowerShell AST/fake-context checks require an available `pwsh`, while actual structured-storage checks require Windows PowerShell 5.1 on Windows. Syntax/fake-context success does not establish live 1C compatibility.
