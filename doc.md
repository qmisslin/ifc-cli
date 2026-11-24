# `ifc-cli` - Functional Integration Guide

## Protocol

`ifc-cli` is a persistent process communicating through **JSONL**:

* input: one JSON request per line on `stdin`;
* output: one JSON response per line on `stdout`, with multiple responses for successful tessellation requests;
* only one IFC file is loaded at a time;
* all commands using the loaded model must be sent to the same process.

Request:

```json
{"id":"1","command":"load","params":{"path":"./samples/1.ifc"}}
```

Success response:

```json
{"id":"1","ok":true,"result":{}}
```

Error response:

```json
{
  "id":"1",
  "ok":false,
  "error":{
    "code":"ENTITY_NOT_FOUND",
    "message":"Entity not found",
    "details":{}
  }
}
```

The protocol is strict:

* `id`, `command`, and `params` are required;
* `params` must always be a JSON object;
* unknown fields are rejected;
* command names are case-sensitive;
* values are not automatically converted between types.

Returned identifiers are opaque:

```text
transform:42
geometry:17
material:8
```

The client must not generate identifiers. All identifiers become invalid after another `load` command.

---

## Usage lifecycle

```text
start ifc-cli
→ load
→ inspect or modify the model
→ save
→ exit
```

---

## General commands

| Command           | `params`         | Action                                          |
| ----------------- | ---------------- | ----------------------------------------------- |
| `help`            | `{}`             | Returns the built-in help                       |
| `getCapabilities` | `{}`             | Returns available features and IFC schemas      |
| `load`            | `{"path":"..."}` | Loads an IFC file and starts a new session      |
| `save`            | `{"path":"..."}` | Saves the current model                         |
| `exit`            | `{}`             | Returns a response, then terminates the process |

---

## Transforms

A transform represents an IFC object in the scene.

| Command               | `params`                 | Action                                             |
| --------------------- | ------------------------ | -------------------------------------------------- |
| `transformGetAll`     | `{}`                     | Lists all transforms                               |
| `transformGet`        | `{"id":"transform:42"}`  | Returns a complete transform                       |
| `transformCreate`     | see below                | Creates a generic IFC element                      |
| `transformUpdate`     | see below                | Updates a transform                                |
| `transformDelete`     | `{"id":"transform:42"}`  | Recursively deletes the transform and its children |
| `transformTessellate` | see tessellation section | Generates the final triangle mesh                  |
| `transformDuplicate` | `{"id":"transform:42","includeChildren":true}` | Duplicates a transform and optionally its descendants |

Create:

```json
{
  "name":"Element",
  "parents":{
    "spatial":null,
    "placement":null,
    "decomposition":null
  },
  "transform":{
    "space":"world",
    "matrix":[
      1,0,0,0,
      0,1,0,0,
      0,0,1,0,
      0,0,0,1
    ]
  },
  "properties":[]
}
```

Update:

```json
{
  "id":"transform:42",
  "name":"Updated name",
  "transform":{
    "space":"world",
    "matrix":[
      1,0,0,2,
      0,1,0,1,
      0,0,1,0,
      0,0,0,1
    ]
  }
}
```

For `transformUpdate`, `id` and at least one modified field are required.

Editable fields:

```text
name, parents, transform, properties
```

When provided, `properties` replaces all editable properties.

---

## Geometries

A geometry created through the API is an additive or subtractive polygon extrusion.

| Command          | `params`                         | Action                                   |
| ---------------- | -------------------------------- | ---------------------------------------- |
| `geometryGetAll` | `{"transformId":null}`           | Lists all geometries                     |
| `geometryGetAll` | `{"transformId":"transform:42"}` | Lists geometries attached to a transform |
| `geometryGet`    | `{"id":"geometry:17"}`           | Returns one geometry                     |
| `geometryCreate` | see below                        | Creates an extrusion                     |
| `geometryUpdate` | see below                        | Updates an editable geometry             |
| `geometryDelete` | `{"id":"geometry:17"}`           | Deletes a geometry                       |

Create:

```json
{
  "parentId":"transform:42",
  "name":"Opening",
  "transform":{
    "space":"parent",
    "matrix":[
      1,0,0,0,
      0,1,0,0,
      0,0,1,0,
      0,0,0,1
    ]
  },
  "profile":[[0,0],[1.2,0],[1.2,2.1],[0,2.1]],
  "depth":0.3,
  "operation":"SUBTRACT"
}
```

`operation` accepts:

```text
ADD
SUBTRACT
```

Update:

```json
{
  "id":"geometry:17",
  "depth":0.5,
  "profile":[[0,0],[1,0],[1,2],[0,2]]
}
```

Editable fields:

```text
name, transform, profile, depth, operation
```

Native IFC geometries may be returned with `editable:false`. Such geometries cannot be updated.

### Geometry kinds

Every geometry exposes a `kind` field:

