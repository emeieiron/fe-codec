# Getting started

Fe Codec turns your Fe types into JSON, deterministic CBOR or a format of your own, and
hashes them as EIP-712 typed data. You describe a type once, a generator writes its
serializers, and the same code runs in contracts, native programs and WebAssembly.

You need the Fe toolchain, version 26.6.0. Nothing else: the generator is written in Fe too.

## Install the generator

Check out the repository at the revision you depend on, and build the generator into a
directory on your `PATH`:

```bash
git clone https://github.com/emeieiron/fe-codec.git && cd fe-codec && git checkout <commit>
```

```bash
fe build --backend native tools/generator --out-dir ~/.local/bin
```

The command is `fe_codec_gen`.

## Add the ingots

Add the runtime ingots to your `fe.toml`, pinned to the same revision. One Git source supplies
several ingots by name:

```toml
[dependencies]
fe_codec_core = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_core" }
fe_codec_json = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_json" }
fe_codec_cbor = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_cbor" }
```

Add `fe_codec_eip712` the same way if you sign typed data.

## Describe your types

Mark structs and unit enums `#[serializable]`. The types are ordinary Fe: nothing else
changes about how you use them.

```fe
use fe_codec_core::Text
use std::evm::Address

#[serializable]
pub struct Order {
    pub id: u64,
    pub buyer: Address,
    pub total: u256,
    pub status: OrderStatus,
    pub note: Option<Text<32>>,
}

#[serializable]
pub enum OrderStatus {
    Open,
    Paid,
}
```

## Generate the serializers

Write them into a module beside the models, and commit the result:

```bash
fe_codec_gen --source src/models.fe --out src/serializers.fe
```

Run the same command with `--check` in CI to catch a stale file. Pass `--source` once per
module when your models span several files. The generated module is ordinary Fe that
implements traits on your types, so you never call into it directly. [The generator](generator.md)
describes every option, and how FeMP can run it on each build instead.

## Encode and decode

The annotated bindings choose the type and the buffer capacity:

```fe
fn round_trip(_ order: Order) -> CodecResult<Order> {
    let bytes: BoundedBuffer<192> = fe_codec_json::encode(order)?
    let back: Order = fe_codec_json::decode(bytes)?
    Result::Ok(back)
}
```

Without annotations, name both generic arguments: `fe_codec_json::encode<Order, 192>(order)`.
Fe does not accept just one of them.

Switching formats doesn't touch the serializer. Change the ingot, or write code that is
generic over `Format`:

```fe
let cbor = fe_codec_cbor::encode<Order, 64>(order)?

pub fn store<F: Format>(_ format: F, _ order: Order) -> CodecResult<BoundedBuffer<192>> {
    format.encode<Order, 192>(order)
}
```

An order with ID 7, a buyer, a total of 10¹⁸, status `Paid` and the note `"gift"` encodes as
119 bytes of JSON or 43 bytes of CBOR:

```json
{"id":7,"buyer":"0xd8da6bf26964af9d7eed9e03e53415d37aa96045","total":1000000000000000000,"status":"Paid","note":"gift"}
```

```text
a5 01 07 02 54 d8da6bf26964af9d7eed9e03e53415d37aa96045 03 1b 0de0b6b3a7640000 04 01 05 64 67696674
```

## Next

- [Types and limits](types.md): what can be serialized, buffers, errors and schema evolution.
- [EIP-712](eip712.md): hashing and signing typed data.
- [Writing serializers](writing-serializers.md) and [writing a format](writing-a-format.md).
- [Wire profiles](formats.md): exactly what each format writes.
