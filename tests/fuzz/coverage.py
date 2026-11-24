"""Which features a spec exercises, counted from the spec itself."""

import collections

from .spec import instances, walk

INT_TYPES = ["%s%d" % (p, b) for p in "ui" for b in (8, 16, 32, 64, 128, 256)]

MAP_KEYS = ["u%d" % b for b in (8, 16, 32, 64, 128, 256)] + ["Text<N>", "Address", "PackedBytes<N>"]

# The features every run with the default seeds must exercise, in report order.
REQUIRED = (
    ["type: bool"] + ["type: " + n for n in INT_TYPES]
    + ["type: Text<N>", "type: BoundedBuffer<N>", "type: PackedBytes<N>", "type: Address", "type: Option<T>",
       "type: [T; N]", "type: local record", "type: local enum", "type: array of local record",
       "array: unrolled (not Copy)", "array: Copy local element", "array: of a generic parameter",
       "array: Option of array", "array: of Option", "array: nested", "array: empty",
       "generic: type parameter", "generic: const parameter", "generic: Copy bound", "generic: instantiation",
       "generic: instantiated with a local record", "generic: instantiated with a generic",
       "generic: Option of a parameter", "generic: array of Option of a parameter",
       "type: tuple", "tuple: one element", "tuple: eight elements", "tuple: nested", "tuple: array of tuples",
       "tuple: Option of tuple", "tuple: of a generic parameter", "tuple: with a local record",
       "tuple: parenthesized single type", "test: tuples have no EIP-712 form",
       "type: List<T, N>", "list: capacity zero", "list: of local record", "list: of tuples", "list: nested",
       "list: Option of list", "list: of Option", "list: array of lists", "list: of arrays",
       "list: of a generic parameter", "list: const capacity", "eip712: list",
       "type: Map<K, V, N>"] + ["map: key " + n for n in MAP_KEYS]
    + ["map: capacity zero", "map: const capacity", "map: of local record", "map: of tuples", "map: of lists",
       "map: of arrays", "map: of Option", "map: nested", "map: Option of map", "map: list of maps",
       "map: array of maps", "map: tuple holding a map", "map: of a generic parameter",
       "map: entries inserted out of key order", "map: empty value", "map: full to capacity",
       "map: of a record that skips unknown fields", "map: field left out inside a map value",
       "test: compact rejects maps", "test: maps have no EIP-712 form",
       "type: enum with payloads", "enum: unit variant beside payload variants", "enum: tuple variant of one element",
       "enum: tuple variant of several elements", "enum: record variant", "enum: record variant without fields",
       "enum: record variant with an optional field", "enum: only tuple variants", "enum: only record variants",
       "enum: payload enum impl Copy", "enum: payload enum as a struct field", "enum: payload enum in Option",
       "enum: payload enum in an array", "enum: payload enum in a list", "enum: payload enum as a map value",
       "enum: payload enum in a tuple", "enum: payload enum inside a variant's payload",
       "enum: optional field left out of a record variant", "ids: on variants of an enum with payloads",
       "ids: on the fields of a record variant", "skip: marked enum", "skip: unknown JSON members in a record variant",
       "skip: unknown CBOR fields in a record variant", "test: required field missing from a record variant",
       "test: optional field dropped from a record variant", "test: payload variant written bare is rejected",
       "test: unit variant written as a wrapper is rejected", "test: wrapper of two entries is rejected",
       "test: wrapper of no entry is rejected", "test: compact rejects payload variants",
       "test: compact cannot read payload enums", "test: payload enums have no EIP-712 form",
       "ids: explicit decimal", "ids: explicit hex", "ids: explicit with underscores", "ids: on enum variants",
       "ids: sparse", "ids: out of order", "ids: wider than 32 bits",
       "eip712: root", "eip712: struct reached from a root", "eip712: array of structs", "eip712: nested arrays",
       "source: several modules", "source: CRLF", "source: reference across modules", "source: pub(ingot) type",
       "source: pub(ingot) field", "source: impl Copy", "source: 31-byte name", "source: empty record",
       "comment between items", "doc comment on an item", "comment between fields", "doc comment on a field",
       "blank line between fields", "several attributes on one item", "one-line struct",
       "unrelated item: fn", "unrelated item: trait", "unrelated item: impl block", "unrelated item: const",
       "test: missing required field", "test: dropped optional field", "metamorphic: field permutation",
       "skip: marked record", "skip: unknown JSON members", "skip: unknown CBOR fields",
       "metamorphic: newline variant accepted",
       "metamorphic: newline variant rejected where fe check rejects it"]
)


