# Writing serializers by hand

`fe_codec_gen` writes serializers for `#[serializable]` structs and enums. Write one yourself
when a type needs its own representation, or when it is not a plain record or unit enum.
A handwritten serializer is used exactly like a generated one, including as a field of a
generated record.

A serializer implements `Serialize` and `Deserialize` on the type itself:

```fe
pub trait Serialize {
    fn serialize<E: Encoder>(self, _ encoder: mut E) -> CodecResult<()>
}

pub trait Deserialize {
    fn deserialize<D: Decoder>(_ decoder: mut D) -> CodecResult<Self>
}
```

Fe only allows an impl in the ingot that declares the trait or the type. For a type from
another ingot, wrap it in a type of your own.

## A scalar with its own representation

Most custom serializers write one value through the encoder and read it back. This one
writes a temperature as text such as `"21C"` in every format:

```fe
use fe_codec_core::{CodecError, CodecResult, Decoder, Deserialize, Encoder, ErrorKind, Serialize, Text}

pub struct Celsius { pub degrees: u8 }

impl Serialize for Celsius {
    fn serialize<E: Encoder>(self, _ encoder: mut E) -> CodecResult<()> {
        encoder.write_text(self.text()?)
    }
}

impl Deserialize for Celsius {
    fn deserialize<D: Decoder>(_ decoder: mut D) -> CodecResult<Self> {
        let start = decoder.offset()
        match Celsius::parse(decoder.read_text<4>()?) {
            Option::Some(value) => Result::Ok(value),
            Option::None => Result::Err(CodecError::at(kind: ErrorKind::InvalidSyntax, offset: start)),
        }
    }
}
```

`examples/vanilla/src/temperature.fe` has the complete version. A serializer makes exactly
one value call, or opens one record or sequence and closes it. Every format rejects a
serializer that writes nothing or writes twice (`InvalidState`).

## Choosing a serializer for one field

To change how a single field is written, without a wrapper type, implement `FieldCodec<T>` on
an empty type and name it with `#[codec_with]`. The codec is a set of functions, not a value:

```fe
pub trait FieldCodec<T> {
    fn serialize<E: Encoder>(_ value: T, _ encoder: mut E) -> CodecResult<()>
    fn deserialize<D: Decoder>(_ decoder: mut D) -> CodecResult<T>
    fn describe<V: Describer>(_ describer: mut V) -> CodecResult<()>   // optional
}
```

This one writes a `u64` as lowercase hexadecimal text, such as `"ff"`, in every format. The
codec is as strict as the rest of the library: reading rejects uppercase digits and leading
zeros, so each value has one encoding. `hex` and `parse_hex` stand for the digit conversions.

```fe
use fe_codec_core::{CodecError, CodecResult, Decoder, Describer, Encoder, ErrorKind, FieldCodec}

pub struct HexAmount {}

impl FieldCodec<u64> for HexAmount {
    fn serialize<E: Encoder>(_ value: u64, _ encoder: mut E) -> CodecResult<()> {
        encoder.write_text(hex(value)?)
    }

    fn deserialize<D: Decoder>(_ decoder: mut D) -> CodecResult<u64> {
        let start = decoder.offset()
        match parse_hex(decoder.read_text<16>()?) {
            Option::Some(value) => Result::Ok(value),
            Option::None => Result::Err(CodecError::at(kind: ErrorKind::InvalidSyntax, offset: start)),
        }
    }

    fn describe<V: Describer>(_ describer: mut V) -> CodecResult<()> {
        describer.text(16)
    }
}

#[serializable]
pub struct Payment {
    #[codec_with = HexAmount]
    pub amount: u64,
    #[codec_with = HexAmount]
    pub tip: Option<u64>,
}
```

`examples/transfer/shared/src/codecs.fe` has the complete version, with a second codec that
writes a record as a sequence.

- `serialize` follows the contract of `Serialize`: one value call, or one record or sequence
  opened and closed. `value` is passed by reference, so the type need not be `Copy`.
- `describe` is only for formats that read types, such as EIP-712. It states the shape
  `serialize` writes. The default fails with `Unsupported`.
- Errors from `deserialize` are named after the field by the generated code, as for any other
  field. A codec can fail with whatever kind and offset suits it; `decoder.offset()` before
  reading gives the start of the value.
- For `Option<T>` fields the codec sees only `T`; the generated code writes the format's
  marker for `Some` first and reads it back first. `None` never reaches the codec.

Generated code calls `FieldCodec` through `FieldKey::read_with` and `read_optional_with`, and
the functions `serialize_optional_with` and `describe_optional_with`. A handwritten record can
use them the same way.

## A record

A record also implements `Schema`, which lists its fields by *position*: position 0 is the
field with the smallest ID, and IDs ascend. Formats use the names (JSON) or IDs (CBOR) from
here.

```fe
use fe_codec_core::{CodecResult, Decoder, Deserialize, Encoder, FieldInfo, Schema, Serialize, schema_name}

pub struct Point { pub x: i64, pub y: i64 }

impl Schema for Point {
    fn field(_ position: usize) -> Option<FieldInfo> {
        match position {
            0 => Option::Some(FieldInfo { name: schema_name<1>("x"), id: 1, optional: false }),
            1 => Option::Some(FieldInfo { name: schema_name<1>("y"), id: 2, optional: false }),
            _ => Option::None,
        }
    }
}

impl Serialize for Point {
    fn serialize<E: Encoder>(self, _ encoder: mut E) -> CodecResult<()> {
        var record = encoder.begin_record<Self>(2)?
        encoder.field<Self>(mut record, 0)?
        self.x.serialize(mut encoder)?
        encoder.field<Self>(mut record, 1)?
        self.y.serialize(mut encoder)?
        encoder.end_record(record)
    }
}

impl Deserialize for Point {
    fn deserialize<D: Decoder>(_ decoder: mut D) -> CodecResult<Self> {
        var x: Option<i64> = Option::None
        var y: Option<i64> = Option::None
        var record = decoder.begin_record<Self>()?
        while let Option::Some(key) = decoder.next_field<Self>(mut record)? {
            match key.position {
                0 => key.read(mut decoder, mut x)?,
                1 => key.read(mut decoder, mut y)?,
                _ => return Result::Err(key.unknown()),
            }
        }
        decoder.end_record(record)?
        Result::Ok(Point { x: Self::required(x, 0, decoder.offset())?, y: Self::required(y, 1, decoder.offset())? })
    }
}
```

The rules every format enforces:

- **Encoding.** `begin_record` takes the number of fields you will visit. Visit them in
  position order. Formats that write keys set `E::OMIT_NONE`; when it is true, leave out
  optional fields that are `None` and do not count them. Otherwise visit every field. A
  visit out of order or a wrong count is an error in every format.
- **Names** are 1 to 31 bytes, written with `schema_name<N>("...")`.
- **Decoding.** `next_field` returns the fields present on the wire, in the format's order,
  which need not be position order. `key.read` decodes the value, rejects a second value
  for the same field (`DuplicateField`) and names the field in any error from the value.
  `Self::required` reports a missing field (`MissingField`) with its name;
  `fe_codec_core::optional` turns a missing optional field into `None`.

Check a handwritten schema in a test with `fe_codec_core::validate_schema<Point>()`. Run
the type through every format you support. `examples/transfer/shared/src/conformance.fe`
shows a check that works for any `Format`.

## Unit enums

An enum implements `Schema::variant`, listing each variant's name and ID, and writes
its variant's index with `encoder.write_variant<Self>(index)`. On decode,
`decoder.read_variant<Self>()` returns the index of the variant on the wire.
