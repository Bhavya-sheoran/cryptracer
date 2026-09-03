# SIH26183 — Real-Time Identification of Fraud-Linked Cryptocurrency Exchanges

Prototype for Smart India Hackathon 2026, Problem Statement **SIH26183**
(Ministry of Home Affairs · Blockchain & Cybersecurity).

A victim reports a suspect wallet address. The system traces the flow of funds
across hops, attributes the terminal address cluster to an exchange/VASP, scores
that exchange's fraud linkage against prior reported cases, and hands an
investigator an explainable, actionable result.

---

## Honest scoping — read this first

**On the problem statement.** The official SIH portal has not published an
expanded background or expected-outcome annexure for SIH26183 (unlike its
sibling SIH26184). The architecture here is a considered engineering
interpretation of the problem statement *title*, not a paraphrase of an official
brief.

**On the data.** This system contains **no real NCRP complaint data and no real
exchange KYC data**, and no such access is claimed anywhere in this repository.
Everything it operates on is one of:

- **Synthetic** — victim complaints and the fraud-ring transaction graph are
  produced by `scripts/generate_synthetic_complaints.py`, a permanent part of
  this codebase.
- **Public datasets** — the Elliptic Bitcoin Dataset (classifier training and
  validation), Etherscan Label Cloud, GraphSense TagPacks, and the OFAC SDN
  crypto address list (VASP tagging seed data).
- **Public blockchain indexer APIs** — Etherscan, TronGrid, Blockchair — when
  API keys are supplied and `DEMO_MODE=false`.

The backend reports its own provenance at `/api/v1/health/ready`, and the UI
banner is rendered from that field rather than from hardcoded copy, so the claim
on screen cannot drift from how the system is actually configured.

**On enforcement actions.** The system **recommends**; an authorised officer
**approves**. Freeze requests and disclosure/STR drafts are created in a `draft`
state and cannot reach `approved` without an explicit click by a user holding the
`supervisor` role. Nothing auto-fires. This is enforced at three layers: a
database `CHECK` constraint, a service-layer role check, and a test.

---

## Status

| Phase | Scope | State |
|---|---|---|
| **0** | Repo scaffold, docker-compose, Postgres + Neo4j schema | **complete** |
| **1** | Wallet intake, chain detection, connectors, graph writer, clustering | **complete** |
| **2** | VASP attribution, fraud-risk scoring, `/api/wallet` | **complete** |
| 3 | React (JSX) investigator dashboard, Sankey trace view | not started |
| 4 | Case management, hashed PDF export, mock NCRP, STR drafts, freeze workflow, alerts | not started |
| 5 | End-to-end integration test, RBAC/security pass | not started |

---

## Architecture

```
victim report ──▶ FastAPI intake ──▶ chain detect ──▶ blockchain connectors
                       │                                (Etherscan/TronGrid/Blockchair
                       │                                 or synthetic in DEMO_MODE)
                       ▼
                 Neo4j graph writer ──▶ clustering (common-input-ownership,
                       │                            change-address) via GDS
                       ▼
                 multi-hop trace (default depth 6) ──▶ terminal cluster
                       │
                       ▼
                 VASP attribution ──▶ tagged-address DB (public sources)
                       │              └─ fallback: behavioural classifier
                       ▼
                 fraud-linkage score (time-decayed, per contributing case)
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
   Redis Stream ──▶ WebSocket   PostgreSQL (cases, scores, evidence, audit)
          │                         │
          ▼                         ▼
   live alert on dashboard    React (JSX) investigator dashboard
```

| Layer | Choice |
|---|---|
| Backend | Python 3.11 · FastAPI |
| Graph | Neo4j 5.26 Community + Graph Data Science |
| Relational | PostgreSQL 16 |
| Events | Redis Streams → WebSocket |
| ML | scikit-learn / XGBoost, trained on the public Elliptic dataset (a PyTorch Geometric GNN remains a stretch goal only) |
| Frontend | React 18 · **plain JavaScript + JSX only** · Vite 6 · D3 / d3-sankey |
| Auth | Simplified JWT with role claims |
| Infra | Docker Compose |

