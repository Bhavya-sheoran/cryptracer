# API contracts — SIH26183

Base URL: `http://localhost:8001`, prefix `/api/v1`. Interactive docs at
`/docs`.

> All responses describing on-chain data carry a `data_source` /
> `data_provenance` field. In `DEMO_MODE` it reads `synthetic`. Nothing in this
> API presents synthetic data as live-chain data, and nothing presents any data
> as real NCRP complaint or exchange KYC data.

---

## Health

### `GET /api/v1/health/live`
Process liveness. No dependency checks.

```json
{ "status": "ok", "app": "SIH26183 Fraud-Linked Exchange Identification" }
```

### `GET /api/v1/health/ready`
Dependency check across Postgres, Neo4j and Redis.

```json
{
  "status": "ready",
  "checks": { "postgres": {"ok": true}, "neo4j": {"ok": true}, "redis": {"ok": true} },
  "demo_mode": true,
  "data_source": "synthetic",
  "phase": "5 - e2e verification, RBAC hardening"
}
```

`data_source` drives the frontend provenance banner. It is served by the backend
precisely so the UI claim cannot drift from how the system is configured.

---

## Wallets

### `POST /api/v1/wallets/validate`
Detect the chain and verify the checksum **without** creating a case. Lets the
intake form reject a mistyped address before it becomes an untraceable case.

Request:
```json
{ "address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa" }
```

Response `200`:
```json
{
  "address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
  "valid": true,
  "chain": "BTC",
  "address_norm": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
  "address_kind": "p2pkh",
  "reason": null,
  "warnings": []
}
```

Always `200` — validity is in the body, not the status code. An invalid address
returns `valid: false` with a `reason`.

**Supported formats and what is checked**

| Input | Chain | `address_kind` | Integrity check |
|---|---|---|---|
| `1...` | BTC | `p2pkh` | Base58Check (double-SHA256) |
| `3...` | BTC | `p2sh` | Base58Check |
| `bc1q...` (20 B) | BTC | `p2wpkh` | Bech32 polymod (BIP-173) |
| `bc1q...` (32 B) | BTC | `p2wsh` | Bech32 polymod |
| `bc1p...` | BTC | `p2tr` | Bech32m polymod (BIP-350) |
| `0x` + 40 hex | ETH | `eoa_or_contract` | EIP-55 when mixed case |
| `T...` | TRON | `tron_base58` | Base58Check, version `0x41` |

Notes:
- A **mixed-case** Ethereum address failing EIP-55 is **rejected** — that is the
  transcription error the checksum exists to catch. An all-lowercase address is
  accepted with a warning, since it carries no checksum to verify.
- Payment-URI prefixes (`bitcoin:`, `ethereum:`, `tron:`) and surrounding
  whitespace are stripped — victims commonly paste these straight out of a chat.
- ETH normalises to lowercase. BTC/TRON Base58 is **case-significant** and is
  preserved verbatim; bech32 normalises to lowercase.

---

### `POST /api/v1/wallets`
Report a suspect wallet. Opens a case, traces the outbound money flow, writes
the graph, and runs clustering.

Request:
```json
{
  "address": "TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a",
  "victim_ref": "SYN-DEMO-001",
  "amount_inr": 250000,
  "narrative": "Task-based earning scheme.",
  "ncrp_ref": null,
  "source": "manual",
  "trace_depth": 8
}
```

`victim_ref` is a **pseudonymous handle**. The schema has nowhere to put a name
or phone number, deliberately.

Response `201`:
```json
{
  "case_id": "a021b8c1-...",
  "case_number": "SIH183-2026-000001",
  "wallet_id": "2322957d-...",
  "address": "TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a",
  "chain": "TRON",
  "address_kind": "tron_base58",
  "reported_at": "2026-09-01T12:55:54Z",
  "duplicate": {
    "is_duplicate": false,
    "prior_case_count": 0,
    "prior_case_numbers": [],
    "note": null
  },
  "trace": {
    "trace_run_id": "38304599-...",
    "status": "complete",
    "data_source": "synthetic",
    "max_depth": 8,
    "hops_discovered": 7,
    "addresses_touched": 9,
    "transactions_ingested": 10,
    "mixer_interaction": false
  },
  "cluster": {
    "cluster_key": "tron:single:TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a",
    "heuristic": "account_single",
    "size": 1,
    "members": ["TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a"]
  },
  "warnings": [],
  "data_provenance": "synthetic"
}
```

`422` when the address fails validation, with the reason in `detail`.

**Dedupe.** A wallet already named in earlier cases returns
`duplicate.is_duplicate: true` and lists the prior case numbers. Each report
still gets its own case — one wallet, many victims — because that fan-out *is*
the fraud-ring signal.

