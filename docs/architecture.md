# Architecture

A guide for contributors. For installation and everyday use, see the
[README](../README.md).

## Code map

| File | Responsibility |
| --- | --- |
| `addon.py` | Anki hooks, profile lifecycle, review tracking, and progress display |
| `storage.py` | Review records, reward calculations, and the persistent delivery queue |
| `engine.py` / `api.py` | Background delivery, retries, and TaskHero API requests |
| `ui.py` | Connection and reward settings |
| `config.py` / `credentials.py` | Profile settings and token storage |

Runtime files live in `taskhero_anki/` and use only Python's standard library
and Anki's bundled APIs.

## From review to reward

1. Anki's hooks report answers and completed syncs to the active profile's
   controller. Closing a profile stops its timer and prevents new background work.
2. The controller reads review IDs and sync sequence numbers, not card content.
   Ratings 1–4 count; rescheduling entries do not.
3. Desktop reviews wait ten seconds for Undo before being accepted. Reviews
   discovered through sync can be accepted immediately.
4. The store creates batch rewards and daily habit completions at the configured
   thresholds. A background worker sends one queued event at a time.

Sync scans use both review IDs and sync sequence numbers, so older-timestamp
mobile reviews can still be discovered. Reviews already present at connection,
or seen while disconnected, are persistently excluded by ID: a later sync
metadata change must not make them eligible. Bulk writes keep ingestion within
one SQLite transaction. Mobile reviews first synced after reconnection can still
count, even when their timestamps precede that connection; there is no ledger of
connected/disconnected time intervals.

## Days and progress

Review days follow TaskHero's account UTC offset and signed rollover offset.
Connection testing, habit refresh, and habit creation fetch these rules from
`GET /me`; Save caches them. Users must refresh and save after changing
TaskHero preferences. Existing queued rewards keep their original payloads;
past timezone changes are not reconstructed.
Unreadable or invalid saved day settings pause accounting and delivery until
fresh rules are saved; valid reward and habit preferences are preserved where
possible.

Changing batch size or points per batch starts a fresh partial batch; completed
batches stay credited. Disconnect ends the reward session, cancels unsent work,
and resets progress, including when reconnecting to the same account. Review IDs
and sent receipts remain to prevent replay.
If a saved token is unavailable while its session still has activity, Save is
blocked until the user confirms Disconnect. Missing credentials must not let
another account inherit queued rewards or partial progress. Normal restarts
with a valid saved token preserve the session and its retries.
There is no daily points cap; all complete batches earn their configured points.

Renaming a habit keeps its ID. Replacing a deleted or ineligible habit cancels
unsent completions for the old ID, including future retries of in-flight work.
An already-running success may still be recorded. Replacement does not replay
a missed completion that day.

## Delivery and credentials

Anki `QueryOp` runs network requests outside the UI thread. Requests time out
after ten seconds. Temporary failures retry with bounded exponential backoff;
other failures wait for the user to fix the problem and retry.

Stable event IDs serve as idempotency keys, so losing a successful response
doesn't award the same reward twice. IDs include a random profile identifier:
they do not deduplicate rewards across separate desktops.
Receipts retain only event ID, kind, remote ID and sent time, not full API
responses. Schema upgrades preserve these IDs and reward progress while removing
obsolete response-body and cap columns.

The Anki token is saved atomically in a separate profile-local file, outside
the collection. File permissions protect it; it is not separately encrypted.
The API endpoint is fixed for normal use and authenticated redirects are refused.
See [Privacy](../PRIVACY.md) for storage and deletion details.

## Known limits

Redo does not restore discarded review credit. A crash between accepting a
review and creating its reward can lose that credit; events already in the
delivery queue survive restarts. Neither case changes Anki's study history.

## Packaging

`build.py` creates `dist/taskhero-for-anki.ankiaddon` from an explicit allowlist
of nine runtime files plus `LICENSE`. Files sit at the archive root.
Unexpected runtime files and symlinks are rejected; fixed timestamps and
permissions make builds reproducible. Development files and local data are not
packaged. See [Releasing](releasing.md) for the publication checklist.
