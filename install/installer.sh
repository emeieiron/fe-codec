#!/bin/sh
# Installs the prebuilt fe_codec_gen binary for Fe Codec.
#
#   curl -fsSL https://github.com/emeieiron/fe-codec/releases/latest/download/installer.sh | sh
#
# Options, passed as `sh -s -- <options>` when piping:
#   --no-modify-path  do not add the install directory to PATH in shell startup files
#
# Environment:
#   FE_CODEC_INSTALL_DIR   where to install fe_codec_gen (default: ~/.local/bin)
#   FE_CODEC_VERSION       release tag to install, such as v0.1.0 (default: latest)
#   FE_CODEC_DOWNLOAD_URL  base URL of release files, for mirrors and testing
set -eu

REPOSITORY="emeieiron/fe-codec"
modify_path=true

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

has() {
    command -v "$1" >/dev/null 2>&1
}

download() {
    if has curl; then
        curl --proto '=https,file' --tlsv1.2 -fsSL "$1" -o "$2"
    elif has wget; then
        wget -q -O "$2" "$1"
    else
        fail "the fe_codec_gen installer needs curl or wget"
    fi
}

sha256() {
    if has sha256sum; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif has shasum; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    elif has openssl; then
        openssl dgst -sha256 "$1" | sed 's/.* //'
    else
        fail "the fe_codec_gen installer needs sha256sum, shasum, or openssl to verify downloads"
    fi
}

# Checks `file` against the entry for `name` in a SHA256SUMS file.
verify() {
    expected=$(awk -v name="$3" '{ file = $2; sub(/^\*/, "", file) } file == name { print $1 }' "$2")
    [ -n "$expected" ] || fail "no checksum for $3"
    actual=$(sha256 "$1")
    [ "$actual" = "$expected" ] || fail "$3 is corrupt (SHA-256 $actual, expected $expected)"
}

detect_target() {
    os=$(uname -s)
    arch=$(uname -m)
    case "$arch" in
        x86_64 | amd64) arch=x86_64 ;;
        arm64 | aarch64) arch=aarch64 ;;
        *) fail "fe_codec_gen is not built for $arch processors" ;;
    esac
    case "$os" in
        Linux)
            if ldd --version 2>&1 | grep -qi musl; then
                fail "fe_codec_gen is built for glibc-based Linux; musl systems such as Alpine are not supported"
            fi
            target="$arch-unknown-linux-gnu"
            ;;
        Darwin)
            # A shell running under Rosetta reports x86_64 on Apple silicon.
            if [ "$arch" = x86_64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" = 1 ]; then
                arch=aarch64
            fi
            if [ "$arch" != aarch64 ]; then
                fail "Fe's native backend requires Apple silicon (aarch64) on macOS; Intel macOS is not supported"
            fi
            target="aarch64-apple-darwin"
            ;;
        *)
            fail "fe_codec_gen prebuilt binaries support Linux and macOS; this system is $os"
            ;;
    esac
}

release_url() {
    if [ -n "${FE_CODEC_DOWNLOAD_URL:-}" ]; then
        printf '%s' "${FE_CODEC_DOWNLOAD_URL%/}"
    elif [ -n "${FE_CODEC_VERSION:-}" ]; then
        printf 'https://github.com/%s/releases/download/%s' "$REPOSITORY" "$FE_CODEC_VERSION"
    else
        printf 'https://github.com/%s/releases/latest/download' "$REPOSITORY"
    fi
}

install_gen() {
    archive="fe_codec_gen-$target.tar.gz"
    base=$(release_url)
    say "Downloading $archive"
    download "$base/$archive" "$work/$archive" || fail "could not download $base/$archive"
    download "$base/SHA256SUMS" "$work/SHA256SUMS" || fail "could not download $base/SHA256SUMS"
    verify "$work/$archive" "$work/SHA256SUMS" "$archive"
    tar -xzf "$work/$archive" -C "$work"
    [ -x "$work/fe_codec_gen" ] || fail "$archive does not contain fe_codec_gen"
    if [ "$os" = Darwin ]; then
        xattr -d com.apple.quarantine "$work/fe_codec_gen" 2>/dev/null || true
    fi
    mkdir -p "$install_dir"
    tmp_bin=$(mktemp "$install_dir/.fe_codec_gen.XXXXXX")
    cp "$work/fe_codec_gen" "$tmp_bin"
    chmod +x "$tmp_bin"
    mv -f "$tmp_bin" "$install_dir/fe_codec_gen"
}

add_to_profile() {
    if ! grep -qsF "$line" "$1"; then
        printf '\n%s\n' "$line" >>"$1"
    fi
}

modify_path() {
    [ "$modify_path" = true ] || return 0
    if [ "$install_dir" = "$HOME/.local/bin" ]; then
        dir_text='$HOME/.local/bin'
    else
        dir_text="$install_dir"
    fi
    line="export PATH=\"$dir_text:\$PATH\""
    add_to_profile "$HOME/.profile"
    # Bash reads these instead of .profile when they exist.
    for file in "$HOME/.bash_profile" "$HOME/.bashrc"; do
        if [ -f "$file" ]; then
            add_to_profile "$file"
        fi
    done
    case "${SHELL:-}" in
        */zsh) add_to_profile "$HOME/.zshrc" ;;
        *)
            if [ -f "$HOME/.zshrc" ]; then
                add_to_profile "$HOME/.zshrc"
            fi
            ;;
    esac
    if has fish || [ -d "$HOME/.config/fish" ]; then
        mkdir -p "$HOME/.config/fish/conf.d"
        printf 'fish_add_path -g "%s"\n' "$install_dir" >"$HOME/.config/fish/conf.d/fe_codec_gen.fish"
    fi
}

usage() {
    cat <<'USAGE'
Installs the prebuilt fe_codec_gen binary for Fe Codec.

Usage: installer.sh [--no-modify-path]

  --no-modify-path  do not add the install directory to PATH in shell startup files

Environment: FE_CODEC_INSTALL_DIR, FE_CODEC_VERSION, FE_CODEC_DOWNLOAD_URL
USAGE
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-modify-path) modify_path=false ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) fail "unknown option $1" ;;
        esac
        shift
    done

    [ -n "${HOME:-}" ] || fail "HOME is not set"
    install_dir="${FE_CODEC_INSTALL_DIR:-$HOME/.local/bin}"
    case "$install_dir" in
        /) ;;
        */) install_dir="${install_dir%/}" ;;
    esac

    detect_target
    work=$(mktemp -d 2>/dev/null || mktemp -d -t fe_codec_gen)
    trap 'rm -rf "$work"' EXIT
    trap 'exit 130' INT TERM

    install_gen
    modify_path

    version=$("$install_dir/fe_codec_gen" --version)
    say "Installed $version to $install_dir/fe_codec_gen"
    case ":$PATH:" in
        *":$install_dir:"*) ;;
        *)
            say "Add $install_dir to your PATH if needed:"
            say "  export PATH=\"$install_dir:\$PATH\""
            ;;
    esac
}

main "$@"
