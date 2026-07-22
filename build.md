# Portable release builds

This document describes the reproducible workflow used to build and publish portable `ifc-cli` bundles for:

- Windows 10/11 x64
- macOS Apple Silicon arm64

It assumes that the build environments are already configured and functional.

## General rules

- Build both platforms from the same Git tag.
- Commit source and build-system changes before creating the tag.
- Do not commit generated binaries, bundles, or archives.
- Upload generated archives directly to the GitHub Release.
- Always build in `Release`.
- Replace `0.2.0` below with the target version.

## Expected build environment

### Windows

The Windows machine already provides:

- Visual Studio 2022 with the C++ desktop workload
- MSVC x64 and a Windows SDK
- CMake
- Git Bash
- PowerShell
- GitHub CLI (`gh`)
- The local IfcOpenShell tree expected by `CMakeLists.txt`:
  - `IfcOpenShell/_installed-vs2022-x64`
  - `IfcOpenShell/_deps-vs2022-x64-installed`
  - `IfcOpenShell/_deps/boost_1_86_0/stage/vs2022-x64`

The CMake install rules copy the executable, its detected runtime dependencies, and the required MSVC runtime DLLs into the portable bundle.

### macOS

The macOS machine already provides:

- Apple Silicon hardware
- Xcode Command Line Tools
- CMake
- Ninja
- Homebrew
- Boost
- Eigen
- OpenCascade
- nlohmann/json
- GitHub CLI (`gh`)
- IfcOpenShell installed at `$HOME/.local/ifcopenshell`

The CMake install rules use `BundleUtilities` and `fixup_bundle()` to copy required non-system dynamic libraries and rewrite their paths for a relocatable bundle.

## 1. Prepare the release

Perform this after all source and CMake changes have been committed.

```bash
VERSION=0.2.0
RELEASE_TAG="v${VERSION}"

git switch main
git pull --ff-only
git status --short
```

Commit pending source or build-system changes when required:

```bash
git add CMakeLists.txt cmake src
git commit -m "release: prepare v${VERSION}"
git push origin main
```

Do not create an empty commit when there are no changes.

Create and push the release tag:

```bash
git tag -a "${RELEASE_TAG}" -m "ifc-cli ${RELEASE_TAG}"
git push origin "${RELEASE_TAG}"
```

Create a draft release:

```bash
gh auth status

gh release create "${RELEASE_TAG}" \
    --title "ifc-cli ${RELEASE_TAG}" \
    --generate-notes \
    --verify-tag \
    --draft
```

## 2. Windows x64 portable bundle

Run these commands from Git Bash on the Windows build machine.

### Clone and select the release tag

```bash
git clone https://github.com/qmisslin/ifc-cli.git
cd ifc-cli

VERSION=0.2.0
RELEASE_TAG="v${VERSION}"

git fetch --tags
git switch --detach "${RELEASE_TAG}"
```

Confirm the target architecture and revision:

```bash
git status --short
git log -1 --oneline
```

### Configure and compile

```bash
rm -rf build package-windows dist

cmake \
    -S . \
    -B build \
    -G "Visual Studio 17 2022" \
    -A x64

cmake --build build \
    --config Release \
    --parallel
```

### Generate the portable bundle

```bash
PACKAGE_DIR="$(pwd -W)/package-windows"

cmake --install build \
    --config Release \
    --prefix "${PACKAGE_DIR}"
```

The bundle must contain `ifc-cli.exe`, the MSVC runtime DLLs, and any detected non-system runtime dependencies:

```bash
find package-windows \
    -maxdepth 1 \
    -type f \
    -printf '%f\n' \
    | sort
```

### Smoke-test the bundle with a minimal `PATH`

```bash
cat > test-portable.ps1 <<'PS1'
$ErrorActionPreference = "Stop"

$env:Path = "$env:SystemRoot\System32;$env:SystemRoot"

'{"id":"portable-test","command":"help","params":{}}' |
    & "$PSScriptRoot\package-windows\ifc-cli.exe"

if ($LASTEXITCODE -ne 0) {
    throw "ifc-cli exited with code $LASTEXITCODE"
}
PS1

powershell.exe \
    -NoProfile \
    -ExecutionPolicy Bypass \
    -File "$(cygpath -w test-portable.ps1)"

rm test-portable.ps1
```

Also test loading and tessellating a real IFC file. A clean Windows Sandbox or VM remains the definitive portability test.

### Create the Windows archive

```bash
BUNDLE_NAME="ifc-cli-v${VERSION}-windows-x64"

mkdir -p "dist/${BUNDLE_NAME}"
cp -R package-windows/. "dist/${BUNDLE_NAME}/"

powershell.exe \
    -NoProfile \
    -Command "Compress-Archive -Path 'dist\\${BUNDLE_NAME}' -DestinationPath 'dist\\${BUNDLE_NAME}.zip' -Force"

sha256sum \
    "dist/${BUNDLE_NAME}.zip" \
    > "dist/${BUNDLE_NAME}.zip.sha256"
```

Inspect the archive:

