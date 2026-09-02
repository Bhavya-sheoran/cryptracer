# Schema — SIH26183

Phase 0 deliverable. Two stores, deliberately split by what they are good at.

- **PostgreSQL** — case management, identity, tags, scores, work product, audit.
  Anything that needs transactions, constraints, and a paper trail.
- **Neo4j** — the money graph. Addresses, transactions, clusters, and the
  multi-hop paths between them. Anything that needs traversal.

The two are joined by stable string keys, not foreign keys across engines:
`clusters.cluster_key` in Postgres == `Cluster.cluster_key` in Neo4j, and
`entities.id` == `Entity.entity_id`.

> **Data provenance.** Every record described here is synthetic (from
> `scripts/generate_synthetic_complaints.py`) or derived from public datasets
> (Elliptic, Etherscan Label Cloud, GraphSense TagPacks, OFAC SDN). No real
> NCRP complaint data and no real exchange KYC data is used anywhere.

---

## 1. PostgreSQL

Authoritative DDL: [infra/postgres/init.sql](../infra/postgres/init.sql). It is
applied automatically the first time the `postgres` container starts on an empty
volume.

### 1.1 Enumerated types

| Type | Values | Notes |
|---|---|---|
| `chain_t` | `BTC`, `ETH`, `TRON` | The three rails in scope. TRON covers USDT-TRC20. |
| `user_role_t` | `investigator`, `supervisor`, `admin` | Only `supervisor` may approve a freeze request. |
| `case_status_t` | `open`, `tracing`, `analysed`, `escalated`, `closed` | |
| `wallet_role_t` | `reported_suspect`, `intermediate`, `terminal` | Position of an address within a case. |
| `entity_type_t` | `exchange`, `mixer`, `gambling`, `darknet`, `sanctioned`, `payment_processor`, `unknown` | |
| `risk_label_t` | `low`, `medium`, `high` | |
| `trace_status_t` | `queued`, `running`, `complete`, `failed` | |
| `attribution_m_t` | `tagged_db`, `classifier`, `manual`, `none` | Always surfaced in the UI so an investigator knows whether an attribution came from a curated tag or a model guess. |
| `approval_t` | `draft`, `pending_approval`, `approved`, `rejected`, `dispatched` | The human-in-the-loop lifecycle. |

### 1.2 Tables

**Identity & audit**

| Table | Purpose |
|---|---|
| `users` | Investigators, supervisors, admins. Password hash only. |
| `audit_log` | Append-only. Every state-changing action, especially freeze approvals, lands here with actor, entity, JSON payload, and IP. |

**Wallets & cases**

| Table | Purpose |
|---|---|
| `wallets` | One row per address, global across cases. `address_norm` is lowercased for ETH and verbatim for BTC/TRON (Base58 is case-significant). `UNIQUE (chain, address_norm)` is what makes dedupe cheap. |
| `cases` | One victim complaint. `victim_ref` is a pseudonymous handle — no PII columns exist by design. `source` distinguishes `manual` / `ncrp_mock` / `synthetic`. |
| `case_wallets` | Many-to-many, carrying `role`. **This is the fraud-ring signal**: several distinct `case_id`s pointing at one `wallet_id` means several victims reported the same address. |

**Intelligence**

| Table | Purpose |
|---|---|
| `entities` | A VASP, mixer, or other named service. |
| `tagged_addresses` | Curated address→entity tags. `source` is **mandatory**, so every attribution the UI shows can cite its origin. Unique per `(chain, address_norm, source)` so two sources tagging the same address coexist and can disagree. |
| `clusters` | Postgres-side mirror of a Neo4j cluster, plus its attribution result and confidence. |

**Tracing & scoring**

| Table | Purpose |
|---|---|
| `trace_runs` | One traversal execution. Records `max_depth`, `data_source` (`synthetic` vs a named live indexer), and `mixer_interaction` — mixers are *flagged as a risk signal*, never de-mixed. |
| `risk_scores` | A fraud-linkage score for an entity or cluster: 0–100, a label, the `half_life_days` used, and `model_version`. |
| `risk_contributions` | **The explainability table.** One row per case that pushed the score up, with `age_days`, `decay_weight` = `0.5 ^ (age_days / half_life_days)`, and the resulting `points`. The dashboard renders these under the number, so the score is reconstructable by hand. |

**Work product & human-in-the-loop**

