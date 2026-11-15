use std::collections::{BTreeMap, BTreeSet};

use fe_parser::ast::prelude::*;
use fe_parser::ast::{
    ArrayType, Enum, GenericArgKind, ItemKind, PathType, Root, Struct, Type, VariantKind,
};
use fe_parser::{
    NodeOrToken, RecoveryMode, SyntaxKind, SyntaxNode, SyntaxToken, parse_source_file,
};

const MAX_NAME_BYTES: usize = 64;
const MAX_ARRAY_ELEMENTS: usize = 64;

#[derive(Debug)]
pub struct Diagnostic {
    pub offset: usize,
    pub message: String,
}

impl Diagnostic {
    fn at(node: &SyntaxNode, message: impl Into<String>) -> Self {
        Self {
            offset: u32::from(node.text_range().start()) as usize,
            message: message.into(),
        }
    }
}

type Result<T> = std::result::Result<T, Diagnostic>;

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ValueType {
    Bool,
    Integer { signed: bool, bits: u8 },
    Text(usize),
    Array(Box<ValueType>, usize),
    Named(String),
}

impl ValueType {
    pub fn fe(&self) -> String {
        match self {
            Self::Bool => "bool".into(),
            Self::Integer { signed, bits } => {
                format!("{}{bits}", if *signed { "i" } else { "u" })
            }
            Self::Text(capacity) => format!("Text<{capacity}>"),
            Self::Array(element, length) => format!("[{}; {length}]", element.fe()),
            Self::Named(name) => format!("model_types::{name}"),
        }
    }
}

#[derive(Debug)]
pub struct Field {
    pub name: String,
    pub ty: ValueType,
    pub optional: bool,
    pub id: Option<u64>,
}

#[derive(Debug)]
pub struct Variant {
    pub name: String,
    pub id: Option<u64>,
}

#[derive(Debug)]
pub enum Shape {
    Record(Vec<Field>),
    Enum(Vec<Variant>),
}

pub struct Schema {
    pub models: BTreeMap<String, Shape>,
    pub nodes: Vec<ValueType>,
    indices: BTreeMap<ValueType, usize>,
}

impl Schema {
    pub fn index(&self, ty: &ValueType) -> usize {
        self.indices[ty]
    }

    pub fn model_index(&self, name: &str) -> usize {
        self.index(&ValueType::Named(name.into()))
    }
}

enum Declaration {
    Record(Struct),
    Enum(Enum),
}

struct Extraction<'a> {
    module: &'a str,
    declarations: BTreeMap<String, Declaration>,
    models: BTreeMap<String, Shape>,
    visiting: BTreeSet<String>,
    nodes: BTreeSet<ValueType>,
}

pub fn extract(source: &str, module: &str, roots: &[String]) -> Result<Schema> {
    let (green, diagnostics) = parse_source_file(source, RecoveryMode::Recover);
    if let Some(error) = diagnostics.first() {
        return Err(Diagnostic {
            offset: u32::from(error.range().start()) as usize,
            message: error.msg(),
        });
    }

    let root = Root::cast(SyntaxNode::new_root(green)).expect("Fe parser returned a root");
    let mut declarations = BTreeMap::new();
    for item in root.items().into_iter().flatten() {
        let (name, declaration) = match item.kind() {
            Some(ItemKind::Struct(model)) => (model.name(), Declaration::Record(model)),
            Some(ItemKind::Enum(model)) => (model.name(), Declaration::Enum(model)),
            _ => continue,
        };
        if let Some(name) = name
            && declarations
                .insert(name.text().into(), declaration)
                .is_some()
        {
            return Err(Diagnostic {
                offset: u32::from(name.text_range().start()) as usize,
                message: format!("duplicate declaration '{}'", name.text()),
            });
        }
    }

    let mut extraction = Extraction {
        module,
        declarations,
        models: BTreeMap::new(),
        visiting: BTreeSet::new(),
        nodes: BTreeSet::new(),
    };
    for name in roots.iter().collect::<BTreeSet<_>>() {
        extraction.visit_model(name, root.syntax())?;
    }

    let nodes: Vec<_> = extraction.nodes.into_iter().collect();
    let indices = nodes.iter().cloned().zip(0..).collect();
    Ok(Schema {
        models: extraction.models,
        nodes,
        indices,
    })
}