Architecture and design rationale: **[docs/architecture.md](docs/architecture.md)**.
Live demo click-path: **[docs/demo-script.md](docs/demo-script.md)**.
Full data model: **[docs/schema.md](docs/schema.md)**.
API contracts: **[docs/api.md](docs/api.md)**.

### What Phase 1 actually does

1. **Intake** validates the address at *checksum* level, not with a regex —
   Base58Check for BTC/TRON, bech32/bech32m polymod for segwit, EIP-55 for
   Ethereum. A mixed-case ETH address that fails EIP-55 is rejected, because
   that is exactly the transcription error the checksum exists to catch.
2. **Dedupe** resolves the address to a single global wallet row. One wallet
   named by several victims yields several cases — that fan-out *is* the
   fraud-ring signal, surfaced at `/api/v1/wallets/multi-reported`.
3. **Connectors** fetch transactions. `DEMO_MODE=true` serves the synthetic
   fraud ring; otherwise Etherscan / TronGrid / Blockchair. If a live connector
   is requested but unconfigured it falls back to synthetic **and reports
   `synthetic` as the source** rather than misrepresenting provenance.
4. **Tracing** walks breadth-first in the direction the money moved, to a
   configurable depth (default 6).
5. **Graph writer** MERGEs both the full UTXO shape
   (`:Address`→`:Transaction`→`:Address`) and a denormalised `:TRANSFERRED`
   edge for cheap multi-hop traversal. Idempotent.
6. **Clustering** applies common-input-ownership and a deliberately
   conservative change-address heuristic, both emitting `:SAME_OWNER` edges,
   then resolves them with a GDS Weakly Connected Components pass.

### What Phase 2 actually does

7. **VASP attribution**, two tiers, and the tier used is always reported:
   `tagged_db` (a curated tag from a public source, citing that source) or
   `classifier` (no tag matched; a category predicted from behaviour). The
   behavioural tier **never invents a company name** - it returns a category and
   a confidence, because behaviour alone cannot identify a company. `none` is a
   legitimate result; a fabricated attribution is not.
8. **Fraud-linkage scoring** aggregates how often victim-reported wallets
   terminate at an exchange, time-decayed with a configurable half-life
   (default 90 days) and squashed onto 0-100 so one prolific reporter cannot
   dominate. Every contributing case is stored with its age, decay weight and
   points, so the number is reconstructable by hand.
9. **`GET /api/v1/wallet`** returns trace path, attribution, risk label, risk
   score and the contributing case IDs in one response.

### What Phase 3 actually does

Open **http://localhost:5174**. Plain JavaScript + JSX throughout — no
TypeScript, PropTypes for runtime type-checking.

10. **Intake form** validates as you type against `/wallets/validate`, so a
    mistyped address is rejected *before* it becomes a case that can never be
    traced. The feedback is real checksum validation (Base58Check, bech32
    polymod, EIP-55), not a regex. The victim field is labelled pseudonymous and
    the copy tells the investigator not to enter PII.
11. **Sankey money-flow trace** (D3 `d3-sankey`). Columns are pinned to the
    backend's own `hop` number rather than d3's default alignment — otherwise
    every dead-end is drawn in the last column, showing a peel-off at hop 3
    level with the terminal exchange at hop 7 and visually asserting a depth
    that is not true. Ribbon width is value-proportional with a floor, so
    low-value peel-chain hops stay visible instead of becoming hairlines.
    Colour marks the reported wallet, attributed services, mixers and sanctioned
    addresses. Nodes are clickable and every node/ribbon has a hover tooltip
    with exact amounts and transaction ids.
