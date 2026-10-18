# Monitoring reconciliation

The production rule is seven days from assignment for never-traded accounts,
reset by the latest actual trade opening. Closing a trade, syncing a row, or
changing a dashboard pointer must not restart that clock. A position with open
exposure remains monitored. Existing financial WATCHDOG protection is retained.
Any expired account must leave the registry and appear archived in its account
record through one supported transaction; do not implement independent writes
that can leave a partially archived account.

Replacement handovers are scoped to an exact account journey. Verify parent,
child, trader and purchase/entitlement identities before activating the child and
retiring its exact parent. A trader's single current_account_id is a dashboard
selection, not proof that every other account is invalid. Root assignments need
purchase/entitlement proof; do not invent a parent link. Terminal lifecycle
markers prevent reactivation even if equity subsequently recovers.

## Implemented review tools

`monitoring_policy.py` contains side-effect-free reconciliation decisions.
`audit_monitoring.py` paginates the account, registry, and purchase tables,
checks exact account trade evidence with four concurrent requests, and writes a
credential-free review report. It never activates, retires, or archives anything.
Run with securely injected SUPABASE_URL and SUPABASE_KEY:

```
python audit_monitoring.py
python -m unittest discover -s tests -v
```

The audit excludes superseded journeys, parent accounts with recorded children,
terminal records, and non-inactivity retirements from restoration. Failed or
missing trade evidence requires review. These tools are not wired into the
production roster or a scheduled worker yet.

## Confirmed production findings

The V46 roster intentionally sets trade_history_available=False because broad
trader_trades scans previously timed out. Do not simply turn those scans back on
inside /monitorable_accounts. This hot endpoint supplies DD coverage.

Existing inactivity retirement updates the monitoring registry but leaves some
trader_accounts rows active. A separate bounded maintenance worker and an atomic
archive transaction are required for consistent lifecycle cleanup. The worker
must use reliable current activity/exposure evidence from the MT5 collector;
missing evidence must not archive an account.

On 9 October 2026, 25 inactivity-retired accounts were restored through the
existing np_monitoring_activate RPC after verifying exact current purchases,
recent trade openings, no successor/child, and no terminal markers. Each registry
activation was verified. This does not establish successful MT5 connections.
Account 477498044's missing registry and MT5-pool assignment links were also
repaired following the owner's confirmation that both parallel accounts are
valid. Its first live-state observation remains unverified.

## Deployment gate

The selected repositories contain APIs, not the external MT5/DD collector.
Before production rollout, obtain its source/configuration and identify the
running service, roster-refresh interval, shard assignment, connection failures,
and current-position evidence. Confirm the Render start command chooses the
intended monitoring entry point: monitoring_api.py and
monitoring_api_GLOBAL_FEED_AUTHORITY.py are different implementations.

Validate representative independent journeys, parent-to-child exchange, the
seven-day boundary, unavailable trade history, open exposure, WATCHDOG accounts,
and live-state observations across every collector shard. Investigate API worker
timeouts and memory usage separately. Do not label the system production-ready
based only on unit tests or active registry entries. Local repository edits have
not been deployed to Render.

## Incident evidence, 10 October 2026

Owner screenshots confirm: compute upgraded; two sync workers deployed;
Python 3.11.9 active; public HTTPS connects but does not respond; production
loopback requests time out; direct Flask test_client GET / returns 200; separate
Gunicorn on loopback port 18081 returns 200 using the same installed application.
Therefore application import and idle root handling work, while production
request handling remains unavailable. This does not establish the cause or
prove a Gunicorn defect. Worker poll and master futex wait states alone are not
stack traces. Render denied py-spy attachment. Extra VPS roster watcher/relay
processes are evidenced; their ownership and relationship to the stall remain
unverified. No broad process kill is authorized by this report.

The repository now aborts strict roster construction if any authoritative bulk
lookup fails, instead of treating an outage as missing account records and
running retirement logic. Four roster bulk queries use this behavior. Legacy
non-roster callers preserve their existing fallback behavior. Regression tests
cover database timeout, empty ID sets, and all four authoritative callers.
This safety fix and the lightweight liveness endpoint were published in
addf0c5; the owner supplied a Render screenshot showing that commit Live. They do not establish repair of the
production server stall. Research HTTP requests from this workspace failed with
ProxyError; no external root-cause report has been verified.


## Exact account activity correction — 2026-10-10

Trade history used for inactivity must belong to the exact trader_account_id.
A reused MT5 login must not import a predecessor account’s history. Legacy
login-only history cannot authorize inactivity retirement.

The owner confirmed that trade closing time restarts the seven-day clock for
swing traders. Opening and closing timestamps count; ingestion and row update
timestamps do not. Open positions retain protection. Automatic inactivity
archival remains disabled because trade_history_available is still False.

Four regression tests cover closing-time activity, reused login isolation, open
position protection, and sync-time exclusion. The full local suite passed 58
tests. This correction does not establish a fix for the production HTTP stall.

## HTTP availability repair — 16 October 2026

Render Events reported repeated `dial tcp ...:10000: i/o timeout while running
 your code`. This establishes availability failure, not OOM or a particular
blocked stack. The installed Supabase client's default database timeout was
120 seconds. The repository previously had no Gunicorn configuration.

`gunicorn.conf.py` now supplies two gthread workers with eight threads each,
keeps the 30-second worker watchdog, and bounds accepted connection capacity.
The startup hook handles an explicit sync-worker/thread CLI override and
refreshes Gunicorn's cached worker class before spawning workers. Gunicorn
loads this configuration automatically when started from the repository root;
an explicit alternative `--config` or different working directory must be
checked if the deployment does not emit the startup marker below.

`monitoring_http.py` admits at most six non-probe requests per worker and one
roster refresh per worker. Root and `/livez` probes retain capacity. Busy calls
return HTTP503 with `success=false`, `operation_started=false`, and no roster
`data`; collectors must retain the last good population and retry. No account
activation, retirement, or financial decision is made by admission. Response
writes are chunked and client socket idle waits are limited to ten seconds.
Supabase database waits are limited to six seconds rather than 120 seconds.
Failed verification still fails closed; database errors do not prove a missing
account, a breach, a phase pass, or an empty population.

Startup logs identify `NP MONITOR HTTP V51`, actual class/threads/bind and request
slots. `/livez` reports `http_capacity_release` and `RENDER_GIT_COMMIT` without
querying storage. It is a process probe, **not** proof of broker freshness.
`/monitoring_health` remains the separate freshness diagnostic. No broker
balances, target/DD rules, inactivity clocks, or lifecycle functions changed.

Validation uses a real local Gunicorn process with six blocked fixture requests,
an explicit legacy sync/one-thread override, concurrent health probes, overload
rejection without starting a mutation, and recovery after the fixture releases.
The production app is not imported for this test and no production account is
used. Follow deployment with readiness requests and exact-account observation
checks; do not equate successful deployment with repaired MT5 connectivity.

Live validation after 033c259 confirmed `/livez` and `/` HTTP200 and a successful
47-account roster. A separate live diagnostic also returned `Server disconnected`.
The SDK constructs HTTP/2 clients by default. The follow-up database transport
uses authenticated HTTP/1.1, at most eight database connections, no cached idle
connections, six-second read/write waits and two-second connect/pool waits.
It never automatically replays a disconnected mutation. SDK query/filter/auth
behavior is preserved and exercised with an HTTP transport fixture. This removes
the shared HTTP/2/idle-connection dependency; it does not establish that HTTP/2
was the sole source of the original availability failure.