def count(spec, renderer_stats, extra):
    c = collections.Counter(renderer_stats)
    c.update(extra)
    models = spec.models
    if spec.modules > 1:
        c["source: several modules"] += 1
    for m in models:
        if m.vis != "pub":
            c["source: pub(ingot) type"] += 1
        if m.copy_impl:
            c["source: impl Copy"] += 1
        if len(m.name) == 31:
            c["source: 31-byte name"] += 1
        c["type: local record" if m.is_record else "type: local enum"] += 1
        if m.is_record and not m.fields:
            c["source: empty record"] += 1
        if m.eip712:
            c["eip712: root"] += 1
        if m.skip_unknown:
            c["skip: marked record"] += 1
        if m.generics:
            c["generic: instantiation"] += 0
            for is_const, name, copy_bound in m.generics:
                c["generic: const parameter" if is_const else "generic: type parameter"] += 1
                if copy_bound:
                    c["generic: Copy bound"] += 1
        if not m.is_record:
            if m.explicit:
                c["ids: on enum variants"] += 1
                for v in m.variants:
                    c["ids: explicit " + _style(v.id_style)] += 1
            if m.has_payload:
                payload_enum(spec, m, c)
            continue
        if m.explicit:
            ids = [f.id for f in m.fields]
            if ids != sorted(ids):
                c["ids: out of order"] += 1
            if sorted(ids) != list(range(1, len(ids) + 1)):
                c["ids: sparse"] += 1
            if any(i >= 1 << 32 for i in ids):
                c["ids: wider than 32 bits"] += 1
            for f in m.fields:
                c["ids: explicit " + _style(f.id_style)] += 1
        for f in m.fields:
            if f.vis != "pub":
                c["source: pub(ingot) field"] += 1
            if is_payload_enum(spec, f.ty):
                c["enum: payload enum as a struct field"] += 1
            kinds(spec, m, f.ty, c, top=True)
            for w in walk(f.ty):
                if w[0] == "ref" and spec.model(w[1]).module != m.module:
                    c["source: reference across modules"] += 1
    if any(m.eip712 for m in models):
        pass
    for t in instances(spec):
        c["generic: instantiation"] += 1
        g = spec.model(t[1])
        for arg in t[2]:
            if isinstance(arg, tuple) and arg[0] == "ref":
                m = spec.model(arg[1])
                if m.generics:
                    c["generic: instantiated with a generic"] += 1
                elif m.is_record:
                    c["generic: instantiated with a local record"] += 1
    return c


def is_payload_enum(spec, t):
    return t[0] == "ref" and not spec.model(t[1]).is_record and spec.model(t[1]).has_payload


def payload_enum(spec, m, c):
    """What an enum that has variants with a payload exercises."""
    c["type: enum with payloads"] += 1
    shapes = {v.kind for v in m.variants}
    if "unit" in shapes:
        c["enum: unit variant beside payload variants"] += 1
    if shapes == {"tuple"}:
        c["enum: only tuple variants"] += 1
    if shapes == {"record"}:
        c["enum: only record variants"] += 1
    if m.copy_impl:
        c["enum: payload enum impl Copy"] += 1
    if m.explicit:
        c["ids: on variants of an enum with payloads"] += 1
    if m.skip_unknown and "record" in shapes:
        c["skip: marked enum"] += 1
    for v in m.variants:
        if v.kind == "tuple":
            c["enum: tuple variant of one element" if len(v.elems) == 1 else "enum: tuple variant of several elements"] += 1
        if v.kind == "record":
            c["enum: record variant"] += 1
            if not v.fields:
                c["enum: record variant without fields"] += 1
            if any(f.ty[0] == "opt" for f in v.fields):
                c["enum: record variant with an optional field"] += 1
            if v.explicit_fields:
                c["ids: on the fields of a record variant"] += 1
                for f in v.fields:
                    c["ids: explicit " + _style(f.id_style)] += 1
        for t in v.payload_types():
            if any(is_payload_enum(spec, w) for w in walk(t)):
                c["enum: payload enum inside a variant's payload"] += 1
            kinds(spec, m, t, c)
            for w in walk(t):
                if w[0] == "ref" and spec.model(w[1]).module != m.module:
                    c["source: reference across modules"] += 1


def _style(s):
    return {"dec": "decimal", "hex": "hex", "under": "with underscores"}[s]