12. **Attribution card** leads with the *method* badge — `CURATED TAG` vs
    `BEHAVIOURAL GUESS` vs `UNATTRIBUTED` — because whether an attribution is a
    sourced fact or a model's guess matters more than the name attached to it.
    The source (`ofac_sdn`, `etherscan_labels`, …) is always shown.
13. **Risk panel + contributing cases.** The score is never displayed alone: the
    label, the meter with threshold ticks, the contributing factors, and a table
    of every contributing case with its age, decay weight and points. The table
    footer sums the raw points and shows the resulting score, so an investigator
    can recompute the number by hand. That is the explainability requirement.
14. **Deep links.** `?address=<addr>` analyses on load, so a case can be shared
    as a URL rather than a description of where to click.
15. **Freeze workflow** is live as of Phase 4 (see below).

### What Phase 5 actually does

**`scripts/e2e_demo.py`** drives the whole system over HTTP and asserts each
stage really happened, printing the value it observed so a failure names the
broken link. **42/42 checks pass.** It covers intake and checksum rejection,
graph build, clustering, attribution with its source, the explainable score,
alert publication, live delivery over the authenticated WebSocket, an
independently verified PDF hash, evidence chain of custody, every path through
the freeze approval gate, the mock NCRP feed, the STR draft, and that
case-linked data is unreachable anonymously.

```bash
docker compose exec backend python scripts/e2e_demo.py
docker compose exec backend python scripts/rbac_audit.py
```

**`scripts/rbac_audit.py`** probes every endpoint three ways — anonymous, as an
investigator, as a supervisor — and fails if any is reachable more freely than
its class allows. It tests behaviour, not declarations: a forgotten
`Depends(get_current_user)` shows up as a 200 where a 401 belongs. It also
checks separation of duties, token forgery, and input handling.

#### The finding this pass produced

Four endpoints were serving the **investigation picture to anonymous callers**:

| Endpoint | What it leaked |
|---|---|
| `GET /api/v1/wallet` | contributing case numbers, case ids, reported timestamps |
| `GET /api/v1/exchanges/ranked` | the case numbers behind every exchange score |
| `GET /api/v1/alerts/recent` | alert text naming the case and destination exchange |
| `GET /api/v1/wallets/multi-reported` | which addresses recur across complaints |

A single unauthenticated request returned 23 case numbers and the exchanges
under scrutiny. All four now require a signed-in officer, as does the alert
WebSocket (token as a query parameter, since a browser socket cannot set
headers). The dashboard gates those views behind sign-in.

Worth being explicit: the first run of the audit reported *no findings*, because
the audit's own classification said those endpoints were meant to be public —
written by the same author as the endpoints. The finding only surfaced when the
responses were read rather than the status codes counted.

### Interface design

The dashboard is hand-built — no component library, no image assets, 19 `.jsx`
files and a token-based stylesheet. The layout conventions are drawn from the
tools investigators already use, adapted rather than copied:

| Pattern | Borrowed from | Why it earns its place here |
|---|---|---|
| Light theme by default, dark as a real second palette | Etherscan, TRM, MetaSleuth | Findings get screenshotted into briefs, where a dark capture reads badly. Dark follows the OS preference on first visit. |
| Persistent global search, `/` to focus | Etherscan, developer consoles | An address is the entry point to everything; it should never be more than one keystroke away. |
| Address chip: truncated mono + one-click copy + inline entity tag | Etherscan | A raw 42-character hex string is unusable. This is the single most repeated element in the UI. |
| Summary stat row above the detail | Etherscan address pages | Chain, hops, terminal service and risk answer "what am I looking at" before any scrolling. |
| Tabbed detail (Flow / Attribution / Risk / Case file) | MetaSleuth, Arkham | A trace produces four kinds of evidence; stacking them means scrolling past three to reach the fourth. |
| Left rail for Investigate / Cases / Alerts / Exchanges | Compliance consoles generally | These are destinations, not sections of a document. |
| Expandable, pinnable flow graph with entity labels | MetaSleuth fund-flow maps | Following hops is the core task; nodes carry their attribution inline. |
| Relative timestamps with exact value on hover | Every block explorer | "3m ago" is scannable; evidence needs the precise instant. |

