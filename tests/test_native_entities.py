import json
import math
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest

from collections import Counter
from pathlib import Path


# Standalone integration suite using only the Python standard library.
#
# Run:
# python3 tests/test_native_entities.py ./build/ifc-cli
#
# Not executed against the compiled CLI.
#
# All fixtures use IFC4.
# STEP inspection is limited to the controlled fixtures generated here.
# Native projection tests enforce physical union semantics and may expose
# limitations in additive feature tessellation.
#
# Shared representation tests verify preservation of IFC references and
# tessellation of both products without requiring duplicate session indexes.
#
# Alternative placement tests use IfcGridPlacement, which is inspectable
# but not editable through the simplified placement API.

EXECUTABLE = Path("./build/ifc-cli").resolve()
REQUEST_TIMEOUT = 60
MESH_TOLERANCE = 1e-6
CURVED_VOLUME_TOLERANCE = 0.5

PROJECT_GLOBAL_ID = "0000000000000000000001"
PRODUCT_GLOBAL_ID = "0000000000000000000002"
SECOND_PRODUCT_GLOBAL_ID = "0000000000000000000003"

IDENTITY_MATRIX = [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1
]

CUBE_POINTS = [
    (0, 0, 0),
    (2, 0, 0),
    (2, 2, 0),
    (0, 2, 0),
    (0, 0, 2),
    (2, 0, 2),
    (2, 2, 2),
    (0, 2, 2)
]

CUBE_FACES = [
    (0, 3, 2, 1),
    (4, 5, 6, 7),
    (0, 1, 5, 4),
    (1, 2, 6, 5),
    (2, 3, 7, 6),
    (3, 0, 4, 7)
]

CUBE_TRIANGLES = [
    triangle
    for a, b, c, d in CUBE_FACES
    for triangle in ((a, b, c), (a, c, d))
]


def step_string(value):
    return "'" + value.replace("'", "''") + "'"


def step_real(value):
    return f"{float(value):.16E}"


def step_coordinates(values):
    return "(" + ",".join(step_real(value) for value in values) + ")"


def step_reference(identifier):
    return "$" if identifier is None else f"#{identifier}"


def step_references(identifiers):
    return "(" + ",".join(
        step_reference(identifier) for identifier in identifiers
    ) + ")"


def translation_matrix(x=0, y=0, z=0):
    matrix = IDENTITY_MATRIX.copy()
    matrix[3] = x
    matrix[7] = y
    matrix[11] = z
    return matrix


def transform_input(matrix=None):
    return {
        "space": "parent",
        "matrix": IDENTITY_MATRIX.copy() if matrix is None else matrix
    }


def property_input(name, value, value_type="IfcLabel", property_set="Pset_Editor"):
    return {
        "propertySet": property_set,
        "name": name,
        "valueType": value_type,
        "value": value,
        "unit": None,
        "source": "occurrence"
    }


