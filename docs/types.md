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
| `#[serializable]` structs | Named fields of up to 31-byte names; type and const generic parameters |
| `#[serializable]` enums | Unit variants of up to 31-byte names |
| Your own types | With handwritten impls; see [writing serializers](writing-serializers.md) |

Not supported yet: enums with payloads, variable-length collections, maps, tuples, ignoring
unknown fields, and choosing a serializer per field.

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

A type numbers all of its members or none. Field IDs start at 1 and variant IDs at 0; without
IDs, members are numbered in declaration order. [Wire profiles](formats.md#schema-evolution)
lists what each change preserves in each format.