**Design system.** `styles/tokens.css` holds one source of truth for colour,
type, spacing and elevation; components reference tokens and never hardcode a
hex value. Dark theme redefines only the applied tokens, so a component written
against `--bg` / `--text` needs no dark-specific rule — including the D3 Sankey,
which reads its palette from the same tokens at render time.

Colour is semantic, never decorative: green means low-risk or an attributed
service, amber means a mixer, red means sanctioned or high risk. Mixer contact
gets its own tone rather than reusing red, so an investigator never reads
"touched a mixer" as "sanctioned entity".

### What Phase 4 actually does

16. **Officer sign-in** with JWT + role claims. Two roles matter:
    `investigator` files and drafts; `supervisor` is the **only** role that can
    approve a freeze or an STR. The role is re-read from the database on every
    request rather than trusted from the token, so revoking it takes effect
    immediately instead of at token expiry.
17. **Case file**: notes, evidence upload, and a hashed forensic PDF.
18. **Chain of custody.** Every exhibit and every exported report stores a
    SHA-256 of the bytes actually written. `Verify hash` re-reads the file and
    recomputes — and the digest is reproducible outside the system entirely
    (`sha256sum report.pdf`).
19. **Mock NCRP/1930 intake** (`POST /api/v1/ncrp/intake`). Models the contract
    such an integration would need. Not connected to anything real; every
    complaint it accepts is stored with `source = "ncrp_mock"`, and it rejects a
    bad checksum at the boundary rather than opening an untraceable case.
20. **FIU-IND-style STR draft.** Draft only — nothing is filed, and the document
    says so. It also states its own evidential gaps (no KYC, no account holder,
    clustering is probabilistic), because a narrative that hides them is worse
    than one that names them.
21. **Freeze workflow with mandatory human approval** — see below.
22. **Real-time alerts**: Redis Streams → WebSocket → dashboard. A Medium/High
    resolution pushes an alert; **Low deliberately does not**, because alerting
    on everything trains investigators to ignore the feed. The stream is
    replayable, so an alert raised while nobody had the dashboard open is still
    delivered on connect.

### The freeze workflow: how "no auto-fire" is actually enforced

```
draft  →  pending_approval  →  approved  →  dispatched
                          ↘   rejected
```

Five independent guards, each with a test:

| Guard | Enforced by |
|---|---|
| Creation never yields an approved request | `status` is hardcoded to `draft`; not settable by the caller |
| A draft cannot be approved | `409` unless status is `pending_approval` |
| Only a supervisor/admin can approve | `require_approver` dependency → `403` |
| The requester cannot approve their own request | Explicit identity check → `403`, even for a supervisor |
| Nothing is dispatched unapproved | `409` unless status is `approved` |

Plus a database constraint (`ck_freeze_approved_needs_actor`) that refuses an
approved row with no approver, and an audit-log entry for every transition.

**No code path in this system calls the approval function.** Not intake, not
scoring, not alerting. It is reachable only from an authenticated HTTP request
made by a person. There is a test (`test_no_analysis_call_ever_creates_an_approved_freeze`)
that runs the whole pipeline and asserts no approval appeared.

Verified against the live database after the test run:

```
status            count  with_approver
approved              4              4
dispatched            5              5
-- approved/dispatched rows lacking an approver: 0
```

### The tagged-address database

Seeded from the public sources named in the brief, **3,221 tags / 1,472
entities**, every one recording where it came from:

| Source | Tags | What it is |
|---|---:|---|
| Etherscan Label Cloud | 1,066 | via GraphSense `etherscan-wordcloud-*` packs |
| OFAC SDN | 948 | US Treasury sanctioned digital-currency addresses |
| GraphSense OFAC pack | 544 | community-curated sanctions tags |
| WalletExplorer | 386 | BTC service-wallet attributions |
| GraphSense TagPacks | 272 | exchange + mixer packs |
| Synthetic (demo ring) | 5 | fictional, marked `source: synthetic` |

