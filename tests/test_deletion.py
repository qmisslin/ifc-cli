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
# Not executed against the compiled CLI.
#
# Deletion is validated at three levels:
# - Session indexes and API references.
# - Physical and domain tessellation results.
# - Native STEP entities and their references.
#
# Orphan cleanup tests intentionally enforce complete cleanup.
# The current CLI may leave unreferenced entities because some cleanup
# operations process dependencies before their owning entities.
# Exclusive property set deletion may also leave orphan property values.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 60
NUMERIC_TOLERANCE = 1e-7
MESH_TOLERANCE = 1e-6

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

ROOT_GLOBAL_ID = "0000000000000000000001"
SURVIVOR_GLOBAL_ID = "0000000000000000000002"
STOREY_GLOBAL_ID = "0000000000000000000003"
PROJECT_GLOBAL_ID = "0000000000000000000004"

DEFAULT_PROFILE = [
    [0, 0],
    [2, 0],
    [2, 3],
    [0, 3]
]

DEFAULT_VISUAL = {
    "color": [0.4, 0.6, 0.8],
    "opacity": 1.0,
    "metallic": 0.2,
    "roughness": 0.5
}

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('deletion-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCDIRECTION((0.,0.,1.));
#3=IFCDIRECTION((1.,0.,0.));
#4=IFCAXIS2PLACEMENT3D(#1,#2,#3);
#5=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-5,#4,$);
#6=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#7=IFCUNITASSIGNMENT((#6));
#8=IFCPROJECT('0000000000000000000004',$,'Deletion test project',$,$,$,$,(#5),#7);
#9=IFCLOCALPLACEMENT($,#4);
#10=IFCCARTESIANPOINT((0.,0.));
#11=IFCAXIS2PLACEMENT2D(#10,$);
#12=IFCRECTANGLEPROFILEDEF(.AREA.,$,#11,2.,3.);
#13=IFCEXTRUDEDAREASOLID(#12,#4,#2,4.);
#14=IFCCARTESIANPOINT((5.,0.,0.));
#15=IFCAXIS2PLACEMENT3D(#14,#2,#3);
#16=IFCEXTRUDEDAREASOLID(#12,#15,#2,4.);
#17=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#13,#16));
#18=IFCPRODUCTDEFINITIONSHAPE($,$,(#17));
#19=IFCBUILDINGELEMENTPROXY('0000000000000000000001',$,'Root product',$,$,#9,#18,$,.NOTDEFINED.);
#20=IFCCARTESIANPOINT((20.,0.,0.));
#21=IFCAXIS2PLACEMENT3D(#20,#2,#3);
#22=IFCLOCALPLACEMENT($,#21);
#23=IFCEXTRUDEDAREASOLID(#12,#4,#2,4.);
#24=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#23));
#25=IFCPRODUCTDEFINITIONSHAPE($,$,(#24));
#26=IFCBUILDINGELEMENTPROXY('0000000000000000000002',$,'Surviving product',$,$,#22,#25,$,.NOTDEFINED.);
#27=IFCLOCALPLACEMENT($,#4);
#28=IFCBUILDINGSTOREY('0000000000000000000003',$,'External storey',$,$,#27,$,$,.ELEMENT.,0.);
#29=IFCRELCONTAINEDINSPATIALSTRUCTURE('0000000000000000000005',$,$,$,(#19,#26),#28);
#30=IFCPROPERTYSINGLEVALUE('SharedReference',$,IFCLABEL('SHARED-PROPERTY-VALUE'),$);
#31=IFCPROPERTYSET('0000000000000000000006',$,'Pset_Shared',$,(#30));
#32=IFCRELDEFINESBYPROPERTIES('0000000000000000000007',$,$,$,(#19,#26),#31);
ENDSEC;
END-ISO-10303-21;
"""


def translation_matrix(x=0.0, y=0.0, z=0.0):
    return [
        1, 0, 0, x,
        0, 1, 0, y,
        0, 0, 1, z,
        0, 0, 0, 1
    ]


def transform_input(x=0.0, y=0.0, z=0.0, space="parent"):
    return {
        "space": space,
        "matrix": translation_matrix(x, y, z)
    }


def parents_input(spatial=None, placement=None, decomposition=None):
    return {
        "spatial": spatial,
        "placement": placement,
        "decomposition": decomposition
    }


def rectangular_profile(width, length):
    return [
        [0, 0],
        [width, 0],
        [width, length],
        [0, length]
    ]


def property_input(
    name,
    value,
    value_type="IfcLabel",
    property_set="Pset_DeletionTest",
    unit=None,
    source="occurrence"
):
    return {
        "propertySet": property_set,
        "name": name,
        "valueType": value_type,
        "value": value,
        "unit": unit,
        "source": source
    }


def mesh_points(mesh):
    vertices = mesh["vertices"]

    return [
        tuple(vertices[offset:offset + 3])
        for offset in range(0, len(vertices), 3)
    ]


def mesh_triangles(mesh):
    points = mesh_points(mesh)
    indices = mesh["indices"]

    for offset in range(0, len(indices), 3):
        yield tuple(
            points[index]
            for index in indices[offset:offset + 3]
        )


def mesh_signature(mesh, precision=7):
    return Counter(
        tuple(sorted(
            tuple(round(value, precision) for value in point)
            for point in triangle
        ))
        for triangle in mesh_triangles(mesh)
    )


def mesh_volume(mesh):
    points = mesh_points(mesh)

    if not points:
        return 0.0

    origin = points[0]
    terms = []

    for triangle in mesh_triangles(mesh):
        a, b, c = [
            tuple(
                point[axis] - origin[axis]
                for axis in range(3)
            )
            for point in triangle
        ]

        cross = (
            b[1] * c[2] - b[2] * c[1],
            b[2] * c[0] - b[0] * c[2],
            b[0] * c[1] - b[1] * c[0]
        )

        terms.append(
            math.fsum(
                a[axis] * cross[axis]
                for axis in range(3)
            )
        )

    return abs(math.fsum(terms)) / 6.0


def mesh_surface_area(mesh):
    areas = []

    for a, b, c in mesh_triangles(mesh):
        u = tuple(b[axis] - a[axis] for axis in range(3))
        v = tuple(c[axis] - a[axis] for axis in range(3))

        cross = (
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0]
        )

        areas.append(
            math.sqrt(
                math.fsum(value * value for value in cross)
            ) / 2.0
        )

    return math.fsum(areas)


def mesh_bounds(mesh):
    points = mesh_points(mesh)

    if not points:
        raise AssertionError("Mesh contains no vertices")

    return tuple(
        function(point[axis] for point in points)
        for function in (min, max)
        for axis in range(3)
    )


def parse_step_entities(content):
    # Targeted STEP parser for the controlled fixtures.
    # Not intended as a general-purpose IFC parser.

    pattern = re.compile(
        r"#(\d+)\s*=\s*(IFC[A-Z0-9_]+)\s*\((.*?)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL
    )

    entities = {}

    for match in pattern.finditer(content):
        identifier = int(match.group(1))

        if identifier in entities:
            raise AssertionError(
                f"Duplicate STEP identifier #{identifier}"
            )

        entities[identifier] = {
            "class": match.group(2).upper(),
            "arguments": match.group(3)
        }

    if not entities:
        raise AssertionError("No STEP entities found")

    return entities


def step_references(arguments):
    without_strings = re.sub(
        r"'(?:[^']|'')*'",
        "''",
        arguments
    )

    return [
        int(identifier)
        for identifier in re.findall(
            r"#(\d+)",
            without_strings
        )
    ]


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
                current.append(character)

            elif character == ")":
                depth -= 1
                current.append(character)

            elif character == "," and depth == 0:
                result.append("".join(current).strip())
                current = []

            else:
                current.append(character)

        else:
            current.append(character)

        index += 1

    result.append("".join(current).strip())

    return result


def entity_arguments(entities, identifier):
    return split_step_arguments(
        entities[identifier]["arguments"]
    )


def forward_graph(entities, roots, excluded_ids=None):
    excluded_ids = set(excluded_ids or [])
    pending = list(roots)
    visited = set()

    while pending:
        identifier = pending.pop()

        if identifier in visited or identifier in excluded_ids:
            continue

        if identifier not in entities:
            raise AssertionError(
                f"Missing STEP entity #{identifier}"
            )

        visited.add(identifier)

        pending.extend(
            step_references(
                entities[identifier]["arguments"]
            )
        )

    return visited


def inverse_references(entities, target_id):
    return {
        identifier
        for identifier, entity in entities.items()
        if target_id in step_references(entity["arguments"])
    }


def entities_by_class(entities, class_name):
    return {
        identifier: entity
        for identifier, entity in entities.items()
        if entity["class"] == class_name.upper()
    }


class Client:
    def __init__(self, executable, timeout=REQUEST_TIMEOUT):
        self.timeout = timeout
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
            json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False
            ) + "\n"
        )

        self.process.stdin.flush()

        return request_id

    def read(self, request_id):
        try:
            item = self.responses.get(timeout=self.timeout)
        except queue.Empty as error:
            raise TimeoutError(
                f"No CLI response for request {request_id}"
            ) from error

        if item is None:
            raise RuntimeError(
                "CLI terminated before returning a response"
            )

        if isinstance(item, Exception):
            raise RuntimeError(
                "Could not read CLI stdout"
            ) from item

        try:
            response = json.loads(item)
        except json.JSONDecodeError as error:
            raise AssertionError(
                f"Invalid JSONL response: {item!r}"
            ) from error

        if not isinstance(response, dict):
            raise AssertionError(response)

        if response.get("id") != request_id:
            raise AssertionError(
                f"Unexpected response identifier: {response}"
            )

        if type(response.get("ok")) is not bool:
            raise AssertionError(response)

        expected_fields = (
            {"id", "ok", "result"}
            if response["ok"]
            else {"id", "ok", "error"}
        )

        if set(response) != expected_fields:
            raise AssertionError(
                f"Invalid response structure: {response}"
            )

        if not response["ok"]:
            error = response["error"]

            if not isinstance(error, dict):
                raise AssertionError(response)

            if set(error) != {"code", "message", "details"}:
                raise AssertionError(response)

            if not isinstance(error["code"], str):
                raise AssertionError(response)

            if not isinstance(error["message"], str):
                raise AssertionError(response)

            if not isinstance(error["details"], dict):
                raise AssertionError(response)

        return response

    def request(self, command, params=None):
        request_id = self.send(command, params)
        response = self.read(request_id)

        if not response["ok"]:
            raise RuntimeError(
                f"{command}: {response['error']}"
            )

        return response["result"]

    def expect_error(
        self,
        command,
        params,
        expected_code="ENTITY_NOT_FOUND"
    ):
        request_id = self.send(command, params)
        response = self.read(request_id)

        if response["ok"]:
            raise AssertionError(
                f"{command} unexpectedly succeeded: {response}"
            )

        if response["error"]["code"] != expected_code:
            raise AssertionError(
                f"Expected {expected_code}, "
                f"received {response['error']}"
            )

        if not response["error"]["message"]:
            raise AssertionError(
                "Empty error message"
            )

        return response["error"]

    def tessellate(
        self,
        transform_id,
        kind="SOLID",
        space="local",
        geometry_id=None,
        include_children=False,
        include_materials=True
    ):
        options = {
            "space": space,
            "kind": kind,
            "includeChildren": include_children,
            "includeNormals": False,
            "includeMaterials": include_materials
        }

        if geometry_id is not None:
            options["geometryId"] = geometry_id

        request_id = self.send(
            "transformTessellate",
            {
                "id": transform_id,
                "options": options
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
        last_event = -1
        materials_received = False

        while True:
            response = self.read(request_id)

            if not response["ok"]:
                raise RuntimeError(
                    f"Tessellation failed: {response['error']}"
                )

            event = response["result"]
            event_name = event.get("event")

            if event_name not in event_order:
                raise AssertionError(
                    f"Unexpected mesh event: {event}"
                )

            current_order = event_order[event_name]

            if current_order < last_event:
                raise AssertionError(
                    f"Invalid mesh event order: {event}"
                )

            last_event = current_order

            if event_name == "mesh.begin":
                if begin is not None:
                    raise AssertionError(
                        "Duplicate mesh.begin event"
                    )

                begin = event
                continue

            if begin is None:
                raise AssertionError(
                    "Mesh data received before mesh.begin"
                )

            if event_name == "mesh.end":
                break

            if event_name == "mesh.materials":
                if not include_materials or materials_received:
                    raise AssertionError(
                        "Unexpected mesh.materials event"
                    )

                if not isinstance(event["groups"], list):
                    raise AssertionError(event)

                mesh["groups"] = event["groups"]
                materials_received = True
                continue

            key = event_name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(
                    f"Invalid mesh chunk offset: {event}"
                )

            if not isinstance(event["values"], list):
                raise AssertionError(event)

            mesh[key].extend(event["values"])

        if materials_received != include_materials:
            raise AssertionError(
                "Incorrect presence of mesh.materials event"
            )

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        for field in ("vertexCount", "indexCount"):
            if type(mesh[field]) is not int or mesh[field] < 0:
                raise AssertionError(begin)

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError("Invalid triangle indices")

        for index in mesh["indices"]:
            if (
                type(index) is not int
                or not 0 <= index < mesh["vertexCount"]
            ):
                raise AssertionError(
                    f"Invalid vertex index: {index}"
                )

        for value in mesh["vertices"]:
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
            ):
                raise AssertionError(
                    f"Invalid mesh coordinate: {value}"
                )

        return mesh

    def close(self):
        try:
            if self.process.poll() is None:
                result = self.request("exit")

                if result != {}:
                    raise AssertionError(
                        f"Unexpected exit result: {result}"
                    )

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


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-deletion-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)
        self.input_path = self.root_path / "input.ifc"
        self.output_path = self.root_path / "output.ifc"

        self.input_path.write_text(
            IFC_FIXTURE,
            encoding="utf-8"
        )

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.load_model(self.input_path)

    def tearDown(self):
        self.client.close()

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
        result = self.client.request(
            "save",
            {"path": str(self.output_path)}
        )

        self.assertEqual(
            result,
            {"path": str(self.output_path)}
        )

        self.assertTrue(self.output_path.is_file())
        self.assertGreater(self.output_path.stat().st_size, 0)

        return self.output_path.read_text(encoding="utf-8")

    def save_entities(self):
        return parse_step_entities(
            self.save_model()
        )

    def save_and_reload(self):
        self.save_model()

        return self.load_model(self.output_path)

    @property
    def root_id(self):
        return self.find_transform(ROOT_GLOBAL_ID)["id"]

    @property
    def survivor_id(self):
        return self.find_transform(SURVIVOR_GLOBAL_ID)["id"]

    @property
    def storey_id(self):
        return self.find_transform(STOREY_GLOBAL_ID)["id"]

    @property
    def project_id(self):
        return self.find_transform(PROJECT_GLOBAL_ID)["id"]

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

    def find_transform(self, global_id):
        matches = [
            item
            for item in self.list_transforms()
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
            item
            for item in self.list_geometries()
            if item["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one geometry with GlobalId {global_id}"
        )

        return self.get_geometry(matches[0]["id"])

    def find_material(self, name):
        matches = [
            material
            for material in self.snapshot_materials().values()
            if material["name"] == name
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one material named {name}"
        )

        return matches[0]

    def native_geometries(self, parent_id):
        return [
            self.get_geometry(item["id"])
            for item in self.list_geometries(parent_id)
            if item["source"] == "ifc"
        ]

    def snapshot_transforms(self):
        return {
            item["id"]: self.get_transform(item["id"])
            for item in self.list_transforms()
        }

    def snapshot_geometries(self):
        return {
            item["id"]: self.get_geometry(item["id"])
            for item in self.list_geometries()
        }

    def snapshot_materials(self):
        return {
            item["id"]: self.get_material(item["id"])
            for item in self.list_materials()
        }

    def snapshot_model(self):
        return {
            "transforms": self.snapshot_transforms(),
            "geometries": self.snapshot_geometries(),
            "materials": self.snapshot_materials()
        }

    def create_transform(
        self,
        name="Created transform",
        parents=None,
        properties=None,
        x=0.0,
        y=0.0,
        z=0.0
    ):
        return self.client.request(
            "transformCreate",
            {
                "name": name,
                "parents": (
                    parents_input()
                    if parents is None
                    else parents
                ),
                "transform": transform_input(x, y, z),
                "properties": (
                    []
                    if properties is None
                    else properties
                )
            }
        )

    def create_geometry(
        self,
        parent_id=None,
        name="Created geometry",
        kind="SOLID",
        operation="ADD",
        profile=None,
        depth=2.0,
        x=0.0,
        y=0.0,
        z=0.0
    ):
        return self.client.request(
            "geometryCreate",
            {
                "parentId": (
                    self.root_id
                    if parent_id is None
                    else parent_id
                ),
                "name": name,
                "kind": kind,
                "operation": operation,
                "profile": (
                    DEFAULT_PROFILE
                    if profile is None
                    else profile
                ),
                "depth": depth,
                "transform": transform_input(x, y, z)
            }
        )

    def create_domain(
        self,
        parent_id=None,
        name="Created domain",
        profile=None,
        depth=2.0,
        x=0.0,
        y=0.0,
        z=0.0
    ):
        return self.create_geometry(
            parent_id=parent_id,
            name=name,
            kind="DOMAIN",
            operation="ADD",
            profile=profile,
            depth=depth,
            x=x,
            y=y,
            z=z
        )

    def create_material(
        self,
        name="Created material",
        category="concrete",
        properties=None
    ):
        return self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": category,
                "visual": DEFAULT_VISUAL,
                "properties": (
                    []
                    if properties is None
                    else properties
                )
            }
        )

    def assign_material(self, material_id, target_id):
        params = {
            "materialId": material_id,
            "targetId": target_id
        }

        self.assertEqual(
            self.client.request("materialAssign", params),
            params
        )

    def delete_transform(self, identifier):
        result = self.client.request(
            "transformDelete",
            {"id": identifier}
        )

        self.assertEqual(result, {"id": identifier})
        self.assert_transform_deleted(identifier)

    def delete_geometry(self, identifier):
        result = self.client.request(
            "geometryDelete",
            {"id": identifier}
        )

        self.assertEqual(result, {"id": identifier})
        self.assert_geometry_deleted(identifier)

    def delete_material(self, identifier):
        result = self.client.request(
            "materialDelete",
            {"id": identifier}
        )

        self.assertEqual(result, {"id": identifier})
        self.assert_material_deleted(identifier)

    def tessellate(self, identifier, **options):
        return self.client.tessellate(
            identifier,
            **options
        )

    def tessellate_domain(self, geometry, **options):
        return self.tessellate(
            geometry["parentId"],
            kind="DOMAIN",
            geometry_id=geometry["id"],
            **options
        )

    def assert_transform_deleted(self, identifier):
        self.assertNotIn(
            identifier,
            {
                item["id"]
                for item in self.list_transforms()
            }
        )

        self.client.expect_error(
            "transformGet",
            {"id": identifier}
        )

    def assert_geometry_deleted(self, identifier):
        self.assertNotIn(
            identifier,
            {
                item["id"]
                for item in self.list_geometries()
            }
        )

        self.client.expect_error(
            "geometryGet",
            {"id": identifier}
        )

    def assert_material_deleted(self, identifier):
        self.assertNotIn(
            identifier,
            {
                item["id"]
                for item in self.list_materials()
            }
        )

        self.client.expect_error(
            "materialGet",
            {"id": identifier}
        )

    def assert_mesh_equivalent(self, before, after):
        self.assertEqual(
            before["indexCount"],
            after["indexCount"]
        )

        self.assertEqual(
            mesh_signature(before),
            mesh_signature(after)
        )

        self.assertAlmostEqual(
            mesh_volume(before),
            mesh_volume(after),
            delta=MESH_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_surface_area(before),
            mesh_surface_area(after),
            delta=MESH_TOLERANCE
        )

    def assert_empty_mesh(self, mesh):
        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)
        self.assertEqual(mesh["vertices"], [])
        self.assertEqual(mesh["indices"], [])
        self.assertEqual(mesh["groups"], [])

    def assert_volume(self, mesh, expected):
        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected,
            delta=MESH_TOLERANCE
        )

    def assert_bounds(self, mesh, expected):
        actual = mesh_bounds(mesh)

        self.assertEqual(len(actual), len(expected))

        for value, reference in zip(actual, expected):
            self.assertAlmostEqual(
                value,
                reference,
                delta=MESH_TOLERANCE
            )

    def assert_index_consistency(self):
        transforms = self.snapshot_transforms()
        geometries = self.snapshot_geometries()
        materials = self.snapshot_materials()

        expected_geometry_links = {
            identifier: set()
            for identifier in transforms
        }

        for identifier, geometry in geometries.items():
            parent_id = geometry["parentId"]

            self.assertIn(parent_id, transforms)

            expected_geometry_links[parent_id].add(identifier)

            if "materials" in geometry:
                material_ids = geometry["materials"]

                self.assertEqual(
                    len(material_ids),
                    len(set(material_ids))
                )

                self.assertTrue(
                    set(material_ids).issubset(materials)
                )

                if geometry["kind"] == "DOMAIN":
                    self.assertEqual(material_ids, [])

        for identifier, transform in transforms.items():
            geometry_ids = transform["geometries"]
            material_ids = transform["materials"]

            self.assertEqual(
                len(geometry_ids),
                len(set(geometry_ids))
            )

            self.assertEqual(
                set(geometry_ids),
                expected_geometry_links[identifier]
            )

            self.assertEqual(
                len(material_ids),
                len(set(material_ids))
            )

            self.assertTrue(
                set(material_ids).issubset(materials)
            )

            for parent_id in transform["parents"].values():
                if parent_id is not None:
                    self.assertIn(parent_id, transforms)
                    self.assertNotEqual(parent_id, identifier)

        entity_ids = [
            entity["ifcEntityId"]
            for entity in [
                *transforms.values(),
                *geometries.values(),
                *materials.values()
            ]
        ]

        self.assertEqual(
            len(entity_ids),
            len(set(entity_ids))
        )

    def assert_step_references_valid(self, entities):
        dangling = []

        for identifier, entity in entities.items():
            for reference in step_references(entity["arguments"]):
                if reference not in entities:
                    dangling.append({
                        "entity": identifier,
                        "class": entity["class"],
                        "reference": reference
                    })

        self.assertFalse(
            dangling,
            f"Dangling STEP references: {dangling[:20]}"
        )

    def assert_step_entities_removed(self, entities, deleted_ids):
        remaining = set(deleted_ids) & set(entities)

        self.assertFalse(
            remaining,
            f"Deleted IFC entities remain: {sorted(remaining)}"
        )

    def assert_no_references_to(self, entities, deleted_ids):
        deleted_ids = set(deleted_ids)
        dangling = []

        for identifier, entity in entities.items():
            references = set(
                step_references(entity["arguments"])
            )

            invalid = references & deleted_ids

            if invalid:
                dangling.append({
                    "entity": identifier,
                    "class": entity["class"],
                    "references": sorted(invalid)
                })

        self.assertFalse(
            dangling,
            f"References to deleted entities: {dangling[:20]}"
        )

    def assert_preserved_step_entities(
        self,
        before,
        after,
        preserved_ids
    ):
        for identifier in preserved_ids:
            self.assertIn(
                identifier,
                after,
                f"Shared IFC entity #{identifier} was deleted"
            )

            self.assertEqual(
                after[identifier],
                before[identifier],
                f"Shared IFC entity #{identifier} was modified"
            )

    def assert_global_ids_absent(self, deleted_global_ids):
        deleted_global_ids = {
            global_id
            for global_id in deleted_global_ids
            if global_id is not None
        }

        current_global_ids = {
            entity["globalId"]
            for entity in [
                *self.list_transforms(),
                *self.list_geometries(),
                *self.list_materials()
            ]
            if entity["globalId"] is not None
        }

        self.assertFalse(
            deleted_global_ids & current_global_ids
        )

        content = self.save_model()

        for global_id in deleted_global_ids:
            self.assertNotIn(
                f"'{global_id}'",
                content
            )

    def property_relations(self, entities, target_step_id):
        result = set()

        for identifier, entity in entities.items():
            if entity["class"] != "IFCRELDEFINESBYPROPERTIES":
                continue

            arguments = entity_arguments(
                entities,
                identifier
            )

            related_objects = step_references(
                arguments[-2]
            )

            if target_step_id in related_objects:
                result.add(identifier)

        return result

    def property_definition_ids(self, entities, target_step_id):
        result = set()

        for relation_id in self.property_relations(
            entities,
            target_step_id
        ):
            arguments = entity_arguments(
                entities,
                relation_id
            )

            result.update(
                step_references(arguments[-1])
            )

        return result

    def material_relation_ids(self, entities, material_step_id):
        return {
            identifier
            for identifier, entity in entities.items()
            if (
                entity["class"] == "IFCRELASSOCIATESMATERIAL"
                and entity_arguments(
                    entities,
                    identifier
                )[-1] == f"#{material_step_id}"
            )
        }

    def material_targets(self, entities, material_step_id):
        result = []

        for relation_id in self.material_relation_ids(
            entities,
            material_step_id
        ):
            arguments = entity_arguments(
                entities,
                relation_id
            )

            result.extend(
                step_references(arguments[-2])
            )

        self.assertEqual(
            len(result),
            len(set(result))
        )

        return set(result)

    def feature_relation_ids(self, entities, feature_step_id):
        relation_classes = {
            "IFCRELPROJECTSELEMENT",
            "IFCRELVOIDSELEMENT",
            "IFCRELASSIGNSTOPRODUCT"
        }

        return {
            identifier
            for identifier, entity in entities.items()
            if (
                entity["class"] in relation_classes
                and feature_step_id in step_references(
                    entity["arguments"]
                )
            )
        }

    def representation_ids(self, entities, transform_step_id):
        # IfcProduct.Representation is the seventh STEP argument.

        arguments = entity_arguments(
            entities,
            transform_step_id
        )

        return set(
            step_references(arguments[6])
        )

    def shape_representation_ids(self, entities, transform_step_id):
        result = set()

        for representation_id in self.representation_ids(
            entities,
            transform_step_id
        ):
            arguments = entity_arguments(
                entities,
                representation_id
            )

            result.update(
                step_references(arguments[-1])
            )

        return result

    def representation_item_ids(self, entities, transform_step_id):
        result = set()

        for representation_id in self.shape_representation_ids(
            entities,
            transform_step_id
        ):
            arguments = entity_arguments(
                entities,
                representation_id
            )

            result.update(
                step_references(arguments[-1])
            )

        return result

    def assert_no_orphans(self, entities, candidate_ids):
        remaining = set(candidate_ids) & set(entities)

        details = [
            {
                "id": identifier,
                "class": entities[identifier]["class"],
                "referencesFrom": sorted(
                    inverse_references(entities, identifier)
                )
            }
            for identifier in sorted(remaining)
        ]

        self.assertFalse(
            remaining,
            f"Orphan IFC entities remain: {details[:30]}"
        )

    def test_delete_transform(self):
        """Verify deletion of a simple transform without geometry."""

        initial = self.snapshot_model()

        target = self.create_transform(
            name="Transform to delete",
            parents=parents_input(
                spatial=self.storey_id
            ),
            x=10.0,
            y=20.0,
            z=30.0
        )

        self.assertEqual(target["geometries"], [])
        self.assertEqual(target["materials"], [])

        self.assertEqual(
            len(self.list_transforms()),
            len(initial["transforms"]) + 1
        )

        self.assert_empty_mesh(
            self.tessellate(target["id"])
        )

        self.delete_transform(target["id"])

        self.assertEqual(
            self.snapshot_model(),
            initial
        )

        entities = self.save_entities()

        self.assert_step_entities_removed(
            entities,
            [target["ifcEntityId"]]
        )

        self.assert_no_references_to(
            entities,
            [target["ifcEntityId"]]
        )

        self.assert_global_ids_absent(
            [target["globalId"]]
        )

        self.assert_step_references_valid(entities)

        replacement = self.create_transform(
            name="Replacement transform"
        )

        self.assertNotEqual(
            replacement["id"],
            target["id"]
        )

        self.assertNotEqual(
            replacement["globalId"],
            target["globalId"]
        )

        self.assert_transform_deleted(target["id"])
        self.assert_index_consistency()

    def test_delete_hierarchy(self):
        """Verify recursive deletion of descendants."""

        initial = self.snapshot_model()

        root = self.create_transform(
            name="Hierarchy root",
            parents=parents_input(
                spatial=self.storey_id
            )
        )

        first_child = self.create_transform(
            name="First hierarchy child",
            parents=parents_input(
                placement=root["id"],
                decomposition=root["id"]
            ),
            x=10.0
        )

        second_child = self.create_transform(
            name="Second hierarchy child",
            parents=parents_input(
                placement=root["id"]
            ),
            x=20.0
        )

        grandchild = self.create_transform(
            name="Hierarchy grandchild",
            parents=parents_input(
                placement=first_child["id"],
                decomposition=first_child["id"]
            ),
            y=10.0
        )

        hierarchy = [
            root,
            first_child,
            second_child,
            grandchild
        ]

        geometries = [
            self.create_domain(
                parent_id=transform["id"],
                name=f"Hierarchy domain {index}",
                profile=rectangular_profile(2.0, 2.0),
                depth=2.0
            )
            for index, transform in enumerate(hierarchy)
        ]

        self.assert_volume(
            self.tessellate(
                root["id"],
                kind="DOMAIN",
                include_children=True
            ),
            32.0
        )

        self.delete_transform(root["id"])

        for transform in hierarchy:
            self.assert_transform_deleted(
                transform["id"]
            )

        for geometry in geometries:
            self.assert_geometry_deleted(
                geometry["id"]
            )

        self.assertEqual(
            self.snapshot_model(),
            initial
        )

        deleted_step_ids = {
            entity["ifcEntityId"]
            for entity in hierarchy + geometries
        }

        entities = self.save_entities()

        self.assert_step_entities_removed(
            entities,
            deleted_step_ids
        )

        self.assert_no_references_to(
            entities,
            deleted_step_ids
        )

        self.assert_global_ids_absent([
            entity["globalId"]
            for entity in hierarchy + geometries
        ])

        self.assert_step_references_valid(entities)
        self.assert_index_consistency()

    def test_delete_transform_geometries(self):
        """Verify deletion of physical geometries owned by a deleted transform."""

        root = self.get_transform(self.root_id)
        survivor = self.get_transform(self.survivor_id)

        survivor_mesh = self.tessellate(
            survivor["id"]
        )

        native_geometries = self.native_geometries(
            root["id"]
        )

        self.assertEqual(
            len(native_geometries),
            2
        )

        addition = self.create_geometry(
            parent_id=root["id"],
            name="Deleted additive geometry",
            operation="ADD",
            depth=3.0,
            z=3.0
        )

        subtraction = self.create_geometry(
            parent_id=root["id"],
            name="Deleted subtractive geometry",
            operation="SUBTRACT",
            profile=rectangular_profile(0.5, 0.5),
            depth=6.0,
            x=-0.25,
            y=-0.25,
            z=-1.0
        )

        geometries = [
            *native_geometries,
            addition,
            subtraction
        ]

        self.assertEqual(
            set(self.get_transform(root["id"])["geometries"]),
            {
                geometry["id"]
                for geometry in geometries
            }
        )

        before = self.save_entities()

        deleted_relation_ids = (
            self.feature_relation_ids(
                before,
                addition["ifcEntityId"]
            )
            | self.feature_relation_ids(
                before,
                subtraction["ifcEntityId"]
            )
        )

        deleted_representation_ids = (
            self.representation_ids(
                before,
                root["ifcEntityId"]
            )
            | self.shape_representation_ids(
                before,
                root["ifcEntityId"]
            )
        )

        self.delete_transform(root["id"])

        for geometry in geometries:
            self.assert_geometry_deleted(
                geometry["id"]
            )

        self.assertEqual(
            self.get_transform(survivor["id"]),
            survivor
        )

        self.assert_mesh_equivalent(
            survivor_mesh,
            self.tessellate(survivor["id"])
        )

        deleted_step_ids = {
            root["ifcEntityId"],
            *(geometry["ifcEntityId"] for geometry in geometries),
            *deleted_relation_ids,
            *deleted_representation_ids
        }

        after = self.save_entities()

        self.assert_step_entities_removed(
            after,
            deleted_step_ids
        )

        self.assert_no_references_to(
            after,
            deleted_step_ids
        )

        self.assert_global_ids_absent([
            root["globalId"],
            addition["globalId"],
            subtraction["globalId"]
        ])

        self.assert_step_references_valid(after)
        self.assert_index_consistency()

    def test_delete_transform_domains(self):
        """Verify deletion of managed domains owned by deleted transforms."""

        parent = self.create_transform(
            name="Domain owner"
        )

        child = self.create_transform(
            name="Domain owner child",
            parents=parents_input(
                placement=parent["id"],
                decomposition=parent["id"]
            ),
            x=20.0
        )

        domains = [
            self.create_domain(
                parent_id=parent["id"],
                name="First deleted domain",
                profile=rectangular_profile(5.0, 3.0),
                depth=2.0
            ),
            self.create_domain(
                parent_id=parent["id"],
                name="Second deleted domain",
                x=10.0
            ),
            self.create_domain(
                parent_id=child["id"],
                name="Deleted descendant domain",
                profile=rectangular_profile(2.0, 2.0),
                depth=4.0
            )
        ]

        preserved = self.create_domain(
            parent_id=self.survivor_id,
            name="Preserved domain",
            profile=rectangular_profile(4.0, 5.0),
            depth=3.0
        )

        preserved_mesh = self.tessellate_domain(
            preserved
        )

        self.assert_volume(preserved_mesh, 60.0)

        self.assert_volume(
            self.tessellate(
                parent["id"],
                kind="DOMAIN",
                include_children=True
            ),
            58.0
        )

        before = self.save_entities()

        relation_ids = set()

        for domain in domains:
            domain_relations = self.feature_relation_ids(
                before,
                domain["ifcEntityId"]
            )

            self.assertEqual(
                len(domain_relations),
                1
            )

            relation_ids.update(domain_relations)

        self.delete_transform(parent["id"])

        self.assert_transform_deleted(child["id"])

        for domain in domains:
            self.assert_geometry_deleted(domain["id"])

        self.assertEqual(
            self.get_geometry(preserved["id"]),
            preserved
        )

        self.assert_mesh_equivalent(
            preserved_mesh,
            self.tessellate_domain(preserved)
        )

        remaining_domains = self.list_geometries(
            kind="DOMAIN"
        )

        self.assertEqual(
            {
                geometry["id"]
                for geometry in remaining_domains
            },
            {preserved["id"]}
        )

        deleted_step_ids = {
            parent["ifcEntityId"],
            child["ifcEntityId"],
            *(domain["ifcEntityId"] for domain in domains),
            *relation_ids
        }

        after = self.save_entities()

        self.assert_step_entities_removed(
            after,
            deleted_step_ids
        )

        self.assert_no_references_to(
            after,
            deleted_step_ids
        )

        self.assert_step_references_valid(after)

        self.save_and_reload()

        reloaded = self.find_geometry(
            preserved["globalId"]
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            1
        )

        self.assert_mesh_equivalent(
            preserved_mesh,
            self.tessellate_domain(reloaded)
        )

        self.assert_global_ids_absent([
            parent["globalId"],
            child["globalId"],
            *(domain["globalId"] for domain in domains)
        ])

        self.assert_index_consistency()

    def test_delete_transform_properties(self):
        """Verify cleanup of exclusively owned IFC properties."""

        preserved = self.create_transform(
            name="Preserved property owner",
            properties=[
                property_input(
                    "PreservedReference",
                    "PRESERVED-PROPERTY"
                ),
                property_input(
                    "PreservedCount",
                    10,
                    value_type="IfcInteger"
                )
            ]
        )

        target = self.create_transform(
            name="Deleted property owner",
            properties=[
                property_input(
                    "DeletedReference",
                    "DELETED-PROPERTY",
                    property_set="Pset_DeletedIdentity"
                ),
                property_input(
                    "DeletedDescription",
                    "Deleted property description",
                    value_type="IfcText",
                    property_set="Pset_DeletedIdentity"
                ),
                property_input(
                    "DeletedCount",
                    42,
                    value_type="IfcInteger",
                    property_set="Pset_DeletedValues"
                ),
                property_input(
                    "DeletedDensity",
                    7850.5,
                    value_type="IfcReal",
                    property_set="Pset_DeletedValues"
                ),
                property_input(
                    "DeletedEnabled",
                    True,
                    value_type="IfcBoolean",
                    property_set="Pset_DeletedValues"
                )
            ]
        )

        self.assertEqual(
            len(target["properties"]),
            5
        )

        before = self.save_entities()

        property_relations = self.property_relations(
            before,
            target["ifcEntityId"]
        )

        property_definitions = self.property_definition_ids(
            before,
            target["ifcEntityId"]
        )

        self.assertEqual(
            len(property_relations),
            2
        )

        self.assertEqual(
            len(property_definitions),
            2
        )

        property_graph = forward_graph(
            before,
            property_definitions
        )

        property_value_ids = {
            identifier
            for identifier in property_graph
            if before[identifier]["class"] == "IFCPROPERTYSINGLEVALUE"
        }

        self.assertEqual(
            len(property_value_ids),
            5
        )

        preserved_property_graph = forward_graph(
            before,
            self.property_definition_ids(
                before,
                preserved["ifcEntityId"]
            )
        )

        self.assertFalse(
            property_graph & preserved_property_graph
        )

        self.delete_transform(target["id"])

        self.assertEqual(
            self.get_transform(preserved["id"]),
            preserved
        )

        after = self.save_entities()

        self.assert_step_entities_removed(
            after,
            {
                target["ifcEntityId"],
                *property_relations,
                *property_definitions
            }
        )

        self.assert_preserved_step_entities(
            before,
            after,
            preserved_property_graph
        )

        self.assert_step_references_valid(after)

        # Removing a property set must also remove its
        # exclusively owned property values.

        self.assert_no_orphans(
            after,
            property_value_ids
        )

        self.save_and_reload()

        reloaded = self.find_transform(
            preserved["globalId"]
        )

        self.assertEqual(
            reloaded["properties"],
            preserved["properties"]
        )

        self.assert_global_ids_absent(
            [target["globalId"]]
        )

        self.assert_index_consistency()

    def test_delete_geometry(self):
        """Verify geometry deletion without deleting its parent."""

        root = self.get_transform(self.root_id)

        baseline = self.tessellate(root["id"])

        material = self.create_material(
            name="Preserved geometry material"
        )

        subtraction = self.create_geometry(
            parent_id=root["id"],
            name="Deleted through opening",
            operation="SUBTRACT",
            profile=rectangular_profile(1.0, 1.0),
            depth=6.0,
            x=-0.5,
            y=-0.5,
            z=-1.0
        )

        self.assign_material(
            material["id"],
            subtraction["id"]
        )

        preserved_domain = self.create_domain(
            parent_id=root["id"],
            name="Preserved domain"
        )

        domain_mesh = self.tessellate_domain(
            preserved_domain
        )

        self.assert_volume(baseline, 48.0)

        self.assert_volume(
            self.tessellate(root["id"]),
            44.0
        )

        before = self.save_entities()

        deleted_relations = self.feature_relation_ids(
            before,
            subtraction["ifcEntityId"]
        )

        self.assertEqual(
            len(deleted_relations),
            1
        )

        self.delete_geometry(subtraction["id"])

        current_root = self.get_transform(root["id"])

        self.assertEqual(
            current_root["globalId"],
            root["globalId"]
        )

        self.assertEqual(
            set(current_root["geometries"]),
            set(root["geometries"]) | {
                preserved_domain["id"]
            }
        )

        self.assertEqual(
            self.get_material(material["id"])["name"],
            material["name"]
        )

        self.assertEqual(
            self.get_geometry(preserved_domain["id"]),
            preserved_domain
        )

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate(root["id"])
        )

        self.assert_mesh_equivalent(
            domain_mesh,
            self.tessellate_domain(preserved_domain)
        )

        after = self.save_entities()

        self.assert_step_entities_removed(
            after,
            {
                subtraction["ifcEntityId"],
                *deleted_relations
            }
        )

        self.assertEqual(
            self.material_targets(
                after,
                material["ifcEntityId"]
            ),
            set()
        )

        self.assert_no_references_to(
            after,
            [subtraction["ifcEntityId"]]
        )

        self.assert_step_references_valid(after)

        self.delete_geometry(preserved_domain["id"])

        self.assert_empty_mesh(
            self.tessellate(
                root["id"],
                kind="DOMAIN"
            )
        )

        self.assertEqual(
            self.get_transform(root["id"]),
            root
        )

        self.assert_index_consistency()

    def test_delete_native_geometry(self):
        """Verify deletion of a native IFC representation item."""

        root = self.get_transform(self.root_id)

        native_geometries = sorted(
            self.native_geometries(root["id"]),
            key=lambda item: item["ifcEntityId"]
        )

        self.assertEqual(
            len(native_geometries),
            2
        )

        deleted, preserved = native_geometries

        self.assertEqual(
            deleted["source"],
            "ifc"
        )

        self.assertIs(
            deleted["editable"],
            False
        )

        self.assert_volume(
            self.tessellate(root["id"]),
            48.0
        )

        material = self.create_material(
            name="Native representation material"
        )

        self.assign_material(
            material["id"],
            deleted["id"]
        )

        before = self.save_entities()

        representation_ids = self.representation_ids(
            before,
            root["ifcEntityId"]
        )

        shape_ids = self.shape_representation_ids(
            before,
            root["ifcEntityId"]
        )

        styled_items = {
            identifier
            for identifier, entity in before.items()
            if (
                entity["class"] == "IFCSTYLEDITEM"
                and entity_arguments(
                    before,
                    identifier
                )[0] == f"#{deleted['ifcEntityId']}"
            )
        }

        self.assertTrue(styled_items)

        self.delete_geometry(deleted["id"])

        self.assertEqual(
            self.get_transform(root["id"])["geometries"],
            [preserved["id"]]
        )

        self.assertEqual(
            self.get_geometry(preserved["id"]),
            preserved
        )

        remaining_mesh = self.tessellate(
            root["id"]
        )

        self.assert_volume(
            remaining_mesh,
            24.0
        )

        self.assert_bounds(
            remaining_mesh,
            (
                4.0, -1.5, 0.0,
                6.0, 1.5, 4.0
            )
        )

        after = self.save_entities()

        self.assert_step_entities_removed(
            after,
            {
                deleted["ifcEntityId"],
                *styled_items
            }
        )

        self.assertTrue(
            representation_ids.issubset(after)
        )

        self.assertTrue(
            shape_ids.issubset(after)
        )

        self.assertEqual(
            self.representation_item_ids(
                after,
                root["ifcEntityId"]
            ),
            {preserved["ifcEntityId"]}
        )

        self.assert_no_references_to(
            after,
            [deleted["ifcEntityId"]]
        )

        self.assert_step_references_valid(after)

        self.assertEqual(
            self.get_material(material["id"])["name"],
            material["name"]
        )

        self.save_and_reload()

        self.assertEqual(
            len(self.native_geometries(self.root_id)),
            1
        )

        self.assert_mesh_equivalent(
            remaining_mesh,
            self.tessellate(self.root_id)
        )

        self.assert_index_consistency()

    def test_delete_last_representation(self):
        """Verify cleanup of an empty native product representation."""

        root = self.get_transform(self.root_id)

        survivor_mesh = self.tessellate(
            self.survivor_id
        )

        geometries = self.native_geometries(
            root["id"]
        )

        self.assertEqual(len(geometries), 2)

        before = self.save_entities()

        representation_ids = self.representation_ids(
            before,
            root["ifcEntityId"]
        )

        shape_ids = self.shape_representation_ids(
            before,
            root["ifcEntityId"]
        )

        self.assertEqual(
            len(representation_ids),
            1
        )

        self.assertEqual(
            len(shape_ids),
            1
        )

        self.delete_geometry(
            geometries[0]["id"]
        )

        intermediate = self.save_entities()

        self.assertTrue(
            representation_ids.issubset(intermediate)
        )

        self.assertTrue(
            shape_ids.issubset(intermediate)
        )

        self.assert_volume(
            self.tessellate(root["id"]),
            24.0
        )

        self.delete_geometry(
            geometries[1]["id"]
        )

        self.assertEqual(
            self.get_transform(root["id"])["geometries"],
            []
        )

        self.assert_empty_mesh(
            self.tessellate(root["id"])
        )

        after = self.save_entities()

        deleted_ids = {
            *representation_ids,
            *shape_ids,
            *(geometry["ifcEntityId"] for geometry in geometries)
        }

        self.assert_step_entities_removed(
            after,
            deleted_ids
        )

        product_arguments = entity_arguments(
            after,
            root["ifcEntityId"]
        )

        self.assertEqual(
            product_arguments[6],
            "$",
            "Product still references an empty representation"
        )

        self.assert_no_references_to(
            after,
            deleted_ids
        )

        self.assert_step_references_valid(after)

        self.assert_mesh_equivalent(
            survivor_mesh,
            self.tessellate(self.survivor_id)
        )

        self.save_and_reload()

        reloaded = self.find_transform(
            root["globalId"]
        )

        self.assertEqual(
            reloaded["geometries"],
            []
        )

        self.assert_empty_mesh(
            self.tessellate(reloaded["id"])
        )

        # A product without a representation must remain
        # usable as the owner of newly created domains.

        domain = self.create_domain(
            parent_id=reloaded["id"]
        )

        self.assert_volume(
            self.tessellate_domain(domain),
            12.0
        )

        self.assert_empty_mesh(
            self.tessellate(reloaded["id"])
        )

        self.assert_index_consistency()

    def test_delete_material(self):
        """Verify material deletion without deleting assigned entities."""

        material = self.create_material(
            name="Material to delete",
            properties=[
                property_input(
                    "DeletedReference",
                    "DELETED-MATERIAL-VALUE",
                    property_set="Pset_DeletedMaterial",
                    source="material"
                )
            ]
        )

        preserved_material = self.create_material(
            name="Preserved material"
        )

        geometry = self.create_geometry(
            name="Preserved assigned feature",
            operation="SUBTRACT",
            profile=rectangular_profile(0.5, 0.5),
            depth=6.0,
            x=-0.25,
            y=-0.25,
            z=-1.0
        )

        native = self.native_geometries(
            self.root_id
        )[0]

        targets = [
            self.root_id,
            self.survivor_id,
            geometry["id"]
        ]

        for target_id in targets:
            self.assign_material(
                material["id"],
                target_id
            )

        self.assign_material(
            material["id"],
            native["id"]
        )

        self.assign_material(
            preserved_material["id"],
            self.root_id
        )

        self.assign_material(
            preserved_material["id"],
            geometry["id"]
        )

        root_mesh = self.tessellate(
            self.root_id
        )

        survivor_mesh = self.tessellate(
            self.survivor_id
        )

        transforms_before = self.snapshot_transforms()
        geometries_before = self.snapshot_geometries()

        before = self.save_entities()

        material_relations = self.material_relation_ids(
            before,
            material["ifcEntityId"]
        )

        styled_items = {
            identifier
            for identifier, entity in before.items()
            if (
                entity["class"] == "IFCSTYLEDITEM"
                and entity_arguments(
                    before,
                    identifier
                )[0] == f"#{native['ifcEntityId']}"
            )
        }

        self.assertTrue(material_relations)
        self.assertTrue(styled_items)

        self.delete_material(material["id"])

        self.assertEqual(
            set(self.snapshot_transforms()),
            set(transforms_before)
        )

        self.assertEqual(
            set(self.snapshot_geometries()),
            set(geometries_before)
        )

        for identifier, reference in transforms_before.items():
            current = self.get_transform(identifier)

            expected = {
                **reference,
                "materials": [
                    material_id
                    for material_id in reference["materials"]
                    if material_id != material["id"]
                ]
            }

            self.assertEqual(current, expected)

        for identifier, reference in geometries_before.items():
            current = self.get_geometry(identifier)
            expected = dict(reference)

            if "materials" in expected:
                expected["materials"] = [
                    material_id
                    for material_id in reference["materials"]
                    if material_id != material["id"]
                ]

            self.assertEqual(current, expected)

        self.assertEqual(
            self.get_transform(self.root_id)["materials"],
            [preserved_material["id"]]
        )

        self.assertEqual(
            self.get_transform(self.survivor_id)["materials"],
            []
        )

        self.assertEqual(
            self.get_geometry(geometry["id"])["materials"],
            [preserved_material["id"]]
        )

        self.assert_mesh_equivalent(
            root_mesh,
            self.tessellate(self.root_id)
        )

        self.assert_mesh_equivalent(
            survivor_mesh,
            self.tessellate(self.survivor_id)
        )

        after = self.save_entities()

        self.assert_step_entities_removed(
            after,
            {
                material["ifcEntityId"],
                *material_relations,
                *styled_items
            }
        )

        self.assert_no_references_to(
            after,
            [material["ifcEntityId"]]
        )

        self.assertEqual(
            self.material_targets(
                after,
                preserved_material["ifcEntityId"]
            ),
            {
                self.get_transform(self.root_id)["ifcEntityId"],
                geometry["ifcEntityId"]
            }
        )

        self.assert_step_references_valid(after)

        content = self.save_model()

        self.assertNotIn(
            "DELETED-MATERIAL-VALUE",
            content
        )

        self.save_and_reload()

        self.assertEqual(
            len(self.list_materials()),
            1
        )

        reloaded_material = self.find_material(
            preserved_material["name"]
        )

        reloaded_geometry = self.find_geometry(
            geometry["globalId"]
        )

        self.assertEqual(
            reloaded_geometry["materials"],
            [reloaded_material["id"]]
        )

        self.assertEqual(
            self.get_transform(self.root_id)["materials"],
            [reloaded_material["id"]]
        )

        self.assert_index_consistency()

    def test_shared_entities(self):
        """Verify preservation of IFC entities still referenced by other objects."""

        root = self.get_transform(self.root_id)
        survivor = self.get_transform(self.survivor_id)

        survivor_mesh = self.tessellate(
            survivor["id"]
        )

        material = self.create_material(
            name="Shared surviving material"
        )

        self.assign_material(
            material["id"],
            root["id"]
        )

        self.assign_material(
            material["id"],
            survivor["id"]
        )

        before = self.save_entities()

        root_items = self.representation_item_ids(
            before,
            root["ifcEntityId"]
        )

        survivor_items = self.representation_item_ids(
            before,
            survivor["ifcEntityId"]
        )

        root_graph = forward_graph(
            before,
            root_items
        )

        survivor_graph = forward_graph(
            before,
            survivor_items
        )

        shared_representation_entities = (
            root_graph & survivor_graph
        )

        self.assertTrue(
            shared_representation_entities
        )

        self.assertTrue(
            any(
                before[identifier]["class"]
                == "IFCRECTANGLEPROFILEDEF"
                for identifier in shared_representation_entities
            )
        )

        self.assertTrue(
            any(
                before[identifier]["class"]
                == "IFCAXIS2PLACEMENT3D"
                for identifier in shared_representation_entities
            )
        )

        root_properties = self.property_definition_ids(
            before,
            root["ifcEntityId"]
        )

        survivor_properties = self.property_definition_ids(
            before,
            survivor["ifcEntityId"]
        )

        self.assertEqual(
            len(root_properties & survivor_properties),
            1
        )

        shared_property_graph = forward_graph(
            before,
            root_properties & survivor_properties
        )

        shared_property_relations = (
            self.property_relations(
                before,
                root["ifcEntityId"]
            )
            & self.property_relations(
                before,
                survivor["ifcEntityId"]
            )
        )

        self.assertEqual(
            len(shared_property_relations),
            1
        )

        self.delete_transform(root["id"])

        after = self.save_entities()

        self.assert_preserved_step_entities(
            before,
            after,
            shared_representation_entities
        )

        self.assert_preserved_step_entities(
            before,
            after,
            shared_property_graph
        )

        self.assertTrue(
            shared_property_relations.issubset(after)
        )

        for relation_id in shared_property_relations:
            arguments = entity_arguments(
                after,
                relation_id
            )

            self.assertEqual(
                step_references(arguments[-2]),
                [survivor["ifcEntityId"]]
            )

        self.assertEqual(
            self.material_targets(
                after,
                material["ifcEntityId"]
            ),
            {survivor["ifcEntityId"]}
        )

        self.assertEqual(
            self.get_transform(survivor["id"])["materials"],
            [material["id"]]
        )

        self.assert_mesh_equivalent(
            survivor_mesh,
            self.tessellate(survivor["id"])
        )

        self.assert_no_references_to(
            after,
            [root["ifcEntityId"]]
        )

        self.assert_step_references_valid(after)

        self.save_and_reload()

        reloaded_survivor = self.find_transform(
            survivor["globalId"]
        )

        reloaded_material = self.find_material(
            material["name"]
        )

        self.assertEqual(
            reloaded_survivor["materials"],
            [reloaded_material["id"]]
        )

        self.assert_mesh_equivalent(
            survivor_mesh,
            self.tessellate(reloaded_survivor["id"])
        )

        self.assert_global_ids_absent(
            [root["globalId"]]
        )

        self.assert_index_consistency()

    def test_multiple_relation_paths(self):
        """Verify that descendants reachable through multiple paths are deleted once."""

        baseline = self.snapshot_model()

        root = self.create_transform(
            name="Multiple path root"
        )

        first = self.create_transform(
            name="First branch",
            parents=parents_input(
                placement=root["id"],
                decomposition=root["id"]
            )
        )

        second = self.create_transform(
            name="Second branch",
            parents=parents_input(
                placement=root["id"],
                decomposition=root["id"]
            ),
            x=10.0
        )

        shared_descendant = self.create_transform(
            name="Shared descendant",
            parents=parents_input(
                placement=first["id"],
                decomposition=second["id"]
            ),
            y=10.0
        )

        last_descendant = self.create_transform(
            name="Last descendant",
            parents=parents_input(
                placement=shared_descendant["id"],
                decomposition=root["id"]
            ),
            z=10.0
        )

        hierarchy = [
            root,
            first,
            second,
            shared_descendant,
            last_descendant
        ]

        self.assertEqual(
            shared_descendant["parents"],
            parents_input(
                placement=first["id"],
                decomposition=second["id"]
            )
        )

        domains = [
            self.create_domain(
                parent_id=transform["id"],
                name=f"Multiple path domain {index}",
                profile=rectangular_profile(1.0, 1.0),
                depth=1.0,
                x=index * 3.0
            )
            for index, transform in enumerate(hierarchy)
        ]

        self.assert_volume(
            self.tessellate(
                root["id"],
                kind="DOMAIN",
                include_children=True
            ),
            5.0
        )

        self.delete_transform(root["id"])

        for transform in hierarchy:
            self.assert_transform_deleted(
                transform["id"]
            )

        for domain in domains:
            self.assert_geometry_deleted(
                domain["id"]
            )

        self.assertEqual(
            self.snapshot_model(),
            baseline
        )

        entities = self.save_entities()

        deleted_step_ids = {
            entity["ifcEntityId"]
            for entity in hierarchy + domains
        }

        self.assert_step_entities_removed(
            entities,
            deleted_step_ids
        )

        self.assert_no_references_to(
            entities,
            deleted_step_ids
        )

        self.assert_step_references_valid(entities)

        # Exercise spatial containment as an additional path.

        spatial_parent = self.storey_id

        spatial_child = self.create_transform(
            name="Spatial multiple path child",
            parents=parents_input(
                spatial=spatial_parent,
                placement=spatial_parent,
                decomposition=spatial_parent
            )
        )

        spatial_grandchild = self.create_transform(
            name="Spatial multiple path grandchild",
            parents=parents_input(
                spatial=spatial_parent,
                placement=spatial_child["id"],
                decomposition=spatial_child["id"]
            )
        )

        self.delete_transform(spatial_parent)

        self.assert_transform_deleted(
            spatial_child["id"]
        )

        self.assert_transform_deleted(
            spatial_grandchild["id"]
        )

        # The fixture's two native products also belong
        # to this storey and must have been deleted.

        remaining_global_ids = {
            item["globalId"]
            for item in self.list_transforms()
        }

        self.assertNotIn(
            ROOT_GLOBAL_ID,
            remaining_global_ids
        )

        self.assertNotIn(
            SURVIVOR_GLOBAL_ID,
            remaining_global_ids
        )

        self.assertNotIn(
            STOREY_GLOBAL_ID,
            remaining_global_ids
        )

        self.assertIn(
            PROJECT_GLOBAL_ID,
            remaining_global_ids
        )

        self.assertEqual(
            self.list_geometries(),
            []
        )

        self.assert_step_references_valid(
            self.save_entities()
        )

        self.assert_index_consistency()

    def test_repeated_deletion(self):
        """Verify explicit errors when deleting an already deleted entity."""

        transform = self.create_transform(
            name="Repeated deletion transform"
        )

        geometry = self.create_geometry(
            name="Repeated deletion feature",
            operation="ADD"
        )

        domain = self.create_domain(
            name="Repeated deletion domain"
        )

        material = self.create_material(
            name="Repeated deletion material"
        )

        native = self.native_geometries(
            self.root_id
        )[0]

        cases = [
            ("transformDelete", transform["id"]),
            ("geometryDelete", geometry["id"]),
            ("geometryDelete", domain["id"]),
            ("geometryDelete", native["id"]),
            ("materialDelete", material["id"])
        ]

        for command, identifier in cases:
            with self.subTest(
                command=command,
                identifier=identifier
            ):
                result = self.client.request(
                    command,
                    {"id": identifier}
                )

                self.assertEqual(
                    result,
                    {"id": identifier}
                )

                model_before = self.snapshot_model()
                entities_before = self.save_entities()

                for iteration in range(3):
                    with self.subTest(iteration=iteration):
                        self.client.expect_error(
                            command,
                            {"id": identifier},
                            "ENTITY_NOT_FOUND"
                        )

                        self.assertEqual(
                            self.snapshot_model(),
                            model_before
                        )

                self.assertEqual(
                    self.save_entities(),
                    entities_before
                )

                self.assert_index_consistency()

        for command, identifier in (
            ("transformDelete", "transform:999999999"),
            ("geometryDelete", "geometry:999999999"),
            ("materialDelete", "material:999999999")
        ):
            self.client.expect_error(
                command,
                {"id": identifier},
                "ENTITY_NOT_FOUND"
            )

        replacement = self.create_transform(
            name="Recovery after repeated deletions"
        )

        replacement_domain = self.create_domain(
            parent_id=replacement["id"]
        )

        replacement_material = self.create_material(
            name="Recovery material"
        )

        self.assertNotEqual(
            replacement["id"],
            transform["id"]
        )

        self.assertNotIn(
            replacement_domain["id"],
            {
                geometry["id"],
                domain["id"],
                native["id"]
            }
        )

        self.assertNotEqual(
            replacement_material["id"],
            material["id"]
        )

        self.assert_volume(
            self.tessellate_domain(replacement_domain),
            12.0
        )

        self.assert_step_references_valid(
            self.save_entities()
        )

        self.assert_index_consistency()

    def test_orphan_cleanup(self):
        """Verify removal of exclusively owned placement and geometry dependencies."""

        baseline = self.save_entities()
        baseline_ids = set(baseline)

        # Test transform placement cleanup independently.

        with self.subTest(category="transform placement"):
            transform = self.create_transform(
                name="Exclusive placement owner",
                x=11.0,
                y=22.0,
                z=33.0
            )

            created = self.save_entities()

            created_ids = set(created) - baseline_ids

            placement_ids = {
                identifier
                for identifier in created_ids
                if created[identifier]["class"] in {
                    "IFCLOCALPLACEMENT",
                    "IFCAXIS2PLACEMENT3D",
                    "IFCCARTESIANPOINT",
                    "IFCDIRECTION"
                }
            }

            self.assertTrue(placement_ids)

            self.assertTrue(
                any(
                    created[identifier]["class"]
                    == "IFCLOCALPLACEMENT"
                    for identifier in placement_ids
                )
            )

            self.delete_transform(transform["id"])

            after = self.save_entities()

            self.assert_no_orphans(
                after,
                placement_ids
            )

        # Each operation receives a newly captured baseline
        # so failures in one cleanup category do not mask others.

        cases = [
            ("SOLID", "ADD"),
            ("SOLID", "SUBTRACT"),
            ("DOMAIN", "ADD")
        ]

        for kind, operation in cases:
            with self.subTest(
                category="geometry dependencies",
                kind=kind,
                operation=operation
            ):
                before = self.save_entities()
                before_ids = set(before)

                geometry = self.create_geometry(
                    name=f"Exclusive {kind} {operation}",
                    kind=kind,
                    operation=operation,
                    profile=[
                        [0, 0],
                        [4, 0],
                        [4, 2],
                        [2, 2],
                        [2, 5],
                        [0, 5]
                    ],
                    depth=3.0,
                    x=12.0,
                    y=24.0,
                    z=36.0
                )

                created = self.save_entities()

                created_ids = (
                    set(created) - before_ids
                )

                feature_graph = forward_graph(
                    created,
                    [geometry["ifcEntityId"]],
                    excluded_ids=before_ids
                )

                owned_classes = {
                    created[identifier]["class"]
                    for identifier in feature_graph
                }

                self.assertIn(
                    "IFCEXTRUDEDAREASOLID",
                    owned_classes
                )

                self.assertIn(
                    "IFCARBITRARYCLOSEDPROFILEDEF",
                    owned_classes
                )

                self.assertIn(
                    "IFCPOLYLINE",
                    owned_classes
                )

                self.assertIn(
                    "IFCPRODUCTDEFINITIONSHAPE",
                    owned_classes
                )

                self.assertIn(
                    "IFCSHAPEREPRESENTATION",
                    owned_classes
                )

                self.assertIn(
                    "IFCLOCALPLACEMENT",
                    owned_classes
                )

                relation_ids = self.feature_relation_ids(
                    created,
                    geometry["ifcEntityId"]
                )

                self.assertTrue(relation_ids)

                expected_removed = (
                    feature_graph | relation_ids
                ) & created_ids

                self.delete_geometry(geometry["id"])

                after = self.save_entities()

                self.assert_step_references_valid(after)

                self.assert_preserved_step_entities(
                    before,
                    after,
                    before_ids
                )

                self.assert_no_orphans(
                    after,
                    expected_removed
                )

        with self.subTest(category="material dependencies"):
            before = self.save_entities()
            before_ids = set(before)

            material = self.create_material(
                name="Exclusive material dependencies",
                properties=[
                    property_input(
                        "OrphanReference",
                        "ORPHAN-MATERIAL-VALUE",
                        source="material"
                    )
                ]
            )

            created = self.save_entities()

            created_ids = (
                set(created) - before_ids
            )

            self.assertTrue(
                any(
                    created[identifier]["class"]
                    == "IFCSURFACESTYLERENDERING"
                    for identifier in created_ids
                )
            )

            self.assertTrue(
                any(
                    created[identifier]["class"]
                    == "IFCMATERIALPROPERTIES"
                    for identifier in created_ids
                )
            )

            self.delete_material(material["id"])

            after = self.save_entities()

            self.assert_no_orphans(
                after,
                created_ids
            )

            self.assert_step_references_valid(after)

        with self.subTest(category="native representation dependencies"):
            before = self.save_entities()

            geometries = self.native_geometries(
                self.root_id
            )

            self.assertEqual(len(geometries), 2)

            survivor = self.get_transform(
                self.survivor_id
            )

            survivor_graph = forward_graph(
                before,
                [survivor["ifcEntityId"]]
            )

            root = self.get_transform(
                self.root_id
            )

            representation_graph = forward_graph(
                before,
                self.representation_ids(
                    before,
                    root["ifcEntityId"]
                )
            )

            # Keep project-owned and surviving product-owned
            # entities outside the deletion candidate set.

            preserved_roots = [
                item["ifcEntityId"]
                for item in self.list_transforms()
                if item["id"] != root["id"]
            ]

            preserved_graph = forward_graph(
                before,
                preserved_roots
            )

            exclusive_representation_entities = (
                representation_graph
                - survivor_graph
                - preserved_graph
            )

            self.assertTrue(
                exclusive_representation_entities
            )

            for geometry in geometries:
                self.delete_geometry(
                    geometry["id"]
                )

            after = self.save_entities()

            self.assert_no_orphans(
                after,
                exclusive_representation_entities
            )

            self.assert_step_references_valid(after)

        self.assert_index_consistency()

    def test_dangling_references(self):
        """Verify absence of STEP references to deleted IFC entities."""

        initial = self.save_entities()

        self.assert_step_references_valid(initial)

        material = self.create_material(
            name="Dangling reference material"
        )

        preserved_material = self.create_material(
            name="Dangling reference survivor"
        )

        parent = self.create_transform(
            name="Dangling reference parent",
            parents=parents_input(
                spatial=self.storey_id
            ),
            properties=[
                property_input(
                    "Reference",
                    "DANGLING-PARENT"
                )
            ]
        )

        child = self.create_transform(
            name="Dangling reference child",
            parents=parents_input(
                spatial=self.storey_id,
                placement=parent["id"],
                decomposition=parent["id"]
            ),
            properties=[
                property_input(
                    "Reference",
                    "DANGLING-CHILD"
                )
            ]
        )

        addition = self.create_geometry(
            parent_id=parent["id"],
            name="Dangling reference addition"
        )

        subtraction = self.create_geometry(
            parent_id=self.root_id,
            name="Dangling reference subtraction",
            operation="SUBTRACT",
            profile=rectangular_profile(0.5, 0.5),
            depth=6.0,
            x=-0.25,
            y=-0.25,
            z=-1.0
        )

        domain = self.create_domain(
            parent_id=child["id"],
            name="Dangling reference domain"
        )

        preserved_domain = self.create_domain(
            parent_id=self.survivor_id,
            name="Preserved dangling reference domain"
        )

        native = self.native_geometries(
            self.root_id
        )[0]

        for target_id in (
            self.root_id,
            self.survivor_id,
            parent["id"],
            child["id"],
            addition["id"],
            subtraction["id"],
            native["id"]
        ):
            self.assign_material(
                material["id"],
                target_id
            )

        self.assign_material(
            preserved_material["id"],
            self.survivor_id
        )

        self.assert_step_references_valid(
            self.save_entities()
        )

        deleted_step_ids = set()

        self.delete_geometry(subtraction["id"])

        deleted_step_ids.add(
            subtraction["ifcEntityId"]
        )

        entities = self.save_entities()

        self.assert_step_entities_removed(
            entities,
            deleted_step_ids
        )

        self.assert_no_references_to(
            entities,
            deleted_step_ids
        )

        self.assert_step_references_valid(entities)

        self.delete_geometry(native["id"])

        deleted_step_ids.add(
            native["ifcEntityId"]
        )

        entities = self.save_entities()

        self.assert_no_references_to(
            entities,
            deleted_step_ids
        )

        self.assert_step_references_valid(entities)

        self.delete_transform(parent["id"])

        deleted_step_ids.update({
            parent["ifcEntityId"],
            child["ifcEntityId"],
            addition["ifcEntityId"],
            domain["ifcEntityId"]
        })

        entities = self.save_entities()

        self.assert_step_entities_removed(
            entities,
            deleted_step_ids
        )

        self.assert_no_references_to(
            entities,
            deleted_step_ids
        )

        self.assert_step_references_valid(entities)

        self.delete_material(material["id"])

        deleted_step_ids.add(
            material["ifcEntityId"]
        )

        entities = self.save_entities()

        self.assert_step_entities_removed(
            entities,
            deleted_step_ids
        )

        self.assert_no_references_to(
            entities,
            deleted_step_ids
        )

        self.assert_step_references_valid(entities)

        self.assertEqual(
            self.get_transform(self.root_id)["materials"],
            []
        )

        self.assertEqual(
            self.get_transform(self.survivor_id)["materials"],
            [preserved_material["id"]]
        )

        self.assertEqual(
            self.get_geometry(preserved_domain["id"]),
            preserved_domain
        )

        self.assert_volume(
            self.tessellate_domain(preserved_domain),
            12.0
        )

        self.assert_index_consistency()

        self.save_and_reload()

        self.assert_step_references_valid(
            self.save_entities()
        )

        self.assert_global_ids_absent([
            parent["globalId"],
            child["globalId"],
            addition["globalId"],
            subtraction["globalId"],
            domain["globalId"]
        ])

        reloaded_domain = self.find_geometry(
            preserved_domain["globalId"]
        )

        self.assert_volume(
            self.tessellate_domain(reloaded_domain),
            12.0
        )

        self.assert_index_consistency()

    def test_deletion_persistence(self):
        """Verify that deleted entities never reappear after IFC roundtrips."""

        deleted_transform = self.create_transform(
            name="Persistent deleted transform",
            properties=[
                property_input(
                    "Reference",
                    "PERSISTENT-DELETED-TRANSFORM"
                )
            ]
        )

        deleted_child = self.create_transform(
            name="Persistent deleted child",
            parents=parents_input(
                placement=deleted_transform["id"],
                decomposition=deleted_transform["id"]
            )
        )

        deleted_geometry = self.create_geometry(
            parent_id=deleted_transform["id"],
            name="Persistent deleted geometry"
        )

        deleted_child_domain = self.create_domain(
            parent_id=deleted_child["id"],
            name="Persistent deleted descendant domain"
        )

        deleted_domain = self.create_domain(
            parent_id=self.root_id,
            name="Persistent deleted domain"
        )

        preserved_domain = self.create_domain(
            parent_id=self.survivor_id,
            name="Persistent preserved domain",
            profile=rectangular_profile(4.0, 5.0),
            depth=3.0,
            x=2.0,
            y=3.0,
            z=4.0
        )

        deleted_material = self.create_material(
            name="Persistent deleted material",
            properties=[
                property_input(
                    "Reference",
                    "PERSISTENT-DELETED-MATERIAL",
                    source="material"
                )
            ]
        )

        preserved_material = self.create_material(
            name="Persistent preserved material"
        )

        self.assign_material(
            deleted_material["id"],
            self.root_id
        )

        self.assign_material(
            deleted_material["id"],
            self.survivor_id
        )

        self.assign_material(
            deleted_material["id"],
            deleted_geometry["id"]
        )

        self.assign_material(
            preserved_material["id"],
            self.survivor_id
        )

        deleted_native = self.native_geometries(
            self.root_id
        )[0]

        deleted_entities = [
            deleted_transform,
            deleted_child,
            deleted_geometry,
            deleted_child_domain,
            deleted_domain,
            deleted_material,
            deleted_native
        ]

        deleted_global_ids = {
            entity["globalId"]
            for entity in deleted_entities
            if entity["globalId"] is not None
        }

        self.delete_transform(
            deleted_transform["id"]
        )

        self.delete_geometry(
            deleted_domain["id"]
        )

        self.delete_geometry(
            deleted_native["id"]
        )

        self.delete_material(
            deleted_material["id"]
        )

        expected_transform_count = len(
            self.list_transforms()
        )

        expected_geometry_count = len(
            self.list_geometries()
        )

        expected_material_count = len(
            self.list_materials()
        )

        expected_transform_global_ids = {
            item["globalId"]
            for item in self.list_transforms()
        }

        expected_geometry_global_ids = {
            item["globalId"]
            for item in self.list_geometries()
            if item["globalId"] is not None
        }

        root_mesh = self.tessellate(
            self.root_id
        )

        survivor_mesh = self.tessellate(
            self.survivor_id
        )

        domain_mesh = self.tessellate_domain(
            preserved_domain
        )

        self.assert_volume(root_mesh, 24.0)
        self.assert_volume(survivor_mesh, 24.0)
        self.assert_volume(domain_mesh, 60.0)

        self.assertEqual(
            expected_material_count,
            1
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            1
        )

        for iteration in range(5):
            with self.subTest(roundtrip=iteration):
                self.assert_global_ids_absent(
                    deleted_global_ids
                )

                entities = self.save_entities()

                self.assert_step_references_valid(
                    entities
                )

                result = self.load_model(
                    self.output_path
                )

                self.assertEqual(
                    result["transformCount"],
                    expected_transform_count
                )

                self.assertEqual(
                    result["geometryCount"],
                    expected_geometry_count
                )

                self.assertEqual(
                    result["materialCount"],
                    expected_material_count
                )

                self.assertEqual(
                    {
                        item["globalId"]
                        for item in self.list_transforms()
                    },
                    expected_transform_global_ids
                )

                self.assertEqual(
                    {
                        item["globalId"]
                        for item in self.list_geometries()
                        if item["globalId"] is not None
                    },
                    expected_geometry_global_ids
                )

                materials = self.snapshot_materials()

                self.assertEqual(
                    {
                        item["name"]
                        for item in materials.values()
                    },
                    {preserved_material["name"]}
                )

                current_material = self.find_material(
                    preserved_material["name"]
                )

                current_domain = self.find_geometry(
                    preserved_domain["globalId"]
                )

                self.assertEqual(
                    current_domain["parentId"],
                    self.survivor_id
                )

                self.assertEqual(
                    current_domain["profile"],
                    preserved_domain["profile"]
                )

                self.assertAlmostEqual(
                    current_domain["depth"],
                    preserved_domain["depth"],
                    delta=NUMERIC_TOLERANCE
                )

                self.assertEqual(
                    current_domain["kind"],
                    "DOMAIN"
                )

                self.assertEqual(
                    current_domain["materials"],
                    []
                )

                self.assertEqual(
                    self.get_transform(
                        self.survivor_id
                    )["materials"],
                    [current_material["id"]]
                )

                self.assertEqual(
                    self.get_transform(
                        self.root_id
                    )["materials"],
                    []
                )

                self.assert_mesh_equivalent(
                    root_mesh,
                    self.tessellate(self.root_id)
                )

                self.assert_mesh_equivalent(
                    survivor_mesh,
                    self.tessellate(self.survivor_id)
                )

                self.assert_mesh_equivalent(
                    domain_mesh,
                    self.tessellate_domain(current_domain)
                )

                self.assert_empty_mesh(
                    self.tessellate(
                        self.root_id,
                        kind="DOMAIN"
                    )
                )

                self.assert_index_consistency()

        # Verify that persisted survivors remain editable.

        current_domain = self.find_geometry(
            preserved_domain["globalId"]
        )

        updated_domain = self.client.request(
            "geometryUpdate",
            {
                "id": current_domain["id"],
                "depth": 6.0
            }
        )

        self.assert_volume(
            self.tessellate_domain(updated_domain),
            120.0
        )

        self.save_and_reload()

        reloaded_domain = self.find_geometry(
            preserved_domain["globalId"]
        )

        self.assert_volume(
            self.tessellate_domain(reloaded_domain),
            120.0
        )

        self.assert_global_ids_absent(
            deleted_global_ids
        )

        # Delete the final preserved domain and material,
        # then verify the resulting empty indexes persist.

        self.delete_geometry(
            reloaded_domain["id"]
        )

        reloaded_material = self.find_material(
            preserved_material["name"]
        )

        self.delete_material(
            reloaded_material["id"]
        )

        deleted_global_ids.add(
            preserved_domain["globalId"]
        )

        self.save_and_reload()

        self.assertEqual(
            self.list_materials(),
            []
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assertEqual(
            self.get_transform(self.root_id)["materials"],
            []
        )

        self.assertEqual(
            self.get_transform(self.survivor_id)["materials"],
            []
        )

        self.assert_mesh_equivalent(
            root_mesh,
            self.tessellate(self.root_id)
        )

        self.assert_mesh_equivalent(
            survivor_mesh,
            self.tessellate(self.survivor_id)
        )

        self.assert_global_ids_absent(
            deleted_global_ids
        )

        self.assert_step_references_valid(
            self.save_entities()
        )

        self.assert_index_consistency()


def main():
    global EXECUTABLE

    arguments = sys.argv[1:]

    if arguments and not arguments[0].startswith("-"):
        EXECUTABLE = Path(
            arguments.pop(0)
        ).resolve()

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