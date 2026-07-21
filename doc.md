# `ifc-cli` - Functional Integration Guide

## Protocol

`ifc-cli` is a persistent process communicating through **JSONL**:

* input: one JSON request per line on `stdin`;
* output: one JSON response per line on `stdout`;
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