By type: 949 exchange, 948 sanctioned, 240 darknet, 201 gambling, 184 mixer.

Refresh with:
```bash
docker compose exec backend python ml/src/download_data.py all
docker compose exec backend python ml/src/seed_tags.py
```

### Fraud classifier - real held-out numbers

Trained on the **public Elliptic Bitcoin Dataset** (46,564 labelled
transactions, 4,545 illicit, 182 features), validated on a **temporal split** -
train on time steps 1-34, test on 35-49. A random split would leak future
information (transactions within a time step are highly correlated) and inflate
these numbers, so it is not used.

| Metric (illicit class, held out) | Value |
|---|---:|
| Precision | **0.8824** |
| Recall | **0.7341** |
| F1 | **0.8014** |
| ROC-AUC | **0.9299** |
| Average precision | **0.8048** |

Held-out confusion matrix: TP 795, FP 106, FN 288, TN 15,481 (16,670 rows,
1,083 illicit). Full report: `ml/artifacts/metrics.json`.

Trained and validated with xgboost's **native Booster API**, not the
scikit-learn wrapper. The wrapper writes `_estimator_type` into the model JSON,
which scikit-learn 1.9 no longer defines, so a wrapper-written artifact stops
loading the moment the image is rebuilt onto a different xgboost. The native
JSON loads across xgboost 2.x and 3.x; `test_model_artifact.py` guards this.

Recall is materially lower than precision, and that is the honest result rather
than a tuning failure: the Elliptic data contains a known distribution shift
after time step 43 (the "dark market shutdown"), which the temporal protocol
deliberately exposes. Reproduce with:

```bash
docker compose exec backend python ml/src/train_fraud_clf.py
```

### Stretch goal: a GNN, and an honest negative result

Your brief listed a PyTorch Geometric GNN as a stretch goal once the MVP was
done. It is built, reproducible, and **it does not beat the baseline** — which
is the result, not a failure to report.

2-layer GraphSAGE over the full 203,769-node / 234,355-edge Elliptic graph,
same temporal split and same metrics as the XGBoost baseline:

| Metric (illicit, held out) | XGBoost | GraphSAGE | Δ |
|---|---:|---:|---:|
| Precision | **0.8824** | 0.5672 | −0.3152 |
| Recall | **0.7341** | 0.6076 | −0.1265 |
| F1 | **0.8014** | 0.5867 | −0.2147 |
| ROC-AUC | **0.9299** | 0.8900 | −0.0399 |

This matches the published finding for Elliptic: in Weber et al. (2019) Random
Forest outperformed a GCN on illicit recall (0.67 vs 0.51); this GraphSAGE sits
between the two. Elliptic's node features already encode aggregated
neighbourhood statistics, so much of what message passing would add is present
in the features, and the post-step-43 distribution shift hurts the graph model
harder.

**XGBoost remains the shipped model. The GNN is not wired into the API.**

Two real bugs surfaced getting here, both worth recording:

1. **A gutted graph.** Building it from labelled nodes only discarded 197,731 of
   234,355 edges — Elliptic's labelled transactions connect to each other
   *through* unlabelled ones. Fixed by including all 203,769 nodes for message
   passing while keeping loss and metrics on labelled nodes only.
2. **Unnormalised features.** The matrix spans −13 to 445,268 with σ≈300. Trees
   are scale-invariant so the baseline never cared; the GNN's first-epoch loss
   was 35 (cross-entropy should start near 0.69) and it collapsed to predicting
   one class. Z-scoring — fitted on **training nodes only**, to avoid leaking
   the held-out tail — moved F1 from 0.137 to 0.587 and ROC-AUC from 0.576 to
   0.890.

