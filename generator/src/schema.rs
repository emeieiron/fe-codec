//! Reads `#[serializable]` declarations from Fe source files.
//!
//! The generator does not resolve types. Field types are copied into the generated module,
//! which imports everything its source modules import, and the Fe compiler checks that each
//! one implements the codec traits. The checks here are the ones the compiler cannot make or
//! reports poorly: unsupported integer widths, values that cannot round-trip, local types
//! that were not marked serializable, and arrays that need `Copy` elements.

use std::collections::{BTreeMap, BTreeSet};

use fe_parser::ast::prelude::*;
use fe_parser::ast::{
    ArrayType, Enum, GenericArgKind, GenericParamKind, ItemKind, PathType, Root,
    Struct, Type, VariantKind,
};
use fe_parser::{NodeOrToken, RecoveryMode, SyntaxKind, SyntaxNode, SyntaxToken, parse_source_file};

const MAX_NAME_BYTES: usize = 31;
/// Literal-length arrays of non-`Copy` elements are decoded element by element up to this
/// length.
pub const MAX_UNROLLED_ELEMENTS: usize = 64;

#[derive(Debug)]
pub struct Diagnostic {
    /// Index of the source file the offset refers to.
    pub source: usize,
    pub offset: usize,
    pub message: String,
}

type Result<T> = std::result::Result<T, Diagnostic>;

/// One input file: its module name and text.
pub struct Source<'a> {
    pub module: &'a str,
    pub text: &'a str,
}

/// Everything the renderer needs.
pub struct Output {
    /// Source module names, imported by glob.
    pub modules: Vec<String>,
    /// The sources' own `use` items, which field types may rely on.
    pub uses: Vec<String>,
    pub models: Vec<Model>,
}

pub struct Model {
    pub name: String,
    pub generics: Vec<Generic>,
    /// Type parameters whose decoding builds arrays of them, so they must be `Copy`.
    pub copy_params: BTreeSet<String>,
    pub shape: Shape,
}

pub enum Generic {
    Type { name: String, bounds: Option<String> },
    Const { name: String, ty: String },
}

impl Generic {
    pub fn name(&self) -> &str {
        match self {
            Self::Type { name, .. } | Self::Const { name, .. } => name,
        }
    }
}

pub enum Shape {
    /// Fields in ascending ID order, which is their schema position.
    Record(Vec<Field>),
    Enum(Vec<Variant>),
}

pub struct Field {
    pub name: String,
    /// The declared type, as written.
    pub ty: String,
    pub optional: bool,
    pub id: u64,
    /// For a literal-length array decoded element by element: the element type and length.
    pub unrolled: Option<(String, usize)>,
}

pub struct Variant {
    pub name: String,
    pub id: u64,
}

/// A local struct or enum, whether it is serializable and whether it implements `Copy`.
struct Local {
    serializable: bool,
    copy: bool,
}

