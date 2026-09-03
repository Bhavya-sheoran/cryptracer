# Architecture — SIH26183

Real-Time Identification of Fraud-Linked Cryptocurrency Exchanges from
Victim-Reported Suspect Wallet Addresses through Automated Blockchain Analytics.

> **Framing.** The SIH portal has not published an expanded background or
> expected-outcome annexure for this problem statement (unlike its sibling
> SIH26184). Everything below is an engineering interpretation of the title,
> not a paraphrase of an official brief. Where a design choice could have gone
> either way, the reasoning is stated so it can be argued with.

---

## 1. The question the system answers

A victim reports a wallet address. Within seconds the system must say:

1. **Where did the money go?** — trace the flow across hops, peel chains and
   mixers.
2. **Whose address is the destination?** — attribute the terminal cluster to an
   exchange or service, and say how confident that is.
3. **Is this destination a repeat offender?** — score how often victim-reported
   funds terminate there, weighted toward recent complaints.
4. **What should a human do about it?** — hand an investigator an explainable
   answer and a workflow, without ever acting autonomously.

The fourth is a hard constraint, not a feature. The system recommends; an
authorised officer acts.

---

## 2. Component map

```
                    ┌──────────────────────────────────────────┐
   victim / NCRP →  │  Intake API        chain detect + dedupe │
                    └────────────────┬─────────────────────────┘
                                     │
                    ┌────────────────▼─────────────────────────┐
                    │  Connector layer                          │
                    │  synthetic │ Etherscan │ TronGrid │       │
                    │            │ Blockchair                   │
                    └────────────────┬─────────────────────────┘
                                     │ normalised ChainTransaction
                    ┌────────────────▼─────────────────────────┐
                    │  Neo4j + GDS                              │
                    │  money graph · clustering heuristics      │
                    └────────────────┬─────────────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
      ┌───────────────┐     ┌────────────────┐    ┌─────────────────┐
      │ Attribution   │     │ Risk scoring   │    │ Alerting        │
      │ tags → class. │     │ time-decayed   │    │ Redis Streams   │
      └───────┬───────┘     └───────┬────────┘    └────────┬────────┘
              └─────────────────────┼──────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │  PostgreSQL — cases, evidence, approvals │
                    │  React dashboard  ← WebSocket ───────────┤
                    └──────────────────────────────────────────┘
```

Ports are deliberately non-default (5434 / 7475 / 7688 / 6380 / 8001 / 5174) so
the stack runs beside another local project.

---

## 3. Design decisions, and why

### Two graph shapes, on purpose

The graph writer stores both:

```
(:Address)-[:SENT]->(:Transaction)-[:RECEIVED_BY]->(:Address)   full UTXO structure
(:Address)-[:TRANSFERRED]->(:Address)                            denormalised
```

Common-input-ownership needs a transaction's *complete input set* as a
first-class object, which the first shape gives. Multi-hop tracing is a
variable-length traversal, which is far cheaper on the second. Keeping both
costs storage and buys correctness plus speed; the writer is `MERGE`-based, so
re-tracing updates rather than duplicates.

### Clustering: assert, then resolve

Both UTXO heuristics emit `:SAME_OWNER` edges, and a single GDS Weakly Connected
Components pass turns those into `:Cluster` nodes. Separating "assert
co-ownership" from "resolve co-ownership" means each heuristic is independently
testable, and a third heuristic needs no change to the resolver.

The change-address heuristic **abstains when ambiguous** — if a 2-output
transaction has two never-before-seen outputs, no merge happens. A false merge
corrupts every attribution downstream, so declining to guess is the correct
failure mode.

Cluster keys derive from the lexicographically smallest member, so a component
that grows is re-keyed and stale memberships are deleted. An address is never in
two clusters.

### Attribution is two-tier, and always says which tier

| Tier | Meaning |
|---|---|
| `tagged_db` | A curated tag from a public source, cited by name |
| `classifier` | No tag matched; a *category* inferred from behaviour |
| `none` | No tag and insufficient behaviour — a legitimate answer |

The behavioural tier **never returns a company name**. Behaviour cannot identify
a company, and a fabricated attribution in a case file is worse than no
attribution. Its confidence is capped below curated tags so a guess can never
outrank a sourced fact.

### Risk scoring is arithmetic, not learned

```
weight_i = 0.5 ^ (age_days_i / half_life_days)      # default half-life 90 days
points_i = 10 · weight_i
raw      = Σ points_i + mixer_bonus(8) + sanctioned_bonus(40)
score    = 100 · (1 − e^(−raw / 60))                # saturating, 0..100
```

Learned would be harder to defend in a case file. This is reconstructable by
hand from the contributing-cases table the dashboard renders beneath it. Time
decay because an exchange that tightened KYC two years ago should not rank like
one receiving proceeds this week; a saturating curve because the difference
between 1 and 5 linked cases matters far more than between 80 and 85.

**The Elliptic classifier is a separate signal, not this score.** Elliptic labels
Bitcoin *transactions*, not exchange culpability. Conflating them would
misrepresent what the model knows.