```bash
docker compose exec backend python ml/src/download_data.py elliptic-full   # ~67 min
docker compose exec backend python ml/src/download_data.py edgelist
docker compose --profile ml run --rm ml python ml/src/train_gnn.py
```

PyTorch lives in a **separate `ml/` image** (1.9 GB) behind a compose profile,
so the API image stays at 1.46 GB and the service never ships a training stack
it cannot use.

**What this model is not.** It scores how illicit a *Bitcoin transaction* looks.
It is not the exchange fraud-linkage score - Elliptic labels transactions, not
exchange culpability. The exchange score is the explainable aggregate in
`app/services/risk.py`; conflating the two would misrepresent what the model
knows.

---

## Frontend rule: JSX only

No TypeScript anywhere in this repo — no `.ts`, no `.tsx`, no `tsconfig.json`,
no `@types/*`. Runtime type-checking is done with **PropTypes**, and **ESLint is
the static gate** in place of `tsc` (`react/prop-types` is set to `error`).

Enforced by `scripts/check_no_typescript.sh`, which fails on any TypeScript
source, tsconfig, or TS tooling dependency:

```bash
bash scripts/check_no_typescript.sh
```

---

## Setup

**Prerequisites:** Docker with Compose v2.20+. Python 3.11 and Node 22+ are only
needed if you want to run services outside containers.

```bash
cp .env.example .env      # optional: the defaults work as-is
docker compose up -d      # build and start postgres, neo4j, redis, backend, frontend
```

First start takes a few minutes: images pull, and **Neo4j needs ~85 s** to load
the GDS and APOC plugins before it reports healthy. The backend waits for it.

A `Makefile` wraps the common commands, but `make` is not installed on every
Windows setup — the raw equivalents are:

| Task | Make | Raw |
|---|---|---|
| Start | `make up` | `docker compose up -d --build` |
| Stop | `make down` | `docker compose down` |
| Stop + wipe data | `make nuke` | `docker compose down -v` |
| Status / ports | `make ports` | `docker compose ps` |
| Readiness | `make health` | `curl -s localhost:8001/api/v1/health/ready` |
| Backend tests | `make test` | `docker compose exec backend pytest -q` |
| Frontend lint | `make lint-frontend` | `docker compose exec frontend npm run lint` |

| Service | URL |
|---|---|
| Frontend | http://localhost:5174 |
| Backend API docs | http://localhost:8001/docs |
| Readiness probe | http://localhost:8001/api/v1/health/ready |
| Neo4j Browser | http://localhost:7475 (`neo4j` / `sihdevpass`) |
| PostgreSQL | `localhost:5434` (`sih` / `sihdev` / db `sih183`) |

> **Host ports are deliberately non-default** (5434 / 7475 / 7688 / 6380 / 8001 /
> 5174) so this stack can run side by side with another local project holding the
> conventional ports. Container-internal ports are unchanged; override any of
> them in `.env`.

The Postgres schema in `infra/postgres/init.sql` is applied automatically on
first start. Neo4j constraints from `infra/neo4j/init.cypher` are applied by the
backend at startup, idempotently.

### Blockchain API keys — optional

The stack runs fully without any key: `DEMO_MODE=true` serves the synthetic
fraud-ring dataset. To trace live chain data, put keys in `.env` and set
`DEMO_MODE=false`. Blockchair works keyless at low rates; Etherscan and TronGrid
need a free key each.

### Troubleshooting

**`Bind for 0.0.0.0:<port> failed: port is already allocated`** — another stack
holds that port. Override the offending entry in `.env` (see *Host ports* above)
rather than stopping the other stack.

**Neo4j reported unhealthy on first `up`** — its `start_period` is 150 s to cover
plugin loading. If the host is slow, re-run `docker compose up -d`; the container
keeps starting in the background and the second invocation picks it up healthy.