| Kind | Description |
| --- | --- |
| `SOLID` | Physical geometry or physical geometry operation |
| `DOMAIN` | Non-physical spatial or simulation volume |

`geometryCreate` accepts an optional `kind` field.
The default value is `SOLID`.

A domain is an independent closed polygonal extrusion.
It does not add or subtract physical matter.

Domains created by the API use:

- `IfcSpatialZone`
- `IfcExtrudedAreaSolid`
- `IfcRelAssignsToProduct`

Creating a domain requires IFC4 or later and a parent product
with a valid local placement.

Domains support the standard geometry inspection, update,
deletion and duplication operations.

Only the `ADD` operation is supported for domains.
The geometry kind cannot be changed after creation.

Managed domains cannot receive physical materials.

Native IFC spaces and spatial zones are classified as domains.
Native geometry editability rules remain unchanged.

### Geometry filtering

`geometryGetAll` accepts an optional `kind` parameter:

- `ALL`: return all geometries (default).
- `SOLID`: return physical geometries.
- `DOMAIN`: return spatial and simulation domains.

### Tessellation filtering

`transformTessellate` accepts an optional `options.kind`:

- `SOLID`: tessellate physical geometries (default).
- `DOMAIN`: tessellate spatial and simulation volumes.

Domain tessellation supports an optional `options.geometryId`
to select one managed domain in the requested transform hierarchy.

Domains are never included in physical mesh results.

Both kinds use the existing JSONL mesh event protocol.

Geometry profiles, extrusion depths and placement matrices
use IFC model units. Tessellated vertices use metres.

Domain meshes represent closed volume boundaries, not
physical surfaces or occupied material.

---

## Materials

| Command            | `params`                                                | Action                                   |
| ------------------ | ------------------------------------------------------- | ---------------------------------------- |
| `materialGetAll`   | `{}`                                                    | Lists all materials                      |
| `materialGet`      | `{"id":"material:8"}`                                   | Returns one material                     |
| `materialCreate`   | see below                                               | Creates a material                       |
| `materialUpdate`   | see below                                               | Updates an editable material             |
| `materialDelete`   | `{"id":"material:8"}`                                   | Deletes the material and its assignments |
| `materialAssign`   | `{"materialId":"material:8","targetId":"transform:42"}` | Assigns a material                       |
| `materialUnassign` | same parameters                                         | Removes a material assignment            |

Create:

```json
{
  "name":"Concrete",
  "category":"Concrete",
  "visual":{
    "color":[0.6,0.6,0.6],
    "opacity":1,
    "metallic":0,
    "roughness":0.8
  },
  "properties":[]
}
```

Update:

```json
{
  "id":"material:8",
  "visual":{
    "color":[0.5,0.5,0.5],
    "opacity":1,
    "metallic":0,
    "roughness":0.9
  }
}
```

Editable fields:

```text
name, category, visual, properties
```

`targetId` may reference either a transform or a geometry.

---

## Tessellation

Request:

```json
{
  "id":"10",
  "command":"transformTessellate",
  "params":{
    "id":"transform:42",
    "options":{
      "space":"world",
      "includeNormals":true,
      "includeMaterials":true,
      "includeChildren":false
    }
  }
}
```

This command produces multiple responses using the same request `id`:

```text
mesh.begin
mesh.vertices
mesh.indices
mesh.normals
mesh.materials
mesh.end
```

Example:

```json
{"id":"10","ok":true,"result":{"event":"mesh.begin","vertexCount":100,"indexCount":300}}
{"id":"10","ok":true,"result":{"event":"mesh.vertices","offset":0,"values":[0,0,0,1,0,0]}}
{"id":"10","ok":true,"result":{"event":"mesh.indices","offset":0,"values":[0,1,2]}}
{"id":"10","ok":true,"result":{"event":"mesh.end"}}
```

The client must accumulate all mesh blocks until `mesh.end` is received.

---

## Common data types

Transform:

```json
{
  "space":"parent",
  "matrix":[
    1,0,0,0,
    0,1,0,0,
    0,0,1,0,
    0,0,0,1
  ]
}
```

`space` accepts `parent` or `world`.

Matrices contain exactly 16 finite numbers in row-major order.

Parent relations:

```json
{
  "spatial":"transform:1",
  "placement":"transform:1",
  "decomposition":null
}
```

Property:

```json
{
  "propertySet":"Pset_Custom",
  "name":"Reference",
  "valueType":"IfcLabel",
  "value":"ABC",
  "unit":null,
  "source":"occurrence"
}
```

For material properties, `source` must be `material`.

---

## Advanced operation semantics

### Transform duplication

The `transformDuplicate` command duplicates a transform and optionally its descendants.

Parameters:

```json
{
  "id":"transform:42",
  "includeChildren":true
}
```

Result:

```json
{
  "rootId":"transform:100",
  "transforms":{
    "transform:42":"transform:100"
  },
  "geometries":{
    "geometry:17":"geometry:101"
  }
}
```

