import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from pathlib import Path


# Standalone integration suite.
# Expected results must be validated against the compiled CLI.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30
MATRIX_TOLERANCE = 1e-7
MESH_TOLERANCE = 1e-6

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

PHYSICAL_GLOBAL_ID = "0000000000000000000001"
PROJECT_GLOBAL_ID = "0000000000000000000002"

DEFAULT_PROFILE = [
    [0, 0],
    [5, 0],
    [5, 3],
    [0, 3]
]

DEFAULT_DEPTH = 2.0

PHYSICAL_CORNERS = [
    (x, y, z)
    for x in (-1.0, 1.0)
    for y in (-0.5, 0.5)
    for z in (0.0, 1.0)
]

DOMAIN_CORNERS = [
    (x, y, z)
    for x in (0.0, 5.0)
    for y in (0.0, 3.0)
    for z in (0.0, 2.0)
]

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('placements-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCDIRECTION((0.,0.,1.));
#3=IFCDIRECTION((1.,0.,0.));
#4=IFCLOCALPLACEMENT($,#5);
#5=IFCAXIS2PLACEMENT3D(#1,#2,#3);
#6=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-5,#5,$);
#7=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#8=IFCUNITASSIGNMENT((#7));
#9=IFCCARTESIANPOINT((0.,0.));
#10=IFCAXIS2PLACEMENT2D(#9,$);
#11=IFCRECTANGLEPROFILEDEF(.AREA.,$,#10,2.,1.);
#12=IFCEXTRUDEDAREASOLID(#11,#5,#2,1.);
#13=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#12));
#14=IFCPRODUCTDEFINITIONSHAPE($,$,(#13));
#15=IFCBUILDINGELEMENTPROXY('0000000000000000000001',$,'Physical solid',$,$,#4,#14,$,.NOTDEFINED.);
#16=IFCPROJECT('0000000000000000000002',$,'Placement test project',$,$,$,$,(#6),#8);
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


def rotation_x(angle):
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)

    return [
        1, 0, 0, 0,
        0, cosine, -sine, 0,
        0, sine, cosine, 0,
        0, 0, 0, 1
    ]


