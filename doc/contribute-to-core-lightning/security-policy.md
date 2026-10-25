---
title: Security policy
slug: security-policy
content:
  excerpt: Learn how to responsibly report a security issue.
privacy:
  view: public
---
## Supported Versions

Only the latest `-blake2b.N` release receives security fixes. Upgrade to it before reporting, if you can.

## Reporting a Vulnerability

Report vulnerabilities in this repository to: security@privkey.io

A vulnerability in upstream Core Lightning that is not specific to this version belongs with its maintainers, as described in [their security policy](https://github.com/ElementsProject/lightning/blob/master/SECURITY.md).

**PGP Key:**
```
-----BEGIN PGP PUBLIC KEY BLOCK-----

xjMEaGVRQBYJKwYBBAHaRw8BAQdAF9dwAiS2eOxTwDNy/1LvnTfqP6m8h4rY
BZxx1v30tJjNKXNlY3VyaXR5QHByaXZrZXkuaW8gPHNlY3VyaXR5QHByaXZr
ZXkuaW8+wsARBBMWCgCDBYJoZVFAAwsJBwmQuDUnCJWoCwtFFAAAAAAAHAAg
c2FsdEBub3RhdGlvbnMub3BlbnBncGpzLm9yZ1633ld0W07KI/fGiqv/RPdn
rKNn456SSIdAiJXTdN5bAxUKCAQWAAIBAhkBApsDAh4BFiEE50kamFXBudeZ
trSRuDUnCJWoCwsAAELLAQD8gmp8ClfdlOXbOEeFGuvz4LoDlAktfN4L28Wl
EeedvQD/VrR64FFB0ZsJ4eW0axdjcT3ph4xv96Lqn6tNO0WmUgbOOARoZVFA
EgorBgEEAZdVAQUBAQdANUQ4xZ3hZzlCsOAJeVN7PkZwEF/Q9DdTZNaUkFXT
8T8DAQgHwr4EGBYKAHAFgmhlUUAJkLg1JwiVqAsLRRQAAAAAABwAIHNhbHRA
bm90YXRpb25zLm9wZW5wZ3Bqcy5vcme2RcuuIdqCuXe6p0nzXLc6RICA0iVC
/6RhJxujpAdrdQKbDBYhBOdJGphVwbnXmba0kbg1JwiVqAsLAABrEwEA1Y9e
BF6SXFgvOtu+iRdD6e+a1E1l0j3N8qyqb1tJ39MBAMT4UzjZ9IQ2Brz3ZYmV
kyew0MAIis6DCtVkNduBlBYA
=3LT9
-----END PGP PUBLIC KEY BLOCK-----
```

**Fingerprint:** `E749 1A98 55C1 B9D7 99B6 B491 B835 2708 95A8 0B0B`

**Key Servers:**
- [keys.openpgp.org](https://keys.openpgp.org/search?q=E7491A9855C1B9D799B6B491B835270895A80B0B)
- [keyserver.ubuntu.com](https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xE7491A9855C1B9D799B6B491B835270895A80B0B)

**Process:**
1. Encrypt report with PGP key above
2. Send to security@privkey.io
3. Expect acknowledgment within 48 hours
4. Coordinate disclosure timeline (default: 90 days)

## Signatures For Releases

Releases are signed with `A47D 99B6 DB0D 715D 40C5 9A20 23AE 8A8E A7E2 4E38` (Kyle Santiago, `kyle@privkey.io`), from `v26.06.9-blake2b.8` on by its signing subkey `1004 97C5 8727 107D 9B9D A0FA F160 CC14 F64F 8EF8`. The key is attached to each release as `privkeyio-signing-key.asc`. Verify a download with:

```
gpg --import privkeyio-signing-key.asc
gpg --verify SHA256SUMS-<version>.asc SHA256SUMS-<version>
sha256sum -c --ignore-missing SHA256SUMS-<version>
```

arm64 tarballs have their own manifest, `SHA256SUMS-<version>-arm64`, verified the same way. Releases up to `v26.06.9-blake2b.9` have a single `SHA256SUMS-<version>` covering both.