pub fn extract(sources: &[Source]) -> Result<Output> {
    let mut roots = Vec::new();
    for (index, source) in sources.iter().enumerate() {
        let (green, diagnostics) = parse_source_file(source.text, RecoveryMode::Recover);
        if let Some(error) = diagnostics.first() {
            return Err(Diagnostic {
                source: index,
                offset: u32::from(error.range().start()) as usize,
                message: error.msg(),
            });
        }
        roots.push(Root::cast(SyntaxNode::new_root(green)).expect("Fe parser returned a root"));
    }

    // Every struct and enum declared in the sources, and which implement `Copy`.
    let mut locals: BTreeMap<String, Local> = BTreeMap::new();
    let mut copies = BTreeSet::new();
    for (index, root) in roots.iter().enumerate() {
        for item in root.items().into_iter().flatten() {
            let (name, serializable) = match item.kind() {
                Some(ItemKind::Struct(model)) => (model.name(), serializable(&model, index)?),
                Some(ItemKind::Enum(model)) => (model.name(), serializable(&model, index)?),
                Some(ItemKind::ImplTrait(implementation)) => {
                    let is_copy = implementation.trait_ref().is_some_and(|trait_ref| {
                        let text = compact(trait_ref.syntax());
                        text == "Copy" || text.ends_with("::Copy")
                    });
                    if is_copy
                        && let Some(name) = implementation
                            .ty()
                            .and_then(|ty| PathType::cast(ty.syntax().clone()))
                            .and_then(|path| path_parts(&path))
                            .map(|(name, _)| name)
                    {
                        copies.insert(name);
                    }
                    continue;
                }
                _ => continue,
            };
            let Some(name) = name else { continue };
            let local = Local {
                serializable,
                copy: false,
            };
            if locals.insert(name.text().into(), local).is_some() {
                return Err(Diagnostic {
                    source: index,
                    offset: start(&name),
                    message: format!("duplicate declaration '{}'", name.text()),
                });
            }
        }
    }
    for name in &copies {
        if let Some(local) = locals.get_mut(name) {
            local.copy = true;
        }
    }

    let mut output = Output {
        modules: sources.iter().map(|source| source.module.to_string()).collect(),
        uses: Vec::new(),
        models: Vec::new(),
    };
    for (index, root) in roots.iter().enumerate() {
        let extraction = Extraction {
            source: index,
            locals: &locals,
        };
        for item in root.items().into_iter().flatten() {
            match item.kind() {
                Some(ItemKind::Use(item)) => {
                    if let Some(tree) = item.use_tree() {
                        let text = format!("use {}", compact(tree.syntax()));
                        if !output.uses.contains(&text) {
                            output.uses.push(text);
                        }
                    }
                }
                Some(ItemKind::Struct(model)) if serializable(&model, index)? => {
                    output.models.push(extraction.record(&model)?);
                }
                Some(ItemKind::Enum(model)) if serializable(&model, index)? => {
                    output.models.push(extraction.enumeration(&model)?);
                }
                _ => {}
            }
        }
    }
    if output.models.is_empty() {
        return Err(Diagnostic {
            source: 0,
            offset: 0,
            message: "no #[serializable] struct or enum found".into(),
        });
    }
    Ok(output)
}

/// Whether `item` carries `#[serializable]`, which takes no arguments.
fn serializable(item: &impl AttrListOwner, source: usize) -> Result<bool> {
    let Some(attributes) = item.attr_list() else {
        return Ok(false);
    };
    let mut found = false;
    for attribute in attributes.normal_attrs() {
        if attribute.is_named("serializable") {
            if attribute.args().is_some() || attribute.value().is_some() {
                return Err(at(source, attribute.syntax(), "#[serializable] takes no arguments"));
            }
            found = true;
        } else if attribute.is_named("codec_id") {
            return Err(at(
                source,
                attribute.syntax(),
                "codec_id belongs on fields and enum variants",
            ));
        }
    }
    Ok(found)
}

struct Extraction<'a> {
    source: usize,
    locals: &'a BTreeMap<String, Local>,
}

/// Whether values of a type are known to be `Copy`.
#[derive(Clone, Copy, PartialEq)]
enum Copyness {
    Yes,
    No,
    /// A type the generator cannot see, such as one from another module.
    Unknown,
}

/// The parts of a field type the checks inspect.
enum Ty {
    Path { name: String, args: Vec<Option<Ty>> },
    Array { element: Box<Ty>, length: Option<usize> },
    Other,
}

