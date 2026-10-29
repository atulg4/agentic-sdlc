# Control Center: live delivery and telemetry

[`docs/control-center.md`](control-center.md) describes the **snapshot** — one
read-only document answering *what state is every work unit in*. Two things sit
on top of it, and both are data rather than a server:

| Module | Answers |
|---|---|
| `control_center_stream.py` | *what changed since I last looked, and what do I do if I missed something* |
| `control_center_telemetry.py` | *what did it cost, how much parallelism did we get, and was the result worth it* |

The browser UI is deliberately **not** here. The hosting decision on #45 puts
the responsive console on the MarketMaestro surface with Forge exposing a
narrow read-only contract, and #108 owns the mobile-first, chart-first UX. What
this layer owns is the contract that console consumes.

## The live delivery contract

A console cannot re-render a dense backlog table on every tick, and a
reconnecting browser cannot assume nothing happened while it was gone. The
stream module defines the frames a push transport carries and the rules that
keep a reconnect honest.

### Frames

| Kind | Meaning |
|---|---|
| `snapshot` | Replace everything; the full document travels with the response |
| `change` | Apply one ordered change (`work-unit`, `incident`, `project-health`, `telemetry`, `backlog`) |
| `heartbeat` | Nothing changed; the cursor and freshness are still current |
| `reset` | The cursor cannot be served; take the snapshot that follows |

Each frame carries its sequence, a stable `eventId`, the project and work unit
it concerns, and a bounded payload.

### Four rules

- **Ordering is contiguous.** `StreamLog` assigns every sequence itself, so
  there are no gaps. A client that receives 7 after 5 knows it missed 6 rather
  than rendering a hole. Appending a frame whose sequence is not the next one
  fails closed.
- **Replay is idempotent.** A frame already recorded is recognised by its
  `eventId` and changes nothing, so a redelivered event, a retried flush or a
  double-subscribed client cannot double-count. The same ID with a *different*
  payload fails closed.
- **A cursor that cannot be served says so.** Retention is bounded. A client
  resuming from before the window gets a `reset` frame naming the reason and
  the frames that were dropped, then a full snapshot — never a silent
  fast-forward past changes it did not see. This is the snapshot fallback for
  missed events.
- **Staleness is labeled.** Every reconnect response carries the freshness of
  the data behind it and the instant it was last good, so a console shows
  "as of 09:12" instead of implying live truth.

### Resume

```python
response = build_reconnect_response(
    log,
    snapshot,
    cursor=StreamCursor.decode(last_event_id),
    observed_at=now,
    age_seconds=12,
)
```

Three outcomes, and the client is always told which through `mode` and
`resetReason`:

| Situation | `mode` | What it gets |
|---|---|---|
| First connection (no cursor) | `snapshot` | One snapshot frame and a cursor |
| Resumable cursor | `resumed` | Only the change frames after it |
| Cursor beyond retention, ahead of the log, or reconciled against other content | `snapshot` | A `reset` frame stating why, then the snapshot |

A cursor carries both a sequence **and** the content digest the client
reconciled against, because deltas applied to the wrong base are worse than no
deltas. A digest that differs is tolerated only when pending frames explain the
difference; otherwise it resets.

The encoded cursor is published as `lastEventId` — what SSE calls it, and what
the browser echoes back in `Last-Event-ID`, so reconnect needs no client-side
bookkeeping. It is deliberately *not* called `token`: it is not a credential,
and a field by that name would trip every secret scanner, including this
module's own.

### The wire form

`render_sse_frame` emits the server-sent-events block a transport writes:

```
id: 42-9f2c…
event: change
data: {"change":"work-unit","eventId":"f-42",…}

```

Data is one line of JSON by construction — a raw newline would terminate the
field and split one frame into two — and a malformed render fails closed.

## The telemetry overlay

