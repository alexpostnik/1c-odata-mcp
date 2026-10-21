# PR #43 review corrections

Source plan: review 5602915426 against original candidate 94c7f02, the upstream CONTRIBUTING contract and BP 3.0 metadata snapshot at c37b397. Reused the existing journal, resolver, and metadata helpers. Belarus schema fixture contains only fields, types, and keys from targeted read-only TEST observations on 2026-10-21 (configuration/platform versions supplied by the owner).

The user journeys are: apply saved accounts to existing bank documents under either published counterparty representation; recover a lost group-creation reply without duplicate advice; retain old creation lookup behavior and call count; keep version and uncertain-write guards honest.

| Stage | Executed validation | Result |
| --- | --- | --- |
| RED | `npm test -- test/existing-bank-settlement.test.ts test/counterparty-groups.test.ts test/write-reconciliation.test.ts` | 18 failed, 123 passed, 3 files failed. Failures reproduced BP 3.0 fields/enumeration, unchanged advance account, overly broad readback, legacy hierarchy lookup, missing recovery inventory and group marker fallback. |
| Initial GREEN | `npm test` | 598 passed in 36 files. |
| Final full GREEN | `npm test` | 602 passed in 36 files after compound-key recovery changes. |
| Register recovery RED | `npm test -- test/saved-settlement-settings.test.ts` | 2 failed, 69 passed: lost POST/PATCH remained unverifiable despite exact expected record. |
| Register recovery GREEN | `npm test -- test/saved-settlement-settings.test.ts test/write-reconciliation.test.ts` | 107 passed in 2 files. |
| Quality | `npm run lint`, `npm run format:check`, `npm run typecheck`, `npm run build`, `npm run manifest` | All passed. Manifest rebuilt offline with synthetic connection. |
| Coverage attempt | `npm test -- --coverage` | Unavailable: `@vitest/coverage-v8` is not installed. No coverage percentage is claimed. |

Tests exercise actual BP 3.0 metadata and enumeration values, Belarus receipt/payment row schemas, both account resources, amount/tax/analytics protection, legitimate recalculated settlement fields, exact-ref group recovery, every operationId tool in the upstream recovery inventory, and the versionless Belarus register's explicit singleWriter update. Legacy creation tests verify zero hierarchy reads and retention of bounded register fallback. Mutation hierarchy cycle/deletion/depth/truncation tests remain strict.

Remaining limits: no live writes of this revised candidate; all conditional-unit contracts in apply_bank_settlement_settings are explicitly refused because rate/settlement-amount conversion is not implemented. Group-membership changes remain manual/unverifiable and held. Register status reconciles an exact compound-key record whose protected-field fingerprint matches, without sending the mutation again; missing/different/unreadable results remain held; they are never advised to retry blindly. Post/Unpost only has a fresh version precheck, not a proven atomic CAS; versionless register updates still require sole-writer operation and have no server-side concurrency lock. Existing successful live evidence belongs to earlier candidates, not this revised head.

## Final narrowed release and regulated currency

The maintainer comment6322906963 and owner decision exclude conditional-unit support from PR43. Reuse header-only9da00a6; the separate local UE experiment is not part of this release. Header protection had its own RED (5 failures) / GREEN (111 passed) before this base. All observed published header fields except DataVersion are protected.

Release regression RED363fdd2: 10 failed,115 passed in2 files. It reproduced a same-currency UE contract reaching currency/settings lookup, a cross-currency UE message mismatch, missing per-base currency isolation/fallback, invalid inputs and missing health configuration provenance. GREEN:125 passed in2files after the small fix. Added an exact saved-setting read regression using BYN933 and RUB643 within one process, plus preserved conditional-unit classification outside the bounded mutation tool.

No live current-head test, deployment, rights probe or all-tools compatibility certification is inferred from these offline tests. See docs/ACCOUNTING.md for observed schema versus declared/unknown versions and the separate #375 profile boundary.

Final narrowed-release checks: all622tests passed in37files after build (16.29s), including the startup error-path test. The earlier pre-build run had621passed/1skipped: test/startup.test.ts intentionally skips when dist/index.js is absent; it is not a live test and was executed successfully after build. Lint, format:check, typecheck, build and manifest passed; manifest remains74tools/3prompts. Fake-only copied transport tests: baseline4tests/2errors; proposed4passed. Installed runtime was not changed.
