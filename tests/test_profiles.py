import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from collections import Counter
from pathlib import Path


# Standalone integration suite.
# Expected results must be validated against the compiled CLI.
# SOLID and DOMAIN profiles are both tested through the parametric API.
# Independent mesh validation uses DOMAIN geometry because SOLID
# features are tessellated as part of their parent transform.
# Large profiles use an extended timeout due to quadratic validation
# and OpenCascade tessellation costs.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30
LARGE_PROFILE_TIMEOUT = 180

PROFILE_TOLERANCE = 1e-9
MESH_TOLERANCE = 1e-6
VOLUME_RELATIVE_TOLERANCE = 1e-6

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

PARENT_GLOBAL_ID = "0000000000000000000001"
PROJECT_GLOBAL_ID = "0000000000000000000002"

GEOMETRY_KINDS = (
    "SOLID",
    "DOMAIN"
)

IDENTITY_MATRIX = [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1
]

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
FILE_NAME('profiles-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#16=IFCPROJECT('0000000000000000000002',$,'Profile test project',$,$,$,$,(#6),#8);
ENDSEC;
END-ISO-10303-21;
"""


def transform_input():
    return {
        "space": "parent",
        "matrix": IDENTITY_MATRIX.copy()
    }


def signed_polygon_area(profile):
    terms = [
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(
            profile,
            profile[1:] + profile[:1]
        )
    ]

    return math.fsum(terms) / 2.0


def polygon_area(profile):
    return abs(signed_polygon_area(profile))


def polygon_perimeter(profile):
    return math.fsum(
        math.dist(first, second)
        for first, second in zip(
            profile,
            profile[1:] + profile[:1]
        )
    )


def rectangular_profile(width, length):
    return [
        [0, 0],
        [width, 0],
        [width, length],
        [0, length]
    ]


def regular_polygon(
    point_count,
    radius,
    center_x=0.0,
    center_y=0.0,
    start_angle=0.0
):
    return [
        [
            center_x + radius * math.cos(
                start_angle + 2.0 * math.pi * index / point_count
            ),
            center_y + radius * math.sin(
                start_angle + 2.0 * math.pi * index / point_count
            )
        ]
        for index in range(point_count)
    ]


def regular_polygon_area(point_count, radius):
    return (
        point_count
        * radius ** 2
        * math.sin(2.0 * math.pi / point_count)
        / 2.0
    )


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


def mesh_bounds(mesh):
    points = mesh_points(mesh)

    if not points:
        raise AssertionError("Mesh has no vertices")

    return tuple(
        function(point[axis] for point in points)
        for function in (min, max)
        for axis in range(3)
    )


def expected_extrusion_bounds(profile, depth):
    return (
        min(point[0] for point in profile),
        min(point[1] for point in profile),
        0.0,
        max(point[0] for point in profile),
        max(point[1] for point in profile),
        depth
    )


def subtract_vectors(first, second):
    return tuple(
        first[axis] - second[axis]
        for axis in range(3)
    )


def cross_product(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0]
    )


def dot_product(first, second):
    return math.fsum(
        first[axis] * second[axis]
        for axis in range(3)
    )


def vector_length(vector):
    return math.sqrt(
        math.fsum(value * value for value in vector)
    )


def triangle_area(a, b, c):
    return vector_length(
        cross_product(
            subtract_vectors(b, a),
            subtract_vectors(c, a)
        )
    ) / 2.0


def mesh_surface_area(mesh):
    return math.fsum(
        triangle_area(a, b, c)
        for a, b, c in mesh_triangles(mesh)
    )


def mesh_volume(mesh):
    points = mesh_points(mesh)

    if not points:
        return 0.0

    origin = points[0]
    terms = []

    for a, b, c in mesh_triangles(mesh):
        a = subtract_vectors(a, origin)
        b = subtract_vectors(b, origin)
        c = subtract_vectors(c, origin)

        terms.append(
            dot_product(
                a,
                cross_product(b, c)
            )
        )

    return abs(math.fsum(terms)) / 6.0


def mesh_cap_areas(mesh, depth):
    bottom = []
    top = []

    for triangle in mesh_triangles(mesh):
        if all(
            abs(point[2]) <= MESH_TOLERANCE
            for point in triangle
        ):
            bottom.append(
                triangle_area(*triangle)
            )

        elif all(
            abs(point[2] - depth) <= MESH_TOLERANCE
            for point in triangle
        ):
            top.append(
                triangle_area(*triangle)
            )

    return (
        math.fsum(bottom),
        math.fsum(top)
    )


def mesh_edge_counts(mesh, precision=8):
    edges = Counter()

    for triangle in mesh_triangles(mesh):
        points = [
            tuple(round(value, precision) for value in point)
            for point in triangle
        ]

        if len(set(points)) != 3:
            raise AssertionError(
                f"Degenerate triangle: {triangle}"
            )

        a, b, c = points

        for first, second in (
            (a, b),
            (b, c),
            (c, a)
        ):
            edges[
                tuple(sorted((first, second)))
            ] += 1

    return edges


def point_on_segment(
    point,
    start,
    end,
    tolerance=MESH_TOLERANCE
):
    dx = end[0] - start[0]
    dy = end[1] - start[1]

    length_squared = dx * dx + dy * dy

    if length_squared == 0:
        return math.dist(point, start) <= tolerance

    parameter = (
        (point[0] - start[0]) * dx
        + (point[1] - start[1]) * dy
    ) / length_squared

    parameter = max(
        0.0,
        min(1.0, parameter)
    )

    nearest = (
        start[0] + parameter * dx,
        start[1] + parameter * dy
    )

    return math.dist(point, nearest) <= tolerance


def point_in_polygon(
    point,
    profile,
    tolerance=MESH_TOLERANCE
):
    x, y = point
    inside = False

    for first, second in zip(
        profile,
        profile[1:] + profile[:1]
    ):
        if point_on_segment(
            point,
            first,
            second,
            tolerance
        ):
            return True

        x1, y1 = first
        x2, y2 = second

        if (y1 > y) != (y2 > y):
            intersection_x = (
                x1
                + (y - y1)
                * (x2 - x1)
                / (y2 - y1)
            )

            if x < intersection_x:
                inside = not inside

    return inside


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

    def next_request_id(self):
        self.counter += 1

        return f"request-{self.counter}"

    def write_raw(self, payload):
        if self.process.poll() is not None:
            raise RuntimeError(
                f"CLI is not running: {self.process.returncode}"
            )

        if not payload.endswith("\n"):
            payload += "\n"

        self.process.stdin.write(payload)
        self.process.stdin.flush()

    def send(self, command, params=None):
        request_id = self.next_request_id()

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

        self.write_raw(payload)

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

        if "id" not in response:
            raise AssertionError(
                f"Missing response identifier: {response}"
            )

        if response["id"] != request_id:
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

    def assert_error_response(
        self,
        response,
        expected_code
    ):
        if response["ok"]:
            raise AssertionError(
                f"Request unexpectedly succeeded: {response}"
            )

        if response["error"]["code"] != expected_code:
            raise AssertionError(
                f"Expected {expected_code}, "
                f"received {response['error']}"
            )

        return response["error"]

    def expect_error(
        self,
        command,
        params,
        expected_code
    ):
        request_id = self.send(command, params)

        return self.assert_error_response(
            self.read(request_id),
            expected_code
        )

    def expect_raw_error(
        self,
        payload,
        expected_code="INVALID_JSON",
        expected_id=None
    ):
        self.write_raw(payload)

        return self.assert_error_response(
            self.read(expected_id),
            expected_code
        )

    def tessellate(
        self,
        transform_id,
        geometry_id,
        space="local"
    ):
        request_id = self.send(
            "transformTessellate",
            {
                "id": transform_id,
                "options": {
                    "space": space,
                    "kind": "DOMAIN",
                    "geometryId": geometry_id,
                    "includeNormals": True,
                    "includeMaterials": False,
                    "includeChildren": False
                }
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

            if not isinstance(event, dict):
                raise AssertionError(
                    f"Invalid mesh event: {event}"
                )

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

        for field in (
            "vertexCount",
            "indexCount"
        ):
            if type(mesh[field]) is not int or mesh[field] < 0:
                raise AssertionError(
                    f"Invalid mesh count: {begin}"
                )

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError(
                "Invalid vertex count"
            )

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError(
                "Invalid index count"
            )

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError(
                "Invalid triangle indices"
            )

        if len(mesh["normals"]) % 3 != 0:
            raise AssertionError(
                "Invalid normal array length"
            )

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


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-profiles-"
        )

        self.addCleanup(
            self.directory.cleanup
        )

        self.root_path = Path(
            self.directory.name
        )

        self.input_path = (
            self.root_path / "input.ifc"
        )

        self.output_path = (
            self.root_path / "output.ifc"
        )

        self.input_path.write_text(
            IFC_FIXTURE,
            encoding="utf-8"
        )

        self.client = Client(EXECUTABLE)

        self.addCleanup(
            self.client.terminate
        )

        self.load_model()

    def tearDown(self):
        self.client.close()

    def load_model(self, path=None):
        path = (
            self.input_path
            if path is None
            else path
        )

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

        transforms = self.client.request(
            "transformGetAll"
        )

        matches = [
            transform
            for transform in transforms
            if transform["globalId"] == PARENT_GLOBAL_ID
        ]

        self.assertEqual(
            len(matches),
            1
        )

        self.parent_id = matches[0]["id"]

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

        self.assertTrue(
            self.output_path.is_file()
        )

        self.assertGreater(
            self.output_path.stat().st_size,
            0
        )

    def save_and_reload(self):
        self.save_model()

        return self.load_model(
            self.output_path
        )

    def list_geometries(self, kind=None):
        params = {
            "transformId": None
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

    def get_parent(self):
        return self.client.request(
            "transformGet",
            {"id": self.parent_id}
        )

    def geometry_create_params(
        self,
        profile,
        depth=DEFAULT_DEPTH,
        kind="DOMAIN",
        name="Profile geometry"
    ):
        return {
            "parentId": self.parent_id,
            "name": name,
            "kind": kind,
            "operation": "ADD",
            "transform": transform_input(),
            "profile": profile,
            "depth": depth
        }

    def create_geometry(
        self,
        profile,
        depth=DEFAULT_DEPTH,
        kind="DOMAIN",
        name="Profile geometry"
    ):
        return self.client.request(
            "geometryCreate",
            self.geometry_create_params(
                profile=profile,
                depth=depth,
                kind=kind,
                name=name
            )
        )

    def update_geometry(
        self,
        identifier,
        **changes
    ):
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

    def snapshot_geometries(self):
        return {
            item["id"]: self.get_geometry(
                item["id"]
            )
            for item in self.list_geometries()
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

    def assert_numeric_value(
        self,
        actual,
        expected,
        absolute_tolerance=MESH_TOLERANCE,
        relative_tolerance=VOLUME_RELATIVE_TOLERANCE
    ):
        self.assertIn(
            type(actual),
            (int, float)
        )

        self.assertTrue(
            math.isfinite(actual)
        )

        self.assertTrue(
            math.isclose(
                actual,
                expected,
                rel_tol=relative_tolerance,
                abs_tol=absolute_tolerance
            ),
            f"{actual} != {expected}"
        )

    def assert_profile(self, actual, expected):
        self.assertIsInstance(
            actual,
            list
        )

        self.assertEqual(
            len(actual),
            len(expected)
        )

        self.assertGreaterEqual(
            len(actual),
            3
        )

        for index, (point, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertIsInstance(
                point,
                list
            )

            self.assertEqual(
                len(point),
                2
            )

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
                    delta=PROFILE_TOLERANCE,
                    msg=(
                        f"Invalid profile point {index}, "
                        f"axis {axis}"
                    )
                )

        self.assertNotEqual(
            actual[0],
            actual[-1],
            "The returned profile must not repeat its endpoint"
        )

    def assert_geometry_definition(
        self,
        geometry,
        profile,
        depth,
        kind
    ):
        self.assertEqual(
            geometry["source"],
            "editor"
        )

        self.assertIs(
            geometry["editable"],
            True
        )

        self.assertEqual(
            geometry["kind"],
            kind
        )

        self.assertEqual(
            geometry["operation"],
            "ADD"
        )

        self.assertEqual(
            geometry["parentId"],
            self.parent_id
        )

        self.assertEqual(
            geometry["ifcClass"],
            (
                "IfcSpatialZone"
                if kind == "DOMAIN"
                else "IfcProjectionElement"
            )
        )

        self.assertIsInstance(
            geometry["globalId"],
            str
        )

        self.assertTrue(
            geometry["globalId"]
        )

        self.assert_profile(
            geometry["profile"],
            profile
        )

        self.assert_numeric_value(
            geometry["depth"],
            depth,
            absolute_tolerance=PROFILE_TOLERANCE,
            relative_tolerance=0.0
        )

        self.assertEqual(
            geometry["transforms"]["local"],
            IDENTITY_MATRIX
        )

        self.assertEqual(
            geometry["transforms"]["world"],
            IDENTITY_MATRIX
        )

        self.assertIs(
            geometry["transforms"]["editable"],
            True
        )

        self.assertEqual(
            geometry["transforms"]["sourceType"],
            "IfcLocalPlacement"
        )

        self.assertEqual(
            geometry["materials"],
            []
        )

        self.assertEqual(
            self.get_geometry(geometry["id"]),
            geometry
        )

        parent = self.get_parent()

        self.assertIn(
            geometry["id"],
            parent["geometries"]
        )

    def assert_bounds(self, mesh, expected):
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
                delta=MESH_TOLERANCE,
                msg=f"Invalid bounding box component {index}"
            )

    def assert_closed_mesh(self, mesh):
        edges = mesh_edge_counts(mesh)

        self.assertTrue(
            edges,
            "Mesh contains no edges"
        )

        invalid_edges = {
            edge: count
            for edge, count in edges.items()
            if count != 2
        }

        self.assertFalse(
            invalid_edges,
            f"Non-manifold or open mesh edges: "
            f"{list(invalid_edges.items())[:10]}"
        )

    def assert_mesh_normals(self, mesh):
        normals = mesh["normals"]

        self.assertTrue(
            normals,
            "Mesh normals are missing"
        )

        self.assertEqual(
            len(normals),
            mesh["vertexCount"] * 3
        )

        for offset in range(
            0,
            len(normals),
            3
        ):
            normal = normals[offset:offset + 3]

            self.assertAlmostEqual(
                vector_length(normal),
                1.0,
                delta=MESH_TOLERANCE
            )

    def assert_profile_corners(
        self,
        mesh,
        profile,
        depth
    ):
        # Collinear intermediate points may be removed by
        # the tessellator without changing the extrusion.

        mesh_vertices = mesh_points(mesh)

        for index, current in enumerate(profile):
            previous = profile[
                (index - 1) % len(profile)
            ]

            following = profile[
                (index + 1) % len(profile)
            ]

            if point_on_segment(
                current,
                previous,
                following,
                tolerance=PROFILE_TOLERANCE
            ):
                continue

            for z in (0.0, depth):
                expected = (
                    current[0],
                    current[1],
                    z
                )

                self.assertTrue(
                    any(
                        math.dist(expected, vertex)
                        <= MESH_TOLERANCE
                        for vertex in mesh_vertices
                    ),
                    f"Missing extrusion corner: {expected}"
                )

    def assert_mesh_inside_profile(
        self,
        mesh,
        profile,
        depth
    ):
        for point in mesh_points(mesh):
            self.assertGreaterEqual(
                point[2],
                -MESH_TOLERANCE
            )

            self.assertLessEqual(
                point[2],
                depth + MESH_TOLERANCE
            )

            self.assertTrue(
                point_in_polygon(
                    point[:2],
                    profile
                ),
                f"Mesh vertex outside profile: {point}"
            )

        # Cap triangle centroids must remain inside the polygon.
        # This detects triangulation across concave recesses.

        for triangle in mesh_triangles(mesh):
            on_bottom = all(
                abs(point[2]) <= MESH_TOLERANCE
                for point in triangle
            )

            on_top = all(
                abs(point[2] - depth) <= MESH_TOLERANCE
                for point in triangle
            )

            if not on_bottom and not on_top:
                continue

            centroid = tuple(
                math.fsum(
                    point[axis]
                    for point in triangle
                ) / 3.0
                for axis in range(2)
            )

            self.assertTrue(
                point_in_polygon(
                    centroid,
                    profile
                ),
                f"Cap triangle outside profile: {triangle}"
            )

    def assert_extrusion_mesh(
        self,
        geometry,
        profile,
        depth,
        expected_area=None,
        check_polygon=True,
        check_corners=True
    ):
        self.assertEqual(
            geometry["kind"],
            "DOMAIN"
        )

        mesh = self.client.tessellate(
            self.parent_id,
            geometry["id"]
        )

        self.assertGreater(
            mesh["vertexCount"],
            0
        )

        self.assertGreater(
            mesh["indexCount"],
            0
        )

        self.assertEqual(
            mesh["indexCount"] % 3,
            0
        )

        area = (
            polygon_area(profile)
            if expected_area is None
            else expected_area
        )

        perimeter = polygon_perimeter(profile)

        expected_volume = area * depth

        expected_surface = (
            2.0 * area
            + perimeter * depth
        )

        self.assert_bounds(
            mesh,
            expected_extrusion_bounds(
                profile,
                depth
            )
        )

        self.assert_numeric_value(
            mesh_volume(mesh),
            expected_volume
        )

        self.assert_numeric_value(
            mesh_surface_area(mesh),
            expected_surface
        )

        bottom_area, top_area = mesh_cap_areas(
            mesh,
            depth
        )

        self.assert_numeric_value(
            bottom_area,
            area
        )

        self.assert_numeric_value(
            top_area,
            area
        )

        self.assert_closed_mesh(mesh)
        self.assert_mesh_normals(mesh)

        if check_corners:
            self.assert_profile_corners(
                mesh,
                profile,
                depth
            )

        if check_polygon:
            self.assert_mesh_inside_profile(
                mesh,
                profile,
                depth
            )

        return mesh

    def assert_valid_profile(
        self,
        profile,
        depth,
        expected_area=None,
        name="Valid profile",
        persistence=True,
        check_polygon=True,
        check_corners=True
    ):
        if expected_area is not None:
            self.assert_numeric_value(
                polygon_area(profile),
                expected_area,
                absolute_tolerance=PROFILE_TOLERANCE
            )

        created = {}
        meshes = {}

        for kind in GEOMETRY_KINDS:
            with self.subTest(
                kind=kind,
                phase="creation"
            ):
                geometry = self.create_geometry(
                    profile=profile,
                    depth=depth,
                    kind=kind,
                    name=f"{name} {kind}"
                )

                self.assert_geometry_definition(
                    geometry,
                    profile,
                    depth,
                    kind
                )

                created[kind] = geometry

                if kind == "DOMAIN":
                    meshes[kind] = self.assert_extrusion_mesh(
                        geometry,
                        profile,
                        depth,
                        expected_area=expected_area,
                        check_polygon=check_polygon,
                        check_corners=check_corners
                    )

        if persistence:
            self.save_and_reload()

            for kind, geometry in created.items():
                with self.subTest(
                    kind=kind,
                    phase="reload"
                ):
                    reloaded = self.find_geometry(
                        geometry["globalId"]
                    )

                    self.assert_geometry_definition(
                        reloaded,
                        profile,
                        depth,
                        kind
                    )

                    self.assertEqual(
                        reloaded["globalId"],
                        geometry["globalId"]
                    )

                    self.assertEqual(
                        reloaded["name"],
                        geometry["name"]
                    )

                    if kind == "DOMAIN":
                        reloaded_mesh = self.assert_extrusion_mesh(
                            reloaded,
                            profile,
                            depth,
                            expected_area=expected_area,
                            check_polygon=check_polygon,
                            check_corners=check_corners
                        )

                        self.assert_numeric_value(
                            mesh_volume(reloaded_mesh),
                            mesh_volume(meshes[kind])
                        )

                        self.assert_numeric_value(
                            mesh_surface_area(reloaded_mesh),
                            mesh_surface_area(meshes[kind])
                        )

                    created[kind] = reloaded

        return created

    def assert_invalid_profiles(
        self,
        cases,
        expected_code
    ):
        existing = {
            kind: self.create_geometry(
                profile=DEFAULT_PROFILE,
                kind=kind,
                name=f"Preserved {kind}"
            )
            for kind in GEOMETRY_KINDS
        }

        initial = self.snapshot_geometries()
        parent_before = self.get_parent()

        for label, profile in cases:
            for kind in GEOMETRY_KINDS:
                with self.subTest(
                    case=label,
                    kind=kind
                ):
                    self.client.expect_error(
                        "geometryCreate",
                        self.geometry_create_params(
                            profile=profile,
                            kind=kind,
                            name=f"Rejected {label} {kind}"
                        ),
                        expected_code
                    )

                    self.assertEqual(
                        self.snapshot_geometries(),
                        initial
                    )

                    self.assertEqual(
                        self.get_parent(),
                        parent_before
                    )

                    self.client.expect_error(
                        "geometryUpdate",
                        {
                            "id": existing[kind]["id"],
                            "profile": profile
                        },
                        expected_code
                    )

                    self.assertEqual(
                        self.snapshot_geometries(),
                        initial
                    )

                    self.assertEqual(
                        self.get_parent(),
                        parent_before
                    )

        self.assert_extrusion_mesh(
            existing["DOMAIN"],
            DEFAULT_PROFILE,
            DEFAULT_DEPTH,
            expected_area=15.0
        )

        replacement = [
            [0, 0],
            [4, 0],
            [0, 3]
        ]

        for kind, geometry in existing.items():
            updated = self.update_geometry(
                geometry["id"],
                profile=replacement
            )

            self.assert_identity(
                geometry,
                updated
            )

            self.assert_geometry_definition(
                updated,
                replacement,
                DEFAULT_DEPTH,
                kind
            )

            if kind == "DOMAIN":
                self.assert_extrusion_mesh(
                    updated,
                    replacement,
                    DEFAULT_DEPTH,
                    expected_area=6.0
                )

        self.save_and_reload()

        for kind, geometry in existing.items():
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assert_geometry_definition(
                reloaded,
                replacement,
                DEFAULT_DEPTH,
                kind
            )

    def test_triangle_profile(self):
        """Verify extrusion of triangular profiles."""

        cases = [
            (
                "Right triangle",
                [
                    [0, 0],
                    [4, 0],
                    [0, 3]
                ],
                2.0,
                6.0
            ),
            (
                "Isosceles triangle",
                [
                    [-3, 0],
                    [3, 0],
                    [0, 4]
                ],
                5.0,
                12.0
            ),
            (
                "Scalene triangle",
                [
                    [1, 1],
                    [6, 2],
                    [3, 5]
                ],
                3.5,
                9.0
            )
        ]

        for name, profile, depth, area in cases:
            with self.subTest(profile=name):
                self.assert_valid_profile(
                    profile,
                    depth,
                    expected_area=area,
                    name=name
                )

    def test_rectangle_profile(self):
        """Verify extrusion of rectangular profiles."""

        cases = [
            (1.0, 1.0, 1.0),
            (2.0, 3.0, 4.0),
            (10.0, 2.0, 0.5),
            (0.125, 0.25, 0.75),
            (20.0, 15.0, 10.0)
        ]

        for width, length, depth in cases:
            with self.subTest(
                width=width,
                length=length,
                depth=depth
            ):
                profile = rectangular_profile(
                    width,
                    length
                )

                self.assert_valid_profile(
                    profile,
                    depth,
                    expected_area=width * length,
                    name=f"Rectangle {width}x{length}x{depth}"
                )

    def test_convex_profile(self):
        """Verify extrusion of convex polygons with multiple vertices."""

        cases = [
            (5, 3.0, 2.0),
            (6, 4.0, 3.0),
            (8, 5.0, 1.5),
            (12, 10.0, 4.0),
            (20, 8.0, 2.5)
        ]

        for point_count, radius, depth in cases:
            with self.subTest(
                point_count=point_count
            ):
                profile = regular_polygon(
                    point_count,
                    radius
                )

                expected_area = regular_polygon_area(
                    point_count,
                    radius
                )

                self.assertGreater(
                    signed_polygon_area(profile),
                    0.0
                )

                self.assert_valid_profile(
                    profile,
                    depth,
                    expected_area=expected_area,
                    name=f"Convex polygon {point_count}"
                )

        irregular_profile = [
            [0, 0],
            [5, 0],
            [8, 2],
            [7, 6],
            [3, 8],
            [-1, 5]
        ]

        self.assert_valid_profile(
            irregular_profile,
            3.0,
            expected_area=52.5,
            name="Irregular convex polygon"
        )

    def test_concave_profile(self):
        """Verify extrusion of a concave L-shaped profile."""

        profile = [
            [0, 0],
            [6, 0],
            [6, 2],
            [2, 2],
            [2, 5],
            [0, 5]
        ]

        expected_area = (
            6.0 * 2.0
            + 2.0 * 3.0
        )

        created = self.assert_valid_profile(
            profile,
            3.0,
            expected_area=expected_area,
            name="L-shaped profile"
        )

        domain = created["DOMAIN"]

        mesh = self.assert_extrusion_mesh(
            domain,
            profile,
            3.0,
            expected_area=18.0
        )

        self.assert_numeric_value(
            mesh_volume(mesh),
            54.0
        )

        self.assertFalse(
            point_in_polygon(
                (4.0, 4.0),
                profile
            )
        )

        for triangle in mesh_triangles(mesh):
            if not all(
                abs(point[2] - 3.0)
                <= MESH_TOLERANCE
                for point in triangle
            ):
                continue

            centroid = tuple(
                math.fsum(
                    point[axis]
                    for point in triangle
                ) / 3.0
                for axis in range(2)
            )

            self.assertFalse(
                centroid[0] > 2.0 + MESH_TOLERANCE
                and centroid[1] > 2.0 + MESH_TOLERANCE
            )

    def test_complex_concave_profile(self):
        """Verify extrusion of U-shaped, T-shaped and stepped profiles."""

        cases = [
            (
                "U-shaped profile",
                [
                    [0, 0],
                    [8, 0],
                    [8, 8],
                    [6, 8],
                    [6, 2],
                    [2, 2],
                    [2, 8],
                    [0, 8]
                ],
                40.0,
                3.0
            ),
            (
                "T-shaped profile",
                [
                    [3, 0],
                    [5, 0],
                    [5, 6],
                    [8, 6],
                    [8, 8],
                    [0, 8],
                    [0, 6],
                    [3, 6]
                ],
                28.0,
                4.0
            ),
            (
                "Stepped profile",
                [
                    [0, 0],
                    [8, 0],
                    [8, 2],
                    [6, 2],
                    [6, 4],
                    [4, 4],
                    [4, 6],
                    [2, 6],
                    [2, 8],
                    [0, 8]
                ],
                40.0,
                2.5
            ),
            (
                "Cross-shaped profile",
                [
                    [2, 0],
                    [4, 0],
                    [4, 2],
                    [6, 2],
                    [6, 4],
                    [4, 4],
                    [4, 6],
                    [2, 6],
                    [2, 4],
                    [0, 4],
                    [0, 2],
                    [2, 2]
                ],
                20.0,
                5.0
            )
        ]

        for name, profile, area, depth in cases:
            with self.subTest(profile=name):
                self.assert_valid_profile(
                    profile,
                    depth,
                    expected_area=area,
                    name=name
                )

    def test_clockwise_profile(self):
        """Verify extrusion of clockwise-oriented profiles."""

        counterclockwise = [
            [0, 0],
            [6, 0],
            [6, 4],
            [0, 4]
        ]

        clockwise = list(
            reversed(counterclockwise)
        )

        self.assertLess(
            signed_polygon_area(clockwise),
            0.0
        )

        created = self.assert_valid_profile(
            clockwise,
            3.0,
            expected_area=24.0,
            name="Clockwise rectangle"
        )

        domain = created["DOMAIN"]

        mesh_before = self.assert_extrusion_mesh(
            domain,
            clockwise,
            3.0,
            expected_area=24.0
        )

        updated = self.update_geometry(
            domain["id"],
            profile=counterclockwise
        )

        self.assert_identity(
            domain,
            updated
        )

        self.assert_profile(
            updated["profile"],
            counterclockwise
        )

        mesh_after = self.assert_extrusion_mesh(
            updated,
            counterclockwise,
            3.0,
            expected_area=24.0
        )

        self.assert_numeric_value(
            mesh_volume(mesh_before),
            mesh_volume(mesh_after)
        )

        self.assert_numeric_value(
            mesh_surface_area(mesh_before),
            mesh_surface_area(mesh_after)
        )

        self.assert_bounds(
            mesh_after,
            mesh_bounds(mesh_before)
        )

        concave_clockwise = list(reversed([
            [0, 0],
            [6, 0],
            [6, 2],
            [2, 2],
            [2, 5],
            [0, 5]
        ]))

        self.assertLess(
            signed_polygon_area(
                concave_clockwise
            ),
            0.0
        )

        self.assert_valid_profile(
            concave_clockwise,
            2.0,
            expected_area=18.0,
            name="Clockwise concave profile"
        )

    def test_counterclockwise_profile(self):
        """Verify equivalent extrusion from counterclockwise profiles."""

        profile = [
            [0, 0],
            [7, 0],
            [7, 2],
            [3, 2],
            [3, 6],
            [0, 6]
        ]

        reversed_profile = list(
            reversed(profile)
        )

        self.assertGreater(
            signed_polygon_area(profile),
            0.0
        )

        self.assertLess(
            signed_polygon_area(
                reversed_profile
            ),
            0.0
        )

        first = self.assert_valid_profile(
            profile,
            4.0,
            expected_area=26.0,
            name="Counterclockwise concave profile",
            persistence=False
        )

        second = self.assert_valid_profile(
            reversed_profile,
            4.0,
            expected_area=26.0,
            name="Equivalent clockwise profile",
            persistence=False
        )

        first_mesh = self.assert_extrusion_mesh(
            first["DOMAIN"],
            profile,
            4.0,
            expected_area=26.0
        )

        second_mesh = self.assert_extrusion_mesh(
            second["DOMAIN"],
            reversed_profile,
            4.0,
            expected_area=26.0
        )

        self.assert_numeric_value(
            mesh_volume(first_mesh),
            mesh_volume(second_mesh)
        )

        self.assert_numeric_value(
            mesh_surface_area(first_mesh),
            mesh_surface_area(second_mesh)
        )

        self.assert_bounds(
            second_mesh,
            mesh_bounds(first_mesh)
        )

        self.save_and_reload()

        for created, expected_profile in (
            (first, profile),
            (second, reversed_profile)
        ):
            for kind, geometry in created.items():
                reloaded = self.find_geometry(
                    geometry["globalId"]
                )

                self.assert_geometry_definition(
                    reloaded,
                    expected_profile,
                    4.0,
                    kind
                )

                if kind == "DOMAIN":
                    self.assert_extrusion_mesh(
                        reloaded,
                        expected_profile,
                        4.0,
                        expected_area=26.0
                    )

    def test_circle_approximation(self):
        """Verify extrusion of polygonal circle approximations."""

        radius = 5.0
        depth = 3.0

        analytic_area = (
            math.pi * radius ** 2
        )

        analytic_volume = (
            analytic_area * depth
        )

        previous_error = math.inf

        for point_count in (
            16,
            32,
            64,
            128,
            256
        ):
            with self.subTest(
                point_count=point_count
            ):
                profile = regular_polygon(
                    point_count,
                    radius
                )

                expected_area = regular_polygon_area(
                    point_count,
                    radius
                )

                created = self.assert_valid_profile(
                    profile,
                    depth,
                    expected_area=expected_area,
                    name=f"Circle approximation {point_count}",
                    persistence=False
                )

                mesh = self.assert_extrusion_mesh(
                    created["DOMAIN"],
                    profile,
                    depth,
                    expected_area=expected_area
                )

                volume = mesh_volume(mesh)

                self.assertLess(
                    volume,
                    analytic_volume
                )

                error = (
                    analytic_volume - volume
                )

                self.assertLess(
                    error,
                    previous_error,
                    "Circle approximation did not converge"
                )

                previous_error = error

                self.assert_bounds(
                    mesh,
                    (
                        -radius,
                        -radius,
                        0.0,
                        radius,
                        radius,
                        depth
                    )
                )

                for geometry in created.values():
                    self.delete_geometry(
                        geometry["id"]
                    )

        self.assertLess(
            previous_error / analytic_volume,
            0.0002
        )

    def test_negative_coordinates(self):
        """Verify profiles with negative and mixed-sign coordinates."""

        cases = [
            (
                "Entirely negative profile",
                [
                    [-10, -8],
                    [-2, -8],
                    [-2, -3],
                    [-10, -3]
                ],
                40.0,
                2.0
            ),
            (
                "Origin-crossing rectangle",
                [
                    [-4, -3],
                    [6, -3],
                    [6, 5],
                    [-4, 5]
                ],
                80.0,
                4.0
            ),
            (
                "Negative triangle",
                [
                    [-8, -6],
                    [-2, -6],
                    [-5, -1]
                ],
                15.0,
                3.0
            ),
            (
                "Mixed-sign concave profile",
                [
                    [-5, -4],
                    [3, -4],
                    [3, -1],
                    [-2, -1],
                    [-2, 4],
                    [-5, 4]
                ],
                39.0,
                2.5
            )
        ]

        for name, profile, area, depth in cases:
            with self.subTest(profile=name):
                self.assert_valid_profile(
                    profile,
                    depth,
                    expected_area=area,
                    name=name
                )

    def test_fractional_coordinates(self):
        """Verify profiles containing fractional coordinates and depths."""

        cases = [
            (
                "Fractional rectangle",
                [
                    [0.125, 0.25],
                    [2.375, 0.25],
                    [2.375, 1.75],
                    [0.125, 1.75]
                ],
                3.375,
                0.625
            ),
            (
                "Fractional triangle",
                [
                    [0.1, 0.2],
                    [4.6, 0.2],
                    [0.1, 2.7]
                ],
                5.625,
                1.125
            ),
            (
                "Fractional negative rectangle",
                [
                    [-3.75, -2.125],
                    [1.25, -2.125],
                    [1.25, 0.875],
                    [-3.75, 0.875]
                ],
                15.0,
                2.375
            ),
            (
                "Small fractional profile",
                [
                    [0.001, 0.002],
                    [0.011, 0.002],
                    [0.011, 0.022],
                    [0.001, 0.022]
                ],
                0.0002,
                0.03
            )
        ]

        for name, profile, area, depth in cases:
            with self.subTest(profile=name):
                created = self.assert_valid_profile(
                    profile,
                    depth,
                    expected_area=area,
                    name=name
                )

                mesh = self.assert_extrusion_mesh(
                    created["DOMAIN"],
                    profile,
                    depth,
                    expected_area=area
                )

                self.assert_numeric_value(
                    mesh_volume(mesh),
                    area * depth,
                    absolute_tolerance=1e-12,
                    relative_tolerance=1e-5
                )

    def test_collinear_points(self):
        """Verify valid polygons with intermediate collinear vertices."""

        simple_profile = [
            [0, 0],
            [6, 0],
            [6, 4],
            [0, 4]
        ]

        collinear_profile = [
            [0, 0],
            [2, 0],
            [4, 0],
            [6, 0],
            [6, 1],
            [6, 2],
            [6, 4],
            [3, 4],
            [0, 4],
            [0, 2],
            [0, 1]
        ]

        simple = self.assert_valid_profile(
            simple_profile,
            3.0,
            expected_area=24.0,
            name="Rectangle without collinear points",
            persistence=False
        )

        collinear = self.assert_valid_profile(
            collinear_profile,
            3.0,
            expected_area=24.0,
            name="Rectangle with collinear points",
            persistence=False
        )

        simple_mesh = self.assert_extrusion_mesh(
            simple["DOMAIN"],
            simple_profile,
            3.0,
            expected_area=24.0
        )

        collinear_mesh = self.assert_extrusion_mesh(
            collinear["DOMAIN"],
            collinear_profile,
            3.0,
            expected_area=24.0
        )

        self.assert_numeric_value(
            mesh_volume(simple_mesh),
            mesh_volume(collinear_mesh)
        )

        self.assert_numeric_value(
            mesh_surface_area(simple_mesh),
            mesh_surface_area(collinear_mesh)
        )

        self.assert_bounds(
            collinear_mesh,
            mesh_bounds(simple_mesh)
        )

        for kind, geometry in collinear.items():
            updated = self.update_geometry(
                geometry["id"],
                profile=simple_profile
            )

            self.assert_identity(
                geometry,
                updated
            )

            self.assert_geometry_definition(
                updated,
                simple_profile,
                3.0,
                kind
            )

            if kind == "DOMAIN":
                updated_mesh = self.assert_extrusion_mesh(
                    updated,
                    simple_profile,
                    3.0,
                    expected_area=24.0
                )

                self.assert_numeric_value(
                    mesh_volume(updated_mesh),
                    mesh_volume(simple_mesh)
                )

        self.save_and_reload()

        for kind, geometry in collinear.items():
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assert_profile(
                reloaded["profile"],
                simple_profile
            )

        concave_collinear = [
            [0, 0],
            [3, 0],
            [6, 0],
            [6, 1],
            [6, 2],
            [4, 2],
            [2, 2],
            [2, 4],
            [2, 5],
            [0, 5],
            [0, 3]
        ]

        self.assert_valid_profile(
            concave_collinear,
            2.0,
            expected_area=18.0,
            name="Concave profile with collinear points"
        )

    def test_insufficient_points(self):
        """Verify rejection of profiles with fewer than three points."""

        cases = [
            (
                "Empty profile",
                []
            ),
            (
                "Single point",
                [
                    [0, 0]
                ]
            ),
            (
                "Two distinct points",
                [
                    [0, 0],
                    [5, 0]
                ]
            ),
            (
                "Two identical points",
                [
                    [0, 0],
                    [0, 0]
                ]
            )
        ]

        self.assert_invalid_profiles(
            cases,
            "INVALID_PARAMETERS"
        )

        insufficient_distinct_points = [
            (
                "Three identical points",
                [
                    [0, 0],
                    [0, 0],
                    [0, 0]
                ]
            ),
            (
                "Three points with two distinct positions",
                [
                    [0, 0],
                    [5, 0],
                    [0, 0]
                ]
            ),
            (
                "Four points with two distinct positions",
                [
                    [0, 0],
                    [5, 0],
                    [0, 0],
                    [5, 0]
                ]
            )
        ]

        self.assert_invalid_profiles(
            insufficient_distinct_points,
            "INVALID_PROFILE"
        )

    def test_repeated_endpoint(self):
        """Verify rejection of explicitly closed input profiles."""

        cases = [
            (
                "Closed triangle",
                [
                    [0, 0],
                    [4, 0],
                    [0, 3],
                    [0, 0]
                ]
            ),
            (
                "Closed rectangle",
                [
                    [0, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3],
                    [0, 0]
                ]
            ),
            (
                "Closed concave polygon",
                [
                    [0, 0],
                    [6, 0],
                    [6, 2],
                    [2, 2],
                    [2, 5],
                    [0, 5],
                    [0, 0]
                ]
            ),
            (
                "Repeated closing point",
                [
                    [0, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3],
                    [0, 0],
                    [0, 0]
                ]
            ),
            (
                "Closed clockwise rectangle",
                [
                    [0, 0],
                    [0, 3],
                    [5, 3],
                    [5, 0],
                    [0, 0]
                ]
            )
        ]

        self.assert_invalid_profiles(
            cases,
            "INVALID_PROFILE"
        )

        self.assert_valid_profile(
            DEFAULT_PROFILE,
            DEFAULT_DEPTH,
            expected_area=15.0,
            name="Implicitly closed profile"
        )

    def test_duplicate_points(self):
        """Verify rejection of consecutive and repeated internal vertices."""

        cases = [
            (
                "Duplicate first vertex",
                [
                    [0, 0],
                    [0, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3]
                ]
            ),
            (
                "Duplicate middle vertex",
                [
                    [0, 0],
                    [5, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3]
                ]
            ),
            (
                "Duplicate final vertex",
                [
                    [0, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3],
                    [0, 3]
                ]
            ),
            (
                "Multiple consecutive duplicates",
                [
                    [0, 0],
                    [5, 0],
                    [5, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3]
                ]
            ),
            (
                "Duplicate nonconsecutive vertex",
                [
                    [0, 0],
                    [6, 0],
                    [6, 4],
                    [3, 2],
                    [0, 4],
                    [3, 2]
                ]
            ),
            (
                "Duplicate fractional vertex",
                [
                    [0.1, 0.2],
                    [5.1, 0.2],
                    [5.1, 0.2],
                    [5.1, 3.2],
                    [0.1, 3.2]
                ]
            )
        ]

        self.assert_invalid_profiles(
            cases,
            "INVALID_PROFILE"
        )

    def test_zero_area(self):
        """Verify rejection of polygons with zero signed area."""

        cases = [
            (
                "Horizontal collinear triangle",
                [
                    [0, 0],
                    [2, 0],
                    [5, 0]
                ]
            ),
            (
                "Vertical collinear triangle",
                [
                    [0, 0],
                    [0, 2],
                    [0, 5]
                ]
            ),
            (
                "Diagonal collinear triangle",
                [
                    [0, 0],
                    [2, 2],
                    [5, 5]
                ]
            ),
            (
                "Negative collinear triangle",
                [
                    [-5, -5],
                    [-3, -3],
                    [-1, -1]
                ]
            ),
            (
                "Four collinear vertices",
                [
                    [0, 0],
                    [1, 1],
                    [2, 2],
                    [3, 3]
                ]
            ),
            (
                "Backtracking collinear polygon",
                [
                    [0, 0],
                    [5, 0],
                    [2, 0],
                    [8, 0]
                ]
            )
        ]

        for label, profile in cases:
            with self.subTest(case=label):
                self.assertAlmostEqual(
                    signed_polygon_area(profile),
                    0.0,
                    delta=PROFILE_TOLERANCE
                )

        self.assert_invalid_profiles(
            cases,
            "INVALID_PROFILE"
        )

        # A nonzero area must remain valid even if the polygon
        # is substantially thinner than the default rectangle.

        thin_profile = [
            [0, 0],
            [10, 0],
            [10, 0.01],
            [0, 0.01]
        ]

        self.assert_valid_profile(
            thin_profile,
            2.0,
            expected_area=0.1,
            name="Thin nonzero-area profile"
        )

    def test_self_intersection(self):
        """Verify rejection of intersecting and self-touching polygons."""

        cases = [
            (
                "Symmetric bow-tie",
                [
                    [0, 0],
                    [4, 4],
                    [0, 4],
                    [4, 0]
                ]
            ),
            (
                "Asymmetric bow-tie with nonzero signed area",
                [
                    [0, 0],
                    [6, 5],
                    [0, 4],
                    [4, 0]
                ]
            ),
            (
                "Intersecting pentagon",
                [
                    [0, 0],
                    [6, 0],
                    [0, 5],
                    [6, 5],
                    [3, -2]
                ]
            ),
            (
                "Vertex touching a nonadjacent edge",
                [
                    [0, 0],
                    [6, 0],
                    [6, 4],
                    [3, 0],
                    [0, 4]
                ]
            ),
            (
                "Overlapping nonadjacent segments",
                [
                    [0, 0],
                    [6, 0],
                    [6, 4],
                    [2, 4],
                    [2, 0],
                    [4, 0],
                    [4, 2],
                    [0, 2]
                ]
            ),
            (
                "Self-touching polygon",
                [
                    [0, 0],
                    [4, 0],
                    [4, 4],
                    [2, 2],
                    [0, 4],
                    [2, 2]
                ]
            )
        ]

        regular = regular_polygon(
            5,
            5.0
        )

        star = [
            regular[index]
            for index in (
                0,
                2,
                4,
                1,
                3
            )
        ]

        cases.append(
            (
                "Self-intersecting five-point star",
                star
            )
        )

        self.assertGreater(
            abs(signed_polygon_area(cases[1][1])),
            PROFILE_TOLERANCE
        )

        self.assertGreater(
            abs(signed_polygon_area(star)),
            PROFILE_TOLERANCE
        )

        self.assert_invalid_profiles(
            cases,
            "INVALID_PROFILE"
        )

        valid_star_outline = []

        for index in range(10):
            angle = (
                2.0 * math.pi * index / 10
            )

            radius = (
                5.0
                if index % 2 == 0
                else 2.0
            )

            valid_star_outline.append([
                radius * math.cos(angle),
                radius * math.sin(angle)
            ])

        self.assert_valid_profile(
            valid_star_outline,
            2.0,
            name="Simple concave star outline"
        )

    def test_zero_length_segment(self):
        """Verify rejection of zero-length and near-zero-length edges."""

        cases = [
            (
                "Exact zero-length edge",
                [
                    [0, 0],
                    [5, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3]
                ]
            ),
            (
                "Zero-length edge at origin",
                [
                    [0, 0],
                    [0.0, -0.0],
                    [5, 0],
                    [5, 3],
                    [0, 3]
                ]
            ),
            (
                "Near-zero horizontal edge",
                [
                    [0, 0],
                    [1e-13, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3]
                ]
            ),
            (
                "Near-zero vertical edge",
                [
                    [0, 0],
                    [5, 0],
                    [5, 1e-13],
                    [5, 3],
                    [0, 3]
                ]
            ),
            (
                "Near-zero diagonal edge",
                [
                    [0, 0],
                    [1e-13, 1e-13],
                    [5, 0],
                    [5, 3],
                    [0, 3]
                ]
            ),
            (
                "Near-zero closing edge",
                [
                    [0, 0],
                    [5, 0],
                    [5, 3],
                    [0, 3],
                    [0, 1e-13]
                ]
            )
        ]

        self.assert_invalid_profiles(
            cases,
            "INVALID_PROFILE"
        )

        valid_short_edge = [
            [0, 0],
            [0.01, 0],
            [5, 0],
            [5, 3],
            [0, 3]
        ]

        self.assert_valid_profile(
            valid_short_edge,
            2.0,
            expected_area=15.0,
            name="Valid short edge"
        )

    def test_invalid_point_dimension(self):
        """Verify rejection of malformed profile points and coordinate types."""

        cases = [
            (
                "Empty point",
                [
                    [],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "One-dimensional point",
                [
                    [0],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Three-dimensional point",
                [
                    [0, 0, 0],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Four-dimensional point",
                [
                    [0, 0, 0, 1],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Scalar point",
                [
                    0,
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "String point",
                [
                    "0,0",
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Null point",
                [
                    None,
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Object point",
                [
                    {"x": 0, "y": 0},
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Boolean coordinate",
                [
                    [True, 0],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "String coordinate",
                [
                    ["0", 0],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Null coordinate",
                [
                    [None, 0],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Array coordinate",
                [
                    [[0], 0],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Object coordinate",
                [
                    [{"value": 0}, 0],
                    [5, 0],
                    [0, 3]
                ]
            ),
            (
                "Flat coordinate array",
                [
                    0, 0,
                    5, 0,
                    0, 3
                ]
            ),
            (
                "Null profile",
                None
            ),
            (
                "String profile",
                "0,0;5,0;0,3"
            ),
            (
                "Object profile",
                {
                    "points": DEFAULT_PROFILE
                }
            )
        ]

        self.assert_invalid_profiles(
            cases,
            "INVALID_PARAMETERS"
        )

    def test_invalid_depth(self):
        """Verify rejection of zero, negative and malformed extrusion depths."""

        cases = [
            ("Zero depth", 0),
            ("Floating-point zero", 0.0),
            ("Negative zero", -0.0),
            ("Negative depth", -1),
            ("Negative fractional depth", -0.125),
            ("Large negative depth", -1000000),
            ("Null depth", None),
            ("Boolean true depth", True),
            ("Boolean false depth", False),
            ("String depth", "2.0"),
            ("Empty string depth", ""),
            ("Array depth", [2.0]),
            ("Object depth", {"value": 2.0})
        ]

        existing = {
            kind: self.create_geometry(
                profile=DEFAULT_PROFILE,
                depth=DEFAULT_DEPTH,
                kind=kind,
                name=f"Depth validation {kind}"
            )
            for kind in GEOMETRY_KINDS
        }

        initial = self.snapshot_geometries()
        parent_before = self.get_parent()

        for label, depth in cases:
            for kind in GEOMETRY_KINDS:
                with self.subTest(
                    case=label,
                    kind=kind
                ):
                    self.client.expect_error(
                        "geometryCreate",
                        self.geometry_create_params(
                            profile=DEFAULT_PROFILE,
                            depth=depth,
                            kind=kind,
                            name=f"Invalid depth {kind}"
                        ),
                        "INVALID_PARAMETERS"
                    )

                    self.assertEqual(
                        self.snapshot_geometries(),
                        initial
                    )

                    self.assertEqual(
                        self.get_parent(),
                        parent_before
                    )

                    self.client.expect_error(
                        "geometryUpdate",
                        {
                            "id": existing[kind]["id"],
                            "depth": depth
                        },
                        "INVALID_PARAMETERS"
                    )

                    self.assertEqual(
                        self.snapshot_geometries(),
                        initial
                    )

                    self.assertEqual(
                        self.get_parent(),
                        parent_before
                    )

        self.assert_extrusion_mesh(
            existing["DOMAIN"],
            DEFAULT_PROFILE,
            DEFAULT_DEPTH,
            expected_area=15.0
        )

        valid_depths = (
            0.001,
            0.125,
            1.0,
            5.75,
            100.0
        )

        for depth in valid_depths:
            for kind, geometry in existing.items():
                with self.subTest(
                    valid_depth=depth,
                    kind=kind
                ):
                    updated = self.update_geometry(
                        geometry["id"],
                        depth=depth
                    )

                    self.assert_identity(
                        geometry,
                        updated
                    )

                    self.assert_geometry_definition(
                        updated,
                        DEFAULT_PROFILE,
                        depth,
                        kind
                    )

                    if kind == "DOMAIN":
                        self.assert_extrusion_mesh(
                            updated,
                            DEFAULT_PROFILE,
                            depth,
                            expected_area=15.0
                        )

        self.save_and_reload()

        for kind, geometry in existing.items():
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assert_geometry_definition(
                reloaded,
                DEFAULT_PROFILE,
                valid_depths[-1],
                kind
            )

    def test_nonfinite_coordinates(self):
        """Verify rejection of non-finite and unrepresentable coordinates."""

        # NaN and Infinity are invalid JSON number tokens.
        # Exponents exceeding the double range are syntactically
        # valid JSON numbers but are rejected by the JSON parser.
        # Both cases must return INVALID_JSON with a null response ID.

        invalid_tokens = [
            "NaN",
            "Infinity",
            "-Infinity",
            "1e309",
            "-1e309",
            "1e9999",
            "-1e9999",
            "9.999999999999999e9999"
        ]

        sentinel = "__INVALID_NUMERIC_TOKEN__"

        existing = {
            kind: self.create_geometry(
                profile=DEFAULT_PROFILE,
                kind=kind,
                name=f"Nonfinite validation {kind}"
            )
            for kind in GEOMETRY_KINDS
        }

        initial = self.snapshot_geometries()
        parent_before = self.get_parent()

        for token in invalid_tokens:
            for axis in (0, 1):
                for kind in GEOMETRY_KINDS:
                    with self.subTest(
                        token=token,
                        axis=axis,
                        kind=kind
                    ):
                        profile = [
                            [0, 0],
                            [5, 0],
                            [5, 3],
                            [0, 3]
                        ]

                        profile[1][axis] = sentinel

                        commands = [
                            (
                                "geometryCreate",
                                self.geometry_create_params(
                                    profile=profile,
                                    kind=kind,
                                    name="Rejected nonfinite profile"
                                )
                            ),
                            (
                                "geometryUpdate",
                                {
                                    "id": existing[kind]["id"],
                                    "profile": profile
                                }
                            )
                        ]

                        for command, params in commands:
                            request = {
                                "id": self.client.next_request_id(),
                                "command": command,
                                "params": params
                            }

                            payload = json.dumps(
                                request,
                                allow_nan=False
                            )

                            payload = payload.replace(
                                json.dumps(sentinel),
                                token
                            )

                            self.client.expect_raw_error(
                                payload,
                                expected_code="INVALID_JSON",
                                expected_id=None
                            )

                            self.assertEqual(
                                self.snapshot_geometries(),
                                initial
                            )

                            self.assertEqual(
                                self.get_parent(),
                                parent_before
                            )

        # Non-finite extrusion depths must also be rejected
        # before reaching the geometry implementation.

        for token in invalid_tokens:
            for kind in GEOMETRY_KINDS:
                with self.subTest(
                    depth_token=token,
                    kind=kind
                ):
                    commands = [
                        (
                            "geometryCreate",
                            self.geometry_create_params(
                                profile=DEFAULT_PROFILE,
                                depth=sentinel,
                                kind=kind
                            )
                        ),
                        (
                            "geometryUpdate",
                            {
                                "id": existing[kind]["id"],
                                "depth": sentinel
                            }
                        )
                    ]

                    for command, params in commands:
                        request = {
                            "id": self.client.next_request_id(),
                            "command": command,
                            "params": params
                        }

                        payload = json.dumps(
                            request,
                            allow_nan=False
                        ).replace(
                            json.dumps(sentinel),
                            token
                        )

                        self.client.expect_raw_error(
                            payload,
                            expected_code="INVALID_JSON",
                            expected_id=None
                        )

                        self.assertEqual(
                            self.snapshot_geometries(),
                            initial
                        )

        # String representations of non-finite numbers are
        # valid JSON but invalid profile coordinate types.

        for value in (
            "NaN",
            "Infinity",
            "-Infinity",
            "1e309"
        ):
            for kind in GEOMETRY_KINDS:
                with self.subTest(
                    string_value=value,
                    kind=kind
                ):
                    profile = [
                        [0, 0],
                        [value, 0],
                        [0, 3]
                    ]

                    self.client.expect_error(
                        "geometryCreate",
                        self.geometry_create_params(
                            profile=profile,
                            kind=kind
                        ),
                        "INVALID_PARAMETERS"
                    )

                    self.client.expect_error(
                        "geometryUpdate",
                        {
                            "id": existing[kind]["id"],
                            "profile": profile
                        },
                        "INVALID_PARAMETERS"
                    )

                    self.assertEqual(
                        self.snapshot_geometries(),
                        initial
                    )

        self.assert_extrusion_mesh(
            existing["DOMAIN"],
            DEFAULT_PROFILE,
            DEFAULT_DEPTH,
            expected_area=15.0
        )

        capabilities = self.client.request(
            "getCapabilities"
        )

        self.assertIn(
            "supportedGeometryKinds",
            capabilities
        )

        self.save_and_reload()

        for kind, geometry in existing.items():
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.assert_geometry_definition(
                reloaded,
                DEFAULT_PROFILE,
                DEFAULT_DEPTH,
                kind
            )

    def test_large_profile(self):
        """Verify extrusion and tessellation of profiles with thousands of vertices."""

        self.client.timeout = LARGE_PROFILE_TIMEOUT

        radius = 100.0
        depth = 5.0

        # The larger radius keeps adjacent vertices sufficiently
        # separated from the geometric kernel's modeling tolerance.

        point_counts = (
            1024,
            2048,
            4096
        )

        previous_error = math.inf

        analytic_volume = (
            math.pi * radius ** 2 * depth
        )

        initial_geometry_count = len(
            self.list_geometries()
        )

        for point_count in point_counts:
            with self.subTest(
                point_count=point_count
            ):
                profile = regular_polygon(
                    point_count,
                    radius
                )

                expected_area = regular_polygon_area(
                    point_count,
                    radius
                )

                expected_volume = (
                    expected_area * depth
                )

                self.assertEqual(
                    len(profile),
                    point_count
                )

                self.assertEqual(
                    len({
                        tuple(point)
                        for point in profile
                    }),
                    point_count
                )

                self.assertGreater(
                    signed_polygon_area(profile),
                    0.0
                )

                created = self.assert_valid_profile(
                    profile,
                    depth,
                    expected_area=expected_area,
                    name=f"Large polygon {point_count}",
                    persistence=False,
                    check_polygon=False,
                    check_corners=False
                )

                # Avoid quadratic Python point-in-polygon and
                # nearest-corner checks for large profiles.
                # Bounds, topology, volume, surface area and
                # both cap areas are still validated.

                domain = created["DOMAIN"]

                mesh = self.assert_extrusion_mesh(
                    domain,
                    profile,
                    depth,
                    expected_area=expected_area,
                    check_polygon=False,
                    check_corners=False
                )

                self.assert_numeric_value(
                    mesh_volume(mesh),
                    expected_volume
                )

                self.assertGreaterEqual(
                    mesh["vertexCount"],
                    point_count * 2
                )

                # A simple n-sided prism has 2n lateral
                # triangles and 2(n-2) cap triangles.

                minimum_triangle_count = (
                    4 * point_count - 4
                )

                self.assertGreaterEqual(
                    mesh["indexCount"] // 3,
                    minimum_triangle_count
                )

                error = abs(
                    analytic_volume - mesh_volume(mesh)
                )

                self.assertLess(
                    error,
                    previous_error
                )

                previous_error = error

                self.assert_bounds(
                    mesh,
                    (
                        -radius,
                        -radius,
                        0.0,
                        radius,
                        radius,
                        depth
                    )
                )

                self.assertEqual(
                    len(self.list_geometries()),
                    initial_geometry_count + 2
                )

                self.save_and_reload()

                for kind, geometry in created.items():
                    reloaded = self.find_geometry(
                        geometry["globalId"]
                    )

                    self.assert_geometry_definition(
                        reloaded,
                        profile,
                        depth,
                        kind
                    )

                    self.assertEqual(
                        len(reloaded["profile"]),
                        point_count
                    )

                    if kind == "DOMAIN":
                        reloaded_mesh = self.assert_extrusion_mesh(
                            reloaded,
                            profile,
                            depth,
                            expected_area=expected_area,
                            check_polygon=False,
                            check_corners=False
                        )

                        self.assert_numeric_value(
                            mesh_volume(reloaded_mesh),
                            mesh_volume(mesh)
                        )

                        self.assert_numeric_value(
                            mesh_surface_area(reloaded_mesh),
                            mesh_surface_area(mesh)
                        )

                        updated_depth = 2.5

                        updated = self.update_geometry(
                            reloaded["id"],
                            depth=updated_depth
                        )

                        self.assert_identity(
                            reloaded,
                            updated
                        )

                        self.assert_profile(
                            updated["profile"],
                            profile
                        )

                        updated_mesh = self.assert_extrusion_mesh(
                            updated,
                            profile,
                            updated_depth,
                            expected_area=expected_area,
                            check_polygon=False,
                            check_corners=False
                        )

                        self.assert_numeric_value(
                            mesh_volume(updated_mesh),
                            expected_area * updated_depth
                        )

                    self.delete_geometry(
                        reloaded["id"]
                    )

                self.assertEqual(
                    len(self.list_geometries()),
                    initial_geometry_count
                )

        self.assertLess(
            previous_error / analytic_volume,
            1e-6
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.save_and_reload()

        self.assertEqual(
            len(self.list_geometries()),
            initial_geometry_count
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        # Verify that the session remains usable after processing
        # and deleting all large polygonal extrusions.

        recovery_profile = [
            [0, 0],
            [4, 0],
            [0, 3]
        ]

        recovery = self.create_geometry(
            profile=recovery_profile,
            depth=2.0,
            kind="DOMAIN",
            name="Large profile recovery"
        )

        self.assert_extrusion_mesh(
            recovery,
            recovery_profile,
            2.0,
            expected_area=6.0
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