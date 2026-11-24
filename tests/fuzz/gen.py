"""Random model specs (see spec.py for the type language)."""

import random

from .spec import (ADDR, BOOL, Field, Int, Model, Spec, Variant, depth, eligible_for_eip712,
                   instances, is_copy, type_param_needs_copy, weight)

# The most type nodes a non-generic record may expand to. Records without tuples stay under about
# 170 (see `weight`). Tuples of up to 8 elements, each holding records that hold tuples, multiply
# that quickly, and the generated JSON decoder of such a model overflows the EVM stack on sonatina
# at a size where the same shape built from records and arrays overflows too (a record of 6
# records of 6 records of 4 integers does). That limit is not specific to tuples, so the generator
# stays inside what it already produces for records, and still exercises every tuple feature.
MAX_RECORD_WEIGHT = 200

# The most type nodes the payloads of one enum may expand to, and the most containers deep a model
# a field or payload may name can nest (the default limit is 32, and a variant with a payload is
# one container more than the payload).
MAX_ENUM_WEIGHT = 60
MAX_REFERENCED_DEPTH = 8

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
    def __init__(self, rng, seed=0):
        self.rng = rng
        self.erng = random.Random("enums/%d" % seed)  # the payloads of enums, apart from the rest
        self.seed = seed
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
            if depth(self.spec, ("ref", m.name, ())) > MAX_REFERENCED_DEPTH:
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
                # EIP-712 has no maps either.
                choices.append(("map", 2.2))
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
        if kind == "map":
            return self.map(depth, top, plain, params, consts, avail_generics)
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

    def map_key(self):
        """The key of a `Map`: an unsigned integer of any width, `Text<N>`, `Address` or
        `PackedBytes<N>`, always concrete."""
        rng = self.rng
        r = rng.random()
        if r < 0.50:
            return Int(False, rng.choice([8, 16, 32, 64, 128, 256]))
        if r < 0.70:
            return ("text", rng.choice([1, 2, 3, 5, 8, rng.randint(1, 32)]))
        if r < 0.82:
            return ADDR
        return ("packed", rng.choice([1, 2, 3, 4, 8, 20, 31, 32, rng.randint(1, 32)]))

    def map(self, depth, top, plain, params, consts, avail_generics):
        """`Map<K, V, N>`. Its values are always `Copy`, since a map stores copies of them. A
        capacity of 0 is a field's whole type only, as for an empty array and list."""
        rng = self.rng
        if consts and rng.random() < 0.35:
            length = rng.choice(consts)
        else:
            length = rng.choice([0, 0, 1, 2, 3, 4]) if top and rng.random() < 0.35 else rng.choice([1, 2, 3, 4])
        if not plain and depth < 1 and rng.random() < 0.2:
            value = self.tuple(depth + 1, True, plain, params, consts, avail_generics)
        else:
            value = self.gen_type(depth + 1, False, True, plain, params, consts, avail_generics)
        return ("map", self.map_key(), value, length)

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

    def to_payload(self, m):
        """Gives the variants of the unit-only enum `m` tuple and record payloads (all drawn from a
        stream of their own, so the models around it come out as they would without payloads): each
        becomes a unit, tuple or record variant, with at least one that is not unit. Variant IDs stay;
        field IDs of a record variant are explicit in some. The payloads are `Copy` if `m` is."""
        saved, self.rng = self.rng, self.erng
        try:
            rng = self.rng
            pool = rng.choice([("unit", "tuple", "record")] * 3 + [("tuple",), ("record",), ("unit", "tuple"),
                                                                  ("unit", "record"), ("tuple", "record")])
            kinds = [rng.choice(pool) for _ in m.variants]
            if all(k == "unit" for k in kinds):
                kinds[rng.randrange(len(kinds))] = rng.choice([k for k in pool if k != "unit"])
            generics = [x for x in self.spec.models if x.generics]
            for v, kind in zip(m.variants, kinds):
                v.kind = kind
                if kind == "tuple":
                    v.elems = [self.gen_type(0, False, m.copy_impl, False, [], [], generics)
                               for _ in range(rng.choice([1, 1, 2, 3, 4]))]
                elif kind == "record":
                    count = rng.choice([0, 1, 2, 2, 3, 4]) if rng.random() < 0.9 else 0
                    v.explicit_fields = count > 0 and rng.random() < 0.4
                    fids = self.make_ids(count, 1, None) if v.explicit_fields else list(range(1, count + 1))
                    for fname, fid in zip(rng.sample(FIELD_WORDS, count), fids):
                        v.fields.append(Field(long_name(rng, fname),
                                              self.gen_type(0, True, m.copy_impl, False, [], [], generics),
                                              fid, "pub", self.id_style()))
            # The payloads stay within what the generated decoders of a record of that weight manage.
            slots = []
            for v in m.variants:
                slots.extend((v.elems, i) for i in range(len(v.elems)))
                slots.extend((f, None) for f in v.fields)

            def slot_type(slot):
                holder, i = slot
                return holder[i] if i is not None else holder.ty

            while sum(weight(self.spec, slot_type(sl)) for sl in slots) > MAX_ENUM_WEIGHT:
                holder, i = max(slots, key=lambda sl: weight(self.spec, slot_type(sl)))
                if i is not None:
                    holder[i] = self.scalar()
                else:
                    holder.ty = self.scalar()
            if m.copy_impl and not all(is_copy(self.spec, t) for t in m.payload_types()):
                raise AssertionError("a Copy enum was given a payload that is not")
        finally:
            self.rng = saved

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
                   ("list", ("opt", pt), rng.randint(1, 3)), ("map", self.map_key(), pt, rng.randint(1, 3)),
                   ("opt", ("map", self.map_key(), pt, rng.randint(1, 3))),
                   ("map", self.map_key(), ("opt", pt), rng.randint(1, 3))]
        if consts:
            n = rng.choice(consts)
            options += [("arr", pt, n), ("opt", ("arr", pt, n)), ("arr", ("opt", pt), n), ("list", pt, n),
                        ("list", ("arr", pt, 2), n), ("map", self.map_key(), pt, n)]
        return rng.choice(options)

    def const_use(self, c, params):
        rng = self.rng
        options = [("text", c), ("buf", c), ("packed", c), ("arr", Int(False, rng.choice([8, 64])), c),
                   ("arr", BOOL, c), ("list", Int(False, rng.choice([8, 64])), c), ("list", ADDR, c),
                   ("map", self.map_key(), Int(False, rng.choice([8, 64])), c), ("map", self.map_key(), BOOL, c)]
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
                if not plain and self.erng.random() < 0.7:
                    self.to_payload(m)
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
        slot = Model("Slot%d" % len(spec.models), "record", rng.randrange(spec.modules))
        slot.fields = [Field("id", Int(False, 8), 1, "pub"), Field("tag", ("opt", ("text", 4)), 2, "pub")]
        slot.copy_impl = True
        spec.models.append(slot)
        for m in spec.models:
            if m.is_record and not m.generics and m.plain and eligible_for_eip712(spec, m):
                m.eip712 = self.rng.random() < 0.75
        for m in spec.models:
            if m.is_record:
                m.skip_unknown = self.rng.random() < 0.4
        for m in spec.models:
            if not m.is_record and any(v.kind == "record" for v in m.variants):
                m.skip_unknown = self.erng.random() < 0.4
        slot.skip_unknown = True
        self.map_models(slot)
        self.payload_models()
        return spec

    def map_models(self, slot):
        """Rare map features come from random types too rarely to count on, so every spec also
        gets records of maps, taken from `MAP_MENU` in turn: `MAP_PER_SPEC` entries a spec, starting
        where the previous seed stopped, so every entry is used by any run of 4 or more seeds in a row
        of the default. `slot` is a record that is `Copy`, has a required and an optional field,
        and skips unknown fields; the menu's record entry holds maps of it."""
        rng, spec = self.rng, self.spec
        first = (self.seed * MAP_PER_SPEC) % len(MAP_MENU)
        fields = [MAP_MENU[(first + i) % len(MAP_MENU)] for i in range(MAP_PER_SPEC)]
        user = Model("Mapped%d" % len(spec.models), "record", rng.randrange(spec.modules))
        names = rng.sample(FIELD_WORDS, len(fields))
        for i, (name, make) in enumerate(zip(names, fields)):
            user.fields.append(Field(name, make(slot.name), i + 1, "pub"))
        if self.seed % 3 == 0:
            # A generic record whose map has a parameter for its values and a const capacity.
            table = Model("Table%d" % len(spec.models), "record", rng.randrange(spec.modules))
            table.generics = [(False, "T", False), (True, "N", False)]
            table.fields = [Field("rows", ("map", Int(False, 8), ("param", "T"), "N"), 1, "pub")]
            spec.models.append(table)
            user.fields.append(Field("table", ("ref", table.name, (Int(False, 16), rng.randint(1, 3))),
                                     len(user.fields) + 1, "pub"))
        spec.models.append(user)


    def payload_models(self):
        """Rare enum features come from random types too rarely to count on, so every spec also gets
        enums of every kind of variant (`Anchor`, which is `Copy`; `Solo`, with variants of one
        kind; `Wrap`, which holds `Anchor`s in each place a payload can be) and a record holding
        `PAYLOAD_PER_SPEC` places a payload enum can be, taken from `PAYLOAD_MENU` in turn, as for
        maps. What varies with the seed: explicit variant IDs, explicit field IDs on a record variant,
        and whether the enum skips unknown fields."""
        rng, spec, seed = self.rng, self.spec, self.seed
        n = len(spec.models)

        def variant(name, kind, elems=(), fields=()):
            return Variant(name, 0, "dec", kind, elems, [Field(f, t, 0, "pub") for f, t in fields])

        def finish(m, ids, skip):
            """Sets the IDs of the variants, and of the fields of each record variant."""
            m.copy_impl = True
            m.skip_unknown = skip
            m.explicit = ids
            order = self.make_ids(len(m.variants), 0, None) if ids else list(range(len(m.variants)))
            style = self.id_style()
            for v, vid in zip(m.variants, order):
                v.id = vid
                v.id_style = style if rng.random() < 0.6 else self.id_style()
                v.explicit_fields = v.kind == "record" and len(v.fields) > 1 and (seed + vid) % 3 == 0
                fids = (self.make_ids(len(v.fields), 1, None) if v.explicit_fields
                        else list(range(1, len(v.fields) + 1)))
                for f, fid in zip(v.fields, fids):
                    f.id = fid
                    f.id_style = self.id_style()
            return m

        names = rng.sample(VARIANT_WORDS, 12)
        anchor = Model("Anchor%d" % n, "enum", rng.randrange(spec.modules))
        anchor.variants = [variant(names[0], "unit"), variant(names[1], "tuple", [Int(False, 16)]),
                           variant(names[2], "tuple", [Int(False, 8), ("text", 4)]),
                           variant(names[3], "record", fields=[("alpha", Int(False, 8)), ("beta", ("opt", ("text", 4)))])]
        finish(anchor, seed % 2 == 0, seed % 2 == 1)
        solo = Model("Solo%d" % (n + 1), "enum", rng.randrange(spec.modules))
        if seed % 2 == 0:  # only tuple variants
            solo.variants = [variant(names[4], "tuple", [Int(True, 32)]),
                             variant(names[5], "tuple", [BOOL, ("opt", Int(False, 8))])]
        else:  # only record variants
            solo.variants = [variant(names[4], "record", fields=[("gamma", Int(False, 8))]),
                             variant(names[5], "record", fields=[("delta", ("opt", Int(False, 16))), ("rank", BOOL)]),
                             variant(names[6], "record")]
        finish(solo, seed % 3 == 1, seed % 4 == 1)
        a = ("ref", anchor.name, ())
        wrap = Model("Wrap%d" % (n + 2), "enum", rng.randrange(spec.modules))
        wrap.variants = [variant(names[7], "unit"), variant(names[8], "tuple", [a]),
                         variant(names[9], "tuple", [a, BOOL]),
                         variant(names[10], "record", fields=[("first", a), ("many", ("list", a, 2)),
                                                             ("maybe", ("opt", a))])]
        finish(wrap, seed % 4 >= 2, seed % 2 == 0)
        spec.models.extend([anchor, solo, wrap])
        house = Model("Housed%d" % (n + 3), "record", rng.randrange(spec.modules))
        first = (seed * PAYLOAD_PER_SPEC) % len(PAYLOAD_MENU)
        names = rng.sample(FIELD_WORDS, PAYLOAD_PER_SPEC)
        for i, name in enumerate(names):
            house.fields.append(Field(name, PAYLOAD_MENU[(first + i) % len(PAYLOAD_MENU)](anchor.name, wrap.name,
                                                                                         solo.name), i + 1, "pub"))
        spec.models.append(house)
        if seed % 3 == 0:
            # Nested arrays and an array of structs in an EIP-712 root, which random types reach too
            # rarely to count on.
            leaf = Model("Leaf%d" % (n + 4), "record", rng.randrange(spec.modules))
            leaf.plain = True
            leaf.fields = [Field("a", Int(False, 8), 1, "pub"), Field("b", BOOL, 2, "pub")]
            grid = Model("Grid%d" % (n + 5), "record", rng.randrange(spec.modules))
            grid.plain = True
            grid.fields = [Field("cells", ("arr", ("arr", Int(False, 8), 2), 2), 1, "pub"),
                           Field("leaves", ("arr", ("ref", leaf.name, ()), 2), 2, "pub")]
            grid.eip712 = True
            spec.models.extend([leaf, grid])


