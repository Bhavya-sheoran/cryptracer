# Demo script — SIH26183

The click-path for a live demonstration, with what to say and what the judge
should be looking at. Roughly **7 minutes** at a comfortable pace.

Everything shown is synthetic or public-dataset derived. Say so once at the
start; the UI repeats it in the banner, in every API response and in the
exported PDF, so you never have to defend the claim twice.

---

## Before you start (5 minutes, off-camera)

```bash
docker compose up -d                                   # ~60s to healthy
docker compose exec backend python ml/src/download_data.py all   # once, ~65 min
docker compose exec backend python ml/src/seed_tags.py           # 3,221 public tags
docker compose exec backend python scripts/load_demo.py          # files 13 complaints
```

Then confirm the whole pipeline is green **before** anyone is watching:

```bash
docker compose exec backend python scripts/e2e_demo.py     # expect 42/42
```

Open **http://localhost:5174** and sign in as **investigator** so the first
click of the demo is not a login form. Keep a second browser profile signed in
as **supervisor** — you will need both for the approval step.

Grab a TRON address to use throughout:

```bash
python -c "import json;d=json.load(open('ml/seeds/synthetic_dataset.json'));print([c['address'] for c in d['complaints'] if c['chain']=='TRON'][0])"
```

---

## 1 · The problem (30s, no screen)

> A victim reports a wallet address to NCRP or the 1930 helpline. Today that
> address sits in a complaint record. The money, meanwhile, has already moved
> through a chain of mule wallets and landed at an exchange — and by the time
> anyone works out *which* exchange, it has been withdrawn.
>
> This system answers one question fast: **where did the money end up, and is
> that destination a repeat offender?**

---

## 2 · Intake and validation (45s)

Paste the TRON address into **Report a suspect wallet**. Pause before submitting.

> Notice the address validated as you typed. That is a real Tron Base58Check
> verification, not a regex — the same checksum the network uses.

Now paste a deliberately corrupted Ethereum address:

```
0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD
```

> Rejected: **"EIP-55 checksum mismatch — address appears mistyped."** One
> character is wrong. Catching that here is the difference between an
> investigation and a week spent tracing an address that never existed.

Restore the TRON address and press **File complaint & trace**.

---

## 3 · The money flow (90s) — *the centrepiece*

The **Money flow** tab draws the Sankey.

> Blue on the left is the wallet the victim reported. Each column is one hop.
> The green block on the right is a deposit address belonging to an exchange —
> reached at **hop 7**.
>
> The horizontal position is the hop count, not a layout convenience. A
> small amount peeled off at hop 3 is drawn at hop 3, because in a forensic
> tool distance on screen has to mean distance in the chain.

Hover a ribbon (exact amount and transaction ids). Click a node to pin it.

> Those thin ribbons are a peel chain — a few percent shaved off at each hop.
> They are given a minimum width deliberately: strictly proportional ribbons
> would render the most interesting hops as invisible hairlines.

---

## 4 · Attribution (45s)

Open the **Attribution** tab.

> The badge is the first thing to read: **CURATED TAG**, not a guess. It names
> the source, and the tag database is seeded from public sources — Etherscan
> Label Cloud, WalletExplorer, GraphSense TagPacks and the OFAC SDN list.
> 3,221 tags in total.
>
> When nothing matches, the system falls back to a behavioural classifier —
> and that path deliberately **never returns a company name**. It returns a
> category and a confidence. Behaviour cannot identify a company, and inventing
> one would be the most dangerous thing this system could do.

---

## 5 · Risk, and why (60s)

Open the **Risk** tab.

> High, 97 out of 100. But the number alone is worthless — here are the
> complaints behind it.

Scroll to the contributing-cases table.

> Every case that traces to this exchange, with its age, its time-decay weight
> and the points it contributes. Ten points per case, halved every 90 days, on
> a saturating curve so one prolific reporter cannot dominate the ranking.
>
> An investigator can recompute this score by hand from this table. That
> matters: a "High" rating an officer cannot justify in a case file is not
> actionable.

---

## 6 · Human in the loop (75s) — *the point judges remember*

Open the **Case file** tab → **Exchange freeze request**. Write a justification,
press **Draft freeze request**, then **Submit for approval**.

> I am signed in as an investigator. Watch what the approval control says.

Point at it: *"Awaiting a supervisor. Your role (investigator) cannot authorise
a freeze."*

Switch to the supervisor window, open the same case, press **Review & approve…**

> A second, explicit confirmation — and it states that the approval is recorded
> against this officer's name.

Approve it.

> Every guard here is enforced server-side, not by the interface. Creating a
> request always yields a draft. A draft cannot be approved. An investigator is
> refused. **An officer cannot approve their own request**, even a supervisor.
> The database itself rejects an approved row with no approver.
>
> Nothing in intake, scoring or alerting can reach the approval path. There is
> a test that runs the entire pipeline and asserts no approval appears.

---

## 7 · Evidence and export (45s)

Still in the case file: attach a file, then **Generate hashed PDF report**, then
**Verify hash**.

> Chain of custody. The SHA-256 is taken from the bytes actually written, and
> it verifies outside this system entirely.

Download it and, in a terminal:

```bash
sha256sum ~/Downloads/SIH183-*.pdf     # matches the digest on screen
```

> If anyone altered that PDF after export, this would not match.

---

## 8 · Cross-case view (30s)

Open **Exchanges** in the left rail.

> This is the question the problem statement actually asks. Not "is this
> transaction suspicious" — *which exchanges do reported funds keep arriving
> at?* Ranked across every complaint, time-decayed.

Open **Alerts**.

> Live over a WebSocket. Medium and High only — a feed that fires on everything
> is a feed people learn to ignore.

---

## 9 · Close (30s)

> Built on Python/FastAPI, Neo4j with the Graph Data Science library for
> clustering, PostgreSQL, Redis Streams and a React dashboard. The classifier
> is XGBoost trained on the public Elliptic dataset — **0.88 precision, 0.73
> recall on a temporal split**, which is the honest number, not a random split
> that would have looked better.
>
> No real NCRP data and no exchange KYC data was available, and none is
> claimed. The demonstration ring is synthetic and the code that generates it
> ships with the repo.
>
> And the system recommends. It never acts.

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| Dashboard shows "Backend unreachable" | `docker compose ps` — usually Neo4j still starting. Wait ~40s. |
| Trace returns no graph | Test runs clear graph data. `docker compose exec backend python scripts/load_demo.py --skip-tags` |
| Attribution shows "Unattributed" | Tags not seeded: `docker compose exec backend python ml/src/seed_tags.py` |
| Alerts stuck on "polling" | The socket needs a signed-in officer. Sign in, then reopen Alerts. |
| Backend serving stale code | Check logs for `WatchfilesRustInternalError: File system loop` — a symlink inside a mounted directory kills the watcher. |

**Have a fallback.** Run `scripts/e2e_demo.py` beforehand and keep the terminal
output open in a tab. If the UI misbehaves live, the 42-check pass is a
complete, verifiable demonstration of the same pipeline.
