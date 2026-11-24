# Types and limits

## Supported types

| Type | Notes |
| --- | --- |
| `bool`, `u8`–`u256`, `i8`–`i256` | `usize` and `isize` are rejected: their width differs per target |
| `Text<N>` | UTF-8, up to `N` bytes |
| `BoundedBuffer<N>` | Bytes, up to `N` |
| `core::bytes::PackedBytes<N>`, `std::evm::Address` | Bytes of exactly `N`, or 20 |
| `Option<T>` | Anywhere, except `Option<Option<T>>`, which cannot round-trip |
| `[T; N]` | As a field, any length of `Copy` elements, or up to 64 of other elements. Elsewhere, such as inside an `Option`, decoding needs `T: Copy` and `N > 0` |
| `List<T, N>` | 0 to `N` elements, each of a `Copy` type; `Copy` itself when `T` is. A field or nested, with any supported element type |
| `Map<K, V, N>` | 0 to `N` entries with unique keys; `K` is an unsigned integer, `Text<N>`, `Address` or `PackedBytes<N>`, and `V` a `Copy` type; `Copy` itself when both are. A field or nested, with any supported value type |
| `(T0, ..., T7)` | Tuples of 1 to 8 supported types, anywhere a type can appear. `()` has no wire form and is rejected |
| `#[serializable]` structs | Named fields of up to 31-byte names; type and const generic parameters |
| `#[serializable]` enums | Unit variants of up to 31-byte names |
| Your own types | With handwritten impls; see [writing serializers](writing-serializers.md) |

Not supported yet: enums with payloads.

## Lists

`List<T, N>` from `fe_codec_core` is the variable-length collection: its capacity is fixed at
compile time, like `Text<N>`'s, and its length is part of the value. It stores copies of its
elements, so `T` must be `Copy`; the generator rejects a local type that is not, and adds the
bound to a type parameter. Encoding writes only the elements present; decoding fails with
`CapacityExceeded` when the input holds more than `N`.

```fe
#[serializable]
pub struct Batch {
    pub ids: List<u32, 8>,
}

var ids = List<u32, 8>::new()
ids.push(7)?                                   // CapacityExceeded once it holds 8
let batch = Batch { ids }
let same = List<u32, 8>::from_array([7, 9])?   // or from an array of up to 8 elements
```

`len`, `capacity`, `is_empty` and `get(index)` read a list, and lists of equal elements are
`==`. [Wire profiles](formats.md#lists) say how each format writes one.

## Maps

`Map<K, V, N>` holds up to `N` entries with unique keys. Like a list it has a capacity fixed at
compile time and stores copies, so `V` must be `Copy`; the generator rejects a local value type
that is not, and adds the bound to a type parameter. Entries are kept sorted by key, so the
order they were inserted in never shows: equal maps are `==` and encode to the same bytes in
every format.

The key type implements `MapKey`, which fixes the order. The impls are for the unsigned
integers, `Text<N>` (shorter first, then bytewise), `Address` and `PackedBytes<N>` (bytewise).
They are the keys whose order in a deterministic CBOR map is the order of the values, and
whose JSON form is a string. Signed integers, `bool`, records and the other types are not
keys: CBOR's canonical order puts a negative integer after every non-negative one, against
numeric order. The generator rejects a key type it knows is none of these; a type parameter
used as a key is left to the compiler, so declare it `K: MapKey` on the struct.

```fe
#[serializable]
pub struct Book {
    pub balances: Map<Address, u64, 8>,
}

var balances = Map<Address, u64, 8>::new()
balances.insert(owner, 5)?          // returns the value it replaced; CapacityExceeded when full
let five = balances.get(owner)      // Option<u64>
let gone = balances.remove(owner)   // Option<u64>
```

`len`, `capacity`, `is_empty`, `contains` and `get(key)` read a map, and `key_at(index)` and
`value_at(index)` visit its entries in key order. Maps with the same entries are `==` when the
values are. Decoding rejects a key that appears twice with `DuplicateField` at the key, and
more than `N` entries with `CapacityExceeded`. [Wire profiles](formats.md#maps) say how each
format writes one.

## Choosing a serializer per field

`#[codec_with = Path]` on a field names a `FieldCodec` that writes and reads that field
instead of its type's own impls. The field's type can be any type the codec accepts, so the
generator checks none of it: `usize`, a type that isn't `#[serializable]` and a non-`Copy`
array are all fine if the codec handles them.

```fe
#[serializable]
pub struct Payment {
    #[codec_with = HexAmount]
    pub amount: u64,
    #[codec_with = HexAmount]
    pub tip: Option<u64>,
}
```

For an `Option<T>` field the codec handles `T`: `None` is still left out by formats that
identify fields by key, and a present value follows the format's marker for one. See
[writing serializers](writing-serializers.md#choosing-a-serializer-for-one-field).

## Buffers and limits

Buffers have a capacity fixed at compile time, and nothing is heap-allocated. Encoding
fails with `CapacityExceeded` at the first byte that doesn't fit. `Limits` bounds the
nesting depth and the entries per container. `fe_codec_json::encode` and the other free
functions use `Limits::standard()` (32 levels, 256 entries); `Json::with_limits(..)` and
`Cbor::with_limits(..)` set others:

```fe
store(Cbor::with_limits(Limits { max_depth: 1, max_items: 5 }), order)
```

Move bytes across platform boundaries with `BoundedBuffer<N>::from_span` and `copy_to`.

## Errors

Errors carry a kind, the byte offset and the field:

```fe
match fe_codec_json::decode<Order, 25>(input) {
    Result::Err(error) => {
        // error.kind == ErrorKind::UnknownVariant, error.offset == 17,
        // error.field() is the name "status"
    },
    Result::Ok(order) => { ... },
}
```

`error.kind.code()` is a stable number for platform boundaries. On the EVM, `unwrap` on a
failed result reverts with that code and the offset as two ABI words.

## Schema evolution

Give fields stable wire identities with `#[codec_id]`, and add new fields as `Option`s:

```fe
#[serializable]
pub struct Account {
    #[codec_id = 1]
    pub owner: u64,
    #[codec_id = 2]
    pub balance: u64,
    #[codec_id = 3]
    pub label: Option<Text<8>>,   // added later: old data still decodes
}
```

A reader built from an older model can accept data written by a newer one if its type opts in
with `#[codec_skip_unknown]`, which skips every field the type does not list. Without it, an
unknown field is an error.

```fe
#[serializable]
#[codec_skip_unknown]
pub struct Account {
    #[codec_id = 1]
    pub owner: u64,
    #[codec_id = 2]
    pub balance: u64,
}
```

The skipped value must still be well formed and within the limits, and unknown fields count
toward `Limits.max_items`. Only the marked record skips: a record nested inside it does so only
if it is marked too. See [wire profiles](formats.md#ignoring-unknown-fields) for what each
format checks, and [writing a format](writing-a-format.md) for formats that cannot skip.

A type numbers all of its members or none. Field IDs start at 1 and variant IDs at 0; without
IDs, members are numbered in declaration order. [Wire profiles](formats.md#schema-evolution)
lists what each change preserves in each format.
