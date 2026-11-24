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


# Standalone integration suite.
# Expected results must be validated against the compiled CLI.
# Domain meshes are tessellated individually, while SOLID features
# are inspected through their parametric definitions and IFC persistence.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30
MATRIX_TOLERANCE = 1e-7
MESH_TOLERANCE = 1e-6

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

PROJECT_GLOBAL_ID = "0000000000000000000001"
FIRST_PRODUCT_GLOBAL_ID = "0000000000000000000002"
SECOND_PRODUCT_GLOBAL_ID = "0000000000000000000003"
SPACE_GLOBAL_ID = "0000000000000000000004"
VIRTUAL_PRODUCT_GLOBAL_ID = "0000000000000000000005"

GLOBAL_ID_PATTERN = re.compile(r"^[0-3][0-9A-Za-z_$]{21}$")

GEOMETRY_SUMMARY_FIELDS = {
    "id",
    "ifcEntityId",
    "globalId",
    "parentId",
    "source",
    "editable",
    "operation",
    "kind"
}

NATIVE_GEOMETRY_FIELDS = GEOMETRY_SUMMARY_FIELDS | {
    "ifcClass",
    "definition"
}

EDITOR_GEOMETRY_FIELDS = NATIVE_GEOMETRY_FIELDS | {
    "name",
    "transforms",
    "profile",
    "depth",
    "materials"
}

PLACEMENT_FIELDS = {
    "local",
    "world",
    "editable",
    "sourceType"
}

DEFAULT_PROFILE = [
    [0, 0],
    [5, 0],
    [5, 3],
    [0, 3]
]