### The GNN stretch goal, and why it is not shipped

A 2-layer GraphSAGE over the full Elliptic graph scores F1 0.587 / ROC-AUC 0.890
against the XGBoost baseline's 0.801 / 0.930, on the same temporal split. It is
kept as a reproducible experiment and is **not wired into the API**.

That ordering is consistent with the published Elliptic results (Weber et al.
2019: Random Forest beat a GCN on illicit recall, 0.67 vs 0.51). The dataset's
node features already carry aggregated neighbourhood statistics, so message
passing has less to add than it would on a raw graph.

Worth trying if it were pursued: concatenating the GNN embedding with the raw
features into the tree model, or temporal batching that respects the
distribution shift after step 43.

### Mixers are flagged, never unwound

Contact with a tagged mixer raises the score and is recorded as a layering
indicator. The system makes no attempt to defeat mixing, and says so in the UI,
the STR draft and the PDF.

### Redis Streams over Kafka

One less heavy container for a demo, and the publish/consume path sits behind
`alerts.py` so Kafka is a swap rather than a rewrite. A *stream* rather than
pub/sub because it is replayable: an alert raised while nobody had the dashboard
open is still delivered on connect.

### Simplified JWT over Keycloak

Keycloak adds a container and a realm to configure without changing what this
demonstrates. The token shape (subject + role claim) is what a Keycloak-issued
token would carry, so swapping the issuer is a change of verification function,
not of call sites. **This is prototype scope and is flagged as such.**

---

## 4. Human-in-the-loop, concretely

```
draft → pending_approval → approved → dispatched
                        ↘  rejected
```

Five independent guards, each with a test:

1. Creation always yields `draft`; status is not settable by the caller.
2. A draft cannot be approved (`409`).
3. Only supervisor/admin may approve (`403` otherwise).
4. **The requester cannot approve their own request** — even a supervisor.
5. Nothing is dispatched unapproved (`409`).

Plus a database constraint refusing an approved row with no approver, and an
audit-log entry for every transition with the actor and IP.

No code path in intake, scoring or alerting reaches the approval function. It is
reachable only from an authenticated request made by a person, and
`test_no_analysis_call_ever_creates_an_approved_freeze` runs the pipeline and
asserts no approval appeared.

---

## 5. Honesty constraints, enforced in code

These are not comments; they are behaviour:

- `DEMO_MODE` drives the provenance banner from `/health/ready`, so the UI claim
  cannot drift from configuration.
- A live connector that is unconfigured falls back to synthetic **and reports
  `synthetic`** as the source. A test asserts it cannot masquerade as live.
- Every trace records `data_source`; every tag records `source`.
- The synthetic generator produces genuinely checksum-valid addresses so the
  demo data passes the same validator real input does.
- The PDF, the STR draft and every alert carry a synthetic-data notice.
- The complaint schema has **nowhere to put a name or phone number**.

---

## 6. Access control

| Class | Endpoints |
|---|---|
| Public | `/health/*`, `POST /wallets/validate`, `POST /wallets` (victim intake), `POST /ncrp/intake`, `GET /ncrp/contract`, `POST /auth/login` |
| Authenticated | `GET /wallet`, `/exchanges/ranked`, `/alerts/recent`, `WS /ws/alerts`, `/wallets/multi-reported`, all `/cases/*` |
| Approver only | freeze `approve`/`reject`/`dispatch`, STR `approve` |

The authenticated set was closed during the Phase 5 security pass: each of those
responses names case numbers, case ids, or the exchange a case resolved to. An
earlier revision served all of it anonymously — 23 case numbers from a single
unauthenticated request. Victim intake stays open deliberately: a complaint must
be fileable without an account.

`scripts/rbac_audit.py` probes every endpoint anonymous / investigator /
supervisor and fails if anything is reachable more freely than its class allows.

---

## 7. Data

| Source | Use | Real? |
|---|---|---|
| Elliptic Bitcoin Dataset | classifier training + validation | Real, public |
| OFAC SDN crypto addresses | sanctioned-address tags | Real, public |
| Etherscan Label Cloud, WalletExplorer, GraphSense TagPacks | VASP attribution | Real, public |
| Synthetic fraud ring | the demonstration itself | **Fictional, generated** |
| NCRP complaints, exchange KYC | — | **Never used. No access.** |

---

## 8. What this is not

- Not connected to NCRP, the 1930 helpline, FIU-IND or any exchange.
- Not holding real complaint or KYC data.
- Not an official MHA or I4C product.
- Not autonomous: it recommends, and a human acts.
- Not a de-mixing tool.

---

## 9. Verifying it

```bash
docker compose exec backend pytest -q                    # 158 tests
docker compose exec backend python scripts/e2e_demo.py   # 42 end-to-end checks
docker compose exec backend python scripts/rbac_audit.py # access-control probe
docker compose exec frontend npm run lint                # JS static gate
bash scripts/check_no_typescript.sh                      # JSX-only gate
```

See [demo-script.md](demo-script.md) for the live click-path and
[api.md](api.md) for endpoint contracts.
