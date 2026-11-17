"""Random model specs, and what the specification says about them.

A spec is a list of `Model`s. Everything the tests expect is computed here from docs/formats.md
(through tests/vectors/reference.py) and the EIP-712 specification (tests/vectors/eip712.py).
Nothing in this module reads the generator's output.

Types are tuples:

    ('int', signed, bits)  ('bool',)  ('addr',)
    ('text', n)  ('buf', n)  ('packed', n)       n is an int, or a const parameter's name
    ('opt', t)  ('arr', t, n)                    n likewise
    ('ref', name, args)                          a local record or enum; args are types or ints
    ('param', name)                              a type parameter of the enclosing generic record
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
    if k == "ref":
        return ("ref", t[1], tuple(subst(a, env) if isinstance(a, tuple) else size(a) for a in t[2]))
    if k == "param":
        return env.get(t[1], t)
    return t


def walk(t):
    yield t
    if t[0] == "opt" or t[0] == "arr":
        yield from walk(t[1])
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


def is_copy(spec, t):
    k = t[0]
    if k == "opt":
        return is_copy(spec, t[1])
    if k == "arr":
        return is_copy(spec, t[1])
    if k == "ref":
        m = spec.model(t[1])
        return m.copy_impl and not m.generics
    return True  # scalars and, by the instantiation rule, parameters


def uses_array(t):
    """Whether a parameter appears under an array in `t`."""
    k = t[0]
    if k == "arr":
        return any(w[0] == "param" for w in walk(t[1]))
    if k == "opt":
        return uses_array(t[1])
    return False


def type_param_needs_copy(m, name):
    for f in m.fields:
        t = f.ty
        for w in walk(t):
            if w[0] == "arr" and any(x == ("param", name) for x in walk(w[1])):
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
    if k == "ref":
        m = spec.model(t[1])
        if not m.is_record:
            return rng.choice(m.variants).name
        return {name: gen_value(spec, rng, ft) for name, ft, _ in instance_fields(spec, t)}
    raise ValueError(t)


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
    if k == "arr":
        return [to_js(spec, t[1], x) for x in v]
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
    if k == "arr":
        return [to_cbor(spec, t[1], x) for x in v]
    m = spec.model(t[1])
    if not m.is_record:
        return next(x.id for x in m.variants if x.name == v)
    return {fid: to_cbor(spec, ft, v[name]) for name, ft, fid in instance_fields(spec, t)}


def json_bytes(spec, t, v):
    return reference.js(to_js(spec, t, v)).encode()


def cbor_bytes(spec, t, v):
    return reference.cbor(to_cbor(spec, t, v))


# JSON a decoder must also accept: members in any order, whitespace between tokens, `null` for an
# absent optional field, \u escapes and either case of hex digits.

WS = [" ", "\t", "\n", "\r", "  ", " \n\t"]


def noisy_json(spec, rng, t, v, drop=None, counter=None):
    """Compact-JSON-equivalent text of `v`. `drop` is (occurrence, field name): that field of that
    record occurrence (numbered in visiting order) is left out."""
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
        if k == "arr":
            items = [ws() + go(t[1], x) + ws() for x in v]
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
        rng.shuffle(members)
        return "{" + ws() + ",".join(members) + ws() + "}"

    return ws() + go(t, v) + ws()


def record_occurrences(spec, t, v, out=None):
    """[(occurrence, record type, value)] for every record in `v`, in visiting order."""
    out = [] if out is None else out
    k = t[0]
    if k == "opt":
        if v is not None:
            record_occurrences(spec, t[1], v, out)
    elif k == "arr":
        for x in v:
            record_occurrences(spec, t[1], x, out)
    elif k == "ref" and spec.model(t[1]).is_record:
        out.append((len(out), t, v))
        for name, ft, _ in instance_fields(spec, t):
            record_occurrences(spec, ft, v[name], out)
    return out


def dropped_value(spec, t, v, occurrence, name):
    """`v` with field `name` of the record occurrence set to None (an optional field)."""
    count = [0]

    def go(t, v):
        k = t[0]
        if k == "opt":
            return None if v is None else go(t[1], v)
        if k == "arr":
            return [go(t[1], x) for x in v]
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
    m = spec.model(t[1])
    return t[1] if m.is_record else "uint8"


def to_eip(spec, t, v):
    k = t[0]
    if k == "arr":
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
                    if w[0] == "opt":
                        return False
                    if w[0] == "packed" and not 1 <= w[1] <= 32:
                        return False
        else:
            if any(x.id > 255 for x in c.variants):
                return False
    return structs <= 8  # the generic path holds 8 struct types per message
