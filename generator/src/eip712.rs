//! Renders static EIP-712 struct hashing for `#[eip712]` structs and the types they use.
//!
//! The generator knows every struct's members, so it computes each `encodeType` string here
//! and emits its type hash as a `core::keccak` constant, which the Fe compiler evaluates.
//! Each `hash_struct` is then one `keccak_words` over the members' words, as a hand-written
//! implementation would be. The output depends only on its inputs, like `render`'s.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;

use crate::render;
use crate::schema::{Diagnostic, Field, Model, Output, Shape, Ty};

const EIP712: &str = "fe_codec_eip712";

pub fn render(output: &Output) -> Result<String, Diagnostic> {
    let models: BTreeMap<&str, &Model> = output
        .models
        .iter()
        .map(|model| (model.name.as_str(), model))
        .collect();
    let roots: Vec<&Model> = output.models.iter().filter(|model| model.eip712).collect();
    if roots.is_empty() {
        return Err(Diagnostic {
            source: 0,
            offset: 0,
            message: "no #[eip712] struct found".into(),
        });
    }

    // Every struct and enum the roots reach, with each struct's members and direct
    // struct dependencies.
    let mut structs: BTreeMap<String, Definition> = BTreeMap::new();
    let mut enums: BTreeSet<String> = BTreeSet::new();
    let mut pending: Vec<&Model> = roots;
    while let Some(model) = pending.pop() {
        if structs.contains_key(&model.name) {
            continue;
        }
        let definition = define(model, &models, &mut enums)?;
        for dependency in &definition.dependencies {
            pending.push(models[dependency.as_str()]);
        }
        structs.insert(model.name.clone(), definition);
    }

    let mut out = String::from(render::HEADER);
    render::imports(&mut out, output);
    for model in &output.models {
        if structs.contains_key(&model.name) {
            render_struct(&mut out, model, &structs);
        } else if enums.contains(&model.name) {
            render_enum(&mut out, model);
        }
    }
    Ok(out)
}

struct Definition {
    /// `Name(type name,...)`.
    text: String,
    /// The structs its members name, directly.
    dependencies: BTreeSet<String>,
}

fn error(model: &Model, offset: usize, message: impl Into<String>) -> Diagnostic {
    Diagnostic {
        source: model.source,
        offset,
        message: message.into(),
    }
}

fn define(
    model: &Model,
    models: &BTreeMap<&str, &Model>,
    enums: &mut BTreeSet<String>,
) -> Result<Definition, Diagnostic> {
    let Shape::Record(fields) = &model.shape else {
        return Err(error(model, model.offset, "#[eip712] applies to structs"));
    };
    if !model.generics.is_empty() {
        return Err(error(
            model,
            model.offset,
            format!(
                "'{}' is generic, so its EIP-712 type depends on its arguments; hash it with the generic path",
                model.name
            ),
        ));
    }
    if model.name == "EIP712Domain" {
        return Err(error(model, model.offset, "EIP712Domain is reserved for the domain"));
    }
    let mut dependencies = BTreeSet::new();
    let mut members = Vec::new();
    for field in fields {
        let ty = eip712_type(&field.kind, models, &mut dependencies, enums)
            .map_err(|message| error(model, field.offset, format!("field '{}': {message}", field.name)))?;
        members.push(format!("{ty} {}", field.name));
    }
    Ok(Definition {
        text: format!("{}({})", model.name, members.join(",")),
        dependencies,
    })
}