```bash
unzip -l "dist/${BUNDLE_NAME}.zip"
```

### Upload the Windows assets

```bash
gh release upload "${RELEASE_TAG}" \
    "dist/${BUNDLE_NAME}.zip" \
    "dist/${BUNDLE_NAME}.zip.sha256" \
    --repo qmisslin/ifc-cli
```

## 3. macOS arm64 portable bundle

Run these commands on the Apple Silicon macOS build machine.

### Clone and select the same release tag

```bash
git clone https://github.com/qmisslin/ifc-cli.git
cd ifc-cli

VERSION=0.2.0
RELEASE_TAG="v${VERSION}"

git fetch --tags
git switch --detach "${RELEASE_TAG}"
```

Confirm the machine and revision:

```bash
uname -m
git status --short
git log -1 --oneline
```

`uname -m` must report:

```text
arm64
```

### Configure and compile

```bash
export IFCOPENSHELL_ROOT="${IFCOPENSHELL_ROOT:-$HOME/.local/ifcopenshell}"

rm -rf build-macos-arm64 package-macos-arm64 dist

cmake \
    -S . \
    -B build-macos-arm64 \
    -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DCMAKE_PREFIX_PATH="$(brew --prefix)" \
    -DIFCOPENSHELL_ROOT="${IFCOPENSHELL_ROOT}"

cmake --build build-macos-arm64 --parallel
```

Confirm the architecture:

```bash
file build-macos-arm64/ifc-cli
```

The result must identify a Mach-O arm64 executable.

### Generate the portable bundle

```bash
cmake \
    --install build-macos-arm64 \
    --prefix "$(pwd)/package-macos-arm64"
```

The install step runs the CMake macOS bundle fixup and copies the required non-system dynamic libraries.

Inspect the generated files:

```bash
find package-macos-arm64 \
    -maxdepth 2 \
    -type f \
    -print \
    | sort
```

### Verify relocated dynamic-library paths

Generate a dependency report:

```bash
DEPENDENCY_REPORT="$(mktemp)"

find package-macos-arm64 -type f -print0 |
while IFS= read -r -d '' file_path; do
    if file "${file_path}" | grep -q 'Mach-O'; then
        echo "===== ${file_path} ====="
        otool -L "${file_path}"
    fi
done > "${DEPENDENCY_REPORT}"

cat "${DEPENDENCY_REPORT}"
```

Reject the bundle when a development-machine path remains:

```bash
if grep -E '^[[:space:]]+(/Users/|/opt/homebrew/)' "${DEPENDENCY_REPORT}"; then
    echo "ERROR: local dependency paths remain in the bundle"
    rm -f "${DEPENDENCY_REPORT}"
    exit 1
fi

rm -f "${DEPENDENCY_REPORT}"
```

References using `@executable_path`, `@loader_path`, `@rpath`, `/usr/lib`, or `/System/Library` are expected.

### Apply an ad hoc signature

Remove local metadata:

```bash
find package-macos-arm64 -name '.DS_Store' -delete
xattr -cr package-macos-arm64
```

Sign bundled Mach-O dependencies first:

```bash
find package-macos-arm64 \
    -type f \
    ! -path "package-macos-arm64/ifc-cli" \
    -print0 |
while IFS= read -r -d '' file_path; do
    if file "${file_path}" | grep -q 'Mach-O'; then
        codesign \
            --force \
            --sign - \
            "${file_path}"
    fi
done
```

Sign and verify the executable:

```bash
codesign \
    --force \
    --sign - \
    package-macos-arm64/ifc-cli

codesign \
    --verify \
    --strict \
    --verbose=2 \
    package-macos-arm64/ifc-cli
```

This is an ad hoc signature, not Apple Developer ID notarization.

### Smoke-test without Homebrew in `PATH`

```bash
printf '{"id":"portable-test","command":"help","params":{}}\n' |
env -i \
    HOME="$HOME" \
    PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    ./package-macos-arm64/ifc-cli
```

Also test loading and tessellating a real IFC file on a machine without the development dependency paths.

### Create the macOS archive

```bash
BUNDLE_NAME="ifc-cli-v${VERSION}-macos-arm64"

mkdir -p dist
cp -R package-macos-arm64 "dist/${BUNDLE_NAME}"

tar \
    -czf "dist/${BUNDLE_NAME}.tar.gz" \
    -C dist \
    "${BUNDLE_NAME}"

(
    cd dist

    shasum -a 256 \
        "${BUNDLE_NAME}.tar.gz" \
        > "${BUNDLE_NAME}.tar.gz.sha256"
)
```

Inspect the archive:

```bash
tar -tzf "dist/${BUNDLE_NAME}.tar.gz" | head -50
```

### Upload the macOS assets

```bash
gh release upload "${RELEASE_TAG}" \
    "dist/${BUNDLE_NAME}.tar.gz" \
    "dist/${BUNDLE_NAME}.tar.gz.sha256" \
    --repo qmisslin/ifc-cli
```

Use `--clobber` only when intentionally replacing assets with identical names.
