NAIRAPIPS VPS RECOVERY - REVIEW PACKAGE, 17 OCTOBER 2026

FIRST ACTION ONLY
Extract this zip into a NEW folder in Downloads. Double-click CHECK_ONLY.cmd.
Attach NairaPips-VPS-check.json from the VPS Desktop to this chat.
This check reads file hashes and process identities only. It does not stop,
restart, overwrite, install or contact any API. It does not export raw command
lines, secrets, customer data, latch contents or broker credentials.
If Windows blocks this script, report the error; do not change execution policy.

DO NOT COPY THE PYTHON FILES OVER YOUR LIVE FILES YET.
The other files are the prepared recovery source, not an auto-installer.

FINDINGS
The uploaded 2,920-byte supervisor ends inside spawn_shard and has no main
routine. It cannot start the fleet when executed as uploaded. The live process
may have loaded an earlier complete version; do not close it merely because
its on-disk file is incomplete.
The restored supervisor is based on the earlier complete source, retaining four
shards, isolated terminals, durable state paths and separate helper processes.
Restart delays are tracked per child rather than blocking other supervision.
The updated roster watcher rejects error envelopes, sample-only responses,
malformed rows and duplicate MT5 identities, preserving the previous snapshot.
Explicit valid empty account lists remain supported as authoritative responses.
Role-specific lifetime OS locks prevent duplicates among UPDATED programs.
They cannot detect or stop older processes that do not implement these locks.
The relay retains its existing settlement/report behavior; only role locking is
added. The uploaded DD worker is NOT replaced or included in this archive.

VALIDATION AND LIMITATIONS
Seven offline tests passed: Linux lock behavior, mocked Windows byte locking,
malformed roster rejection, account validation, exact shard launch paths,
relay acknowledgement and independent helper recovery. No real MT5, network,
production database or trading operations were used in these tests.
The Windows OS-lock path and actual terminal handover still require VPS tests.
This package does NOT implement or release the new custom-package product.
DD calculations, trade closing, broker passwords and the Operations/Target
engine are outside this recovery change.

CUTOVER REQUIREMENTS BEFORE RUNNING REPLACEMENT
Confirm exact installed file hashes, currently running identities and retained
fleet state. Back up the actual live source and durable state. Account for old
helper processes and reconcile them by exact identity, not broad Python kills.
Keep the current working fleet until the replacement preflight passes.
Verify actual observations and four-shard DD coverage after a controlled cutover;
process counts alone do not establish financial protection or dashboard health.
The old uploaded DEPLOYMENT_ORDER.txt is NOT an instruction for this cutover.