| Table | Purpose |
|---|---|
| `case_notes`, `evidence`, `reports` | Investigator notes, uploaded files, generated PDFs. `evidence.sha256` and `reports.sha256` are the chain-of-custody digests. |
| `freeze_requests` | Starts at `draft`. A `CHECK` constraint makes `approved` impossible without both `approved_by` and `approved_at`. The service layer additionally requires that actor to hold the `supervisor` role. |
| `str_drafts` | FIU-IND-style STR drafts, same approval lifecycle. |
| `alerts` | Durable mirror of what went out on the Redis Stream, so the dashboard has history after a reload. |

**View**

`v_multi_reported_wallets` — wallets appearing in more than one case, with
`case_count` and first/last report timestamps. Backs the cross-case search
screen in Phase 3.

---

## 2. Neo4j

Authoritative constraints: [infra/neo4j/init.cypher](../infra/neo4j/init.cypher),
applied idempotently by the backend on startup.

### 2.1 Nodes

| Label | Key | Properties |
|---|---|---|
| `:Address` | `(chain, address_norm)` | `address`, `chain`, `address_norm`, `first_seen`, `last_seen`, `tx_count`, `entity_type` (denormalised tag, nullable), `is_mixer` |
| `:Transaction` | `(chain, txid)` | `txid`, `chain`, `timestamp`, `block_height`, `total_value`, `fee`, `input_count`, `output_count` |
| `:Cluster` | `cluster_key` | `cluster_key`, `chain`, `heuristic`, `size` |
| `:Entity` | `entity_id` | `entity_id` (== Postgres `entities.id`), `name`, `entity_type` |
| `:Case` | `case_id` | `case_id`, `case_number`, `reported_at` |

### 2.2 Relationships

| Pattern | Properties | Why it exists |
|---|---|---|
| `(:Address)-[:SENT]->(:Transaction)` | `value`, `vin_index` | Full UTXO shape. **Required** for common-input-ownership: two addresses co-spending into one transaction are presumed same-owner. |
| `(:Transaction)-[:RECEIVED_BY]->(:Address)` | `value`, `vout_index`, `is_change` | `is_change` is set by the change-address heuristic. |
| `(:Address)-[:TRANSFERRED]->(:Address)` | `txid`, `value`, `timestamp`, `asset`, `hop` | Denormalised edge collapsing the two above. Multi-hop tracing runs on this — a variable-length match over `TRANSFERRED` is roughly 2× cheaper than bouncing through `:Transaction` nodes. For account-model chains (ETH/TRON) this is the *only* edge written; there is no UTXO structure to preserve. |
| `(:Address)-[:MEMBER_OF]->(:Cluster)` | `heuristic`, `assigned_at` | Output of the clustering jobs. |
| `(:Cluster)-[:ATTRIBUTED_TO]->(:Entity)` | `method`, `confidence` | Output of VASP attribution. |
| `(:Address)-[:TAGGED_AS]->(:Entity)` | `source`, `confidence` | Direct tag from the curated DB, independent of clustering. |
| `(:Case)-[:REPORTED]->(:Address)` | `reported_at` | Lets a single Cypher query answer "which other cases touch this subgraph". |

### 2.3 Why both `:Transaction` nodes and a `:TRANSFERRED` edge

Redundant on purpose. The UTXO heuristics need the transaction as a first-class
node with its full input and output sets; the tracing endpoint needs a cheap
variable-length traversal and would otherwise pay for a hop through
`:Transaction` at every step. Write both at ingest, keep them consistent via
`txid`, and let each query use the shape it needs.

### 2.4 Clustering heuristics (implemented in Phase 1)

1. **Common-input-ownership** (UTXO only) — all `:Address` nodes with a `:SENT`
   edge into the same `:Transaction` are merged into one `:Cluster`. Implemented
   as a GDS Weakly Connected Components run over a co-spend projection.
2. **Change-address** (UTXO only) — a transaction output is marked
   `is_change = true` when it is the sole output whose address has never been
   seen before, its script type matches an input's, and the output count is 2.
   Conservative by design: a false merge corrupts attribution downstream.
3. **`account_single`** (ETH/TRON) — account-model chains have no co-spend
   structure, so each address is its own cluster unless a curated tag groups it.

Mixer interaction is **flagged, not unwound**: touching a known mixer sets
`Address.is_mixer` on the counterparty and `trace_runs.mixer_interaction`, which
feeds the risk score as a signal. This system does not attempt to defeat mixers.