def kinds(spec, model, t, c, top=False):
    k = t[0]
    if k == "bool":
        c["type: bool"] += 1
    elif k == "int":
        c["type: %s%d" % ("i" if t[1] else "u", t[2])] += 1
    elif k == "text":
        c["type: Text<N>"] += 1
    elif k == "buf":
        c["type: BoundedBuffer<N>"] += 1
    elif k == "packed":
        c["type: PackedBytes<N>"] += 1
    elif k == "addr":
        c["type: Address"] += 1
    elif k == "opt":
        c["type: Option<T>"] += 1
        if t[1][0] == "arr":
            c["array: Option of array"] += 1
        if t[1][0] == "param":
            c["generic: Option of a parameter"] += 1
        if t[1][0] == "tup":
            c["tuple: Option of tuple"] += 1
        if t[1][0] == "list":
            c["list: Option of list"] += 1
        if t[1][0] == "map":
            c["map: Option of map"] += 1
        if is_payload_enum(spec, t[1]):
            c["enum: payload enum in Option"] += 1
        kinds(spec, model, t[1], c)
    elif k == "arr":
        c["type: [T; N]"] += 1
        e = t[1]
        if t[2] == 0:
            c["array: empty"] += 1
        if e[0] == "arr":
            c["array: nested"] += 1
        if e[0] == "opt":
            c["array: of Option"] += 1
            if e[1][0] == "param":
                c["generic: array of Option of a parameter"] += 1
        if e[0] == "param":
            c["array: of a generic parameter"] += 1
        if e[0] == "tup":
            c["tuple: array of tuples"] += 1
        if e[0] == "list":
            c["list: array of lists"] += 1
        if e[0] == "map":
            c["map: array of maps"] += 1
        if is_payload_enum(spec, e):
            c["enum: payload enum in an array"] += 1
        if e[0] == "ref":
            em = spec.model(e[1])
            if em.is_record:
                c["type: array of local record"] += 1
                if em.copy_impl and not em.generics:
                    c["array: Copy local element"] += 1
        if top and isinstance(t[2], int) and unrolled(spec, e):
            c["array: unrolled (not Copy)"] += 1
        kinds(spec, model, e, c)
    elif k == "list":
        c["type: List<T, N>"] += 1
        e = t[1]
        if t[2] == 0:
            c["list: capacity zero"] += 1
        if isinstance(t[2], str):
            c["list: const capacity"] += 1
        if e[0] == "ref" and spec.model(e[1]).is_record:
            c["list: of local record"] += 1
        if e[0] == "tup":
            c["list: of tuples"] += 1
        if e[0] == "list":
            c["list: nested"] += 1
        if e[0] == "opt":
            c["list: of Option"] += 1
        if e[0] == "arr":
            c["list: of arrays"] += 1
        if e[0] == "param":
            c["list: of a generic parameter"] += 1
        if e[0] == "map":
            c["map: list of maps"] += 1
        if is_payload_enum(spec, e):
            c["enum: payload enum in a list"] += 1
        kinds(spec, model, e, c)
    elif k == "map":
        c["type: Map<K, V, N>"] += 1
        key, e = t[1], t[2]
        c["map: key " + (("u%d" % key[2]) if key[0] == "int" else {
            "text": "Text<N>", "addr": "Address", "packed": "PackedBytes<N>"}[key[0]])] += 1
        if t[3] == 0:
            c["map: capacity zero"] += 1
        if isinstance(t[3], str):
            c["map: const capacity"] += 1
        if e[0] == "ref" and spec.model(e[1]).is_record:
            c["map: of local record"] += 1
            if spec.model(e[1]).skip_unknown:
                c["map: of a record that skips unknown fields"] += 1
        if e[0] == "tup":
            c["map: of tuples"] += 1
        if e[0] == "list":
            c["map: of lists"] += 1
        if e[0] == "arr":
            c["map: of arrays"] += 1
        if e[0] == "opt":
            c["map: of Option"] += 1
        if e[0] == "map":
            c["map: nested"] += 1
        if e[0] == "param":
            c["map: of a generic parameter"] += 1
        if is_payload_enum(spec, e):
            c["enum: payload enum as a map value"] += 1
        kinds(spec, model, e, c)
    elif k == "tup":
        c["type: tuple"] += 1
        if len(t[1]) == 1:
            c["tuple: one element"] += 1
        if len(t[1]) == 8:
            c["tuple: eight elements"] += 1
        for e in t[1]:
            if is_payload_enum(spec, e):
                c["enum: payload enum in a tuple"] += 1
            if e[0] == "map":
                c["map: tuple holding a map"] += 1
            if e[0] == "tup":
                c["tuple: nested"] += 1
            if e[0] == "param":
                c["tuple: of a generic parameter"] += 1
            if e[0] == "ref" and spec.model(e[1]).is_record:
                c["tuple: with a local record"] += 1
            kinds(spec, model, e, c)
    elif k == "ref":
        for a in t[2]:
            if isinstance(a, tuple):
                kinds(spec, model, a, c)


def unrolled(spec, e):
    """Whether the generator decodes an array field of `e` element by element: its element is not a
    bare parameter and not known to be Copy."""
    if e[0] == "param":
        return False
    return not known_copy(spec, e)


def known_copy(spec, t):
    k = t[0]
    if k in ("int", "bool", "text", "buf", "packed", "addr"):
        return True
    if k in ("opt", "arr", "list"):
        return known_copy(spec, t[1])
    if k == "map":
        return known_copy(spec, t[2])
    if k == "tup":
        return all(known_copy(spec, e) for e in t[1])
    if k == "ref":
        m = spec.model(t[1])
        return m.copy_impl and not m.generics
    return False
