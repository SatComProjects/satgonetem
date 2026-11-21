"""Characterization tests for :mod:`satgonetem.utils.ip_utils`.

These pin the documented behaviour from ``docs/utils.md`` and the boundary
handling implemented in ``IPUtils``. The class is a small, cached helper layer
and is not expected to validate text as strictly as :mod:`ipaddress`.
"""

from __future__ import annotations

import math

import pytest

from satgonetem.utils.ip_utils import IPUtils

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "1.2.3.4",
        "10.128.0.1",
        "192.168.1.1",
        "255.255.255.255",
    ],
)
def test_is_valid_ipv4_accepts_dotted_quads(address: str) -> None:
    assert IPUtils.is_valid_ipv4(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "010.0.0.1",
        " 1.2.3.4",
        "1.2.3.4\n",
    ],
)
def test_is_valid_ipv4_is_permissive_about_leading_zeros_and_whitespace(
    address: str,
) -> None:
    # ``int()`` tolerates leading zeros and surrounding whitespace, and the
    # implementation relies on it. Only leading zeros are documented.
    assert IPUtils.is_valid_ipv4(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "",
        "1.2.3",
        "1.2.3.4.5",
        "1.2.3.256",
        "1.2.3.-1",
        "1..3.4",
        "a.b.c.d",
        "1.2.3.4.",
    ],
)
def test_is_valid_ipv4_rejects_malformed_addresses(address: str) -> None:
    assert IPUtils.is_valid_ipv4(address) is False


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("0.0.0.0", "0" * 32),
        ("255.255.255.255", "1" * 32),
        ("10.0.0.1", "00001010000000000000000000000001"),
        ("192.168.1.1", "11000000101010000000000100000001"),
        ("010.0.0.1", "00001010000000000000000000000001"),
    ],
)
def test_ipv4_to_binary_returns_exact_bits(address: str, expected: str) -> None:
    bits = IPUtils.ipv4_to_binary(address)

    assert bits == expected
    assert len(bits) == IPUtils.IPV4_ADDRESS_LENGTH
    assert set(bits) <= {"0", "1"}


@pytest.mark.parametrize(
    "address",
    [
        "1.2.3",
        "1.2.3.4.5",
        "1.2.3.256",
        "1.2.3.-1",
        "a.b.c.d",
    ],
)
def test_ipv4_to_binary_rejects_invalid_addresses(address: str) -> None:
    with pytest.raises(ValueError, match="Invalid IPv4 address"):
        IPUtils.ipv4_to_binary(address)


def test_quaddot_converts_exact_32_bit_strings() -> None:
    assert IPUtils.quaddot("00001010000000000000000000000001") == "10.0.0.1"
    assert IPUtils.quaddot("0" * 32) == "0.0.0.0"
    assert IPUtils.quaddot("1" * 32) == "255.255.255.255"


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "1.2.3.4",
        "10.128.0.1",
        "192.168.1.1",
        "255.255.255.255",
    ],
)
def test_quaddot_round_trips_ipv4_to_binary(address: str) -> None:
    assert IPUtils.quaddot(IPUtils.ipv4_to_binary(address)) == address


def test_quaddot_round_trip_canonicalizes_leading_zeros() -> None:
    assert IPUtils.quaddot(IPUtils.ipv4_to_binary("010.0.0.1")) == "10.0.0.1"


@pytest.mark.parametrize(
    ("address", "prefix", "expected"),
    [
        ("10.128.7.200", 0, "0.0.0.0"),
        ("10.128.7.200", 8, "10.0.0.0"),
        ("10.128.7.200", 24, "10.128.7.0"),
        ("10.128.7.200", 27, "10.128.7.192"),
        ("10.128.7.200", 32, "10.128.7.200"),
        ("255.255.255.255", 24, "255.255.255.0"),
    ],
)
def test_summarize_ipv4_address_masks_to_prefix(
    address: str, prefix: int, expected: str
) -> None:
    assert IPUtils.summarize_ipv4_address(address, prefix) == expected


@pytest.mark.parametrize("prefix", [-1, 33, 32.5])
def test_summarize_ipv4_address_rejects_invalid_prefix(prefix: float) -> None:
    with pytest.raises(ValueError, match="Prefix must be between 0 and 32"):
        IPUtils.summarize_ipv4_address("10.128.7.200", prefix)


@pytest.mark.parametrize(
    "address",
    [
        "10.128.7",
        "10.128.7.256",
        "10.128.7.-1",
        "not-an-address",
    ],
)
def test_summarize_ipv4_address_rejects_invalid_address(address: str) -> None:
    with pytest.raises(ValueError, match="Invalid IPv4 address"):
        IPUtils.summarize_ipv4_address(address, 24)


def test_entropy_is_zero_for_uniform_bit_patterns() -> None:
    assert IPUtils.get_ipv4_address_entropy("0.0.0.0") == 0
    assert IPUtils.get_ipv4_address_entropy("255.255.255.255") == 0


def test_entropy_matches_manual_bit_distribution() -> None:
    # 192.168.1.1 decomposes into 7 one bits and 25 zero bits.
    p_one = 7 / 32
    p_zero = 25 / 32
    expected = -32 * (p_zero * math.log2(p_zero) + p_one * math.log2(p_one))

    entropy = IPUtils.get_ipv4_address_entropy("192.168.1.1")

    assert entropy == pytest.approx(expected)
    assert entropy == pytest.approx(24.252110801228653)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "1.2.3.4",
        "10.128.0.1",
        "192.168.1.1",
        "255.255.255.255",
    ],
)
def test_entropy_is_bounded_by_address_width(address: str) -> None:
    entropy = IPUtils.get_ipv4_address_entropy(address)

    assert 0.0 <= entropy <= IPUtils.IPV4_ADDRESS_LENGTH


def test_entropy_rejects_invalid_address() -> None:
    with pytest.raises(ValueError, match="Invalid IPv4 address"):
        IPUtils.get_ipv4_address_entropy("300.1.1.1")
