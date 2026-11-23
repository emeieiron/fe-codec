"""Fe source text for a spec, built from tokens and whitespace gaps so that a variant can replace
every gap, as the metamorphic checks need."""

import collections
import random

from .spec import fe_type, walk


class Gap:
    """Whitespace between two tokens. `base` is what the base source has there (possibly empty).
    kind: 'free' any nonempty mix; 'nl' must contain a newline; 'fattr' the gap between a field's
    attribute and the field: a newline, and no comment; 'h' spaces and tabs only, because the Fe
    compiler rejects a newline there; 'x' a place where the compiler accepts a newline or not
    depending on what follows, such as after a field's colon: only metamorphic variant (d) puts
    newlines there, and checks a rejection against `fe check`."""

    __slots__ = ("base", "kind")

    def __init__(self, base, kind="free"):
        self.base = base
        self.kind = kind


class Src:
    def __init__(self):
        self.parts = []

    def t(self, text):
        self.parts.append(text)

    def g(self, base, kind="free"):
        self.parts.append(Gap(base, kind))

    def extend(self, other):
        self.parts.extend(other.parts)

    def text(self, fill=None):
        out = []
        for p in self.parts:
            if isinstance(p, Gap):
                out.append(p.base if fill is None or p.base == "" else fill(p))
            else:
                out.append(p)
        return "".join(out)


def mix_fill(rng, newline_gaps=False):
    """A gap filler that picks a different nonempty mix of spaces, tabs and newlines, sometimes
    with a `// comment` before a newline."""

    def fill(gap):
        if gap.kind == "fattr":  # Fe takes empty lines here, but not lines of spaces
            return "\n" * rng.randint(1, 3) + rng.choice(["", " ", "\t", "  "])
        need_nl = gap.kind == "nl"
        horizontal = gap.kind == "h" or (gap.kind == "x" and not newline_gaps)
        out = ""
        for _ in range(rng.randint(1, 3)):
            out += rng.choice([" ", "\t", "  ", " \t "] if horizontal else [" ", "\t", "  ", "\n", "\n\n", " \t "])
        if gap.kind != "fattr" and not horizontal and rng.random() < 0.3:
            out += " // note %d\n" % rng.randrange(100)
            out += rng.choice(["", " ", "\t"])
        if need_nl and "\n" not in out:
            out += "\n"
        return out

    return fill


# ----------------------------------------------------------------------------------------


def id_text(value, style, rng):
    if style == "hex":
        return ("0x%X" if rng.random() < 0.5 else "0x%x") % value
    if style == "under":
        digits = str(value)
        if len(digits) > 1:
            cut = rng.randint(1, len(digits) - 1)
            return digits[:cut] + "_" + digits[cut:]
        return digits
    return str(value)