**`mixer_interaction`** is a flag, never an unwinding. The system records that
funds touched a tagged mixer and feeds it to risk scoring; it makes no attempt
to defeat mixing.

---

### `GET /api/v1/wallets/{chain}/{address}`
Wallet detail: the cases naming it and its cluster. `422` if the address is not
valid for `{chain}`; `404` if never reported.

### `GET /api/v1/wallets/id/{wallet_id}`
Same payload, addressed by UUID.

### `GET /api/v1/wallets/multi-reported`
Wallets named in **more than one** case — candidate fraud rings. Backed by the
`v_multi_reported_wallets` view.

```json
[
  {
    "wallet_id": "2322957d-...",
    "address": "TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a",
    "chain": "TRON",
    "case_count": 2,
    "first_reported_at": "2026-09-01T12:55:54Z",
    "last_reported_at": "2026-09-01T12:56:09Z"
  }
]
```

---

## Tracing behaviour

The expansion is a breadth-first walk **in the direction the money moved**: from
an address we take the transactions it *spent* into, and those outputs become
the next hop. Transactions where the address only *received* are still written
to the graph for context — they show where the victim's money came in — but are
not expanded, or every trace would balloon backwards into unrelated history.

- Depth: `TRACE_MAX_DEPTH`, default `8`, per-request override 1-8. Raised from
  6 in Phase 2: the demo ring's exchange sits at hop 7, and depth 6 missed it
  entirely and fell back to a behavioural guess.
- Breadth: `TRACE_MAX_BREADTH`, default `25` transactions per address.
- Writes are `MERGE`-based, so re-tracing a wallet updates the graph rather than
  duplicating it.

## Clustering behaviour

| Heuristic | Chains | Rule |
|---|---|---|
| `common_input_ownership` | BTC | Addresses co-spending into one transaction share an owner. |
| `change_address` | BTC | In a 2-output spend, an output is change only if it is the *sole* output making its debut in that transaction and is not itself an input. Ambiguous cases are declined. |
| `account_single` | ETH, TRON | No co-spend structure exists; each address is its own cluster. |

Both UTXO heuristics emit `:SAME_OWNER` edges; a GDS Weakly Connected Components
pass resolves those into `:Cluster` nodes. Cluster keys derive from the
lexicographically smallest member, so a component that grows is re-keyed and
stale memberships are deleted — an address is never in two clusters.

---

## Analysis

### `GET /api/v1/wallet`
The Phase 2 deliverable: trace, attribution and risk in one response.

Query: `address` (required), `depth` (optional, 1-8, default `TRACE_MAX_DEPTH`=8).

```json
{
  "address": "TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a",
  "chain": "TRON",
  "trace_path": {
    "root": "TFxX...", "depth": 8,
    "hops": [{"address": "...", "hop": 7, "entity_name": "Meridian Exchange"}],
    "nodes": [{"address": "...", "hop": 0, "role": "reported_suspect"}],
    "links": [{"source": "...", "target": "...", "txid": "...", "value": 5988.2}],
    "node_count": 12, "link_count": 11, "truncated": false
  },
  "terminal_attributions": [
    {"address": "TRyR...", "hop": 7, "entity_name": "Meridian Exchange",
     "entity_type": "exchange", "cluster_key": "tron:single:TRyR..."}
  ],
  "attribution": {
    "method": "tagged_db",
    "entity_name": "Meridian Exchange", "entity_type": "exchange",
    "confidence": 1.0, "source": "synthetic",
    "matched_address": "TRyR...", "cluster_key": "...", "cluster_size": 1,
    "evidence": [{"address": "...", "entity": "...", "source": "...", "confidence": 1.0}],
    "note": "Attributed from a curated tag (synthetic). ..."
  },
  "risk_label": "high",
  "risk_score": 81.11,
  "risk_explanation": "Score 81.11/100 (high). 10 reported case(s) trace to this cluster ...",
  "risk_factors": ["10 reported case(s) trace to this cluster (time-decayed, 90-day half-life)"],
  "contributing_case_ids": ["...", "..."],
  "contributions": [
    {"case_id": "...", "case_number": "SIH183-2026-000149", "age_days": 0,
     "decay_weight": 1.0, "points": 10.0, "terminal_address": "TRyR..."}
  ],
  "mixer_interaction": false,
  "reported_in_cases": [{"case_number": "SIH183-2026-000149", "status": "analysed"}],
  "data_provenance": "synthetic",
  "notice": "Recommendation only. Any freeze or disclosure request requires explicit approval ..."
}
```

`404` when the address is valid but has no graph data — submit it via
`POST /api/v1/wallets` first. `422` when the address fails validation.

