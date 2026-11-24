"""Random model specs, and what the specification says about them.

A spec is a list of `Model`s. Everything the tests expect is computed here from docs/formats.md
(through tests/vectors/reference.py) and the EIP-712 specification (tests/vectors/eip712.py).
Nothing in this module reads the generator's output.

Types are tuples:

    ('int', signed, bits)  ('bool',)  ('addr',)
    ('text', n)  ('buf', n)  ('packed', n)       n is an int, or a const parameter's name
    ('opt', t)  ('arr', t, n)                    n likewise
    ('list', t, n)                               List<t, n>: 0 to n elements, which are Copy
    ('map', k, v, n)                             Map<k, v, n>: 0 to n entries; k is an unsigned
                                                 integer, text, addr or packed, never a parameter,
                                                 and v is Copy
    ('tup', (t, ...))                            1 to 8 element types
    ('ref', name, args)                          a local record or enum; args are types or ints
    ('param', name)                              a type parameter of the enclosing generic record

A map's value is a list of (key, value) pairs in the order they are inserted, which is never the
key order of two or more of them. Keys are Python ints, strs, and bytes (addr and packed).
"""

import contextlib
import importlib.util
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
VECTORS = os.path.normpath(os.path.join(HERE, "..", "vectors"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(VECTORS, filename))
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):  # reference.py prints its constants
        spec.loader.exec_module(module)
    return module


reference = _load("fuzz_reference_vectors", "reference.py")
eip712 = _load("fuzz_eip712_vectors", "eip712.py")



# eip712.py checks its own Keccak sponge against hashlib's SHA3-256 when it is imported.

BOOL = ("bool",)
ADDR = ("addr",)


def Int(signed, bits):
    return ("int", signed, bits)


class Field:
    def __init__(self, name, ty, fid, vis="pub", id_style="dec"):
        self.name = name
        self.ty = ty
        self.id = fid  # the effective ID
        self.vis = vis
        self.id_style = id_style  # how an explicit ID is written


class Variant:
    def __init__(self, name, vid, id_style="dec"):
        self.name = name
        self.id = vid
        self.id_style = id_style


class Model:
    def __init__(self, name, kind, module):
        self.name = name
        self.kind = kind  # 'record' or 'enum'
        self.module = module
        self.vis = "pub"
        self.generics = []  # [(is_const, name, copy_bound)]
        self.fields = []  # declaration order
        self.variants = []  # declaration order
        self.explicit = False  # members carry #[codec_id]
        self.copy_impl = False
        self.eip712 = False
        self.skip_unknown = False  # #[codec_skip_unknown]
        self.plain = False
        self.copy_path = "Copy"  # how `impl Copy for` spells the trait

    @property
    def is_record(self):
        return self.kind == "record"

    @property
    def is_generic(self):
        return bool(self.generics)

    def sorted_fields(self):
        return sorted(self.fields, key=lambda f: f.id)


class Spec:
    def __init__(self):
        self.models = []
        self.modules = 1

    def model(self, name):
        for m in self.models:
            if m.name == name:
                return m
        raise KeyError(name)


# ----------------------------------------------------------------------------------------
# Types.


def fe_type(t):
    k = t[0]
    if k == "int":
        return ("i" if t[1] else "u") + str(t[2])
    if k == "bool":
        return "bool"
    if k == "addr":
        return "Address"
    if k == "text":
        return "Text<%s>" % t[1]
    if k == "buf":
        return "BoundedBuffer<%s>" % t[1]
    if k == "packed":
        return "PackedBytes<%s>" % t[1]
    if k == "opt":
        return "Option<%s>" % fe_type(t[1])
    if k == "arr":
        return "[%s; %s]" % (fe_type(t[1]), t[2])
    if k == "list":
        return "List<%s, %s>" % (fe_type(t[1]), t[2])
    if k == "map":
        return "Map<%s, %s, %s>" % (fe_type(t[1]), fe_type(t[2]), t[3])
    if k == "tup":
        return "(%s%s)" % (", ".join(fe_type(e) for e in t[1]), "," if len(t[1]) == 1 else "")
    if k == "ref":
        return t[1] + ("<%s>" % ", ".join(fe_arg(a) for a in t[2]) if t[2] else "")
    if k == "param":
        return t[1]
    raise ValueError(t)