The result maps source session identifiers to their duplicated counterparts.

The duplicated root receives a new name with a ` Copy` suffix and a new IFC GlobalId.

Duplicated entities preserve their native IFC class, supported attributes, geometry representations, material assignments and occurrence property graphs.

Exclusive occurrence property sets and their properties are independently duplicated, including complex native properties.

Shared property relationships, type definitions, quantities and material references retain their existing sharing semantics.

Managed simulation domains are duplicated independently of physical geometries.

IfcProject objects and transforms using unsupported placement types cannot be duplicated. These operations return `TRANSFORM_NOT_DUPLICABLE`.

The selected hierarchy is validated before duplication begins.

### Physical boolean operations

Physical geometry operations are evaluated during tessellation.

For each affected product, the physical result is computed as:

```text
physical = union(base, additions) - union(subtractions)
```

Additions are applied before subtractions, regardless of creation order.

Both native and API-created IFC projection and opening relationships are considered.

Boolean operations are performed on OpenCascade BRep geometry before triangulation.

The original IFC representations and feature relationships remain unchanged.

A complete subtraction produces a valid empty mesh.

Physical geometries belonging to different transforms are not globally merged.

Simulation domains never participate in physical boolean operations.

### Property validation

Property values must be compatible with their declared IFC scalar type.

For example:

* `IfcInteger` requires an integer.
* `IfcBoolean` requires a boolean.
* `IfcReal` requires a finite numeric value.
* `IfcLabel` requires a string.

A property unit must reference a compatible unit name available in the loaded IFC model.

Invalid IFC value types, incompatible scalar values and unknown units return `INVALID_PARAMETERS`.

Updating occurrence properties preserves unsupported native properties and avoids modifying property sets still shared by other objects.

### Mutation pre-validation

Creation and update commands validate their supported parameters before applying modifications to the IFC model.

This includes:

* IFC property types and values;
* property unit references;
* placement matrices;
* polygon profiles;
* geometry operations;
* material appearance parameters;
* parent relationship compatibility.

Recursive duplication also validates the selected hierarchy before creating duplicates.

Requests rejected during pre-validation leave the existing IFC model and session indexes unchanged.

This does not constitute a general transactional rollback mechanism. Unexpected errors occurring during IFC mutation may still require application-level recovery.

A failed `load` operation does not restore the previously loaded model.

### Schema compatibility

The `getCapabilities` command returns the actual IFC schemas available in the current executable, excluding internal STEP header schemas.

IFC2X3 materials use `IfcExtendedMaterialProperties`.

IFC4 and later schemas use `IfcMaterialProperties`.

Material categories are stored in editor metadata when the native IFC material entity does not provide a Category attribute.

Managed simulation domains require a schema supporting `IfcSpatialZone` and cannot be created in IFC2X3.

### Entity deletion

Deletion operations remove the selected entities and their supported exclusively owned dependencies.

Geometry representation dependencies, local placement dependencies, occurrence properties and material presentation styles are cleaned up when no references remain.

Shared dependencies are preserved while other entities reference them.

Deletion is destructive and does not provide an undo operation.

### Mesh response handling

The JSONL protocol is synchronous.

Normal commands return a single response. Successful tessellation commands return multiple responses sharing the request identifier.

The client must wait for the final `mesh.end` event before treating a mesh response as complete.

An error response terminates the failed request.

Mesh offsets refer to scalar positions in the corresponding arrays, not vertex or triangle identifiers.

Mesh data is currently computed before the first `mesh.begin` event is emitted.

JSONL chunking limits individual response sizes but does not provide progressive geometry computation.

Empty mesh responses contain zero vertices and indices.

### Interactive and network integration

The CLI communicates through standard input and output. Network transport, authentication, concurrent client coordination and application-level caching are responsibilities of the integrating application.

Each CLI process handles requests sequentially.

For interactive applications:

* Keep the CLI process alive between requests.
* Cache unchanged meshes on the application side.
* Request tessellation only for affected transforms and dependent geometry.
* Avoid tessellating the complete model after every modification.
* Separate interactive transform manipulation from expensive geometry recomputation.
* Allow long-running tessellation requests to complete outside the rendering loop.

The CLI does not provide hard real-time execution guarantees.

## Complete example

```bash
cat <<'JSONL' | ./build/ifc-cli
{"id":"1","command":"load","params":{"path":"./samples/1.ifc"}}
{"id":"2","command":"transformGetAll","params":{}}
{"id":"3","command":"geometryGetAll","params":{"transformId":null}}
{"id":"4","command":"materialGetAll","params":{}}
{"id":"5","command":"save","params":{"path":"./samples/1-copy.ifc"}}
{"id":"6","command":"exit","params":{}}
JSONL
```

Every command returns one response, except `transformTessellate`, which returns a response sequence ending with `mesh.end`.