# Forms of map that reach rare cases, as functions of the name of a `Copy` record: a key of each
# kind, a capacity of 0, and maps inside other types.
def _int_key(bits):
    return lambda slot: ("map", Int(False, bits), Int(False, 8), 2)


MAP_MENU = (
    [_int_key(b) for b in (8, 16, 32, 64, 128, 256)]
    + [lambda slot: ("map", ("text", 5), BOOL, 2), lambda slot: ("map", ADDR, Int(False, 64), 2),
       lambda slot: ("map", ("packed", 3), Int(True, 16), 2),
       lambda slot: ("map", Int(False, 8), BOOL, 0),
       lambda slot: ("map", Int(False, 16), ("ref", slot, ()), 3),
       lambda slot: ("map", Int(False, 8), ("list", Int(False, 8), 2), 2),
       lambda slot: ("map", Int(False, 8), ("map", Int(False, 8), Int(False, 8), 2), 2),
       lambda slot: ("map", Int(False, 8), ("tup", (BOOL, Int(False, 8))), 2),
       lambda slot: ("map", Int(False, 8), ("arr", Int(False, 8), 2), 2),
       lambda slot: ("map", ADDR, ("opt", Int(False, 8)), 2),
       lambda slot: ("opt", ("map", Int(False, 16), BOOL, 2)),
       lambda slot: ("list", ("map", Int(False, 8), Int(False, 8), 2), 2),
       lambda slot: ("arr", ("map", Int(False, 8), Int(False, 8), 2), 2),
       lambda slot: ("tup", (("map", Int(False, 8), BOOL, 2), BOOL))]
)
MAP_PER_SPEC = 5


