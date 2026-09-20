# Public service status

The Stack Gateway serves unauthenticated `GET` and `HEAD /status.json` on the
Stack Console origin, including Public Mode. The version 1 document exposes only
fixed component IDs, states, timestamps and allowlisted image/version identifiers.
It contains no installation names, credentials, URLs, environment, probe output or
errors. This is an informational observation interface, with no write actions.
Caddy reads `data/console/status.json` through its existing read-only mount and
receives no Docker socket or administrative endpoint.

This deliberately extends the previous operator-only version metadata posture:
allowlisted version numbers and image digests in `/status.json` are public even in
Public Mode. `/versions.json` remains restricted by `LG_OPERATOR_ALLOW`. Neither
interface authorizes administration or application access.

## Install independently

Python 3.11+, Docker CLI with Compose, Docker access for the ordinary installation
user, and a running systemd user manager are required. HTTP probes also require
that the host can reach the containers' default bridge IP addresses. Rootless or
remote Docker hosts may not provide that route. Keep the checkout and env file
available at their selected absolute paths. Put persistent Compose overlays,
project name and image overrides in that env file; interactive shell overrides
are deliberately excluded from configuration inspection. Docker connection
settings are inherited from the invoking environment or user manager.

Bootstrap records its preparation/startup outcome and attempts an initial observation.
Periodic observation is opt-in; bootstrap does not install host units. Run these as the installation user after reviewing
and deploying the gateway configuration:

```sh
python3 /absolute/checkout/scripts/status_observer.py \
  --checkout /absolute/checkout --env-file /absolute/checkout/.env
python3 /absolute/checkout/scripts/install_status_timer.py --install \
  --checkout /absolute/checkout --env-file /absolute/checkout/.env
systemctl --user status llm-gateway-status.timer
```

The installer writes `llm-gateway-status.service` and `.timer` to the user's
systemd directory and enables the timer. It refuses existing units. One timer
selects one checkout/env pair per user. It does not configure lingering: for
observations after logout, the host operator must enable the installation user's
systemd lingering according to host policy. Without a running user manager the
file expires. Inspect `journalctl --user -u llm-gateway-status.service` for fixed
failure messages; no probe bodies or configuration documents are logged.

To remove it or select another installation:

```sh
systemctl --user disable --now llm-gateway-status.timer
systemctl --user stop llm-gateway-status.service
rm "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/llm-gateway-status.service" \
   "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/llm-gateway-status.timer"
systemctl --user daemon-reload
```

Existing documents expire normally after removal. If installation fails after
writing units, first disable the retained timer and service with `systemctl --user disable --now` as above, then inspect or remove that pair before retrying. No host unit is enabled
by repository development, validation or bootstrap.

Status recording is best-effort and never replaces a bootstrap failure. Both
`data/` and its status directories must have safe ownership and permissions for
execution recording. A group-writable `data/` can leave that record unknown while
the stack still starts normally.

The observer preserves existing directory permissions and refuses symlinks in
publication paths, special files, hardlinked destinations, and destination
directories owned by another user or writable by group/others. New public
directories/files use 0755/0644 subject to the caller's umask. Existing restrictive
directories are never widened automatically. For an older `data/console` created
with group write enabled, the operator must remove that write permission before
installing the observer. Caddy's container user must be able to read the resulting
public directory/file. Private bootstrap records use `data/status` and 0700/0600.
The env file is read only by Compose and is never copied into a unit or public file.

## Evidence and limits

| Component | Positive readiness evidence | Runtime version evidence |
| --- | --- | --- |
| Caddy | Its unpublished listener answers the fixed `/health/status` response | `caddy version` in its container |
| LiteLLM | `/health/readiness` reports `db: connected` | Readiness version, otherwise installed `litellm` distribution metadata |
| Langfuse web | `/api/public/health?failIfDatabaseUnavailable=true` reports `OK`, and `/api/public/ready` succeeds | Health response version |
| Langfuse worker | Its own `/api/ready` reports `ok` | Only if its response supplies a recognized version; current upstream supplies no version |
| Postgres | Both `langfuse` and `litellm` databases accept local `SHOW server_version` | The two server version responses agree |
| ClickHouse | Local authenticated `SELECT version()` succeeds | Query result |
| Valkey | Authenticated local `PING` returns `PONG` and `INFO server` succeeds | `valkey_version` from INFO |
| RustFS | S3 listener `/health/ready` succeeds | `rustfs --version` in its container, when supported |
| Postgres exporter | Its own `/metrics` has `pg_up 1`, with no failed `pg_up` sample | Its exporter build-info version |
| Valkey exporter | Its own `/metrics` has `redis_up 1`, with no failed `redis_up` sample | Its exporter build-info version |