impl Extraction<'_> {
    fn visit_model(&mut self, name: &str, site: &SyntaxNode) -> Result<()> {
        if self.models.contains_key(name) {
            return Ok(());
        }
        let declaration = self.declarations.get(name).ok_or_else(|| {
            Diagnostic::at(
                site,
                format!("'{name}' must name a supported type declared in this file"),
            )
        })?;
        if !self.visiting.insert(name.into()) {
            return Err(Diagnostic::at(
                site,
                format!("recursive model '{name}' is unsupported"),
            ));
        }
        let shape = match declaration {
            Declaration::Record(model) => self.record(model)?,
            Declaration::Enum(model) => self.enumeration(model)?,
        };
        if let Shape::Record(fields) = &shape {
            for (field, def) in fields.iter().zip(match declaration {
                Declaration::Record(model) => model.fields().expect("validated above"),
                Declaration::Enum(_) => unreachable!(),
            }) {
                self.visit_type(&field.ty, def.syntax())?;
            }
        }

        self.visiting.remove(name);
        self.nodes.insert(ValueType::Named(name.into()));
        self.models.insert(name.into(), shape);
        Ok(())
    }

    fn visit_type(&mut self, ty: &ValueType, site: &SyntaxNode) -> Result<()> {
        match ty {
            ValueType::Named(name) => self.visit_model(name, site)?,
            ValueType::Array(element, _) => self.visit_type(element, site)?,
            _ => {}
        }
        self.nodes.insert(ty.clone());
        Ok(())
    }

    fn record(&self, model: &Struct) -> Result<Shape> {
        self.model_header(model)?;
        let fields = model
            .fields()
            .filter(|fields| !fields.is_tuple())
            .ok_or_else(|| {
                Diagnostic::at(model.syntax(), "expected a record struct with named fields")
            })?;
        let mut seen = BTreeSet::new();
        let mut result = Vec::new();
        for field in fields {
            let id = explicit_id(&field)?;
            if field.pub_kw().is_none()
                || field.vis_restriction().is_some()
                || field.unsafe_kw().is_some()
            {
                return Err(Diagnostic::at(
                    field.syntax(),
                    "fields must be public and safe",
                ));
            }
            let name = checked_name(field.name(), field.syntax())?;
            if !seen.insert(name.clone()) {
                return Err(Diagnostic::at(
                    field.syntax(),
                    format!("duplicate field '{name}'"),
                ));
            }
            let ty = field
                .ty()
                .ok_or_else(|| Diagnostic::at(field.syntax(), "missing field type"))?;
            let optional_type = option_payload(&ty)?;
            let optional = optional_type.is_some();
            let ty = parse_type(&optional_type.unwrap_or(ty))?;
            if id == Some(0) {
                return Err(Diagnostic::at(
                    field.syntax(),
                    "a field codec_id must be positive",
                ));
            }
            result.push(Field {
                name,
                ty,
                optional,
                id,
            });
        }
        validate_ids(result.iter().map(|field| field.id), model.syntax())?;
        Ok(Shape::Record(result))
    }

    fn enumeration(&self, model: &Enum) -> Result<Shape> {
        self.model_header(model)?;
        let mut seen = BTreeSet::new();
        let mut variants = Vec::new();
        for variant in model.variants().into_iter().flatten() {
            let id = explicit_id(&variant)?;
            if !matches!(variant.kind(), VariantKind::Unit) {
                return Err(Diagnostic::at(
                    variant.syntax(),
                    "only unit enum variants are supported",
                ));
            }
            let name = checked_name(variant.name(), variant.syntax())?;
            if !seen.insert(name.clone()) {
                return Err(Diagnostic::at(
                    variant.syntax(),
                    format!("duplicate variant '{name}'"),
                ));
            }
            variants.push(Variant { name, id });
        }
        if variants.is_empty() {
            return Err(Diagnostic::at(
                model.syntax(),
                "empty enums are unsupported",
            ));
        }
        validate_ids(variants.iter().map(|variant| variant.id), model.syntax())?;
        Ok(Shape::Enum(variants))
    }

    fn model_header<T>(&self, model: &T) -> Result<()>
    where
        T: ItemModifierOwner + GenericParamsOwner + WhereClauseOwner + AttrListOwner,
    {
        reject_attributes(model)?;
        if model.pub_kw().is_none()
            || model.vis_restriction().is_some()
            || model.unsafe_kw().is_some()
        {
            return Err(Diagnostic::at(
                model.syntax(),
                "serializable declarations must be public and safe",
            ));
        }
        if model.generic_params().is_some() || model.where_clause().is_some() {
            return Err(Diagnostic::at(
                model.syntax(),
                "generic models are unsupported",
            ));
        }
        let name = model
            .syntax()
            .children_with_tokens()
            .filter_map(NodeOrToken::into_token)
            .find(|token| token.kind() == SyntaxKind::Ident);
        let name = checked_name(name, model.syntax())?;
        if self.module.len() + 2 + name.len() > MAX_NAME_BYTES {
            return Err(Diagnostic::at(
                model.syntax(),
                format!("qualified schema name exceeds {MAX_NAME_BYTES} bytes"),
            ));
        }
        Ok(())
    }
}