def fe_arg(a):
    return fe_type(a) if isinstance(a, tuple) else str(a)


def subst(t, env):
    """`t` with the generic parameters in `env` (name -> type or int) replaced."""
    k = t[0]

    def size(n):
        return env.get(n, n) if isinstance(n, str) else n

    if k in ("text", "buf", "packed"):
        return (k, size(t[1]))
    if k == "opt":
        return ("opt", subst(t[1], env))
    if k == "arr":
        return ("arr", subst(t[1], env), size(t[2]))
    if k == "list":
        return ("list", subst(t[1], env), size(t[2]))
    if k == "map":
        return ("map", t[1], subst(t[2], env), size(t[3]))
    if k == "tup":
        return ("tup", tuple(subst(e, env) for e in t[1]))
    if k == "ref":
        return ("ref", t[1], tuple(subst(a, env) if isinstance(a, tuple) else size(a) for a in t[2]))
    if k == "param":
        return env.get(t[1], t)
    return t


def walk(t):
    yield t
    if t[0] in ("opt", "arr", "list"):
        yield from walk(t[1])
    elif t[0] == "map":
        yield from walk(t[1])
        yield from walk(t[2])
    elif t[0] == "tup":
        for e in t[1]:
            yield from walk(e)
    elif t[0] == "ref":
        for a in t[2]:
            if isinstance(a, tuple):
                yield from walk(a)


def instance_fields(spec, t):
    """The fields of the record `t` (a ref), with generic arguments substituted, in ascending
    ID order: [(name, type, id)]."""
    m = spec.model(t[1])
    env = {g[1]: a for g, a in zip(m.generics, t[2])}
    return [(f.name, subst(f.ty, env), f.id) for f in m.sorted_fields()]


def weight(spec, t):
    """How many type nodes `t` expands to: scalars and enums count 1, arrays count their length
    times their element, and a record is 1 plus its fields, instance arguments substituted. A const
    parameter used as a length counts as 3."""
    k = t[0]
    if k == "opt":
        return 1 + weight(spec, t[1])
    if k in ("arr", "list"):
        return 1 + (t[2] if isinstance(t[2], int) else 3) * weight(spec, t[1])
    if k == "map":
        return 1 + (t[3] if isinstance(t[3], int) else 3) * (1 + weight(spec, t[2]))
    if k == "tup":
        return 1 + sum(weight(spec, e) for e in t[1])
    if k == "ref" and spec.model(t[1]).is_record:
        return 1 + sum(weight(spec, ft) for _, ft, _ in instance_fields(spec, t))
    return 1


def is_copy(spec, t):
    k = t[0]
    if k == "opt":
        return is_copy(spec, t[1])
    if k == "arr" or k == "list":
        return is_copy(spec, t[1])
    if k == "map":
        return is_copy(spec, t[2])  # the keys always are
    if k == "tup":
        return all(is_copy(spec, e) for e in t[1])
    if k == "ref":
        m = spec.model(t[1])
        return m.copy_impl and not m.generics
    return True  # scalars and, by the instantiation rule, parameters


def uses_array(t):
    """Whether a parameter appears under an array, a list or a map in `t`."""
    k = t[0]
    if k == "arr" or k == "list":
        return any(w[0] == "param" for w in walk(t[1]))
    if k == "map":
        return any(w[0] == "param" for w in walk(t[2]))
    if k == "opt":
        return uses_array(t[1])
    if k == "tup":
        return any(uses_array(e) for e in t[1])
    return False


