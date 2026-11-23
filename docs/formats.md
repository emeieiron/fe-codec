# Wire profiles

Both first-party formats encode the same data model and follow the same rules for records.
Each is strict: given a type, every value has exactly one encoding, and a decoder accepts
only that encoding (JSON also accepts insignificant variations listed below).

## Rules shared by every format

- **Records** are written with their fields in ascending ID order. A field's ID is its
  `#[codec_id]`, or its one-based declaration position when the type assigns none.
- **Optional fields** (`Option<T>`) are left out when `None` by formats that write field
  keys (JSON, CBOR). Decoders accept the field missing or present as `null`.
- **Decoders reject** unknown fields (unless the record is marked `#[codec_skip_unknown]`; see
  [below](#ignoring-unknown-fields)), duplicate fields, missing required fields, trailing
  data after the root value, sequences of the wrong length, and integers outside the
  field's type.
- **Limits** cap nesting depth and entries per container. `Limits::standard()` allows 32
  nested containers and 256 entries. Buffers have a fixed capacity `N`, and the encoder
  reports the first byte that does not fit.
- **Errors** report a kind (with a stable numeric `code()`), the byte offset where the
  problem was found, and the innermost field involved (`error.field()`).
- **Names** of fields and variants are 1 to 31 bytes. A JSON key longer than that cannot
  match a field and is reported as `UnknownField`, or skipped in a record that skips unknown
  fields.

## JSON

Output is compact UTF-8 with no whitespace. [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html)

| Data model | JSON |
| --- | --- |
| `bool` | `true` / `false` |
| Integers of every width | Decimal numbers, `-` for negatives, no leading zeros |
| `Text<N>` | A string; `"`, `\` and control characters escaped, other bytes as UTF-8 |
| Bytes (`BoundedBuffer<N>`, `PackedBytes<N>`, `Address`) | A string of `0x` and two lowercase hex digits per byte |
| `None` | `null` (left out as a record field) |
| Unit enum variant | Its name as a string |
| Record | An object keyed by field name |
| Array `[T; N]` | An array of exactly `N` values |
| Tuple `(T0, ..., Tn)` | An array of its elements in order, of exactly its length |

Decoding also accepts whitespace between tokens, members in any order, `\u` escapes
including surrogate pairs, and hex digits of either case. It rejects fractions and
exponents in integer fields (`Unsupported`), a `0X` prefix, and an odd number of hex
digits. It does not verify EIP-55 address checksums.

Integers above 2^53 are exact in this encoding but not in every JSON parser. Consumers
in other languages need an integer-preserving decoder for `u64`, `i64` and wider types.

## CBOR

Deterministic encoding as specified by
[RFC 8949 §4.2.1](https://www.rfc-editor.org/rfc/rfc8949.html#section-4.2.1).

| Data model | CBOR |
| --- | --- |
| `bool` | `0xf4` / `0xf5` |
| Integers that fit 64 bits | Major type 0 or 1 with the shortest head |
| Wider integers | Tag 2 (unsigned) or 3 (negative) around the big-endian magnitude, without leading zero bytes |
| `Text<N>` | Major type 3 |
| Bytes | Major type 2 |
| `None` | `0xf6` (left out as a record field) |
| Unit enum variant | Its ID as an unsigned integer |
| Record | A definite-length map keyed by field ID, keys ascending |
| Array `[T; N]` | A definite-length array of exactly `N` items |
| Tuple `(T0, ..., Tn)` | A definite-length array of its elements in order, of exactly its length |

Fields of 64 bits or fewer accept only major types 0 and 1, so a bignum there is a
`TypeMismatch`; a canonical bignum would not fit them anyway. Variant IDs are the zero-based
declaration positions unless the enum assigns `#[codec_id]`s. Decoding enforces the shortest head for every argument, ascending map keys,
and the bignum rules: a bignum must exceed 64 bits, fit 256 bits, and have no leading zero.
The profile has no floating-point values, other tags, or indefinite-length items.

The encoder and decoder share one function for head widths, and `femp prove` proves that
every argument has exactly one accepted head (see [design](design.md#guarantees)).

## Schema evolution

| Change | JSON | CBOR |
| --- | --- | --- |
| Append an optional field with a new ID | Old data decodes in new code. New data decodes in old code while the field is `None`. | Same |
| Rename a field, keeping its ID | Breaks: the key is the name | Compatible: the key is the ID |
| Reorder fields without explicit IDs | Compatible: decoders accept any order | Breaks: IDs follow declaration order |
| Reorder fields with explicit IDs | Compatible | Compatible |
| Append a field of any kind to a type that skips unknown fields | Old readers accept new data | Same |
| Remove a field, or change its type | Breaks | Breaks |

Assign `#[codec_id]` to every field and variant of a type whose CBOR encoding is stored or
exchanged. Readers reject fields they do not know unless their type is marked
`#[codec_skip_unknown]`, so either add fields only once every reader has been upgraded to
accept them, or mark the readers' types before the writers change. A skipping reader still
needs a field it requires: removing one breaks it.

## Ignoring unknown fields

A record marked `#[codec_skip_unknown]` skips the fields it does not list, in JSON and CBOR.
The decoder reads each skipped value as strictly as any other, so a malformed or over-limit
value is still an error, with its usual kind and offset. Skipped fields count toward
`Limits.max_items` and their values toward `Limits.max_depth`. Duplicates, order and missing
fields are checked for the fields the record lists as usual.

- **JSON** skips any RFC 8259 value: strings with their escapes and UTF-8 checked, numbers of
  the full grammar (fractions and exponents too, which integer fields reject), `true`,
  `false`, `null`, and arrays and objects of those. A key longer than 31 bytes is skippable.
  Repeated names in a skipped object, or among unknown members, are not detected, because
  nothing is kept to compare them with.
- **CBOR** skips one item of the profile above: shortest heads, definite lengths, major
  types 0 to 5, tags 2 and 3 with the bignum rules, and false, true and null. Floats, other
  tags and other simple values are `Unsupported`. Unknown field IDs must still ascend
  (`NonCanonical`) and not repeat (`DuplicateField`), and ID 0 is `UnknownField`. Keys of
  a skipped map may be any item, and must ascend bytewise in their encodings, as
  RFC 8949 §4.2.1 requires; a descending key is `NonCanonical` and a repeated one
  `DuplicateField`, at the key.
- **Compact** cannot skip: it writes no keys or lengths to find a value's end by. Its
  `skip_value` is the default, `Unsupported`, and its decoder reads fields by position, so the
  attribute has no effect there.