fn explicit_id(item: &impl AttrListOwner) -> Result<Option<u64>> {
    let Some(attributes) = item.attr_list() else {
        return Ok(None);
    };
    let mut id = None;
    for attribute in attributes.normal_attrs() {
        if !attribute.is_named("codec_id") {
            return Err(Diagnostic::at(
                attribute.syntax(),
                "attributes on serialization declarations are unsupported",
            ));
        }
        if id.is_some() {
            return Err(Diagnostic::at(
                attribute.syntax(),
                "duplicate codec_id attribute",
            ));
        }
        id = match attribute.value() {
            Some(fe_parser::ast::AttrArgValueKind::Lit(lit)) => literal_u64(lit.syntax()),
            Some(fe_parser::ast::AttrArgValueKind::Expr(expr)) => literal_u64(expr.syntax()),
            _ => None,
        };
        if id.is_none() {
            return Err(Diagnostic::at(
                attribute.syntax(),
                "codec_id must be an unsigned 64-bit integer literal",
            ));
        }
    }
    Ok(id)
}

fn validate_ids(ids: impl Iterator<Item = Option<u64>>, site: &SyntaxNode) -> Result<()> {
    let ids: Vec<_> = ids.collect();
    let assigned: BTreeSet<_> = ids.iter().flatten().collect();
    if assigned.is_empty() {
        return Ok(());
    }
    if ids.iter().any(Option::is_none) {
        return Err(Diagnostic::at(
            site,
            "assign codec_id to every member of an explicitly numbered type",
        ));
    }
    if assigned.len() != ids.len() {
        return Err(Diagnostic::at(site, "duplicate codec_id in type"));
    }
    Ok(())
}

fn reject_attributes(item: &impl AttrListOwner) -> Result<()> {
    if let Some(attribute) = item.attr_list().and_then(|list| list.normal_attrs().next()) {
        return Err(Diagnostic::at(
            attribute.syntax(),
            "attributes on serialization declarations are unsupported",
        ));
    }
    Ok(())
}

fn checked_name(token: Option<SyntaxToken>, site: &SyntaxNode) -> Result<String> {
    let token = token.ok_or_else(|| Diagnostic::at(site, "missing name"))?;
    if token.text().len() > MAX_NAME_BYTES {
        return Err(Diagnostic::at(
            site,
            format!("schema name exceeds {MAX_NAME_BYTES} bytes"),
        ));
    }
    Ok(token.text().into())
}

fn path_parts(path: &PathType) -> Option<(String, Vec<GenericArgKind>)> {
    let segments: Vec<_> = path.path()?.segments().collect();
    let (last, parents) = segments.split_last()?;
    if parents
        .iter()
        .any(|segment| segment.generic_args().is_some())
    {
        return None;
    }
    let names: Option<Vec<_>> = segments
        .iter()
        .map(|segment| segment.ident().map(|ident| ident.text().to_string()))
        .collect();
    let arguments = last
        .generic_args()
        .into_iter()
        .flatten()
        .map(|arg| arg.kind())
        .collect();
    Some((names?.join("::"), arguments))
}

