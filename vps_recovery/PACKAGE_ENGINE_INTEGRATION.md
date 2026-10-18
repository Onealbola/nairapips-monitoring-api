# Package engine integration candidate

The monitoring rule resolvers use the exact package contract UUID, trader UUID, purchase UUID, account UUID, stage and rules hash. Frozen target, DD type/limit, profit share, resets and paid-cycle terms win over mutable plan/profile values. A missing or mismatched package contract never falls back to a legacy plan.

`DD_POLICE_PACKAGE_CONTRACT_V1.py` is a separate worker candidate derived from the supplied V3.20 source. It imports `package_dd_runtime.py`, `package_account_rules.py` and `package_contracts.py` from this folder. Legacy account calculations retain their original branch. Package accounts use the purchased equity-based static or trailing rule, with the trailing watermark stored durably per exact account. The original broker authentication, durable breach latch, close-to-zero verification and delayed breach relay remain in place. The live snapshot includes an explicit open-position count for the future target consumer.

The journal directory defaults to `package_dd_state` beside the worker and can be configured with `NP_PACKAGE_DD_JOURNAL_DIR`. Preserve this directory across restarts. A corrupt journal or an old account without a proven journal requires recovery; the worker will not guess or lower its watermark. A brand-new assignment can initialize only within five minutes of its recorded start time.

Offline tests cover exact identities, static equity rules, persistent trailing watermarks, exact breach boundaries, negative equity, restart/recovery behavior, corrupted state and stale observations. No Windows MT5 process has been imported, started or stopped during these tests.

The running VPS fleet has not been replaced. Keep it unchanged until a controlled cutover verifies the complete installed supervisor, account bindings, broker authentication, persisted watermark directory and zero-exposure behavior. The atomic target/lifecycle, payout-cycle and included-reset consumers remain unfinished and are still release blockers. This worker candidate does not make package sales ready to launch.
