mod eip712;
mod render;
mod schema;

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Parser;
use fe_parser::parser::token_stream::{LexicalToken, TokenStream};

#[derive(Parser)]
#[command(
    version,
    about = "Generate Fe Codec impls for #[serializable] structs and enums",
    long_about = "Generate Fe Codec impls for #[serializable] structs and enums.\n\n\
        With --out, writes (or with --check, verifies) a module beside the sources. With \
        --stdout, prints the module for a build system to place: this is the FSGP v1 \
        generator mode, which writes no files."
)]
struct Args {
    /// A top-level module of the ingot that declares models. Repeat for several modules.
    #[arg(long, required = true)]
    source: Vec<PathBuf>,

    /// The sibling module to create or update.
    #[arg(long, required_unless_present = "stdout")]
    out: Option<PathBuf>,

    /// Print the module instead of writing it.
    #[arg(long, conflicts_with_all = ["out", "check"])]
    stdout: bool,

    /// Report a missing or stale --out module without writing it.
    #[arg(long)]
    check: bool,

    /// Generate static EIP-712 struct hashing for #[eip712] structs instead of serializers.
    /// The module needs the fe_codec_eip712 ingot.
    #[arg(long)]
    eip712: bool,
}

type Result<T> = std::result::Result<T, String>;

fn main() -> ExitCode {
    match run(Args::parse()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: Args) -> Result<()> {
    let mut modules = Vec::new();
    let mut parent = None;
    for path in &args.source {
        let module = module_name(path)?;
        let dir = source_dir(path)?;
        if parent.get_or_insert_with(|| dir.clone()) != &dir {
            return Err("error: every --source must be in the same ingot's src directory".into());
        }
        if modules.contains(&module) {
            return Err(format!("{}: error: source given twice", path.display()));
        }
        modules.push(module);
    }
    let parent = parent.expect("clap requires a source");
    if let Some(path) = &args.out {
        let module = module_name(path)?;
        if parent != source_dir(path)? || modules.contains(&module) {
            return Err(
                "error: the output must be a different module beside the sources".into(),
            );
        }
    }

    let texts = args
        .source
        .iter()
        .map(|path| fs::read_to_string(path).map_err(|error| at_path(path, error)))
        .collect::<Result<Vec<_>>>()?;
    let sources: Vec<_> = modules
        .iter()
        .zip(&texts)
        .map(|(module, text)| schema::Source { module, text })
        .collect();
    let schema = schema::extract(&sources).map_err(|error| {
        let index = error.source;
        diagnostic(&args.source[index], &texts[index], error)
    })?;
    let output = if args.eip712 {
        eip712::render(&schema).map_err(|error| {
            let index = error.source;
            diagnostic(&args.source[index], &texts[index], error)
        })?
    } else {
        render::render(&schema)
    };
    if args.stdout {
        io::stdout()
            .lock()
            .write_all(output.as_bytes())
            .map_err(|error| format!("cannot write generated source: {error}"))
    } else {
        let path = args.out.expect("required by the command line");
        write_output(&path, &parent, output.as_bytes(), args.check)
    }
}

fn module_name(path: &Path) -> Result<&str> {
    let name = path
        .file_stem()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    let mut bytes = name.bytes();
    if path.extension().and_then(|ext| ext.to_str()) != Some("fe")
        || matches!(name, "_" | "lib")
        || !bytes
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic() || c == b'_')
        || !bytes.all(|c| c.is_ascii_alphanumeric() || c == b'_')
        || !fe_parser::lexer::Lexer::new(name)
            .next()
            .is_some_and(|token| token.syntax_kind() == fe_parser::SyntaxKind::Ident)
    {
        return Err(format!(
            "{}: error: expected a named Fe module file",
            path.display()
        ));
    }
    Ok(name)
}

fn source_dir(path: &Path) -> Result<PathBuf> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let dir = parent
        .canonicalize()
        .map_err(|error| at_path(parent, error))?;
    if dir.file_name().and_then(|name| name.to_str()) != Some("src")
        || !dir
            .parent()
            .is_some_and(|parent| parent.join("fe.toml").is_file())
    {
        return Err(format!(
            "{}: error: expected a top-level module in an ingot's src directory",
            path.display()
        ));
    }
    Ok(dir)
}

fn write_output(path: &Path, dir: &Path, output: &[u8], check: bool) -> Result<()> {
    let existing = match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_file() => {
            Some(fs::read(path).map_err(|error| at_path(path, error))?)
        }
        Ok(_) => {
            return Err(format!(
                "{}: error: expected a regular output file",
                path.display()
            ));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(at_path(path, error)),
    };
    if existing.as_deref() == Some(output) {
        return Ok(());
    }
    if check {
        return Err(format!(
            "{}: error: generated serializers are missing or stale",
            path.display()
        ));
    }
    if existing
        .as_ref()
        .is_some_and(|bytes| !bytes.starts_with(render::HEADER.as_bytes()))
    {
        return Err(format!(
            "{}: error: refusing to replace handwritten source",
            path.display()
        ));
    }

    let mut temp = tempfile::NamedTempFile::new_in(dir).map_err(|error| at_path(path, error))?;
    temp.write_all(output)
        .map_err(|error| at_path(path, error))?;
    if existing.is_some() {
        temp.persist(path)
    } else {
        temp.persist_noclobber(path)
    }
    .map_err(|error| at_path(path, error))?;
    Ok(())
}

fn at_path(path: &Path, error: impl std::fmt::Display) -> String {
    format!("{}: error: {error}", path.display())
}

fn diagnostic(path: &Path, source: &str, error: schema::Diagnostic) -> String {
    let bytes = &source.as_bytes()[..error.offset.min(source.len())];
    let line = bytes.split(|byte| *byte == b'\n').count();
    let column = bytes
        .rsplit(|byte| *byte == b'\n')
        .next()
        .map_or(1, |line| line.len() + 1);
    format!("{}:{line}:{column}: error: {}", path.display(), error.message)
}