**`docker compose logs neo4j` shows `chown: ... Read-only file system`** — the
Neo4j entrypoint chowns its import directory, so it cannot take a read-only bind
mount there. `infra/neo4j/init.cypher` is therefore mounted into the *backend*
container, which applies the constraints itself at startup.

---

## Running the demo

The full end-to-end demo script is a Phase 5 deliverable. What works today:

```bash
# 1. (Re)generate the synthetic fraud ring - deterministic, seed 26183
docker compose exec backend python scripts/generate_synthetic_complaints.py   --out /app/ml_seeds/synthetic_dataset.json

# 2. Validate an address without opening a case
curl -s -X POST http://localhost:8001/api/v1/wallets/validate   -H 'Content-Type: application/json'   -d '{"address":"1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"}'

# 3. Report a suspect wallet - traces, builds the graph, clusters
curl -s -X POST http://localhost:8001/api/v1/wallets   -H 'Content-Type: application/json'   -d '{"address":"<address from ml/seeds/synthetic_dataset.json>","source":"synthetic"}'

# 4. Wallets reported by more than one victim
curl -s http://localhost:8001/api/v1/wallets/multi-reported
```

### One-command demo load

```bash
docker compose exec backend python ml/src/download_data.py all   # once: public datasets
docker compose exec backend python scripts/load_demo.py          # seed tags + file complaints
```

`load_demo.py` seeds the tag database and files all 13 synthetic complaints,
then prints the exchange ranking. Expected output:

```
  HIGH    81.11   10 cases  TRON  Meridian Exchange
  MEDIUM  56.54    5 cases  ETH   Northwind Digital
  MEDIUM  48.66    4 cases  BTC   Kestrel Trade
```

Then analyse a single wallet end to end:

```bash
curl -s "http://localhost:8001/api/v1/wallet?address=<synthetic address>"
curl -s "http://localhost:8001/api/v1/exchanges/ranked"
```

All three fictional exchanges are reached by tracing: Meridian at hop 7 (TRON),
Northwind at hop 7 (ETH, via a mixer, flagged), Kestrel at hop 5 (BTC).

### Dashboard demo path

1. Open **http://localhost:5174**.
2. Paste a synthetic address from `ml/seeds/synthetic_dataset.json` into the
   form — chain and checksum validate as you type.
3. **Analyse only** draws the trace for an address already in the graph;
   **File complaint & trace** opens a new case, builds the graph, then analyses.
   Filing the same address twice surfaces the repeat-report fraud-ring banner.
4. Or jump straight in with a deep link:
   `http://localhost:5174/?address=<synthetic address>`

The TRON address reaches *Meridian Exchange* at hop 7 with a **HIGH** score and
ten contributing cases listed underneath.

The generator writes `ml/seeds/synthetic_dataset.json`: 43 transactions across
BTC/ETH/TRON, 13 complaints, 4 entities (3 fictional exchanges + 1 mixer). Every
generated address is genuinely checksum-valid, so the demo data passes the same
validator real input does.

> On Git Bash for Windows, prefix `docker compose exec` with `MSYS_NO_PATHCONV=1`
> when an argument is an absolute container path, or Git Bash rewrites `/app/...`
> into a Windows path.

---

## Tests

```bash
docker compose exec backend pytest -q     # backend tests
docker compose exec backend ruff check app/
docker compose exec frontend npm run lint # ESLint - the static gate in place of tsc
docker compose exec frontend npm run build # production build must succeed
bash scripts/check_no_typescript.sh       # hard JSX-only gate
```

Latest result (end of Phase 5): **158 passed** (pytest), **ruff clean**,
**ESLint 0 errors**, **production build OK**, **PASS** (JSX-only gate — 11
`.jsx` + 1 `.js`, zero TypeScript).

The frontend has no unit-test runner. Its gates are ESLint (which carries the
weight `tsc` would in a TypeScript project, with `prop-types` validation on), a
clean production build, and a **real headless-browser render** — the dashboard
was loaded in Edge and screenshotted to confirm it actually draws, rather than
merely compiling. Adding Vitest is a reasonable follow-up.

