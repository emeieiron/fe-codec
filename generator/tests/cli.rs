use std::fs;
use std::path::PathBuf;
use std::process::{Command, Output};

struct Ingot {
    _dir: tempfile::TempDir,
    source: PathBuf,
    output: PathBuf,
}

impl Ingot {
    fn new(source: &str) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("src");
        fs::create_dir(&src).unwrap();
        fs::write(
            dir.path().join("fe.toml"),
            "[ingot]\nname = \"test_models\"\n",
        )
        .unwrap();
        let source_path = src.join("models.fe");
        fs::write(&source_path, source).unwrap();
        Self {
            _dir: dir,
            source: source_path,
            output: src.join("serializers.fe"),
        }
    }

    fn generate(&self, roots: &[&str], check: bool) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_fe-codec-gen"));
        command
            .arg("--source")
            .arg(&self.source)
            .arg("--out")
            .arg(&self.output);
        for root in roots {
            command.args(["--type", root]);
        }
        if check {
            command.arg("--check");
        }
        command.output().unwrap()
    }

    fn generated(&self) -> String {
        fs::read_to_string(&self.output).unwrap()
    }
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn success(output: Output) {
    assert!(output.status.success(), "{}", stderr(&output));
}

#[test]
fn discovers_transitive_models_and_generates_deterministically() {
    let ingot = Ingot::new(
        r#"
        use fe_codec_core::Text

        pub struct Batch { pub children: [Item; 0x02] }
        pub struct Item {
            pub kind: Kind,
            pub label: Option<Text<8>>,
        }
        pub enum Kind { First, Second }
        pub struct Ignored { private: u256 }
        "#,
    );
    success(ingot.generate(&["Batch", "Kind"], false));
    let first = ingot.generated();
    for ty in ["BatchSerializer", "ItemSerializer", "KindSerializer"] {
        assert!(first.contains(&format!("pub struct {ty}")));
    }
    assert!(!first.contains("IgnoredSerializer"));
    assert!(!first.contains("fe_codec_json"));
    assert!(!first.contains("fe_codec_cbor"));
    assert_eq!(first.matches("pub struct KindSerializer").count(), 1);

    success(ingot.generate(&["Kind", "Batch", "Kind"], true));
    success(ingot.generate(&["Batch"], false));
    assert_eq!(first, ingot.generated());
}

#[test]
fn check_detects_missing_and_stale_output_without_writing() {
    let ingot = Ingot::new("pub struct Item { pub id: u8 }\n");
    let missing = ingot.generate(&["Item"], true);
    assert!(!missing.status.success());
    assert!(stderr(&missing).contains("missing or stale"));
    assert!(!ingot.output.exists());

    success(ingot.generate(&["Item"], false));
    let old = ingot.generated();
    fs::write(&ingot.source, "pub struct Item { pub id: u16 }\n").unwrap();
    let stale = ingot.generate(&["Item"], true);
    assert!(!stale.status.success());
    assert!(stderr(&stale).contains("missing or stale"));
    assert_eq!(old, ingot.generated());

    success(ingot.generate(&["Item"], false));
    assert_ne!(old, ingot.generated());
    success(ingot.generate(&["Item"], true));
}

#[test]
fn unsupported_declarations_report_locations_and_preserve_generated_output() {
    let ingot = Ingot::new("pub struct Item { pub id: u8 }\n");
    success(ingot.generate(&["Item"], false));
    let original = ingot.generated();
    let cases = [
        (
            "pub struct Item {\n    pub id: u256\n}\n",
            "unsupported integer type",
        ),
        (
            "pub struct Item {\n    pub id: [u8; 65]\n}\n",
            "array length",
        ),
        (
            "pub struct Item {\n    pub id: [Option<u8>; 1]\n}\n",
            "outer type",
        ),
        (
            "pub struct Item {\n    id: u8\n}\n",
            "fields must be public",
        ),
        (
            "pub struct Item {\n    pub child: Item\n}\n",
            "recursive model",
        ),
        (
            "pub enum Item {\n    Variant(u8)\n}\n",
            "only unit enum variants",
        ),
        (
            "pub struct Item {\n    #[cfg(target = \"wasm\")]\n    pub id: u8\n}\n",
            "attributes",
        ),
    ];
    for (source, message) in cases {
        fs::write(&ingot.source, source).unwrap();
        let output = ingot.generate(&["Item"], false);
        assert!(!output.status.success());
        let error = stderr(&output);
        assert!(error.contains("models.fe:2:"), "{error}");
        assert!(error.contains(message), "{error}");
        assert_eq!(original, ingot.generated());
    }
}

#[test]
fn malformed_source_and_handwritten_destinations_are_left_intact() {
    let ingot = Ingot::new("pub struct Item { pub id: u8 }\n");
    let handwritten = "// Handwritten\npub fn manual() {}\n";
    fs::write(&ingot.output, handwritten).unwrap();

    let output = ingot.generate(&["Item"], false);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("refusing to replace handwritten source"));
    assert_eq!(handwritten, ingot.generated());

    fs::write(&ingot.source, "pub struct Item {\n    pub id:\n").unwrap();
    let output = ingot.generate(&["Item"], false);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("models.fe:"));
    assert_eq!(handwritten, ingot.generated());
}

#[test]
fn stdout_produces_the_same_source_without_writing_an_output_file() {
    let ingot = Ingot::new("pub struct Item { pub id: u8 }\n");
    let output = Command::new(env!("CARGO_BIN_EXE_fe-codec-gen"))
        .arg("--source")
        .arg(&ingot.source)
        .args(["--type", "Item", "--stdout"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{}", stderr(&output));
    assert!(!ingot.output.exists());

    success(ingot.generate(&["Item"], false));
    assert_eq!(output.stdout, fs::read(&ingot.output).unwrap());
}

#[test]
fn rejects_incomplete_duplicate_and_zero_field_ids() {
    for (members, expected) in [
        ("#[codec_id = 7]\npub a: u8,\npub b: u8", "every member"),
        (
            "#[codec_id = 7]\npub a: u8,\n#[codec_id = 7]\npub b: u8",
            "duplicate codec_id",
        ),
        ("#[codec_id = 0]\npub a: u8", "must be positive"),
    ] {
        let ingot = Ingot::new(&format!("pub struct Item {{\n{members}\n}}\n"));
        let result = ingot.generate(&["Item"], false);
        assert!(!result.status.success());
        assert!(stderr(&result).contains(expected), "{}", stderr(&result));
    }
}
