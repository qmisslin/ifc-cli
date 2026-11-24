# ifc-cli

`ifc-cli` is a C++17 command-line tool for reading, editing, tessellating, and saving IFC files through a synchronous JSONL API.

It is built on top of [IfcOpenShell](https://ifcopenshell.org/) and is intended to run as a persistent child process communicating through standard input and output.

## Features

* Load and save IFC files
* Inspect IFC scene objects
* Read and update transforms
* Create additive or subtractive extruded geometry
* Create and assign materials
* Read and update property sets
* Tessellate IFC objects into triangle meshes
* Communicate through JSON Lines over `stdin` and `stdout`
* Create and edit non-physical polygonal simulation domains
* Filter physical geometries and simulation domains independently
* Tessellate physical geometries and simulation domains separately
* Persist simulation domains in IFC4 spatial zones
* Duplicate transforms and their descendants while preserving native IFC properties and representations
* Compute physical boolean unions and differences using OpenCascade
* Preserve shared IFC dependencies during property updates and entity deletion
* Validate mutation parameters before modifying the IFC model
* Support IFC2X3-compatible material property containers

## Requirements

Required dependencies:

* C++17 compiler
* CMake 3.21 or newer
* IfcOpenShell with IfcGeom and OpenCascade support
* Boost
* Eigen
* OpenCascade
* nlohmann/json

### Windows

The Windows build uses:

* Visual Studio 2022 with the Desktop development with C++ workload
* MSVC x64
* A Windows SDK
* A local IfcOpenShell build located in `./IfcOpenShell`

IfcOpenShell must be built and installed with the following directories:

```text
IfcOpenShell/_installed-vs2022-x64
IfcOpenShell/_deps-vs2022-x64-installed
```

The expected IfcOpenShell CMake package is:

```text
IfcOpenShell/_installed-vs2022-x64/lib/cmake/IfcOpenShell/IfcOpenShellConfig.cmake
```

### macOS

Install the dependencies with Homebrew:

```bash
brew install cmake ninja boost eigen opencascade nlohmann-json
```

IfcOpenShell must already be installed. The default installation path is:

```text
$HOME/.local/ifcopenshell
```

## Build

### Windows

Run the following commands from Git Bash, a Visual Studio developer shell, or another terminal where CMake and Visual Studio are available:

```bash
rm -rf build

cmake \
    -S . \
    -B build \
    -G "Visual Studio 17 2022" \
    -A x64

cmake --build build --config Release --parallel
```

The executable is generated at:

```text
build/Release/ifc-cli.exe
```

IfcOpenShell is currently built and installed in `Release`, so the CLI must also be compiled using the `Release` configuration.

### macOS

```bash
cmake \
    -S . \
    -B build \
    -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$(brew --prefix)" \
    -DIFCOPENSHELL_ROOT="$HOME/.local/ifcopenshell"

cmake --build build --parallel
```

The executable is generated at:

```text
build/ifc-cli
```

### Portable release bundles

For reproducible portable Windows x64 and macOS arm64 release builds, see [`build.md`](build.md).

## Usage

Start the process:

```bash
./build/ifc-cli
```

Each input line must contain one JSON request. Each output line contains one JSON response.

Example:

```bash
cat <<'JSONL' | ./build/ifc-cli
{"id":"1","command":"load","params":{"path":"./samples/model.ifc"}}
{"id":"2","command":"transformGetAll","params":{}}
{"id":"3","command":"save","params":{"path":"./samples/model-copy.ifc"}}
{"id":"4","command":"exit","params":{}}
JSONL
```

Successful response:

```json
{"id":"1","ok":true,"result":{}}
```

Error response:

```json
{
  "id":"1",
  "ok":false,
  "error":{
    "code":"ERROR_CODE",
    "message":"Error description",
    "details":{}
  }
}
```

The process keeps the loaded IFC model in memory. All commands operating on a model must therefore be sent to the same process.

## API documentation

* [`doc.md`](doc.md): Functional JSONL API and client integration guide.
* [`src/spec.md`](src/spec.md): Detailed protocol and implementation specification.
* [`build.md`](build.md): Portable release build instructions.

## Testing

The project includes 24 automated test suites covering 375 tests.

The test suites cover:

* JSONL protocol and session management
* Entity identifiers and indexing
* Transforms, hierarchies and placements
* IFC properties and materials
* Native and editable geometries
* Polygon profiles and boolean operations
* Tessellation and simulation domains
* Duplication and recursive deletion
* IFC persistence and semantic graph preservation
* Model units and schema compatibility
* Error recovery and mutation pre-validation
* Complete integration workflows
* Execution time, mesh streaming and memory measurements

### Python dependencies

Create a dedicated Python virtual environment from the repository root.

macOS / Linux:

```bash
python3 -m venv tests/.venv
source tests/.venv/bin/activate
python -m pip install -r tests/requirements.txt
```

Windows PowerShell:

```powershell
python -m venv tests/.venv
.\tests\.venv\Scripts\Activate.ps1
python -m pip install -r tests/requirements.txt
```

The Python dependencies are used for semantic IFC comparisons and memory measurements. They are not runtime dependencies of the CLI executable.

### Run all tests

macOS:

```bash
python tests/run_all.py ./build/ifc-cli
```

Windows:

```powershell
python tests/run_all.py ./build/Release/ifc-cli.exe
```

### Run selected test suites

```bash
python tests/run_all.py --only geometries boolean_operations tessellation --verbose
```

### Run tests without performance benchmarks

```bash
python tests/run_all.py --skip-performance
```

### Run performance benchmarks

```bash
python tests/run_all.py --only performance --verbose
```

### Test reports

Each execution generates a timestamped report directory under `tests/reports/`.

The generated files include:

* `report.md`: Human-readable test report.
* `report.json`: Structured test results.
* `performance.json`: Benchmark measurements.
* `logs/`: Standard output and error logs for each suite.

### Validation status

Reference validation performed on November 24, 2026:

| Metric | Result |
| --- | ---: |
| Test suites | 24 / 24 |
| Tests | 376 / 376 |
| Failures | 0 |
| Errors | 0 |
| Skipped tests | 0 |
| Performance measurements | 104 |

Reference environment: macOS arm64, Python 3.15.2, Release build.

Performance measurements are informational and depend on the execution environment. Successful benchmarks do not establish hard real-time guarantees or prove the absence of memory leaks.

## Project structure

```text
ifc-cli/
├── CMakeLists.txt
├── README.md
├── build.md
├── doc.md
├── LICENSE
├── cmake/
├── src/
│   ├── main.cpp
│   ├── protocol.hpp
│   ├── scene.hpp
│   ├── transform.hpp
│   ├── geometry.hpp
│   ├── material.hpp
│   └── spec.md
└── tests/
    ├── requirements.txt
    ├── run_all.py
    └── test_*.py
```

## License

This project is distributed under the [MIT License](LICENSE).
