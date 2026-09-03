#!/usr/bin/env python3
"""Synthetic victim-complaint and fraud-ring generator.

THIS PRODUCES ENTIRELY FICTIONAL DATA. Every address, transaction, exchange
name and complaint below is generated. Nothing here is derived from real NCRP
complaints, real exchange KYC records, or real on-chain activity. The exchange
names are invented and are not meant to resemble any real VASP.

The generator is a permanent part of the codebase, not a throwaway fixture: it
is what `DEMO_MODE=true` serves through the synthetic connector, and what the
end-to-end demo runs against.

Shape of the simulated ring
---------------------------
Three chains, each with a different structure, chosen so every downstream
component has something real to chew on:

  TRON  - the dominant rail in reported Indian crypto fraud. Victims deposit to
          scam collection addresses, which fan into a mule layer, then run a
          peel chain into one exchange hot wallet.
  ETH   - a smaller ring; one branch routes through a mixer so the mixer-flag
          path is exercised (flagged as a risk signal, never unwound).
  BTC   - a UTXO ring built so the clustering heuristics have real material:
          consolidation transactions where several mule addresses co-spend into
          one transaction (common-input-ownership), and 2-output payments that
          leave a fresh change address (change-address heuristic).

Addresses are generated as genuinely checksum-valid strings - real Base58Check
and EIP-55 - because the intake API validates checksums, and demo data that its
own validator rejects would be worse than useless.

Usage:
    python scripts/generate_synthetic_complaints.py [--out PATH] [--seed N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import base58
from Crypto.Hash import keccak

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "ml" / "seeds" / "synthetic_dataset.json"

# Real contract addresses for the tokens the ring moves. The transactions are
# fictional; the token identities are genuine, so the pipeline exercises real
# contract handling rather than a placeholder string. USDT-TRC20 is the
# dominant rail in reported Indian crypto fraud.
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_ERC20_CONTRACT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
DEFAULT_SEED = 26183

NOTICE = (
    "SYNTHETIC DATA - generated for demonstration. Contains no real NCRP complaint "
    "data, no real exchange KYC data, and no real on-chain activity. Exchange and "
    "service names are fictional."
)


# ---------------------------------------------------------------------------
# Address generation (checksum-valid by construction)
# ---------------------------------------------------------------------------
def _hash160(rng: random.Random) -> bytes:
    return bytes(rng.getrandbits(8) for _ in range(20))


def gen_btc_address(rng: random.Random, p2sh: bool = False) -> str:
    version = b"\x05" if p2sh else b"\x00"
    return base58.b58encode_check(version + _hash160(rng)).decode()


def gen_tron_address(rng: random.Random) -> str:
    return base58.b58encode_check(b"\x41" + _hash160(rng)).decode()


def gen_eth_address(rng: random.Random) -> str:
    body = _hash160(rng).hex()
    digest = keccak.new(digest_bits=256, data=body.encode()).hexdigest()
    return "0x" + "".join(
        c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(body)
    )


def gen_address(rng: random.Random, chain: str) -> str:
    if chain == "BTC":
        return gen_btc_address(rng)
    if chain == "TRON":
        return gen_tron_address(rng)
    return gen_eth_address(rng)


def make_txid(chain: str, seq: int, seed: int) -> str:
    raw = f"{chain}:{seed}:{seq}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return digest if chain == "BTC" else f"0x{digest}"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class RingBuilder:
    """Accumulates transactions and complaints for one simulated fraud ring."""

    def __init__(self, seed: int, now: datetime):
        self.rng = random.Random(seed)
        # Separate stream for timing jitter. Drawing it from `self.rng` would
        # consume draws from the same sequence that generates addresses, so any
        # change to the timing logic would silently change every address in the
        # dataset - and every hardcoded address in a demo script or doc.
        self.time_rng = random.Random(seed + 1)
        self.seed = seed
        self.now = now
        self.transactions: list[dict] = []
        self.complaints: list[dict] = []
        self.entities: list[dict] = []
        self._seq = 0
        # Last time each address was seen receiving funds. Used to force every
        # transaction to occur after the funds it spends actually arrived -
        # otherwise a randomly-dated consolidation can precede the hop it feeds,
        # producing a route that runs backwards in time. Real money cannot do
        # that, and the exposure engine's continuity check correctly flags it.
        self._addr_time: dict[str, datetime] = {}
        # Running balance per address, so value flows THROUGH the ring instead
        # of each hop inventing an amount. Without this the on-chain figures
        # contradict the complaints - victims report lakhs while the mule layer
        # moves a few units - which absolute fiat valuation exposes immediately.
        self._balance: dict[str, float] = {}

    # -- helpers ---------------------------------------------------------
    def _next_txid(self, chain: str) -> str:
        self._seq += 1
        return make_txid(chain, self._seq, self.seed)

    def add_tx(
        self,
        chain: str,
        inputs: list[tuple[str, float]],
        outputs: list[tuple[str, float, bool]],
        ts: datetime,
        asset: str,
        token_contract: str | None = None,
        token_decimals: int = 6,
    ) -> str:
        txid = self._next_txid(chain)

        # Clamp: a spend cannot precede the arrival of what it spends.
        arrivals = [self._addr_time[a] for a, _ in inputs if a in self._addr_time]
        if arrivals:
            earliest = max(arrivals) + timedelta(minutes=self.time_rng.randint(3, 240))
            if ts < earliest:
                ts = earliest
        for addr, value, _ in outputs:
            self._balance[addr] = self._balance.get(addr, 0.0) + value
        for addr, value in inputs:
            self._balance[addr] = max(self._balance.get(addr, 0.0) - value, 0.0)

        for addr, _, _ in outputs:
            # Keep the LATEST arrival. Overwriting with an earlier one would let
            # a later spend be dated before funds actually arrived, which is the
            # very inconsistency this clamp exists to prevent.
            prior = self._addr_time.get(addr)
            self._addr_time[addr] = ts if prior is None or ts > prior else prior

        self.transactions.append(
            {
                "chain": chain,
                "txid": txid,
                "timestamp": ts.isoformat(),
                "block_height": 800_000 + self._seq,
                "fee": round(self.rng.uniform(0.1, 2.0), 4),
                "asset": asset,
                # None => the chain's native currency.
                "token_contract": token_contract,
                "token_decimals": token_decimals,
                "status": "success",
                "inputs": [
                    {"address": a, "value": round(v, 6), "index": i}
                    for i, (a, v) in enumerate(inputs)
                ],
                "outputs": [
                    {"address": a, "value": round(v, 6), "index": i, "is_change": chg}
                    for i, (a, v, chg) in enumerate(outputs)
                ],
            }
        )
        return txid

    def add_complaint(
        self, victim_ref: str, address: str, chain: str, amount_inr: float, days_ago: int
    ) -> None:
        self.complaints.append(
            {
                "victim_ref": victim_ref,
                "address": address,
                "chain": chain,
                "amount_inr": round(amount_inr, 2),
                "reported_at": (self.now - timedelta(days=days_ago)).isoformat(),
                "narrative": self.rng.choice(
                    [
                        "Victim responded to a task-based earning scheme promoted on a "
                        "messaging app and was instructed to deposit to the address shown.",
                        "Investment platform promised guaranteed daily returns; withdrawals were "
                        "blocked after the deposit.",
                        "Caller impersonated a courier company and directed payment to settle a "
                        "fabricated customs charge.",
                        "Fake trading dashboard displayed rising balances; support stopped "
                        "responding once further deposits were declined.",
                    ]
                ),
                "source": "synthetic",
            }
        )

    def held(self, address: str) -> float:
        """What this address currently holds, per the ring built so far."""
        return self._balance.get(address, 0.0)

    # -- ring construction ------------------------------------------------
    def build_account_ring(
        self,
        chain: str,
        asset: str,
        token_contract: str | None,
        exchange_name: str,
        exchange_jurisdiction: str,
        victim_count: int,
        mule_layers: int,
        peel_hops: int,
        route_via_mixer: bool,
        mixer_name: str | None,
    ) -> dict:
        """Fan-in then peel chain, for an account-model chain (ETH / TRON)."""
        rng = self.rng
        hot_wallet = gen_address(rng, chain)
        entity = {
            "name": exchange_name,
            "entity_type": "exchange",
            "jurisdiction": exchange_jurisdiction,
            "website": f"https://{exchange_name.lower().replace(' ', '')}.example",
            "addresses": [
                {"chain": chain, "address": hot_wallet, "label": f"{exchange_name} hot wallet 1"}
            ],
        }

        mixer_address = None
        if route_via_mixer and mixer_name:
            mixer_address = gen_address(rng, chain)
            self.entities.append(
                {
                    "name": mixer_name,
                    "entity_type": "mixer",
                    "jurisdiction": "unknown",
                    "website": None,
                    "addresses": [
                        {"chain": chain, "address": mixer_address, "label": f"{mixer_name} pool"}
                    ],
                }
            )

        collectors: list[str] = []
        for v in range(victim_count):
            days_ago = rng.randint(1, 150)
            ts = self.now - timedelta(days=days_ago, hours=rng.randint(0, 20))
            collector = gen_address(rng, chain)
            collectors.append(collector)

            victim_wallet = gen_address(rng, chain)
            amount_inr = rng.uniform(45_000, 900_000)
            amount_coin = amount_inr / (250_000 if chain == "ETH" else 85)

            self.add_tx(chain, [(victim_wallet, amount_coin)], [(collector, amount_coin, False)],
                        ts, asset, token_contract)
            self.add_complaint(
                f"SYN-{chain}-V{v + 1:03d}", collector, chain, amount_inr, days_ago
            )

        # Fan collectors into a shrinking mule layer.
        layer = collectors
        for depth in range(mule_layers):
            mules = [gen_address(rng, chain) for _ in range(max(1, len(layer) // 2))]
            for i, src in enumerate(layer):
                dst = mules[i % len(mules)]
                ts = self.now - timedelta(days=rng.randint(1, 140), hours=depth)
                value = self.held(src)
                if value <= 0:
                    continue
                # A mule keeps a small cut and forwards the rest.
                forwarded = value * rng.uniform(0.96, 0.99)
                self.add_tx(chain, [(src, value)], [(dst, forwarded, False)], ts, asset,
                            token_contract)
            layer = mules

        # Peel chain: repeatedly shave a small amount off and forward the rest.
        current = layer[0]
        for hop in range(peel_hops):
            nxt = gen_address(rng, chain)
            side = gen_address(rng, chain)
            ts = self.now - timedelta(days=max(1, 20 - hop), hours=hop)
            total = self.held(current)
            if total <= 0:
                break
            peel = total * rng.uniform(0.04, 0.11)
            self.add_tx(
                chain, [(current, total)], [(side, peel, False), (nxt, total - peel, False)],
                ts, asset, token_contract
            )
            current = nxt

        if mixer_address:
            ts = self.now - timedelta(days=3)
            amt = self.held(current)
            self.add_tx(chain, [(current, amt)], [(mixer_address, amt, False)], ts, asset,
                        token_contract)
            post_mix = gen_address(rng, chain)
            self.add_tx(
                chain,
                [(mixer_address, amt * 0.97)],
                [(post_mix, amt * 0.97, False)],
                ts + timedelta(hours=6),
                asset,
                token_contract,
            )
            current = post_mix

        # Terminal deposit into the exchange hot wallet.
        ts = self.now - timedelta(days=1)
        final = self.held(current)
        self.add_tx(chain, [(current, final)], [(hot_wallet, final, False)], ts, asset,
                    token_contract)

        self.entities.append(entity)
        return {"hot_wallet": hot_wallet, "mixer": mixer_address}

    def build_utxo_ring(
        self,
        exchange_name: str,
        exchange_jurisdiction: str,
        victim_count: int,
        consolidation_groups: int,
    ) -> dict:
        """BTC ring shaped so both UTXO heuristics have something to find.

        Consolidation transactions put several mule addresses on the input side
        of one transaction, which is exactly the common-input-ownership
        premise. Forwarding payments have two outputs, one of them a
        never-before-seen change address.
        """
        rng = self.rng
        chain, asset = "BTC", "BTC"
        hot_wallet = gen_btc_address(rng)
        deposit_2 = gen_btc_address(rng, p2sh=True)

        collectors: list[str] = []
        for v in range(victim_count):
            days_ago = rng.randint(2, 160)
            ts = self.now - timedelta(days=days_ago)
            collector = gen_btc_address(rng)
            collectors.append(collector)
            victim_wallet = gen_btc_address(rng)
            amount_inr = rng.uniform(60_000, 1_200_000)
            amount_btc = amount_inr / 5_500_000

            self.add_tx(chain, [(victim_wallet, amount_btc)],
                        [(collector, amount_btc, False)], ts, asset)
            self.add_complaint(f"SYN-BTC-V{v + 1:03d}", collector, chain, amount_inr, days_ago)

        # Consolidation: co-spend groups of collectors into one output each.
        consolidated: list[str] = []
        group_size = max(2, len(collectors) // consolidation_groups)
        for g in range(consolidation_groups):
            group = collectors[g * group_size : (g + 1) * group_size]
            if len(group) < 2:
                continue
            ts = self.now - timedelta(days=rng.randint(1, 60))
            values = [rng.uniform(0.01, 0.2) for _ in group]
            dest = gen_btc_address(rng)
            consolidated.append(dest)
            self.add_tx(
                chain,
                list(zip(group, values, strict=True)),
                [(dest, sum(values) * 0.997, False)],
                ts,
                asset,
            )

        # Forwarding hops that leave a fresh change address behind.
        current = consolidated[0] if consolidated else gen_btc_address(rng)
        for hop in range(3):
            ts = self.now - timedelta(days=max(1, 15 - hop * 4))
            total = self.held(current)
            if total <= 0:
                break
            # A peel chain shaves a SMALL amount off to a payee and carries the
            # bulk forward as change. Sending the majority to the dead-end payee
            # (the earlier behaviour) left under 1% of the victims' money
            # reaching the exchange, which is not how laundering looks.
            spend = total * rng.uniform(0.10, 0.25)
            payee = gen_btc_address(rng)
            change = gen_btc_address(rng)  # fresh - never seen before this tx
            self.add_tx(
                chain,
                [(current, total)],
                [(payee, spend, False), (change, total - spend, True)],
                ts,
                asset,
            )
            current = change

        # Terminal deposits into two exchange addresses.
        for dest in (hot_wallet, deposit_2):
            ts = self.now - timedelta(days=rng.randint(1, 4))
            amt = self.held(current) / 2.0
            if amt <= 0:
                break
            self.add_tx(chain, [(current, amt)], [(dest, amt, False)], ts, asset)

        self.entities.append(
            {
                "name": exchange_name,
                "entity_type": "exchange",
                "jurisdiction": exchange_jurisdiction,
                "website": f"https://{exchange_name.lower().replace(' ', '')}.example",
                "addresses": [
                    {"chain": chain, "address": hot_wallet,
                     "label": f"{exchange_name} hot wallet 1"},
                    {"chain": chain, "address": deposit_2,
                     "label": f"{exchange_name} deposit pool"},
                ],
            }
        )
        return {"hot_wallet": hot_wallet, "deposit_2": deposit_2}


def build_dataset(seed: int = DEFAULT_SEED, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    b = RingBuilder(seed, now)

    tron = b.build_account_ring(
        chain="TRON",
        asset="USDT",
        token_contract=USDT_TRC20_CONTRACT,
        exchange_name="Meridian Exchange",
        exchange_jurisdiction="Seychelles",
        victim_count=6,
        mule_layers=2,
        peel_hops=4,
        route_via_mixer=False,
        mixer_name=None,
    )
    eth = b.build_account_ring(
        chain="ETH",
        asset="ETH",
        token_contract=None,   # native ether
        exchange_name="Northwind Digital",
        exchange_jurisdiction="Estonia",
        victim_count=3,
        mule_layers=1,
        peel_hops=3,
        route_via_mixer=True,
        mixer_name="Cyclone Pool",
    )
    btc = b.build_utxo_ring(
        exchange_name="Kestrel Trade",
        exchange_jurisdiction="Singapore",
        victim_count=4,
        consolidation_groups=2,
    )

    return {
        "meta": {
            "notice": NOTICE,
            "seed": seed,
            "generated_at": now.isoformat(),
            "generator": "scripts/generate_synthetic_complaints.py",
            "is_synthetic": True,
        },
        "entities": b.entities,
        "transactions": b.transactions,
        "complaints": b.complaints,
        "terminals": {"TRON": tron, "ETH": eth, "BTC": btc},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args()

    data = build_dataset(seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2), encoding="utf-8")

    chains = {t["chain"] for t in data["transactions"]}
    print(f"wrote {args.out}")
    print(f"  {len(data['transactions'])} transactions across {sorted(chains)}")
    print(f"  {len(data['complaints'])} synthetic complaints")
    print(f"  {len(data['entities'])} entities")
    print(f"  seed={args.seed} (deterministic)")


if __name__ == "__main__":
    main()
