"""Which features a spec exercises, counted from the spec itself."""

import collections

from .spec import instances, walk

INT_TYPES = ["%s%d" % (p, b) for p in "ui" for b in (8, 16, 32, 64, 128, 256)]

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
       "ids: explicit decimal", "ids: explicit hex", "ids: explicit with underscores", "ids: on enum variants",
       "ids: sparse", "ids: out of order", "ids: wider than 32 bits",
       "eip712: root", "eip712: struct reached from a root", "eip712: array of structs", "eip712: nested arrays",
       "source: several modules", "source: CRLF", "source: reference across modules", "source: pub(ingot) type",
       "source: pub(ingot) field", "source: impl Copy", "source: 31-byte name", "source: empty record",
       "comment between items", "doc comment on an item", "comment between fields", "doc comment on a field",
       "blank line between fields", "several attributes on one item", "one-line struct",
       "unrelated item: fn", "unrelated item: trait", "unrelated item: impl block", "unrelated item: const",
       "test: missing required field", "test: dropped optional field", "metamorphic: field permutation",
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
        if e[0] == "ref":
            em = spec.model(e[1])
            if em.is_record:
                c["type: array of local record"] += 1
                if em.copy_impl and not em.generics:
                    c["array: Copy local element"] += 1
        if top and isinstance(t[2], int) and unrolled(spec, e):
            c["array: unrolled (not Copy)"] += 1
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
    if k in ("opt", "arr"):
        return known_copy(spec, t[1])
    if k == "ref":
        m = spec.model(t[1])
        return m.copy_impl and not m.generics
    return False
