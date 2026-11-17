# Fe tooling for Fe Codec

This directory holds `fe-codec-gen` rewritten in Fe, so generating serializers needs only the
`fe` toolchain:

- `syntax/` — ingot `fe_syntax`: a lexer and parser for Fe declarations (`use` items, structs,
  enums, impl headers). A library with no I/O and no knowledge of Fe Codec.
- `generator/` — ingot `fe_codec_gen`: the native executable. It ports `generator/src/*.rs`
  module for module and depends on `fe_syntax`.
- `syntax-dump/` — ingot `fe_syntax_dump`: prints what `fe_syntax` parses, in the format of
  `generator/examples/syntax_dump.rs`, which prints what fe-parser parses.
  `tests/syntax_parity.py` requires the two to agree on every Fe file in the repository and in
  the toolchain's `core` and `std` ingots.

The Rust generator in `generator/` is the reference. `tests/generator_parity.py` runs both on
every case in `tests/generator_corpus/` and requires the same results: identical output on
success, identical `path:line:col: error: message` text on semantic errors, and for syntax
errors a failure with the same location prefix.

## Building and testing

```bash
export PATH=$HOME/.fe/bin:$PATH                                    # fe 26.6.0; femp too, for the examples
fe build --backend native tools/generator --out-dir target/fe     # → target/fe/fe_codec_gen
cd tools/syntax && fe test --backend native && fe test             # parser tests, both backends
fe build --backend native tools/syntax-dump --out-dir target/fe   # → target/fe/fe_syntax_dump
cargo build --locked --example syntax_dump                         # → the fe-parser reference
python3 tests/syntax_parity.py --rust target/debug/examples/syntax_dump --fe target/fe/fe_syntax_dump
cargo build --locked                                               # → target/debug/fe-codec-gen, the reference
```

`fe check <dir>` type-checks without running. Ingots that use `std::fs::Fs` set
`target = "native"` in `fe.toml`, because `Fs` exists only on native targets.

## Fe as used here

Read real code before guessing syntax: `core/`, `formats/` and `formats/eip712/` in this repo,
the toolchain's `~/.femp/toolchains/fe-26.6.0/src/ingots/{core,std}/src`, its language notes in
`src/docs/language/`, and native examples in `src/crates/fe/tests/fixtures/native/*.fe`.

- No semicolons. Statements end at the newline.
- Parameters declared `name: T` must be labeled at the call (`write_word(offset: 0, value: x)`);
  declare `_ name: T` to pass positionally.
- Generic arguments are all or nothing: `f<A, B>(x)`, never `f<A>(x)` for `f<A, const N>`.
- `as` converts only when lossless. Otherwise `use core::num::IntDowncast` and call
  `.downcast_truncate()` or `.downcast()` (an `Option`).
- `Result<E, T>`: the error type comes first. `?` propagates it.
- `contract`, `msg`, `recv`, `uses`, `with`, `own`, `ref` and `var` are keywords; don't use them
  as names.
- `let` binds, `var` binds mutably. A `mut` parameter is passed as `mut x` at the call.
- Effects: `fn f() uses (fs: Fs)`; a function that writes takes `mut Fs`. `main` is
  `pub fn main() -> i32 uses (out: mut Stdout, err: mut Stderr, args: Args, fs: mut Fs)`.
- Text: `format!(a, b, ...)` concatenates `Display` values into a `MemBuffer`;
  `println!`/`eprintln!` print them. A string literal is a `[u8; N]`
  (`let word: [u8; 3] = "use"`).
- Memory: `MemBuffer` (bytes: `new`, `push`, `len`, `read_be<u8>`, `slice(offset:, len:)`) and
  `MemVec<T>` (growable). Store trees as arenas of nodes addressed by index, not as recursive
  boxes.
- `fe test` runs `#[test]` functions on `sonatina` (EVM) by default and on `--backend native`.
  There is no wasm backend for `fe test`.

## Rules for changes here

- No `unsafe`, no new dependencies, and no edits outside the files your task names.
- Copy diagnostic messages verbatim from the Rust source; parity compares them.
- Commit messages describe the change in plain sentences and carry no co-author, "Generated
  with" or other attribution lines.
- If a Fe limitation blocks something, stop and report it rather than working around it.
