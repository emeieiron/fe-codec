//! Prints what fe-parser sees in Fe source files, in the format `fe_syntax_dump` prints, so the
//! Fe parser in `tools/syntax` can be checked against fe-parser node by node:
//!
//! ```text
//! cargo run --example syntax_dump -- FILE...
//! ```
//!
//! Offsets are byte offsets. `[...]` holds a node's text as the generator's `compact` gives
//! it, and `-` marks something absent. After a syntax error only its offset is printed.

use fe_parser::ast::prelude::*;
use fe_parser::ast::{
    ArrayType, AttrArgValueKind, AttrList, GenericArgKind, GenericParamKind, ItemKind, PathType,
    Root, Type, VariantKind,
};
use fe_parser::{NodeOrToken, RecoveryMode, SyntaxKind, SyntaxNode, SyntaxToken, parse_source_file};

fn main() {
    for path in std::env::args().skip(1) {
        let text = std::fs::read_to_string(&path).expect("readable source");
        println!("== {path}");
        dump(&text);
    }
}

fn dump(text: &str) {
    let (green, diagnostics) = parse_source_file(text, RecoveryMode::Recover);
    if let Some(error) = diagnostics.first() {
        println!("error {}", u32::from(error.range().start()));
        return;
    }
    let root = Root::cast(SyntaxNode::new_root(green)).expect("a root");
    for item in root.items().into_iter().flatten() {
        match item.kind() {
            Some(ItemKind::Use(item)) => {
                println!("use {} {}", start(item.syntax()), opt(item.use_tree().map(|tree| compact(tree.syntax()))));
            }
            Some(ItemKind::Struct(model)) => {
                let fields = match model.fields() {
                    None => "none",
                    Some(fields) if fields.is_tuple() => "tuple",
                    Some(_) => "record",
                };
                println!(
                    "struct {} {} name={} generics={} where={} fields={fields}",
                    start(model.syntax()),
                    modifiers(model.pub_kw(), model.vis_restriction().map(|v| compact(v.syntax())), model.unsafe_kw()),
                    name(model.name()),
                    model.generic_params().is_some() as u8,
                    model.where_clause().map_or("-".into(), |clause| start(clause.syntax()).to_string()),
                );
                attrs(model.attr_list(), "  ");
                for parameter in model.generic_params().into_iter().flatten() {
                    match parameter.kind() {
                        GenericParamKind::Type(parameter) => println!(
                            "  gtype {} name={} bounds={} default={}",
                            start(parameter.syntax()),
                            name(parameter.name()),
                            opt(parameter.bounds().map(|bounds| {
                                compact(bounds.syntax()).trim_start_matches(':').trim().to_string()
                            })),
                            parameter.default_ty().is_some() as u8,
                        ),
                        GenericParamKind::Const(parameter) => println!(
                            "  gconst {} name={} ty={} default={}",
                            start(parameter.syntax()),
                            name(parameter.name()),
                            opt(parameter.ty().map(|ty| compact(ty.syntax()))),
                            parameter.default_expr().is_some() as u8,
                        ),
                        other => println!("  gother {}", start(other.syntax())),
                    }
                }
                for field in model.fields().into_iter().flatten() {
                    println!(
                        "  field {} {} name={} ty={}",
                        start(field.syntax()),
                        modifiers(field.pub_kw(), field.vis_restriction().map(|v| compact(v.syntax())), field.unsafe_kw()),
                        name(field.name()),
                        field.ty().map_or("-".into(), |ty| format!(
                            "{} [{}] {}",
                            start(ty.syntax()),
                            compact(ty.syntax()),
                            shape(&ty)
                        )),
                    );
                    attrs(field.attr_list(), "    ");
                }
            }
            Some(ItemKind::Enum(model)) => {
                println!(
                    "enum {} {} name={} generics={} where={}",
                    start(model.syntax()),
                    modifiers(model.pub_kw(), model.vis_restriction().map(|v| compact(v.syntax())), model.unsafe_kw()),
                    name(model.name()),
                    model.generic_params().is_some() as u8,
                    model.where_clause().is_some() as u8,
                );
                attrs(model.attr_list(), "  ");
                for variant in model.variants().into_iter().flatten() {
                    let kind = match variant.kind() {
                        VariantKind::Unit => "unit",
                        VariantKind::Tuple(_) => "tuple",
                        VariantKind::Record(_) => "record",
                    };
                    println!("  variant {} name={} kind={kind}", start(variant.syntax()), name(variant.name()));
                    attrs(variant.attr_list(), "    ");
                }
            }
            Some(ItemKind::ImplTrait(implementation)) => {
                println!(
                    "impl_trait {} trait={} ty={}",
                    start(implementation.syntax()),
                    opt(implementation.trait_ref().map(|t| compact(t.syntax()))),
                    implementation
                        .ty()
                        .and_then(|ty| PathType::cast(ty.syntax().clone()))
                        .and_then(|path| path_name(&path))
                        .unwrap_or_else(|| "-".into()),
                );
            }
            _ => println!("other {}", start(item.syntax())),
        }
    }
}

