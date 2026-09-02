"""Chain auto-detection and address validation.

A victim submits a bare string. Before anything else happens we have to know
which chain it belongs to and whether it is actually a well-formed address -
otherwise a typo turns into an empty trace and a wasted investigation.

Validation here is *checksum-level*, not regex-level. Every supported format
carries an integrity check and we verify it:

  * Bitcoin Base58Check  (P2PKH '1...', P2SH '3...')  - 4-byte double-SHA256 tail
  * Bitcoin Bech32/Bech32m ('bc1...')                 - BIP-173 / BIP-350 polymod
  * Ethereum-style 0x hex                             - EIP-55 mixed-case checksum
  * Tron Base58Check ('T...')                         - same as BTC, 0x41 version

Base58 for BTC and TRON is disambiguated by the decoded version byte
(0x00/0x05 vs 0x41), which is exactly what produces the leading '1'/'3' vs 'T',
so the two can never be confused.

Note on Keccak: Ethereum uses original Keccak-256, which is NOT the same as
hashlib.sha3_256 (different padding). We use pycryptodome's keccak.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import base58
from Crypto.Hash import keccak

# --- chain identifiers (match the chain_t enum in Postgres) -----------------
CHAIN_BTC = "BTC"
CHAIN_ETH = "ETH"
CHAIN_TRON = "TRON"

SUPPORTED_CHAINS = (CHAIN_BTC, CHAIN_ETH, CHAIN_TRON)

# Base58Check version bytes
_BTC_P2PKH_VERSION = 0x00
_BTC_P2SH_VERSION = 0x05
_TRON_VERSION = 0x41

_ETH_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BECH32_RE = re.compile(r"^(bc1)[023456789acdefghjklmnpqrstuvwxyz]{6,87}$", re.IGNORECASE)

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


@dataclass
class AddressInfo:
    """Result of inspecting a submitted address string."""

    address: str
    valid: bool
    chain: str | None = None
    address_norm: str | None = None
    address_kind: str | None = None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "valid": self.valid,
            "chain": self.chain,
            "address_norm": self.address_norm,
            "address_kind": self.address_kind,
            "reason": self.reason,
            "warnings": self.warnings,
        }


class InvalidAddressError(ValueError):
    """Raised by validate() when an address fails detection."""


# ---------------------------------------------------------------------------
# Bech32 / Bech32m  (BIP-173, BIP-350)
# ---------------------------------------------------------------------------
def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_decode(addr: str) -> tuple[str | None, list[int] | None, int | None]:
    """Return (hrp, data, checksum_constant) or (None, None, None)."""
    # Mixed case is forbidden outright by BIP-173.
    if addr != addr.lower() and addr != addr.upper():
        return None, None, None
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr) or len(addr) > 90:
        return None, None, None
    hrp = addr[:pos]
    try:
        data = [_BECH32_CHARSET.index(c) for c in addr[pos + 1 :]]
    except ValueError:
        return None, None, None

    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const not in (_BECH32_CONST, _BECH32M_CONST):
        return None, None, None
    return hrp, data[:-6], const


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool) -> list[int] | None:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def _check_segwit(address: str) -> AddressInfo | None:
    """Validate a bech32/bech32m segwit address. Returns None if not one."""
    hrp, data, const = _bech32_decode(address)
    if hrp != "bc" or data is None or len(data) < 1:
        return None

    witness_version = data[0]
    program = _convertbits(data[1:], 5, 8, False)
    if program is None or not (2 <= len(program) <= 40):
        return None
    if witness_version > 16:
        return None

    # BIP-350: v0 must use bech32, v1+ must use bech32m.
    expected_const = _BECH32_CONST if witness_version == 0 else _BECH32M_CONST
    if const != expected_const:
        return None

    if witness_version == 0:
        if len(program) == 20:
            kind = "p2wpkh"
        elif len(program) == 32:
            kind = "p2wsh"
        else:
            return None
    elif witness_version == 1 and len(program) == 32:
        kind = "p2tr"
    else:
        kind = f"witness_v{witness_version}"

    return AddressInfo(
        address=address,
        valid=True,
        chain=CHAIN_BTC,
        address_norm=address.lower(),  # bech32 is case-insensitive; lowercase is canonical
        address_kind=kind,
    )


# ---------------------------------------------------------------------------
# Base58Check  (Bitcoin legacy + Tron)
# ---------------------------------------------------------------------------
def _b58check_payload(address: str) -> bytes | None:
    """Decode Base58 and verify the 4-byte double-SHA256 checksum."""
    try:
        raw = base58.b58decode(address)
    except Exception:
        return None
    if len(raw) != 25:
        return None
    payload, checksum = raw[:21], raw[21:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        return None
    return payload


def _check_base58(address: str) -> AddressInfo | None:
    payload = _b58check_payload(address)
    if payload is None:
        return None

    version = payload[0]
    if version == _BTC_P2PKH_VERSION:
        return AddressInfo(address, True, CHAIN_BTC, address, "p2pkh")
    if version == _BTC_P2SH_VERSION:
        return AddressInfo(address, True, CHAIN_BTC, address, "p2sh")
    if version == _TRON_VERSION:
        return AddressInfo(address, True, CHAIN_TRON, address, "tron_base58")
    return None


# ---------------------------------------------------------------------------
# Ethereum  (EIP-55)
# ---------------------------------------------------------------------------
def eip55_checksum(address: str) -> str:
    """Return the EIP-55 mixed-case form of a 0x address."""
    body = address[2:].lower()
    digest = keccak.new(digest_bits=256, data=body.encode("ascii")).hexdigest()
    return "0x" + "".join(
        c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(body)
    )


def _check_eth(address: str) -> AddressInfo | None:
    if not _ETH_RE.match(address):
        return None

    body = address[2:]
    info = AddressInfo(
        address=address,
        valid=True,
        chain=CHAIN_ETH,
        address_norm=address.lower(),
        address_kind="eoa_or_contract",
    )

    # All-lower or all-upper carries no checksum - valid, but unverifiable.
    if body == body.lower() or body == body.upper():
        info.warnings.append("no EIP-55 checksum present (all-lowercase or all-uppercase form)")
        return info

    if eip55_checksum(address) != address:
        # Mixed case that fails the checksum is a corrupted address, not a
        # stylistic choice. Reject it - this is exactly the transcription error
        # the checksum exists to catch.
        return AddressInfo(
            address=address,
            valid=False,
            chain=CHAIN_ETH,
            reason="EIP-55 checksum mismatch - address appears mistyped",
        )
    return info


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def detect(raw: str) -> AddressInfo:
    """Detect the chain and validate `raw`. Never raises; inspect `.valid`."""
    if raw is None:
        return AddressInfo(address="", valid=False, reason="no address supplied")

    address = raw.strip()
    if not address:
        return AddressInfo(address="", valid=False, reason="no address supplied")

    # Guard against pasted URIs like "bitcoin:1A1zP..." or "ethereum:0x..."
    for scheme in ("bitcoin:", "ethereum:", "tron:"):
        if address.lower().startswith(scheme):
            address = address[len(scheme) :].split("?", 1)[0]

    if len(address) > 128:
        return AddressInfo(address=raw, valid=False, reason="too long to be a wallet address")

    if address.lower().startswith("0x"):
        result = _check_eth(address)
        if result is not None:
            return result
        return AddressInfo(
            address=raw,
            valid=False,
            chain=CHAIN_ETH,
            reason="expected 0x followed by exactly 40 hex characters",
        )

    if _BECH32_RE.match(address):
        result = _check_segwit(address)
        if result is not None:
            return result
        return AddressInfo(
            address=raw,
            valid=False,
            chain=CHAIN_BTC,
            reason="bech32 checksum or witness program invalid",
        )

    result = _check_base58(address)
    if result is not None:
        return result

    # Give a targeted reason when it merely *looks* like a Base58 address.
    if address[0] in "123" or address[0] == "T":
        return AddressInfo(
            address=raw,
            valid=False,
            reason="Base58Check checksum failed - address appears mistyped",
        )

    return AddressInfo(
        address=raw,
        valid=False,
        reason="unrecognised address format (expected BTC, Ethereum-style 0x, or Tron)",
    )


def validate(raw: str) -> AddressInfo:
    """Like detect(), but raises InvalidAddressError when invalid."""
    info = detect(raw)
    if not info.valid:
        raise InvalidAddressError(info.reason or "invalid address")
    return info


def normalize_address(chain: str, address: str) -> str:
    """Canonical storage/lookup form for an already-well-formed address.

    THE single source of truth for this rule. ETH is case-insensitive and is
    stored lowercase; BTC/TRON Base58 is case-significant and kept verbatim.

    Getting this wrong is silent rather than loud: a mismatched key just
    returns no data, so an ETH trace stops after one hop instead of raising.
    Every lookup path (connector index, graph writer, BFS expansion) must go
    through here.
    """
    return address.lower() if chain == CHAIN_ETH else address


def normalize(raw: str) -> tuple[str, str]:
    """Return (chain, address_norm) for a valid address, else raise."""
    info = validate(raw)
    return info.chain, info.address_norm
