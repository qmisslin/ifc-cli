# ifc-cli

`ifc-cli` is a small, synchronous IFC editing process built on top of
[IfcOpenShell](https://ifcopenshell.org/).

It loads one IFC file, accepts one JSON command per line through standard input,
and writes one JSON response per line to standard output. This makes it usable
both interactively in a terminal and as a child process of another application,
including a realtime server.

The application has no graphical interface and does not require a network
service, socket server, or custom IPC protocol.

## Features

- Load and save IFC files.
- Return the IFC object hierarchy and all STEP entity IDs.
- Inspect attributes, inverse attributes, placement, geometry, materials,
  property sets, and optional triangulated meshes.
- Add standalone extruded primitives.
- Add or subtract an extruded primitive from an existing solid representation.
- Delete IFC products and clean up their orphaned owned data.
- Update native IFC attributes such as `Name`, `Description`, and `Tag`.
- Assign, create, replace, or remove a material association.
- Create and update custom property sets.
- Change a concrete `IfcElement` to another compatible concrete IFC class.
- Communicate using synchronous JSON Lines over `stdin` and `stdout`.

## Project status and compatibility

The current source targets **IfcOpenShell 0.8.5** and C++17.

The same source can be compiled on Windows, Linux, and macOS. IfcOpenShell and
its native dependencies must be compiled separately on each operating system;
compiled libraries cannot be copied from one platform to another.

Supported IFC schemas depend on the schemas enabled when IfcOpenShell is built.
The default Unix build includes IFC2X3, IFC4, and IFC4X3_ADD2.

## Repository layout

```text
ifc-cli/
├── CMakeLists.txt
├── main.cpp
└── README.md
```

The local `IfcOpenShell/` dependency directory and generated `build*`
directories should not be committed unless IfcOpenShell is intentionally added
as a Git submodule.

## Requirements

All platforms require:

- A 64-bit C++17 compiler.
- CMake 3.21 or newer.
- Git.
- IfcOpenShell 0.8.5 compiled with `IfcGeom` and OpenCASCADE support.
- Approximately 8 GB or more of free disk space while building dependencies.

`nlohmann/json` is discovered through CMake when installed. Otherwise, the
provided `CMakeLists.txt` downloads version 3.11.3 during configuration.

## Building on Windows

### 1. Install the build tools

Install:

- [Git for Windows](https://git-scm.com/download/win)
- [CMake](https://cmake.org/download/)
- Visual Studio Build Tools 2022 or Visual Studio 2022 with the
  **Desktop development with C++** workload
- Visual Studio Code, optionally with the C/C++ and CMake Tools extensions

Visual Studio Code is the editor. The MSVC compiler and Windows SDK are supplied
by Visual Studio Build Tools or Visual Studio.

### 2. Download IfcOpenShell

From the `ifc-cli` repository root:

```powershell
git clone --branch ifcconvert-0.8.5 --depth 1 https://github.com/IfcOpenShell/IfcOpenShell.git
```

This creates `ifc-cli/IfcOpenShell`.

### 3. Build IfcOpenShell

Open an **x64 Native Tools Command Prompt for VS 2022**, then run:

```bat
cd IfcOpenShell\win

build-all.cmd vs2022-x64 Release ^
  -DVERSION_OVERRIDE=ON ^
  -DMINIMAL_BUILD=ON ^
  -DBUILD_ONLY_COMMON_SCHEMAS=ON ^
  -DBUILD_SHARED_LIBS=OFF ^
  -DBUILD_IFCGEOM=ON ^
  -DWITH_OPENCASCADE=ON ^
  -DBUILD_IFCPYTHON=OFF ^
  -DBUILD_CONVERT=OFF ^
  -DBUILD_GEOMSERVER=OFF ^
  -DBUILD_EXAMPLES=OFF ^
  -DWITH_ROCKSDB=OFF
```

The command must be executed from the `IfcOpenShell\win` directory. Avoid
placing the repository in a path containing spaces because some dependency
scripts pass paths through PowerShell without preserving them correctly.

### 4. Configure and build ifc-cli

Return to the `ifc-cli` repository root. Replace the example path with the real
absolute path to your checkout:

```bat
cmake -S . -B build -G "Visual Studio 17 2022" -A x64 -DIFCOPENSHELL_ROOT="C:/dev/ifc-cli/IfcOpenShell"
cmake --build build --config Release
```

The executable is generated at:

```text
build/Release/ifc-cli.exe
```

Run it from Git Bash with:

```bash
./build/Release/ifc-cli.exe
```

Or from Command Prompt with:

```bat
build\Release\ifc-cli.exe
```

## Building on Linux

The following instructions target Ubuntu and Debian-based distributions.

### 1. Install the build tools

```bash
sudo apt update
sudo apt install -y \
  build-essential git cmake python3 \
  autoconf automake bison byacc patch \
  bzip2 xz-utils \
  mesa-common-dev libfontconfig1-dev \
  libffi-dev libssl-dev zlib1g-dev
```

### 2. Download and build IfcOpenShell

From the `ifc-cli` repository root:

```bash
git clone --branch ifcconvert-0.8.5 --depth 1 \
  https://github.com/IfcOpenShell/IfcOpenShell.git

cd IfcOpenShell

BUILD_CFG=Release python3 nix/build-all.py IfcGeom \
  --without-hdf5 \
  --without-rocksdb \
  --without-opencollada \
  --without-cgal

cd ..
```

The default x86-64 installation root is:

```text
IfcOpenShell/build/Linux/x86_64/install
```

On ARM Linux, replace `x86_64` with the architecture directory created by the
IfcOpenShell build script, such as `aarch64`.

### 3. Configure and build ifc-cli

```bash
cmake -S . -B build-linux \
  -DCMAKE_BUILD_TYPE=Release \
  -DIFCOPENSHELL_ROOT="$PWD/IfcOpenShell/build/Linux/x86_64/install"

cmake --build build-linux -j
```

Run:

```bash
./build-linux/ifc-cli
```

## Building on macOS

### 1. Install the build tools

Install the Apple command-line tools and dependencies from Homebrew:

```bash
xcode-select --install
brew install cmake git python bison autoconf automake libffi xz
export PATH="$(brew --prefix bison)/bin:$PATH"
```

Add the `PATH` export to `~/.zshrc` if you want it to persist across terminal
sessions.

### 2. Download and build IfcOpenShell

From the `ifc-cli` repository root:

```bash
git clone --branch ifcconvert-0.8.5 --depth 1 \
  https://github.com/IfcOpenShell/IfcOpenShell.git

cd IfcOpenShell

BUILD_CFG=Release python3 nix/build-all.py IfcGeom \
  --without-hdf5 \
  --without-rocksdb \
  --without-opencollada \
  --without-cgal

cd ..
```

The default installation root is normally:

```text
# Apple Silicon
IfcOpenShell/build/Darwin/arm64/10.15/install

# Intel Mac
IfcOpenShell/build/Darwin/x86_64/10.15/install
```

### 3. Configure and build ifc-cli

For Apple Silicon:

```bash
cmake -S . -B build-macos \
  -DCMAKE_BUILD_TYPE=Release \
  -DIFCOPENSHELL_ROOT="$PWD/IfcOpenShell/build/Darwin/arm64/10.15/install"

cmake --build build-macos -j
```

For an Intel Mac, replace `arm64` with `x86_64`.

Run:

```bash
./build-macos/ifc-cli
```

## Communication protocol

`ifc-cli` uses [JSON Lines](https://jsonlines.org/):

1. Start the process once.
2. Read the initial `ready` response from standard output.
3. Write exactly one JSON object followed by a newline to standard input.
4. Read exactly one JSON object followed by a newline from standard output.
5. Wait for that response before sending the next command.
6. Send `exit` or close standard input when finished.

The process is intentionally synchronous. Only one command is executed at a
time, and each command blocks until its response is available.

IfcOpenShell diagnostic messages are written to standard error. Standard output
must be reserved for protocol messages.

### Initial response

```json
{"ok":true,"data":{"status":"ready","protocol":"json-lines"}}
```

### Successful response

```json
{"ok":true,"data":{}}
```

The contents of `data` depend on the action.

### Error response

```json
{"ok":false,"error":"Human-readable error message"}
```

An action error does not terminate the process. The caller may send another
command after reading the error response.

## Actions

| Action | Required fields | Optional fields | Description |
| --- | --- | --- | --- |
| `help` | `action` | — | Return a compact list of supported actions. |
| `load` | `action`, `path` | — | Load an IFC file, replacing the currently loaded model. |
| `tree` | `action` | — | Return all entity IDs and the `IfcObjectDefinition` hierarchy. |
| `get` | `action`, `id` | `mesh` | Return all available information for one entity. |
| `primitive` | `action`, `profile`, `depth` | `targetId`, `operation`, `transform`, `name`, `containerId` | Add a standalone primitive or modify a selected product. |
| `update` | `action`, `id` | `class`, attribute shortcuts, `attributes`, `material`, `properties` | Update an existing IFC element. |
| `delete` | `action`, `id` | — | Delete an `IfcProduct` and clean up owned orphaned entities. |
| `save` | `action` | `path` | Save the in-memory model. |
| `exit` | `action` | — | Return a final response and terminate normally. |

The `tree`, `get`, `primitive`, `update`, `delete`, and `save` actions require a
successfully loaded IFC file.

## Load a file

```json
{"action":"load","path":"C:/models/input.ifc"}
```

Windows paths should use forward slashes or escaped backslashes:

```json
{"action":"load","path":"C:\\models\\input.ifc"}
```

Example response:

```json
{"ok":true,"data":{"path":"C:/models/input.ifc","schema":"IFC4","entityCount":1250}}
```

Loading another file discards unsaved in-memory changes to the previous file.

## Read the hierarchy

```json
{"action":"tree"}
```

The response contains:

- `allEntityIds`: every STEP entity ID in the file.
- `nodes`: every `IfcObjectDefinition`, including its IFC class, direct
  supertype, name, geometry flag, parent IDs, and child IDs.

The hierarchy recognizes aggregation, nesting, spatial containment, opening,
and opening-fill relationships.

Example node:

```json
{
  "id": 42,
  "type": "IfcWall",
  "supertype": "IfcBuildingElement",
  "name": "External wall",
  "hasGeometry": true,
  "parentIds": [12],
  "childIds": [84]
}
```

## Inspect an entity

Without mesh triangulation:

```json
{"action":"get","id":42}
```

With mesh triangulation:

```json
{"action":"get","id":42,"mesh":true}
```

The response may include:

- Native explicit IFC attributes.
- Inverse attributes.
- A 4 × 4 world transform.
- Property sets.
- Assigned types.
- Materials.
- Product representation data.
- Triangulated vertices, faces, normals, and material IDs when `mesh` is true.

Mesh extraction can be significantly slower and may return a large JSON
response. Enable it only when needed.

## Transform format

Transforms contain 16 numbers in **column-major** order:

```json
[1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]
```

Translation is stored at indices 12, 13, and 14. For example, a translation of
10 units on X, 20 units on Y, and 3 units on Z is:

```json
[1,0,0,0, 0,1,0,0, 0,0,1,0, 10,20,3,1]
```

Primitive coordinates and extrusion depths use the length convention of the
loaded IFC model. `ifc-cli` does not automatically convert units.

For a targeted boolean operation, the primitive transform is expressed in the
target representation's local coordinates. For a standalone primitive, the
transform becomes the product placement.

## Add a standalone primitive

The profile is a list of at least three 2D `[x, y]` points. It is closed
automatically if the final point does not repeat the first point.

```json
{
  "action": "primitive",
  "name": "New block",
  "containerId": 25,
  "profile": [[0,0],[4,0],[4,2],[0,2]],
  "depth": 3,
  "transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 10,20,0,1]
}
```

When `containerId` is omitted, the first available `IfcBuildingStorey`,
`IfcBuilding`, or `IfcSite` is used. The created product initially has the class
`IfcBuildingElementProxy`.

## Union a primitive with an existing product

Union aliases are `union` and `add`:

```json
{
  "action": "primitive",
  "targetId": 42,
  "operation": "union",
  "profile": [[0,0],[1,0],[1,1],[0,1]],
  "depth": 2,
  "transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 3,0,1,1]
}
```

## Subtract material from an existing product

Subtraction aliases are `subtract` and `difference`:

```json
{
  "action": "primitive",
  "targetId": 42,
  "operation": "difference",
  "profile": [[0,0],[0.9,0],[0.9,2.1],[0,2.1]],
  "depth": 0.3,
  "transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 2,0,1,1]
}
```

Targeted primitive operations require an existing body representation made of
solid-compatible IFC items. They are represented using `IfcBooleanResult` and
do not perform a destructive OpenCASCADE mesh rewrite.

## Update common attributes

Common shortcut fields are:

- `name` → `Name`
- `description` → `Description`
- `tag` → `Tag`
- `objectType` → `ObjectType`
- `predefinedType` → `PredefinedType`

```json
{
  "action": "update",
  "id": 42,
  "name": "Main wall",
  "description": "Updated by ifc-cli",
  "tag": "W-001"
}
```

Native attribute names may also be supplied explicitly:

```json
{
  "action": "update",
  "id": 42,
  "attributes": {
    "Name": "Main wall",
    "Description": "External structural wall",
    "Tag": "W-001",
    "PredefinedType": "STANDARD"
  }
}
```

Attribute names and enumeration values are case-sensitive. Setting an optional
attribute to `null` unsets it. The generic attribute editor supports scalar
strings, numbers, booleans, enumerations, nulls, and entity references by ID;
it does not provide a generic aggregate editor.

## Change the IFC class

```json
{"action":"update","id":42,"class":"wall"}
```

Available aliases are:

| Alias | Resulting class | Default predefined type |
| --- | --- | --- |
| `proxy` | `IfcBuildingElementProxy` | `NOTDEFINED` |
| `wall` | `IfcWall` | `NOTDEFINED` |
| `slab` | `IfcSlab` | `NOTDEFINED` |
| `floor` | `IfcSlab` | `FLOOR` |
| `ceiling` | `IfcCovering` | `CEILING` |
| `roof` | `IfcRoof` | `NOTDEFINED` |
| `column` | `IfcColumn` | `NOTDEFINED` |
| `beam` | `IfcBeam` | `NOTDEFINED` |
| `door` | `IfcDoor` | `NOTDEFINED` |
| `window` | `IfcWindow` | `NOTDEFINED` |
| `stair` | `IfcStair` | `NOTDEFINED` |
| `railing` | `IfcRailing` | `NOTDEFINED` |
| `furniture` | `IfcFurniture` | `NOTDEFINED` |

An exact concrete IFC class name may be supplied instead of an alias:

```json
{"action":"update","id":42,"class":"IfcWall"}
```

Class changes are limited to concrete `IfcElement` classes present in the
loaded schema. The numeric STEP ID, common attributes, placement,
representation, and external references are preserved where compatible.

Class conversion is a low-level operation. The caller remains responsible for
ensuring that the resulting element satisfies schema-specific and project-level
semantic requirements.

## Assign or create a material

Create or reuse a material by name:

```json
{
  "action": "update",
  "id": 42,
  "material": {
    "name": "Concrete",
    "description": "Cast concrete",
    "category": "Concrete"
  }
}
```

The short string form is also accepted:

```json
{"action":"update","id":42,"material":"Concrete"}
```

Assign an existing material entity:

```json
{"action":"update","id":42,"material":{"id":128}}
```

An integer ID is also accepted directly:

```json
{"action":"update","id":42,"material":128}
```

Remove the direct material association:

```json
{"action":"update","id":42,"material":null}
```

Material editing currently manages direct `IfcRelAssociatesMaterial`
associations. It does not construct layered, profiled, constituent, or styled
material definitions.

## Create or update property sets

```json
{
  "action": "update",
  "id": 42,
  "properties": {
    "Pset_SAFEStudio": {
      "Reference": "W-001",
      "FireRating": "REI 120",
      "LoadBearing": true,
      "AcousticRating": 52.5
    }
  }
}
```

Property values may be strings, integers, real numbers, booleans, or `null`.
New values are stored as `IfcPropertySingleValue` entries. When a property set
is shared by several objects, it is copied before being modified so unrelated
objects retain their previous values.

## Combine updates

```json
{
  "action": "update",
  "id": 42,
  "class": "wall",
  "name": "Main wall",
  "tag": "W-001",
  "material": "Concrete",
  "properties": {
    "Pset_SAFEStudio": {
      "Reference": "W-001",
      "LoadBearing": true
    }
  }
}
```

## Delete a product

```json
{"action":"delete","id":42}
```

Deletion is limited to `IfcProduct` entities. The operation removes invalid
relationships and unreferenced owned placement, representation, and property
entities while protecting shared data such as materials, units, owner history,
and representation contexts.

The deletion only affects the in-memory model until `save` is called.

## Save the IFC file

Save to a new path:

```json
{"action":"save","path":"C:/models/output.ifc"}
```

Save to the current path:

```json
{"action":"save"}
```

When `path` is omitted, the most recently loaded or saved path is overwritten.
Use a separate output path while testing edits and keep backups of source IFC
files.

## Exit

```json
{"action":"exit"}
```

Response:

```json
{"ok":true,"data":{"status":"bye"}}
```

## Terminal example

Start `ifc-cli` and enter one compact JSON object per line:

```text
$ ./ifc-cli
{"ok":true,"data":{"status":"ready","protocol":"json-lines"}}
{"action":"load","path":"./model.ifc"}
{"ok":true,"data":{"path":"./model.ifc","schema":"IFC4","entityCount":1250}}
{"action":"tree"}
{"ok":true,"data":{"allEntityIds":[1,2,3],"nodes":[]}}
{"action":"exit"}
{"ok":true,"data":{"status":"bye"}}
```

Do not pretty-print requests across multiple lines. Each input line must contain
one complete JSON object.

## Batch example

Create `commands.jsonl`:

```jsonl
{"action":"load","path":"./input.ifc"}
{"action":"update","id":42,"tag":"W-001"}
{"action":"save","path":"./output.ifc"}
{"action":"exit"}
```

Linux and macOS:

```bash
./ifc-cli < commands.jsonl > responses.jsonl
```

PowerShell:

```powershell
Get-Content commands.jsonl | .\ifc-cli.exe > responses.jsonl
```

The first line in `responses.jsonl` is always the initial `ready` message.

## C# process integration

The following example targets modern .NET and keeps one `ifc-cli` process alive
for several synchronous requests:

```csharp
using System.Diagnostics;
using System.Text.Json;

string executable = OperatingSystem.IsWindows()
    ? @"C:\tools\ifc-cli.exe"
    : "/usr/local/bin/ifc-cli";

var startInfo = new ProcessStartInfo
{
    FileName = executable,
    UseShellExecute = false,
    RedirectStandardInput = true,
    RedirectStandardOutput = true,
    RedirectStandardError = true,
    CreateNoWindow = true
};

using var process = new Process { StartInfo = startInfo };

process.ErrorDataReceived += (_, eventArgs) =>
{
    if (eventArgs.Data is not null)
        Console.Error.WriteLine($"[ifc-cli] {eventArgs.Data}");
};

if (!process.Start())
    throw new InvalidOperationException("Unable to start ifc-cli.");

process.BeginErrorReadLine();
process.StandardInput.AutoFlush = true;

string ready = await process.StandardOutput.ReadLineAsync()
    ?? throw new EndOfStreamException("ifc-cli stopped before its ready response.");

Console.WriteLine(ready);

async Task<JsonDocument> SendAsync(object command)
{
    string request = JsonSerializer.Serialize(command);
    await process.StandardInput.WriteLineAsync(request);

    string response = await process.StandardOutput.ReadLineAsync()
        ?? throw new EndOfStreamException("ifc-cli stopped before responding.");

    return JsonDocument.Parse(response);
}

using JsonDocument loadResponse = await SendAsync(new
{
    action = "load",
    path = @"C:\models\input.ifc"
});

using JsonDocument updateResponse = await SendAsync(new
{
    action = "update",
    id = 42,
    tag = "W-001"
});

using JsonDocument saveResponse = await SendAsync(new
{
    action = "save",
    path = @"C:\models\output.ifc"
});

using JsonDocument exitResponse = await SendAsync(new
{
    action = "exit"
});

await process.WaitForExitAsync();
```

For server use, serialize access to each child process. Do not allow two callers
to write commands concurrently to the same process because responses do not
contain request IDs.

## Operational model

- One process owns one in-memory IFC model.
- Loading a file replaces the current model.
- Commands are executed sequentially.
- Edits remain in memory until `save` succeeds.
- There is no transaction, undo, or automatic backup mechanism.
- The process exits on `exit`, end-of-input, or a fatal process-level failure.
- Normal command failures return `{ "ok": false }` and leave the process
  available for subsequent commands.

For concurrent jobs, start one `ifc-cli` process per independently edited IFC
file or place a serialized request queue in front of each process.

## Known limitations

- No visualization is provided.
- Only one IFC file is loaded per process.
- Primitive profiles are polygonal and extruded along their local positive Z
  axis.
- Profiles with curved segments, holes, or arbitrary swept paths are not
  currently supported.
- Boolean edits require solid-compatible existing representation items.
- Material editing is limited to direct material associations.
- Generic native aggregate attributes cannot be edited through `update`.
- Class conversion does not automatically create all semantic relationships
  that specialized BIM authoring tools may require.
- IFC validity is not automatically checked after every edit.
- Large `get` responses with `mesh: true` may consume significant memory.
- Output files are written directly to their destination path rather than by an
  atomic temporary-file replacement.

## Troubleshooting

### CMake cannot find IfcOpenShell

Verify that `IFCOPENSHELL_ROOT` points to:

- The IfcOpenShell source root on Windows, containing
  `_installed-vs2022-x64` and `_deps-vs2022-x64-installed`.
- The generated `install` directory on Linux and macOS, containing the
  `ifcopenshell` directory and dependency installation directories.

Use an absolute path and forward slashes in Windows CMake commands.

### A previous executable name remains in the build directory

Changing the CMake target does not necessarily remove an older executable.
Configure a fresh build directory or manually remove the obsolete binary.

### macOS cannot find the dependency directory

Check the actual architecture reported by:

```bash
uname -m
```

Then inspect:

```bash
find IfcOpenShell/build/Darwin -type d -name install
```

Use the returned `install` directory as `IFCOPENSHELL_ROOT`.

### Protocol parsing fails

Ensure that:

- Every request is valid JSON on exactly one line.
- The command is terminated with a newline.
- The caller reads and removes the initial `ready` response.
- Only standard output is parsed as JSON.
- Diagnostic standard error is drained independently.

### A modified IFC does not appear changed on disk

Editing actions modify the in-memory model. Send a successful `save` action
before terminating the process.

## License

`ifc-cli` is released under the [MIT License](LICENSE).

### Dependency licenses

This project uses third-party components with their own licenses, including:

- [IfcOpenShell](https://github.com/IfcOpenShell/IfcOpenShell), licensed under
  LGPL-3.0-or-later.
- [Open CASCADE Technology](https://dev.opencascade.org/), licensed under
  LGPL-2.1 with the upstream additional exception.
- [nlohmann/json](https://github.com/nlohmann/json), licensed under the MIT
  License.

The MIT License applies to the original `ifc-cli` source code and does not
replace the licenses of its dependencies. When distributing binaries,
especially statically linked binaries, review and satisfy the redistribution
requirements of all linked dependencies.

## References

- [IfcOpenShell repository](https://github.com/IfcOpenShell/IfcOpenShell)
- [IfcOpenShell 0.8.5 source tag](https://github.com/IfcOpenShell/IfcOpenShell/tree/ifcconvert-0.8.5)
- [IfcOpenShell C++ installation documentation](https://docs.ifcopenshell.org/ifcopenshell/installation.html)
- [IfcOpenShell Unix build script](https://github.com/IfcOpenShell/IfcOpenShell/blob/ifcconvert-0.8.5/nix/build-all.py)
- [JSON Lines specification](https://jsonlines.org/)