def type_param_needs_copy(m, name):
    for f in m.fields:
        t = f.ty
        for w in walk(t):
            if w[0] in ("arr", "list") and any(x == ("param", name) for x in walk(w[1])):
                return True
            if w[0] == "map" and any(x == ("param", name) for x in walk(w[2])):
                return True
    return any(g[1] == name and g[2] for g in m.generics)


def closure_models(spec, t, out=None):
    """Names of the local models `t` reaches (including itself when a ref)."""
    out = [] if out is None else out
    for w in walk(t):
        if w[0] == "ref" and w[1] not in out:
            out.append(w[1])
            m = spec.model(w[1])
            if m.is_record:
                env = {g[1]: a for g, a in zip(m.generics, w[2])}
                for f in m.fields:
                    closure_models(spec, subst(f.ty, env), out)
    return out


def instances(spec):
    """Every concrete generic instantiation a field of a non-generic model names, nested ones
    included, in first-seen order."""
    found = []

    def see(t):
        for w in walk(t):
            if w[0] == "ref":
                m = spec.model(w[1])
                if m.generics and w not in found:
                    found.append(w)
                    env = {g[1]: a for g, a in zip(m.generics, w[2])}
                    for f in m.fields:
                        see(subst(f.ty, env))

    for m in spec.models:
        if m.is_record and not m.generics:
            for f in m.fields:
                see(f.ty)
    return found


# ----------------------------------------------------------------------------------------
# Values. Records are dicts keyed by field name; enums are variant names.

TEXT_POOL = ["a", "b", "Z", "0", " ", '"', "\\", "/", "\n", "\t", "\x00", "\x1f", "\x7f", "\x08", "\r",
             "é", "水", "😀", " ", "{", "}", "[", "]", ",", ":"]


def rand_int(rng, signed, bits):
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    r = rng.random()
    if r < 0.12:
        return lo
    if r < 0.24:
        return hi
    if r < 0.32:
        return 0
    if r < 0.40 and signed:
        return -1
    if r < 0.50:
        return 1
    if r < 0.72:
        return rng.randint(max(lo, -100), min(hi, 100))
    if r < 0.82:
        return rng.randint(max(lo, -70000), min(hi, 70000))
    return rng.randint(lo, hi)


def rand_text(rng, n):
    target = rng.choice([0, 1, n, rng.randint(0, n), rng.randint(0, n)])
    out, used = [], 0
    while True:
        ch = rng.choice(TEXT_POOL) if rng.random() < 0.6 else rng.choice("abcdefghij")
        size = len(ch.encode())
        if used + size > min(target, n):
            break
        out.append(ch)
        used += size
    return "".join(out)


def gen_value(spec, rng, t):
    k = t[0]
    if k == "int":
        return rand_int(rng, t[1], t[2])
    if k == "bool":
        return rng.random() < 0.5
    if k == "addr":
        return bytes(rng.randrange(256) for _ in range(20))
    if k == "text":
        return rand_text(rng, t[1])
    if k == "buf":
        n = rng.choice([0, 1, t[1], rng.randint(0, t[1])])
        return bytes(rng.randrange(256) for _ in range(min(n, t[1])))
    if k == "packed":
        return bytes(rng.randrange(256) for _ in range(t[1]))
    if k == "opt":
        return None if rng.random() < 0.4 else gen_value(spec, rng, t[1])
    if k == "arr":
        return [gen_value(spec, rng, t[1]) for _ in range(t[2])]
    if k == "list":
        # Empty, full and everything between, so each length class is tried.
        n = t[2]
        count = rng.choice([0, 1, n, n, rng.randint(0, n), rng.randint(0, n)])
        return [gen_value(spec, rng, t[1]) for _ in range(min(count, n))]
    if k == "map":
        return gen_map(spec, rng, t)
    if k == "tup":
        return [gen_value(spec, rng, e) for e in t[1]]
    if k == "ref":
        m = spec.model(t[1])
        if not m.is_record:
            return rng.choice(m.variants).name
        return {name: gen_value(spec, rng, ft) for name, ft, _ in instance_fields(spec, t)}
    raise ValueError(t)