| Suite | Tests | Covers |
|---|---|---|
| `test_chain_detect.py` | 33 | BIP-173/BIP-350/EIP-55 reference vectors, checksum rejection, normalisation |
| `test_connectors.py` | 19 | Factory + provenance fallback, synthetic dataset integrity, ETH normalisation regression |
| `test_analysis.py` | 14 | `/api/wallet` contract, Sankey shape, explainability, ranking |
| `test_clustering.py` | 13 | Both UTXO heuristics against a real Neo4j + GDS |
| `test_risk.py` | 14 | Time decay, saturation, thresholds, aggravating factors |
| `test_attribution.py` | 10 | Tag lookup, cluster propagation, "never name a guess" |
| `test_intake.py` | 11 | Intake API, dedupe, cross-case view |
| `test_phase4.py` | 33 | Auth, notes, evidence + report hashing, NCRP mock, STR, **freeze approval gate**, alerts, WebSocket |
| `test_model_artifact.py` | 6 | Trained model loads + infers in the shipped runtime |
| `test_smoke.py` | 4 | App boots, liveness contract |

Graph-backed tests run against the live compose stack and skip cleanly when it
is down.

> **Hot reload.** Both services reload on save (verified: ~2s each). If the
> backend ever stops picking up edits, check its logs for
> `WatchfilesRustInternalError: File system loop found` — a symlink pointing at
> its own ancestor inside a mounted directory crashes the file watcher outright
> and silently kills reload. Remove the symlink; do not reach for a restart loop.

> **Running the tests clears graph transaction data.** The fixtures scope their
> cleanup so the seeded tag database survives, but the traced money flow does
> not. Reload the demo afterwards with
> `docker compose exec backend python scripts/load_demo.py --skip-tags`.

---

## Assumptions

Recorded here rather than blocking on questions, per the working agreement.

1. **Redis Streams instead of Kafka.** One less heavy container for a demo
   stack. The publish path is isolated behind `app/services/alerts.py`, so
   moving to Kafka is a driver swap rather than a rewrite.
2. **Simplified JWT with role claims instead of Keycloak.** Roles:
   `investigator`, `supervisor` (the only role that can approve a freeze
   request), `admin`. Keycloak would add a container and realm configuration
   without changing what the prototype demonstrates.
3. **`DEMO_MODE` defaults to true**, including when configuration is missing.
   Fail-safe: absent explicit configuration the system assumes synthetic data
   rather than claiming live-chain provenance.
4. **Neo4j 5.26 Community with the GDS plugin.** GDS on Community lacks the
   enterprise projection features, but Weakly Connected Components and label
   propagation — what the clustering heuristics need — work at demo scale.
5. **The Elliptic dataset is not committed** (~200 MB, redistribution
   restrictions). `ml/src/download_data.py` fetches it and
   `ml/artifacts/metrics.json` holds the reported held-out numbers so the
   metric is reproducible.
6. **Default trace depth 8** (`TRACE_MAX_DEPTH`), raised from 6 during Phase 2.
   Peel chains in this problem domain routinely run past six hops - the demo
   ring's exchange sits at hop 7, and at depth 6 the trace missed it entirely
   and fell back to a behavioural guess. The brief's range is 6-8; 8 is the end
   of it that actually reaches the destination.
7. **Mixers are flagged, not unwound.** Interaction with a known mixer is
   recorded as a risk signal; this system does not attempt to defeat mixing.

---

## Repository layout

```
backend/    FastAPI service - API, services, connectors, graph/DB access, tests
frontend/   Vite + React (JSX only) investigator dashboard
ml/         Dataset fetch, feature engineering, model training, tag seed data
infra/      Postgres DDL, Neo4j constraints, helper scripts
docs/       Architecture, schema, API contracts, demo script
scripts/    Synthetic complaint generator, e2e demo, JSX-only guard
```