def rotation_y(angle):
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)

    return [
        cosine, 0, sine, 0,
        0, 1, 0, 0,
        -sine, 0, cosine, 0,
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


def scaling_matrix(x=1, y=1, z=1):
    return [
        x, 0, 0, 0,
        0, y, 0, 0,
        0, 0, z, 0,
        0, 0, 0, 1
    ]


def shear_matrix(xy=0, xz=0, yx=0, yz=0, zx=0, zy=0):
    return [
        1, xy, xz, 0,
        yx, 1, yz, 0,
        zx, zy, 1, 0,
        0, 0, 0, 1
    ]


def multiply_matrices(left, right):
    if len(left) != 16 or len(right) != 16:
        raise ValueError("Expected two 4x4 matrices")

    return [
        sum(
            left[row * 4 + index] * right[index * 4 + column]
            for index in range(4)
        )
        for row in range(4)
        for column in range(4)
    ]


def compose_matrices(*matrices):
    result = identity_matrix()

    for matrix in matrices:
        result = multiply_matrices(result, matrix)

    return result


def rigid_inverse(matrix):
    result = identity_matrix()

    for row in range(3):
        for column in range(3):
            result[row * 4 + column] = matrix[column * 4 + row]

    translation = (
        matrix[3],
        matrix[7],
        matrix[11]
    )

    for row in range(3):
        result[row * 4 + 3] = -sum(
            result[row * 4 + column] * translation[column]
            for column in range(3)
        )

    return result


def transform_point(matrix, point):
    vector = (
        point[0],
        point[1],
        point[2],
        1.0
    )

    return tuple(
        sum(
            matrix[row * 4 + column] * vector[column]
            for column in range(4)
        )
        for row in range(3)
    )


def transform_direction(matrix, direction):
    return tuple(
        sum(
            matrix[row * 4 + column] * direction[column]
            for column in range(3)
        )
        for row in range(3)
    )


def determinant(matrix):
    a, b, c = matrix[0:3]
    d, e, f = matrix[4:7]
    g, h, i = matrix[8:11]

    return (
        a * (e * i - f * h)
        - b * (d * i - f * g)
        + c * (d * h - e * g)
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

    def expect_error(self, command, params, expected_code):
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

        if begin is None:
            raise AssertionError("Missing mesh.begin event")

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


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-placements-"
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

        self.physical_id = self.find_transform(
            PHYSICAL_GLOBAL_ID
        )["id"]

        self.project_id = self.find_transform(
            PROJECT_GLOBAL_ID
        )["id"]

        return result

    def save_and_reload(self):
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

        return self.load_model(self.output_path)

    def list_transforms(self):
        return self.client.request("transformGetAll")

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

    def list_geometries(self, parent_id=None, kind=None):
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

    def transform_create_params(
        self,
        name="Placement transform",
        matrix=None,
        space="parent",
        parent_id=None
    ):
        return {
            "name": name,
            "parents": parents_input(
                placement=parent_id
            ),
            "transform": transform_input(
                matrix,
                space
            ),
            "properties": []
        }

    def create_transform(
        self,
        name="Placement transform",
        matrix=None,
        space="parent",
        parent_id=None
    ):
        return self.client.request(
            "transformCreate",
            self.transform_create_params(
                name=name,
                matrix=matrix,
                space=space,
                parent_id=parent_id
            )
        )

    def update_transform(
        self,
        identifier,
        matrix,
        space="parent"
    ):
        return self.client.request(
            "transformUpdate",
            {
                "id": identifier,
                "transform": transform_input(
                    matrix,
                    space
                )
            }
        )

    def geometry_create_params(
        self,
        parent_id,
        name="Placement domain",
        matrix=None,
        space="parent"
    ):
        return {
            "parentId": parent_id,
            "name": name,
            "kind": "DOMAIN",
            "transform": transform_input(
                matrix,
                space
            ),
            "profile": DEFAULT_PROFILE,
            "depth": DEFAULT_DEPTH,
            "operation": "ADD"
        }

    def create_domain(
        self,
        parent_id,
        name="Placement domain",
        matrix=None,
        space="parent"
    ):
        return self.client.request(
            "geometryCreate",
            self.geometry_create_params(
                parent_id=parent_id,
                name=name,
                matrix=matrix,
                space=space
            )
        )

    def update_geometry(
        self,
        identifier,
        matrix,
        space="parent"
    ):
        return self.client.request(
            "geometryUpdate",
            {
                "id": identifier,
                "transform": transform_input(
                    matrix,
                    space
                )
            }
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

            self.assertTrue(math.isfinite(value))

            self.assertAlmostEqual(
                value,
                reference,
                delta=tolerance,
                msg=(
                    f"Invalid matrix component [{index // 4}, "
                    f"{index % 4}]: {value} != {reference}"
                )
            )

    def assert_vector(
        self,
        actual,
        expected,
        tolerance=MESH_TOLERANCE
    ):
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
                msg=f"Invalid vector component {index}"
            )

    def assert_rigid_matrix(self, matrix):
        self.assertEqual(len(matrix), 16)

        self.assert_vector(
            matrix[12:16],
            [0, 0, 0, 1],
            tolerance=MATRIX_TOLERANCE
        )

        axes = [
            tuple(
                matrix[row * 4 + column]
                for row in range(3)
            )
            for column in range(3)
        ]

        for axis in axes:
            length = math.sqrt(
                sum(value * value for value in axis)
            )

            self.assertAlmostEqual(
                length,
                1.0,
                delta=MATRIX_TOLERANCE
            )

        for first in range(3):
            for second in range(first + 1, 3):
                dot = sum(
                    axes[first][index] * axes[second][index]
                    for index in range(3)
                )

                self.assertAlmostEqual(
                    dot,
                    0.0,
                    delta=MATRIX_TOLERANCE
                )

        self.assertAlmostEqual(
            determinant(matrix),
            1.0,
            delta=MATRIX_TOLERANCE
        )

        self.assert_matrix(
            multiply_matrices(
                matrix,
                rigid_inverse(matrix)
            ),
            identity_matrix()
        )

    def assert_placement(
        self,
        entity,
        expected_local,
        expected_world
    ):
        placement = entity["transforms"]

        self.assertEqual(
            placement["sourceType"],
            "IfcLocalPlacement"
        )

        self.assertIs(
            placement["editable"],
            True
        )

        self.assert_matrix(
            placement["local"],
            expected_local
        )

        self.assert_matrix(
            placement["world"],
            expected_world
        )

        self.assert_rigid_matrix(
            placement["local"]
        )

        self.assert_rigid_matrix(
            placement["world"]
        )

    def assert_identity(self, before, after):
        for field in (
            "id",
            "ifcEntityId",
            "globalId"
        ):
            self.assertEqual(
                before[field],
                after[field],
                f"Entity identity changed: {field}"
            )

    def assert_bounds(
        self,
        mesh,
        expected,
        tolerance=MESH_TOLERANCE
    ):
        actual = mesh_bounds(mesh)

        self.assert_vector(
            actual,
            expected,
            tolerance=tolerance
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

    def assert_box_mesh(
        self,
        mesh,
        corners,
        matrix,
        expected_volume
    ):
        expected_points = [
            transform_point(matrix, point)
            for point in corners
        ]

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

        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected_volume,
            delta=MESH_TOLERANCE
        )

    def assert_normal_directions(
        self,
        mesh,
        matrix
    ):
        source_normals = [
            (1, 0, 0),
            (-1, 0, 0),
            (0, 1, 0),
            (0, -1, 0),
            (0, 0, 1),
            (0, 0, -1)
        ]

        expected_normals = [
            transform_direction(matrix, normal)
            for normal in source_normals
        ]

        normals = [
            tuple(mesh["normals"][offset:offset + 3])
            for offset in range(
                0,
                len(mesh["normals"]),
                3
            )
        ]

        self.assertTrue(normals)

        self.assertEqual(
            len(normals),
            mesh["vertexCount"]
        )

        for normal in normals:
            length = math.sqrt(
                sum(value * value for value in normal)
            )

            self.assertAlmostEqual(
                length,
                1.0,
                delta=MESH_TOLERANCE
            )

        self.assert_point_sets(
            normals,
            expected_normals
        )

    def assert_domain_mesh(
        self,
        parent_id,
        geometry_id,
        expected_matrix,
        space="world"
    ):
        mesh = self.client.tessellate(
            parent_id,
            kind="DOMAIN",
            space=space,
            geometry_id=geometry_id
        )

        self.assert_box_mesh(
            mesh,
            DOMAIN_CORNERS,
            expected_matrix,
            expected_volume=30.0
        )

        return mesh

    def assert_physical_mesh(
        self,
        expected_matrix,
        space="world"
    ):
        mesh = self.client.tessellate(
            self.physical_id,
            kind="SOLID",
            space=space
        )

        self.assert_box_mesh(
            mesh,
            PHYSICAL_CORNERS,
            expected_matrix,
            expected_volume=2.0
        )

        return mesh

    def assert_invalid_placements(
        self,
        matrices,
        expected_code
    ):
        domain = self.create_domain(
            self.physical_id,
            name="Invalid placement validation domain"
        )

        transform_before = self.get_transform(
            self.physical_id
        )

        geometry_before = self.get_geometry(
            domain["id"]
        )

        transforms_before = self.list_transforms()
        geometries_before = self.list_geometries()

        for label, matrix in matrices:
            with self.subTest(matrix=label):
                commands = [
                    (
                        "transformCreate",
                        self.transform_create_params(
                            name="Invalid transform",
                            matrix=matrix
                        )
                    ),
                    (
                        "transformUpdate",
                        {
                            "id": self.physical_id,
                            "transform": {
                                "space": "parent",
                                "matrix": matrix
                            }
                        }
                    ),
                    (
                        "geometryCreate",
                        {
                            **self.geometry_create_params(
                                self.physical_id
                            ),
                            "transform": {
                                "space": "parent",
                                "matrix": matrix
                            }
                        }
                    ),
                    (
                        "geometryUpdate",
                        {
                            "id": domain["id"],
                            "transform": {
                                "space": "parent",
                                "matrix": matrix
                            }
                        }
                    )
                ]

                for command, params in commands:
                    with self.subTest(command=command):
                        if command == "transformCreate":
                            params["transform"]["matrix"] = matrix

                        self.client.expect_error(
                            command,
                            params,
                            expected_code
                        )

                        self.assertEqual(
                            self.list_transforms(),
                            transforms_before
                        )

                        self.assertEqual(
                            self.list_geometries(),
                            geometries_before
                        )

                        self.assertEqual(
                            self.get_transform(self.physical_id),
                            transform_before
                        )

                        self.assertEqual(
                            self.get_geometry(domain["id"]),
                            geometry_before
                        )

        self.assert_physical_mesh(
            identity_matrix()
        )

        self.assert_domain_mesh(
            self.physical_id,
            domain["id"],
            identity_matrix()
        )

        valid = self.update_transform(
            self.physical_id,
            translation_matrix(1, 2, 3)
        )

        self.assert_placement(
            valid,
            translation_matrix(1, 2, 3),
            translation_matrix(1, 2, 3)
        )

    def test_identity_matrix(self):
        """Verify creation of an identity placement."""

        created = self.create_transform(
            name="Identity transform",
            matrix=identity_matrix()
        )

        self.assert_placement(
            created,
            identity_matrix(),
            identity_matrix()
        )

        self.assertEqual(
            created["parents"],
            parents_input()
        )

        retrieved = self.get_transform(
            created["id"]
        )

        self.assertEqual(
            retrieved,
            created
        )

        domain = self.create_domain(
            created["id"],
            matrix=identity_matrix()
        )

        self.assert_placement(
            domain,
            identity_matrix(),
            identity_matrix()
        )

        for space in ("local", "world"):
            with self.subTest(space=space):
                self.assert_domain_mesh(
                    created["id"],
                    domain["id"],
                    identity_matrix(),
                    space=space
                )

        self.assert_physical_mesh(
            identity_matrix()
        )

        self.save_and_reload()

        reloaded = self.find_transform(
            created["globalId"]
        )

        reloaded_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assert_placement(
            reloaded,
            identity_matrix(),
            identity_matrix()
        )

        self.assert_placement(
            reloaded_domain,
            identity_matrix(),
            identity_matrix()
        )

        self.assertEqual(
            reloaded_domain["parentId"],
            reloaded["id"]
        )

    def test_translation(self):
        """Verify translations along all three axes."""

        cases = [
            (10, 0, 0),
            (0, 20, 0),
            (0, 0, 30),
            (10, 20, 30),
            (-10, -20, -30),
            (0.125, -2.75, 3.5),
            (0, 0, 0)
        ]

        original = self.get_transform(
            self.physical_id
        )

        domain = self.create_domain(
            self.physical_id
        )

        for x, y, z in cases:
            with self.subTest(x=x, y=y, z=z):
                expected = translation_matrix(
                    x,
                    y,
                    z
                )

                updated = self.update_transform(
                    self.physical_id,
                    expected
                )

                self.assert_identity(
                    original,
                    updated
                )

                self.assert_placement(
                    updated,
                    expected,
                    expected
                )

                self.assert_physical_mesh(
                    expected
                )

                self.assert_physical_mesh(
                    identity_matrix(),
                    space="local"
                )

                self.assert_domain_mesh(
                    self.physical_id,
                    domain["id"],
                    expected
                )

                self.assert_domain_mesh(
                    self.physical_id,
                    domain["id"],
                    identity_matrix(),
                    space="local"
                )

                retrieved_domain = self.get_geometry(
                    domain["id"]
                )

                self.assert_placement(
                    retrieved_domain,
                    identity_matrix(),
                    expected
                )

    def test_rotation_x(self):
        """Verify rotations around the X axis."""

        domain = self.create_domain(
            self.physical_id
        )

        for angle in (
            0,
            30,
            90,
            -90,
            180,
            270,
            360
        ):
            with self.subTest(angle=angle):
                expected = rotation_x(angle)

                updated = self.update_transform(
                    self.physical_id,
                    expected
                )

                self.assert_placement(
                    updated,
                    expected,
                    expected
                )

                physical_mesh = self.assert_physical_mesh(
                    expected
                )

                domain_mesh = self.assert_domain_mesh(
                    self.physical_id,
                    domain["id"],
                    expected
                )

                self.assert_normal_directions(
                    physical_mesh,
                    expected
                )

                self.assert_normal_directions(
                    domain_mesh,
                    expected
                )

                self.assert_physical_mesh(
                    identity_matrix(),
                    space="local"
                )

        self.update_transform(
            self.physical_id,
            rotation_x(90)
        )

        self.assert_bounds(
            self.client.tessellate(
                self.physical_id
            ),
            (-1, -1, -0.5, 1, 0, 0.5)
        )

    def test_rotation_y(self):
        """Verify rotations around the Y axis."""

        domain = self.create_domain(
            self.physical_id
        )

        for angle in (
            0,
            45,
            90,
            -90,
            180,
            270,
            360
        ):
            with self.subTest(angle=angle):
                expected = rotation_y(angle)

                updated = self.update_transform(
                    self.physical_id,
                    expected
                )

                self.assert_placement(
                    updated,
                    expected,
                    expected
                )

                physical_mesh = self.assert_physical_mesh(
                    expected
                )

                domain_mesh = self.assert_domain_mesh(
                    self.physical_id,
                    domain["id"],
                    expected
                )

                self.assert_normal_directions(
                    physical_mesh,
                    expected
                )

                self.assert_normal_directions(
                    domain_mesh,
                    expected
                )

                self.assert_physical_mesh(
                    identity_matrix(),
                    space="local"
                )

        self.update_transform(
            self.physical_id,
            rotation_y(90)
        )

        self.assert_bounds(
            self.client.tessellate(
                self.physical_id
            ),
            (0, -0.5, -1, 1, 0.5, 1)
        )

    def test_rotation_z(self):
        """Verify rotations around the Z axis."""

        domain = self.create_domain(
            self.physical_id
        )

        for angle in (
            0,
            15,
            45,
            90,
            -90,
            180,
            270,
            360
        ):
            with self.subTest(angle=angle):
                expected = rotation_z(angle)

                updated = self.update_transform(
                    self.physical_id,
                    expected
                )

                self.assert_placement(
                    updated,
                    expected,
                    expected
                )

                physical_mesh = self.assert_physical_mesh(
                    expected
                )

                domain_mesh = self.assert_domain_mesh(
                    self.physical_id,
                    domain["id"],
                    expected
                )

                self.assert_normal_directions(
                    physical_mesh,
                    expected
                )

                self.assert_normal_directions(
                    domain_mesh,
                    expected
                )

                self.assert_physical_mesh(
                    identity_matrix(),
                    space="local"
                )

        self.update_transform(
            self.physical_id,
            rotation_z(90)
        )

        self.assert_bounds(
            self.client.tessellate(
                self.physical_id
            ),
            (-0.5, -1, 0, 0.5, 1, 1)
        )

    def test_combined_transform(self):
        """Verify combined rotation and translation matrices."""

        combined = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(35),
            rotation_y(25),
            rotation_x(-15)
        )

        updated = self.update_transform(
            self.physical_id,
            combined
        )

        self.assert_placement(
            updated,
            combined,
            combined
        )

        physical_mesh = self.assert_physical_mesh(
            combined
        )

        self.assert_normal_directions(
            physical_mesh,
            combined
        )

        self.assert_physical_mesh(
            identity_matrix(),
            space="local"
        )

        geometry_local = compose_matrices(
            translation_matrix(2, 3, 4),
            rotation_x(45),
            rotation_z(-20)
        )

        geometry_world = multiply_matrices(
            combined,
            geometry_local
        )

        domain = self.create_domain(
            self.physical_id,
            matrix=geometry_local
        )

        self.assert_placement(
            domain,
            geometry_local,
            geometry_world
        )

        self.assert_domain_mesh(
            self.physical_id,
            domain["id"],
            geometry_world
        )

        local_mesh = self.assert_domain_mesh(
            self.physical_id,
            domain["id"],
            geometry_local,
            space="local"
        )

        self.assert_normal_directions(
            local_mesh,
            geometry_local
        )

        self.save_and_reload()

        reloaded = self.get_transform(
            self.physical_id
        )

        reloaded_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assert_placement(
            reloaded,
            combined,
            combined
        )

        self.assert_placement(
            reloaded_domain,
            geometry_local,
            geometry_world
        )

        self.assert_physical_mesh(
            combined
        )

        self.assert_domain_mesh(
            self.physical_id,
            reloaded_domain["id"],
            geometry_world
        )

    def test_matrix_convention(self):
        """Verify row-major matrices using column-vector multiplication."""

        expected = [
            0, -1, 0, 10,
            1, 0, 0, 20,
            0, 0, 1, 30,
            0, 0, 0, 1
        ]

        updated = self.update_transform(
            self.physical_id,
            expected
        )

        self.assert_placement(
            updated,
            expected,
            expected
        )

        self.assert_vector(
            transform_point(
                expected,
                (0, 0, 0)
            ),
            (10, 20, 30)
        )

        self.assert_vector(
            transform_point(
                expected,
                (1, 0, 0)
            ),
            (10, 21, 30)
        )

        self.assert_vector(
            transform_point(
                expected,
                (0, 1, 0)
            ),
            (9, 20, 30)
        )

        self.assert_vector(
            transform_point(
                expected,
                (0, 0, 1)
            ),
            (10, 20, 31)
        )

        domain = self.create_domain(
            self.physical_id
        )

        mesh = self.client.tessellate(
            self.physical_id,
            kind="DOMAIN",
            geometry_id=domain["id"]
        )

        expected_points = [
            (10, 20, 30),
            (10, 20, 32),
            (10, 25, 30),
            (10, 25, 32),
            (7, 20, 30),
            (7, 20, 32),
            (7, 25, 30),
            (7, 25, 32)
        ]

        self.assert_point_sets(
            mesh_points(mesh),
            expected_points
        )

        self.assert_bounds(
            mesh,
            (7, 20, 30, 10, 25, 32)
        )

        self.assert_bounds(
            self.client.tessellate(
                self.physical_id,
                kind="SOLID"
            ),
            (9.5, 19, 30, 10.5, 21, 31)
        )

        transposed = [
            expected[column * 4 + row]
            for row in range(4)
            for column in range(4)
        ]

        self.client.expect_error(
            "transformUpdate",
            {
                "id": self.physical_id,
                "transform": transform_input(
                    transposed
                )
            },
            "INVALID_PLACEMENT"
        )

        self.assert_placement(
            self.get_transform(self.physical_id),
            expected,
            expected
        )

    def test_world_to_local(self):
        """Verify conversion of world-space input into parent-relative placement."""

        parent_world = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(90)
        )

        parent = self.create_transform(
            name="World-to-local parent",
            matrix=parent_world
        )

        requested_world = compose_matrices(
            translation_matrix(12, 23, 34),
            rotation_z(180)
        )

        expected_local = compose_matrices(
            translation_matrix(3, -2, 4),
            rotation_z(90)
        )

        child = self.create_transform(
            name="World-space child",
            parent_id=parent["id"],
            matrix=requested_world,
            space="world"
        )

        self.assert_placement(
            child,
            expected_local,
            requested_world
        )

        self.assertEqual(
            child["parents"]["placement"],
            parent["id"]
        )

        second_world = compose_matrices(
            translation_matrix(-5, 15, 25),
            rotation_y(30),
            rotation_x(45)
        )

        second_local = multiply_matrices(
            rigid_inverse(parent_world),
            second_world
        )

        updated = self.update_transform(
            child["id"],
            second_world,
            space="world"
        )

        self.assert_placement(
            updated,
            second_local,
            second_world
        )

        domain_world = compose_matrices(
            translation_matrix(100, 200, 300),
            rotation_x(25),
            rotation_z(35)
        )

        domain_local = multiply_matrices(
            rigid_inverse(second_world),
            domain_world
        )

        domain = self.create_domain(
            child["id"],
            matrix=domain_world,
            space="world"
        )

        self.assert_placement(
            domain,
            domain_local,
            domain_world
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            domain_world
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            domain_local,
            space="local"
        )

        updated_domain_world = compose_matrices(
            translation_matrix(-100, -200, 50),
            rotation_y(60)
        )

        updated_domain_local = multiply_matrices(
            rigid_inverse(second_world),
            updated_domain_world
        )

        updated_domain = self.update_geometry(
            domain["id"],
            updated_domain_world,
            space="world"
        )

        self.assert_identity(
            domain,
            updated_domain
        )

        self.assert_placement(
            updated_domain,
            updated_domain_local,
            updated_domain_world
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            updated_domain_world
        )

    def test_local_to_world(self):
        """Verify world placement calculation from a local hierarchy."""

        parent_matrix = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(90)
        )

        child_matrix = compose_matrices(
            translation_matrix(2, 3, 4),
            rotation_x(90)
        )

        expected_world = [
            0, 0, 1, 7,
            1, 0, 0, 22,
            0, 1, 0, 34,
            0, 0, 0, 1
        ]

        parent = self.create_transform(
            name="Local-to-world parent",
            matrix=parent_matrix
        )

        child = self.create_transform(
            name="Local-to-world child",
            parent_id=parent["id"],
            matrix=child_matrix
        )

        self.assert_placement(
            parent,
            parent_matrix,
            parent_matrix
        )

        self.assert_placement(
            child,
            child_matrix,
            expected_world
        )

        domain_local = translation_matrix(
            5,
            6,
            7
        )

        domain_world = multiply_matrices(
            expected_world,
            domain_local
        )

        domain = self.create_domain(
            child["id"],
            matrix=domain_local
        )

        self.assert_placement(
            domain,
            domain_local,
            domain_world
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            domain_world
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            domain_local,
            space="local"
        )

        root_local_mesh = self.client.tessellate(
            parent["id"],
            kind="DOMAIN",
            space="local",
            include_children=True
        )

        expected_root_local = multiply_matrices(
            child_matrix,
            domain_local
        )

        self.assert_box_mesh(
            root_local_mesh,
            DOMAIN_CORNERS,
            expected_root_local,
            expected_volume=30.0
        )

    def test_nested_rotations(self):
        """Verify composition of nested translations and rotations."""

        root_local = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(35)
        )

        child_local = compose_matrices(
            translation_matrix(2, 3, 4),
            rotation_y(-45)
        )

        grandchild_local = compose_matrices(
            translation_matrix(-5, 6, 7),
            rotation_x(60)
        )

        leaf_local = compose_matrices(
            translation_matrix(1, -2, 3),
            rotation_z(-25),
            rotation_y(15)
        )

        root = self.create_transform(
            name="Nested root",
            matrix=root_local
        )

        child = self.create_transform(
            name="Nested child",
            parent_id=root["id"],
            matrix=child_local
        )

        grandchild = self.create_transform(
            name="Nested grandchild",
            parent_id=child["id"],
            matrix=grandchild_local
        )

        leaf = self.create_transform(
            name="Nested leaf",
            parent_id=grandchild["id"],
            matrix=leaf_local
        )

        chain = [
            (root, root_local),
            (child, child_local),
            (grandchild, grandchild_local),
            (leaf, leaf_local)
        ]

        expected_world = identity_matrix()

        for entity, local in chain:
            expected_world = multiply_matrices(
                expected_world,
                local
            )

            with self.subTest(identifier=entity["id"]):
                self.assert_placement(
                    self.get_transform(entity["id"]),
                    local,
                    expected_world
                )

        leaf_world = expected_world

        geometry_local = compose_matrices(
            translation_matrix(0.5, 1.5, -2.5),
            rotation_x(20),
            rotation_y(40)
        )

        geometry_world = multiply_matrices(
            leaf_world,
            geometry_local
        )

        domain = self.create_domain(
            leaf["id"],
            matrix=geometry_local
        )

        self.assert_placement(
            domain,
            geometry_local,
            geometry_world
        )

        self.assert_domain_mesh(
            leaf["id"],
            domain["id"],
            geometry_world
        )

        root_mesh = self.client.tessellate(
            root["id"],
            kind="DOMAIN",
            space="local",
            include_children=True
        )

        expected_root_local = compose_matrices(
            child_local,
            grandchild_local,
            leaf_local,
            geometry_local
        )

        self.assert_box_mesh(
            root_mesh,
            DOMAIN_CORNERS,
            expected_root_local,
            expected_volume=30.0
        )

        self.save_and_reload()

        expected_world = identity_matrix()

        for entity, local in chain:
            expected_world = multiply_matrices(
                expected_world,
                local
            )

            reloaded = self.find_transform(
                entity["globalId"]
            )

            self.assert_placement(
                reloaded,
                local,
                expected_world
            )

        reloaded_leaf = self.find_transform(
            leaf["globalId"]
        )

        reloaded_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assert_domain_mesh(
            reloaded_leaf["id"],
            reloaded_domain["id"],
            geometry_world
        )

    def test_parent_movement(self):
        """Verify propagation of parent movement to children and geometries."""

        parent_initial = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(20)
        )

        child_local = compose_matrices(
            translation_matrix(2, 3, 4),
            rotation_x(30)
        )

        grandchild_local = compose_matrices(
            translation_matrix(5, 6, 7),
            rotation_y(45)
        )

        parent = self.create_transform(
            name="Moving parent",
            matrix=parent_initial
        )

        child = self.create_transform(
            name="Moving child",
            matrix=child_local,
            parent_id=parent["id"]
        )

        grandchild = self.create_transform(
            name="Moving grandchild",
            matrix=grandchild_local,
            parent_id=child["id"]
        )

        physical_local = compose_matrices(
            translation_matrix(-2, 4, 6),
            rotation_z(-15)
        )

        self.client.request(
            "transformUpdate",
            {
                "id": self.physical_id,
                "parents": parents_input(
                    placement=child["id"]
                ),
                "transform": transform_input(
                    physical_local
                )
            }
        )

        parent_domain_local = translation_matrix(
            1,
            2,
            3
        )

        child_domain_local = compose_matrices(
            translation_matrix(4, 5, 6),
            rotation_z(25)
        )

        grandchild_domain_local = compose_matrices(
            translation_matrix(-2, -3, -4),
            rotation_x(-35)
        )

        domains = [
            (
                parent,
                self.create_domain(
                    parent["id"],
                    name="Parent domain",
                    matrix=parent_domain_local
                ),
                parent_domain_local
            ),
            (
                child,
                self.create_domain(
                    child["id"],
                    name="Child domain",
                    matrix=child_domain_local
                ),
                child_domain_local
            ),
            (
                grandchild,
                self.create_domain(
                    grandchild["id"],
                    name="Grandchild domain",
                    matrix=grandchild_domain_local
                ),
                grandchild_domain_local
            )
        ]

        parent_positions = [
            compose_matrices(
                translation_matrix(100, 200, 300),
                rotation_z(90),
                rotation_y(30)
            ),
            compose_matrices(
                translation_matrix(-50, 75, 125),
                rotation_x(45),
                rotation_y(-20)
            ),
            identity_matrix()
        ]

        for index, parent_world in enumerate(parent_positions):
            with self.subTest(position=index):
                updated = self.update_transform(
                    parent["id"],
                    parent_world,
                    space="world"
                )

                self.assert_identity(
                    parent,
                    updated
                )

                self.assert_placement(
                    updated,
                    parent_world,
                    parent_world
                )

                child_world = multiply_matrices(
                    parent_world,
                    child_local
                )

                grandchild_world = multiply_matrices(
                    child_world,
                    grandchild_local
                )

                physical_world = multiply_matrices(
                    child_world,
                    physical_local
                )

                self.assert_placement(
                    self.get_transform(child["id"]),
                    child_local,
                    child_world
                )

                self.assert_placement(
                    self.get_transform(grandchild["id"]),
                    grandchild_local,
                    grandchild_world
                )

                self.assert_placement(
                    self.get_transform(self.physical_id),
                    physical_local,
                    physical_world
                )

                self.assert_physical_mesh(
                    physical_world
                )

                self.assert_physical_mesh(
                    identity_matrix(),
                    space="local"
                )

                for owner, domain, local in domains:
                    owner_world = self.get_transform(
                        owner["id"]
                    )["transforms"]["world"]

                    expected_world = multiply_matrices(
                        owner_world,
                        local
                    )

                    retrieved = self.get_geometry(
                        domain["id"]
                    )

                    self.assert_identity(
                        domain,
                        retrieved
                    )

                    self.assert_placement(
                        retrieved,
                        local,
                        expected_world
                    )

                    self.assert_domain_mesh(
                        owner["id"],
                        domain["id"],
                        expected_world
                    )

                    self.assert_domain_mesh(
                        owner["id"],
                        domain["id"],
                        local,
                        space="local"
                    )

    def test_reparent_local(self):
        """Verify local placement preservation during reparenting."""

        first_parent_world = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(90)
        )

        second_parent_world = compose_matrices(
            translation_matrix(100, 200, 300),
            rotation_y(45)
        )

        child_local = compose_matrices(
            translation_matrix(2, 3, 4),
            rotation_x(30)
        )

        first_parent = self.create_transform(
            name="First local parent",
            matrix=first_parent_world
        )

        second_parent = self.create_transform(
            name="Second local parent",
            matrix=second_parent_world
        )

        child = self.create_transform(
            name="Locally reparented child",
            parent_id=first_parent["id"],
            matrix=child_local
        )

        domain_local = compose_matrices(
            translation_matrix(5, 6, 7),
            rotation_z(25)
        )

        domain = self.create_domain(
            child["id"],
            matrix=domain_local
        )

        original_world = multiply_matrices(
            first_parent_world,
            child_local
        )

        self.assert_placement(
            child,
            child_local,
            original_world
        )

        updated = self.client.request(
            "transformUpdate",
            {
                "id": child["id"],
                "parents": parents_input(
                    placement=second_parent["id"]
                )
            }
        )

        expected_world = multiply_matrices(
            second_parent_world,
            child_local
        )

        self.assert_identity(
            child,
            updated
        )

        self.assert_placement(
            updated,
            child_local,
            expected_world
        )

        self.assertEqual(
            updated["parents"]["placement"],
            second_parent["id"]
        )

        self.assertNotEqual(
            updated["transforms"]["world"],
            child["transforms"]["world"]
        )

        self.assert_placement(
            self.get_geometry(domain["id"]),
            domain_local,
            multiply_matrices(
                expected_world,
                domain_local
            )
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            domain_local,
            space="local"
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            multiply_matrices(
                expected_world,
                domain_local
            )
        )

        detached = self.client.request(
            "transformUpdate",
            {
                "id": child["id"],
                "parents": parents_input()
            }
        )

        self.assert_placement(
            detached,
            child_local,
            child_local
        )

        self.assertIsNone(
            detached["parents"]["placement"]
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            multiply_matrices(
                child_local,
                domain_local
            )
        )

        self.save_and_reload()

        reloaded = self.find_transform(
            child["globalId"]
        )

        self.assert_placement(
            reloaded,
            child_local,
            child_local
        )

        self.assertEqual(
            reloaded["parents"],
            parents_input()
        )

    def test_reparent_world(self):
        """Verify world placement preservation during explicit reparenting."""

        first_parent_world = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(45)
        )

        second_parent_world = compose_matrices(
            translation_matrix(-100, 200, 50),
            rotation_y(-35),
            rotation_x(25)
        )

        child_world = compose_matrices(
            translation_matrix(50, 60, 70),
            rotation_z(15),
            rotation_x(20)
        )

        first_parent = self.create_transform(
            name="First world parent",
            matrix=first_parent_world
        )

        second_parent = self.create_transform(
            name="Second world parent",
            matrix=second_parent_world
        )

        child = self.create_transform(
            name="World-reparented child",
            parent_id=first_parent["id"],
            matrix=child_world,
            space="world"
        )

        domain_local = compose_matrices(
            translation_matrix(2, 4, 6),
            rotation_y(35)
        )

        domain_world = multiply_matrices(
            child_world,
            domain_local
        )

        domain = self.create_domain(
            child["id"],
            matrix=domain_local
        )

        expected_local = multiply_matrices(
            rigid_inverse(second_parent_world),
            child_world
        )

        updated = self.client.request(
            "transformUpdate",
            {
                "id": child["id"],
                "parents": parents_input(
                    placement=second_parent["id"]
                ),
                "transform": transform_input(
                    child_world,
                    space="world"
                )
            }
        )

        self.assert_identity(
            child,
            updated
        )

        self.assert_placement(
            updated,
            expected_local,
            child_world
        )

        self.assertEqual(
            updated["parents"]["placement"],
            second_parent["id"]
        )

        self.assert_placement(
            self.get_geometry(domain["id"]),
            domain_local,
            domain_world
        )

        self.assert_domain_mesh(
            child["id"],
            domain["id"],
            domain_world
        )

        self.save_and_reload()

        reloaded = self.find_transform(
            child["globalId"]
        )

        reloaded_parent = self.find_transform(
            second_parent["globalId"]
        )

        reloaded_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assert_placement(
            reloaded,
            expected_local,
            child_world
        )

        self.assertEqual(
            reloaded["parents"]["placement"],
            reloaded_parent["id"]
        )

        self.assert_domain_mesh(
            reloaded["id"],
            reloaded_domain["id"],
            domain_world
        )

        detached = self.client.request(
            "transformUpdate",
            {
                "id": reloaded["id"],
                "parents": parents_input(),
                "transform": transform_input(
                    child_world,
                    space="world"
                )
            }
        )

        self.assert_placement(
            detached,
            child_world,
            child_world
        )

        self.assertEqual(
            detached["parents"],
            parents_input()
        )

        self.assert_domain_mesh(
            reloaded["id"],
            reloaded_domain["id"],
            domain_world
        )

    def test_invalid_matrix_size(self):
        """Verify rejection of invalid matrix dimensions and component types."""

        cases = [
            ("empty matrix", []),
            ("single value", [1]),
            ("3x3 matrix", [1] * 9),
            ("3x4 matrix", [1] * 12),
            ("15 components", [1] * 15),
            ("17 components", [1] * 17),
            ("25 components", [1] * 25),
            (
                "nested 4x4 matrix",
                [
                    [1, 0, 0, 0],
                    [0, 1, 0, 0],
                    [0, 0, 1, 0],
                    [0, 0, 0, 1]
                ]
            ),
            ("null matrix", None),
            ("string matrix", "identity"),
            ("object matrix", {}),
            ("boolean matrix", True),
            (
                "string component",
                ["1"] + identity_matrix()[1:]
            ),
            (
                "boolean component",
                [True] + identity_matrix()[1:]
            ),
            (
                "null component",
                [None] + identity_matrix()[1:]
            )
        ]

        self.assert_invalid_placements(
            cases,
            "INVALID_PARAMETERS"
        )

    def test_invalid_affine_matrix(self):
        """Verify rejection of non-affine matrices."""

        cases = []

        invalid_rows = [
            [1, 0, 0, 1],
            [0, 1, 0, 1],
            [0, 0, 1, 1],
            [0, 0, 0, 0],
            [0, 0, 0, 2],
            [0, 0, 0, -1],
            [0.001, 0, 0, 1],
            [0, -0.001, 0, 1],
            [0, 0, 0.001, 1],
            [0.1, 0.2, 0.3, 1]
        ]

        for row in invalid_rows:
            matrix = identity_matrix()
            matrix[12:16] = row

            cases.append(
                (
                    f"invalid last row {row}",
                    matrix
                )
            )

        perspective = [
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            0, 0, -1, 0
        ]

        cases.append(
            (
                "perspective projection",
                perspective
            )
        )

        self.assert_invalid_placements(
            cases,
            "INVALID_PLACEMENT"
        )

    def test_invalid_scaling(self):
        """Verify rejection of uniform and non-uniform scaling."""

        cases = [
            (
                "uniform scale 2",
                scaling_matrix(2, 2, 2)
            ),
            (
                "uniform scale 0.5",
                scaling_matrix(0.5, 0.5, 0.5)
            ),
            (
                "X scaling",
                scaling_matrix(2, 1, 1)
            ),
            (
                "Y scaling",
                scaling_matrix(1, 2, 1)
            ),
            (
                "Z scaling",
                scaling_matrix(1, 1, 2)
            ),
            (
                "non-uniform scaling",
                scaling_matrix(2, 3, 4)
            ),
            (
                "zero X scale",
                scaling_matrix(0, 1, 1)
            ),
            (
                "zero Y scale",
                scaling_matrix(1, 0, 1)
            ),
            (
                "zero Z scale",
                scaling_matrix(1, 1, 0)
            ),
            (
                "zero matrix basis",
                scaling_matrix(0, 0, 0)
            ),
            (
                "small non-unit scaling",
                scaling_matrix(1.0001, 1, 1)
            ),
            (
                "rotation with scaling",
                compose_matrices(
                    rotation_z(45),
                    scaling_matrix(2, 1, 1)
                )
            ),
            (
                "translation with scaling",
                compose_matrices(
                    translation_matrix(10, 20, 30),
                    scaling_matrix(1, 2, 1)
                )
            )
        ]

        self.assert_invalid_placements(
            cases,
            "INVALID_PLACEMENT"
        )

    def test_invalid_shear(self):
        """Verify rejection of shear and non-orthogonal coordinate systems."""

        cases = [
            (
                "XY shear",
                shear_matrix(xy=0.5)
            ),
            (
                "XZ shear",
                shear_matrix(xz=0.5)
            ),
            (
                "YX shear",
                shear_matrix(yx=0.5)
            ),
            (
                "YZ shear",
                shear_matrix(yz=0.5)
            ),
            (
                "ZX shear",
                shear_matrix(zx=0.5)
            ),
            (
                "ZY shear",
                shear_matrix(zy=0.5)
            ),
            (
                "combined shear",
                shear_matrix(
                    xy=0.3,
                    xz=-0.2,
                    yz=0.4
                )
            ),
            (
                "small shear",
                shear_matrix(xy=0.0001)
            ),
            (
                "rotated shear",
                compose_matrices(
                    rotation_z(45),
                    shear_matrix(xy=0.5)
                )
            ),
            (
                "unit-length non-orthogonal basis",
                [
                    1, 0.5, 0, 0,
                    0, math.sqrt(0.75), 0, 0,
                    0, 0, 1, 0,
                    0, 0, 0, 1
                ]
            )
        ]

        self.assert_invalid_placements(
            cases,
            "INVALID_PLACEMENT"
        )

    def test_invalid_reflection(self):
        """Verify rejection of left-handed coordinate systems."""

        cases = [
            (
                "X reflection",
                scaling_matrix(-1, 1, 1)
            ),
            (
                "Y reflection",
                scaling_matrix(1, -1, 1)
            ),
            (
                "Z reflection",
                scaling_matrix(1, 1, -1)
            ),
            (
                "central inversion",
                scaling_matrix(-1, -1, -1)
            ),
            (
                "rotated reflection",
                compose_matrices(
                    rotation_z(45),
                    scaling_matrix(-1, 1, 1)
                )
            ),
            (
                "translated reflection",
                compose_matrices(
                    translation_matrix(10, 20, 30),
                    scaling_matrix(1, -1, 1)
                )
            ),
            (
                "swapped X and Y axes",
                [
                    0, 1, 0, 0,
                    1, 0, 0, 0,
                    0, 0, 1, 0,
                    0, 0, 0, 1
                ]
            )
        ]

        for label, matrix in cases:
            with self.subTest(matrix=label):
                self.assertAlmostEqual(
                    determinant(matrix),
                    -1.0,
                    delta=MATRIX_TOLERANCE
                )

        self.assert_invalid_placements(
            cases,
            "INVALID_PLACEMENT"
        )

        # Two negative axes form a valid 180-degree rotation,
        # rather than a left-handed reflection.

        valid_cases = [
            scaling_matrix(-1, -1, 1),
            scaling_matrix(-1, 1, -1),
            scaling_matrix(1, -1, -1)
        ]

        for matrix in valid_cases:
            with self.subTest(valid_rotation=matrix):
                self.assertAlmostEqual(
                    determinant(matrix),
                    1.0,
                    delta=MATRIX_TOLERANCE
                )

                updated = self.update_transform(
                    self.physical_id,
                    matrix
                )

                self.assert_placement(
                    updated,
                    matrix,
                    matrix
                )

                self.assert_physical_mesh(
                    matrix
                )

    def test_numeric_precision(self):
        """Verify numerical stability after repeated updates and IFC roundtrips."""

        root_matrix = compose_matrices(
            translation_matrix(
                1234.123456789,
                -5678.987654321,
                0.000123456789
            ),
            rotation_z(12.3456789),
            rotation_y(-23.4567891),
            rotation_x(34.5678912)
        )

        child_local = compose_matrices(
            translation_matrix(
                0.123456789,
                -0.987654321,
                1.234567891
            ),
            rotation_x(-17.1234567),
            rotation_z(27.9876543)
        )

        domain_local = compose_matrices(
            translation_matrix(
                0.000234567,
                0.345678912,
                -0.456789123
            ),
            rotation_y(18.7654321),
            rotation_x(-28.6543219)
        )

        root = self.create_transform(
            name="Precision root",
            matrix=root_matrix
        )

        child = self.create_transform(
            name="Precision child",
            parent_id=root["id"],
            matrix=child_local
        )

        domain = self.create_domain(
            child["id"],
            name="Precision domain",
            matrix=domain_local
        )

        root_global_id = root["globalId"]
        child_global_id = child["globalId"]
        domain_global_id = domain["globalId"]

        expected_root = root_matrix
        expected_child_world = multiply_matrices(
            expected_root,
            child_local
        )

        expected_domain_world = multiply_matrices(
            expected_child_world,
            domain_local
        )

        self.assert_placement(
            root,
            expected_root,
            expected_root
        )

        self.assert_placement(
            child,
            child_local,
            expected_child_world
        )

        self.assert_placement(
            domain,
            domain_local,
            expected_domain_world
        )

        increment = compose_matrices(
            translation_matrix(
                0.000123456,
                -0.000234567,
                0.000345678
            ),
            rotation_z(0.125),
            rotation_x(-0.0625),
            rotation_y(0.03125)
        )

        iterations = 100
        reload_interval = 10

        for iteration in range(1, iterations + 1):
            with self.subTest(iteration=iteration):
                expected_root = multiply_matrices(
                    expected_root,
                    increment
                )

                current_root = self.get_transform(
                    root["id"]
                )

                actual_incremented_root = multiply_matrices(
                    current_root["transforms"]["world"],
                    increment
                )

                updated_root = self.update_transform(
                    root["id"],
                    actual_incremented_root,
                    space="world"
                )

                self.assert_placement(
                    updated_root,
                    expected_root,
                    expected_root
                )

                expected_child_world = multiply_matrices(
                    expected_root,
                    child_local
                )

                expected_domain_world = multiply_matrices(
                    expected_child_world,
                    domain_local
                )

                self.assert_placement(
                    self.get_transform(child["id"]),
                    child_local,
                    expected_child_world
                )

                self.assert_placement(
                    self.get_geometry(domain["id"]),
                    domain_local,
                    expected_domain_world
                )

                if iteration % reload_interval == 0:
                    self.save_and_reload()

                    root = self.find_transform(
                        root_global_id
                    )

                    child = self.find_transform(
                        child_global_id
                    )

                    domain = self.find_geometry(
                        domain_global_id
                    )

                    self.assert_placement(
                        root,
                        expected_root,
                        expected_root
                    )

                    self.assert_placement(
                        child,
                        child_local,
                        expected_child_world
                    )

                    self.assert_placement(
                        domain,
                        domain_local,
                        expected_domain_world
                    )

                    self.assertEqual(
                        child["parents"]["placement"],
                        root["id"]
                    )

                    self.assertEqual(
                        domain["parentId"],
                        child["id"]
                    )

        world_mesh = self.assert_domain_mesh(
            child["id"],
            domain["id"],
            expected_domain_world
        )

        local_mesh = self.assert_domain_mesh(
            child["id"],
            domain["id"],
            domain_local,
            space="local"
        )

        self.assertAlmostEqual(
            mesh_volume(world_mesh),
            mesh_volume(local_mesh),
            delta=MESH_TOLERANCE
        )

        self.assert_normal_directions(
            world_mesh,
            expected_domain_world
        )

        self.assert_normal_directions(
            local_mesh,
            domain_local
        )

        for iteration in range(10):
            with self.subTest(roundtrip=iteration):
                self.save_and_reload()

                root = self.find_transform(
                    root_global_id
                )

                child = self.find_transform(
                    child_global_id
                )

                domain = self.find_geometry(
                    domain_global_id
                )

                self.assert_placement(
                    root,
                    expected_root,
                    expected_root
                )

                self.assert_placement(
                    child,
                    child_local,
                    expected_child_world
                )

                self.assert_placement(
                    domain,
                    domain_local,
                    expected_domain_world
                )

        final_world_mesh = self.assert_domain_mesh(
            child["id"],
            domain["id"],
            expected_domain_world
        )

        final_local_mesh = self.assert_domain_mesh(
            child["id"],
            domain["id"],
            domain_local,
            space="local"
        )

        self.assert_point_sets(
            mesh_points(final_world_mesh),
            mesh_points(world_mesh)
        )

        self.assert_point_sets(
            mesh_points(final_local_mesh),
            mesh_points(local_mesh)
        )

        self.assertAlmostEqual(
            mesh_volume(final_world_mesh),
            30.0,
            delta=MESH_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_volume(final_local_mesh),
            30.0,
            delta=MESH_TOLERANCE
        )


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