**`attribution.method` is the field to read first.**

| Method | Meaning |
|---|---|
| `tagged_db` | A curated tag from a public source. `source` names it (`ofac_sdn`, `etherscan_labels`, `walletexplorer`, `graphsense_tagpacks`, `synthetic`). A tag on any cluster member attributes the whole cluster, because clustering already asserted shared ownership. |
| `classifier` | No tag matched. A service *category* predicted from behaviour (deposit frequency, wallet age, counterparty diversity). **`entity_name` is always `null`** — behaviour cannot name a company, and inventing one would be fabrication. Confidence is capped at 0.75 so a guess never outranks a curated fact. |
| `none` | No tag and insufficient behavioural data. A legitimate, useful answer. |

**How the risk score is built** — deliberately arithmetic, not learned:

```
weight_i = 0.5 ^ (age_days_i / half_life_days)      # default half-life 90 days
points_i = 10.0 * weight_i
raw      = sum(points_i) + mixer_bonus(8) + sanctioned_bonus(40)
score    = 100 * (1 - exp(-raw / 60))               # saturating, 0..100
```

Time decay because an exchange that was a fraud destination two years ago and
has since tightened KYC should not rank like one receiving proceeds this week.
The saturating curve because the difference between 1 and 5 linked cases matters
far more than between 80 and 85 — a linear sum would let one prolific reporter
dominate. Thresholds: `>=70` high, `>=40` medium (configurable).

Every contributing case is returned with its `age_days`, `decay_weight` and
`points`, so the score can be recomputed by hand. That is the explainability
requirement: a "High" rating that cannot name the complaints behind it is not
actionable.

Mixer contact is **flagged, never unwound**. It raises the score as a signal;
the system makes no attempt to defeat mixing.

### `GET /api/v1/exchanges/ranked`
Exchanges ranked by fraud linkage across all cases — the repeat-destination
view.

Query: `chain` (optional filter), `limit` (default 20).

```json
{
  "entities": [
    {"entity_name": "Meridian Exchange", "entity_type": "exchange", "chain": "TRON",
     "case_count": 10, "case_numbers": ["SIH183-2026-000149"],
     "risk_score": 81.11, "risk_label": "high"}
  ],
  "scoring": {"half_life_days": 90, "medium_threshold": 40.0,
              "high_threshold": 70.0, "model_version": "risk-v1-timedecay"},
  "notice": "Scores are explainable aggregates over reported cases, not a black box."
}
```

Each entity is scored on the chain its own tagged addresses live on.

---

## Frontend consumption (Phase 3)

The dashboard at `http://localhost:5174` reads these fields, so they are part of
the contract rather than incidental:

| Field | Used for |
|---|---|
| `trace_path.nodes[].hop` | **Sankey column position.** Pinned to this value, not d3's default alignment — horizontal position must mean hop count. |
| `trace_path.nodes[].entity_name` / `entity_type` | Node colour + label (mixer, sanctioned, attributed service) |
| `trace_path.links[].value` / `txid` / `asset` | Ribbon width and hover tooltip |
| `attribution.method` | The badge the investigator reads first |
| `attribution.source` | Shown alongside every curated attribution |
| `contributions[]` | The explainability table under the score |
| `risk_factors` / `risk_explanation` | Rendered beside the number; never the number alone |
| `scoring.half_life_days` (ranked endpoint) | Displayed half-life — the UI never hardcodes it |

`GET /api/v1/wallet` also backs the deep link `/?address=<addr>`.

---

## Authentication (Phase 4)

`POST /api/v1/auth/login` — OAuth2 password flow (form-encoded, not JSON).
Returns `access_token`, `role`, `full_name`. Send it as `Authorization: Bearer <token>`.

`GET /api/v1/auth/me` — current officer, including `can_approve`.

`POST /api/v1/auth/seed-demo-users` — creates the demonstration accounts
`investigator` / `investigator123` and `supervisor` / `supervisor123`.
**Published demo credentials. Replace before any real deployment.**

Roles: `investigator` files and drafts; `supervisor` and `admin` may approve.
The role is re-read from the database on each request, not trusted from the
token, so a revoked role takes effect immediately.

---

## Cases (Phase 4)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/cases` | List cases (auth required) |
| `GET /api/v1/cases/{id}` | Case detail: wallets, traces, notes, evidence, reports |
| `POST /api/v1/cases/{id}/notes` | Add an investigation note |
| `POST /api/v1/cases/{id}/evidence` | Upload an exhibit (multipart, 25 MB cap) |
| `GET /api/v1/cases/{id}/evidence/{eid}/verify` | Recompute the exhibit digest |
| `POST /api/v1/cases/{id}/report` | Generate the hashed forensic PDF |
| `GET  /api/v1/cases/{id}/report/{rid}/download` | Download the PDF |
| `GET  /api/v1/cases/{id}/report/{rid}/verify` | Recompute the report digest |

