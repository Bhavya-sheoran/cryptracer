"""Chain detection / address validation tests.

Vectors are well-known public addresses and the reference test vectors from
BIP-173 (bech32), BIP-350 (bech32m) and EIP-55.
"""

import pytest

from app.services.chain_detect import (
    CHAIN_BTC,
    CHAIN_ETH,
    CHAIN_TRON,
    InvalidAddressError,
    detect,
    eip55_checksum,
    normalize,
    validate,
)

# --- valid vectors: (address, chain, kind) ---------------------------------
VALID = [
    # Bitcoin legacy Base58Check
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", CHAIN_BTC, "p2pkh"),   # genesis coinbase
    ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", CHAIN_BTC, "p2sh"),
    # Bitcoin segwit (BIP-173 / BIP-350 vectors)
    ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", CHAIN_BTC, "p2wpkh"),
    (
        "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3",
        CHAIN_BTC,
        "p2wsh",
    ),
    (
        "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr",
        CHAIN_BTC,
        "p2tr",
    ),
    # Ethereum - EIP-55 checksummed and plain lowercase
    ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", CHAIN_ETH, "eoa_or_contract"),
    ("0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae", CHAIN_ETH, "eoa_or_contract"),
    # Tron - USDT-TRC20 contract, the dominant rail in reported crypto fraud
    ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", CHAIN_TRON, "tron_base58"),
]


@pytest.mark.parametrize("address,chain,kind", VALID)
def test_valid_addresses_detected(address, chain, kind):
    info = detect(address)
    assert info.valid, f"{address} rejected: {info.reason}"
    assert info.chain == chain
    assert info.address_kind == kind


def test_btc_and_tron_base58_are_not_confused():
    """Both are Base58Check; only the version byte separates them."""
    assert detect("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa").chain == CHAIN_BTC
    assert detect("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").chain == CHAIN_TRON


# --- normalisation ---------------------------------------------------------
def test_eth_normalises_to_lowercase():
    chain, norm = normalize("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
    assert chain == CHAIN_ETH
    assert norm == "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"


def test_base58_normalisation_preserves_case():
    """Base58 is case-significant - lowercasing would corrupt the address."""
    addr = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    assert normalize(addr)[1] == addr


def test_bech32_normalises_to_lowercase():
    upper = "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4"
    info = detect(upper)
    assert info.valid
    assert info.address_norm == upper.lower()


# --- checksum enforcement --------------------------------------------------
def test_eth_mixed_case_bad_checksum_rejected():
    """A mistyped character in a checksummed address must not slip through."""
    bad = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"  # final char case flipped
    info = detect(bad)
    assert not info.valid
    assert "EIP-55" in info.reason


def test_eth_lowercase_warns_about_missing_checksum():
    info = detect("0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae")
    assert info.valid
    assert any("EIP-55" in w for w in info.warnings)


def test_eip55_checksum_matches_reference_vector():
    assert (
        eip55_checksum("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed")
        == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    )


def test_btc_base58_bad_checksum_rejected():
    bad = "1A1zP1eP5QGefi2DMPTfTL5SLmv7Divfna"  # last chars altered
    info = detect(bad)
    assert not info.valid
    assert "checksum" in info.reason.lower()


def test_tron_bad_checksum_rejected():
    bad = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6a"
    assert not detect(bad).valid


def test_bech32_bad_checksum_rejected():
    bad = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5"
    assert not detect(bad).valid


def test_bech32_mixed_case_rejected():
    """BIP-173 forbids mixed case outright."""
    mixed = "bc1QW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert not detect(mixed).valid


def test_bech32m_constant_enforced_for_taproot():
    """A v1 (taproot) program using the bech32 constant instead of bech32m is
    invalid under BIP-350."""
    # BIP-350 invalid vector: v1 witness encoded with the bech32 constant.
    assert not detect("bc1p38j9r5y49hruaue7wxjce0updqjuyyx0kh56v8s25huc6995vvpql3jow4").valid


# --- malformed input -------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not-an-address",
        "0x",
        "0x1234",                                        # too short
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAedFF",  # too long
        "0xzzzzb6053f3e94c9b9a09f33669435e7ef1beaed",    # non-hex
        "1" * 200,
    ],
)
def test_malformed_rejected(bad):
    info = detect(bad)
    assert not info.valid
    assert info.reason


def test_whitespace_is_tolerated():
    info = detect("  1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa \n")
    assert info.valid
    assert info.chain == CHAIN_BTC


def test_uri_scheme_prefix_is_stripped():
    """Victims often paste a payment URI straight out of a chat app."""
    info = detect("bitcoin:1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa?amount=0.5")
    assert info.valid
    assert info.chain == CHAIN_BTC


def test_none_input_does_not_raise():
    assert not detect(None).valid


def test_validate_raises_on_invalid():
    with pytest.raises(InvalidAddressError):
        validate("not-an-address")


def test_validate_returns_info_on_valid():
    assert validate("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa").chain == CHAIN_BTC