impl Extraction<'_> {
    fn error(&self, site: &SyntaxNode, message: impl Into<String>) -> Diagnostic {
        at(self.source, site, message)
    }

    fn record(&self, model: &Struct) -> Result<Model> {
        let name = self.header(model, model.name())?;
        let generics = self.generics(model)?;
        let fields = model
            .fields()
            .filter(|fields| !fields.is_tuple())
            .ok_or_else(|| self.error(model.syntax(), "expected a struct with named fields"))?;
        let parameters: BTreeSet<String> = generics
            .iter()
            .filter(|generic| matches!(generic, Generic::Type { .. }))
            .map(|generic| generic.name().to_string())
            .collect();
        let mut copy_params = BTreeSet::new();
        let mut seen = BTreeSet::new();
        let mut result = Vec::new();
        let mut ids = Vec::new();
        for field in fields {
            ids.push(self.explicit_id(&field)?);
            let visible = field.pub_kw().is_some()
                && field
                    .vis_restriction()
                    .is_none_or(|restriction| compact(restriction.syntax()).contains("ingot"));
            if !visible || field.unsafe_kw().is_some() {
                return Err(self.error(
                    field.syntax(),
                    "serializable fields must be `pub` or `pub(ingot)`, and safe",
                ));
            }
            let field_name = self.name(field.name(), field.syntax())?;
            if !seen.insert(field_name.clone()) {
                return Err(self.error(field.syntax(), format!("duplicate field '{field_name}'")));
            }
            let declared = field
                .ty()
                .ok_or_else(|| self.error(field.syntax(), "missing field type"))?;
            let ty = parse_ty(&declared);
            self.check(&ty, &name, declared.syntax())?;
            let optional = option_payload(&ty).is_some();
            let unrolled = if optional {
                self.nested_arrays(&ty, &parameters, &mut copy_params, declared.syntax())?;
                None
            } else {
                self.top_level(&ty, &declared, &parameters, &mut copy_params)?
            };
            result.push(Field {
                name: field_name,
                ty: compact(declared.syntax()),
                optional,
                id: 0,
                unrolled,
            });
        }
        let ids = self.resolve_ids(&ids, 1, model.syntax())?;
        for (field, id) in result.iter_mut().zip(ids) {
            field.id = id;
        }
        result.sort_by_key(|field| field.id);
        Ok(Model {
            name,
            generics,
            copy_params,
            shape: Shape::Record(result),
        })
    }

    fn enumeration(&self, model: &Enum) -> Result<Model> {
        let name = self.header(model, model.name())?;
        if model.generic_params().is_some() || model.where_clause().is_some() {
            return Err(self.error(model.syntax(), "generic enums are unsupported"));
        }
        let mut seen = BTreeSet::new();
        let mut variants = Vec::new();
        let mut ids = Vec::new();
        for variant in model.variants().into_iter().flatten() {
            ids.push(self.explicit_id(&variant)?);
            if !matches!(variant.kind(), VariantKind::Unit) {
                return Err(self.error(
                    variant.syntax(),
                    "only unit enum variants are supported",
                ));
            }
            let variant_name = self.name(variant.name(), variant.syntax())?;
            if !seen.insert(variant_name.clone()) {
                return Err(self.error(
                    variant.syntax(),
                    format!("duplicate variant '{variant_name}'"),
                ));
            }
            variants.push(Variant {
                name: variant_name,
                id: 0,
            });
        }
        if variants.is_empty() {
            return Err(self.error(model.syntax(), "empty enums are unsupported"));
        }
        let ids = self.resolve_ids(&ids, 0, model.syntax())?;
        for (variant, id) in variants.iter_mut().zip(ids) {
            variant.id = id;
        }
        Ok(Model {
            name,
            generics: Vec::new(),
            copy_params: BTreeSet::new(),
            shape: Shape::Enum(variants),
        })
    }

    fn header<T>(&self, model: &T, name: Option<SyntaxToken>) -> Result<String>
    where
        T: ItemModifierOwner,
    {
        let visible = model.pub_kw().is_some()
            && model
                .vis_restriction()
                .is_none_or(|restriction| compact(restriction.syntax()).contains("ingot"));
        if !visible || model.unsafe_kw().is_some() {
            return Err(self.error(
                model.syntax(),
                "serializable declarations must be `pub` or `pub(ingot)`, and safe",
            ));
        }
        self.name(name, model.syntax())
    }

    fn generics(&self, model: &Struct) -> Result<Vec<Generic>> {
        if let Some(clause) = model.where_clause() {
            return Err(self.error(
                clause.syntax(),
                "where clauses on serializable structs are unsupported",
            ));
        }
        let mut generics = Vec::new();
        for parameter in model.generic_params().into_iter().flatten() {
            let generic = match parameter.kind() {
                GenericParamKind::Type(parameter) if parameter.default_ty().is_none() => {
                    let name = parameter.name().ok_or_else(|| {
                        self.error(parameter.syntax(), "missing generic parameter name")
                    })?;
                    if name.text() == "CodecEncoder" || name.text() == "CodecDecoder" {
                        return Err(self.error(
                            parameter.syntax(),
                            "this parameter name is reserved for generated code",
                        ));
                    }
                    Generic::Type {
                        name: name.text().to_string(),
                        bounds: parameter.bounds().map(|bounds| {
                            compact(bounds.syntax()).trim_start_matches(':').trim().to_string()
                        }),
                    }
                }
                GenericParamKind::Const(parameter) if parameter.default_expr().is_none() => {
                    let name = parameter.name().ok_or_else(|| {
                        self.error(parameter.syntax(), "missing generic parameter name")
                    })?;
                    let ty = parameter.ty().ok_or_else(|| {
                        self.error(parameter.syntax(), "missing const parameter type")
                    })?;
                    Generic::Const {
                        name: name.text().to_string(),
                        ty: compact(ty.syntax()),
                    }
                }
                other => {
                    return Err(self.error(
                        other.syntax(),
                        "only type and const generic parameters without defaults are supported",
                    ));
                }
            };
            generics.push(generic);
        }
        Ok(generics)
    }

    /// Rejects types that cannot be serialized faithfully.
    fn check(&self, ty: &Ty, model: &str, site: &SyntaxNode) -> Result<()> {
        match ty {
            Ty::Path { name, args } => {
                if name == "usize" || name == "isize" {
                    return Err(self.error(
                        site,
                        format!(
                            "'{name}' has a different width on each target; use a fixed-width integer"
                        ),
                    ));
                }
                if let Some(Ty::Path { name: inner, .. }) = option_payload(ty)
                    && is_option(inner)
                {
                    return Err(self.error(
                        site,
                        "Option<Option<T>> cannot round-trip: Some(None) and None encode alike",
                    ));
                }
                if let Some(local) = self.locals.get(name)
                    && !local.serializable
                {
                    return Err(self.error(
                        site,
                        format!("'{name}' is used by '{model}' but is not #[serializable]"),
                    ));
                }
                for arg in args.iter().flatten() {
                    self.check(arg, model, site)?;
                }
                Ok(())
            }
            Ty::Array { element, .. } => self.check(element, model, site),
            Ty::Other => Ok(()),
        }
    }

    fn copyness(&self, ty: &Ty, parameters: &BTreeSet<String>) -> Copyness {
        match ty {
            Ty::Path { name, args } => {
                let last = name.rsplit("::").next().unwrap_or(name);
                if is_primitive(last)
                    || matches!(last, "Text" | "BoundedBuffer" | "PackedBytes" | "Address")
                {
                    return Copyness::Yes;
                }
                if is_option(name) {
                    return match args.first() {
                        Some(Some(inner)) => self.copyness(inner, parameters),
                        _ => Copyness::Unknown,
                    };
                }
                if parameters.contains(name) {
                    return Copyness::Unknown;
                }
                match self.locals.get(name) {
                    Some(local) if local.copy => Copyness::Yes,
                    Some(_) => Copyness::No,
                    None => Copyness::Unknown,
                }
            }
            Ty::Array { element, .. } => self.copyness(element, parameters),
            Ty::Other => Copyness::Unknown,
        }
    }

    /// Type parameters an array of `element` makes `Copy`, and an error for a local
    /// non-`Copy` element.
    fn array_element(
        &self,
        element: &Ty,
        parameters: &BTreeSet<String>,
        copy_params: &mut BTreeSet<String>,
        site: &SyntaxNode,
    ) -> Result<()> {
        if let Ty::Path { name, .. } = element
            && parameters.contains(name)
        {
            copy_params.insert(name.clone());
            return Ok(());
        }
        if self.copyness(element, parameters) == Copyness::No {
            return Err(self.error(
                site,
                "decoding this array builds it from copies of its first element; implement Copy for the element type",
            ));
        }
        Ok(())
    }

    /// Checks every array inside `ty`, which the trait impls decode.
    fn nested_arrays(
        &self,
        ty: &Ty,
        parameters: &BTreeSet<String>,
        copy_params: &mut BTreeSet<String>,
        site: &SyntaxNode,
    ) -> Result<()> {
        match ty {
            Ty::Path { args, .. } => {
                for arg in args.iter().flatten() {
                    self.nested_arrays(arg, parameters, copy_params, site)?;
                }
                Ok(())
            }
            Ty::Array { element, .. } => {
                self.array_element(element, parameters, copy_params, site)?;
                self.nested_arrays(element, parameters, copy_params, site)
            }
            Ty::Other => Ok(()),
        }
    }

    /// Chooses how a required field's value is decoded. A literal-length array whose
    /// elements may not be `Copy`, or that is empty, is decoded element by element.
    fn top_level(
        &self,
        ty: &Ty,
        declared: &Type,
        parameters: &BTreeSet<String>,
        copy_params: &mut BTreeSet<String>,
    ) -> Result<Option<(String, usize)>> {
        let site = declared.syntax();
        let Ty::Array { element, length } = ty else {
            self.nested_arrays(ty, parameters, copy_params, site)?;
            return Ok(None);
        };
        self.nested_arrays(element, parameters, copy_params, site)?;
        let is_parameter = matches!(&**element, Ty::Path { name, .. } if parameters.contains(name));
        let copyness = self.copyness(element, parameters);
        let unroll = match length {
            Some(0) => true,
            Some(length) => {
                !is_parameter && copyness != Copyness::Yes && *length <= MAX_UNROLLED_ELEMENTS
            }
            None => false,
        };
        if !unroll {
            self.array_element(element, parameters, copy_params, site)?;
            return Ok(None);
        }
        let element_text = ArrayType::cast(site.clone())
            .and_then(|array| array.elem_ty())
            .map(|element| compact(element.syntax()))
            .ok_or_else(|| self.error(site, "missing array element type"))?;
        Ok(Some((element_text, length.unwrap_or(0))))
    }

    fn explicit_id(&self, item: &impl AttrListOwner) -> Result<Option<u64>> {
        let Some(attributes) = item.attr_list() else {
            return Ok(None);
        };
        let mut id = None;
        for attribute in attributes.normal_attrs() {
            let path = attribute
                .path()
                .map(|path| compact(path.syntax()))
                .unwrap_or_default();
            if path.starts_with("codec") && path != "codec_id" || path == "serializable" {
                return Err(self.error(
                    attribute.syntax(),
                    format!("unknown codec attribute '{path}'; fields and variants take codec_id"),
                ));
            }
            if path != "codec_id" {
                continue;
            }
            if id.is_some() {
                return Err(self.error(attribute.syntax(), "duplicate codec_id attribute"));
            }
            id = match attribute.value() {
                Some(fe_parser::ast::AttrArgValueKind::Lit(lit)) => literal_u64(lit.syntax()),
                Some(fe_parser::ast::AttrArgValueKind::Expr(expr)) => literal_u64(expr.syntax()),
                _ => None,
            };
            if id.is_none() {
                return Err(self.error(
                    attribute.syntax(),
                    "codec_id must be an unsigned 64-bit integer literal",
                ));
            }
        }
        Ok(id)
    }

    /// Explicit IDs, which every member must have when one does, or declaration positions
    /// counted from `first`.
    fn resolve_ids(&self, ids: &[Option<u64>], first: u64, site: &SyntaxNode) -> Result<Vec<u64>> {
        if ids.iter().all(Option::is_none) {
            return Ok((first..).take(ids.len()).collect());
        }
        if ids.iter().any(Option::is_none) {
            return Err(self.error(
                site,
                "assign codec_id to every member of an explicitly numbered type",
            ));
        }
        let ids: Vec<u64> = ids.iter().flatten().copied().collect();
        if ids.iter().collect::<BTreeSet<_>>().len() != ids.len() {
            return Err(self.error(site, "duplicate codec_id in type"));
        }
        if first == 1 && ids.contains(&0) {
            return Err(self.error(site, "a field codec_id must be positive"));
        }
        Ok(ids)
    }

    fn name(&self, token: Option<SyntaxToken>, site: &SyntaxNode) -> Result<String> {
        let token = token.ok_or_else(|| self.error(site, "missing name"))?;
        if token.text().len() > MAX_NAME_BYTES {
            return Err(self.error(site, format!("schema name exceeds {MAX_NAME_BYTES} bytes")));
        }
        Ok(token.text().into())
    }
}

