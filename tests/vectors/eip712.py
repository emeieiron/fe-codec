"""EIP-712 test vectors from a direct transcription of the specification.

This shares no code with the Fe implementation. It prints the constants used by
examples/transfer/shared/src/eip712_tests.fe and writes tests/vectors/eip712_cases.json,
which tests/eip712_viem.mjs checks again with viem.

Run with: python3 -I tests/vectors/eip712.py
"""

import json
from pathlib import Path

# Keccak-256 (the Ethereum variant: multi-rate padding with 0x01), from FIPS 202's
# Keccak-f[1600] permutation.
ROUND_CONSTANTS = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
ROTATIONS = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
]
MASK = (1 << 64) - 1


def rotl(value, shift):
    return ((value << shift) | (value >> (64 - shift))) & MASK if shift else value


def permute(lanes):
    for constant in ROUND_CONSTANTS:
        c = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ rotl(c[(x + 1) % 5], 1) for x in range(5)]
        lanes = [[lanes[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = rotl(lanes[x][y], ROTATIONS[x][y])
        lanes = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        lanes[0][0] ^= constant
    return lanes


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data) + b"\x01" + b"\x00" * ((-len(data) - 2) % rate) + b"\x80"
    if len(padded) % rate:  # one-byte padding case
        raise AssertionError("padding")
    lanes = [[0] * 5 for _ in range(5)]
    for block in range(0, len(padded), rate):
        for i in range(rate // 8):
            x, y = i % 5, i // 5
            lanes[x][y] ^= int.from_bytes(padded[block + 8 * i: block + 8 * i + 8], "little")
        lanes = permute(lanes)
    out = b"".join(lanes[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out


assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"

# EIP-712 encoding, following the specification's definitions.


def find_dependencies(primary, types, found):
    if primary in found or primary not in types:
        return found
    found.append(primary)
    for member in types[primary]:
        base = member["type"].split("[")[0]
        find_dependencies(base, types, found)
    return found


def encode_type(primary, types):
    deps = find_dependencies(primary, types, [])
    deps = [primary] + sorted(d for d in deps if d != primary)
    return "".join(f"{d}(" + ",".join(f"{m['type']} {m['name']}" for m in types[d]) + ")" for d in deps)


def type_hash(primary, types):
    return keccak256(encode_type(primary, types).encode())


def word(value, signed=False):
    if signed and value < 0:
        value += 1 << 256
    return value.to_bytes(32, "big")


def encode_value(ty, value, types):
    if ty.endswith("]"):
        element = ty[: ty.rindex("[")]
        return keccak256(b"".join(encode_value(element, item, types) for item in value))
    if ty in types:
        return hash_struct(ty, value, types)
    if ty == "bool":
        return word(1 if value else 0)
    if ty == "address":
        return word(int.from_bytes(value, "big"))
    if ty == "string":
        return keccak256(value.encode())
    if ty == "bytes":
        return keccak256(value)
    if ty.startswith("bytes"):
        return bytes(value) + b"\x00" * (32 - len(value))
    if ty.startswith("uint"):
        return word(value)
    if ty.startswith("int"):
        return word(value, signed=True)
    raise ValueError(ty)


def hash_struct(primary, data, types):
    encoded = type_hash(primary, types) + b"".join(
        encode_value(m["type"], data[m["name"]], types) for m in types[primary]
    )
    return keccak256(encoded)


DOMAIN_FIELDS = [
    ("name", "string"), ("version", "string"), ("chainId", "uint256"),
    ("verifyingContract", "address"), ("salt", "bytes32"),
]


def domain_types(domain):
    return [{"name": n, "type": t} for n, t in DOMAIN_FIELDS if n in domain]


def domain_separator(domain):
    return hash_struct("EIP712Domain", domain, {"EIP712Domain": domain_types(domain)})


def digest(domain, primary, message, types):
    return keccak256(b"\x19\x01" + domain_separator(domain) + hash_struct(primary, message, types))


# The JSON Fe Codec writes: compact, keys in the order types, primaryType, domain,
# message; integers as decimal strings; bytes and addresses as lowercase 0x hex.


def json_value(ty, value, types):
    if ty.endswith("]"):
        element = ty[: ty.rindex("[")]
        return [json_value(element, item, types) for item in value]
    if ty in types:
        return {m["name"]: json_value(m["type"], value[m["name"]], types) for m in types[ty]}
    if ty == "bool":
        return value
    if ty == "string":
        return value
    if ty == "address" or ty.startswith("bytes"):
        return "0x" + bytes(value).hex()
    return str(value)


def typed_data_json(domain, primary, message, types):
    deps = find_dependencies(primary, types, [])
    order = [primary] + sorted(d for d in deps if d != primary)
    payload = {
        "types": {"EIP712Domain": domain_types(domain), **{d: types[d] for d in order}},
        "primaryType": primary,
        "domain": {n: json_value(t, domain[n], {}) for n, t in DOMAIN_FIELDS if n in domain},
        "message": json_value(primary, message, types),
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


# The cases. Type definitions mirror examples/transfer/shared/src/typed.fe.


def t(*members):
    return [{"name": n, "type": ty} for n, ty in members]


TYPES = {
    "Person": t(("name", "string"), ("wallet", "address")),
    "Mail": t(("from", "Person"), ("to", "Person"), ("contents", "string")),
    "Group": t(("name", "string"), ("members", "Person[2]")),
    "Grid": t(("cells", "uint8[2][3]")),
    "Inbox": t(("owner", "address"), ("mails", "Mail[0]")),
    "Kinds": t(
        ("flag", "bool"), ("small", "int8"), ("big", "int256"), ("ubig", "uint256"),
        ("data", "bytes"), ("hash", "bytes32"), ("short", "bytes4"), ("label", "string"),
        ("who", "address"), ("level", "uint8"),
    ),
    "Contact": t(("email", "string"),),
    "Party": t(("who", "address"), ("contact", "Contact")),
    "Asset": t(("token", "address"), ("amount", "uint256")),
    "Terms": t(("deadline", "uint64"), ("partial", "bool")),
    "Order": t(("maker", "Party"), ("item", "Asset"), ("terms", "Terms")),
    "Flat": t(("a", "uint256"), ("b", "address"), ("c", "bool")),
}


def subset(primary):
    return {name: TYPES[name] for name in find_dependencies(primary, TYPES, [])}


COW = bytes.fromhex("cd2a3d9f938e13cd947ec05abc7fe734df8dd826")
BOB = bytes.fromhex("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
VERIFIER = bytes.fromhex("cccccccccccccccccccccccccccccccccccccccc")
SALT = bytes(range(1, 33))
cow = {"name": "Cow", "wallet": COW}
bob = {"name": "Bob", "wallet": BOB}
mail = {"from": cow, "to": bob, "contents": "Hello, Bob!"}

DOMAINS = {
    "MAIL": {"name": "Ether Mail", "version": "1", "chainId": 1, "verifyingContract": VERIFIER},
    "FULL": {"name": "Full", "version": "2", "chainId": 137, "verifyingContract": VERIFIER, "salt": SALT},
    "PARTIAL": {"name": "Partial", "chainId": 10},
    "EMPTY": {},
}

CASES = [
    ("MAIL", "Mail", mail, "MAIL"),
    ("GROUP", "Group", {"name": "Team", "members": [cow, bob]}, "FULL"),
    ("GRID", "Grid", {"cells": [[1, 2], [3, 4], [5, 255]]}, "PARTIAL"),
    ("INBOX", "Inbox", {"owner": COW, "mails": []}, "EMPTY"),
    ("KINDS", "Kinds", {
        "flag": True, "small": -12, "big": -(1 << 200), "ubig": (1 << 255) + 7,
        "data": bytes([1, 2, 3]), "hash": bytes(range(32)), "short": bytes.fromhex("deadbeef"),
        "label": 'say "hi" ü', "who": BOB, "level": 1,
    }, "MAIL"),
    ("ORDER", "Order", {
        "maker": {"who": COW, "contact": {"email": "cow@example.org"}},
        "item": {"token": bytes(19) + b"\x01", "amount": 10 ** 18},
        "terms": {"deadline": 1700000000, "partial": False},
    }, "FULL"),
]


def fe_word(data: bytes) -> str:
    return "0x" + data.hex()


def fe_literal(text: str):
    return len(text.encode()), json.dumps(text, ensure_ascii=False)


def main():
    # The EIP's own reference values, from its Example.js.
    m = subset("Mail")
    assert encode_type("Mail", m) == "Mail(Person from,Person to,string contents)Person(string name,address wallet)"
    assert type_hash("Mail", m).hex() == "a0cedeb2dc280ba39b857546d74f5549c3a1d7bdc2dd96bf881f76108e23dac2"
    assert hash_struct("Mail", mail, m).hex() == "c52c0ee5d84264471806290a3f2c4cecfc5490626bf912d01f240d7a274b371e"
    assert domain_separator(DOMAINS["MAIL"]).hex() == "f2cee375fa42b42143804025fc449deafd50cc031ca257e0b194a650a912090f"
    assert digest(DOMAINS["MAIL"], "Mail", mail, m).hex() == "be609aee343fb3c4b28e1df9e632fca64fcfaede20f02e86244efddf30957bd2"

    cases = []
    for label, primary, message, domain_name in CASES:
        types = subset(primary)
        domain = DOMAINS[domain_name]
        encoded = encode_type(primary, types)
        payload = typed_data_json(domain, primary, message, types)
        n, literal = fe_literal(encoded)
        print(f"const {label}_ENCODE_TYPE: [u8; {n}] = {literal}")
        print(f"const {label}_TYPE_HASH: u256 = {fe_word(type_hash(primary, types))}")
        print(f"const {label}_HASH: u256 = {fe_word(hash_struct(primary, message, types))}")
        print(f"const {label}_DIGEST: u256 = {fe_word(digest(domain, primary, message, types))}")
        n, literal = fe_literal(payload)
        print(f"const {label}_JSON: [u8; {n}] = {literal}")
        cases.append({
            "label": label,
            "json": payload,
            "encodeType": encoded,
            "typeHash": fe_word(type_hash(primary, types)),
            "hashStruct": fe_word(hash_struct(primary, message, types)),
            "domainSeparator": fe_word(domain_separator(domain)),
            "digest": fe_word(digest(domain, primary, message, types)),
        })
    for name, domain in DOMAINS.items():
        print(f"const {name}_SEPARATOR: u256 = {fe_word(domain_separator(domain))}")
    flat = encode_type("Flat", subset("Flat"))
    print(f"// Flat: {flat}")
    out = Path(__file__).with_name("eip712_cases.json")
    out.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
