# Writing serializers by hand

`fe_codec_gen` writes serializers for `#[serializable]` structs and enums. Write one yourself
when a type needs its own representation, or when it is not a plain record or enum.
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
one value call, or opens one record, sequence or map and closes it. Every format rejects a
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

- `serialize` follows the contract of `Serialize`: one value call, or one record, sequence or map
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

## Enums

An enum implements `Schema::variant`, listing each variant's name and ID, and writes
its variant's index with `encoder.write_variant<Self>(index)`. On decode,
`decoder.read_variant<Self>()` returns the index of the variant on the wire.

A variant with a payload is written inside a wrapper: `begin_variant<Self>(index)`, the
payload as one value, and `end_variant`. On decode, `begin_variant<Self>()` reads either form of
any variant and returns a `VariantKey`; the variant's index is `key.index`. The example has one
variant of each kind. The record variant has a schema of its own, an empty type that lists its
fields, which `begin_record` and `next_field` take in place of `Self`:

```fe
pub enum Shape {
    Empty,
    Point(u64),
    Circle { pub radius: u64 },
}

struct CircleFields {}

impl Schema for CircleFields {
    fn name() -> SchemaName { schema_name<6>("Circle") }
    fn field(_ position: usize) -> Option<FieldInfo> {
        match position {
            0 => Option::Some(FieldInfo { name: schema_name<6>("radius"), id: 1, optional: false }),
            _ => Option::None,
        }
    }
}

impl Schema for Shape {
    fn name() -> SchemaName { schema_name<5>("Shape") }
    fn variant(_ index: usize) -> Option<VariantInfo> {
        match index {
            0 => Option::Some(VariantInfo { name: schema_name<5>("Empty"), id: 0 }),
            1 => Option::Some(VariantInfo { name: schema_name<5>("Point"), id: 1 }),
            2 => Option::Some(VariantInfo { name: schema_name<6>("Circle"), id: 2 }),
            _ => Option::None,
        }
    }
}

impl Serialize for Shape {
    fn serialize<E: Encoder>(self, _ encoder: mut E) -> CodecResult<()> {
        match self {
            Shape::Empty => encoder.write_variant<Self>(0),
            Shape::Point(ref value) => {
                let variant = encoder.begin_variant<Self>(1)?
                value.serialize(mut encoder)?
                encoder.end_variant(variant)
            },
            Shape::Circle { radius: ref radius } => {
                let variant = encoder.begin_variant<Self>(2)?
                var record = encoder.begin_record<CircleFields>(1)?
                encoder.field<CircleFields>(mut record, 0)?
                radius.serialize(mut encoder)?
                encoder.end_record(record)?
                encoder.end_variant(variant)
            },
        }
    }
}

impl Deserialize for Shape {
    fn deserialize<D: Decoder>(_ decoder: mut D) -> CodecResult<Self> {
        let variant = decoder.begin_variant<Self>()?
        match variant.index {
            0 => {
                variant.expect_unit()?
                Result::Ok(Shape::Empty)
            },
            1 => {
                variant.expect_payload()?
                let value = u64::deserialize(mut decoder)?
                decoder.end_variant(variant)?
                Result::Ok(Shape::Point(value))
            },
            2 => {
                variant.expect_payload()?
                var radius: Option<u64> = Option::None
                var record = decoder.begin_record<CircleFields>()?
                while let Option::Some(key) = decoder.next_field<CircleFields>(mut record)? {
                    match key.position {
                        0 => key.read(mut decoder, mut radius)?,
                        _ => return Result::Err(key.unknown()),
                    }
                }
                decoder.end_record(record)?
                let value = Shape::Circle { radius: <CircleFields as Schema>::required(radius, 0, decoder.offset())? }
                decoder.end_variant(variant)?
                Result::Ok(value)
            },
            _ => Result::Err(variant.unknown()),
        }
    }
}
```

A payload of several elements is a sequence, written as in `impl Serialize for (T0, T1)`. A format
that does not support payload variants fails with `Unsupported` at `begin_variant`.