class Fixture:
    def __init__(self):
        self.entities = []
        self.guid_counter = 100

        self.origin = self.add("IFCCARTESIANPOINT", "(0.,0.,0.)")
        self.z_axis = self.add("IFCDIRECTION", "(0.,0.,1.)")
        self.x_axis = self.add("IFCDIRECTION", "(1.,0.,0.)")
        self.y_axis = self.add("IFCDIRECTION", "(0.,1.,0.)")

        self.axis = self.add(
            "IFCAXIS2PLACEMENT3D",
            f"#{self.origin},#{self.z_axis},#{self.x_axis}"
        )

        self.context = self.add(
            "IFCGEOMETRICREPRESENTATIONCONTEXT",
            f"$,'Model',3,1.E-6,#{self.axis},$"
        )

        self.length_unit = self.add(
            "IFCSIUNIT",
            "*,.LENGTHUNIT.,$,.METRE."
        )

        angle_unit = self.add(
            "IFCSIUNIT",
            "*,.PLANEANGLEUNIT.,$,.RADIAN."
        )

        units = self.add(
            "IFCUNITASSIGNMENT",
            step_references([self.length_unit, angle_unit])
        )

        self.project = self.add(
            "IFCPROJECT",
            f"'{PROJECT_GLOBAL_ID}',$,'Native entity test project',$,$,$,$,"
            f"(#{self.context}),#{units}"
        )

    def guid(self):
        self.guid_counter += 1
        return str(self.guid_counter).zfill(22)

    def add(self, class_name, arguments):
        identifier = len(self.entities) + 1
        self.entities.append(f"#{identifier}={class_name.upper()}({arguments});")
        return identifier

    def point(self, *coordinates):
        return self.add(
            "IFCCARTESIANPOINT",
            step_coordinates(coordinates)
        )

    def axis3d(self, x=0, y=0, z=0):
        point = self.point(x, y, z)

        return self.add(
            "IFCAXIS2PLACEMENT3D",
            f"#{point},#{self.z_axis},#{self.x_axis}"
        )

    def placement(self, x=0, y=0, z=0, parent=None):
        axis = self.axis3d(x, y, z)

        return self.add(
            "IFCLOCALPLACEMENT",
            f"{step_reference(parent)},#{axis}"
        )

    def rectangle(self, width, length, x=0, y=0):
        point = self.point(x, y)

        axis = self.add(
            "IFCAXIS2PLACEMENT2D",
            f"#{point},$"
        )

        return self.add(
            "IFCRECTANGLEPROFILEDEF",
            f".AREA.,$,#{axis},{step_real(width)},{step_real(length)}"
        )

    def extrusion(
        self,
        width=4,
        length=3,
        depth=2,
        x=0,
        y=0,
        z=0,
        profile=None
    ):
        if profile is None:
            profile = self.rectangle(width, length)

        axis = self.axis3d(x, y, z)

        return self.add(
            "IFCEXTRUDEDAREASOLID",
            f"#{profile},#{axis},#{self.z_axis},{step_real(depth)}"
        )

    def shape_representation(
        self,
        items,
        representation_type="SweptSolid",
        identifier="Body"
    ):
        return self.add(
            "IFCSHAPEREPRESENTATION",
            f"#{self.context},{step_string(identifier)},"
            f"{step_string(representation_type)},{step_references(items)}"
        )

    def representation(self, items, representation_type="SweptSolid"):
        shape = self.shape_representation(items, representation_type)

        return self.add(
            "IFCPRODUCTDEFINITIONSHAPE",
            f"$,$,(#{shape})"
        )

    def product(
        self,
        class_name="IfcBuildingElementProxy",
        global_id=PRODUCT_GLOBAL_ID,
        name="Native product",
        representation=None,
        placement=None,
        predefined_type="NOTDEFINED",
        object_type=None
    ):
        if placement is None:
            placement = self.placement()

        common = (
            f"'{global_id}',$,{step_string(name)},'Native description',"
            f"{step_string(object_type) if object_type else '$'},"
            f"#{placement},{step_reference(representation)}"
        )

        if class_name == "IfcSpace":
            arguments = (
                f"{common},'Native space long name',.ELEMENT.,"
                f".{predefined_type}.,$"
            )

        elif class_name == "IfcSpatialZone":
            arguments = (
                f"{common},'Native zone long name',.{predefined_type}."
            )

        else:
            arguments = (
                f"{common},'NATIVE-TAG',.{predefined_type}."
            )

        return self.add(class_name, arguments)

    def solid_product(
        self,
        class_name="IfcBuildingElementProxy",
        width=4,
        length=3,
        depth=2,
        predefined_type="NOTDEFINED",
        **options
    ):
        solid = self.extrusion(width, length, depth)
        representation = self.representation([solid])

        product = self.product(
            class_name=class_name,
            representation=representation,
            predefined_type=predefined_type,
            **options
        )

        return product, solid

    def relation(self, class_name, arguments, name=None):
        return self.add(
            class_name,
            f"'{self.guid()}',$,"
            f"{step_string(name) if name else '$'},$,{arguments}"
        )

    def property_set(self, name, properties):
        return self.add(
            "IFCPROPERTYSET",
            f"'{self.guid()}',$,{step_string(name)},$,"
            f"{step_references(properties)}"
        )

    def assign_properties(self, product, property_set):
        return self.relation(
            "IFCRELDEFINESBYPROPERTIES",
            f"(#{product}),#{property_set}"
        )

    def material(self, name, category):
        return self.add(
            "IFCMATERIAL",
            f"{step_string(name)},$,{step_string(category)}"
        )

    def assign_material(self, product, material):
        return self.relation(
            "IFCRELASSOCIATESMATERIAL",
            f"(#{product}),#{material}"
        )

    def build(self):
        body = "\n".join(self.entities)

        return f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('native-entities.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
{body}
ENDSEC;
END-ISO-10303-21;
"""


def split_step_arguments(arguments):
    result = []
    current = []
    depth = 0
    in_string = False
    index = 0

    while index < len(arguments):
        character = arguments[index]

        if character == "'":
            current.append(character)

            if (
                in_string
                and index + 1 < len(arguments)
                and arguments[index + 1] == "'"
            ):
                current.append("'")
                index += 2
                continue

            in_string = not in_string

        elif not in_string:
            if character == "(":
                depth += 1

            elif character == ")":
                depth -= 1

            elif character == "," and depth == 0:
                result.append("".join(current).strip())
                current = []
                index += 1
                continue

            current.append(character)

        else:
            current.append(character)

        index += 1

    result.append("".join(current).strip())
    return result


def parse_step_entities(content):
    pattern = re.compile(
        r"#(\d+)\s*=\s*(IFC[A-Z0-9_]+)\s*\((.*?)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL
    )

    entities = {}

    for match in pattern.finditer(content):
        identifier = int(match.group(1))

        if identifier in entities:
            raise AssertionError(f"Duplicate STEP identifier #{identifier}")

        entities[identifier] = {
            "class": match.group(2).upper(),
            "arguments": split_step_arguments(match.group(3))
        }

    if not entities:
        raise AssertionError("No STEP entities found")

    return entities


def reference_ids(value):
    value = re.sub(r"'(?:[^']|'')*'", "''", value)
    return [int(identifier) for identifier in re.findall(r"#(\d+)", value)]


def entity_references(entity):
    return [
        identifier
        for argument in entity["arguments"]
        for identifier in reference_ids(argument)
    ]


def find_step_root(entities, global_id):
    matches = [
        (identifier, entity)
        for identifier, entity in entities.items()
        if entity["arguments"][0] == step_string(global_id)
    ]

    if len(matches) != 1:
        raise AssertionError(
            f"Expected one entity with GlobalId {global_id}, got {len(matches)}"
        )

    return matches[0]


def entities_by_class(entities, class_name):
    return {
        identifier: entity
        for identifier, entity in entities.items()
        if entity["class"] == class_name.upper()
    }


def forward_graph(entities, roots):
    pending = list(roots)
    visited = set()

    while pending:
        identifier = pending.pop()

        if identifier in visited:
            continue

        if identifier not in entities:
            raise AssertionError(f"Missing STEP entity #{identifier}")

        visited.add(identifier)
        pending.extend(entity_references(entities[identifier]))

    return visited


def mesh_points(mesh):
    return [
        tuple(mesh["vertices"][offset:offset + 3])
        for offset in range(0, len(mesh["vertices"]), 3)
    ]


def mesh_triangles(mesh):
    points = mesh_points(mesh)
    indices = mesh["indices"]

    for offset in range(0, len(indices), 3):
        yield tuple(
            points[index] for index in indices[offset:offset + 3]
        )


def subtract(first, second):
    return tuple(a - b for a, b in zip(first, second))


def cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0]
    )


def dot(first, second):
    return math.fsum(a * b for a, b in zip(first, second))


def mesh_volume(mesh):
    points = mesh_points(mesh)

    if not points:
        return 0.0

    origin = points[0]

    return abs(math.fsum(
        dot(
            subtract(a, origin),
            cross(subtract(b, origin), subtract(c, origin))
        )
        for a, b, c in mesh_triangles(mesh)
    )) / 6.0


def mesh_surface_area(mesh):
    return math.fsum(
        math.sqrt(dot(normal, normal)) / 2.0
        for a, b, c in mesh_triangles(mesh)
        for normal in [cross(subtract(b, a), subtract(c, a))]
    )


def mesh_bounds(mesh):
    points = mesh_points(mesh)

    if not points:
        raise AssertionError("Mesh contains no vertices")

    return tuple(
        operation(point[axis] for point in points)
        for operation in (min, max)
        for axis in range(3)
    )


def mesh_vertex_set(mesh, precision=6):
    return {
        tuple(round(value, precision) for value in point)
        for point in mesh_points(mesh)
    }


def mesh_edge_counts(mesh, precision=6):
    edges = Counter()

    for triangle in mesh_triangles(mesh):
        a, b, c = [
            tuple(round(value, precision) for value in point)
            for point in triangle
        ]

        if len({a, b, c}) != 3:
            raise AssertionError(f"Degenerate triangle: {triangle}")

        for first, second in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted((first, second)))] += 1

    return edges


class Client:
    def __init__(self, executable):
        self.counter = 0
        self.responses = queue.Queue()

        self.process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1
        )

        self.reader = threading.Thread(
            target=self.read_stdout,
            daemon=True
        )

        self.reader.start()

    def read_stdout(self):
        try:
            for line in self.process.stdout:
                self.responses.put(line)
        except Exception as error:
            self.responses.put(error)
        finally:
            self.responses.put(None)

    def send(self, command, params=None):
        if self.process.poll() is not None:
            raise RuntimeError(
                f"CLI is not running: {self.process.returncode}"
            )

        self.counter += 1
        request_id = f"request-{self.counter}"

        request = {
            "id": request_id,
            "command": command,
            "params": {} if params is None else params
        }

        self.process.stdin.write(
            json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n"
        )

        self.process.stdin.flush()

        return request_id

    def read(self, request_id):
        try:
            item = self.responses.get(timeout=REQUEST_TIMEOUT)
        except queue.Empty as error:
            raise TimeoutError(
                f"No CLI response for request {request_id}"
            ) from error

        if item is None:
            raise RuntimeError("CLI terminated before returning a response")

        if isinstance(item, Exception):
            raise RuntimeError("Could not read CLI stdout") from item

        response = json.loads(item)

        if not isinstance(response, dict):
            raise AssertionError(response)

        if response.get("id") != request_id:
            raise AssertionError(f"Unexpected response identifier: {response}")

        if type(response.get("ok")) is not bool:
            raise AssertionError(response)

        expected_fields = (
            {"id", "ok", "result"}
            if response["ok"]
            else {"id", "ok", "error"}
        )

        if set(response) != expected_fields:
            raise AssertionError(f"Invalid response structure: {response}")

        if not response["ok"]:
            if set(response["error"]) != {"code", "message", "details"}:
                raise AssertionError(response)

        return response

    def request(self, command, params=None):
        response = self.read(self.send(command, params))

        if not response["ok"]:
            raise RuntimeError(f"{command}: {response['error']}")

        return response["result"]

    def expect_error(self, command, params, expected_code):
        response = self.read(self.send(command, params))

        if response["ok"]:
            raise AssertionError(f"{command} unexpectedly succeeded")

        if response["error"]["code"] != expected_code:
            raise AssertionError(
                f"Expected {expected_code}, received {response['error']}"
            )

        return response["error"]

    def tessellate(
        self,
        transform_id,
        kind="SOLID",
        space="local",
        include_children=False
    ):
        request_id = self.send(
            "transformTessellate",
            {
                "id": transform_id,
                "options": {
                    "kind": kind,
                    "space": space,
                    "includeChildren": include_children,
                    "includeNormals": False,
                    "includeMaterials": True
                }
            }
        )

        mesh = {
            "vertices": [],
            "indices": [],
            "groups": []
        }

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.materials": 3,
            "mesh.end": 4
        }

        begin = None
        previous_order = -1
        materials_received = False

        while True:
            response = self.read(request_id)

            if not response["ok"]:
                raise RuntimeError(
                    f"Tessellation failed: {response['error']}"
                )

            event = response["result"]
            name = event["event"]

            if name not in event_order:
                raise AssertionError(f"Unexpected mesh event: {event}")

            order = event_order[name]

            if order < previous_order:
                raise AssertionError(f"Invalid mesh event order: {event}")

            previous_order = order

            if name == "mesh.begin":
                if begin is not None:
                    raise AssertionError("Duplicate mesh.begin")

                begin = event
                continue

            if begin is None:
                raise AssertionError("Mesh event received before mesh.begin")

            if name == "mesh.end":
                break

            if name == "mesh.materials":
                if materials_received:
                    raise AssertionError("Duplicate mesh.materials")

                mesh["groups"] = event["groups"]
                materials_received = True
                continue

            key = name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(f"Invalid mesh chunk offset: {event}")

            mesh[key].extend(event["values"])

        if not materials_received:
            raise AssertionError("Missing mesh.materials")

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError("Invalid triangle indices")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(f"Invalid vertex index: {index}")

        for value in mesh["vertices"]:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise AssertionError(f"Invalid vertex coordinate: {value}")

        return mesh

    def close(self):
        try:
            if self.process.poll() is None:
                result = self.request("exit")

                if result != {}:
                    raise AssertionError(f"Unexpected exit result: {result}")

                self.process.wait(timeout=10)

            if self.process.returncode != 0:
                raise RuntimeError(
                    f"CLI exited with code {self.process.returncode}"
                )
        finally:
            self.terminate()

    def terminate(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)

        if not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass

        self.reader.join(timeout=5)

        if not self.process.stdout.closed:
            self.process.stdout.close()


class NativeEntityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-native-entities-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)
        self.input_path = self.root_path / "input.ifc"
        self.save_counter = 0

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

    def tearDown(self):
        self.client.close()

    def load_fixture(self, fixture):
        self.input_path.write_text(
            fixture.build(),
            encoding="utf-8"
        )

        return self.load_model(self.input_path)

    def load_model(self, path):
        result = self.client.request(
            "load",
            {"path": str(path)}
        )

        self.assertEqual(result["path"], str(path))
        self.assertEqual(result["schema"].upper(), "IFC4")

        self.assertEqual(
            result["transformCount"],
            len(self.list_transforms())
        )

        self.assertEqual(
            result["geometryCount"],
            len(self.list_geometries())
        )

        self.assertEqual(
            result["materialCount"],
            len(self.list_materials())
        )

        return result

    def save_model(self):
        self.save_counter += 1

        path = self.root_path / f"roundtrip-{self.save_counter}.ifc"

        self.assertEqual(
            self.client.request("save", {"path": str(path)}),
            {"path": str(path)}
        )

        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)

        return path

    def save_entities(self):
        path = self.save_model()

        entities = parse_step_entities(
            path.read_text(encoding="utf-8")
        )

        self.assert_step_integrity(entities)

        return entities

    def save_and_reload(self):
        path = self.save_model()

        entities = parse_step_entities(
            path.read_text(encoding="utf-8")
        )

        self.assert_step_integrity(entities)
        self.load_model(path)

        return entities

    def list_transforms(self):
        return self.client.request("transformGetAll")

    def list_geometries(self, parent_id=None, kind="ALL"):
        return self.client.request(
            "geometryGetAll",
            {
                "transformId": parent_id,
                "kind": kind
            }
        )

    def list_materials(self):
        return self.client.request("materialGetAll")

    def get_transform(self, identifier):
        return self.client.request(
            "transformGet",
            {"id": identifier}
        )

    def get_geometry(self, identifier):
        return self.client.request(
            "geometryGet",
            {"id": identifier}
        )

    def get_material(self, identifier):
        return self.client.request(
            "materialGet",
            {"id": identifier}
        )

    def find_transform(self, global_id=PRODUCT_GLOBAL_ID):
        matches = [
            item for item in self.list_transforms()
            if item["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one transform with GlobalId {global_id}"
        )

        return self.get_transform(matches[0]["id"])

    def find_geometry(self, global_id):
        matches = [
            item for item in self.list_geometries()
            if item["globalId"] == global_id
        ]

        self.assertEqual(len(matches), 1)

        return self.get_geometry(matches[0]["id"])

    def find_material(self, step_id):
        matches = [
            item for item in self.list_materials()
            if item["ifcEntityId"] == step_id
        ]

        self.assertEqual(len(matches), 1)

        return self.get_material(matches[0]["id"])

    def native_geometry(self, transform, expected_class, kind="SOLID"):
        geometries = self.list_geometries(
            transform["id"],
            kind=kind
        )

        self.assertEqual(len(geometries), 1)

        geometry = self.get_geometry(geometries[0]["id"])

        self.assertEqual(geometry["ifcClass"], expected_class)
        self.assertEqual(geometry["source"], "ifc")
        self.assertEqual(geometry["kind"], kind)
        self.assertEqual(geometry["operation"], "BASE")
        self.assertEqual(geometry["parentId"], transform["id"])
        self.assertIs(geometry["editable"], False)
        self.assertIsNone(geometry["globalId"])

        self.assertEqual(
            geometry["definition"]["ifcClass"],
            expected_class
        )

        self.assertIn(
            geometry["id"],
            transform["geometries"]
        )

        return geometry

    def update_transform(self, identifier, **changes):
        return self.client.request(
            "transformUpdate",
            {
                "id": identifier,
                **changes
            }
        )

    def tessellate(self, transform=None, **options):
        if transform is None:
            transform = self.find_transform()

        return self.client.tessellate(
            transform["id"],
            **options
        )

    def assert_step_integrity(self, entities):
        for identifier, entity in entities.items():
            for reference in entity_references(entity):
                self.assertIn(
                    reference,
                    entities,
                    f"Dangling STEP reference #{identifier} -> #{reference}"
                )

        global_ids = [
            entity["arguments"][0]
            for entity in entities.values()
            if re.fullmatch(
                r"'[0-3][0-9A-Za-z_$]{21}'",
                entity["arguments"][0]
            )
        ]

        self.assertEqual(
            len(global_ids),
            len(set(global_ids))
        )

    def assert_matrix(self, actual, expected):
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for value, reference in zip(actual, expected):
            self.assertAlmostEqual(
                value,
                reference,
                delta=MESH_TOLERANCE
            )

    def assert_bounds(self, mesh, expected):
        actual = mesh_bounds(mesh)

        for value, reference in zip(actual, expected):
            self.assertAlmostEqual(
                value,
                reference,
                delta=MESH_TOLERANCE
            )

    def assert_volume(self, mesh, expected, tolerance=MESH_TOLERANCE):
        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected,
            delta=tolerance
        )

    def assert_surface(self, mesh, expected, tolerance=MESH_TOLERANCE):
        self.assertAlmostEqual(
            mesh_surface_area(mesh),
            expected,
            delta=tolerance
        )

    def assert_empty_mesh(self, mesh):
        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)
        self.assertEqual(mesh["vertices"], [])
        self.assertEqual(mesh["indices"], [])
        self.assertEqual(mesh["groups"], [])

    def assert_valid_mesh(self, mesh, watertight=True):
        self.assertGreater(mesh["vertexCount"], 0)
        self.assertGreater(mesh["indexCount"], 0)

        for a, b, c in mesh_triangles(mesh):
            normal = cross(
                subtract(b, a),
                subtract(c, a)
            )

            self.assertGreater(dot(normal, normal), 1e-20)

        if watertight:
            edges = mesh_edge_counts(mesh)

            self.assertTrue(edges)

            invalid = {
                edge: count
                for edge, count in edges.items()
                if count != 2
            }

            self.assertFalse(
                invalid,
                f"Open or non-manifold edges: {list(invalid.items())[:10]}"
            )

    def assert_mesh_equivalent(self, before, after):
        self.assertEqual(
            mesh_vertex_set(before),
            mesh_vertex_set(after)
        )

        self.assert_bounds(after, mesh_bounds(before))
        self.assert_volume(after, mesh_volume(before))
        self.assert_surface(after, mesh_surface_area(before))

    def assert_readonly_geometry(self, geometry):
        self.client.expect_error(
            "geometryUpdate",
            {
                "id": geometry["id"],
                "depth": 10.0
            },
            "GEOMETRY_NOT_EDITABLE"
        )

        self.assertEqual(
            self.get_geometry(geometry["id"]),
            geometry
        )

    def assert_native_box(
        self,
        class_name,
        width,
        length,
        depth,
        predefined_type="NOTDEFINED",
        kind="SOLID"
    ):
        fixture = Fixture()

        fixture.solid_product(
            class_name=class_name,
            width=width,
            length=length,
            depth=depth,
            predefined_type=predefined_type,
            name=f"Native {class_name}"
        )

        result = self.load_fixture(fixture)

        self.assertEqual(result["transformCount"], 2)
        self.assertEqual(result["geometryCount"], 1)

        transform = self.find_transform()

        self.assertEqual(
            transform["definition"]["ifcClass"],
            class_name
        )

        self.assertEqual(
            transform["name"],
            f"Native {class_name}"
        )

        geometry = self.native_geometry(
            transform,
            "IfcExtrudedAreaSolid",
            kind=kind
        )

        self.assert_readonly_geometry(geometry)

        mesh = self.tessellate(transform, kind=kind)

        self.assert_valid_mesh(mesh)

        self.assert_bounds(
            mesh,
            (
                -width / 2, -length / 2, 0,
                width / 2, length / 2, depth
            )
        )

        self.assert_volume(
            mesh,
            width * length * depth
        )

        self.assert_surface(
            mesh,
            2 * (
                width * length
                + width * depth
                + length * depth
            )
        )

        expected_vertices = {
            (x, y, z)
            for x in (-width / 2, width / 2)
            for y in (-length / 2, length / 2)
            for z in (0, depth)
        }

        self.assertEqual(
            mesh_vertex_set(mesh),
            expected_vertices
        )

        opposite_kind = "DOMAIN" if kind == "SOLID" else "SOLID"

        self.assertEqual(
            self.list_geometries(
                transform["id"],
                kind=opposite_kind
            ),
            []
        )

        self.assert_empty_mesh(
            self.tessellate(
                transform,
                kind=opposite_kind
            )
        )

        entities = self.save_and_reload()

        _, entity = find_step_root(
            entities,
            PRODUCT_GLOBAL_ID
        )

        self.assertEqual(entity["class"], class_name.upper())

        current = self.find_transform()

        self.native_geometry(
            current,
            "IfcExtrudedAreaSolid",
            kind=kind
        )

        self.assert_mesh_equivalent(
            mesh,
            self.tessellate(current, kind=kind)
        )

    def assert_native_representation(
        self,
        fixture,
        expected_class,
        expected_volume,
        expected_bounds=None,
        expected_surface=None,
        volume_tolerance=MESH_TOLERANCE,
        watertight=True
    ):
        self.load_fixture(fixture)

        transform = self.find_transform()

        geometry = self.native_geometry(
            transform,
            expected_class
        )

        self.assert_readonly_geometry(geometry)

        mesh = self.tessellate(transform)

        self.assert_valid_mesh(
            mesh,
            watertight=watertight
        )

        self.assert_volume(
            mesh,
            expected_volume,
            tolerance=volume_tolerance
        )

        if expected_bounds is not None:
            self.assert_bounds(mesh, expected_bounds)

        if expected_surface is not None:
            self.assert_surface(mesh, expected_surface)

        entities = self.save_and_reload()

        self.assertTrue(
            entities_by_class(entities, expected_class)
        )

        current = self.find_transform()

        self.native_geometry(
            current,
            expected_class
        )

        self.assert_mesh_equivalent(
            mesh,
            self.tessellate(current)
        )

        return mesh

    def test_native_wall(self):
        """Verify indexing and tessellation of a native IfcWall."""

        self.assert_native_box(
            class_name="IfcWall",
            width=4,
            length=0.25,
            depth=3,
            predefined_type="STANDARD"
        )

    def test_native_slab(self):
        """Verify indexing and tessellation of a native IfcSlab."""

        self.assert_native_box(
            class_name="IfcSlab",
            width=6,
            length=4,
            depth=0.25,
            predefined_type="FLOOR"
        )

    def test_native_beam(self):
        """Verify indexing and tessellation of a native IfcBeam."""

        self.assert_native_box(
            class_name="IfcBeam",
            width=5,
            length=0.5,
            depth=0.75,
            predefined_type="BEAM"
        )

    def test_native_column(self):
        """Verify indexing and tessellation of a native IfcColumn."""

        self.assert_native_box(
            class_name="IfcColumn",
            width=0.5,
            length=0.5,
            depth=4,
            predefined_type="COLUMN"
        )

    def test_native_space(self):
        """Verify classification of native IfcSpace representations as DOMAIN."""

        self.assert_native_box(
            class_name="IfcSpace",
            width=6,
            length=4,
            depth=3,
            predefined_type="INTERNAL",
            kind="DOMAIN"
        )

        transform = self.find_transform()

        mesh = self.tessellate(
            transform,
            kind="DOMAIN"
        )

        self.assert_volume(mesh, 72.0)
        self.assertEqual(mesh["groups"], [])

        self.assertEqual(
            self.list_geometries(kind="SOLID"),
            []
        )

    def test_native_spatial_zone(self):
        """Verify classification of native IfcSpatialZone representations."""

        self.assert_native_box(
            class_name="IfcSpatialZone",
            width=5,
            length=4,
            depth=3,
            predefined_type="NOTDEFINED",
            kind="DOMAIN"
        )

        transform = self.find_transform()

        self.assertEqual(
            transform["definition"]["ifcClass"],
            "IfcSpatialZone"
        )

        self.assert_volume(
            self.tessellate(transform, kind="DOMAIN"),
            60.0
        )

        self.assert_empty_mesh(
            self.tessellate(transform, kind="SOLID")
        )

        entities = self.save_entities()

        _, zone = find_step_root(
            entities,
            PRODUCT_GLOBAL_ID
        )

        self.assertNotEqual(
            zone["arguments"][4],
            "'ifc-cli:domain'"
        )

        managed_relations = [
            entity
            for entity in entities_by_class(
                entities,
                "IFCRELASSIGNSTOPRODUCT"
            ).values()
            if "'ifc-cli:domain-parent'" in entity["arguments"]
        ]

        self.assertEqual(managed_relations, [])

    def test_native_opening(self):
        """Verify subtraction of an existing native IfcOpeningElement."""

        fixture = Fixture()

        parent, _ = fixture.solid_product(
            class_name="IfcWall",
            predefined_type="STANDARD"
        )

        parent_placement = reference_ids(
            split_step_arguments(
                fixture.entities[parent - 1].split("(", 1)[1][:-2]
            )[5]
        )[0]

        opening_guid = fixture.guid()

        opening_solid = fixture.extrusion(
            width=1,
            length=1,
            depth=4
        )

        opening = fixture.product(
            class_name="IfcOpeningElement",
            global_id=opening_guid,
            name="Native through opening",
            representation=fixture.representation([opening_solid]),
            placement=fixture.placement(
                z=-1,
                parent=parent_placement
            ),
            predefined_type="OPENING"
        )

        fixture.relation(
            "IFCRELVOIDSELEMENT",
            f"#{parent},#{opening}"
        )

        self.load_fixture(fixture)

        transform = self.find_transform()
        geometry = self.find_geometry(opening_guid)

        self.assertEqual(geometry["ifcClass"], "IfcOpeningElement")
        self.assertEqual(geometry["operation"], "SUBTRACT")
        self.assertEqual(geometry["kind"], "SOLID")
        self.assertEqual(geometry["parentId"], transform["id"])

        self.assertNotIn(
            opening_guid,
            {item["globalId"] for item in self.list_transforms()}
        )

        self.assertEqual(
            len(self.list_geometries(transform["id"])),
            2
        )

        mesh = self.tessellate(transform)

        self.assert_valid_mesh(mesh)
        self.assert_volume(mesh, 22.0)
        self.assert_surface(mesh, 58.0)

        self.assert_bounds(
            mesh,
            (-2, -1.5, 0, 2, 1.5, 2)
        )

        entities = self.save_and_reload()

        parent_id, _ = find_step_root(
            entities,
            PRODUCT_GLOBAL_ID
        )

        opening_id, _ = find_step_root(
            entities,
            opening_guid
        )

        relations = list(
            entities_by_class(
                entities,
                "IFCRELVOIDSELEMENT"
            ).values()
        )

        self.assertEqual(len(relations), 1)

        self.assertEqual(
            relations[0]["arguments"][-2:],
            [f"#{parent_id}", f"#{opening_id}"]
        )

        self.assert_mesh_equivalent(
            mesh,
            self.tessellate()
        )

    def test_native_projection(self):
        """Verify additive union of an existing native IfcProjectionElement."""

        fixture = Fixture()

        parent_placement = fixture.placement()

        parent, _ = fixture.solid_product(
            class_name="IfcWall",
            predefined_type="STANDARD",
            placement=parent_placement
        )

        projection_guid = fixture.guid()

        projection_solid = fixture.extrusion(
            width=2,
            length=2,
            depth=2
        )

        projection = fixture.product(
            class_name="IfcProjectionElement",
            global_id=projection_guid,
            name="Native projection",
            representation=fixture.representation([projection_solid]),
            placement=fixture.placement(
                x=2,
                parent=parent_placement
            ),
            predefined_type="NOTDEFINED"
        )

        fixture.relation(
            "IFCRELPROJECTSELEMENT",
            f"#{parent},#{projection}"
        )

        self.load_fixture(fixture)

        transform = self.find_transform()
        geometry = self.find_geometry(projection_guid)

        self.assertEqual(
            geometry["ifcClass"],
            "IfcProjectionElement"
        )

        self.assertEqual(geometry["operation"], "ADD")
        self.assertEqual(geometry["kind"], "SOLID")
        self.assertEqual(geometry["parentId"], transform["id"])

        self.assertNotIn(
            projection_guid,
            {item["globalId"] for item in self.list_transforms()}
        )

        # Base volume: 24 m³.
        # Projection volume: 8 m³.
        # Shared volume: 4 m³.
        # Expected physical union: 28 m³.

        mesh = self.tessellate(transform)

        self.assert_valid_mesh(mesh)
        self.assert_volume(mesh, 28.0)
        self.assert_surface(mesh, 64.0)

        self.assert_bounds(
            mesh,
            (-2, -1.5, 0, 3, 1.5, 2)
        )

        entities = self.save_and_reload()

        parent_id, _ = find_step_root(
            entities,
            PRODUCT_GLOBAL_ID
        )

        projection_id, _ = find_step_root(
            entities,
            projection_guid
        )

        relations = list(
            entities_by_class(
                entities,
                "IFCRELPROJECTSELEMENT"
            ).values()
        )

        self.assertEqual(len(relations), 1)

        self.assertEqual(
            relations[0]["arguments"][-2:],
            [f"#{parent_id}", f"#{projection_id}"]
        )

        self.assert_mesh_equivalent(
            mesh,
            self.tessellate()
        )

    def test_native_mapped_item(self):
        """Verify tessellation of a native IfcMappedItem."""

        fixture = Fixture()

        solid = fixture.extrusion(
            width=2,
            length=3,
            depth=4
        )

        mapped_representation = fixture.shape_representation(
            [solid]
        )

        representation_map = fixture.add(
            "IFCREPRESENTATIONMAP",
            f"#{fixture.axis},#{mapped_representation}"
        )

        target_origin = fixture.point(5, 6, 7)

        mapping_target = fixture.add(
            "IFCCARTESIANTRANSFORMATIONOPERATOR3D",
            f"$,$,#{target_origin},1.,$"
        )

        mapped_item = fixture.add(
            "IFCMAPPEDITEM",
            f"#{representation_map},#{mapping_target}"
        )

        representation = fixture.representation(
            [mapped_item],
            "MappedRepresentation"
        )

        fixture.product(
            representation=representation
        )

        mesh = self.assert_native_representation(
            fixture,
            expected_class="IfcMappedItem",
            expected_volume=24.0,
            expected_bounds=(4, 4.5, 7, 6, 7.5, 11),
            expected_surface=52.0
        )

        self.assertEqual(
            len(mesh_vertex_set(mesh)),
            8
        )

        entities = self.save_entities()

        self.assertEqual(
            len(entities_by_class(entities, "IFCREPRESENTATIONMAP")),
            1
        )

        self.assertEqual(
            len(entities_by_class(entities, "IFCMAPPEDITEM")),
            1
        )

        self.assertEqual(
            len(entities_by_class(
                entities,
                "IFCCARTESIANTRANSFORMATIONOPERATOR3D"
            )),
            1
        )

    def test_native_brep(self):
        """Verify tessellation of a native IfcFacetedBrep."""

        fixture = Fixture()

        points = [
            fixture.point(*coordinates)
            for coordinates in CUBE_POINTS
        ]

        faces = []

        for indices in CUBE_FACES:
            loop = fixture.add(
                "IFCPOLYLOOP",
                step_references([
                    points[index]
                    for index in indices
                ])
            )

            bound = fixture.add(
                "IFCFACEOUTERBOUND",
                f"#{loop},.T."
            )

            face = fixture.add(
                "IFCFACE",
                f"(#{bound})"
            )

            faces.append(face)

        shell = fixture.add(
            "IFCCLOSEDSHELL",
            step_references(faces)
        )

        brep = fixture.add(
            "IFCFACETEDBREP",
            f"#{shell}"
        )

        representation = fixture.representation(
            [brep],
            "Brep"
        )

        fixture.product(
            representation=representation
        )

        mesh = self.assert_native_representation(
            fixture,
            expected_class="IfcFacetedBrep",
            expected_volume=8.0,
            expected_bounds=(0, 0, 0, 2, 2, 2),
            expected_surface=24.0
        )

        self.assertEqual(
            mesh_vertex_set(mesh),
            set(CUBE_POINTS)
        )

        entities = self.save_entities()

        self.assertEqual(
            len(entities_by_class(entities, "IFCFACETEDBREP")),
            1
        )

        self.assertEqual(
            len(entities_by_class(entities, "IFCCLOSEDSHELL")),
            1
        )

        self.assertEqual(
            len(entities_by_class(entities, "IFCFACE")),
            6
        )

        self.assertEqual(
            len(entities_by_class(entities, "IFCPOLYLOOP")),
            6
        )

    def test_native_triangulated_face_set(self):
        """Verify native IfcTriangulatedFaceSet indexing and tessellation."""

        fixture = Fixture()

        coordinates = "(" + ",".join(
            step_coordinates(point)
            for point in CUBE_POINTS
        ) + ")"

        point_list = fixture.add(
            "IFCCARTESIANPOINTLIST3D",
            coordinates
        )

        # IFC triangulated face sets use one-based vertex indices.

        coordinate_indices = "(" + ",".join(
            "(" + ",".join(str(index + 1) for index in triangle) + ")"
            for triangle in CUBE_TRIANGLES
        ) + ")"

        face_set = fixture.add(
            "IFCTRIANGULATEDFACESET",
            f"#{point_list},$,.T.,{coordinate_indices},$"
        )

        representation = fixture.representation(
            [face_set],
            "Tessellation"
        )

        fixture.product(
            representation=representation
        )

        mesh = self.assert_native_representation(
            fixture,
            expected_class="IfcTriangulatedFaceSet",
            expected_volume=8.0,
            expected_bounds=(0, 0, 0, 2, 2, 2),
            expected_surface=24.0
        )

        self.assertEqual(mesh["indexCount"], 36)

        self.assertEqual(
            mesh_vertex_set(mesh),
            set(CUBE_POINTS)
        )

        entities = self.save_entities()

        face_sets = list(
            entities_by_class(
                entities,
                "IFCTRIANGULATEDFACESET"
            ).values()
        )

        self.assertEqual(len(face_sets), 1)

        self.assertEqual(
            face_sets[0]["arguments"][2],
            ".T."
        )

        self.assertEqual(
            len(entities_by_class(
                entities,
                "IFCCARTESIANPOINTLIST3D"
            )),
            1
        )

    def test_native_revolved_solid(self):
        """Verify tessellation of a native IfcRevolvedAreaSolid."""

        fixture = Fixture()

        # Revolving a rectangle around the Y axis produces
        # a hollow cylinder with inner radius 1 m, outer radius
        # 3 m and axial length 3 m.

        profile = fixture.rectangle(
            width=2,
            length=3,
            x=2,
            y=0
        )

        revolution_axis = fixture.add(
            "IFCAXIS1PLACEMENT",
            f"#{fixture.origin},#{fixture.y_axis}"
        )

        solid = fixture.add(
            "IFCREVOLVEDAREASOLID",
            f"#{profile},#{fixture.axis},"
            f"#{revolution_axis},{step_real(2 * math.pi)}"
        )

        fixture.product(
            representation=fixture.representation(
                [solid],
                "SweptSolid"
            )
        )

        self.load_fixture(fixture)

        transform = self.find_transform()

        geometry = self.native_geometry(
            transform,
            "IfcRevolvedAreaSolid"
        )

        self.assert_readonly_geometry(geometry)

        mesh = self.tessellate(transform)

        self.assert_valid_mesh(mesh)

        expected_volume = math.pi * (3 ** 2 - 1 ** 2) * 3

        # Curved surfaces are approximated by triangles, so
        # a larger absolute tolerance is required.

        self.assert_volume(
            mesh,
            expected_volume,
            tolerance=CURVED_VOLUME_TOLERANCE
        )

        expected_surface = (
            2 * math.pi * 3 * 3
            + 2 * math.pi * 1 * 3
            + 2 * math.pi * (3 ** 2 - 1 ** 2)
        )

        self.assert_surface(
            mesh,
            expected_surface,
            tolerance=CURVED_VOLUME_TOLERANCE
        )

        self.assertGreater(
            len(mesh_vertex_set(mesh)),
            16
        )

        points = mesh_points(mesh)

        for x, y, z in points:
            self.assertGreaterEqual(y, -1.5 - MESH_TOLERANCE)
            self.assertLessEqual(y, 1.5 + MESH_TOLERANCE)

            radius = math.hypot(x, z)

            self.assertTrue(
                math.isclose(radius, 1.0, abs_tol=1e-5)
                or math.isclose(radius, 3.0, abs_tol=1e-5),
                f"Unexpected revolution radius: {radius}"
            )

        bounds = mesh_bounds(mesh)

        self.assertAlmostEqual(bounds[1], -1.5, delta=MESH_TOLERANCE)
        self.assertAlmostEqual(bounds[4], 1.5, delta=MESH_TOLERANCE)

        for value, expected in zip(
            (bounds[0], bounds[2], bounds[3], bounds[5]),
            (-3, -3, 3, 3)
        ):
            self.assertAlmostEqual(value, expected, delta=0.02)

        entities = self.save_and_reload()

        revolved_solids = list(
            entities_by_class(
                entities,
                "IFCREVOLVEDAREASOLID"
            ).values()
        )

        self.assertEqual(len(revolved_solids), 1)

        self.assertAlmostEqual(
            float(revolved_solids[0]["arguments"][-1]),
            2 * math.pi,
            delta=MESH_TOLERANCE
        )

        self.native_geometry(
            self.find_transform(),
            "IfcRevolvedAreaSolid"
        )

        reloaded_mesh = self.tessellate()

        self.assert_volume(
            reloaded_mesh,
            mesh_volume(mesh)
        )

        self.assert_surface(
            reloaded_mesh,
            mesh_surface_area(mesh)
        )

        self.assert_bounds(
            reloaded_mesh,
            mesh_bounds(mesh)
        )

    def test_native_boolean_result(self):
        """Verify native IfcBooleanResult difference tessellation."""

        fixture = Fixture()

        base = fixture.extrusion(
            width=4,
            length=3,
            depth=2
        )

        subtraction = fixture.extrusion(
            width=1,
            length=1,
            depth=4,
            z=-1
        )

        boolean_result = fixture.add(
            "IFCBOOLEANRESULT",
            f".DIFFERENCE.,#{base},#{subtraction}"
        )

        representation = fixture.representation(
            [boolean_result],
            "CSG"
        )

        fixture.product(
            representation=representation
        )

        self.assert_native_representation(
            fixture,
            expected_class="IfcBooleanResult",
            expected_volume=22.0,
            expected_bounds=(-2, -1.5, 0, 2, 1.5, 2),
            expected_surface=58.0
        )

        entities = self.save_entities()

        results = list(
            entities_by_class(
                entities,
                "IFCBOOLEANRESULT"
            ).values()
        )

        self.assertEqual(len(results), 1)

        self.assertEqual(
            results[0]["arguments"][0],
            ".DIFFERENCE."
        )

        operands = entity_references(results[0])

        self.assertEqual(len(operands), 2)

        for identifier in operands:
            self.assertEqual(
                entities[identifier]["class"],
                "IFCEXTRUDEDAREASOLID"
            )

        self.assertEqual(
            len(entities_by_class(entities, "IFCRELVOIDSELEMENT")),
            0
        )

    def test_native_complex_properties(self):
        """Verify preservation of complex native IFC properties."""

        fixture = Fixture()

        product, _ = fixture.solid_product()

        simple = fixture.add(
            "IFCPROPERTYSINGLEVALUE",
            "'EditableReference',$,IFCLABEL('ORIGINAL'),$"
        )

        nested = fixture.add(
            "IFCPROPERTYSINGLEVALUE",
            "'NestedReference',$,IFCLABEL('COMPLEX-VALUE'),$"
        )

        complex_property = fixture.add(
            "IFCCOMPLEXPROPERTY",
            f"'ComplexProperty','Native description','NativeUsage',"
            f"(#{nested})"
        )

        list_property = fixture.add(
            "IFCPROPERTYLISTVALUE",
            "'NativeList',$,(IFCLABEL('A'),IFCLABEL('B'),IFCLABEL('C')),$"
        )

        bounded_property = fixture.add(
            "IFCPROPERTYBOUNDEDVALUE",
            "'NativeBounds',$,IFCREAL(100.),IFCREAL(0.),$,IFCREAL(50.)"
        )

        enumeration = fixture.add(
            "IFCPROPERTYENUMERATION",
            "'NativeEnumeration',(IFCLABEL('Low'),IFCLABEL('High')),$"
        )

        enumerated_property = fixture.add(
            "IFCPROPERTYENUMERATEDVALUE",
            f"'NativeSelection',$,(IFCLABEL('High')),#{enumeration}"
        )

        property_set = fixture.property_set(
            "Pset_NativeComplex",
            [
                simple,
                complex_property,
                list_property,
                bounded_property,
                enumerated_property
            ]
        )

        fixture.assign_properties(product, property_set)

        self.load_fixture(fixture)

        transform = self.find_transform()

        self.assertIn(
            property_input(
                "EditableReference",
                "ORIGINAL",
                property_set="Pset_NativeComplex"
            ),
            transform["properties"]
        )

        native_complex_names = {
            "ComplexProperty",
            "NativeList",
            "NativeBounds",
            "NativeSelection"
        }

        self.assertFalse(
            native_complex_names & {
                item["name"]
                for item in transform["properties"]
            }
        )

        before = self.save_entities()

        complex_classes = {
            "IFCCOMPLEXPROPERTY",
            "IFCPROPERTYLISTVALUE",
            "IFCPROPERTYBOUNDEDVALUE",
            "IFCPROPERTYENUMERATEDVALUE"
        }

        original_complex_ids = {
            identifier
            for identifier, entity in before.items()
            if entity["class"] in complex_classes
        }

        self.assertEqual(len(original_complex_ids), 4)

        replacement = property_input(
            "EditableReference",
            "UPDATED",
            property_set="Pset_NativeComplex"
        )

        updated = self.update_transform(
            transform["id"],
            properties=[replacement]
        )

        self.assertIn(
            replacement,
            updated["properties"]
        )

        after = self.save_and_reload()

        # Verify that complex properties remain associated
        # with the product instead of merely surviving as orphans.

        product_id, _ = find_step_root(
            after,
            PRODUCT_GLOBAL_ID
        )

        property_sets = []

        for relation in entities_by_class(
            after,
            "IFCRELDEFINESBYPROPERTIES"
        ).values():
            arguments = relation["arguments"]

            if product_id not in reference_ids(arguments[-2]):
                continue

            property_sets.extend(reference_ids(arguments[-1]))

        property_graph = forward_graph(
            after,
            property_sets
        )

        preserved_complex = [
            after[identifier]
            for identifier in property_graph
            if after[identifier]["class"] in complex_classes
        ]

        self.assertEqual(len(preserved_complex), 4)

        self.assertEqual(
            {entity["arguments"][0] for entity in preserved_complex},
            {step_string(name) for name in native_complex_names}
        )

        before_by_name = {
            before[identifier]["arguments"][0]: before[identifier]
            for identifier in original_complex_ids
        }

        for entity in preserved_complex:
            original = before_by_name[entity["arguments"][0]]

            self.assertEqual(
                entity["class"],
                original["class"]
            )

            # Compare primitive attributes separately from STEP references.

            self.assertEqual(
                [
                    value for value in entity["arguments"]
                    if not reference_ids(value)
                ],
                [
                    value for value in original["arguments"]
                    if not reference_ids(value)
                ]
            )

        nested_values = [
            after[identifier]
            for identifier in property_graph
            if (
                after[identifier]["class"] == "IFCPROPERTYSINGLEVALUE"
                and after[identifier]["arguments"][0] == "'NestedReference'"
            )
        ]

        self.assertEqual(len(nested_values), 1)

        self.assertEqual(
            nested_values[0]["arguments"][2],
            "IFCLABEL('COMPLEX-VALUE')"
        )

        self.assertIn(
            replacement,
            self.find_transform()["properties"]
        )

        # Clearing editable properties must preserve complex values.

        current = self.find_transform()

        self.update_transform(
            current["id"],
            properties=[]
        )

        entities = self.save_and_reload()

        product_id, _ = find_step_root(
            entities,
            PRODUCT_GLOBAL_ID
        )

        property_roots = [
            reference
            for relation in entities_by_class(
                entities,
                "IFCRELDEFINESBYPROPERTIES"
            ).values()
            if product_id in reference_ids(relation["arguments"][-2])
            for reference in reference_ids(relation["arguments"][-1])
        ]

        remaining_graph = forward_graph(
            entities,
            property_roots
        )

        remaining_names = {
            entities[identifier]["arguments"][0]
            for identifier in remaining_graph
            if entities[identifier]["class"] in complex_classes
        }

        self.assertEqual(
            remaining_names,
            {step_string(name) for name in native_complex_names}
        )

        self.assertEqual(
            self.find_transform()["properties"],
            []
        )

    def test_native_material_layers(self):
        """Verify indexing of native multilayer material structures."""

        fixture = Fixture()

        product, _ = fixture.solid_product(
            class_name="IfcWall",
            predefined_type="STANDARD"
        )

        concrete = fixture.material("Concrete", "concrete")
        insulation = fixture.material("Insulation", "insulation")

        concrete_layer = fixture.add(
            "IFCMATERIALLAYER",
            f"#{concrete},0.2,.F.,'Concrete layer',$,'structural',$"
        )

        insulation_layer = fixture.add(
            "IFCMATERIALLAYER",
            f"#{insulation},0.1,.F.,'Insulation layer',$,'thermal',$"
        )

        layer_set = fixture.add(
            "IFCMATERIALLAYERSET",
            f"(#{concrete_layer},#{insulation_layer}),"
            "'Native wall layers','Native multilayer construction'"
        )

        usage = fixture.add(
            "IFCMATERIALLAYERSETUSAGE",
            f"#{layer_set},.AXIS2.,.POSITIVE.,0.,$"
        )

        fixture.assign_material(product, usage)

        result = self.load_fixture(fixture)

        self.assertEqual(result["materialCount"], 6)

        expected_classes = {
            concrete: "IfcMaterial",
            insulation: "IfcMaterial",
            concrete_layer: "IfcMaterialLayer",
            insulation_layer: "IfcMaterialLayer",
            layer_set: "IfcMaterialLayerSet",
            usage: "IfcMaterialLayerSetUsage"
        }

        for step_id, expected_class in expected_classes.items():
            with self.subTest(ifc_class=expected_class):
                material = self.find_material(step_id)

                self.assertEqual(
                    material["definition"]["ifcClass"],
                    expected_class
                )

                self.assertIs(material["editable"], False)

                self.client.expect_error(
                    "materialUpdate",
                    {
                        "id": material["id"],
                        "name": "Invalid native material update"
                    },
                    "MATERIAL_NOT_EDITABLE"
                )

        transform = self.find_transform()
        usage_material = self.find_material(usage)

        self.assertEqual(
            transform["materials"],
            [usage_material["id"]]
        )

        mesh = self.tessellate(transform)

        self.assert_valid_mesh(mesh)
        self.assert_volume(mesh, 24.0)

        entities = self.save_and_reload()

        saved_layer_sets = list(
            entities_by_class(
                entities,
                "IFCMATERIALLAYERSET"
            ).items()
        )

        self.assertEqual(len(saved_layer_sets), 1)

        saved_layer_set_id, saved_layer_set = saved_layer_sets[0]

        layers = reference_ids(
            saved_layer_set["arguments"][0]
        )

        self.assertEqual(len(layers), 2)

        for layer_id, expected_name, expected_thickness in zip(
            layers,
            ("Concrete", "Insulation"),
            (0.2, 0.1)
        ):
            layer = entities[layer_id]

            self.assertEqual(layer["class"], "IFCMATERIALLAYER")

            self.assertAlmostEqual(
                float(layer["arguments"][1]),
                expected_thickness
            )

            material_id = reference_ids(layer["arguments"][0])[0]

            self.assertEqual(
                entities[material_id]["arguments"][0],
                step_string(expected_name)
            )

        saved_usages = list(
            entities_by_class(
                entities,
                "IFCMATERIALLAYERSETUSAGE"
            ).items()
        )

        self.assertEqual(len(saved_usages), 1)

        saved_usage_id, saved_usage = saved_usages[0]

        self.assertEqual(
            saved_usage["arguments"][0],
            f"#{saved_layer_set_id}"
        )

        self.assertEqual(
            saved_usage["arguments"][1:3],
            [".AXIS2.", ".POSITIVE."]
        )

        self.assertEqual(
            self.find_transform()["materials"],
            [self.find_material(saved_usage_id)["id"]]
        )

        self.assertEqual(len(self.list_materials()), 6)

        self.assert_mesh_equivalent(
            mesh,
            self.tessellate()
        )

    def test_native_material_constituents(self):
        """Verify indexing of native material constituent sets."""

        fixture = Fixture()

        product, _ = fixture.solid_product()

        steel = fixture.material("Steel", "metal")
        glass = fixture.material("Glass", "glass")

        steel_constituent = fixture.add(
            "IFCMATERIALCONSTITUENT",
            f"'Frame',$,#{steel},0.4,'structural'"
        )

        glass_constituent = fixture.add(
            "IFCMATERIALCONSTITUENT",
            f"'Panel',$,#{glass},0.6,'glazing'"
        )

        constituent_set = fixture.add(
            "IFCMATERIALCONSTITUENTSET",
            "'Native constituent set',$,"
            f"(#{steel_constituent},#{glass_constituent})"
        )

        fixture.assign_material(
            product,
            constituent_set
        )

        result = self.load_fixture(fixture)

        self.assertEqual(result["materialCount"], 5)

        expected_classes = {
            steel: "IfcMaterial",
            glass: "IfcMaterial",
            steel_constituent: "IfcMaterialConstituent",
            glass_constituent: "IfcMaterialConstituent",
            constituent_set: "IfcMaterialConstituentSet"
        }

        for step_id, expected_class in expected_classes.items():
            with self.subTest(ifc_class=expected_class):
                material = self.find_material(step_id)

                self.assertEqual(
                    material["definition"]["ifcClass"],
                    expected_class
                )

                self.assertIs(material["editable"], False)

        material = self.find_material(constituent_set)

        self.assertEqual(
            self.find_transform()["materials"],
            [material["id"]]
        )

        self.client.expect_error(
            "materialUpdate",
            {
                "id": material["id"],
                "category": "invalid modification"
            },
            "MATERIAL_NOT_EDITABLE"
        )

        mesh = self.tessellate()

        self.assert_volume(mesh, 24.0)

        entities = self.save_and_reload()

        sets = list(
            entities_by_class(
                entities,
                "IFCMATERIALCONSTITUENTSET"
            ).items()
        )

        self.assertEqual(len(sets), 1)

        set_id, saved_set = sets[0]

        constituents = [
            entities[identifier]
            for identifier in reference_ids(saved_set["arguments"][-1])
        ]

        self.assertEqual(len(constituents), 2)

        by_name = {
            constituent["arguments"][0]: constituent
            for constituent in constituents
        }

        expected = {
            "'Frame'": ("Steel", 0.4),
            "'Panel'": ("Glass", 0.6)
        }

        for name, (material_name, fraction) in expected.items():
            constituent = by_name[name]

            self.assertEqual(
                constituent["class"],
                "IFCMATERIALCONSTITUENT"
            )

            material_id = reference_ids(
                constituent["arguments"][2]
            )[0]

            self.assertEqual(
                entities[material_id]["arguments"][0],
                step_string(material_name)
            )

            self.assertAlmostEqual(
                float(constituent["arguments"][3]),
                fraction
            )

        self.assertAlmostEqual(
            sum(
                float(constituent["arguments"][3])
                for constituent in constituents
            ),
            1.0
        )

        self.assertEqual(
            self.find_transform()["materials"],
            [self.find_material(set_id)["id"]]
        )

        self.assertEqual(len(self.list_materials()), 5)

        self.assert_mesh_equivalent(
            mesh,
            self.tessellate()
        )

    def test_native_type_relations(self):
        """Verify preservation of occurrence-to-type relations."""

        fixture = Fixture()

        first, _ = fixture.solid_product(
            class_name="IfcWall",
            predefined_type="STANDARD"
        )

        second, _ = fixture.solid_product(
            class_name="IfcWall",
            global_id=SECOND_PRODUCT_GLOBAL_ID,
            name="Second typed wall",
            placement=fixture.placement(x=10),
            predefined_type="STANDARD"
        )

        type_property = fixture.add(
            "IFCPROPERTYSINGLEVALUE",
            "'TypeReference',$,IFCLABEL('WALL-TYPE-A'),$"
        )

        type_property_set = fixture.property_set(
            "Pset_NativeType",
            [type_property]
        )

        type_guid = fixture.guid()

        wall_type = fixture.add(
            "IFCWALLTYPE",
            f"'{type_guid}',$,'Native wall type','Type description',$,"
            f"(#{type_property_set}),$,'TYPE-TAG',$,.STANDARD."
        )

        fixture.relation(
            "IFCRELDEFINESBYTYPE",
            f"(#{first},#{second}),#{wall_type}",
            name="Native type relation"
        )

        self.load_fixture(fixture)

        expected_type_property = {
            "propertySet": "Pset_NativeType",
            "name": "TypeReference",
            "valueType": "IfcLabel",
            "value": "WALL-TYPE-A",
            "unit": None,
            "source": "type"
        }

        for global_id in (
            PRODUCT_GLOBAL_ID,
            SECOND_PRODUCT_GLOBAL_ID
        ):
            transform = self.find_transform(global_id)

            self.assertIn(
                expected_type_property,
                transform["properties"]
            )

        first_transform = self.find_transform()

        occurrence_property = property_input(
            "OccurrenceReference",
            "WALL-OCCURRENCE-A"
        )

        updated = self.update_transform(
            first_transform["id"],
            name="Updated typed wall",
            properties=[occurrence_property]
        )

        self.assertIn(
            expected_type_property,
            updated["properties"]
        )

        self.assertIn(
            occurrence_property,
            updated["properties"]
        )

        second_transform = self.find_transform(
            SECOND_PRODUCT_GLOBAL_ID
        )

        self.assertIn(
            expected_type_property,
            second_transform["properties"]
        )

        self.assertNotIn(
            occurrence_property,
            second_transform["properties"]
        )

        entities = self.save_and_reload()

        type_id, type_entity = find_step_root(
            entities,
            type_guid
        )

        self.assertEqual(type_entity["class"], "IFCWALLTYPE")

        self.assertEqual(
            type_entity["arguments"][2],
            "'Native wall type'"
        )

        first_id, _ = find_step_root(
            entities,
            PRODUCT_GLOBAL_ID
        )

        second_id, _ = find_step_root(
            entities,
            SECOND_PRODUCT_GLOBAL_ID
        )

        relations = list(
            entities_by_class(
                entities,
                "IFCRELDEFINESBYTYPE"
            ).values()
        )

        self.assertEqual(len(relations), 1)

        relation = relations[0]

        self.assertEqual(
            set(reference_ids(relation["arguments"][-2])),
            {first_id, second_id}
        )

        self.assertEqual(
            relation["arguments"][-1],
            f"#{type_id}"
        )

        for global_id in (
            PRODUCT_GLOBAL_ID,
            SECOND_PRODUCT_GLOBAL_ID
        ):
            self.assertIn(
                expected_type_property,
                self.find_transform(global_id)["properties"]
            )

        self.assertIn(
            occurrence_property,
            self.find_transform()["properties"]
        )

        current = self.find_transform()

        self.update_transform(
            current["id"],
            properties=[]
        )

        self.save_and_reload()

        self.assertEqual(
            self.find_transform()["properties"],
            [expected_type_property]
        )

    def test_native_shared_representation(self):
        """Verify preservation of shared native representation references."""

        fixture = Fixture()

        solid = fixture.extrusion(
            width=2,
            length=3,
            depth=4
        )

        representation = fixture.representation(
            [solid]
        )

        fixture.product(
            representation=representation,
            placement=fixture.placement(),
            name="First shared representation product"
        )

        fixture.product(
            global_id=SECOND_PRODUCT_GLOBAL_ID,
            representation=representation,
            placement=fixture.placement(x=20),
            name="Second shared representation product"
        )

        self.load_fixture(fixture)

        first = self.find_transform()
        second = self.find_transform(SECOND_PRODUCT_GLOBAL_ID)

        self.assertNotEqual(
            first["id"],
            second["id"]
        )

        first_mesh = self.tessellate(
            first,
            space="world"
        )

        second_mesh = self.tessellate(
            second,
            space="world"
        )

        self.assert_valid_mesh(first_mesh)
        self.assert_valid_mesh(second_mesh)

        self.assert_volume(first_mesh, 24.0)
        self.assert_volume(second_mesh, 24.0)

        self.assert_bounds(
            first_mesh,
            (-1, -1.5, 0, 1, 1.5, 4)
        )

        self.assert_bounds(
            second_mesh,
            (19, -1.5, 0, 21, 1.5, 4)
        )

        self.assert_mesh_equivalent(
            self.tessellate(first, space="local"),
            self.tessellate(second, space="local")
        )

        before = self.save_entities()

        _, first_entity = find_step_root(
            before,
            PRODUCT_GLOBAL_ID
        )

        _, second_entity = find_step_root(
            before,
            SECOND_PRODUCT_GLOBAL_ID
        )

        self.assertEqual(
            first_entity["arguments"][6],
            second_entity["arguments"][6]
        )

        self.assertNotEqual(
            first_entity["arguments"][6],
            "$"
        )

        # Updating one product must not modify the shared representation
        # or the placement of the other product.

        self.update_transform(
            first["id"],
            name="Updated shared representation product",
            transform=transform_input(
                translation_matrix(5, 0, 0)
            )
        )

        self.assert_mesh_equivalent(
            second_mesh,
            self.tessellate(second, space="world")
        )

        moved_mesh = self.tessellate(
            first,
            space="world"
        )

        self.assert_bounds(
            moved_mesh,
            (4, -1.5, 0, 6, 1.5, 4)
        )

        self.assert_volume(moved_mesh, 24.0)

        entities = self.save_and_reload()

        _, first_entity = find_step_root(
            entities,
            PRODUCT_GLOBAL_ID
        )

        _, second_entity = find_step_root(
            entities,
            SECOND_PRODUCT_GLOBAL_ID
        )

        self.assertEqual(
            first_entity["arguments"][6],
            second_entity["arguments"][6]
        )

        self.assertEqual(
            len(entities_by_class(
                entities,
                "IFCPRODUCTDEFINITIONSHAPE"
            )),
            1
        )

        self.assertEqual(
            len(entities_by_class(
                entities,
                "IFCEXTRUDEDAREASOLID"
            )),
            1
        )

        self.assert_mesh_equivalent(
            moved_mesh,
            self.tessellate(
                self.find_transform(),
                space="world"
            )
        )

        self.assert_mesh_equivalent(
            second_mesh,
            self.tessellate(
                self.find_transform(SECOND_PRODUCT_GLOBAL_ID),
                space="world"
            )
        )

    def test_native_alternative_placement(self):
        """Verify inspection and preservation of a native IfcGridPlacement."""

        fixture = Fixture()

        def grid_axis(tag, first, second):
            points = [
                fixture.point(*first),
                fixture.point(*second)
            ]

            curve = fixture.add(
                "IFCPOLYLINE",
                step_references(points)
            )

            return fixture.add(
                "IFCGRIDAXIS",
                f"{step_string(tag)},#{curve},.T."
            )

        u_axis = grid_axis(
            "U1",
            (-10, 0),
            (10, 0)
        )

        v_axis = grid_axis(
            "V1",
            (0, -10),
            (0, 10)
        )

        u_axis_2 = grid_axis(
            "U2",
            (-10, 5),
            (10, 5)
        )

        v_axis_2 = grid_axis(
            "V2",
            (5, -10),
            (5, 10)
        )

        grid_guid = fixture.guid()
        grid_placement = fixture.placement()

        fixture.add(
            "IFCGRID",
            f"'{grid_guid}',$,'Native grid',$,$,#{grid_placement},$,"
            f"(#{u_axis},#{u_axis_2}),(#{v_axis},#{v_axis_2}),$,"
            ".RECTANGULAR."
        )

        intersection = fixture.add(
            "IFCVIRTUALGRIDINTERSECTION",
            f"(#{u_axis},#{v_axis}),(0.,0.)"
        )

        alternative_placement = fixture.add(
            "IFCGRIDPLACEMENT",
            f"#{intersection},$"
        )

        solid = fixture.extrusion(
            width=2,
            length=3,
            depth=4
        )

        fixture.product(
            name="Grid placed native product",
            placement=alternative_placement,
            representation=fixture.representation([solid])
        )

        self.load_fixture(fixture)

        transform = self.find_transform()

        self.assertEqual(
            transform["transforms"]["sourceType"],
            "IfcGridPlacement"
        )

        self.assertIs(
            transform["transforms"]["editable"],
            False
        )

        # The simplified API exposes identity matrices for unsupported
        # placement types rather than resolving their native coordinates.

        self.assert_matrix(
            transform["transforms"]["local"],
            IDENTITY_MATRIX
        )

        self.assert_matrix(
            transform["transforms"]["world"],
            IDENTITY_MATRIX
        )

        self.native_geometry(
            transform,
            "IfcExtrudedAreaSolid"
        )

        before = self.save_entities()

        self.client.expect_error(
            "transformUpdate",
            {
                "id": transform["id"],
                "transform": transform_input(
                    translation_matrix(10, 20, 30)
                )
            },
            "TRANSFORM_NOT_EDITABLE"
        )

        self.assertEqual(
            self.get_transform(transform["id"]),
            transform
        )

        self.assertEqual(
            self.save_entities(),
            before
        )

        # Non-placement attributes remain independently editable.

        updated = self.update_transform(
            transform["id"],
            name="Renamed grid placed product"
        )

        self.assertEqual(
            updated["name"],
            "Renamed grid placed product"
        )

        self.assertEqual(
            updated["transforms"]["sourceType"],
            "IfcGridPlacement"
        )

        self.assertIs(
            updated["transforms"]["editable"],
            False
        )

        entities = self.save_and_reload()

        _, product = find_step_root(
            entities,
            PRODUCT_GLOBAL_ID
        )

        placement_ids = reference_ids(
            product["arguments"][5]
        )

        self.assertEqual(len(placement_ids), 1)

        placement = entities[placement_ids[0]]

        self.assertEqual(
            placement["class"],
            "IFCGRIDPLACEMENT"
        )

        placement_graph = forward_graph(
            entities,
            placement_ids
        )

        placement_classes = Counter(
            entities[identifier]["class"]
            for identifier in placement_graph
        )

        self.assertEqual(
            placement_classes["IFCGRIDPLACEMENT"],
            1
        )

        self.assertEqual(
            placement_classes["IFCVIRTUALGRIDINTERSECTION"],
            1
        )

        self.assertEqual(
            placement_classes["IFCGRIDAXIS"],
            2
        )

        self.assertEqual(
            placement_classes["IFCPOLYLINE"],
            2
        )

        _, grid = find_step_root(
            entities,
            grid_guid
        )

        self.assertEqual(grid["class"], "IFCGRID")

        current = self.find_transform()

        self.assertEqual(
            current["name"],
            "Renamed grid placed product"
        )

        self.assertEqual(
            current["transforms"]["sourceType"],
            "IfcGridPlacement"
        )

        self.assertIs(
            current["transforms"]["editable"],
            False
        )

        self.client.expect_error(
            "transformUpdate",
            {
                "id": current["id"],
                "transform": transform_input()
            },
            "TRANSFORM_NOT_EDITABLE"
        )


def main():
    global EXECUTABLE

    arguments = sys.argv[1:]

    if arguments and not arguments[0].startswith("-"):
        EXECUTABLE = Path(arguments.pop(0)).resolve()

    if not EXECUTABLE.is_file():
        raise FileNotFoundError(
            f"CLI executable not found: {EXECUTABLE}"
        )

    unittest.main(
        argv=[sys.argv[0], *arguments],
        verbosity=2
    )


if __name__ == "__main__":
    main()