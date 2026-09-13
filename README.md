# garmin-sync

[![build](https://img.shields.io/github/actions/workflow/status/bradpenney/garmin-sync/build.yaml?branch=main&label=build&logo=githubactions&logoColor=white)](https://github.com/bradpenney/garmin-sync/actions/workflows/build.yaml)
[![pylint](https://img.shields.io/badge/pylint-10.00%2F10-brightgreen?logo=python&logoColor=white)](pyproject.toml)
[![coverage](https://img.shields.io/badge/coverage-100%25%20of%20logic-brightgreen?logo=pytest&logoColor=white)](tests/test_garmin_sync.py)
[![code style: black](https://img.shields.io/badge/code%20style-black-000000)](https://github.com/psf/black)
[![licence](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)

<!-- The build badge is LIVE — it reads the workflow and goes red on a failure.
The other four are STATIC, and that is a deliberate limitation worth knowing:
they assert what CI enforces (pylint --fail-under=10, --cov-fail-under=95,
black --check) rather than measuring it. CI is the control; these are labels
for it. If the gates in pyproject.toml change, these have to change by hand —
a static badge that has drifted is worse than no badge, because it reads as
evidence.

"coverage 100% of logic" is precise, not spin: the HTTP functions carry
`pragma: no cover` and are excluded, for the reasons given at their definition
in garmin_sync.py. -->

Turn Garmin Connect activities into [Wanderer](https://github.com/Flomp/wanderer)
trails, as a Kubernetes CronJob.

Your watch records a ride. Every 30 minutes this checks Garmin for activities it
has not seen, downloads each one's GPX track, and creates a Wanderer trail with
the distance, elevation, duration, start coordinates and a mapped category.

**It talks to PocketBase over the cluster network**, as a peer inside the
namespace — so PocketBase stays `ClusterIP` and nothing has to be published to
run this.

---

## Why it is a CronJob and not a cron job

It started as a systemd timer on the docker host, reaching PocketBase on
`localhost:8090`. Moving Wanderer into Kubernetes took that address away:
PocketBase became a `ClusterIP` Service, reachable only from inside the cluster.

The quick fix is to publish PocketBase's API so a host process can keep reaching
it. That puts an admin API on the network to save a little work. Running the
sync inside the cluster instead means **PocketBase gains no new exposure at
all** — which is the whole point of this repo.

---

## What it does, precisely

For each Garmin activity not already synced, and not older than
`GARMIN_SYNC_START_DATE`:

| Wanderer field | Source |
|---|---|
| `name` | activity name, or `Untitled` |
| `distance`, `elevation_gain`, `duration` | the activity |
| `date`, `lat`, `lon` | activity start |
| `category` | `CATEGORY_MAP[activityType.typeKey]`, empty when unmapped |
| `gpx` | downloaded from Garmin; omitted if the download fails |
| `public` | **always `false`** — publishing is a deliberate act |
| `author` | your ActivityPub actor, not your user id |

An activity is recorded as synced **only after its trail exists**. A failure
anywhere leaves it to be retried on the next run — nothing is silently dropped.
Activities are processed oldest-first, so a partial run leaves a contiguous
history rather than holes.

An unmapped activity type is not an error: the trail is created without a
category and the run logs a warning naming the type to add to `CATEGORY_MAP`.
Garmin's catch-all `other` is mapped to `UTV` because that is what the watch
reports for a side-by-side ride; if you record other things as "Other", change
or remove that entry.

---

## Configuration

All from the environment. Everything without a default is **required**, and a
missing or empty value exits non-zero at startup rather than failing later on an
API call that looks like a rejected password.

| Variable | Default | Notes |
|---|---|---|
| `GARMIN_EMAIL` | — | supply from a Secret |
| `GARMIN_PASSWORD` | — | supply from a Secret |
| `WANDERER_EMAIL` | — | supply from a Secret |
| `WANDERER_PASSWORD` | — | supply from a Secret |
| `WANDERER_URL` | `http://wanderer-db:8090` | your PocketBase Service |
| `STATE_DIR` | `/data` | **must be a volume that persists** |
| `GARMIN_SYNC_START_DATE` | unset | ISO date; ignore anything earlier |
| `GARMIN_ALLOW_INTERACTIVE_LOGIN` | unset | `true` only where a TTY exists |
| `OTEL_SERVICE_NAME` | `garmin-sync` | tags every log line |

---

## ⚠️ Seed the volume before the first run

`STATE_DIR` must already contain two things. **Skipping this is the mistake that
costs you an afternoon.**

**`garmin-tokens/`** — the Garmin OAuth token cache. Without it every run
re-authenticates, which can demand MFA. There is no TTY in a CronJob to answer
that, so the job **refuses to start** rather than hanging. That is the safe
failure, but it will never sync.

**`state.json`** — `{"synced_ids": ["...", "..."]}`, the Garmin activity IDs
already turned into trails. Without it **every past activity looks new**, and
the job creates a duplicate trail for each one, GPX upload and all. PocketBase
ships a `dedup` subcommand; this is why.

Both come from wherever the sync ran before — `.garmin-tokens/garmin_tokens.json`
and the old state file. Copy them in with the CronJob suspended:

```bash
kubectl -n <ns> patch cronjob garmin-sync -p '{"spec":{"suspend":true}}'
# start a pod that mounts the same PVC, then:
kubectl -n <ns> exec -i <pod> -- sh -c 'cat > /data/state.json' < old-state.json
kubectl -n <ns> exec -i <pod> -- sh -c 'mkdir -p /data/garmin-tokens && cat > /data/garmin-tokens/garmin_tokens.json' < garmin_tokens.json
kubectl -n <ns> patch cronjob garmin-sync -p '{"spec":{"suspend":false}}'
```

Read both back **through the volume** afterwards, rather than trusting that the
writes returned zero.

Starting fresh instead? Create `state.json` as `{"synced_ids": []}` and set
`GARMIN_SYNC_START_DATE` to bound how far back the first run reaches.

---

## Deploying

The image is deployed **by digest, never by tag** — a tag lets the running
artifact change under you at any restart.

1. Push to `main`. CI lints, tests, builds, and prints the digest to the run
   summary.
2. Put that digest in your CronJob manifest.
3. Deploy. If Flux watches an OCI artifact rather than a git branch, remember
   the artifact has to be republished first — a git push alone does not deploy.

A minimal CronJob, with the parts that matter:

```yaml
spec:
  schedule: "*/30 * * * *"
  concurrencyPolicy: Forbid
  # Forbid without these is a trap: one wedged job blocks every future run,
  # forever, and nothing reports it.
  startingDeadlineSeconds: 300
  jobTemplate:
    spec:
      activeDeadlineSeconds: 900
      template:
        spec:
          restartPolicy: OnFailure
          securityContext:
            runAsNonRoot: true
            runAsUser: 65532
            runAsGroup: 65532
            fsGroup: 65532          # or the PVC arrives root-owned
          containers:
            - name: garmin-sync
              image: ghcr.io/<you>/garmin-sync@sha256:...
              envFrom:
                - secretRef: {name: garmin-sync}
              env:
                - {name: STATE_DIR, value: /data}
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: {drop: ["ALL"]}
              volumeMounts:
                - {name: state, mountPath: /data}
                - {name: tmp, mountPath: /tmp}   # requests and garth need it
          volumes:
            - name: state
              persistentVolumeClaim: {claimName: garmin-sync-state}
            - name: tmp
              emptyDir: {}
```

If your namespace has a `default-deny` NetworkPolicy, this pod needs egress to
**DNS**, to **PocketBase**, and to **Garmin over HTTPS** — and PocketBase needs
a matching ingress rule. Without them the first API call fails as a timeout,
which reads as the remote being down.

Check your ResourceQuota too: an extra pod and an extra PVC are easy to be one
short of, and the symptom is a job that silently never schedules.

---

## Verifying it works

The useful check is behavioural, not "did the pod run":

```bash
kubectl -n <ns> create job garmin-verify --from=cronjob/garmin-sync
kubectl -n <ns> logs job/garmin-verify -f
```

**`no new activities to sync` is the PASS** when nothing has been recorded since
seeding — it proves the job read your seeded state. A run that reports several
new activities immediately after seeding did **not** see `state.json`, and is
about to create duplicates. Stop it.

---

## Logs

Structured JSON, one object per line, to stdout. Every line carries `ts`,
`level`, `msg`, `service`, and a `run_id` that correlates the lines of a single
run — several runs interleave in one stream, and without it an error cannot be
tied to the run that produced it.

```json
{"ts":"2026-09-10T04:30:01.123+00:00","level":"info","msg":"trail created","run_id":"a1b2c3d4e5f6","service":"garmin-sync","garmin_id":"24288254665","trail_id":"kx82..."}
```

Credentials, tokens and response bodies are never logged. Activity and trail IDs
are, because those are what you search by.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest pytest-cov pylint black
.venv/bin/python -m pytest        # gated at 95% of the logic
.venv/bin/pylint garmin_sync.py   # 10/10 or it fails CI
.venv/bin/black garmin_sync.py tests/
```

The HTTP calls carry `pragma: no cover`. Mocking `requests` would assert that
this file calls the functions this file calls — a green test that measures
nothing. They are covered by running the job against real services, which is
also the only thing that catches an upstream API change.

The tests call the real `select_new_activities()` rather than restating its
logic. An earlier version reimplemented the filter in the test file and stayed
green when the comparison was mutated.

## Licence

MIT — see [LICENSE](LICENSE).