def key_order(keys):
    """`keys` in the canonical order of a map: the order of their deterministic CBOR encodings."""
    return sorted(keys, key=reference.cbor)


def gen_map(spec, rng, t):
    """The (key, value) pairs of a map of type `t`, in an insertion order that is not key order.
    Short of room in a small key type, the map has fewer entries than it drew."""
    n = t[3]
    count = min(rng.choice([0, 1, 2, n, n, rng.randint(0, n), rng.randint(0, n)]), n)
    keys = []
    for _ in range(count * 4):
        if len(keys) == count:
            break
        key = gen_value(spec, rng, t[1])
        if key not in keys:
            keys.append(key)
    rng.shuffle(keys)
    if len(keys) > 1 and keys == key_order(keys):
        keys.reverse()
    return [(key, gen_value(spec, rng, t[2])) for key in keys]


def maps_in(spec, t, v):
    """[(map type, pairs)] for every map in the value `v` of type `t`, outermost first."""
    k = t[0]
    out = []
    if k == "map":
        out.append((t, v))
        for _, x in v:
            out.extend(maps_in(spec, t[2], x))
    elif k == "opt":
        if v is not None:
            out.extend(maps_in(spec, t[1], v))
    elif k == "arr" or k == "list":
        for x in v:
            out.extend(maps_in(spec, t[1], x))
    elif k == "tup":
        for e, x in zip(t[1], v):
            out.extend(maps_in(spec, e, x))
    elif k == "ref" and spec.model(t[1]).is_record:
        for name, ft, _ in instance_fields(spec, t):
            out.extend(maps_in(spec, ft, v[name]))
    return out


def replace_field(value, name, new):
    out = dict(value)
    out[name] = new
    return out


# ----------------------------------------------------------------------------------------
# docs/formats.md: JSON and CBOR.


def to_js(spec, t, v):
    k = t[0]
    if k in ("int", "bool", "text"):
        return v
    if k in ("addr", "buf", "packed"):
        return reference.Hex(v)
    if k == "opt":
        return None if v is None else to_js(spec, t[1], v)
    if k == "arr" or k == "list":
        return [to_js(spec, t[1], x) for x in v]
    if k == "map":
        return reference.Map({to_js(spec, t[1], key): to_js(spec, t[2], x) for key, x in v})
    if k == "tup":
        return [to_js(spec, e, x) for e, x in zip(t[1], v)]
    m = spec.model(t[1])
    if not m.is_record:
        return v
    return {name: to_js(spec, ft, v[name]) for name, ft, _ in instance_fields(spec, t)}


def to_cbor(spec, t, v):
    k = t[0]
    if k in ("int", "bool", "text", "addr", "buf", "packed"):
        return v
    if k == "opt":
        return None if v is None else to_cbor(spec, t[1], v)
    if k == "arr" or k == "list":
        return [to_cbor(spec, t[1], x) for x in v]
    if k == "map":
        return reference.Map({key: to_cbor(spec, t[2], x) for key, x in v})
    if k == "tup":
        return [to_cbor(spec, e, x) for e, x in zip(t[1], v)]
    m = spec.model(t[1])
    if not m.is_record:
        return next(x.id for x in m.variants if x.name == v)
    return {fid: to_cbor(spec, ft, v[name]) for name, ft, fid in instance_fields(spec, t)}


