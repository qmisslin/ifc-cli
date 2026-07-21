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

## Requirements

The current build instructions target macOS.

Required dependencies:

* C++17 compiler
* CMake 3.21 or newer
* Ninja
* IfcOpenShell with IfcGeom and OpenCascade support
* Boost
* Eigen
* OpenCascade
* nlohmann/json

Install the Homebrew dependencies:

```bash
brew install cmake ninja boost eigen opencascade nlohmann-json
```

IfcOpenShell must already be installed. The default path used below is:

```text
$HOME/.local/ifcopenshell
```

## Build

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

The functional JSONL API documentation is available in [`doc.md`](doc.md).

## Project structure

```text
ifc-cli/
├── CMakeLists.txt
├── README.md
├── doc.md
├── LICENSE
└── src/
    ├── main.cpp
    ├── protocol.hpp
    ├── scene.hpp
    ├── transform.hpp
    ├── geometry.hpp
    └── material.hpp
```

## License

This project is distributed under the [MIT License](LICENSE).