`build_telemetry_overlay` joins a snapshot to the telemetry the other ledgers
already produce. It **composes** and adds no arithmetic of its own, so the
honesty rules of the layers below cannot be broken here:

| Section | Source | Notes |
|---|---|---|
| `cost` | `usage_ledger` aggregates | By project, model and stage |
| `billingMix` | usage records | Keeps subscription separable from billed |
| `capacity` | `capacity_metrics` | Model usage and plan capacity |
| `parallelism` | `capacity_metrics` | Concurrency, queue, runners, bottlenecks |
| `effectiveness` | an effectiveness **report document** | First-pass rates, time-to-live, value per unit of effort |
| `workUnits` | usage records | Per-unit drill-down: every run, with estimate versus actual |

The effectiveness side is consumed as a document — the output of
`sdlcctl effectiveness-report` — rather than by importing the module that
produces it, for the same reason the snapshot consumes a backlog document: the
console is a view over whatever each adapter published, and a report produced
an hour ago on another runner is still the report. It is validated fail-closed
on the way in: an unsupported version, an unknown section, a secret-shaped key
or a credential-shaped value is refused rather than rendered.

### Unknown is never a zero

A section with no data behind it reports `status: "unknown"` with the reason:

- no usage records → `cost` is unknown, not zero
- no observation window → `capacity` and `parallelism` are unknown, because
  utilization without an explicit window would drop a quiet period out of the
  denominator
- no effectiveness report → `effectiveness` is unknown, and the console never
  infers delivery from the fact that runs happened

### Drill-down agrees with the rollup

Each work unit's `cost` is taken from the usage ledger's own aggregation rather
than re-summed, so the drill-down and the cost view cannot disagree. A test
asserts they match.

## Historical ranges and export

`filter_usage_window` narrows telemetry to a half-open time range, comparing
**instants** rather than strings — RFC 3339 offsets and fractional seconds do
not sort chronologically as text, and an inverted window fails closed.

`export_control_center` produces one self-contained document — snapshot,
telemetry and stream state — for offline analysis. The snapshot keeps its own
content digest, so an exported file can be checked against the console that
produced it rather than trusted.

## No credentials, ever

The overlay and every frame are serialized to a browser, so both are scanned
for credential-shaped values (percent-encoding peeled first, via the ledger's
own `carries_credential_token`) and secret-shaped keys. Frame payloads are
additionally bounded in text length, key count and nesting depth, so the
transport cannot be used to ship bulk content or a prompt.

## CLI

The `control-center` command grew optional telemetry and stream outputs; every
existing invocation behaves exactly as before.

```sh
# Snapshot, telemetry overlay, reconnect response and a portable export.
sdlcctl control-center \
  --registry projects.json --ledger ledger.json \
  --usage usage.json --effectiveness effectiveness.json \
  --window-start 2026-10-29T00:00:00Z --window-end 2026-10-30T00:00:00Z \
  --observed-at 2026-10-29T09:00:00Z --age-seconds 30 \
  --output snapshot.json \
  --telemetry-output telemetry.json \
  --stream-output reconnect.json \
  --export-output export.json

# What a reconnecting client is sent, from a stored frame log.
sdlcctl control-center --stream frames.json --resume-from "42-9f2c…" \
  --stream-output reconnect.json

# A historical window, for offline analysis.
sdlcctl control-center --usage usage.json \
  --since 2026-10-01T00:00:00Z --until 2026-11-01T00:00:00Z \
  --telemetry-output october.json
```

Each output flag owns one document. The snapshot still defaults to stdout when
no other output is requested, so a caller who wants only the reconnect
response is not forced to name a snapshot path — mixing stdout with files
cannot be written atomically, and the grouped writer refuses it.

## What this layer deliberately cannot do

It opens no socket, holds no credential, and mutates nothing. There is no
merge, deploy, broker or model authority anywhere in it, and no path from a
frame or an overlay back into the factory: a transport built on it stays a
read-only view by construction.
