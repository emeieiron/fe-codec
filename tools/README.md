# Fe tooling for Fe Codec

The generator is written in Fe, so generating serializers needs only the `fe` toolchain:

- `syntax/`: ingot `fe_syntax`, a lexer and parser for Fe declarations (`use` items, structs,
  enums, impl headers). A library with no I/O and no knowledge of Fe Codec.
- `generator/`: ingot `fe_codec_gen`, the native executable. It depends on `fe_syntax`.
- `syntax-dump/`: ingot `fe_syntax_dump`, which prints what `fe_syntax` parses.
- `fe-codec-gen`: a wrapper that builds the generator when its sources change, then runs it.

## Building and testing

```bash
fe build --backend native tools/generator --out-dir target/fe     # → target/fe/fe_codec_gen
fe build --backend native tools/syntax-dump --out-dir target/fe   # → target/fe/fe_syntax_dump
cd tools/syntax && fe test --backend native && fe test             # parser tests, both backends
```

```bash
python3 tests/generator_cases.py   # diagnostics, command line, compiled output
python3 tests/generator_fuzz.py    # random models: wire bytes, Schema, EIP-712, reformatting
python3 tests/parser_vs_fe.py      # the parser against fe check on mutated sources
```

None of these store generator output. `generator_cases.py` checks the cases in
`tests/generator_corpus/`:

- the exact `path:line:col: error: message` of every semantic error;
- syntax errors at the position `fe check` reports;
- successful output compiled with `fe check`.

`generator_fuzz.py` compiles and runs generated code for random models. It compares the JSON
and CBOR bytes with the reference encoders in `tests/vectors`, and EIP-712 hashes with the
specification and the generic path. It also requires that reformatting a model never changes
the output. `parser_vs_fe.py` compares against whichever `fe` is installed, so running it
after a toolchain upgrade checks the parser against the new compiler.

`fe check <dir>` type-checks without running. Ingots that use `std::fs::Fs` set
`target = "native"` in `fe.toml`, because `Fs` exists only on native targets.

## Fe as used here

Read real code before guessing syntax: `core/`, `formats/` and `formats/eip712/` in this repo,
the toolchain's `src/ingots/{core,std}/src`, its language notes in `src/docs/language/`, and
native examples in `src/crates/fe/tests/fixtures/native/*.fe`.

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