DEFAULT_DEPTH = 2.0

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('geometries-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#8=IFCPROJECT('0000000000000000000001',$,'Geometry test project',$,$,$,$,(#5),#7);
#9=IFCLOCALPLACEMENT($,#4);
#10=IFCCARTESIANPOINT((0.,0.));
#11=IFCAXIS2PLACEMENT2D(#10,$);
#12=IFCRECTANGLEPROFILEDEF(.AREA.,$,#11,2.,1.);
#13=IFCEXTRUDEDAREASOLID(#12,#4,#2,1.);
#14=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#13));
#15=IFCPRODUCTDEFINITIONSHAPE($,$,(#14));
#16=IFCBUILDINGELEMENTPROXY('0000000000000000000002',$,'First physical product',$,$,#9,#15,$,.NOTDEFINED.);
#17=IFCCARTESIANPOINT((100.,0.,0.));
#18=IFCAXIS2PLACEMENT3D(#17,#2,#3);
#19=IFCLOCALPLACEMENT($,#18);
#20=IFCBUILDINGELEMENTPROXY('0000000000000000000003',$,'Second physical product',$,$,#19,$,$,.NOTDEFINED.);
#21=IFCCARTESIANPOINT((200.,0.,0.));
#22=IFCAXIS2PLACEMENT3D(#21,#2,#3);
#23=IFCLOCALPLACEMENT($,#22);
#24=IFCRECTANGLEPROFILEDEF(.AREA.,$,#11,6.,4.);
#25=IFCEXTRUDEDAREASOLID(#24,#4,#2,3.);
#26=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#25));
#27=IFCPRODUCTDEFINITIONSHAPE($,$,(#26));
#28=IFCSPACE('0000000000000000000004',$,'Native spatial domain',$,$,#23,#27,$,.ELEMENT.,.NOTDEFINED.,$);
#29=IFCBUILDINGELEMENTPROXY('0000000000000000000005',$,'Virtual product',$,$,$,$,$,.NOTDEFINED.);
ENDSEC;
END-ISO-10303-21;
"""


def identity_matrix():
    return [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1
    ]


def translation_matrix(x=0, y=0, z=0):
    return [
        1, 0, 0, x,
        0, 1, 0, y,
        0, 0, 1, z,
        0, 0, 0, 1
    ]


def rotation_z(angle):
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)

    return [
        cosine, -sine, 0, 0,
        sine, cosine, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1
    ]


def multiply_matrices(left, right):
    return [
        sum(
            left[row * 4 + index] * right[index * 4 + column]
            for index in range(4)
        )
        for row in range(4)
        for column in range(4)
    ]


def transform_point(matrix, point):
    vector = (*point, 1.0)

    return tuple(
        sum(
            matrix[row * 4 + column] * vector[column]
            for column in range(4)
        )
        for row in range(3)
    )


def transform_input(matrix=None, space="parent"):
    return {
        "space": space,
        "matrix": (
            identity_matrix()
            if matrix is None
            else matrix
        )
    }


def parents_input(
    spatial=None,
    placement=None,
    decomposition=None
):
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


def polygon_area(profile):
    return abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(
                profile,
                profile[1:] + profile[:1]
            )
        )
    ) / 2.0


def extrusion_corners(profile, depth):
    return [
        (x, y, z)
        for x, y in profile
        for z in (0.0, depth)
    ]


def mesh_points(mesh):
    vertices = mesh["vertices"]

    return [
        tuple(vertices[offset:offset + 3])
        for offset in range(0, len(vertices), 3)
    ]


def points_bounds(points):
    if not points:
        raise AssertionError("No points available")

    return tuple(
        function(point[axis] for point in points)
        for function in (min, max)
        for axis in range(3)
    )


def mesh_bounds(mesh):
    return points_bounds(mesh_points(mesh))


def mesh_volume(mesh):
    points = mesh_points(mesh)
    indices = mesh["indices"]

    if not points:
        return 0.0

    origin = points[0]
    volume = 0.0

    for offset in range(0, len(indices), 3):
        a, b, c = [
            tuple(
                points[index][axis] - origin[axis]
                for axis in range(3)
            )
            for index in indices[offset:offset + 3]
        ]

        cross = (
            b[1] * c[2] - b[2] * c[1],
            b[2] * c[0] - b[0] * c[2],
            b[0] * c[1] - b[1] * c[0]
        )

        volume += sum(
            a[axis] * cross[axis]
            for axis in range(3)
        )

    return abs(volume) / 6.0


def mesh_signature(mesh, precision=7):
    points = [
        tuple(round(value, precision) for value in point)
        for point in mesh_points(mesh)
    ]

    indices = mesh["indices"]

    return Counter(
        tuple(sorted(
            points[index]
            for index in indices[offset:offset + 3]
        ))
        for offset in range(0, len(indices), 3)
    )


def parse_step_entities(content):
    # Targeted parser for the controlled IFC fixture.
    # This is not a general-purpose STEP parser.

    pattern = re.compile(
        r"#(\d+)\s*=\s*(IFC[A-Z0-9_]+)\s*\((.*?)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL
    )

    return {
        int(match.group(1)): {
            "class": match.group(2).upper(),
            "arguments": match.group(3)
        }
        for match in pattern.finditer(content)
    }


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


class Client:
    def __init__(
        self,
        executable,
        timeout=REQUEST_TIMEOUT
    ):
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

        payload = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False
        )

        self.process.stdin.write(payload + "\n")
        self.process.stdin.flush()

        return request_id

    def read(self, request_id):
        try:
            item = self.responses.get(
                timeout=self.timeout
            )
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

            if set(error) != {
                "code",
                "message",
                "details"
            }:
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
        expected_code="INVALID_PARAMETERS"
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

        return response["error"]

    def tessellate(
        self,
        transform_id,
        kind="SOLID",
        space="world",
        geometry_id=None,
        include_children=False
    ):
        options = {
            "space": space,
            "kind": kind,
            "includeNormals": True,
            "includeMaterials": False,
            "includeChildren": include_children
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
            "normals": []
        }

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.normals": 3,
            "mesh.end": 4
        }

        begin = None
        last_event = -1

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

            current_event = event_order[event_name]

            if current_event < last_event:
                raise AssertionError(
                    f"Invalid mesh event order: {event}"
                )

            last_event = current_event

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

            key = event_name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(
                    f"Invalid mesh chunk offset: {event}"
                )

            if not isinstance(event["values"], list):
                raise AssertionError(
                    f"Invalid mesh values: {event}"
                )

            mesh[key].extend(event["values"])

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        for field in ("vertexCount", "indexCount"):
            if type(mesh[field]) is not int or mesh[field] < 0:
                raise AssertionError(
                    f"Invalid mesh count: {begin}"
                )

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError("Invalid triangle indices")

        if len(mesh["normals"]) % 3 != 0:
            raise AssertionError("Invalid normal array length")

        for index in mesh["indices"]:
            if (
                type(index) is not int
                or not 0 <= index < mesh["vertexCount"]
            ):
                raise AssertionError(
                    f"Invalid vertex index: {index}"
                )

        for value in mesh["vertices"] + mesh["normals"]:
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
            ):
                raise AssertionError(
                    f"Invalid mesh component: {value}"
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


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-geometries-"
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

        self.load_model()

    def tearDown(self):
        self.client.close()

    def load_model(self, path=None):
        path = self.input_path if path is None else path

        result = self.client.request(
            "load",
            {"path": str(path)}
        )

        self.assertEqual(
            result["path"],
            str(path)
        )

        self.assertEqual(
            result["schema"].upper(),
            "IFC4"
        )

        self.project_id = self.find_transform(
            PROJECT_GLOBAL_ID
        )["id"]

        self.first_product_id = self.find_transform(
            FIRST_PRODUCT_GLOBAL_ID
        )["id"]

        self.second_product_id = self.find_transform(
            SECOND_PRODUCT_GLOBAL_ID
        )["id"]

        self.space_id = self.find_transform(
            SPACE_GLOBAL_ID
        )["id"]

        self.virtual_product_id = self.find_transform(
            VIRTUAL_PRODUCT_GLOBAL_ID
        )["id"]

        self.assertEqual(
            result["geometryCount"],
            len(self.list_geometries())
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

        self.assertGreater(
            self.output_path.stat().st_size,
            0
        )

        return self.output_path.read_text(
            encoding="utf-8"
        )

    def save_and_reload(self):
        self.save_model()

        return self.load_model(
            self.output_path
        )

    def list_transforms(self):
        return self.client.request(
            "transformGetAll"
        )

    def get_transform(self, identifier):
        return self.client.request(
            "transformGet",
            {"id": identifier}
        )

    def find_transform(self, global_id):
        matches = [
            transform
            for transform in self.list_transforms()
            if transform["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one transform with GlobalId {global_id}"
        )

        return self.get_transform(
            matches[0]["id"]
        )

    def create_transform(
        self,
        name="Geometry parent",
        parent_id=None,
        matrix=None
    ):
        return self.client.request(
            "transformCreate",
            {
                "name": name,
                "parents": parents_input(
                    placement=parent_id
                ),
                "transform": transform_input(matrix),
                "properties": []
            }
        )

    def list_geometries(
        self,
        parent_id=None,
        kind=None
    ):
        params = {
            "transformId": parent_id
        }

        if kind is not None:
            params["kind"] = kind

        return self.client.request(
            "geometryGetAll",
            params
        )

    def get_geometry(self, identifier):
        return self.client.request(
            "geometryGet",
            {"id": identifier}
        )

    def find_geometry(self, global_id):
        self.assertIsNotNone(global_id)

        matches = [
            geometry
            for geometry in self.list_geometries()
            if geometry["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one geometry with GlobalId {global_id}"
        )

        return self.get_geometry(
            matches[0]["id"]
        )

    def native_geometry(self, parent_id):
        matches = [
            geometry
            for geometry in self.list_geometries(
                parent_id=parent_id
            )
            if geometry["source"] == "ifc"
        ]

        self.assertEqual(
            len(matches),
            1
        )

        return self.get_geometry(
            matches[0]["id"]
        )

    def geometry_create_params(
        self,
        parent_id=None,
        name="Created geometry",
        kind="SOLID",
        operation="ADD",
        profile=None,
        depth=DEFAULT_DEPTH,
        matrix=None,
        space="parent"
    ):
        params = {
            "parentId": (
                self.first_product_id
                if parent_id is None
                else parent_id
            ),
            "name": name,
            "operation": operation,
            "profile": (
                DEFAULT_PROFILE
                if profile is None
                else profile
            ),
            "depth": depth,
            "transform": transform_input(
                matrix,
                space
            )
        }

        if kind is not None:
            params["kind"] = kind

        return params

    def create_geometry(self, **kwargs):
        return self.client.request(
            "geometryCreate",
            self.geometry_create_params(**kwargs)
        )

    def update_geometry(self, identifier, **changes):
        return self.client.request(
            "geometryUpdate",
            {
                "id": identifier,
                **changes
            }
        )

    def delete_geometry(self, identifier):
        result = self.client.request(
            "geometryDelete",
            {"id": identifier}
        )

        self.assertEqual(
            result,
            {"id": identifier}
        )

    def create_material(self, name="Geometry material"):
        return self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": "concrete",
                "visual": {
                    "color": [0.5, 0.5, 0.5],
                    "opacity": 1.0,
                    "metallic": 0.0,
                    "roughness": 0.5
                },
                "properties": []
            }
        )

    def assign_material(self, material_id, target_id):
        params = {
            "materialId": material_id,
            "targetId": target_id
        }

        result = self.client.request(
            "materialAssign",
            params
        )

        self.assertEqual(result, params)

    def find_material(self, name):
        summaries = self.client.request(
            "materialGetAll"
        )

        materials = [
            self.client.request(
                "materialGet",
                {"id": summary["id"]}
            )
            for summary in summaries
        ]

        matches = [
            material
            for material in materials
            if material["name"] == name
        ]

        self.assertEqual(
            len(matches),
            1
        )

        return matches[0]

    def geometry_ids(self, **kwargs):
        return {
            geometry["id"]
            for geometry in self.list_geometries(**kwargs)
        }

    def snapshot_geometries(self):
        return {
            geometry["id"]: self.get_geometry(
                geometry["id"]
            )
            for geometry in self.list_geometries()
        }

    def snapshot_transforms(self):
        return {
            transform["id"]: self.get_transform(
                transform["id"]
            )
            for transform in self.list_transforms()
        }

    def assert_identity(self, before, after):
        for field in (
            "id",
            "ifcEntityId",
            "globalId"
        ):
            self.assertEqual(
                before[field],
                after[field],
                f"Geometry identity changed: {field}"
            )

    def assert_matrix(
        self,
        actual,
        expected,
        tolerance=MATRIX_TOLERANCE
    ):
        self.assertIsInstance(actual, list)
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertIn(
                type(value),
                (int, float)
            )

            self.assertTrue(
                math.isfinite(value)
            )

            self.assertAlmostEqual(
                value,
                reference,
                delta=tolerance,
                msg=f"Invalid matrix component {index}"
            )

    def assert_profile(self, actual, expected):
        self.assertIsInstance(actual, list)

        self.assertEqual(
            len(actual),
            len(expected)
        )

        for index, (point, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertIsInstance(point, list)
            self.assertEqual(len(point), 2)

            for axis in range(2):
                self.assertIn(
                    type(point[axis]),
                    (int, float)
                )

                self.assertTrue(
                    math.isfinite(point[axis])
                )

                self.assertAlmostEqual(
                    point[axis],
                    reference[axis],
                    delta=MATRIX_TOLERANCE,
                    msg=f"Invalid profile point {index}, axis {axis}"
                )

    def assert_geometry_summary(self, summary):
        self.assertEqual(
            set(summary),
            GEOMETRY_SUMMARY_FIELDS
        )

        self.assertIsInstance(
            summary["id"],
            str
        )

        self.assertTrue(
            summary["id"].startswith("geometry:")
        )

        self.assertIs(
            type(summary["ifcEntityId"]),
            int
        )

        self.assertGreater(
            summary["ifcEntityId"],
            0
        )

        self.assertIsInstance(
            summary["parentId"],
            str
        )

        self.assertTrue(
            summary["parentId"].startswith("transform:")
        )

        self.assertIn(
            summary["source"],
            {"ifc", "editor"}
        )

        self.assertIs(
            type(summary["editable"]),
            bool
        )

        self.assertIn(
            summary["operation"],
            {"BASE", "ADD", "SUBTRACT"}
        )

        self.assertIn(
            summary["kind"],
            {"SOLID", "DOMAIN"}
        )

        if summary["source"] == "ifc":
            self.assertIs(
                summary["editable"],
                False
            )

            self.assertEqual(
                summary["operation"],
                "BASE"
            )

        else:
            self.assertIs(
                summary["editable"],
                True
            )

            self.assertIsInstance(
                summary["globalId"],
                str
            )

            self.assertRegex(
                summary["globalId"],
                GLOBAL_ID_PATTERN
            )

            self.assertIn(
                summary["operation"],
                {"ADD", "SUBTRACT"}
            )

    def assert_geometry_structure(self, geometry):
        summary = {
            field: geometry[field]
            for field in GEOMETRY_SUMMARY_FIELDS
        }

        self.assert_geometry_summary(summary)

        if geometry["source"] == "ifc":
            self.assertEqual(
                set(geometry),
                NATIVE_GEOMETRY_FIELDS
            )

        else:
            self.assertEqual(
                set(geometry),
                EDITOR_GEOMETRY_FIELDS
            )

            self.assertIsInstance(
                geometry["name"],
                str
            )

            self.assertEqual(
                set(geometry["transforms"]),
                PLACEMENT_FIELDS
            )

            self.assertEqual(
                geometry["transforms"]["sourceType"],
                "IfcLocalPlacement"
            )

            self.assertIs(
                geometry["transforms"]["editable"],
                True
            )

            for field in ("local", "world"):
                values = geometry["transforms"][field]

                self.assertIsInstance(values, list)
                self.assertEqual(len(values), 16)

                for value in values:
                    self.assertIn(
                        type(value),
                        (int, float)
                    )

                    self.assertTrue(
                        math.isfinite(value)
                    )

            self.assertIsInstance(
                geometry["profile"],
                list
            )

            self.assertGreaterEqual(
                len(geometry["profile"]),
                3
            )

            self.assertIn(
                type(geometry["depth"]),
                (int, float)
            )

            self.assertTrue(
                math.isfinite(geometry["depth"])
            )

            self.assertGreater(
                geometry["depth"],
                0
            )

            self.assertIsInstance(
                geometry["materials"],
                list
            )

            self.assertEqual(
                len(geometry["materials"]),
                len(set(geometry["materials"]))
            )

            for material_id in geometry["materials"]:
                self.assertTrue(
                    material_id.startswith("material:")
                )

            if geometry["kind"] == "DOMAIN":
                self.assertEqual(
                    geometry["ifcClass"],
                    "IfcSpatialZone"
                )

                self.assertEqual(
                    geometry["operation"],
                    "ADD"
                )

                self.assertEqual(
                    geometry["materials"],
                    []
                )

            elif geometry["operation"] == "ADD":
                self.assertEqual(
                    geometry["ifcClass"],
                    "IfcProjectionElement"
                )

            elif geometry["operation"] == "SUBTRACT":
                self.assertEqual(
                    geometry["ifcClass"],
                    "IfcOpeningElement"
                )

        self.assertIsInstance(
            geometry["ifcClass"],
            str
        )

        self.assertIsInstance(
            geometry["definition"],
            dict
        )

        self.assertTrue(
            {
                "ifcClass",
                "attributes",
                "references"
            }.issubset(
                geometry["definition"]
            )
        )

        self.assertEqual(
            geometry["definition"]["ifcClass"],
            geometry["ifcClass"]
        )

        self.assertIsInstance(
            geometry["definition"]["attributes"],
            dict
        )

        self.assertIsInstance(
            geometry["definition"]["references"],
            dict
        )

    def assert_editor_geometry(
        self,
        geometry,
        parent_id,
        name,
        kind="SOLID",
        operation="ADD",
        profile=None,
        depth=DEFAULT_DEPTH,
        local_matrix=None,
        world_matrix=None,
        materials=None
    ):
        profile = (
            DEFAULT_PROFILE
            if profile is None
            else profile
        )

        local_matrix = (
            identity_matrix()
            if local_matrix is None
            else local_matrix
        )

        if world_matrix is None:
            parent = self.get_transform(
                parent_id
            )

            world_matrix = multiply_matrices(
                parent["transforms"]["world"],
                local_matrix
            )

        materials = (
            []
            if materials is None
            else materials
        )

        self.assert_geometry_structure(geometry)

        self.assertEqual(
            geometry["source"],
            "editor"
        )

        self.assertIs(
            geometry["editable"],
            True
        )

        self.assertEqual(
            geometry["parentId"],
            parent_id
        )

        self.assertEqual(
            geometry["name"],
            name
        )

        self.assertEqual(
            geometry["kind"],
            kind
        )

        self.assertEqual(
            geometry["operation"],
            operation
        )

        self.assert_profile(
            geometry["profile"],
            profile
        )

        self.assertAlmostEqual(
            geometry["depth"],
            depth,
            delta=MATRIX_TOLERANCE
        )

        self.assert_matrix(
            geometry["transforms"]["local"],
            local_matrix
        )

        self.assert_matrix(
            geometry["transforms"]["world"],
            world_matrix
        )

        self.assertEqual(
            set(geometry["materials"]),
            set(materials)
        )

        self.assertEqual(
            self.get_geometry(geometry["id"]),
            geometry
        )

        parent = self.get_transform(parent_id)

        self.assertIn(
            geometry["id"],
            parent["geometries"]
        )

    def assert_bounds(
        self,
        mesh,
        expected,
        tolerance=MESH_TOLERANCE
    ):
        actual = mesh_bounds(mesh)

        self.assertEqual(
            len(actual),
            len(expected)
        )

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertAlmostEqual(
                value,
                reference,
                delta=tolerance,
                msg=f"Invalid bounding box component {index}"
            )

    def assert_closed(self, mesh):
        points = [
            tuple(round(value, 7) for value in point)
            for point in mesh_points(mesh)
        ]

        indices = mesh["indices"]

        self.assertTrue(points)
        self.assertTrue(indices)

        edges = Counter()

        for offset in range(0, len(indices), 3):
            a, b, c = (
                points[index]
                for index in indices[offset:offset + 3]
            )

            self.assertEqual(
                len({a, b, c}),
                3,
                "Degenerate triangle detected"
            )

            for edge in (
                (a, b),
                (b, c),
                (c, a)
            ):
                edges[tuple(sorted(edge))] += 1

        invalid_edges = {
            edge: count
            for edge, count in edges.items()
            if count != 2
        }

        self.assertFalse(
            invalid_edges,
            f"Mesh is not watertight: {invalid_edges}"
        )

    def assert_point_sets(
        self,
        actual,
        expected,
        tolerance=MESH_TOLERANCE
    ):
        self.assertTrue(actual)
        self.assertTrue(expected)

        for point in expected:
            self.assertTrue(
                any(
                    math.dist(point, candidate) <= tolerance
                    for candidate in actual
                ),
                f"Missing expected point: {point}"
            )

        for point in actual:
            self.assertTrue(
                any(
                    math.dist(point, candidate) <= tolerance
                    for candidate in expected
                ),
                f"Unexpected point: {point}"
            )

    def assert_equivalent_meshes(self, first, second):
        self.assertEqual(
            first["indexCount"],
            second["indexCount"]
        )

        self.assertEqual(
            mesh_signature(first),
            mesh_signature(second)
        )

    def assert_empty_mesh(self, mesh):
        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)
        self.assertEqual(mesh["vertices"], [])
        self.assertEqual(mesh["indices"], [])
        self.assertEqual(mesh["normals"], [])

    def assert_domain_extrusion(
        self,
        geometry,
        space="world"
    ):
        self.assertEqual(
            geometry["kind"],
            "DOMAIN"
        )

        mesh = self.client.tessellate(
            geometry["parentId"],
            kind="DOMAIN",
            space=space,
            geometry_id=geometry["id"]
        )

        expected_matrix = geometry["transforms"][
            "world" if space == "world" else "local"
        ]

        expected_points = [
            transform_point(
                expected_matrix,
                point
            )
            for point in extrusion_corners(
                geometry["profile"],
                geometry["depth"]
            )
        ]

        expected_volume = (
            polygon_area(geometry["profile"])
            * geometry["depth"]
        )

        self.assertGreater(
            mesh["vertexCount"],
            0
        )

        self.assertGreater(
            mesh["indexCount"],
            0
        )

        self.assert_bounds(
            mesh,
            points_bounds(expected_points)
        )

        self.assert_point_sets(
            mesh_points(mesh),
            expected_points
        )

        self.assert_closed(mesh)

        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected_volume,
            delta=MESH_TOLERANCE
        )

        return mesh

    def assert_geometry_links(self, parent_id, expected_ids):
        expected_ids = set(expected_ids)

        transform = self.get_transform(
            parent_id
        )

        self.assertEqual(
            len(transform["geometries"]),
            len(set(transform["geometries"]))
        )

        self.assertEqual(
            set(transform["geometries"]),
            expected_ids
        )

        self.assertEqual(
            self.geometry_ids(
                parent_id=parent_id
            ),
            expected_ids
        )

        for geometry_id in expected_ids:
            self.assertEqual(
                self.get_geometry(
                    geometry_id
                )["parentId"],
                parent_id
            )

    def assert_geometry_index_consistency(self):
        geometries = self.list_geometries()

        identifiers = [
            geometry["id"]
            for geometry in geometries
        ]

        self.assertEqual(
            len(identifiers),
            len(set(identifiers))
        )

        transforms = {
            transform["id"]
            for transform in self.list_transforms()
        }

        by_parent = {
            transform_id: set()
            for transform_id in transforms
        }

        for summary in geometries:
            self.assert_geometry_summary(summary)

            self.assertIn(
                summary["parentId"],
                transforms
            )

            geometry = self.get_geometry(
                summary["id"]
            )

            self.assert_geometry_structure(geometry)

            self.assertEqual(
                summary,
                {
                    field: geometry[field]
                    for field in GEOMETRY_SUMMARY_FIELDS
                }
            )

            by_parent[summary["parentId"]].add(
                summary["id"]
            )

        for parent_id, geometry_ids in by_parent.items():
            self.assert_geometry_links(
                parent_id,
                geometry_ids
            )

        solid_ids = self.geometry_ids(kind="SOLID")
        domain_ids = self.geometry_ids(kind="DOMAIN")

        self.assertFalse(
            solid_ids & domain_ids
        )

        self.assertEqual(
            solid_ids | domain_ids,
            set(identifiers)
        )

        self.assertEqual(
            self.geometry_ids(kind="ALL"),
            set(identifiers)
        )

    def assert_ifc_extrusion(
        self,
        content,
        geometry
    ):
        entities = parse_step_entities(content)

        matching_features = [
            identifier
            for identifier, entity in entities.items()
            if (
                entity["class"] == geometry["ifcClass"].upper()
                and re.match(
                    rf"\s*'{re.escape(geometry['globalId'])}'\s*,",
                    entity["arguments"]
                )
            )
        ]

        self.assertEqual(
            len(matching_features),
            1
        )

        feature_id = matching_features[0]

        feature_references = step_references(
            entities[feature_id]["arguments"]
        )

        product_shapes = [
            identifier
            for identifier in feature_references
            if (
                identifier in entities
                and entities[identifier]["class"]
                == "IFCPRODUCTDEFINITIONSHAPE"
            )
        ]

        self.assertEqual(
            len(product_shapes),
            1
        )

        representation_ids = step_references(
            entities[product_shapes[0]]["arguments"]
        )

        solids = []

        for representation_id in representation_ids:
            representation = entities[representation_id]

            if representation["class"] != "IFCSHAPEREPRESENTATION":
                continue

            for item_id in step_references(
                representation["arguments"]
            ):
                if (
                    entities[item_id]["class"]
                    == "IFCEXTRUDEDAREASOLID"
                ):
                    solids.append(item_id)

        self.assertEqual(
            len(solids),
            1,
            "Expected one IfcExtrudedAreaSolid"
        )

        solid = entities[solids[0]]

        depth_match = re.search(
            r",\s*([+-]?\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?)\s*$",
            solid["arguments"]
        )

        self.assertIsNotNone(
            depth_match,
            "Missing extrusion depth"
        )

        self.assertAlmostEqual(
            float(depth_match.group(1)),
            geometry["depth"],
            delta=MATRIX_TOLERANCE
        )

        profile_ids = [
            identifier
            for identifier in step_references(
                solid["arguments"]
            )
            if (
                entities[identifier]["class"]
                == "IFCARBITRARYCLOSEDPROFILEDEF"
            )
        ]

        self.assertEqual(
            len(profile_ids),
            1
        )

        profile = entities[profile_ids[0]]

        polyline_ids = [
            identifier
            for identifier in step_references(
                profile["arguments"]
            )
            if (
                entities[identifier]["class"]
                == "IFCPOLYLINE"
            )
        ]

        self.assertEqual(
            len(polyline_ids),
            1
        )

        point_ids = step_references(
            entities[polyline_ids[0]]["arguments"]
        )

        coordinates = []

        for point_id in point_ids:
            point = entities[point_id]

            self.assertEqual(
                point["class"],
                "IFCCARTESIANPOINT"
            )

            values = [
                float(value.strip())
                for value in point["arguments"]
                .strip()
                .strip("()")
                .split(",")
            ]

            self.assertEqual(
                len(values),
                2
            )

            coordinates.append(values)

        self.assertEqual(
            len(coordinates),
            len(geometry["profile"]) + 1
        )

        self.assert_profile(
            coordinates[:-1],
            geometry["profile"]
        )

        self.assert_profile(
            [coordinates[-1]],
            [coordinates[0]]
        )

        if geometry["kind"] == "DOMAIN":
            relation_class = "IFCRELASSIGNSTOPRODUCT"

        elif geometry["operation"] == "ADD":
            relation_class = "IFCRELPROJECTSELEMENT"

        else:
            relation_class = "IFCRELVOIDSELEMENT"

        parent = self.get_transform(
            geometry["parentId"]
        )

        matching_parents = [
            identifier
            for identifier, entity in entities.items()
            if re.match(
                rf"\s*'{re.escape(parent['globalId'])}'\s*,",
                entity["arguments"]
            )
        ]

        self.assertEqual(
            len(matching_parents),
            1
        )

        parent_step_id = matching_parents[0]

        matching_relations = [
            entity
            for entity in entities.values()
            if (
                entity["class"] == relation_class
                and {
                    feature_id,
                    parent_step_id
                }.issubset(
                    set(step_references(entity["arguments"]))
                )
            )
        ]

        self.assertEqual(
            len(matching_relations),
            1
        )

        if geometry["kind"] == "DOMAIN":
            self.assertIn(
                "ifc-cli:domain",
                entities[feature_id]["arguments"]
            )

            self.assertIn(
                "ifc-cli:domain-parent",
                matching_relations[0]["arguments"]
            )

    def test_geometry_listing(self):
        """Verify listing of every indexed geometry."""

        initial = self.list_geometries()

        self.assertEqual(
            len(initial),
            2
        )

        self.assertEqual(
            {
                geometry["kind"]
                for geometry in initial
            },
            {"SOLID", "DOMAIN"}
        )

        self.assertTrue(
            all(
                geometry["source"] == "ifc"
                for geometry in initial
            )
        )

        native_solid = self.native_geometry(
            self.first_product_id
        )

        native_domain = self.native_geometry(
            self.space_id
        )

        self.assertEqual(
            native_solid["kind"],
            "SOLID"
        )

        self.assertEqual(
            native_domain["kind"],
            "DOMAIN"
        )

        transforms_before = self.list_transforms()

        solid = self.create_geometry(
            name="Listed solid",
            kind="SOLID"
        )

        domain = self.create_geometry(
            name="Listed domain",
            kind="DOMAIN"
        )

        opening = self.create_geometry(
            parent_id=self.second_product_id,
            name="Listed opening",
            kind="SOLID",
            operation="SUBTRACT"
        )

        geometries = self.list_geometries()

        self.assertEqual(
            len(geometries),
            5
        )

        self.assertEqual(
            self.geometry_ids(),
            {
                native_solid["id"],
                native_domain["id"],
                solid["id"],
                domain["id"],
                opening["id"]
            }
        )

        self.assertEqual(
            self.list_transforms(),
            transforms_before
        )

        self.assertEqual(
            len([
                geometry
                for geometry in geometries
                if geometry["source"] == "editor"
            ]),
            3
        )

        self.assert_geometry_index_consistency()

        self.delete_geometry(domain["id"])

        self.assertEqual(
            len(self.list_geometries()),
            4
        )

        self.assertNotIn(
            domain["id"],
            self.geometry_ids()
        )

        self.assert_geometry_index_consistency()

    def test_geometry_parent_filter(self):
        """Verify filtering by the direct parent transform."""

        first_native = self.native_geometry(
            self.first_product_id
        )

        first_solid = self.create_geometry(
            parent_id=self.first_product_id,
            name="First product solid"
        )

        first_domain = self.create_geometry(
            parent_id=self.first_product_id,
            name="First product domain",
            kind="DOMAIN"
        )

        second_domain = self.create_geometry(
            parent_id=self.second_product_id,
            name="Second product domain",
            kind="DOMAIN"
        )

        child = self.create_transform(
            name="Child geometry parent",
            parent_id=self.first_product_id
        )

        child_domain = self.create_geometry(
            parent_id=child["id"],
            name="Child domain",
            kind="DOMAIN"
        )

        self.assertEqual(
            self.geometry_ids(
                parent_id=self.first_product_id
            ),
            {
                first_native["id"],
                first_solid["id"],
                first_domain["id"]
            }
        )

        self.assertEqual(
            self.geometry_ids(
                parent_id=self.second_product_id
            ),
            {second_domain["id"]}
        )

        self.assertEqual(
            self.geometry_ids(
                parent_id=child["id"]
            ),
            {child_domain["id"]}
        )

        self.assertNotIn(
            child_domain["id"],
            self.geometry_ids(
                parent_id=self.first_product_id
            ),
            "Parent filtering must not include descendants"
        )

        self.assertEqual(
            self.list_geometries(
                parent_id=self.project_id
            ),
            []
        )

        self.assertEqual(
            self.list_geometries(
                parent_id=self.virtual_product_id
            ),
            []
        )

        self.assertEqual(
            self.geometry_ids(
                parent_id=self.first_product_id,
                kind="DOMAIN"
            ),
            {first_domain["id"]}
        )

        self.assertEqual(
            self.geometry_ids(
                parent_id=self.first_product_id,
                kind="SOLID"
            ),
            {
                first_native["id"],
                first_solid["id"]
            }
        )

        self.client.expect_error(
            "geometryGetAll",
            {
                "transformId": "transform:999999999"
            },
            "ENTITY_NOT_FOUND"
        )

        self.assert_geometry_index_consistency()

    def test_geometry_kind_filter(self):
        """Verify SOLID, DOMAIN and ALL geometry filters."""

        native_solid = self.native_geometry(
            self.first_product_id
        )

        native_domain = self.native_geometry(
            self.space_id
        )

        solid = self.create_geometry(
            name="Filtered solid",
            kind="SOLID"
        )

        opening = self.create_geometry(
            name="Filtered opening",
            kind="SOLID",
            operation="SUBTRACT"
        )

        first_domain = self.create_geometry(
            name="Filtered first domain",
            kind="DOMAIN"
        )

        second_domain = self.create_geometry(
            parent_id=self.second_product_id,
            name="Filtered second domain",
            kind="DOMAIN"
        )

        expected_solids = {
            native_solid["id"],
            solid["id"],
            opening["id"]
        }

        expected_domains = {
            native_domain["id"],
            first_domain["id"],
            second_domain["id"]
        }

        self.assertEqual(
            self.geometry_ids(kind="SOLID"),
            expected_solids
        )

        self.assertEqual(
            self.geometry_ids(kind="DOMAIN"),
            expected_domains
        )

        self.assertEqual(
            self.geometry_ids(kind="ALL"),
            expected_solids | expected_domains
        )

        self.assertEqual(
            self.list_geometries(),
            self.list_geometries(kind="ALL")
        )

        for kind in ("SOLID", "DOMAIN"):
            with self.subTest(kind=kind):
                geometries = self.list_geometries(
                    kind=kind
                )

                self.assertTrue(
                    all(
                        geometry["kind"] == kind
                        for geometry in geometries
                    )
                )

        self.assertEqual(
            self.geometry_ids(
                parent_id=self.space_id,
                kind="DOMAIN"
            ),
            {native_domain["id"]}
        )

        self.assertEqual(
            self.list_geometries(
                parent_id=self.space_id,
                kind="SOLID"
            ),
            []
        )

        self.assert_geometry_index_consistency()

        self.save_and_reload()

        self.assertEqual(
            len(self.list_geometries(kind="SOLID")),
            3
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            3
        )

        self.assert_geometry_index_consistency()

    def test_default_geometry_kind(self):
        """Verify SOLID as the default geometry creation category."""

        params = self.geometry_create_params(
            name="Default kind geometry",
            kind=None
        )

        self.assertNotIn(
            "kind",
            params
        )

        geometry = self.client.request(
            "geometryCreate",
            params
        )

        self.assert_editor_geometry(
            geometry,
            parent_id=self.first_product_id,
            name="Default kind geometry",
            kind="SOLID"
        )

        self.assertEqual(
            geometry["ifcClass"],
            "IfcProjectionElement"
        )

        opening = self.create_geometry(
            name="Default kind opening",
            kind=None,
            operation="SUBTRACT"
        )

        self.assert_editor_geometry(
            opening,
            parent_id=self.first_product_id,
            name="Default kind opening",
            kind="SOLID",
            operation="SUBTRACT"
        )

        explicit = self.create_geometry(
            name="Explicit solid",
            kind="SOLID"
        )

        for field in (
            "kind",
            "source",
            "editable",
            "operation",
            "ifcClass",
            "profile",
            "depth",
            "transforms",
            "materials"
        ):
            self.assertEqual(
                geometry[field],
                explicit[field]
            )

        self.assertIn(
            geometry["id"],
            self.geometry_ids(kind="SOLID")
        )

        self.assertNotIn(
            geometry["id"],
            self.geometry_ids(kind="DOMAIN")
        )

        content = self.save_model()

        self.assert_ifc_extrusion(
            content,
            geometry
        )

        self.assert_ifc_extrusion(
            content,
            opening
        )

        self.load_model(self.output_path)

        reloaded = self.find_geometry(
            geometry["globalId"]
        )

        self.assertEqual(
            reloaded["kind"],
            "SOLID"
        )

        self.assert_geometry_index_consistency()

    def test_geometry_get(self):
        """Verify complete native and editor geometry response structures."""

        native_solid = self.native_geometry(
            self.first_product_id
        )

        native_domain = self.native_geometry(
            self.space_id
        )

        for geometry, kind in (
            (native_solid, "SOLID"),
            (native_domain, "DOMAIN")
        ):
            with self.subTest(native_kind=kind):
                self.assert_geometry_structure(
                    geometry
                )

                self.assertEqual(
                    geometry["source"],
                    "ifc"
                )

                self.assertEqual(
                    geometry["kind"],
                    kind
                )

                self.assertEqual(
                    geometry["ifcClass"],
                    "IfcExtrudedAreaSolid"
                )

                self.assertIsNone(
                    geometry["globalId"]
                )

                self.assertIs(
                    geometry["editable"],
                    False
                )

                self.assertEqual(
                    geometry["operation"],
                    "BASE"
                )

                for field in (
                    "name",
                    "profile",
                    "depth",
                    "transforms",
                    "materials"
                ):
                    self.assertNotIn(
                        field,
                        geometry
                    )

        cases = [
            ("SOLID", "ADD", "IfcProjectionElement"),
            ("SOLID", "SUBTRACT", "IfcOpeningElement"),
            ("DOMAIN", "ADD", "IfcSpatialZone")
        ]

        for kind, operation, ifc_class in cases:
            with self.subTest(
                kind=kind,
                operation=operation
            ):
                name = f"{kind} {operation}"

                geometry = self.create_geometry(
                    name=name,
                    kind=kind,
                    operation=operation,
                    profile=rectangular_profile(8, 4),
                    depth=3.5,
                    matrix=translation_matrix(2, 3, 4)
                )

                self.assert_editor_geometry(
                    geometry,
                    parent_id=self.first_product_id,
                    name=name,
                    kind=kind,
                    operation=operation,
                    profile=rectangular_profile(8, 4),
                    depth=3.5,
                    local_matrix=translation_matrix(2, 3, 4)
                )

                self.assertEqual(
                    geometry["ifcClass"],
                    ifc_class
                )

        self.assert_geometry_index_consistency()

    def test_cube_creation(self):
        """Verify creation of cubic extrusions."""

        baseline = self.client.tessellate(
            self.first_product_id,
            kind="SOLID"
        )

        created_solids = []

        for index, side in enumerate(
            (0.25, 1.0, 2.0, 10.0)
        ):
            profile = rectangular_profile(
                side,
                side
            )

            for kind in ("SOLID", "DOMAIN"):
                with self.subTest(
                    side=side,
                    kind=kind
                ):
                    name = f"Cube {kind} {index}"

                    local_matrix = translation_matrix(
                        index * 20,
                        0,
                        0
                    )

                    geometry = self.create_geometry(
                        parent_id=self.second_product_id,
                        name=name,
                        kind=kind,
                        profile=profile,
                        depth=side,
                        matrix=local_matrix
                    )

                    self.assert_editor_geometry(
                        geometry,
                        parent_id=self.second_product_id,
                        name=name,
                        kind=kind,
                        profile=profile,
                        depth=side,
                        local_matrix=local_matrix
                    )

                    if kind == "DOMAIN":
                        mesh = self.assert_domain_extrusion(
                            geometry,
                            space="local"
                        )

                        self.assertAlmostEqual(
                            mesh_volume(mesh),
                            side ** 3,
                            delta=MESH_TOLERANCE
                        )

                        self.assert_bounds(
                            mesh,
                            (
                                index * 20,
                                0,
                                0,
                                index * 20 + side,
                                side,
                                side
                            )
                        )

                        self.assert_domain_extrusion(
                            geometry,
                            space="world"
                        )

                    else:
                        created_solids.append(
                            geometry
                        )

        content = self.save_model()

        for geometry in created_solids:
            with self.subTest(
                solid=geometry["name"]
            ):
                self.assert_ifc_extrusion(
                    content,
                    geometry
                )

        self.assert_equivalent_meshes(
            baseline,
            self.client.tessellate(
                self.first_product_id,
                kind="SOLID"
            )
        )

        self.assert_geometry_index_consistency()

    def test_cuboid_creation(self):
        """Verify rectangular cuboid extrusion creation."""

        dimensions = [
            (2.0, 3.0, 4.0),
            (10.0, 2.0, 0.5),
            (0.125, 0.25, 0.75),
            (4.5, 7.25, 2.125)
        ]

        created = []

        for index, (width, length, depth) in enumerate(
            dimensions
        ):
            profile = rectangular_profile(
                width,
                length
            )

            local_matrix = translation_matrix(
                index * 20,
                5,
                2
            )

            for kind in ("SOLID", "DOMAIN"):
                with self.subTest(
                    dimensions=(width, length, depth),
                    kind=kind
                ):
                    name = f"Cuboid {kind} {index}"

                    geometry = self.create_geometry(
                        parent_id=self.second_product_id,
                        name=name,
                        kind=kind,
                        profile=profile,
                        depth=depth,
                        matrix=local_matrix
                    )

                    self.assert_editor_geometry(
                        geometry,
                        parent_id=self.second_product_id,
                        name=name,
                        kind=kind,
                        profile=profile,
                        depth=depth,
                        local_matrix=local_matrix
                    )

                    created.append(geometry)

                    if kind == "DOMAIN":
                        mesh = self.assert_domain_extrusion(
                            geometry,
                            space="local"
                        )

                        self.assertAlmostEqual(
                            mesh_volume(mesh),
                            width * length * depth,
                            delta=MESH_TOLERANCE
                        )

                        self.assert_bounds(
                            mesh,
                            (
                                index * 20,
                                5,
                                2,
                                index * 20 + width,
                                5 + length,
                                2 + depth
                            )
                        )

        content = self.save_model()

        for geometry in created:
            self.assert_ifc_extrusion(
                content,
                geometry
            )

        self.load_model(self.output_path)

        for geometry in created:
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assert_profile(
                reloaded["profile"],
                geometry["profile"]
            )

            self.assertAlmostEqual(
                reloaded["depth"],
                geometry["depth"]
            )

            if reloaded["kind"] == "DOMAIN":
                self.assert_domain_extrusion(
                    reloaded
                )

        self.assert_geometry_index_consistency()

    def test_multiple_geometries(self):
        """Verify association of multiple geometries with one transform."""

        native = self.native_geometry(
            self.first_product_id
        )

        parent_before = self.get_transform(
            self.first_product_id
        )

        geometries = []

        for index in range(5):
            geometries.append(
                self.create_geometry(
                    name=f"Multiple solid {index}",
                    kind="SOLID",
                    matrix=translation_matrix(
                        index * 10,
                        0,
                        0
                    )
                )
            )

            geometries.append(
                self.create_geometry(
                    name=f"Multiple domain {index}",
                    kind="DOMAIN",
                    matrix=translation_matrix(
                        index * 20,
                        0,
                        0
                    )
                )
            )

        expected_ids = {
            native["id"],
            *(
                geometry["id"]
                for geometry in geometries
            )
        }

        self.assert_geometry_links(
            self.first_product_id,
            expected_ids
        )

        self.assertEqual(
            len(
                self.list_geometries(
                    parent_id=self.first_product_id
                )
            ),
            11
        )

        self.assertEqual(
            len(
                self.list_geometries(
                    parent_id=self.first_product_id,
                    kind="SOLID"
                )
            ),
            6
        )

        self.assertEqual(
            len(
                self.list_geometries(
                    parent_id=self.first_product_id,
                    kind="DOMAIN"
                )
            ),
            5
        )

        parent_after = self.get_transform(
            self.first_product_id
        )

        for field in parent_before:
            if field == "geometries":
                continue

            self.assertEqual(
                parent_after[field],
                parent_before[field]
            )

        domain_meshes = [
            self.assert_domain_extrusion(
                geometry
            )
            for geometry in geometries
            if geometry["kind"] == "DOMAIN"
        ]

        combined_mesh = self.client.tessellate(
            self.first_product_id,
            kind="DOMAIN"
        )

        self.assertEqual(
            combined_mesh["indexCount"],
            sum(
                mesh["indexCount"]
                for mesh in domain_meshes
            )
        )

        combined_signature = Counter()

        for mesh in domain_meshes:
            combined_signature.update(
                mesh_signature(mesh)
            )

        self.assertEqual(
            mesh_signature(combined_mesh),
            combined_signature
        )

        self.assertAlmostEqual(
            mesh_volume(combined_mesh),
            150.0,
            delta=MESH_TOLERANCE
        )

        selected = geometries[3]

        other_geometries = {
            geometry["id"]: self.get_geometry(
                geometry["id"]
            )
            for geometry in geometries
            if geometry["id"] != selected["id"]
        }

        self.update_geometry(
            selected["id"],
            name="Independently modified geometry",
            depth=8.0
        )

        for geometry_id, before in other_geometries.items():
            self.assertEqual(
                self.get_geometry(geometry_id),
                before
            )

        self.delete_geometry(
            selected["id"]
        )

        expected_ids.remove(
            selected["id"]
        )

        self.assert_geometry_links(
            self.first_product_id,
            expected_ids
        )

        self.assert_geometry_index_consistency()

    def test_geometry_rename(self):
        """Verify renaming editable geometries without changing their identity."""

        created = []

        cases = [
            ("SOLID", "ADD"),
            ("SOLID", "SUBTRACT"),
            ("DOMAIN", "ADD")
        ]

        for index, (kind, operation) in enumerate(cases):
            with self.subTest(
                kind=kind,
                operation=operation
            ):
                geometry = self.create_geometry(
                    name=f"Original geometry {index}",
                    kind=kind,
                    operation=operation
                )

                mesh_before = None

                if kind == "DOMAIN":
                    mesh_before = self.assert_domain_extrusion(
                        geometry
                    )

                new_name = (
                    f"Renamed geometry {index} "
                    "— Géométrie — 建筑"
                )

                renamed = self.update_geometry(
                    geometry["id"],
                    name=new_name
                )

                self.assert_identity(
                    geometry,
                    renamed
                )

                self.assertEqual(
                    renamed["name"],
                    new_name
                )

                for field in geometry:
                    if field in {"name", "definition"}:
                        continue

                    self.assertEqual(
                        renamed[field],
                        geometry[field]
                    )

                if kind == "DOMAIN":
                    self.assert_equivalent_meshes(
                        mesh_before,
                        self.assert_domain_extrusion(
                            renamed
                        )
                    )

                created.append(renamed)

        self.save_and_reload()

        for geometry in created:
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assertEqual(
                reloaded["name"],
                geometry["name"]
            )

            self.assertEqual(
                reloaded["kind"],
                geometry["kind"]
            )

            self.assertEqual(
                reloaded["operation"],
                geometry["operation"]
            )

        self.assert_geometry_index_consistency()

    def test_profile_update(self):
        """Verify replacement of an editable polygonal extrusion profile."""

        profiles = [
            rectangular_profile(8, 6),
            [
                [0, 0],
                [4, 0],
                [2, 3]
            ],
            [
                [0, 0],
                [6, 0],
                [6, 2],
                [2, 2],
                [2, 5],
                [0, 5]
            ],
            [
                [-3, -2],
                [4, -2],
                [4, 3],
                [-3, 3]
            ]
        ]

        created = {
            kind: self.create_geometry(
                parent_id=self.second_product_id,
                name=f"Profile update {kind}",
                kind=kind
            )
            for kind in ("SOLID", "DOMAIN")
        }

        for profile in profiles:
            for kind, geometry in created.items():
                with self.subTest(
                    kind=kind,
                    profile=profile
                ):
                    before = self.get_geometry(
                        geometry["id"]
                    )

                    updated = self.update_geometry(
                        geometry["id"],
                        profile=profile
                    )

                    self.assert_identity(
                        before,
                        updated
                    )

                    self.assert_profile(
                        updated["profile"],
                        profile
                    )

                    for field in before:
                        if field in {
                            "profile",
                            "definition"
                        }:
                            continue

                        self.assertEqual(
                            updated[field],
                            before[field]
                        )

                    if kind == "DOMAIN":
                        mesh = self.assert_domain_extrusion(
                            updated
                        )

                        self.assertAlmostEqual(
                            mesh_volume(mesh),
                            polygon_area(profile)
                            * DEFAULT_DEPTH,
                            delta=MESH_TOLERANCE
                        )

        content = self.save_model()

        for geometry in created.values():
            updated = self.get_geometry(
                geometry["id"]
            )

            self.assert_ifc_extrusion(
                content,
                updated
            )

        self.load_model(self.output_path)

        for kind, geometry in created.items():
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assert_profile(
                reloaded["profile"],
                profiles[-1]
            )

            self.assertAlmostEqual(
                reloaded["depth"],
                DEFAULT_DEPTH
            )

            if kind == "DOMAIN":
                self.assert_domain_extrusion(
                    reloaded
                )

        self.assert_geometry_index_consistency()

    def test_depth_update(self):
        """Verify modification of extrusion depth independently of the profile."""

        profile = rectangular_profile(4, 3)

        created = {
            kind: self.create_geometry(
                parent_id=self.second_product_id,
                name=f"Depth update {kind}",
                kind=kind,
                profile=profile,
                depth=1.0
            )
            for kind in ("SOLID", "DOMAIN")
        }

        depths = [
            0.125,
            0.5,
            2.0,
            5.75,
            20.0,
            1.0
        ]

        for depth in depths:
            for kind, geometry in created.items():
                with self.subTest(
                    kind=kind,
                    depth=depth
                ):
                    before = self.get_geometry(
                        geometry["id"]
                    )

                    updated = self.update_geometry(
                        geometry["id"],
                        depth=depth
                    )

                    self.assert_identity(
                        before,
                        updated
                    )

                    self.assertAlmostEqual(
                        updated["depth"],
                        depth,
                        delta=MATRIX_TOLERANCE
                    )

                    self.assert_profile(
                        updated["profile"],
                        profile
                    )

                    for field in before:
                        if field in {
                            "depth",
                            "definition"
                        }:
                            continue

                        self.assertEqual(
                            updated[field],
                            before[field]
                        )

                    if kind == "DOMAIN":
                        mesh = self.assert_domain_extrusion(
                            updated,
                            space="local"
                        )

                        self.assert_bounds(
                            mesh,
                            (0, 0, 0, 4, 3, depth)
                        )

                        self.assertAlmostEqual(
                            mesh_volume(mesh),
                            12.0 * depth,
                            delta=MESH_TOLERANCE
                        )

        for kind, geometry in created.items():
            before = self.get_geometry(
                geometry["id"]
            )

            for invalid_depth in (
                0,
                -1,
                -0.125,
                None,
                True,
                "2.0"
            ):
                with self.subTest(
                    kind=kind,
                    invalid_depth=invalid_depth
                ):
                    self.client.expect_error(
                        "geometryUpdate",
                        {
                            "id": geometry["id"],
                            "depth": invalid_depth
                        }
                    )

                    self.assertEqual(
                        self.get_geometry(
                            geometry["id"]
                        ),
                        before
                    )

        self.save_and_reload()

        for geometry in created.values():
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assertAlmostEqual(
                reloaded["depth"],
                depths[-1]
            )

        self.assert_geometry_index_consistency()

    def test_geometry_placement_update(self):
        """Verify independent local and world placement updates."""

        self.client.request(
            "transformUpdate",
            {
                "id": self.second_product_id,
                "transform": transform_input(
                    multiply_matrices(
                        translation_matrix(100, 200, 300),
                        rotation_z(90)
                    )
                )
            }
        )

        parent_before = self.get_transform(
            self.second_product_id
        )

        domain = self.create_geometry(
            parent_id=self.second_product_id,
            name="Moved domain",
            kind="DOMAIN"
        )

        sibling = self.create_geometry(
            parent_id=self.second_product_id,
            name="Unchanged sibling",
            kind="DOMAIN",
            matrix=translation_matrix(
                20,
                30,
                40
            )
        )

        solid = self.create_geometry(
            parent_id=self.second_product_id,
            name="Moved solid",
            kind="SOLID"
        )

        sibling_before = self.get_geometry(
            sibling["id"]
        )

        sibling_mesh_before = self.assert_domain_extrusion(
            sibling
        )

        local = multiply_matrices(
            translation_matrix(2, 3, 4),
            rotation_z(45)
        )

        expected_world = multiply_matrices(
            parent_before["transforms"]["world"],
            local
        )

        for geometry in (domain, solid):
            with self.subTest(
                kind=geometry["kind"]
            ):
                updated = self.update_geometry(
                    geometry["id"],
                    transform=transform_input(
                        local
                    )
                )

                self.assert_identity(
                    geometry,
                    updated
                )

                self.assert_matrix(
                    updated["transforms"]["local"],
                    local
                )

                self.assert_matrix(
                    updated["transforms"]["world"],
                    expected_world
                )

                self.assert_profile(
                    updated["profile"],
                    geometry["profile"]
                )

                self.assertEqual(
                    updated["depth"],
                    geometry["depth"]
                )

                if updated["kind"] == "DOMAIN":
                    self.assert_domain_extrusion(
                        updated,
                        space="local"
                    )

                    self.assert_domain_extrusion(
                        updated,
                        space="world"
                    )

        requested_world = multiply_matrices(
            translation_matrix(120, 230, 340),
            rotation_z(180)
        )

        expected_local = multiply_matrices(
            translation_matrix(30, -20, 40),
            rotation_z(90)
        )

        for geometry in (domain, solid):
            updated = self.update_geometry(
                geometry["id"],
                transform=transform_input(
                    requested_world,
                    space="world"
                )
            )

            self.assert_matrix(
                updated["transforms"]["local"],
                expected_local
            )

            self.assert_matrix(
                updated["transforms"]["world"],
                requested_world
            )

        updated_domain = self.get_geometry(
            domain["id"]
        )

        self.assert_domain_extrusion(
            updated_domain
        )

        self.assertEqual(
            self.get_geometry(sibling["id"]),
            sibling_before
        )

        self.assert_equivalent_meshes(
            sibling_mesh_before,
            self.assert_domain_extrusion(
                sibling
            )
        )

        parent_after = self.get_transform(
            self.second_product_id
        )

        for field in parent_before:
            if field == "geometries":
                continue

            self.assertEqual(
                parent_after[field],
                parent_before[field]
            )

        self.save_and_reload()

        for geometry in (domain, solid):
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assert_matrix(
                reloaded["transforms"]["local"],
                expected_local
            )

            self.assert_matrix(
                reloaded["transforms"]["world"],
                requested_world
            )

        self.assert_geometry_index_consistency()

    def test_geometry_partial_update(self):
        """Verify preservation of fields omitted from geometry updates."""

        created = {
            kind: self.create_geometry(
                name=f"Partial update {kind}",
                kind=kind,
                profile=rectangular_profile(5, 4),
                depth=3.0,
                matrix=translation_matrix(
                    2,
                    3,
                    4
                )
            )
            for kind in ("SOLID", "DOMAIN")
        }

        material = self.create_material(
            name="Preserved geometry material"
        )

        self.assign_material(
            material["id"],
            created["SOLID"]["id"]
        )

        changes = [
            (
                {"name": "Updated partial name"},
                {"name"}
            ),
            (
                {"profile": rectangular_profile(8, 6)},
                {"profile"}
            ),
            (
                {"depth": 7.0},
                {"depth"}
            ),
            (
                {
                    "transform": transform_input(
                        translation_matrix(20, 30, 40)
                    )
                },
                {"transforms"}
            )
        ]

        for kind, geometry in created.items():
            for params, modified_fields in changes:
                with self.subTest(
                    kind=kind,
                    params=params
                ):
                    before = self.get_geometry(
                        geometry["id"]
                    )

                    updated = self.update_geometry(
                        geometry["id"],
                        **params
                    )

                    self.assert_identity(
                        before,
                        updated
                    )

                    for field in before:
                        if (
                            field in modified_fields
                            or field == "definition"
                        ):
                            continue

                        self.assertEqual(
                            updated[field],
                            before[field],
                            f"Unexpected modification of {field}"
                        )

                    if "name" in params:
                        self.assertEqual(
                            updated["name"],
                            params["name"]
                        )

                    if "profile" in params:
                        self.assert_profile(
                            updated["profile"],
                            params["profile"]
                        )

                    if "depth" in params:
                        self.assertAlmostEqual(
                            updated["depth"],
                            params["depth"]
                        )

                    if "transform" in params:
                        self.assert_matrix(
                            updated["transforms"]["local"],
                            params["transform"]["matrix"]
                        )

            final = self.get_geometry(
                geometry["id"]
            )

            self.assert_editor_geometry(
                final,
                parent_id=self.first_product_id,
                name="Updated partial name",
                kind=kind,
                profile=rectangular_profile(8, 6),
                depth=7.0,
                local_matrix=translation_matrix(
                    20,
                    30,
                    40
                ),
                materials=(
                    [material["id"]]
                    if kind == "SOLID"
                    else []
                )
            )

            if kind == "DOMAIN":
                self.assert_domain_extrusion(
                    final
                )

            self.client.expect_error(
                "geometryUpdate",
                {"id": geometry["id"]}
            )

            self.assertEqual(
                self.get_geometry(geometry["id"]),
                final
            )

        self.assert_geometry_index_consistency()

    def test_geometry_kind_immutable(self):
        """Verify rejection of attempts to change a geometry category."""

        solid = self.create_geometry(
            name="Immutable solid",
            kind="SOLID"
        )

        domain = self.create_geometry(
            name="Immutable domain",
            kind="DOMAIN"
        )

        initial = self.snapshot_geometries()

        cases = [
            (solid, "DOMAIN"),
            (domain, "SOLID"),
            (solid, "SOLID"),
            (domain, "DOMAIN"),
            (solid, "ALL"),
            (domain, None)
        ]

        for geometry, requested_kind in cases:
            with self.subTest(
                original_kind=geometry["kind"],
                requested_kind=requested_kind
            ):
                self.client.expect_error(
                    "geometryUpdate",
                    {
                        "id": geometry["id"],
                        "kind": requested_kind
                    }
                )

                self.client.expect_error(
                    "geometryUpdate",
                    {
                        "id": geometry["id"],
                        "kind": requested_kind,
                        "name": "Invalid category update",
                        "depth": 100.0
                    }
                )

                self.assertEqual(
                    self.snapshot_geometries(),
                    initial
                )

        converted = self.update_geometry(
            solid["id"],
            operation="SUBTRACT"
        )

        self.assertEqual(
            converted["id"],
            solid["id"]
        )

        self.assertEqual(
            converted["kind"],
            "SOLID"
        )

        self.assertEqual(
            converted["ifcClass"],
            "IfcOpeningElement"
        )

        restored = self.update_geometry(
            solid["id"],
            operation="ADD"
        )

        self.assertEqual(
            restored["id"],
            solid["id"]
        )

        self.assertEqual(
            restored["kind"],
            "SOLID"
        )

        self.assertEqual(
            restored["ifcClass"],
            "IfcProjectionElement"
        )

        self.client.expect_error(
            "geometryUpdate",
            {
                "id": domain["id"],
                "operation": "SUBTRACT"
            }
        )

        self.assertEqual(
            self.get_geometry(domain["id"]),
            domain
        )

        self.assert_geometry_index_consistency()

    def test_native_geometry_update(self):
        """Verify rejection of updates to non-editable native geometries."""

        native_solid = self.native_geometry(
            self.first_product_id
        )

        native_domain = self.native_geometry(
            self.space_id
        )

        solid_mesh_before = self.client.tessellate(
            self.first_product_id,
            kind="SOLID"
        )

        domain_mesh_before = self.client.tessellate(
            self.space_id,
            kind="DOMAIN"
        )

        invalid_updates = [
            {"name": "Invalid native name"},
            {"profile": rectangular_profile(8, 6)},
            {"depth": 5.0},
            {
                "transform": transform_input(
                    translation_matrix(10, 20, 30)
                )
            },
            {"operation": "ADD"},
            {"operation": "SUBTRACT"},
            {
                "name": "Invalid native update",
                "depth": 8.0,
                "profile": rectangular_profile(2, 3)
            }
        ]

        initial = self.snapshot_geometries()

        for geometry in (
            native_solid,
            native_domain
        ):
            self.assertIs(
                geometry["editable"],
                False
            )

            for changes in invalid_updates:
                with self.subTest(
                    kind=geometry["kind"],
                    changes=changes
                ):
                    self.client.expect_error(
                        "geometryUpdate",
                        {
                            "id": geometry["id"],
                            **changes
                        },
                        "GEOMETRY_NOT_EDITABLE"
                    )

                    self.assertEqual(
                        self.get_geometry(
                            geometry["id"]
                        ),
                        geometry
                    )

                    self.assertEqual(
                        self.snapshot_geometries(),
                        initial
                    )

        self.assert_equivalent_meshes(
            solid_mesh_before,
            self.client.tessellate(
                self.first_product_id,
                kind="SOLID"
            )
        )

        self.assert_equivalent_meshes(
            domain_mesh_before,
            self.client.tessellate(
                self.space_id,
                kind="DOMAIN"
            )
        )

        self.save_and_reload()

        self.assert_equivalent_meshes(
            solid_mesh_before,
            self.client.tessellate(
                self.first_product_id,
                kind="SOLID"
            )
        )

        self.assert_equivalent_meshes(
            domain_mesh_before,
            self.client.tessellate(
                self.space_id,
                kind="DOMAIN"
            )
        )

        self.assert_geometry_index_consistency()

    def test_invalid_geometry_parent(self):
        """Verify rejection of incompatible IFC geometry parents."""

        cases = [
            (
                self.project_id,
                "SOLID",
                "ADD",
                "GEOMETRY_OPERATION_NOT_SUPPORTED"
            ),
            (
                self.project_id,
                "SOLID",
                "SUBTRACT",
                "GEOMETRY_OPERATION_NOT_SUPPORTED"
            ),
            (
                self.space_id,
                "SOLID",
                "ADD",
                "GEOMETRY_OPERATION_NOT_SUPPORTED"
            ),
            (
                self.space_id,
                "SOLID",
                "SUBTRACT",
                "GEOMETRY_OPERATION_NOT_SUPPORTED"
            ),
            (
                self.project_id,
                "DOMAIN",
                "ADD",
                "INVALID_RELATION"
            ),
            (
                self.virtual_product_id,
                "DOMAIN",
                "ADD",
                "INVALID_RELATION"
            ),
            (
                "transform:999999999",
                "SOLID",
                "ADD",
                "ENTITY_NOT_FOUND"
            ),
            (
                "transform:999999999",
                "DOMAIN",
                "ADD",
                "ENTITY_NOT_FOUND"
            )
        ]

        native_geometry = self.native_geometry(
            self.first_product_id
        )

        material = self.create_material(
            name="Invalid geometry parent material"
        )

        cases.extend([
            (
                native_geometry["id"],
                "SOLID",
                "ADD",
                "INVALID_ENTITY_TYPE"
            ),
            (
                material["id"],
                "DOMAIN",
                "ADD",
                "INVALID_ENTITY_TYPE"
            )
        ])

        geometries_before = self.snapshot_geometries()
        transforms_before = self.snapshot_transforms()

        for index, (
            parent_id,
            kind,
            operation,
            expected_code
        ) in enumerate(cases):
            with self.subTest(
                parent_id=parent_id,
                kind=kind,
                operation=operation
            ):
                self.client.expect_error(
                    "geometryCreate",
                    self.geometry_create_params(
                        parent_id=parent_id,
                        name=f"Invalid parent geometry {index}",
                        kind=kind,
                        operation=operation
                    ),
                    expected_code
                )

                self.assertEqual(
                    self.snapshot_geometries(),
                    geometries_before
                )

                self.assertEqual(
                    self.snapshot_transforms(),
                    transforms_before
                )

        # A virtual product becomes a valid domain parent
        # after its local placement has been materialized.

        materialized = self.client.request(
            "transformUpdate",
            {
                "id": self.virtual_product_id,
                "transform": transform_input(
                    translation_matrix(10, 20, 30)
                )
            }
        )

        self.assertEqual(
            materialized["transforms"]["sourceType"],
            "IfcLocalPlacement"
        )

        valid = self.create_geometry(
            parent_id=self.virtual_product_id,
            name="Valid materialized parent domain",
            kind="DOMAIN"
        )

        self.assert_editor_geometry(
            valid,
            parent_id=self.virtual_product_id,
            name="Valid materialized parent domain",
            kind="DOMAIN",
            world_matrix=translation_matrix(
                10,
                20,
                30
            )
        )

        self.assert_domain_extrusion(valid)

        self.assert_geometry_index_consistency()

        # This regression check also detects orphaned features
        # created before parent compatibility was validated.

        content = self.save_model()

        self.assertNotIn(
            "Invalid parent geometry ",
            content,
            "Rejected geometry creation left orphaned IFC entities"
        )

    def test_invalid_geometry_kind(self):
        """Verify rejection of unknown or incorrectly typed geometry categories."""

        invalid_creation_kinds = [
            "UNKNOWN",
            "ALL",
            "BASE",
            "VISIBLE",
            "INVISIBLE",
            "solid",
            "domain",
            "Solid",
            "Domain",
            "",
            None,
            0,
            1,
            True,
            False,
            [],
            {}
        ]

        initial_geometries = self.snapshot_geometries()
        initial_transforms = self.snapshot_transforms()

        for kind in invalid_creation_kinds:
            with self.subTest(kind=kind):
                params = self.geometry_create_params(
                    name="Invalid kind geometry"
                )

                params["kind"] = kind

                self.client.expect_error(
                    "geometryCreate",
                    params
                )

                self.assertEqual(
                    self.snapshot_geometries(),
                    initial_geometries
                )

                self.assertEqual(
                    self.snapshot_transforms(),
                    initial_transforms
                )

        invalid_filter_kinds = [
            kind
            for kind in invalid_creation_kinds
            if kind != "ALL"
        ]

        for kind in invalid_filter_kinds:
            with self.subTest(filter_kind=kind):
                self.client.expect_error(
                    "geometryGetAll",
                    {
                        "transformId": None,
                        "kind": kind
                    }
                )

        self.client.expect_error(
            "geometryCreate",
            self.geometry_create_params(
                name="Invalid subtractive domain",
                kind="DOMAIN",
                operation="SUBTRACT"
            )
        )

        self.assertEqual(
            self.snapshot_geometries(),
            initial_geometries
        )

        self.assertEqual(
            self.snapshot_transforms(),
            initial_transforms
        )

        solid = self.create_geometry(
            name="Valid solid recovery",
            kind="SOLID"
        )

        domain = self.create_geometry(
            name="Valid domain recovery",
            kind="DOMAIN"
        )

        self.assertEqual(
            solid["kind"],
            "SOLID"
        )

        self.assertEqual(
            domain["kind"],
            "DOMAIN"
        )

        self.assert_domain_extrusion(domain)

        self.assert_geometry_index_consistency()

    def test_geometry_persistence(self):
        """Verify parametric geometry persistence through IFC roundtrips."""

        specifications = [
            {
                "name": "Persistent solid",
                "kind": "SOLID",
                "operation": "ADD",
                "profile": rectangular_profile(8, 6),
                "depth": 4.5,
                "matrix": translation_matrix(2, 3, 4)
            },
            {
                "name": "Persistent opening",
                "kind": "SOLID",
                "operation": "SUBTRACT",
                "profile": rectangular_profile(2, 1),
                "depth": 3.0,
                "matrix": translation_matrix(0.5, 0.25, -1)
            },
            {
                "name": "Persistent rectangular domain",
                "kind": "DOMAIN",
                "operation": "ADD",
                "profile": rectangular_profile(10, 8),
                "depth": 6.0,
                "matrix": multiply_matrices(
                    translation_matrix(20, 30, 40),
                    rotation_z(35)
                )
            },
            {
                "name": "Persistent concave domain",
                "kind": "DOMAIN",
                "operation": "ADD",
                "profile": [
                    [0, 0],
                    [6, 0],
                    [6, 2],
                    [2, 2],
                    [2, 5],
                    [0, 5]
                ],
                "depth": 2.75,
                "matrix": multiply_matrices(
                    translation_matrix(-20, 10, 5),
                    rotation_z(-25)
                )
            }
        ]

        created = [
            self.create_geometry(
                parent_id=self.first_product_id,
                **specification
            )
            for specification in specifications
        ]

        second_domain = self.create_geometry(
            parent_id=self.second_product_id,
            name="Persistent second product domain",
            kind="DOMAIN",
            profile=rectangular_profile(3, 7),
            depth=5.0,
            matrix=translation_matrix(2, 4, 6)
        )

        created.append(second_domain)

        material = self.create_material(
            name="Persistent extrusion material"
        )

        for geometry in created:
            if (
                geometry["kind"] == "SOLID"
                and geometry["operation"] == "ADD"
            ):
                self.assign_material(
                    material["id"],
                    geometry["id"]
                )

        original = {
            geometry["globalId"]: self.get_geometry(
                geometry["id"]
            )
            for geometry in created
        }

        parent_global_ids = {
            geometry["globalId"]: self.get_transform(
                geometry["parentId"]
            )["globalId"]
            for geometry in original.values()
        }

        original_world_meshes = {
            geometry["globalId"]: self.assert_domain_extrusion(
                geometry,
                space="world"
            )
            for geometry in original.values()
            if geometry["kind"] == "DOMAIN"
        }

        original_local_meshes = {
            geometry["globalId"]: self.assert_domain_extrusion(
                geometry,
                space="local"
            )
            for geometry in original.values()
            if geometry["kind"] == "DOMAIN"
        }

        expected_geometry_count = len(
            self.list_geometries()
        )

        expected_transform_count = len(
            self.list_transforms()
        )

        for iteration in range(5):
            with self.subTest(roundtrip=iteration):
                content = self.save_model()

                for global_id in original:
                    current = self.find_geometry(
                        global_id
                    )

                    self.assert_ifc_extrusion(
                        content,
                        current
                    )

                result = self.load_model(
                    self.output_path
                )

                self.assertEqual(
                    result["geometryCount"],
                    expected_geometry_count
                )

                self.assertEqual(
                    result["transformCount"],
                    expected_transform_count
                )

                reloaded_material = self.find_material(
                    material["name"]
                )

                for global_id, before in original.items():
                    reloaded = self.find_geometry(
                        global_id
                    )

                    expected_parent = self.find_transform(
                        parent_global_ids[global_id]
                    )

                    expected_materials = (
                        [reloaded_material["id"]]
                        if before["materials"]
                        else []
                    )

                    self.assert_editor_geometry(
                        reloaded,
                        parent_id=expected_parent["id"],
                        name=before["name"],
                        kind=before["kind"],
                        operation=before["operation"],
                        profile=before["profile"],
                        depth=before["depth"],
                        local_matrix=before["transforms"]["local"],
                        world_matrix=before["transforms"]["world"],
                        materials=expected_materials
                    )

                    self.assertEqual(
                        reloaded["globalId"],
                        global_id
                    )

                    if reloaded["kind"] == "DOMAIN":
                        world_mesh = self.assert_domain_extrusion(
                            reloaded,
                            space="world"
                        )

                        local_mesh = self.assert_domain_extrusion(
                            reloaded,
                            space="local"
                        )

                        self.assert_equivalent_meshes(
                            world_mesh,
                            original_world_meshes[global_id]
                        )

                        self.assert_equivalent_meshes(
                            local_mesh,
                            original_local_meshes[global_id]
                        )

                self.assert_geometry_index_consistency()

        persistent_domain = self.find_geometry(
            created[2]["globalId"]
        )

        updated_profile = rectangular_profile(
            12,
            9
        )

        updated_matrix = translation_matrix(
            50,
            60,
            70
        )

        updated = self.update_geometry(
            persistent_domain["id"],
            name="Updated persistent domain",
            profile=updated_profile,
            depth=8.0,
            transform=transform_input(
                updated_matrix
            )
        )

        self.assert_editor_geometry(
            updated,
            parent_id=self.first_product_id,
            name="Updated persistent domain",
            kind="DOMAIN",
            profile=updated_profile,
            depth=8.0,
            local_matrix=updated_matrix
        )

        updated_mesh = self.assert_domain_extrusion(
            updated
        )

        self.assertAlmostEqual(
            mesh_volume(updated_mesh),
            864.0,
            delta=MESH_TOLERANCE
        )

        self.save_and_reload()

        final = self.find_geometry(
            persistent_domain["globalId"]
        )

        self.assert_editor_geometry(
            final,
            parent_id=self.first_product_id,
            name="Updated persistent domain",
            kind="DOMAIN",
            profile=updated_profile,
            depth=8.0,
            local_matrix=updated_matrix
        )

        self.assert_equivalent_meshes(
            updated_mesh,
            self.assert_domain_extrusion(final)
        )

        self.assert_geometry_index_consistency()


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