class Renderer:
    def __init__(self, spec, seed, perm=None):
        self.spec = spec
        self.seed = seed
        self.perm = perm or {}
        self.stats = collections.Counter()

    def module_names(self):
        return [chr(ord("a") + i) for i in range(self.spec.modules)]

    # -- noise helpers

    def item_gap(self, rng, doc_ok):
        """The gap before an item, ending at the start of its line."""
        s = "\n"
        if rng.random() < 0.7:
            s += "\n"
        if rng.random() < 0.15:
            s += "\n"
        if rng.random() < 0.25:
            s += "// %s\n" % rng.choice(["note", "section: models", "TODO { not code }", "x // y"])
            self.stats["comment between items"] += 1
        if doc_ok and rng.random() < 0.25:
            s += "/// %s\n" % rng.choice(["A model.", "Documented.", "Fields are `pub`.", "{braces} in a doc"])
            self.stats["doc comment on an item"] += 1
        return s

    def member_gap(self, rng, doc_ok, indent="    "):
        s = "\n"
        if rng.random() < 0.12:
            s += "\n"
            self.stats["blank line between fields"] += 1
        if rng.random() < 0.15:
            s += indent + "// %s\n" % rng.choice(["note", "a comment, with commas", "pub x: u8,"])
            self.stats["comment between fields"] += 1
        if doc_ok and rng.random() < 0.15:
            s += indent + "/// %s\n" % rng.choice(["The field.", "Documented"])
            self.stats["doc comment on a field"] += 1
        return s + indent

    def tight_or_space(self, rng, p=0.04):
        return " " if rng.random() < p else ""

    # -- types

    def emit_type(self, s, t, rng, style):
        k = t[0]
        if k in ("int", "bool", "addr", "param"):
            s.t(fe_type(t))
        elif k in ("text", "buf", "packed"):
            name = {"text": "Text", "buf": "BoundedBuffer", "packed": "PackedBytes"}[k]
            s.t(name)
            s.t("<")
            s.g(self.tight_or_space(rng, style["tspace"]))
            s.t(str(t[1]))
            s.g(self.tight_or_space(rng, style["tspace"]))
            s.t(">")
        elif k == "opt":
            s.t("Option")
            s.t("<")
            s.g(self.tight_or_space(rng, style["tspace"]))
            self.emit_type(s, t[1], rng, style)
            s.g(self.tight_or_space(rng, style["tspace"]))
            s.t(">")
        elif k == "tup":
            # `(T)` is the same one-tuple as `(T,)`, and a trailing comma is optional.
            s.t("(")
            s.g(self.tight_or_space(rng, style["tspace"]))
            for i, e in enumerate(t[1]):
                if i:
                    s.t(",")
                    s.g(" " if rng.random() < 0.85 else "")
                self.emit_type(s, e, rng, style)
                s.g(self.tight_or_space(rng, style["tspace"]))
            if len(t[1]) == 1:
                if rng.random() < 0.5:
                    s.t(",")
                else:
                    self.stats["tuple: parenthesized single type"] += 1
            elif rng.random() < 0.15:
                s.t(",")
                s.g(self.tight_or_space(rng, style["tspace"]))
            s.t(")")
        elif k == "arr":
            s.t("[")
            s.g(self.tight_or_space(rng, style["tspace"]), "x")
            self.emit_type(s, t[1], rng, style)
            s.g(self.tight_or_space(rng, style["tspace"]), "h")
            s.t(";")
            s.g(" " if rng.random() < 0.8 else "", "h")
            s.t(str(t[2]))
            s.g(self.tight_or_space(rng, style["tspace"]), "h")
            s.t("]")
        elif k == "ref":
            s.t(t[1])
            if t[2]:
                s.t("<")
                s.g(self.tight_or_space(rng, style["tspace"]))
                for i, a in enumerate(t[2]):
                    if i:
                        s.t(",")
                        s.g(" " if rng.random() < 0.85 else "")
                    if isinstance(a, tuple):
                        self.emit_type(s, a, rng, style)
                    else:
                        s.t(str(a))
                s.g(self.tight_or_space(rng, style["tspace"]))
                s.t(">")
        else:
            raise ValueError(t)

    # -- items

    def codec_id_attr(self, s, value, id_style, rng, style):
        s.t("#[")
        s.t("codec_id")
        sp = " " if rng.random() < 0.85 else ""
        s.g(sp, "h")
        s.t("=")
        s.g(sp)
        s.t(id_text(value, id_style, rng))
        s.t("]")

    def vis(self, s, vis, rng):
        if vis == "pub":
            s.t("pub")
        else:
            s.t("pub")
            s.t("(")
            s.t("ingot")
            s.t(")")

    def generics(self, s, m, rng, style):
        if not m.generics:
            return
        s.t("<")
        for i, (is_const, name, copy_bound) in enumerate(m.generics):
            if i:
                s.t(",")
                s.g(" ")
            if is_const:
                s.t("const")
                s.g(" ", "h")
                s.t(name)
                s.t(":")
                s.g(" ", "x")
                s.t("usize")
            else:
                s.t(name)
                if copy_bound:
                    s.t(":")
                    s.g(" ", "x")
                    s.t("Copy")
        s.t(">")

    def record(self, rng, m, style):
        s = Src()
        attrs = ["#[serializable]"] + (["#[eip712]"] if m.eip712 else [])
        if m.skip_unknown:
            attrs.append("#[codec_skip_unknown]")
        if len(attrs) > 1 and rng.random() < 0.3:
            rng.shuffle(attrs)
        if len(attrs) > 1:
            self.stats["several attributes on one item"] += 1
        for i, a in enumerate(attrs):
            if i:
                s.g(rng.choice(["\n", "\n", "\n// attribute comment\n", "\n\n"]), "nl")
            s.t(a)
        s.g(rng.choice(["\n", "\n", "\n// between the attribute and the item\n", "\n/// docs after attributes\n"]),
            "nl")
        self.vis(s, m.vis, rng)
        s.g(" ")
        s.t("struct")
        s.g(" ")
        s.t(m.name)
        self.generics(s, m, rng, style)
        s.g(" ")
        s.t("{")
        fields = list(m.fields)
        if m.name in self.perm:
            fields = [fields[i] for i in self.perm[m.name]]
        oneline = (not m.explicit and rng.random() < 0.2) or not fields
        if oneline and fields:
            self.stats["one-line struct"] += 1
        for i, f in enumerate(fields):
            # What a field's own text looks like must not depend on where the field is declared.
            frng = random.Random("%s/%s/%s" % (self.seed, m.name, f.name))
            if oneline:
                s.g(" ")
            else:
                s.g(self.member_gap(rng, doc_ok=True))
            if m.explicit:
                self.codec_id_attr(s, f.id, f.id_style, frng, style)
                s.g(rng.choice(["\n    ", "\n    ", "\n\n    "]) if not oneline else "\n", "fattr")
            self.vis(s, f.vis, rng)
            s.g(" ", "h")
            s.t(f.name)
            s.t(":")
            s.g(" " if frng.random() < 0.9 else "", "x")
            self.emit_type(s, f.ty, frng, style)
            if i + 1 < len(fields) or rng.random() < 0.6:
                s.g(self.tight_or_space(rng, 0.03))
                s.t(",")
        s.g(" " if oneline else "\n")
        s.t("}")
        return s

    def enum(self, rng, m, style):
        s = Src()
        s.t("#[serializable]")
        s.g(rng.choice(["\n", "\n", "\n// plain comment\n"]), "nl")
        self.vis(s, m.vis, rng)
        s.g(" ")
        s.t("enum")
        s.g(" ")
        s.t(m.name)
        s.g(" ")
        s.t("{")
        oneline = not m.explicit and rng.random() < 0.3
        for i, v in enumerate(m.variants):
            if oneline:
                s.g(" ")
            else:
                s.g(self.member_gap(rng, doc_ok=True))
            if m.explicit:
                self.codec_id_attr(s, v.id, v.id_style, rng, style)
                s.g("\n    ", "fattr")
            s.t(v.name)
            if i + 1 < len(m.variants) or rng.random() < 0.6:
                s.g(self.tight_or_space(rng, 0.03))
                s.t(",")
        s.g(" " if oneline else "\n")
        s.t("}")
        return s

    def copy_impl(self, rng, m):
        s = Src()
        s.t("impl")
        s.g(" ")
        s.t(m.copy_path)
        s.g(" ")
        s.t("for")
        s.g(" ")
        s.t(m.name)
        s.g(" ")
        s.t("{")
        s.g(rng.choice(["", "", " "]))
        s.t("}")
        return s

    def noise_item(self, rng, i, spec, flags, here):
        """An item the generator must skip. It is raw text: its whitespace stays as written."""
        kind = rng.choice(["fn", "fn", "fn_str", "const", "trait", "struct", "enum", "inherent", "marker"])
        n = "noise_%d" % i
        self.stats["unrelated item: " + {"fn_str": "fn", "inherent": "impl block", "marker": "impl block",
                                          "struct": "struct", "enum": "enum", "trait": "trait", "const": "const",
                                          "fn": "fn"}[kind]] += 1
        if kind == "fn":
            return ("pub fn %s(x: u32) -> u32 {\n    if x > 1 {\n        return x - 1\n    }\n"
                    "    var total: u32 = 0\n    while total < 3 {\n        total += 1\n    }\n    total\n}" % n)
        if kind == "fn_str":
            return ("fn %s() -> u32 {\n    let braces: [u8; 4] = \"}{}{\"\n    let c: u8 = b'}'\n"
                    "    let d: u8 = b'{'\n    if c == d {\n        return 1\n    }\n    2\n}" % n)
        if kind == "const":
            return "pub const NOISE_%d: u32 = %d" % (i, rng.randint(0, 1000))
        if kind == "trait":
            return "trait Marker%d {\n    fn mark(self) -> u32\n}" % i
        if kind == "struct":
            return "struct Scratch%d {\n    x: u32,\n    y: u32,\n}" % i
        if kind == "enum":
            return "enum Spare%d {\n    One,\n    Two,\n}" % i
        models = [m for m in here if m.is_record and not m.generics]
        if kind == "inherent" and models:
            m = rng.choice(models)
            return "impl %s {\n    fn noise_method_%d(self) -> u32 {\n        if true {\n            return 1\n        }\n        2\n    }\n}" % (m.name, i)
        if kind == "marker" and models:
            m = rng.choice(models)
            return ("trait Tag%d {\n    fn tag(self) -> u32\n}\n\nimpl Tag%d for %s {\n    fn tag(self) -> u32 {\n        %d\n    }\n}"
                    % (i, i, m.name, i))
        return "pub const NOISE_%d: u32 = 1" % i

    def uses(self, rng, m_index, models_here):
        """The module's `use` lines, as Src parts, and the names it needs."""
        spec = self.spec
        names = set()
        foreign = {}
        for m in models_here:
            types = []
            if m.is_record:
                types = [f.ty for f in m.fields]
            for t in types:
                for w in walk(t):
                    if w[0] == "text":
                        names.add("Text")
                    elif w[0] == "buf":
                        names.add("BoundedBuffer")
                    elif w[0] == "packed":
                        names.add("PackedBytes")
                    elif w[0] == "addr":
                        names.add("Address")
                    elif w[0] == "ref":
                        o = spec.model(w[1])
                        if o.module != m_index:
                            foreign.setdefault(o.module, set()).add(o.name)
        lines = []
        core = sorted(n for n in names if n in ("Text", "BoundedBuffer"))
        if rng.random() < 0.15:
            core = sorted(set(core) | {"Text", "BoundedBuffer"})  # an unused import
        if core:
            lines.append(("fe_codec_core", core))
        if "PackedBytes" in names:
            lines.append(("core::bytes", ["PackedBytes"]))
        if "Address" in names:
            lines.append(("std::evm", ["Address"]))
        for mod, nms in sorted(foreign.items()):
            lines.append(("ingot::" + self.module_names()[mod], sorted(nms)))
        out = []
        for path, nms in lines:
            s = Src()
            s.t("use")
            s.g(" ")
            if len(nms) == 1 and rng.random() < 0.6:
                s.t(path + "::" + nms[0])
            else:
                s.t(path + "::{")
                s.g(rng.choice(["", "", " "]))
                for i, n in enumerate(nms):
                    if i:
                        s.t(",")
                        s.g(" " if rng.random() < 0.85 else "")
                    s.t(n)
                s.g(rng.choice(["", "", " "]))
                s.t("}")
            out.append(s)
        return out

    def render(self):
        """{'a': Src, ...}: one source per module."""
        spec = self.spec
        out = {}
        flags = {}
        for mi, mname in enumerate(self.module_names()):
            rng = random.Random("%s/%s" % (self.seed, mname))
            style = {"tspace": 0.04 if rng.random() < 0.3 else 0.0, "attr_space": False}
            here = [m for m in spec.models if m.module == mi]
            s = Src()
            if rng.random() < 0.3:
                s.t("//! Models for module %s." % mname)
                s.g("\n\n", "nl")
            uses = self.uses(rng, mi, here)
            for u in uses:
                s.extend(u)
                s.g("\n", "nl")
            items = []
            for m in here:
                items.append((self.record(rng, m, style) if m.is_record else self.enum(rng, m, style), True))
                if m.copy_impl:
                    items.append((self.copy_impl(rng, m), False))
            noise = rng.randint(0, 3)
            for i in range(noise):
                raw = self.noise_item(rng, mi * 10 + i, spec, flags, here)
                pos = rng.randint(0, len(items))
                src = Src()
                src.t(raw)
                items.insert(pos, (src, False))
            for k, (item, doc_ok) in enumerate(items):
                s.g(self.item_gap(rng, doc_ok) if k else "\n" + ("\n" if uses else ""), "nl")
                s.extend(item)
            s.g("\n", "nl")
            out[mname] = s
        return out


def render_files(spec, seed, perm=None):
    return Renderer(spec, seed, perm).render()


def finalize(src, crlf=False, fill=None):
    text = src.text(fill)
    return text.replace("\n", "\r\n") if crlf else text


def permutation(spec, rng):
    """A random order of the fields of every explicitly numbered record."""
    perm = {}
    for m in spec.models:
        if m.is_record and m.explicit and len(m.fields) > 1:
            order = list(range(len(m.fields)))
            rng.shuffle(order)
            perm[m.name] = order
    return perm
