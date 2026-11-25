# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic, checksum-valid identifiers: LEI, IBAN, ABA, ABN, BIC and friends.

Every function derives its value from ``(seed, key)`` through ``random.Random(seed + crc32(key))``.
Nothing here touches global random state, the wall clock or ``hash()``.
"""

from __future__ import annotations

import random
import string
import zlib

DEFAULT_SEED = 20260101

_UPPER_DIGITS = string.ascii_uppercase + string.digits


def key_seed(seed: int, key: str) -> int:
    """Return the per-entity seed: ``seed + crc32(key)``."""
    return seed + zlib.crc32(key.encode("utf-8"))


def rng_for(seed: int, key: str) -> random.Random:
    """Return a private ``random.Random`` for one entity key."""
    return random.Random(key_seed(seed, key))


def _to_digits(text: str) -> str:
    """Map letters to 10..35 and keep digits, as ISO 7064 and ISO 13616 require."""
    return "".join(str(int(ch, 36)) for ch in text)


def lei(seed: int, key: str) -> str:
    """Return a 20-character LEI starting ``SYNT`` with ISO 17442 (mod 97-10) check digits."""
    r = rng_for(seed, "lei:" + key)
    body = "SYNT00" + "".join(r.choice(_UPPER_DIGITS) for _ in range(12))
    check = 98 - int(_to_digits(body + "00")) % 97
    return f"{body}{check:02d}"


# country -> (BBAN template, total BBAN length). 'a' letter, 'n' digit.
_IBAN_FORMATS: dict[str, str] = {
    "GB": "aaaannnnnnnnnnnnnn",  # bank(4) sort(6) account(8)
    "AE": "nnnnnnnnnnnnnnnnnnn",  # bank(3) account(16)
    "NL": "aaaannnnnnnnnn",  # bank(4) account(10)
    "DE": "nnnnnnnnnnnnnnnnnn",  # BLZ(8) account(10)
}


def iban(seed: int, key: str, country: str = "GB") -> str:
    """Return an IBAN with ISO 13616 mod-97 check digits. Supported: GB, AE, NL, DE."""
    r = rng_for(seed, "iban:" + key)
    bban = "".join(
        r.choice(string.ascii_uppercase) if c == "a" else r.choice(string.digits) for c in _IBAN_FORMATS[country]
    )
    check = 98 - int(_to_digits(bban + country + "00")) % 97
    return f"{country}{check:02d}{bban}"


def aba(seed: int, key: str) -> str:
    """Return a 9-digit ABA routing number whose 3-7-1 weighted sum is divisible by 10."""
    r = rng_for(seed, "aba:" + key)
    d = [r.randint(0, 1), r.randint(0, 9)]
    if d[0] == 1:  # Federal Reserve routing prefixes run 01..12
        d[1] = r.randint(0, 2)
    elif d[1] == 0:
        d[1] = r.randint(1, 9)
    d += [r.randint(0, 9) for _ in range(6)]
    total = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5])
    d.append((10 - total % 10) % 10)
    return "".join(map(str, d))


_ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)


def abn(seed: int, key: str) -> str:
    """Return an 11-digit ABN: subtract 1 from the first digit, weight, sum divisible by 89."""
    r = rng_for(seed, "abn:" + key)
    tail = [r.randint(0, 9) for _ in range(9)]
    rest = sum(w * d for w, d in zip(_ABN_WEIGHTS[2:], tail, strict=True))
    # Leading pair p = d1 d2 contributes 10*(d1-1) + d2 = p - 10, which spans every residue 0..89.
    p = 10 + (-rest) % 89
    digits = [p // 10, p % 10, *tail]
    return "".join(map(str, digits))


_BIC_COUNTRIES = {"GB", "US", "AU", "AE", "NL", "DE", "CY", "JE", "VG"}


def bic(seed: int, key: str, country: str = "GB", branch: bool = False) -> str:
    """Return a BIC (ISO 9362) with a fictional bank code starting ``Z``: 8 or 11 characters."""
    if country not in _BIC_COUNTRIES:
        raise ValueError(f"unsupported BIC country {country!r}")
    r = rng_for(seed, "bic:" + key)
    bank = "Z" + "".join(r.choice(string.ascii_uppercase) for _ in range(3))
    location = r.choice(string.ascii_uppercase) + r.choice(string.ascii_uppercase + string.digits)
    out = bank + country + location
    return out + ("XXX" if branch else "")


def account_number(seed: int, key: str, length: int = 10) -> str:
    """Return a numeric account number of ``length`` digits that never starts with 0."""
    r = rng_for(seed, "acct:" + key)
    return str(r.randint(1, 9)) + "".join(r.choice(string.digits) for _ in range(length - 1))


def bsb(seed: int, key: str) -> str:
    """Return an Australian BSB in ``NNN-NNN`` form."""
    r = rng_for(seed, "bsb:" + key)
    return f"{r.randint(10, 99)}{r.randint(0, 9)}-{r.randint(0, 999):03d}"


def ein(seed: int, key: str) -> str:
    """Return a US-style employer id in ``NN-NNNNNNN`` form."""
    r = rng_for(seed, "ein:" + key)
    return f"{r.randint(10, 98)}-{r.randint(0, 9999999):07d}"


def trade_id(desk: str, year: int, number: int) -> str:
    """Return a trade id such as ``NC-HOU-26-0001``."""
    return f"NC-{desk.upper()}-{year % 100:02d}-{number:04d}"


def swap_digits(account: str) -> str:
    """Return ``account`` with its last two differing adjacent digits transposed.

    Used to plant a swapped-digits account number. Raises ``ValueError`` if no pair differs.
    """
    chars = list(account)
    for i in range(len(chars) - 2, -1, -1):
        if chars[i].isdigit() and chars[i + 1].isdigit() and chars[i] != chars[i + 1]:
            chars[i], chars[i + 1] = chars[i + 1], chars[i]
            return "".join(chars)
    raise ValueError(f"no adjacent differing digits in {account!r}")
