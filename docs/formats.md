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
  data after the root value, sequences of the wrong length, lists and maps longer than their capacity, repeated map keys,
  variants written in the wrong form (see [Enums with payloads](#enums-with-payloads)),
  and integers outside the field's type.
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
| Enum variant without a payload | Its name as a string |
| Enum variant with a payload | An object of one member: its name, and the payload; see [Enums with payloads](#enums-with-payloads) |
| Record | An object keyed by field name |
| Array `[T; N]` | An array of exactly `N` values |
| List `List<T, N>` | An array of 0 to `N` values |
| Map `Map<K, V, N>` | An object of 0 to `N` members, in key order; see [Maps](#maps) |
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
| Enum variant without a payload | Its ID as an unsigned integer |
| Enum variant with a payload | A map of one entry: its ID, and the payload; see [Enums with payloads](#enums-with-payloads) |
| Record | A definite-length map keyed by field ID, keys ascending |
| Array `[T; N]` | A definite-length array of exactly `N` items |
| List `List<T, N>` | A definite-length array of 0 to `N` items |
| Map `Map<K, V, N>` | A definite-length map of 0 to `N` entries whose keys are the keys' own encodings, ascending |
| Tuple `(T0, ..., Tn)` | A definite-length array of its elements in order, of exactly its length |

Fields of 64 bits or fewer accept only major types 0 and 1, so a bignum there is a
`TypeMismatch`; a canonical bignum would not fit them anyway. Variant IDs are the zero-based
declaration positions unless the enum assigns `#[codec_id]`s. Decoding enforces the shortest head for every argument, ascending map keys,
and the bignum rules: a bignum must exceed 64 bits, fit 256 bits, and have no leading zero.
The profile has no floating-point values, other tags, or indefinite-length items.

The encoder and decoder share one function for head widths, and `femp prove` proves that
every argument has exactly one accepted head (see [design](design.md#guarantees)).

## Lists

A `List<T, N>` is written as an array of its current length, so in JSON and CBOR a list and
an array of the same elements have the same bytes, and either decodes the other's when the
length fits. A decoder reads as many elements as the input holds, up to `N`, and rejects more
with `CapacityExceeded`:

- **JSON** reports the first element past `N`, where it starts. A trailing comma is
  `InvalidSyntax`.
- **CBOR** reports an array whose head counts more than `N` at the head, before reading any
  element. An indefinite-length or non-shortest head is `NonCanonical`, as for any array.
- **Compact** writes the length as a ULEB128 number in its shortest form before the elements,
  which a fixed-length array leaves out, and reports a length over `N` at the length.

`Limits.max_items` applies to the elements of a list as to any container, and is checked
first: with `max_items` below `N`, a list of `max_items + 1` elements is `CollectionLimit`
in every format. A list is one level of `Limits.max_depth`.

## Maps

A `Map<K, V, N>` writes its entries in the order of `MapKey::before`, which is the order of the
keys' deterministic CBOR encodings (RFC 8949 §4.2.1), so a map never shows how it was built.
A decoder reads up to `N` entries and rejects more with `CapacityExceeded`; `Limits.max_items`
is checked first, and a map is one level of `Limits.max_depth`.

- **JSON** writes an object. A key is always a string: `Text` as itself, an unsigned integer as
  its canonical decimal in quotes (`"42"`: no sign, no leading zeros, no spaces), and `Address`
  and `PackedBytes` as `0x` hex. A key that is anything else, such as an unquoted number, is
  `TypeMismatch`; a key whose digits are not canonical is `InvalidSyntax`, or
  `IntegerOutOfRange` when it is negative or too large. Members may come in any order, as for
  a record, and the map sorts them. A key that appears twice is `DuplicateField` at the second.
  An extra entry is `CapacityExceeded` at its key. A trailing comma is `InvalidSyntax`.
- **CBOR** writes a definite-length map. Keys are the keys' normal encodings and must strictly
  ascend in bytewise order of those encodings: a key before its predecessor is `NonCanonical`,
  and a repeat is `DuplicateField`, both at the key. The decoder compares the encoded bytes
  as they stand in the input, as it does for the keys of a map it skips. A head counting more
  than `N` entries is `CapacityExceeded` at the head, before any entry is read. An
  indefinite-length or non-shortest head is `NonCanonical`, as for any map.
- **Compact** has no maps: the example keeps the `Unsupported` defaults, because its decoder
  sees the bytes of a key and not its type, and cannot enforce the order.
- **EIP-712** has no map type; see [its notes](eip712.md).

## Enums with payloads

A variant with no payload is written as above in every enum, whether or not the enum has other
variants that do. A variant with a payload is a wrapper of one entry, the variant's name (JSON)
or ID (CBOR), and the payload: `{"Point":5}`, `{"Pair":[1,"a"]}`, `{"Circle":{"radius":2}}`
and `a1 01 05`. The payload is the element itself for a tuple variant of one element, a
sequence of the elements for one of several, and for a record variant a record of its fields,
keyed by name in JSON and by field ID in CBOR, in ascending ID order, with `None` fields left
out. An option payload stays: `{"Maybe":null}` and `a1 01 f6`.

A decoder accepts exactly that. Each error is reported at the byte shown:

| Input | Error |
| --- | --- |
| A variant with no payload written as a wrapper, or one with a payload written bare | `TypeMismatch` at the start of the variant |
| Neither a string nor an object (JSON), neither an unsigned integer nor a map (CBOR) | `TypeMismatch` at the start |
| A wrapper key that is not a string (JSON) or an unsigned integer (CBOR) | `TypeMismatch` at the key |
| A wrapper of no entry | `InvalidLength`, at the closing brace (JSON) or at the head of the map (CBOR) |
| A wrapper of two or more entries | `InvalidLength`, at the comma after the first (JSON) or at the head of the map (CBOR) |
| An unknown name or ID | `UnknownVariant`, at the name or ID, inside the wrapper when there is one |
| A wrapper missing its colon, or JSON that ends inside it | `InvalidSyntax` or `UnexpectedEnd`, as anywhere else |

The wrapper is a container: it counts as one level of `Limits.max_depth` and one entry toward
`Limits.max_items`, and the payload's own container is another level. A record variant's fields,
and the elements of a tuple variant, are checked as those of a record or a tuple are, and an
error in one is named after its field, if it has one. When a required field of a record
variant is missing, `MissingField` is reported where the record ends, before the wrapper is
checked for a second entry. JSON accepts whitespace around the wrapper's tokens as usual.

- **`#[codec_skip_unknown]`** on the enum makes each record variant skip the fields it does
  not list, as [below](#ignoring-unknown-fields); it has no effect on the other variants, and
  an unknown variant is `UnknownVariant` all the same.
- **Compact** keeps the `Unsupported` defaults of `begin_variant` and `end_variant`. A
  positional format has nothing in the bytes to say whether a payload follows a variant's ID,
  so it cannot read an enum with payloads: reading any variant of one, even one with no
  payload, is `Unsupported`, and so is writing one with a payload. A variant with no payload
  still writes as it does in any enum.
- **EIP-712** has no union type; see [its notes](eip712.md).

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
An enum marked so does it for each of its record variants.
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
