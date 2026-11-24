"""Random model specs (see spec.py for the type language)."""

from .spec import (ADDR, BOOL, Field, Int, Model, Spec, Variant, eligible_for_eip712,
                   instances, is_copy, type_param_needs_copy, weight)

# The most type nodes a non-generic record may expand to. Records without tuples stay under about
# 170 (see `weight`). Tuples of up to 8 elements, each holding records that hold tuples, multiply
# that quickly, and the generated JSON decoder of such a model overflows the EVM stack on sonatina
# at a size where the same shape built from records and arrays overflows too (a record of 6
# records of 6 records of 4 integers does). That limit is not specific to tuples, so the generator
# stays inside what it already produces for records, and still exercises every tuple feature.
MAX_RECORD_WEIGHT = 200

RECORD_WORDS = ["Account", "Order", "Ledger", "Packet", "Header", "Entry", "Quote", "Trade", "Vault",
                "Claim", "Ticket", "Config", "Window", "Record", "Slot", "Frame", "Token", "Event"]
ENUM_WORDS = ["Status", "Level", "Kind", "Phase", "Mode", "Side", "Color", "Tier"]
GENERIC_WORDS = ["Pair", "Holder", "Page", "Wrap", "Cell", "Bag"]
FIELD_WORDS = ["alpha", "beta", "gamma", "delta", "amount", "owner", "memo", "nonce", "flag", "items",
               "value", "label", "count", "hash", "data", "kind", "level", "score", "tag", "note",
               "price", "width", "depth", "hops", "slots", "from_", "to", "key", "size", "rank"]
VARIANT_WORDS = ["Open", "Closed", "Paid", "Void", "Low", "High", "Red", "Green", "Blue", "Idle", "Busy",
                 "Done", "Left", "Right", "Early", "Late"]


def long_name(rng, base):
    """Sometimes a name of exactly the longest allowed length, 31 bytes."""
    if rng.random() < 0.08:
        return base + "x" * (31 - len(base))
    return base