def junk_json(rng, depth=0):
    """JSON text of a value an older reader knows nothing about: every kind, fractions and
    exponents included, small enough for any limit."""
    kinds = ["int", "real", "bool", "null", "text"] + (["arr", "obj"] if depth < 3 else [])
    k = rng.choice(kinds)
    if k == "int":
        return str(rng.choice([0, -1, 7, 255, 2 ** 64, -(2 ** 70)]))
    if k == "real":
        return rng.choice(["0.5", "-1.25", "1e3", "2E-2", "1.5e+10", "-0.0", "10.125e2"])
    if k == "bool":
        return rng.choice(["true", "false"])
    if k == "null":
        return "null"
    if k == "text":
        return rng.choice(['""', '"x"', '"a\\"b\\\\c\\/d\\n\\u00e9\\ud83d\\ude00"', '"héllo 😀"'])
    if k == "arr":
        return "[" + ",".join(junk_json(rng, depth + 1) for _ in range(rng.randint(0, 3))) + "]"
    names = rng.sample(["a", "b", "owner", "id", "k" * 40], rng.randint(0, 3))
    return "{" + ",".join('"%s":%s' % (n, junk_json(rng, depth + 1)) for n in names) + "}"


def junk_cbor(rng, depth=0):
    """A Python value for `reference.cbor` that is never None, since a map leaves those out."""
    kinds = ["int", "bool", "text", "bytes", "null"] + (["arr", "map"] if depth < 3 else [])
    k = rng.choice(kinds)
    if k == "int":
        return rng.choice([0, 23, 24, 255, 256, 65536, 2 ** 32, 2 ** 64 - 1, 2 ** 64, -1, -25, -(2 ** 64), -(2 ** 200)])
    if k == "bool":
        return rng.random() < 0.5
    if k == "text":
        return rng.choice(["", "x", "héllo 😀"])
    if k == "bytes":
        return bytes(rng.randrange(256) for _ in range(rng.randint(0, 4)))
    if k == "null":
        return [None]
    if k == "arr":
        return [junk_cbor(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {i: junk_cbor(rng, depth + 1) for i in rng.sample([0, 1, 5, 23, 24, 300], rng.randint(0, 3))}


def to_cbor_with_unknown(spec, rng, t, v, added):
    """`to_cbor`, with unknown fields added to every record marked #[codec_skip_unknown]: IDs the
    record does not use, before, between and after its own. `added` counts them."""
    k = t[0]
    if k == "opt":
        return None if v is None else to_cbor_with_unknown(spec, rng, t[1], v, added)
    if k == "arr" or k == "list":
        return [to_cbor_with_unknown(spec, rng, t[1], x, added) for x in v]
    if k == "map":
        return reference.Map({key: to_cbor_with_unknown(spec, rng, t[2], x, added) for key, x in v})
    if k == "tup":
        return [to_cbor_with_unknown(spec, rng, e, x, added) for e, x in zip(t[1], v)]
    if k != "ref" or not spec.model(t[1]).is_record:
        return to_cbor(spec, t, v)
    m = spec.model(t[1])
    out = {fid: to_cbor_with_unknown(spec, rng, ft, v[name], added) for name, ft, fid in instance_fields(spec, t)}
    if m.skip_unknown:
        top = max(out, default=0)
        # Gaps among the small IDs (the range is cut short, since an ID may be 2 ** 64 - 1), and a
        # few far above the record's own.
        free = [i for i in range(1, min(top, 1000) + 3) if i not in out]
        free += [i for i in sorted({top + 1000, 2 ** 32 + top, 2 ** 64 - 1}) if i not in out and i < 2 ** 64]
        for i in rng.sample(free, rng.randint(1, min(3, len(free)))):
            out[i] = junk_cbor(rng)
            added[0] += 1
    return out


def json_bytes(spec, t, v):
    return reference.js(to_js(spec, t, v)).encode()


def cbor_bytes(spec, t, v):
    return reference.cbor(to_cbor(spec, t, v))


# JSON a decoder must also accept: members in any order, whitespace between tokens, `null` for an
# absent optional field, \u escapes and either case of hex digits.

WS = [" ", "\t", "\n", "\r", "  ", " \n\t"]


def noisy_json(spec, rng, t, v, drop=None, counter=None, added=None):
    """Compact-JSON-equivalent text of `v`. `drop` is (occurrence, field name): that field of that
    record occurrence (numbered in visiting order) is left out. A record marked
    #[codec_skip_unknown] also gets members it does not know; `added` counts them."""
    counter = counter if counter is not None else [0]

    def ws():
        return rng.choice(["", "", rng.choice(WS)])

    def string(s):
        out = ['"']
        for ch in s:
            o = ord(ch)
            r = rng.random()
            if r < 0.12:
                if o > 0xFFFF:
                    o -= 0x10000
                    out.append("\\u%04x\\u%04X" % (0xD800 + (o >> 10), 0xDC00 + (o & 0x3FF)))
                else:
                    out.append(rng.choice(["\\u%04x", "\\u%04X"]) % o)
            elif ch == '"':
                out.append('\\"')
            elif ch == "\\":
                out.append("\\\\")
            elif ch == "/" and r < 0.3:
                out.append("\\/")
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\x08":
                out.append("\\b")
            elif o < 32:
                out.append("\\u%04x" % o)
            else:
                out.append(ch)
        out.append('"')
        return "".join(out)

    def key_text(t, key):
        """A map key: always a JSON string, with an integer in canonical decimal."""
        k = t[0]
        if k == "int":
            return '"%d"' % key
        return go(t, key)

    def go(t, v):
        k = t[0]
        if k == "int":
            return str(v)
        if k == "bool":
            return "true" if v else "false"
        if k == "text":
            return string(v)
        if k in ("addr", "buf", "packed"):
            h = v.hex()
            return '"0x' + (h.upper() if rng.random() < 0.3 else h) + '"'
        if k == "opt":
            return "null" if v is None else go(t[1], v)
        if k == "arr" or k == "list":
            items = [ws() + go(t[1], x) + ws() for x in v]
            return "[" + ws() + ",".join(items) + ws() + "]"
        if k == "map":
            # Members in any order; the keys are strings whatever their type.
            members = [ws() + key_text(t[1], key) + ws() + ":" + ws() + go(t[2], x) + ws() for key, x in v]
            rng.shuffle(members)
            return "{" + ws() + ",".join(members) + ws() + "}"
        if k == "tup":
            items = [ws() + go(e, x) + ws() for e, x in zip(t[1], v)]
            return "[" + ws() + ",".join(items) + ws() + "]"
        m = spec.model(t[1])
        if not m.is_record:
            return '"' + v + '"'
        occurrence = counter[0]
        counter[0] += 1
        members = []
        for name, ft, _ in instance_fields(spec, t):
            if drop is not None and drop[0] == occurrence and drop[1] == name:
                continue
            if ft[0] == "opt" and v[name] is None and rng.random() < 0.5:
                continue
            members.append(ws() + '"' + name + '"' + ws() + ":" + ws() + go(ft, v[name]) + ws())
        if m.skip_unknown and added is not None:
            known = {name for name, _, _ in instance_fields(spec, t)}
            for i in range(rng.randint(1, 3)):
                name = rng.choice(["unknown_%d" % i, "extra", "k" * rng.randint(32, 40)])
                if name not in known:
                    members.append(ws() + '"' + name + '"' + ws() + ":" + ws() + junk_json(rng) + ws())
                    added[0] += 1
        rng.shuffle(members)
        return "{" + ws() + ",".join(members) + ws() + "}"

    return ws() + go(t, v) + ws()


def record_occurrences(spec, t, v, out=None, in_maps=None, under_map=False):
    """[(occurrence, record type, value)] for every record in `v`, in visiting order. The numbers
    of the records that lie inside the value of a map are added to the set `in_maps`."""
    out = [] if out is None else out
    k = t[0]
    if k == "opt":
        if v is not None:
            record_occurrences(spec, t[1], v, out, in_maps, under_map)
    elif k == "arr" or k == "list":
        for x in v:
            record_occurrences(spec, t[1], x, out, in_maps, under_map)
    elif k == "map":
        for _, x in v:
            record_occurrences(spec, t[2], x, out, in_maps, True)
    elif k == "tup":
        for e, x in zip(t[1], v):
            record_occurrences(spec, e, x, out, in_maps, under_map)
    elif k == "ref" and spec.model(t[1]).is_record:
        if under_map and in_maps is not None:
            in_maps.add(len(out))
        out.append((len(out), t, v))
        for name, ft, _ in instance_fields(spec, t):
            record_occurrences(spec, ft, v[name], out, in_maps, under_map)
    return out


def dropped_value(spec, t, v, occurrence, name):
    """`v` with field `name` of the record occurrence set to None (an optional field)."""
    count = [0]

    def go(t, v):
        k = t[0]
        if k == "opt":
            return None if v is None else go(t[1], v)
        if k == "arr" or k == "list":
            return [go(t[1], x) for x in v]
        if k == "map":
            return [(key, go(t[2], x)) for key, x in v]
        if k == "tup":
            return [go(e, x) for e, x in zip(t[1], v)]
        if k == "ref" and spec.model(t[1]).is_record:
            mine = count[0]
            count[0] += 1
            out = {}
            for fname, ft, _ in instance_fields(spec, t):
                out[fname] = go(ft, v[fname])
            if mine == occurrence:
                out[name] = None
            return out
        return v

    return go(t, v)


# ----------------------------------------------------------------------------------------
# EIP-712.


def eip_type(spec, t):
    k = t[0]
    if k == "bool":
        return "bool"
    if k == "int":
        return ("int" if t[1] else "uint") + str(t[2])
    if k == "text":
        return "string"
    if k == "buf":
        return "bytes"
    if k == "packed":
        return "bytes%d" % t[1]
    if k == "addr":
        return "address"
    if k == "arr":
        return "%s[%d]" % (eip_type(spec, t[1]), t[2])
    if k == "list":
        return "%s[]" % eip_type(spec, t[1])
    m = spec.model(t[1])
    return t[1] if m.is_record else "uint8"


def to_eip(spec, t, v):
    k = t[0]
    if k == "arr" or k == "list":
        return [to_eip(spec, t[1], x) for x in v]
    if k == "ref":
        m = spec.model(t[1])
        if m.is_record:
            return {name: to_eip(spec, ft, v[name]) for name, ft, _ in instance_fields(spec, t)}
        return next(x.id for x in m.variants if x.name == v)
    return v


def eip_types(spec, root):
    """The `types` table of the struct names reachable from the record `root` (a ref)."""
    out = {}
    for name in closure_models(spec, root):
        m = spec.model(name)
        if m.is_record:
            t = ("ref", name, ())
            out[name] = [{"name": n, "type": eip_type(spec, ft)} for n, ft, _ in instance_fields(spec, t)]
    return out


def eip_closure_structs(spec, root):
    return [n for n in closure_models(spec, root) if spec.model(n).is_record]


def eligible_for_eip712(spec, m):
    """Whether `m`'s whole closure is EIP-712-expressible."""
    if not m.is_record or m.generics or m.name == "EIP712Domain":
        return False
    root = ("ref", m.name, ())
    structs = 0
    for name in closure_models(spec, root):
        c = spec.model(name)
        if c.generics:
            return False
        if c.is_record:
            structs += 1
            for f in c.fields:
                for w in walk(f.ty):
                    if w[0] in ("opt", "tup", "map"):
                        return False
                    if w[0] == "packed" and not 1 <= w[1] <= 32:
                        return False
        else:
            if any(x.id > 255 for x in c.variants):
                return False
    return structs <= 8  # the generic path holds 8 struct types per message