fn option_payload(ty: &Type) -> Result<Option<Type>> {
    let Some((name, arguments)) = PathType::cast(ty.syntax().clone())
        .as_ref()
        .and_then(path_parts)
    else {
        return Ok(None);
    };
    if name != "Option" && name != "core::Option" {
        return Ok(None);
    }
    if let [GenericArgKind::Type(inner)] = arguments.as_slice()
        && let Some(inner) = inner.ty()
    {
        return Ok(Some(inner));
    }
    Err(Diagnostic::at(ty.syntax(), "expected Option<T>"))
}

fn parse_type(ty: &Type) -> Result<ValueType> {
    let site = ty.syntax();
    if let Some(array) = ArrayType::cast(site.clone()) {
        let length = array
            .len()
            .and_then(|length| literal_usize(length.syntax()));
        let length = length.filter(|length| *length <= MAX_ARRAY_ELEMENTS).ok_or_else(|| {
            Diagnostic::at(site, format!("array length must be an integer literal from 0 through {MAX_ARRAY_ELEMENTS}"))
        })?;
        let element = array
            .elem_ty()
            .ok_or_else(|| Diagnostic::at(site, "missing array element type"))?;
        return Ok(ValueType::Array(Box::new(parse_type(&element)?), length));
    }
    if let Some((name, arguments)) = PathType::cast(site.clone()).as_ref().and_then(path_parts) {
        match (name.as_str(), arguments.as_slice()) {
            ("bool", []) => return Ok(ValueType::Bool),
            ("Text" | "fe_codec_core::Text", [GenericArgKind::Const(capacity)]) => {
                let capacity = capacity
                    .expr()
                    .and_then(|expr| literal_usize(expr.syntax()))
                    .ok_or_else(|| {
                        Diagnostic::at(site, "Text capacity must be an integer literal")
                    })?;
                return Ok(ValueType::Text(capacity));
            }
            ("Option" | "core::Option", _) => {
                return Err(Diagnostic::at(
                    site,
                    "Option<T> is supported only as the outer type of a struct field",
                ));
            }
            ("u128" | "u256" | "i128" | "i256" | "usize" | "isize", _) => {
                return Err(Diagnostic::at(
                    site,
                    format!("unsupported integer type '{name}'"),
                ));
            }
            (_, []) if !name.contains("::") => {
                if let Some((signed, bits)) = integer(&name) {
                    return Ok(ValueType::Integer { signed, bits });
                }
                return Ok(ValueType::Named(name));
            }
            _ => {}
        }
    }
    Err(Diagnostic::at(
        site,
        format!("unsupported field type '{}'", site.text()),
    ))
}

fn integer(name: &str) -> Option<(bool, u8)> {
    let bits = name.get(1..)?.parse::<u8>().ok()?;
    match (name.as_bytes()[0], bits) {
        (b'i', 8 | 16 | 32 | 64) => Some((true, bits)),
        (b'u', 8 | 16 | 32 | 64) => Some((false, bits)),
        _ => None,
    }
}

fn literal_usize(node: &SyntaxNode) -> Option<usize> {
    usize::try_from(literal_u64(node)?).ok()
}

fn literal_u64(node: &SyntaxNode) -> Option<u64> {
    let mut tokens = node
        .descendants_with_tokens()
        .filter_map(NodeOrToken::into_token)
        .filter(|token| !token.kind().is_trivia() && token.kind() != SyntaxKind::Newline);
    let token = tokens.next()?;
    if token.kind() != SyntaxKind::Int || tokens.next().is_some() {
        return None;
    }
    let text = token.text().replace('_', "");
    for (prefix, radix) in [("0x", 16), ("0o", 8), ("0b", 2)] {
        if let Some(digits) = text.strip_prefix(prefix) {
            return u64::from_str_radix(digits, radix).ok();
        }
    }
    text.parse().ok()
}