**Chain of custody.** The SHA-256 is taken from the bytes actually written to
storage, not from anything the client claimed. It is reproducible outside the
system:

```bash
curl -s -H "Authorization: Bearer $TOKEN"   "localhost:8001/api/v1/cases/$CASE/report/$REPORT/download" -o report.pdf
sha256sum report.pdf   # must equal the sha256 the API returned
```

Uploaded filenames never determine a path on disk — exhibits are stored under a
generated name, since the filename is attacker-controlled.

---

## Freeze requests (Phase 4) — human approval is mandatory

```
draft  →  pending_approval  →  approved  →  dispatched
                          ↘   rejected
```

| Endpoint | Guard |
|---|---|
| `POST /api/v1/freeze-requests` | Always creates `draft`. Status is not settable by the caller. |
| `POST /{id}/submit` | `draft` → `pending_approval` |
| `POST /{id}/approve` | **Requires supervisor/admin AND a different officer than the requester.** `409` if not pending; `403` if the role is wrong or it is your own request. |
| `POST /{id}/reject` | Requires an approver role |
| `POST /{id}/dispatch` | `409` unless already `approved` |

Nothing in intake, scoring or alerting calls the approval path. It is reachable
only from an authenticated request made by a person, and the database refuses an
approved row without an approver (`ck_freeze_approved_needs_actor`). Every
transition is written to `audit_log` with the actor and their IP.

This prototype transmits nothing to any real exchange; `dispatch` records that
an officer sent it through their own lawful channel.

---

## Mock NCRP/1930 intake (Phase 4)

`POST /api/v1/ncrp/intake` → `202` with an acknowledgement, or `422` if the
address fails checksum validation. `GET /api/v1/ncrp/contract` documents the
expected payload.

**This is a mock.** No connection to the National Cybercrime Reporting Portal
exists or was available. Complaints accepted here are stored with
`source = "ncrp_mock"`. The schema has no field for a name, phone number or
address — `complainant_ref` is a pseudonymous reference, deliberately.

---

## STR drafts (Phase 4)

`POST /api/v1/str-drafts` (body `{case_id}`), `GET /api/v1/str-drafts`,
`GET /{id}/text` (plain text), `POST /{id}/approve` (approver role, and not the
officer who generated it).

**Draft only.** Nothing is filed with FIU-IND and there is no FinNet connection.
The draft states its own limitations — no KYC, no account holder, probabilistic
clustering — and carries an officer sign-off block.

---

## Alerts (Phase 4)

`WS /api/v1/ws/alerts?backlog=20` — sends a `hello` frame declaring
`data_source`, then replays the recent backlog, then streams live alerts.
Periodic `ping` frames keep the socket open.

`GET /api/v1/alerts/recent` — polling fallback for clients that cannot hold a
socket open.

Transport is Redis Streams (`sih183:alerts`), chosen over pub/sub because a
stream is replayable: an alert raised while no investigator had the dashboard
open is still delivered when one connects. Only **Medium and High** resolutions
alert — alerting on Low would train investigators to ignore the feed.

---

## Access control (Phase 5)

| Class | Endpoints |
|---|---|
| **Public** | `/health/*`, `POST /wallets/validate`, `POST /wallets` (victim intake), `POST /ncrp/intake`, `GET /ncrp/contract`, `POST /auth/login` |
| **Authenticated** | `GET /wallet`, `GET /exchanges/ranked`, `GET /alerts/recent`, `WS /ws/alerts`, `GET /wallets/multi-reported`, all `/cases/*`, `/freeze-requests` (create/list/submit), `/str-drafts` (create/list) |
| **Approver only** | `POST /freeze-requests/{id}/approve` · `/reject` · `/dispatch`, `POST /str-drafts/{id}/approve` |

The authenticated set was closed during the Phase 5 security pass: each of those
responses names case numbers, case ids, or the exchange a case resolved to.
Victim intake stays open deliberately — a complaint must be fileable without an
account.

The alert WebSocket takes its token as a query parameter (`?token=…`) because a
browser `WebSocket` cannot set an `Authorization` header. It is verified exactly
as the HTTP dependency verifies it, the user is re-read from the database, and
an unauthenticated socket is closed **before** `accept()`, so it never sees a
frame.

Verify with:

```bash
docker compose exec backend python scripts/rbac_audit.py   # behavioural probe
docker compose exec backend python scripts/e2e_demo.py     # 42 end-to-end checks
```
