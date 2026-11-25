# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Checksum validity and determinism of the generated identifiers (independent validators)."""

from __future__ import annotations

import re

import pytest

from tests.benchmark.fsi_commodity import ids

pytestmark = pytest.mark.unit

KEYS = [f"k{i}" for i in range(200)]


def _mod97(text: str) -> int:
    return int("".join(str(int(c, 36)) for c in text)) % 97


def valid_lei(v: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", v)) and _mod97(v) == 1


def valid_iban(v: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}", v)) and _mod97(v[4:] + v[:4]) == 1


def valid_aba(v: str) -> bool:
    if not re.fullmatch(r"[0-9]{9}", v):
        return False
    d = [int(c) for c in v]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def valid_abn(v: str) -> bool:
    if not re.fullmatch(r"[0-9]{11}", v):
        return False
    d = [int(c) for c in v]
    d[0] -= 1
    return sum(w * x for w, x in zip((10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19), d, strict=True)) % 89 == 0


def valid_bic(v: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?", v))


def test_validators_reject_known_bad_values() -> None:
    assert valid_iban("GB82WEST12345698765432")  # published ISO 13616 example
    assert not valid_iban("GB83WEST12345698765432")
    assert valid_aba("011401533")
    assert not valid_aba("011401534")
    assert valid_abn("51824753556")  # published ABR example
    assert not valid_abn("51824753557")
    assert valid_lei("5493001KJTIIGC8Y1R12")  # published ISO 17442 example
    assert not valid_lei("5493001KJTIIGC8Y1R13")


@pytest.mark.parametrize("key", KEYS)
def test_lei_is_synt_and_checksum_valid(key: str) -> None:
    v = ids.lei(ids.DEFAULT_SEED, key)
    assert len(v) == 20
    assert v.startswith("SYNT")
    assert valid_lei(v)


@pytest.mark.parametrize("country", ["GB", "AE", "NL", "DE"])
def test_iban_is_valid(country: str) -> None:
    for key in KEYS:
        v = ids.iban(ids.DEFAULT_SEED, key, country)
        assert v.startswith(country)
        assert valid_iban(v), v


def test_aba_is_valid_with_federal_reserve_prefix() -> None:
    for key in KEYS:
        v = ids.aba(ids.DEFAULT_SEED, key)
        assert valid_aba(v), v
        assert 1 <= int(v[:2]) <= 12


def test_abn_is_valid() -> None:
    for key in KEYS:
        v = ids.abn(ids.DEFAULT_SEED, key)
        assert valid_abn(v), v
        assert v[0] != "0"


@pytest.mark.parametrize("country", ["GB", "US", "AU", "AE"])
@pytest.mark.parametrize("branch", [False, True])
def test_bic_format_and_fictional_bank_code(country: str, branch: bool) -> None:
    for key in KEYS[:50]:
        v = ids.bic(ids.DEFAULT_SEED, key, country, branch)
        assert valid_bic(v)
        assert v.startswith("Z")
        assert v[4:6] == country
        assert len(v) == (11 if branch else 8)


def test_bic_rejects_unknown_country() -> None:
    with pytest.raises(ValueError):
        ids.bic(ids.DEFAULT_SEED, "x", "ZZ")


def test_same_seed_and_key_repeat_and_other_seed_differs() -> None:
    for fn in (ids.lei, ids.aba, ids.abn, ids.bic, ids.account_number, ids.bsb, ids.ein):
        assert fn(1, "a") == fn(1, "a")
        assert fn(1, "a") != fn(2, "a")
        assert fn(1, "a") != fn(1, "b")


def test_swap_digits_changes_account_but_keeps_length_and_digits() -> None:
    for key in KEYS[:50]:
        acct = ids.account_number(ids.DEFAULT_SEED, key)
        swapped = ids.swap_digits(acct)
        assert swapped != acct
        assert sorted(swapped) == sorted(acct)
        assert sum(a != b for a, b in zip(acct, swapped, strict=True)) == 2
    with pytest.raises(ValueError):
        ids.swap_digits("1111")


def test_trade_id_format() -> None:
    assert ids.trade_id("hou", 2026, 7) == "NC-HOU-26-0007"