fn at(source: usize, site: &SyntaxNode, message: impl Into<String>) -> Diagnostic {
    Diagnostic {
        source,
        offset: u32::from(site.text_range().start()) as usize,
        message: message.into(),
    }
}

fn start(token: &SyntaxToken) -> usize {
    u32::from(token.text_range().start()) as usize
}

/// The node's text with comments dropped and whitespace runs collapsed to one space.
fn compact(node: &SyntaxNode) -> String {
    let mut text = String::new();
    let mut space = false;
    for token in node
        .descendants_with_tokens()
        .filter_map(NodeOrToken::into_token)
    {
        if token.kind() == SyntaxKind::Comment || token.kind() == SyntaxKind::DocComment {
            continue;
        }
        if token.kind().is_trivia() || token.kind() == SyntaxKind::Newline {
            space = !text.is_empty();
            continue;
        }
        if space {
            text.push(' ');
            space = false;
        }
        text.push_str(token.text());
    }
    text
}

fn is_option(name: &str) -> bool {
    matches!(name, "Option" | "core::Option" | "core::option::Option")
}

fn is_primitive(name: &str) -> bool {
    matches!(
        name,
        "bool" | "u8" | "u16" | "u32" | "u64" | "u128" | "u256" | "i8" | "i16" | "i32" | "i64"
            | "i128" | "i256"
    )
}