/// The EIP-712 type of a field, recording the local structs and enums it names.
fn eip712_type(
    ty: &Ty,
    models: &BTreeMap<&str, &Model>,
    dependencies: &mut BTreeSet<String>,
    enums: &mut BTreeSet<String>,
) -> Result<String, String> {
    match ty {
        Ty::Array { element, length } => {
            let element = eip712_type(element, models, dependencies, enums)?;
            match length {
                Some(length) => Ok(format!("{element}[{length}]")),
                None => Err("EIP-712 array lengths must be integer literals".into()),
            }
        }
        Ty::Path { name, consts, .. } => {
            if let Some(model) = models.get(name.as_str()) {
                return match &model.shape {
                    Shape::Record(_) => {
                        dependencies.insert(name.clone());
                        Ok(name.clone())
                    }
                    Shape::Enum(variants) => {
                        if let Some(variant) = variants.iter().find(|variant| variant.id > 255) {
                            return Err(format!(
                                "enum '{name}' is a uint8 in EIP-712, but variant '{}' has ID {}",
                                variant.name, variant.id
                            ));
                        }
                        enums.insert(name.clone());
                        Ok("uint8".into())
                    }
                };
            }
            let last = name.rsplit("::").next().unwrap_or(name);
            if let Some(integer) = integer(last) {
                return Ok(integer);
            }
            match last {
                "bool" => Ok("bool".into()),
                "Text" => Ok("string".into()),
                "BoundedBuffer" => Ok("bytes".into()),
                "Address" => Ok("address".into()),
                "PackedBytes" => match consts.first() {
                    Some(Some(length)) if (1..=32).contains(length) => Ok(format!("bytes{length}")),
                    _ => Err("PackedBytes<N> is bytesN in EIP-712, so N must be a literal from 1 to 32".into()),
                },
                "Option" => Err("EIP-712 has no optional types".into()),
                _ => Err(format!(
                    "the generator cannot see type '{name}'; pass its module with --source, or hash it with the generic path"
                )),
            }
        }
        Ty::Other => Err("this type has no EIP-712 form".into()),
    }
}

fn integer(name: &str) -> Option<String> {
    let (prefix, bits) = name.split_at(1);
    let bits: u16 = bits.parse().ok()?;
    if !matches!(bits, 8 | 16 | 32 | 64 | 128 | 256) {
        return None;
    }
    match prefix {
        "u" => Some(format!("uint{bits}")),
        "i" => Some(format!("int{bits}")),
        _ => None,
    }
}

/// `encodeType`: the struct's definition, then every struct it reaches, sorted by name.
fn encode_type(name: &str, structs: &BTreeMap<String, Definition>) -> String {
    let mut reached = BTreeSet::new();
    let mut pending: Vec<&str> = structs[name].dependencies.iter().map(String::as_str).collect();
    while let Some(next) = pending.pop() {
        if next != name && reached.insert(next) {
            pending.extend(structs[next].dependencies.iter().map(String::as_str));
        }
    }
    let mut text = structs[name].text.clone();
    for dependency in reached {
        text.push_str(&structs[dependency].text);
    }
    text
}

fn render_struct(out: &mut String, model: &Model, structs: &BTreeMap<String, Definition>) {
    let Shape::Record(fields) = &model.shape else { unreachable!() };
    let name = &model.name;
    writeln!(out, "\nimpl {EIP712}::Eip712Struct for {name} {{").unwrap();
    writeln!(
        out,
        "    const TYPE_HASH: u256 = core::keccak(\"{}\")",
        encode_type(name, structs)
    )
    .unwrap();
    out.push_str("\n    fn hash_struct(self) -> u256 {\n");
    out.push_str("        std::evm::crypto::keccak_words([\n            Self::TYPE_HASH,\n");
    for Field { ty, name, .. } in fields {
        writeln!(
            out,
            "            <{ty} as {EIP712}::Eip712Member>::member(self.{name}),"
        )
        .unwrap();
    }
    out.push_str("        ])\n    }\n}\n");
    writeln!(out, "\nimpl {EIP712}::Eip712Member for {name} {{").unwrap();
    writeln!(
        out,
        "    fn member(self) -> u256 {{\n        <Self as {EIP712}::Eip712Struct>::hash_struct(self)\n    }}\n}}"
    )
    .unwrap();
}

fn render_enum(out: &mut String, model: &Model) {
    let Shape::Enum(variants) = &model.shape else { unreachable!() };
    let name = &model.name;
    writeln!(out, "\nimpl {EIP712}::Eip712Member for {name} {{").unwrap();
    out.push_str("    fn member(self) -> u256 {\n        match self {\n");
    for variant in variants {
        writeln!(out, "            {name}::{} => {},", variant.name, variant.id).unwrap();
    }
    out.push_str("        }\n    }\n}\n");
}