These observations prove only their listed checks. Caddy's internal listener does
not verify public TLS or application routes. Binary/distribution versions describe
the inspected running container, not a second process's memory after an in-place
binary replacement. Web or worker readiness does not prove trace ingestion, job
progress, object round-trips, provider completion, durable recovery or telemetry
collection. No user data is queried. No Docker health records are trusted.

Known endpoints returning 401/403/404 and missing probe executables produce
`unknown`; failed, oversized, malformed or timed-out supported probes produce
`unavailable`. Missing bridge addressing, paused containers and multiple replicas
remain unknown. A stopped service is unavailable; a restarting container is
starting. An inventory that finds other containers in the selected project can
prove an individual configured service absent. An entirely empty inventory is
unknown because a shell-only project selection may differ from the env file.
Inspection failures cannot prove absence. Omitted
services stay unknown; this producer has no explicit disable setting and emits
no inferred disabled state. Telemetry remains unknown because this checkout
cannot inspect the external collector's configuration or collection success.

`configuredVersion`/`configuredDigest` come from freshly resolved Compose
configuration, including the selected env file and its overlays. Only numeric
release patterns and documented image variants are recognized; other tags become
`custom`. `observedImageId` is Docker's local content ID, not the registry manifest
digest. Container identity is checked against project and service labels before
probing. Version probes never borrow configured image tags.

Only `bootstrap` and `rustfs-init` are tasks. Bootstrap's protected record is tied
to its exact env path; render-only and preflight refusals do not record an
execution. Bootstrap records success after gateway readiness and failure during
preparation/startup. Abrupt termination leaves the record unknown; a caught interrupt records unavailable. RustFS initialization
uses its own container start/finish/exit record. An unexecuted or missing task
record is unknown, with no invented execution time. Old execution dates remain
unchanged when their records are inspected again; task success describes that
execution only. Removing the initializer container removes its available record.

## Freshness and bounds

The timer starts after ten seconds and repeats thirty seconds after the previous
oneshot finishes. A file lock prevents concurrent publications. Four components
are probed at a time. Each command/HTTP exchange has a four-second host deadline;
Compose inspection and inventory each have ten seconds. Commands executed inside
containers also have a three-second kill deadline. HTTP responses are capped at
256 KiB; command output at 64 KiB, configuration/inspection at 1 MiB. Both stdout
and stderr count toward the command budget. HTTP uses fixed paths and inspected
IP addresses without redirects, proxies or credentials. The complete public
file is capped at 64 KiB and has twelve components, below the contract's 32 limit.

Configuration and component TTLs are 120 seconds, within the contract's 1..300
seconds. Configuration time dates its own inspection. Component time dates the
start of its bounded container-inspection/probe transaction, including any image
or runtime version evidence. Task observation time dates record inspection;
`lastExecutionAt` remains the execution start. `generatedAt` dates final document
assembly and never renews evidence. Publication uses fsync and atomic replacement.
A failed configuration inspection publishes unknown facts. A publication failure
leaves the previous complete document with its original timestamps.

Caddy supplies its current HTTP Date and `application/json`, `Cache-Control:
no-store`. It strips authorization/cookies and conditional/range request headers,
so a repeated fetch receives the same frozen evidence without a 304 refresh or
partial JSON. Missing files and errors return an empty failure response, with no
HTML fallback. Public Mode HTTP retains the existing HTTPS redirect policy.
Consumers compare Date with their UTC clock (at most five seconds disagreement),
then independently age the configuration and observations. A fresh Date or
`generatedAt` cannot make an expired observation healthy.

Runtime acceptance remains necessary: `scripts/validate.sh`, the gateway contract
in `scripts/smoke-access.py`, and host observer/timer checks on the selected
installation. Unit tests use fake Docker runners and do not establish acceptance
of the pinned images. Probe semantics follow the upstream
[LiteLLM](https://docs.litellm.ai/docs/proxy/health),
[Langfuse](https://langfuse.com/self-hosting/configuration/health-readiness-endpoints),
[worker readiness implementation](https://github.com/langfuse/langfuse/blob/main/worker/src/features/health/index.ts),
and [RustFS](https://docs.rustfs.com/en/operations/status-check) interfaces.

Before dialing a container address, the observer verifies a local Unix Docker
endpoint, a rootful daemon and the selected project's local bridge network. Remote,
rootless and other network modes retain unknown readiness instead of probing a
possibly unrelated host address. Concurrent observations coalesce under the
installation's file lock; the invocation that finds it held exits successfully.

Bootstrap bounds the optional initial observation to 120 seconds; its failure does not change successful stack startup. Conflicting Docker connection settings yield unknown observations.