class Gen:
    def __init__(self, rng):
        self.rng = rng
        self.spec = Spec()

    # -- scalars

    def scalar(self):
        rng = self.rng
        r = rng.random()
        if r < 0.08:
            return BOOL
        if r < 0.50:
            return Int(rng.random() < 0.45, rng.choice([8, 16, 32, 64, 128, 256]))
        if r < 0.66:
            return ("text", rng.choice([1, 2, 3, 5, 8, 12, rng.randint(1, 32)]))
        if r < 0.78:
            return ("buf", rng.choice([1, 2, 4, 8, rng.randint(1, 16)]))
        if r < 0.92:
            return ("packed", rng.choice([1, 2, 3, 4, 8, 20, 31, 32, rng.randint(1, 32)]))
        return ADDR

    # -- compound types

    def usable_models(self, plain, copy_only):
        out = []
        for m in self.spec.models:
            if m.generics:
                continue
            if plain and not m.plain:
                continue
            if copy_only and not m.copy_impl:
                continue
            out.append(m)
        return out

    def gen_type(self, depth, top, force_copy, plain, params, consts, avail_generics):
        rng = self.rng
        choices = [("scalar", 6.0)]
        if self.usable_models(plain, force_copy):
            choices.append(("ref", 3.0))
        if not plain and not force_copy and avail_generics:
            choices.append(("inst", 3.0))
        if depth < 2:
            choices.append(("arr", 2.6))
            choices.append(("list", 2.2))
            if not plain:
                choices.append(("opt", 2.0))
                # EIP-712 has no tuples, so the types a root may use have none.
                choices.append(("tup", 2.4))
        if params:
            choices.append(("param", 2.5))
        total = sum(w for _, w in choices)
        pick = rng.random() * total
        kind = choices[-1][0]
        for name, w in choices:
            if pick < w:
                kind = name
                break
            pick -= w
        if kind == "scalar":
            return self.scalar()
        if kind == "ref":
            return ("ref", rng.choice(self.usable_models(plain, force_copy)).name, ())
        if kind == "inst":
            inst = self.instance(avail_generics, force_copy=False)
            if inst is not None:
                return inst
            return self.scalar()
        if kind == "param":
            return ("param", rng.choice(params))
        if kind == "tup":
            return self.tuple(depth, force_copy, plain, params, consts, avail_generics)
        if kind == "list":
            return self.list(depth, top, plain, params, consts, avail_generics)
        if kind == "opt":
            inner = self.gen_type(depth + 1, False, force_copy, plain, params, consts, avail_generics)
            if inner[0] == "opt":
                return inner
            if inner[0] == "arr" and (inner[2] == 0):
                return ("opt", ("arr", inner[1], 1))
            return ("opt", inner)
        # array
        literal = rng.choice([0, 1, 2, 3]) if top and not force_copy else rng.choice([1, 2, 3])
        if consts and rng.random() < 0.35:
            length = rng.choice(consts)
        else:
            length = literal
        noncopy_ok = top and not force_copy and isinstance(length, int)
        if not plain and depth < 1 and rng.random() < 0.2:
            elem = self.tuple(depth + 1, not noncopy_ok, plain, params, consts, avail_generics)
        else:
            elem = self.gen_type(depth + 1, False, not noncopy_ok, plain, params, consts, avail_generics)
        return ("arr", elem, length)

    def list(self, depth, top, plain, params, consts, avail_generics):
        """`List<T, N>`. Its elements are always `Copy`, since a list stores copies of them. A
        capacity of 0 is a field's whole type only, as an empty array is."""
        rng = self.rng
        if consts and rng.random() < 0.35:
            length = rng.choice(consts)
        else:
            length = rng.choice([0, 1, 2, 3, 4]) if top and rng.random() < 0.12 else rng.choice([1, 2, 3, 4])
        if not plain and depth < 1 and rng.random() < 0.2:
            elem = self.tuple(depth + 1, True, plain, params, consts, avail_generics)
        else:
            elem = self.gen_type(depth + 1, False, True, plain, params, consts, avail_generics)
        return ("list", elem, length)

    def tuple(self, depth, force_copy, plain, params, consts, avail_generics):
        """A tuple of 1 to 8 elements. Core decodes the elements one by one, so none needs `Copy`
        unless the tuple is itself an array element that does (`force_copy`). The elements are not
        top-level, which keeps an empty array, rejected inside a tuple, out of them."""
        count = self.rng.choice([1, 2, 2, 3, 3, 4, 5, 6, 7, 8])
        return ("tup", tuple(self.gen_type(depth + 1, False, force_copy, plain, params, consts, avail_generics)
                             for _ in range(count)))

    def instance(self, generics, force_copy):
        rng = self.rng
        if not generics:
            return None
        g = rng.choice(generics)
        args = []
        for is_const, name, _ in g.generics:
            if is_const:
                args.append(rng.randint(1, 4))
            else:
                need = type_param_needs_copy(g, name)
                args.append(self.type_arg(need))
        return ("ref", g.name, tuple(args))

    def type_arg(self, need_copy):
        rng = self.rng
        r = rng.random()
        if r < 0.55:
            return self.scalar()
        enums_and_records = self.usable_models(False, need_copy)
        if r < 0.90 and enums_and_records:
            return ("ref", rng.choice(enums_and_records).name, ())
        if not need_copy and r < 0.95:
            inst = self.instance([m for m in self.spec.models if m.generics], False)
            if inst is not None:
                return inst
        return self.scalar()

    # -- ids

    def id_style(self):
        r = self.rng.random()
        return "dec" if r < 0.45 else "hex" if r < 0.7 else "under"

    def make_ids(self, count, first, cap):
        """`count` unique IDs, sequential from `first` or sparse, in a random order. `cap` is the
        largest ID allowed, or None."""
        rng = self.rng
        if rng.random() < 0.3:
            ids = [first + i for i in range(count)]
        else:
            hi = cap if cap is not None else first + rng.choice([20, 300, 100000])
            hi = max(hi, first + count)
            ids = rng.sample(range(first, hi + 1), count)
            if cap is None and rng.random() < 0.12:
                ids[0] = rng.choice([1 << 32, (1 << 63) + 5, (1 << 64) - 1])
        rng.shuffle(ids)
        return ids

    # -- models

    def enum(self, name, module, plain):
        rng = self.rng
        m = Model(name, "enum", module)
        m.plain = plain
        m.vis = rng.choice(["pub", "pub", "pub(ingot)"])
        n = rng.randint(1, 5)
        names = rng.sample(VARIANT_WORDS, n)
        if rng.random() < 0.45:
            m.explicit = True
            style = self.id_style()
            ids = self.make_ids(n, 0, 255 if plain else None)
            m.variants = [Variant(long_name(rng, v), i, style if rng.random() < 0.6 else self.id_style())
                          for v, i in zip(names, ids)]
        else:
            m.variants = [Variant(long_name(rng, v), i) for i, v in enumerate(names)]
        m.copy_impl = rng.random() < 0.4
        m.copy_path = rng.choice(["Copy", "Copy", "core::marker::Copy"])
        return m

    def record(self, name, module, plain, generic):
        rng = self.rng
        m = Model(name, "record", module)
        m.plain = plain
        m.vis = rng.choice(["pub", "pub", "pub", "pub(ingot)"])
        params, consts = [], []
        if generic:
            tcount = rng.choice([0, 1, 1, 2])
            ccount = rng.choice([0, 1, 1])
            if tcount + ccount == 0:
                tcount = 1
            for tn in ["T", "U"][:tcount]:
                m.generics.append((False, tn, rng.random() < 0.3))
                params.append(tn)
            for cn in ["N", "M"][:ccount]:
                m.generics.append((True, cn, False))
                consts.append(cn)
        avail_generics = [x for x in self.spec.models if x.generics] if not plain else []
        types = []
        # Every parameter is used at least once.
        for p in params:
            types.append(self.param_use(p, consts))
        for c in consts:
            types.append(self.const_use(c, params))
        count = rng.choice([1, 2, 3, 3, 4, 5, 6]) if not generic else rng.randint(0, 3)
        if not generic and rng.random() < 0.03 and not plain:
            count = 0
        for _ in range(count):
            types.append(self.gen_type(0, True, False, plain, params if generic else [], consts if generic else [],
                                       avail_generics))
        if not generic:
            while sum(weight(self.spec, t) for t in types) > MAX_RECORD_WEIGHT:
                heaviest = max(range(len(types)), key=lambda i: weight(self.spec, types[i]))
                types[heaviest] = self.scalar()
        rng.shuffle(types)
        names = rng.sample(FIELD_WORDS, len(types)) if len(types) <= len(FIELD_WORDS) else None
        explicit = bool(types) and rng.random() < 0.45
        m.explicit = explicit
        ids = self.make_ids(len(types), 1, None) if explicit else list(range(1, len(types) + 1))
        for i, (t, fid) in enumerate(zip(types, ids)):
            fname = long_name(rng, names[i])
            f = Field(fname, t, fid, rng.choice(["pub", "pub", "pub(ingot)"]), self.id_style())
            m.fields.append(f)
        if not generic:
            m.copy_impl = (rng.random() < 0.5 and all(is_copy(self.spec, f.ty) for f in m.fields)
                           and not m.generics)
            m.copy_path = rng.choice(["Copy", "Copy", "core::marker::Copy"])
        return m

    def param_use(self, p, consts):
        rng = self.rng
        pt = ("param", p)
        options = [pt, ("opt", pt), ("arr", pt, rng.randint(1, 3)), ("arr", ("opt", pt), rng.randint(1, 3)),
                   ("opt", ("arr", pt, rng.randint(1, 3))), ("arr", ("arr", pt, rng.randint(1, 2)), rng.randint(1, 3)),
                   ("list", pt, rng.randint(1, 3)), ("opt", ("list", pt, rng.randint(1, 3))),
                   ("list", ("opt", pt), rng.randint(1, 3))]
        if consts:
            n = rng.choice(consts)
            options += [("arr", pt, n), ("opt", ("arr", pt, n)), ("arr", ("opt", pt), n), ("list", pt, n),
                        ("list", ("arr", pt, 2), n)]
        return rng.choice(options)

    def const_use(self, c, params):
        rng = self.rng
        options = [("text", c), ("buf", c), ("packed", c), ("arr", Int(False, rng.choice([8, 64])), c),
                   ("arr", BOOL, c), ("list", Int(False, rng.choice([8, 64])), c), ("list", ADDR, c)]
        return rng.choice(options)

    def run(self):
        rng = self.rng
        spec = self.spec
        spec.modules = rng.choice([1, 1, 2, 2, 3])
        count = rng.randint(5, 11)
        used = set()
        for i in range(count):
            module = rng.randrange(spec.modules)
            r = rng.random()
            plain = rng.random() < 0.45
            if r < 0.20:
                base = rng.choice(ENUM_WORDS)
                kind = "enum"
            elif r < 0.38:
                plain = False
                base = rng.choice(GENERIC_WORDS)
                kind = "generic"
            else:
                base = rng.choice(RECORD_WORDS)
                kind = "record"
            name = "%s%d" % (base, i)
            if rng.random() < 0.08:
                name = name + "x" * (31 - len(name))
            if kind == "enum":
                m = self.enum(name, module, plain)
            elif kind == "generic":
                m = self.record(name, module, False, True)
            else:
                m = self.record(name, module, plain, False)
            spec.models.append(m)
        generics = [m for m in spec.models if m.generics]
        if generics and not instances(spec):
            # Every generic record is used at least once, as a field of a record of its own.
            user = Model("Use%d" % len(spec.models), "record", rng.randrange(spec.modules))
            for i, g in enumerate(rng.sample(generics, min(len(generics), 2))):
                inst = self.instance([g], False)
                user.fields.append(Field(["alpha", "beta"][i], inst, i + 1, "pub"))
            spec.models.append(user)
        # And some generic instantiated with a generic one, where a parameter allows it.
        for g in generics:
            free = [i for i, (is_const, name, _) in enumerate(g.generics)
                    if not is_const and not type_param_needs_copy(g, name)]
            if free and rng.random() < 0.6:
                inner = self.instance([rng.choice(generics)], False)
                args = list(self.instance([g], False)[2])
                args[free[0]] = inner
                user = Model("Nest%d" % len(spec.models), "record", rng.randrange(spec.modules))
                user.fields.append(Field("alpha", ("ref", g.name, tuple(args)), 1, "pub"))
                spec.models.append(user)
                break
        for m in spec.models:
            if m.is_record and not m.generics and m.plain and eligible_for_eip712(spec, m):
                m.eip712 = self.rng.random() < 0.75
        for m in spec.models:
            if m.is_record:
                m.skip_unknown = self.rng.random() < 0.4
        return spec


def make_spec(rng):
    return Gen(rng).run()