fn option_payload(ty: &Ty) -> Option<&Ty> {
    match ty {
        Ty::Path { name, args } if is_option(name) => match args.as_slice() {
            [Some(inner)] => Some(inner),
            _ => None,
        },
        _ => None,
    }
}

fn path_parts(path: &PathType) -> Option<(String, Vec<GenericArgKind>)> {
    let segments: Vec<_> = path.path()?.segments().collect();
    let (last, parents) = segments.split_last()?;
    if parents.iter().any(|segment| segment.generic_args().is_some()) {
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

fn parse_ty(ty: &Type) -> Ty {
    let site = ty.syntax();
    if let Some(array) = ArrayType::cast(site.clone()) {
        let length = array
            .len()
            .and_then(|length| literal_u64(length.syntax()))
            .and_then(|length| usize::try_from(length).ok());
        return match array.elem_ty() {
            Some(element) => Ty::Array {
                element: Box::new(parse_ty(&element)),
                length,
            },
            None => Ty::Other,
        };
    }
    if let Some((name, arguments)) = PathType::cast(site.clone()).as_ref().and_then(path_parts) {
        let args = arguments
            .iter()
            .map(|argument| match argument {
                GenericArgKind::Type(argument) => argument.ty().map(|ty| parse_ty(&ty)),
                _ => None,
            })
            .collect();
        return Ty::Path { name, args };
    }
    Ty::Other
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