fn attrs(list: Option<AttrList>, indent: &str) {
    let Some(list) = list else { return };
    for attribute in list.normal_attrs() {
        let value = match attribute.value() {
            Some(AttrArgValueKind::Lit(lit)) => int(lit.syntax()),
            Some(AttrArgValueKind::Expr(expr)) => int(expr.syntax()),
            Some(AttrArgValueKind::Ident(_)) => "other".into(),
            None => "-".into(),
        };
        println!(
            "{indent}attr {} path={} args={} value={value}",
            start(attribute.syntax()),
            opt(attribute.path().map(|path| compact(path.syntax()))),
            attribute.args().is_some() as u8,
        );
    }
}

fn int(node: &SyntaxNode) -> String {
    literal_u64(node).map_or("other".into(), |value| format!("int {value}"))
}

fn modifiers(pub_kw: Option<SyntaxToken>, restriction: Option<String>, unsafe_kw: Option<SyntaxToken>) -> String {
    format!(
        "pub={} vis={} unsafe={}",
        pub_kw.is_some() as u8,
        opt(restriction),
        unsafe_kw.is_some() as u8
    )
}

fn name(token: Option<SyntaxToken>) -> String {
    token.map_or("-".into(), |token| format!("{}@{}", token.text(), u32::from(token.text_range().start())))
}

fn opt(text: Option<String>) -> String {
    text.map_or("-".into(), |text| format!("[{text}]"))
}

fn start(node: &SyntaxNode) -> u32 {
    u32::from(node.text_range().start())
}

/// The structure `parse_ty` in `src/schema.rs` extracts from a type.
fn shape(ty: &Type) -> String {
    let site = ty.syntax();
    if let Some(array) = ArrayType::cast(site.clone()) {
        let length = array
            .len()
            .and_then(|length| literal_u64(length.syntax()))
            .map_or("-".into(), |length| length.to_string());
        return match array.elem_ty() {
            Some(element) => format!("array({};{length};[{}])", shape(&element), compact(element.syntax())),
            None => "other".into(),
        };
    }
    let Some(path) = PathType::cast(site.clone()) else {
        return "other".into();
    };
    let Some(name) = path_name(&path) else {
        return "other".into();
    };
    let segments: Vec<_> = path.path().into_iter().flat_map(|path| path.segments()).collect();
    let mut text = format!("path({name}");
    for argument in segments.last().and_then(|last| last.generic_args()).into_iter().flatten() {
        text.push(';');
        match argument.kind() {
            GenericArgKind::Type(argument) => match argument.ty() {
                Some(ty) => text.push_str(&format!("type:{}", shape(&ty))),
                None => text.push_str("type:-"),
            },
            GenericArgKind::Const(argument) => match argument.expr().and_then(|expr| literal_u64(expr.syntax())) {
                Some(value) => text.push_str(&format!("const:{value}")),
                None => text.push_str("const:-"),
            },
            _ => text.push('x'),
        }
    }
    text.push(')');
    text
}

/// `path_parts` in `src/schema.rs`: the joined segment names, unless a segment before the
/// last has generic arguments or a segment has no name.
fn path_name(path: &PathType) -> Option<String> {
    let segments: Vec<_> = path.path()?.segments().collect();
    let (_, parents) = segments.split_last()?;
    if parents.iter().any(|segment| segment.generic_args().is_some()) {
        return None;
    }
    let names: Option<Vec<_>> = segments
        .iter()
        .map(|segment| segment.ident().map(|ident| ident.text().to_string()))
        .collect();
    Some(names?.join("::"))
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

fn compact(node: &SyntaxNode) -> String {
    let mut text = String::new();
    let mut space = false;
    for token in node.descendants_with_tokens().filter_map(NodeOrToken::into_token) {
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
