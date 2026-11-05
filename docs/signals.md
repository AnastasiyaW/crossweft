# Signals: an alarm on every link of the map

The map already says which service talks to which, over which route or queue.
With `meta.signals` it also says how each of them is watched, and `check` fails
when a link, a running service or a flow is added without an alarm. `crossweft
signals` turns the same model into a catalog of alarm rules for the alert
router.

Nothing changes for a map without `meta.signals`: the rules are opt-in, so an
upgrade does not fail repositories that have not adopted them.

## What must carry a signal

| Entry (status `current`) | Signal it needs | Problem key when it has none |
|---|---|---|
| link over `https`, `http-loopback`, `webhook`, `mtls` | `signals.red`: requests, errors, latency | `signal:missing:link:<id>` |
| link over `queue` | `signals.queue`: depth, age of the oldest message, rate, and the dead-letter queue | `signal:missing:link:<id>` |
| block of kind `service` or `site` | `signals.health` or, for a background worker, `signals.heartbeat` | `signal:missing:block:<id>` |
| flow | `signals.probe`: a synthetic run of the whole chain from outside | `signal:missing:flow:<id>` |

Any of them can say why it has no signal instead: `"signals": {"skip": "<reason>"}`.
The reason lands in the catalog under `skipped`, so the absence is visible. A
skipped block may still name its `owner`, `product`, `runbook` and `audience`;
it may not also declare a signal. `red` is only for the HTTP transports above
and `queue` only for a `queue` link: anything else is a schema error.

The check also reads the code:

- a heartbeat names the code that sends it (`heartbeat.anchors`); a moved or
  deleted sender is an anchor problem;
- `"health": "GET /path"` on a `route_server` block must be a route the server
  registers (`signal:health-unserved:block:<id>:<check>`), otherwise the health check
  would always fail. Without `meta.route_scan` nothing can verify that, and a full
  check fails with the same key: add the route scan or give the check as a URL;
- with `meta.signals.trace_header`, both ends of every HTTP link must mention that
  header in their code (`signal:trace:link:<id>:<from|to>`): one request id passes
  through HTTP headers, message metadata and logs.

## `meta.signals`

| Field | Required | Meaning |
|---|---|---|
| `notify` | yes | where alarms go: the names of channels your alert router knows, e.g. `["chat", "email"]` (lowercase, distinct) |
| `watchdog` | yes | `{"every": "1m"}` -- an alarm that fires all the time; when it stops arriving, the monitor itself is down |
| `trace_header` | no | e.g. `traceparent` (W3C trace context); turns on the trace rule above |
| `product`, `owner`, `runbook` | no | defaults for every catalog entry |

Signals on an entry without `meta.signals` are a schema error: nothing would say
who gets the alarm.

## `signals` on blocks, links and flows

Durations are `30s`, `10m`, `1h`. A threshold pair is `[warn, crit]`.

**Block**

| Field | Meaning |
|---|---|
| `health` | `"GET /healthz"` (a route of this block), a full `https://` URL, or a list of them -- one per instance |
| `url` | where the service answers, used to build the probe URL of a route health check |
| `heartbeat` | `{"every": "60s", "anchors": [{"path", "find"}]}` -- a background worker reports in every interval |
| `audience` | `customer` (alarms are `crit`) or `internal` (default, `warn`) |
| `owner`, `product`, `runbook` | override the `meta.signals` defaults |
| `skip` | the reason this block has no signal |

**Link**

| Field | Meaning |
|---|---|
| `red` | `true`, or `{"error_rate": [0.02, 0.10], "p95": ["3s", "10s"]}` |
| `queue` | `{"name", "dlq", "max_age"?: ["10m", "30m"], "depth"?: [100, 1000]}`; any message in `dlq` is a `crit` alarm |
| `skip` | the reason this link has no signal |

**Flow**

| Field | Meaning |
|---|---|
| `probe` | `{"every", "creates", "expects", "cleanup"}` -- create a test object, wait until it has passed the queue and reached the database, remove it |
| `skip` | the reason this flow has no probe |

Example (the demo in `examples/polyglot-shop`):

```json
"meta": {"signals": {"notify": ["chat", "email"], "watchdog": {"every": "1m"},
                     "trace_header": "traceparent", "product": "polyglot-shop"}},
"blocks": [{"id": "api", "...": "...",
            "signals": {"health": "GET /healthz", "url": "https://shop.example.com",
                        "audience": "customer"}},
           {"id": "worker", "...": "...",
            "signals": {"heartbeat": {"every": "30s",
                        "anchors": [{"path": "worker/worker.py", "find": "def beat("}]}}}],
"links": [{"id": "web-orders", "...": "...", "signals": {"red": true}}]
```

## `crossweft signals`: the catalog

```bash
crossweft signals --out build/signals.json
```

Prints (or writes) `crossweft.signals.v1` JSON: `notify`, `trace_header`,
`rules`, `skipped` and `missing`. One rule per signal:

| `kind` | From | What the router watches |
|---|---|---|
| `health` | block `health` | HTTP check every minute, 5 s timeout; opens after 3 failures, closes after 2 successes |
| `heartbeat` | block `heartbeat` | key `heartbeat:<block>`; `warn` after 3 missed intervals, `crit` after 6 |
| `red` | HTTP link | error rate over 10 min (at least 10 errors) and p95 latency over 5 min, for the link's routes |
| `queue` | queue link | oldest message `warn` 10 min / `crit` 30 min, depth when given, rate |
| `dlq` | queue link | any message in the dead-letter queue, `crit` |
| `probe` | flow `probe` | the synthetic run and the flow's steps |
| `watchdog` | `meta.signals.watchdog` | fires every interval; stale after two |

The defaults are those of the ops alert plan; a link or block overrides them.
Exit code: 0 complete, 1 an entry lacks a signal it needs (listed under
`missing`, the same list `check` fails on), 2 the map does not load or has no
`meta.signals`.

The catalog depends on the model alone, like `render`. It says *what* to watch;
the router decides delivery (a chat, e-mail, a pager), deduplication, silences and
escalation. No Prometheus or Alertmanager is assumed.

## On the map

`render` draws a bell on every block and link with a signal; the details panel
lists them. In `crossweft report`, a missing signal is a problem like any other:
the link, block or flow is red and the panel says what to add.