# Places a payload enum can be, as functions of the names of two `Copy` payload enums (`Anchor` and
# `Wrap`, which holds `Anchor`s) and of one that has variants of one kind.
def _enum(i):
    return lambda anchor, wrap, solo: ("ref", (anchor, wrap, solo)[i], ())


PAYLOAD_MENU = [
    _enum(0), _enum(1), _enum(2),
    lambda anchor, wrap, solo: ("opt", ("ref", anchor, ())),
    lambda anchor, wrap, solo: ("opt", ("ref", wrap, ())),
    lambda anchor, wrap, solo: ("arr", ("ref", anchor, ()), 2),
    lambda anchor, wrap, solo: ("arr", ("ref", wrap, ()), 2),
    lambda anchor, wrap, solo: ("list", ("ref", anchor, ()), 3),
    lambda anchor, wrap, solo: ("list", ("ref", solo, ()), 2),
    lambda anchor, wrap, solo: ("map", Int(False, 8), ("ref", anchor, ()), 2),
    lambda anchor, wrap, solo: ("map", ADDR, ("ref", wrap, ()), 2),
    lambda anchor, wrap, solo: ("tup", (("ref", anchor, ()), BOOL)),
    lambda anchor, wrap, solo: ("tup", (("ref", solo, ()),)),
    lambda anchor, wrap, solo: ("list", ("opt", ("ref", anchor, ())), 2),
    lambda anchor, wrap, solo: ("opt", ("list", ("ref", anchor, ()), 2)),
    lambda anchor, wrap, solo: ("arr", ("tup", (("ref", solo, ()), Int(False, 8))), 2),
]
PAYLOAD_PER_SPEC = 5


def make_spec(rng, seed=0):
    return Gen(rng, seed).run()
