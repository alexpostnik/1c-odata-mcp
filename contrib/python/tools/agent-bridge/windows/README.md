# Windows 1C 7.7 read adapter

This directory contains the actual legacy source adapter used by the optional Python `agent_bridge`, its metadata reader, and a local interactive-session broker. It exposes read methods only. It does not install services, SSH accounts, firewall rules or scheduled tasks. Every runtime invocation requires an explicit private `-Config` file; no source database, account, host or credential is bundled.

| Script | Function |
|---|---|
| `Invoke-77Bridge.ps1` | One UTF-8 JSON request on stdin → one bounded JSON response on stdout. Validates the source alias, operation, dataset/type and selected fields before COM access. Includes native typed Catalog/Document identity and addressed reads, ordinary document/reference pages, line batching, constants, operations/entries and supported periodic-history reads. |
| `Read-77Metadata.ps1` | Reads only the exact `Metadata/Main MetaData Stream` in the operator-selected `1Cv7.MD`; does not enumerate configuration modules, forms or accounting records. Must remain beside the adapter. |
| `Start-77InteractiveBroker.ps1` | Processes one request at a time from an explicitly configured local spool under an already provisioned interactive Windows account. Includes bounded stdout/stderr handling, timeout, stale-read recovery and heartbeat. `-Once` processes one polling pass; the default keeps polling. |
| `Invoke-77InteractiveBrokerClient.ps1` | Sends one request to that local spool and returns its correlated adapter response. Creates local request files; has no network listener. |
| `Get-77InteractiveBrokerStatus.ps1` | Reports sanitized heartbeat state (`running`, `stale`, `missing`, `invalid`) without starting 1C. Heartbeat does not prove a successful source read. |

The actual COM adapter requires **Windows PowerShell 5.1 x86** with a compatible registered 1C 7.7 automation server. The broker/client can use an operator-selected compatible PowerShell host; its `powerShell32` setting must point to the actual x86 Windows PowerShell executable for the COM child. Linux PowerShell can parse the source and execute the supplied fake-context AST tests; that is not Windows/COM compatibility evidence.

Copy the example configurations outside the checkout and replace every placeholder. `bridge.local.example.json` supplies an explicit dataset allowlist. `fields: []` deliberately selects no user attributes; select actual metadata field names or explicitly opt into `metadataAllFields`. Run modes are allowlisted (`RMAccount`/`RMTrade`), as are automation ProgIDs. A password, if required, comes only from an existing user-bound DPAPI `PSCredential` file selected by `credentialFile`; no plaintext password field or remote expression is accepted.

The broker needs independently provisioned private spool directories, correct Windows ACLs and the explicit bridge account SID. Its response/status ACL checks do not provision a secure installation for you. Administrative setup and service/task management remain operator responsibilities. Do not invoke the runtime examples against an unreviewed live base: 1C initialization may run configuration startup code even though this adapter never writes business objects. Confirm a test copy and source account read rights separately.

```powershell
# Explicit operator invocation only; these commands start source-side reads.
'{"schemaVersion":"1.0","operation":"schema","requestId":"example","sourceAlias":"src77-example-copy"}' |
  & '<x86 Windows PowerShell 5.1 path>' -NoProfile -File .\Invoke-77Bridge.ps1 -Config '<private bridge JSON>'

# Read local status only; this does not start COM.
& .\Get-77InteractiveBrokerStatus.ps1 -Config '<private broker client JSON>'
```

Compatibility limits: schema capability flags describe implemented methods, not successful reads or a snapshot. Native identity remains scoped to the same source registration; base continuity, token lifetime and global identity are unverified. Unsupported relations/history remain explicit holds. Chart of accounts/register/calculation access and periodic Document selection are not implemented in this 7.7 reader. The current broker allowlist does not forward Catalog `probe_reference_identity` or `read_reference_by_native`; those methods are available through the directly invoked worker with the same explicit source allowlist. Do not silently fall back between routes or treat ordinal positions/code/name as native identities. A timeout is not proof of completion; preserve source/request evidence and resolve the reported condition before retrying.

Offline verification lives in `../../../windows-tests` relative to this directory's toolkit root (`contrib/python/windows-tests`). Synthetic PowerShell harnesses parse production functions into fake contexts; they do not initialize COM. Windows structured-storage tests run only on Windows. Administrative SSH/bootstrap, credential provisioning, task installers and production diagnostics were intentionally excluded.
