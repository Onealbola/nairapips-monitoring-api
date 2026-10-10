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
