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
#
# The physical result is defined as:
# (BASE union ADD geometries) minus SUBTRACT geometries.
#
# DOMAIN geometries must never participate in physical boolean operations.
#
# The current CLI delegates physical geometry processing to IfcOpenShell.
# In particular, ADD operations and subtraction from additive material
# may expose limitations in the current implementation.
# These tests intentionally enforce the expected boolean semantics
# rather than accepting incomplete geometry processing.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 60

MESH_TOLERANCE = 1e-6
VOLUME_TOLERANCE = 1e-5
SURFACE_TOLERANCE = 1e-5

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

PARENT_GLOBAL_ID = "0000000000000000000001"
PROJECT_GLOBAL_ID = "0000000000000000000002"

BASE_WIDTH = 10.0
BASE_LENGTH = 8.0
BASE_DEPTH = 6.0

BASE_VOLUME = BASE_WIDTH * BASE_LENGTH * BASE_DEPTH

BASE_SURFACE_AREA = 2.0 * (
    BASE_WIDTH * BASE_LENGTH
    + BASE_WIDTH * BASE_DEPTH
    + BASE_LENGTH * BASE_DEPTH
)

BASE_BOUNDS = (
    0.0, 0.0, 0.0,
    10.0, 8.0, 6.0
)

IDENTITY_MATRIX = [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1
]

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('boolean-operations-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#9=IFCCARTESIANPOINT((5.,4.));
#10=IFCAXIS2PLACEMENT2D(#9,$);
#11=IFCRECTANGLEPROFILEDEF(.AREA.,$,#10,10.,8.);
#12=IFCEXTRUDEDAREASOLID(#11,#5,#2,6.);
#13=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#12));
#14=IFCPRODUCTDEFINITIONSHAPE($,$,(#13));
#15=IFCBUILDINGELEMENTPROXY('0000000000000000000001',$,'Boolean test solid',$,$,#4,#14,$,.NOTDEFINED.);
#16=IFCPROJECT('0000000000000000000002',$,'Boolean operations test project',$,$,$,$,(#6),#8);
ENDSEC;
END-ISO-10303-21;
"""


def rectangular_profile(width, length):
    return [
        [0, 0],
        [width, 0],
        [width, length],
        [0, length]
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


def multiply_matrices(left, right):
    return [
        sum(
            left[row * 4 + index] * right[index * 4 + column]
            for index in range(4)
        )
        for row in range(4)
        for column in range(4)
    ]


def compose_matrices(*matrices):
    result = IDENTITY_MATRIX.copy()

    for matrix in matrices:
        result = multiply_matrices(result, matrix)

    return result


def transform_input(matrix=None, space="parent"):
    return {
        "space": space,
        "matrix": (
            IDENTITY_MATRIX.copy()
            if matrix is None
            else matrix
        )
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


def mesh_signed_volume(mesh):
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

    return math.fsum(terms) / 6.0


def mesh_volume(mesh):
    return abs(mesh_signed_volume(mesh))


def mesh_surface_area(mesh):
    return math.fsum(
        triangle_area(*triangle)
        for triangle in mesh_triangles(mesh)
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


def mesh_planar_area(
    mesh,
    axis,
    coordinate,
    tolerance=MESH_TOLERANCE
):
    return math.fsum(
        triangle_area(*triangle)
        for triangle in mesh_triangles(mesh)
        if all(
            abs(point[axis] - coordinate) <= tolerance
            for point in triangle
        )
    )


def mesh_signature(mesh, precision=7):
    return Counter(
        tuple(sorted(
            tuple(
                round(value, precision)
                for value in point
            )
            for point in triangle
        ))
        for triangle in mesh_triangles(mesh)
    )


def mesh_edge_counts(mesh, precision=7):
    undirected_edges = Counter()
    directed_edges = Counter()

    for triangle in mesh_triangles(mesh):
        points = [
            tuple(
                round(value, precision)
                for value in point
            )
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
            undirected_edges[
                tuple(sorted((first, second)))
            ] += 1

            directed_edges[
                (first, second)
            ] += 1

    return undirected_edges, directed_edges


def mesh_contains_point(mesh, point):
    # Ray casting with a non-axis-aligned direction reduces
    # intersections with shared triangle edges and vertices.
    # Test points must not lie directly on the mesh boundary.

    direction = (
        1.0,
        0.3713906763541037,
        0.5831734928178924
    )

    intersections = []

    for a, b, c in mesh_triangles(mesh):
        edge1 = subtract_vectors(b, a)
        edge2 = subtract_vectors(c, a)

        h = cross_product(direction, edge2)
        determinant = dot_product(edge1, h)

        if abs(determinant) <= 1e-12:
            continue

        inverse = 1.0 / determinant
        s = subtract_vectors(point, a)

        u = inverse * dot_product(s, h)

        if u < -1e-9 or u > 1.0 + 1e-9:
            continue

        q = cross_product(s, edge1)

        v = inverse * dot_product(direction, q)

        if v < -1e-9 or u + v > 1.0 + 1e-9:
            continue

        distance = inverse * dot_product(edge2, q)

        if distance > MESH_TOLERANCE:
            intersections.append(distance)

    intersections.sort()

    unique_intersections = []

    for distance in intersections:
        if (
            not unique_intersections
            or abs(
                distance - unique_intersections[-1]
            ) > MESH_TOLERANCE
        ):
            unique_intersections.append(distance)

    return len(unique_intersections) % 2 == 1


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
        expected_code
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
        space="local",
        geometry_id=None,
        include_children=False,
        include_normals=True
    ):
        options = {
            "space": space,
            "kind": kind,
            "includeNormals": include_normals,
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

            if (
                event_name == "mesh.normals"
                and not include_normals
            ):
                raise AssertionError(
                    "Unexpected mesh.normals event"
                )

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


class BooleanOperationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-boolean-operations-"
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

        self.baseline_mesh = self.physical_mesh()

        self.assert_mesh(
            self.baseline_mesh,
            expected_volume=BASE_VOLUME,
            expected_bounds=BASE_BOUNDS,
            expected_surface=BASE_SURFACE_AREA
        )

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

    def get_parent(self):
        return self.client.request(
            "transformGet",
            {"id": self.parent_id}
        )

    def list_geometries(self, kind=None):
        params = {
            "transformId": self.parent_id
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

    def create_geometry(
        self,
        operation="ADD",
        kind="SOLID",
        name="Boolean geometry",
        profile=None,
        depth=1.0,
        matrix=None
    ):
        profile = (
            rectangular_profile(1.0, 1.0)
            if profile is None
            else profile
        )

        matrix = (
            IDENTITY_MATRIX.copy()
            if matrix is None
            else matrix
        )

        geometry = self.client.request(
            "geometryCreate",
            {
                "parentId": self.parent_id,
                "name": name,
                "kind": kind,
                "operation": operation,
                "profile": profile,
                "depth": depth,
                "transform": transform_input(matrix)
            }
        )

        self.assertEqual(
            geometry["parentId"],
            self.parent_id
        )

        self.assertEqual(
            geometry["source"],
            "editor"
        )

        self.assertIs(
            geometry["editable"],
            True
        )

        self.assertEqual(
            geometry["operation"],
            operation
        )

        self.assertEqual(
            geometry["kind"],
            kind
        )

        self.assertEqual(
            geometry["name"],
            name
        )

        self.assert_profile(
            geometry["profile"],
            profile
        )

        self.assertAlmostEqual(
            geometry["depth"],
            depth,
            delta=MESH_TOLERANCE
        )

        self.assert_matrix(
            geometry["transforms"]["local"],
            matrix
        )

        expected_class = (
            "IfcSpatialZone"
            if kind == "DOMAIN"
            else "IfcProjectionElement"
            if operation == "ADD"
            else "IfcOpeningElement"
        )

        self.assertEqual(
            geometry["ifcClass"],
            expected_class
        )

        self.assertIn(
            geometry["id"],
            self.get_parent()["geometries"]
        )

        return geometry

    def create_box(
        self,
        operation="ADD",
        kind="SOLID",
        name="Boolean box",
        x=0.0,
        y=0.0,
        z=0.0,
        width=1.0,
        length=1.0,
        depth=1.0
    ):
        return self.create_geometry(
            operation=operation,
            kind=kind,
            name=name,
            profile=rectangular_profile(
                width,
                length
            ),
            depth=depth,
            matrix=translation_matrix(
                x,
                y,
                z
            )
        )

    def update_geometry(
        self,
        identifier,
        **changes
    ):
        updated = self.client.request(
            "geometryUpdate",
            {
                "id": identifier,
                **changes
            }
        )

        self.assertEqual(
            updated["id"],
            identifier
        )

        self.assertEqual(
            updated["parentId"],
            self.parent_id
        )

        self.assertEqual(
            self.get_geometry(identifier),
            updated
        )

        return updated

    def delete_geometry(self, identifier):
        result = self.client.request(
            "geometryDelete",
            {"id": identifier}
        )

        self.assertEqual(
            result,
            {"id": identifier}
        )

        self.assertNotIn(
            identifier,
            self.get_parent()["geometries"]
        )

        self.client.expect_error(
            "geometryGet",
            {"id": identifier},
            "ENTITY_NOT_FOUND"
        )

    def physical_mesh(
        self,
        space="local",
        include_children=False
    ):
        return self.client.tessellate(
            self.parent_id,
            kind="SOLID",
            space=space,
            include_children=include_children
        )

    def domain_mesh(
        self,
        geometry_id=None,
        space="local"
    ):
        return self.client.tessellate(
            self.parent_id,
            kind="DOMAIN",
            space=space,
            geometry_id=geometry_id
        )

    def assert_matrix(self, actual, expected):
        self.assertEqual(
            len(actual),
            16
        )

        self.assertEqual(
            len(expected),
            16
        )

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
                delta=MESH_TOLERANCE,
                msg=f"Invalid matrix component {index}"
            )

    def assert_profile(self, actual, expected):
        self.assertEqual(
            len(actual),
            len(expected)
        )

        for point, reference in zip(actual, expected):
            self.assertEqual(
                len(point),
                2
            )

            for value, expected_value in zip(
                point,
                reference
            ):
                self.assertAlmostEqual(
                    value,
                    expected_value,
                    delta=MESH_TOLERANCE
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
        undirected, directed = mesh_edge_counts(mesh)

        self.assertTrue(
            undirected,
            "Mesh contains no edges"
        )

        invalid_edges = {
            edge: count
            for edge, count in undirected.items()
            if count != 2
        }

        self.assertFalse(
            invalid_edges,
            f"Open or non-manifold mesh edges: "
            f"{list(invalid_edges.items())[:10]}"
        )

        invalid_orientations = [
            (first, second)
            for first, second in undirected
            if (
                directed[(first, second)] != 1
                or directed[(second, first)] != 1
            )
        ]

        self.assertFalse(
            invalid_orientations,
            f"Inconsistent triangle winding: "
            f"{invalid_orientations[:10]}"
        )

    def assert_normals(self, mesh):
        normals = mesh["normals"]

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

    def assert_empty_mesh(self, mesh):
        self.assertEqual(
            mesh["vertexCount"],
            0
        )

        self.assertEqual(
            mesh["indexCount"],
            0
        )

        self.assertEqual(
            mesh["vertices"],
            []
        )

        self.assertEqual(
            mesh["indices"],
            []
        )

        self.assertEqual(
            mesh["normals"],
            []
        )

        self.assertEqual(
            mesh_volume(mesh),
            0.0
        )

    def assert_mesh(
        self,
        mesh,
        expected_volume,
        expected_bounds=None,
        expected_surface=None,
        check_topology=True
    ):
        if expected_volume == 0:
            self.assert_empty_mesh(mesh)
            return

        self.assertGreater(
            mesh["vertexCount"],
            0
        )

        self.assertGreater(
            mesh["indexCount"],
            0
        )

        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected_volume,
            delta=VOLUME_TOLERANCE,
            msg=(
                f"Incorrect boolean volume: "
                f"expected {expected_volume}, "
                f"received {mesh_volume(mesh)}"
            )
        )

        if expected_bounds is not None:
            self.assert_bounds(
                mesh,
                expected_bounds
            )

        if expected_surface is not None:
            self.assertAlmostEqual(
                mesh_surface_area(mesh),
                expected_surface,
                delta=SURFACE_TOLERANCE
            )

        for triangle in mesh_triangles(mesh):
            self.assertGreater(
                triangle_area(*triangle),
                1e-12,
                f"Degenerate triangle: {triangle}"
            )

        if check_topology:
            self.assert_closed_mesh(mesh)

        self.assert_normals(mesh)

    def assert_physical_result(
        self,
        expected_volume,
        expected_bounds=None,
        expected_surface=None,
        inside_points=None,
        outside_points=None
    ):
        mesh = self.physical_mesh()

        self.assert_mesh(
            mesh,
            expected_volume=expected_volume,
            expected_bounds=expected_bounds,
            expected_surface=expected_surface
        )

        self.assert_occupancy(
            mesh,
            inside_points=inside_points,
            outside_points=outside_points
        )

        return mesh

    def assert_occupancy(
        self,
        mesh,
        inside_points=None,
        outside_points=None
    ):
        for point in inside_points or []:
            with self.subTest(
                point=point,
                expected="inside"
            ):
                self.assertTrue(
                    mesh_contains_point(
                        mesh,
                        point
                    ),
                    f"Expected physical material at {point}"
                )

        for point in outside_points or []:
            with self.subTest(
                point=point,
                expected="outside"
            ):
                self.assertFalse(
                    mesh_contains_point(
                        mesh,
                        point
                    ),
                    f"Unexpected physical material at {point}"
                )

    def assert_equivalent_meshes(
        self,
        first,
        second,
        exact_triangles=False
    ):
        self.assertAlmostEqual(
            mesh_volume(first),
            mesh_volume(second),
            delta=VOLUME_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_surface_area(first),
            mesh_surface_area(second),
            delta=SURFACE_TOLERANCE
        )

        if first["indexCount"] == 0:
            self.assert_empty_mesh(second)
            return

        self.assert_bounds(
            second,
            mesh_bounds(first)
        )

        # Boolean kernels may change triangulation without
        # changing the underlying physical solid.

        if exact_triangles:
            self.assertEqual(
                mesh_signature(first),
                mesh_signature(second)
            )

    def assert_baseline_restored(self):
        restored = self.assert_physical_result(
            expected_volume=BASE_VOLUME,
            expected_bounds=BASE_BOUNDS,
            expected_surface=BASE_SURFACE_AREA,
            inside_points=[
                (0.5, 0.5, 0.5),
                (5.0, 4.0, 3.0),
                (9.5, 7.5, 5.5)
            ],
            outside_points=[
                (-1.0, 4.0, 3.0),
                (11.0, 4.0, 3.0),
                (5.0, 9.0, 3.0),
                (5.0, 4.0, 7.0)
            ]
        )

        self.assert_equivalent_meshes(
            self.baseline_mesh,
            restored
        )

        return restored

    def assert_geometry_identity(
        self,
        before,
        after
    ):
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

    def assert_operation_counts(
        self,
        additions=0,
        subtractions=0,
        domains=0
    ):
        geometries = self.list_geometries()

        actual = Counter(
            (
                geometry["kind"],
                geometry["operation"]
            )
            for geometry in geometries
        )

        expected = Counter({
            ("SOLID", "BASE"): 1,
            ("SOLID", "ADD"): additions,
            ("SOLID", "SUBTRACT"): subtractions,
            ("DOMAIN", "ADD"): domains
        })

        expected = Counter({
            key: count
            for key, count in expected.items()
            if count > 0
        })

        self.assertEqual(
            actual,
            expected
        )

        geometry_ids = [
            geometry["id"]
            for geometry in geometries
        ]

        self.assertEqual(
            len(geometry_ids),
            len(set(geometry_ids))
        )

        parent_ids = self.get_parent()["geometries"]

        self.assertEqual(
            len(parent_ids),
            len(set(parent_ids))
        )

        self.assertEqual(
            set(parent_ids),
            set(geometry_ids)
        )

    def test_additive_geometry(self):
        """Verify that an additive extrusion increases the physical volume."""

        # Base volume: 480.
        # Addition dimensions: 2 x 3 x 4.
        # One unit of extrusion overlaps the base.
        # Added physical volume: 2 x 3 x 3 = 18.

        addition = self.create_box(
            operation="ADD",
            name="Additive protrusion",
            x=2.0,
            y=2.0,
            z=5.0,
            width=2.0,
            length=3.0,
            depth=4.0
        )

        mesh = self.assert_physical_result(
            expected_volume=498.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 9
            ),
            expected_surface=406.0,
            inside_points=[
                (3.0, 3.0, 7.5),
                (3.0, 3.0, 5.5),
                (8.0, 6.0, 3.0)
            ],
            outside_points=[
                (1.0, 3.0, 7.5),
                (5.0, 3.0, 7.5),
                (3.0, 6.0, 7.5),
                (3.0, 3.0, 9.5)
            ]
        )

        self.assertAlmostEqual(
            mesh_volume(mesh) - BASE_VOLUME,
            18.0,
            delta=VOLUME_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=6.0
            ),
            74.0,
            delta=SURFACE_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=9.0
            ),
            6.0,
            delta=SURFACE_TOLERANCE
        )

        self.assert_operation_counts(
            additions=1
        )

        self.delete_geometry(addition["id"])

        self.assert_baseline_restored()

    def test_subtractive_geometry(self):
        """Verify that a subtractive extrusion reduces the physical volume."""

        # The subtraction enters through the right side of the base.
        # Intersection dimensions: 2 x 3 x 3.
        # Removed physical volume: 18.

        subtraction = self.create_box(
            operation="SUBTRACT",
            name="Side subtraction",
            x=8.0,
            y=2.0,
            z=1.0,
            width=4.0,
            length=3.0,
            depth=3.0
        )

        mesh = self.assert_physical_result(
            expected_volume=462.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=400.0,
            inside_points=[
                (7.5, 3.5, 2.5),
                (9.0, 1.0, 2.5),
                (9.0, 3.5, 5.0)
            ],
            outside_points=[
                (9.0, 3.5, 2.5),
                (9.5, 4.0, 3.0),
                (11.0, 3.5, 2.5)
            ]
        )

        self.assertAlmostEqual(
            BASE_VOLUME - mesh_volume(mesh),
            18.0,
            delta=VOLUME_TOLERANCE
        )

        self.assert_operation_counts(
            subtractions=1
        )

        self.delete_geometry(
            subtraction["id"]
        )

        self.assert_baseline_restored()

    def test_through_opening(self):
        """Verify an opening passing completely through the solid."""

        # Opening area: 2 x 3 = 6.
        # Intersection depth: 6.
        # Removed physical volume: 36.

        opening = self.create_box(
            operation="SUBTRACT",
            name="Through opening",
            x=2.0,
            y=2.0,
            z=-1.0,
            width=2.0,
            length=3.0,
            depth=8.0
        )

        mesh = self.assert_physical_result(
            expected_volume=444.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=424.0,
            inside_points=[
                (1.0, 3.5, 3.0),
                (5.0, 3.5, 3.0),
                (3.0, 1.0, 3.0),
                (3.0, 6.0, 3.0)
            ],
            outside_points=[
                (3.0, 3.5, 0.25),
                (3.0, 3.5, 2.0),
                (3.0, 3.5, 4.0),
                (3.0, 3.5, 5.75)
            ]
        )

        for coordinate in (0.0, 6.0):
            with self.subTest(
                cap=coordinate
            ):
                self.assertAlmostEqual(
                    mesh_planar_area(
                        mesh,
                        axis=2,
                        coordinate=coordinate
                    ),
                    74.0,
                    delta=SURFACE_TOLERANCE
                )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=3.0
            ),
            0.0,
            delta=SURFACE_TOLERANCE
        )

        self.assert_operation_counts(
            subtractions=1
        )

        self.save_and_reload()

        reloaded = self.find_geometry(
            opening["globalId"]
        )

        self.assertEqual(
            reloaded["operation"],
            "SUBTRACT"
        )

        self.assert_equivalent_meshes(
            mesh,
            self.assert_physical_result(
                expected_volume=444.0,
                expected_bounds=BASE_BOUNDS,
                expected_surface=424.0
            )
        )

        self.delete_geometry(reloaded["id"])

        self.assert_baseline_restored()

    def test_blind_opening(self):
        """Verify a non-through cavity with a remaining bottom surface."""

        # Opening dimensions: 3 x 2.
        # Intersection depth: 2.
        # Removed volume: 12.
        # The material below z=4 must remain intact.

        opening = self.create_box(
            operation="SUBTRACT",
            name="Blind opening",
            x=2.0,
            y=2.0,
            z=4.0,
            width=3.0,
            length=2.0,
            depth=4.0
        )

        mesh = self.assert_physical_result(
            expected_volume=468.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=396.0,
            inside_points=[
                (3.5, 3.0, 0.5),
                (3.5, 3.0, 2.0),
                (3.5, 3.0, 3.75),
                (1.0, 3.0, 5.0),
                (6.0, 3.0, 5.0)
            ],
            outside_points=[
                (3.5, 3.0, 4.25),
                (3.5, 3.0, 5.0),
                (3.5, 3.0, 5.75)
            ]
        )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=0.0
            ),
            80.0,
            delta=SURFACE_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=6.0
            ),
            74.0,
            delta=SURFACE_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=4.0
            ),
            6.0,
            delta=SURFACE_TOLERANCE
        )

        self.assert_operation_counts(
            subtractions=1
        )

        self.delete_geometry(opening["id"])

        self.assert_baseline_restored()

    def test_multiple_additions(self):
        """Verify multiple independent additive operations."""

        first = self.create_box(
            operation="ADD",
            name="First addition",
            x=1.0,
            y=1.0,
            z=5.0,
            width=2.0,
            length=2.0,
            depth=3.0
        )

        # Added volume: 2 x 2 x 2 = 8.

        self.assert_physical_result(
            expected_volume=488.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            ),
            expected_surface=392.0
        )

        second = self.create_box(
            operation="ADD",
            name="Second addition",
            x=6.0,
            y=4.0,
            z=5.0,
            width=3.0,
            length=2.0,
            depth=4.0
        )

        # Additional volume: 3 x 2 x 3 = 18.

        self.assert_physical_result(
            expected_volume=506.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 9
            ),
            expected_surface=422.0,
            inside_points=[
                (2.0, 2.0, 7.0),
                (7.5, 5.0, 8.0),
                (5.0, 4.0, 3.0)
            ],
            outside_points=[
                (4.0, 3.0, 7.0),
                (5.0, 5.0, 8.0)
            ]
        )

        self.assert_operation_counts(
            additions=2
        )

        self.delete_geometry(first["id"])

        self.assert_physical_result(
            expected_volume=498.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 9
            ),
            expected_surface=406.0,
            inside_points=[
                (7.5, 5.0, 8.0)
            ],
            outside_points=[
                (2.0, 2.0, 7.0)
            ]
        )

        self.assert_operation_counts(
            additions=1
        )

        self.delete_geometry(second["id"])

        self.assert_baseline_restored()

    def test_multiple_subtractions(self):
        """Verify multiple independent subtractive operations."""

        first = self.create_box(
            operation="SUBTRACT",
            name="First through opening",
            x=1.0,
            y=1.0,
            z=-1.0,
            width=2.0,
            length=2.0,
            depth=8.0
        )

        # Removed volume: 2 x 2 x 6 = 24.

        self.assert_physical_result(
            expected_volume=456.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=416.0
        )

        second = self.create_box(
            operation="SUBTRACT",
            name="Second blind opening",
            x=6.0,
            y=4.0,
            z=4.0,
            width=3.0,
            length=2.0,
            depth=4.0
        )

        # Additional removed volume: 3 x 2 x 2 = 12.

        self.assert_physical_result(
            expected_volume=444.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=436.0,
            inside_points=[
                (7.5, 5.0, 2.0),
                (5.0, 3.0, 3.0),
                (9.5, 7.5, 5.5)
            ],
            outside_points=[
                (2.0, 2.0, 1.0),
                (2.0, 2.0, 5.0),
                (7.5, 5.0, 5.0)
            ]
        )

        self.assert_operation_counts(
            subtractions=2
        )

        self.delete_geometry(first["id"])

        self.assert_physical_result(
            expected_volume=468.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=396.0,
            inside_points=[
                (2.0, 2.0, 3.0)
            ],
            outside_points=[
                (7.5, 5.0, 5.0)
            ]
        )

        self.delete_geometry(second["id"])

        self.assert_baseline_restored()

    def test_overlapping_additions(self):
        """Verify that overlapping additive material is counted only once."""

        first = self.create_box(
            operation="ADD",
            name="First overlapping addition",
            x=1.0,
            y=1.0,
            z=5.0,
            width=4.0,
            length=3.0,
            depth=3.0
        )

        self.assert_physical_result(
            expected_volume=504.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            )
        )

        second = self.create_box(
            operation="ADD",
            name="Second overlapping addition",
            x=3.0,
            y=2.0,
            z=5.0,
            width=4.0,
            length=3.0,
            depth=3.0
        )

        # Each addition contributes 24 units above the base.
        # Shared material above the base: 2 x 2 x 2 = 8.
        # Union volume: 480 + 24 + 24 - 8 = 520.
        # The union footprint perimeter is 20.

        mesh = self.assert_physical_result(
            expected_volume=520.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            ),
            expected_surface=420.0,
            inside_points=[
                (2.0, 2.0, 7.0),
                (4.0, 3.0, 7.0),
                (6.0, 4.0, 7.0)
            ],
            outside_points=[
                (1.5, 4.5, 7.0),
                (6.5, 1.5, 7.0),
                (8.0, 4.0, 7.0)
            ]
        )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=8.0
            ),
            20.0,
            delta=SURFACE_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=6.0
            ),
            60.0,
            delta=SURFACE_TOLERANCE
        )

        self.assertNotAlmostEqual(
            mesh_volume(mesh),
            528.0,
            delta=VOLUME_TOLERANCE,
            msg="Overlapping additive material was counted twice"
        )

        self.assert_operation_counts(
            additions=2
        )

        self.delete_geometry(first["id"])

        self.assert_physical_result(
            expected_volume=504.0,
            inside_points=[
                (4.0, 3.0, 7.0),
                (6.0, 4.0, 7.0)
            ],
            outside_points=[
                (2.0, 2.0, 7.0)
            ]
        )

        self.delete_geometry(second["id"])

        self.assert_baseline_restored()

    def test_overlapping_subtractions(self):
        """Verify that overlapping subtractive material is removed only once."""

        first = self.create_box(
            operation="SUBTRACT",
            name="First overlapping subtraction",
            x=1.0,
            y=1.0,
            z=-1.0,
            width=4.0,
            length=3.0,
            depth=8.0
        )

        self.assert_physical_result(
            expected_volume=408.0,
            expected_bounds=BASE_BOUNDS
        )

        second = self.create_box(
            operation="SUBTRACT",
            name="Second overlapping subtraction",
            x=3.0,
            y=2.0,
            z=-1.0,
            width=4.0,
            length=3.0,
            depth=8.0
        )

        # Each opening intersects 4 x 3 x 6 = 72 units.
        # Shared removed volume: 2 x 2 x 6 = 24.
        # Remaining volume: 480 - 72 - 72 + 24 = 360.
        # Union opening area: 20.
        # Union opening perimeter: 22.

        mesh = self.assert_physical_result(
            expected_volume=360.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=468.0,
            inside_points=[
                (0.5, 0.5, 3.0),
                (1.5, 4.5, 3.0),
                (6.5, 1.5, 3.0),
                (8.0, 6.0, 3.0)
            ],
            outside_points=[
                (2.0, 2.0, 3.0),
                (4.0, 3.0, 3.0),
                (6.0, 4.0, 3.0)
            ]
        )

        for coordinate in (0.0, 6.0):
            self.assertAlmostEqual(
                mesh_planar_area(
                    mesh,
                    axis=2,
                    coordinate=coordinate
                ),
                60.0,
                delta=SURFACE_TOLERANCE
            )

        self.assertNotAlmostEqual(
            mesh_volume(mesh),
            336.0,
            delta=VOLUME_TOLERANCE,
            msg="Overlapping subtractive material was removed twice"
        )

        self.assert_operation_counts(
            subtractions=2
        )

        self.delete_geometry(first["id"])

        self.assert_physical_result(
            expected_volume=408.0,
            expected_bounds=BASE_BOUNDS,
            inside_points=[
                (2.0, 2.0, 3.0)
            ],
            outside_points=[
                (4.0, 3.0, 3.0),
                (6.0, 4.0, 3.0)
            ]
        )

        self.delete_geometry(second["id"])

        self.assert_baseline_restored()

    def test_mixed_operations(self):
        """Verify combined additive and subtractive operations."""

        addition = self.create_box(
            operation="ADD",
            name="Mixed addition",
            x=1.0,
            y=1.0,
            z=5.0,
            width=3.0,
            length=3.0,
            depth=3.0
        )

        # Added volume: 3 x 3 x 2 = 18.

        self.assert_physical_result(
            expected_volume=498.0
        )

        subtraction = self.create_box(
            operation="SUBTRACT",
            name="Mixed through opening",
            x=6.0,
            y=2.0,
            z=-1.0,
            width=2.0,
            length=3.0,
            depth=8.0
        )

        # Removed volume: 2 x 3 x 6 = 36.

        self.assert_physical_result(
            expected_volume=462.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            ),
            expected_surface=448.0,
            inside_points=[
                (2.5, 2.5, 7.0),
                (5.0, 5.0, 3.0)
            ],
            outside_points=[
                (7.0, 3.5, 3.0),
                (5.0, 5.0, 7.0)
            ]
        )

        crossing_subtraction = self.create_box(
            operation="SUBTRACT",
            name="Subtraction crossing additive material",
            x=2.0,
            y=2.0,
            z=4.0,
            width=1.0,
            length=1.0,
            depth=5.0
        )

        # This operation intersects both native and added material.
        # Removed native volume: 1 x 1 x 2 = 2.
        # Removed additive volume: 1 x 1 x 2 = 2.

        self.assert_physical_result(
            expected_volume=458.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            ),
            expected_surface=464.0,
            inside_points=[
                (2.5, 2.5, 3.0),
                (1.5, 1.5, 7.0),
                (3.5, 3.5, 7.0)
            ],
            outside_points=[
                (2.5, 2.5, 5.0),
                (2.5, 2.5, 7.0),
                (7.0, 3.5, 3.0)
            ]
        )

        self.assert_operation_counts(
            additions=1,
            subtractions=2
        )

        self.delete_geometry(
            crossing_subtraction["id"]
        )

        self.assert_physical_result(
            expected_volume=462.0
        )

        self.delete_geometry(
            subtraction["id"]
        )

        self.assert_physical_result(
            expected_volume=498.0
        )

        self.delete_geometry(
            addition["id"]
        )

        self.assert_baseline_restored()

    def test_external_subtraction(self):
        """Verify that external subtraction does not affect the solid."""

        cases = [
            (
                "External positive X",
                20.0, 2.0, 1.0,
                3.0, 3.0, 3.0
            ),
            (
                "External negative X",
                -10.0, 2.0, 1.0,
                3.0, 3.0, 3.0
            ),
            (
                "External positive Y",
                2.0, 20.0, 1.0,
                3.0, 3.0, 3.0
            ),
            (
                "External negative Y",
                2.0, -10.0, 1.0,
                3.0, 3.0, 3.0
            ),
            (
                "External above solid",
                2.0, 2.0, 10.0,
                3.0, 3.0, 3.0
            ),
            (
                "External below solid",
                2.0, 2.0, -10.0,
                3.0, 3.0, 3.0
            ),
            (
                "Tangential external subtraction",
                10.0, 2.0, 1.0,
                3.0, 3.0, 3.0
            )
        ]

        created = []

        for (
            name,
            x,
            y,
            z,
            width,
            length,
            depth
        ) in cases:
            with self.subTest(
                operation=name
            ):
                subtraction = self.create_box(
                    operation="SUBTRACT",
                    name=name,
                    x=x,
                    y=y,
                    z=z,
                    width=width,
                    length=length,
                    depth=depth
                )

                created.append(subtraction)

                self.assert_baseline_restored()

        self.assert_operation_counts(
            subtractions=len(cases)
        )

        self.save_and_reload()

        self.assert_baseline_restored()

        for geometry in created:
            reloaded = self.find_geometry(
                geometry["globalId"]
            )

            self.delete_geometry(
                reloaded["id"]
            )

        self.assert_operation_counts()

        self.assert_baseline_restored()

    def test_complete_subtraction(self):
        """Verify complete removal of physical material."""

        subtraction = self.create_box(
            operation="SUBTRACT",
            name="Complete subtraction",
            x=-1.0,
            y=-1.0,
            z=-1.0,
            width=12.0,
            length=10.0,
            depth=8.0
        )

        # A valid boolean result containing no material must
        # be represented by a successful empty mesh stream.
        # TESSELLATION_FAILED is not accepted as an empty solid.

        self.assert_empty_mesh(
            self.physical_mesh()
        )

        self.assert_empty_mesh(
            self.physical_mesh(
                space="world"
            )
        )

        self.assert_operation_counts(
            subtractions=1
        )

        parent = self.get_parent()

        self.assertEqual(
            parent["globalId"],
            PARENT_GLOBAL_ID
        )

        self.assertEqual(
            len(parent["geometries"]),
            2
        )

        domain = self.create_box(
            operation="ADD",
            kind="DOMAIN",
            name="Domain inside completely subtracted solid",
            x=2.0,
            y=2.0,
            z=2.0,
            width=2.0,
            length=3.0,
            depth=2.0
        )

        self.assert_mesh(
            self.domain_mesh(domain["id"]),
            expected_volume=12.0,
            expected_bounds=(
                2, 2, 2,
                4, 5, 4
            )
        )

        self.assert_empty_mesh(
            self.physical_mesh()
        )

        self.save_and_reload()

        self.assert_empty_mesh(
            self.physical_mesh()
        )

        reloaded_subtraction = self.find_geometry(
            subtraction["globalId"]
        )

        reloaded_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assert_mesh(
            self.domain_mesh(
                reloaded_domain["id"]
            ),
            expected_volume=12.0
        )

        self.delete_geometry(
            reloaded_subtraction["id"]
        )

        self.assert_baseline_restored()

        self.assert_mesh(
            self.domain_mesh(
                reloaded_domain["id"]
            ),
            expected_volume=12.0
        )

        self.assert_operation_counts(
            domains=1
        )

    def test_rotated_operation(self):
        """Verify boolean operations with arbitrary rigid placements."""

        rotated_matrix = compose_matrices(
            translation_matrix(
                5.0,
                3.0,
                -1.0
            ),
            rotation_z(37.0)
        )

        # The rotated opening remains entirely inside the
        # horizontal footprint of the base.
        # Removed volume: 2 x 1.5 x 6 = 18.

        subtraction = self.create_geometry(
            operation="SUBTRACT",
            name="Rotated through opening",
            profile=rectangular_profile(
                2.0,
                1.5
            ),
            depth=8.0,
            matrix=rotated_matrix
        )

        rotated_mesh = self.assert_physical_result(
            expected_volume=462.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=412.0,
            inside_points=[
                (1.0, 1.0, 3.0),
                (8.0, 6.0, 3.0)
            ],
            outside_points=[
                (5.5, 4.0, 1.0),
                (5.5, 4.0, 3.0),
                (5.5, 4.0, 5.0)
            ]
        )

        for coordinate in (0.0, 6.0):
            self.assertAlmostEqual(
                mesh_planar_area(
                    rotated_mesh,
                    axis=2,
                    coordinate=coordinate
                ),
                77.0,
                delta=SURFACE_TOLERANCE
            )

        self.delete_geometry(
            subtraction["id"]
        )

        self.assert_baseline_restored()

        # The following arbitrarily oriented cavity is fully
        # enclosed within the base solid.
        # Its entire extrusion volume must be removed.

        tilted_matrix = compose_matrices(
            translation_matrix(
                4.0,
                3.0,
                2.0
            ),
            rotation_z(25.0),
            rotation_y(30.0),
            rotation_x(15.0)
        )

        tilted = self.create_geometry(
            operation="SUBTRACT",
            name="Arbitrarily oriented internal cavity",
            profile=rectangular_profile(
                1.0,
                2.0
            ),
            depth=2.0,
            matrix=tilted_matrix
        )

        self.assert_physical_result(
            expected_volume=476.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=392.0,
            inside_points=[
                (1.0, 1.0, 1.0),
                (9.0, 7.0, 5.0)
            ]
        )

        self.delete_geometry(
            tilted["id"]
        )

        self.assert_baseline_restored()

        additive_matrix = compose_matrices(
            translation_matrix(
                5.0,
                3.0,
                5.0
            ),
            rotation_z(37.0)
        )

        addition = self.create_geometry(
            operation="ADD",
            name="Rotated additive extrusion",
            profile=rectangular_profile(
                2.0,
                1.5
            ),
            depth=3.0,
            matrix=additive_matrix
        )

        # Added volume: 2 x 1.5 x 2 = 6.

        mesh = self.assert_physical_result(
            expected_volume=486.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            ),
            expected_surface=390.0,
            inside_points=[
                (5.5, 4.0, 7.0)
            ],
            outside_points=[
                (3.0, 4.0, 7.0),
                (8.0, 6.0, 7.0)
            ]
        )

        self.assertAlmostEqual(
            mesh_planar_area(
                mesh,
                axis=2,
                coordinate=8.0
            ),
            3.0,
            delta=SURFACE_TOLERANCE
        )

        self.save_and_reload()

        reloaded = self.find_geometry(
            addition["globalId"]
        )

        self.assert_matrix(
            reloaded["transforms"]["local"],
            additive_matrix
        )

        self.assert_equivalent_meshes(
            mesh,
            self.assert_physical_result(
                expected_volume=486.0,
                expected_bounds=(
                    0, 0, 0,
                    10, 8, 8
                )
            )
        )

    def test_operation_conversion(self):
        """Verify ADD-to-SUBTRACT and SUBTRACT-to-ADD conversions."""

        geometry = self.create_box(
            operation="ADD",
            name="Convertible operation",
            x=2.0,
            y=2.0,
            z=4.0,
            width=3.0,
            length=2.0,
            depth=4.0
        )

        identifier = geometry["id"]

        original_profile = geometry["profile"]
        original_depth = geometry["depth"]
        original_matrix = geometry["transforms"]["local"]

        additive_mesh = self.assert_physical_result(
            expected_volume=492.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            ),
            expected_surface=396.0,
            inside_points=[
                (3.5, 3.0, 5.0),
                (3.5, 3.0, 7.0)
            ]
        )

        converted = self.update_geometry(
            identifier,
            operation="SUBTRACT"
        )

        # The IFC feature may be replaced during conversion.
        # Only its CLI session identifier must remain stable.

        self.assertEqual(
            converted["id"],
            identifier
        )

        self.assertEqual(
            converted["operation"],
            "SUBTRACT"
        )

        self.assertEqual(
            converted["ifcClass"],
            "IfcOpeningElement"
        )

        self.assertEqual(
            converted["kind"],
            "SOLID"
        )

        self.assert_profile(
            converted["profile"],
            original_profile
        )

        self.assertAlmostEqual(
            converted["depth"],
            original_depth
        )

        self.assert_matrix(
            converted["transforms"]["local"],
            original_matrix
        )

        subtractive_mesh = self.assert_physical_result(
            expected_volume=468.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=396.0,
            inside_points=[
                (3.5, 3.0, 3.0)
            ],
            outside_points=[
                (3.5, 3.0, 5.0),
                (3.5, 3.0, 7.0)
            ]
        )

        self.assert_operation_counts(
            subtractions=1
        )

        restored = self.update_geometry(
            identifier,
            operation="ADD"
        )

        self.assertEqual(
            restored["operation"],
            "ADD"
        )

        self.assertEqual(
            restored["ifcClass"],
            "IfcProjectionElement"
        )

        self.assertEqual(
            restored["kind"],
            "SOLID"
        )

        self.assert_profile(
            restored["profile"],
            original_profile
        )

        self.assert_matrix(
            restored["transforms"]["local"],
            original_matrix
        )

        self.assert_equivalent_meshes(
            additive_mesh,
            self.assert_physical_result(
                expected_volume=492.0
            )
        )

        for iteration in range(3):
            with self.subTest(
                conversion=iteration
            ):
                self.update_geometry(
                    identifier,
                    operation="SUBTRACT"
                )

                self.assert_equivalent_meshes(
                    subtractive_mesh,
                    self.assert_physical_result(
                        expected_volume=468.0
                    )
                )

                self.update_geometry(
                    identifier,
                    operation="ADD"
                )

                self.assert_equivalent_meshes(
                    additive_mesh,
                    self.assert_physical_result(
                        expected_volume=492.0
                    )
                )

        self.assert_operation_counts(
            additions=1
        )

        final = self.get_geometry(identifier)

        self.save_and_reload()

        reloaded = self.find_geometry(
            final["globalId"]
        )

        self.assertEqual(
            reloaded["operation"],
            "ADD"
        )

        self.assert_equivalent_meshes(
            additive_mesh,
            self.assert_physical_result(
                expected_volume=492.0
            )
        )

        self.delete_geometry(
            reloaded["id"]
        )

        self.assert_baseline_restored()

    def test_operation_update(self):
        """Verify recalculation after changing extrusion parameters."""

        geometry = self.create_box(
            operation="ADD",
            name="Updated additive extrusion",
            x=2.0,
            y=2.0,
            z=5.0,
            width=2.0,
            length=3.0,
            depth=3.0
        )

        identifier = geometry["id"]

        self.assert_physical_result(
            expected_volume=492.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            )
        )

        depth_updated = self.update_geometry(
            identifier,
            depth=5.0
        )

        self.assert_geometry_identity(
            geometry,
            depth_updated
        )

        self.assert_physical_result(
            expected_volume=504.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 10
            )
        )

        profile_updated = self.update_geometry(
            identifier,
            profile=rectangular_profile(
                3.0,
                4.0
            )
        )

        self.assert_geometry_identity(
            geometry,
            profile_updated
        )

        self.assert_physical_result(
            expected_volume=528.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 10
            ),
            inside_points=[
                (4.5, 5.5, 8.0)
            ]
        )

        placement_updated = self.update_geometry(
            identifier,
            transform=transform_input(
                translation_matrix(
                    2.0,
                    2.0,
                    4.0
                )
            )
        )

        self.assert_geometry_identity(
            geometry,
            placement_updated
        )

        self.assert_physical_result(
            expected_volume=516.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 9
            )
        )

        self.update_geometry(
            identifier,
            name="Renamed additive extrusion"
        )

        self.assert_physical_result(
            expected_volume=516.0
        )

        self.update_geometry(
            identifier,
            operation="SUBTRACT"
        )

        self.assert_physical_result(
            expected_volume=456.0,
            expected_bounds=BASE_BOUNDS
        )

        self.update_geometry(
            identifier,
            depth=1.0
        )

        # Fully enclosed cavity: 3 x 4 x 1 = 12.

        self.assert_physical_result(
            expected_volume=468.0,
            expected_bounds=BASE_BOUNDS,
            inside_points=[
                (3.5, 4.0, 3.5),
                (3.5, 4.0, 5.5)
            ],
            outside_points=[
                (3.5, 4.0, 4.5)
            ]
        )

        self.update_geometry(
            identifier,
            transform=transform_input(
                translation_matrix(
                    20.0,
                    20.0,
                    20.0
                )
            )
        )

        self.assert_baseline_restored()

        self.update_geometry(
            identifier,
            profile=rectangular_profile(
                2.0,
                2.0
            ),
            depth=8.0,
            transform=transform_input(
                translation_matrix(
                    4.0,
                    3.0,
                    -1.0
                )
            )
        )

        final_mesh = self.assert_physical_result(
            expected_volume=456.0,
            expected_bounds=BASE_BOUNDS,
            expected_surface=416.0,
            outside_points=[
                (5.0, 4.0, 0.5),
                (5.0, 4.0, 3.0),
                (5.0, 4.0, 5.5)
            ]
        )

        before_invalid_update = self.get_geometry(
            identifier
        )

        self.client.expect_error(
            "geometryUpdate",
            {
                "id": identifier,
                "depth": -1.0
            },
            "INVALID_PARAMETERS"
        )

        self.assertEqual(
            self.get_geometry(identifier),
            before_invalid_update
        )

        self.assert_equivalent_meshes(
            final_mesh,
            self.physical_mesh()
        )

        self.client.expect_error(
            "geometryUpdate",
            {
                "id": identifier,
                "profile": [
                    [0, 0],
                    [4, 4],
                    [0, 4],
                    [4, 0]
                ]
            },
            "INVALID_PROFILE"
        )

        self.assertEqual(
            self.get_geometry(identifier),
            before_invalid_update
        )

        self.assert_equivalent_meshes(
            final_mesh,
            self.physical_mesh()
        )

        self.save_and_reload()

        reloaded = self.find_geometry(
            before_invalid_update["globalId"]
        )

        self.assertEqual(
            reloaded["operation"],
            "SUBTRACT"
        )

        self.assert_equivalent_meshes(
            final_mesh,
            self.assert_physical_result(
                expected_volume=456.0
            )
        )

    def test_operation_deletion(self):
        """Verify restoration of physical material after deleting operations."""

        addition = self.create_box(
            operation="ADD",
            name="Deleted addition",
            x=1.0,
            y=1.0,
            z=5.0,
            width=3.0,
            length=3.0,
            depth=3.0
        )

        subtraction = self.create_box(
            operation="SUBTRACT",
            name="Deleted through opening",
            x=6.0,
            y=2.0,
            z=-1.0,
            width=2.0,
            length=3.0,
            depth=8.0
        )

        second_subtraction = self.create_box(
            operation="SUBTRACT",
            name="Deleted blind opening",
            x=5.0,
            y=6.0,
            z=4.0,
            width=2.0,
            length=1.0,
            depth=4.0
        )

        # Physical volume:
        # 480 + 18 - 36 - 4 = 458.

        self.assert_physical_result(
            expected_volume=458.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            )
        )

        self.assert_operation_counts(
            additions=1,
            subtractions=2
        )

        self.delete_geometry(
            subtraction["id"]
        )

        self.assert_physical_result(
            expected_volume=494.0,
            inside_points=[
                (7.0, 3.5, 3.0),
                (2.5, 2.5, 7.0)
            ],
            outside_points=[
                (6.0, 6.5, 5.0)
            ]
        )

        self.assert_operation_counts(
            additions=1,
            subtractions=1
        )

        self.delete_geometry(
            addition["id"]
        )

        self.assert_physical_result(
            expected_volume=476.0,
            expected_bounds=BASE_BOUNDS,
            inside_points=[
                (7.0, 3.5, 3.0)
            ],
            outside_points=[
                (2.5, 2.5, 7.0),
                (6.0, 6.5, 5.0)
            ]
        )

        self.delete_geometry(
            second_subtraction["id"]
        )

        self.assert_operation_counts()

        self.assert_baseline_restored()

        deleted_global_ids = {
            addition["globalId"],
            subtraction["globalId"],
            second_subtraction["globalId"]
        }

        self.save_and_reload()

        self.assert_operation_counts()

        self.assert_baseline_restored()

        remaining_global_ids = {
            geometry["globalId"]
            for geometry in self.list_geometries()
        }

        self.assertFalse(
            deleted_global_ids
            & remaining_global_ids
        )

        saved_content = self.output_path.read_text(
            encoding="utf-8"
        )

        for global_id in deleted_global_ids:
            self.assertNotIn(
                global_id,
                saved_content
            )

        self.assertNotIn(
            "IFCRELPROJECTSELEMENT(",
            saved_content.upper()
        )

        self.assertNotIn(
            "IFCRELVOIDSELEMENT(",
            saved_content.upper()
        )

        self.assertNotIn(
            "IFCPROJECTIONELEMENT(",
            saved_content.upper()
        )

        self.assertNotIn(
            "IFCOPENINGELEMENT(",
            saved_content.upper()
        )

    def test_domain_interference(self):
        """Verify that domains never modify physical boolean results."""

        domains = [
            self.create_box(
                operation="ADD",
                kind="DOMAIN",
                name="Domain fully enclosing physical solid",
                x=-5.0,
                y=-5.0,
                z=-5.0,
                width=20.0,
                length=18.0,
                depth=16.0
            ),
            self.create_box(
                operation="ADD",
                kind="DOMAIN",
                name="Domain entirely inside physical solid",
                x=2.0,
                y=2.0,
                z=1.0,
                width=3.0,
                length=2.0,
                depth=4.0
            ),
            self.create_box(
                operation="ADD",
                kind="DOMAIN",
                name="Partially intersecting domain",
                x=8.0,
                y=6.0,
                z=4.0,
                width=4.0,
                length=4.0,
                depth=4.0
            )
        ]

        expected_domain_volumes = [
            5760.0,
            24.0,
            64.0
        ]

        for geometry, expected_volume in zip(
            domains,
            expected_domain_volumes
        ):
            with self.subTest(
                domain=geometry["name"]
            ):
                self.assert_mesh(
                    self.domain_mesh(
                        geometry["id"]
                    ),
                    expected_volume=expected_volume
                )

        self.assert_equivalent_meshes(
            self.baseline_mesh,
            self.assert_baseline_restored(),
            exact_triangles=True
        )

        combined_domain_mesh = self.domain_mesh()

        # DOMAIN meshes are independent volumes.
        # Their overlapping regions must not be boolean-unioned.

        self.assertAlmostEqual(
            mesh_volume(combined_domain_mesh),
            sum(expected_domain_volumes),
            delta=VOLUME_TOLERANCE
        )

        self.assert_operation_counts(
            domains=3
        )

        addition = self.create_box(
            operation="ADD",
            name="Physical addition with domains",
            x=1.0,
            y=1.0,
            z=5.0,
            width=3.0,
            length=3.0,
            depth=3.0
        )

        subtraction = self.create_box(
            operation="SUBTRACT",
            name="Physical subtraction with domains",
            x=6.0,
            y=2.0,
            z=-1.0,
            width=2.0,
            length=3.0,
            depth=8.0
        )

        physical_before = self.assert_physical_result(
            expected_volume=462.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            )
        )

        for geometry, expected_volume in zip(
            domains,
            expected_domain_volumes
        ):
            self.assert_mesh(
                self.domain_mesh(
                    geometry["id"]
                ),
                expected_volume=expected_volume
            )

        self.assert_operation_counts(
            additions=1,
            subtractions=1,
            domains=3
        )

        updated_domain = self.update_geometry(
            domains[0]["id"],
            profile=rectangular_profile(
                30.0,
                25.0
            ),
            depth=20.0,
            transform=transform_input(
                translation_matrix(
                    -10.0,
                    -10.0,
                    -10.0
                )
            )
        )

        self.assert_mesh(
            self.domain_mesh(
                updated_domain["id"]
            ),
            expected_volume=15000.0
        )

        self.assert_equivalent_meshes(
            physical_before,
            self.physical_mesh(),
            exact_triangles=True
        )

        self.client.expect_error(
            "geometryUpdate",
            {
                "id": updated_domain["id"],
                "operation": "SUBTRACT"
            },
            "INVALID_PARAMETERS"
        )

        self.assertEqual(
            self.get_geometry(
                updated_domain["id"]
            ),
            updated_domain
        )

        self.client.expect_error(
            "transformTessellate",
            {
                "id": self.parent_id,
                "options": {
                    "space": "local",
                    "kind": "SOLID",
                    "geometryId": updated_domain["id"],
                    "includeNormals": True,
                    "includeMaterials": False,
                    "includeChildren": False
                }
            },
            "INVALID_PARAMETERS"
        )

        self.assert_equivalent_meshes(
            physical_before,
            self.physical_mesh(),
            exact_triangles=True
        )

        for domain in domains:
            self.delete_geometry(
                domain["id"]
            )

            self.assert_equivalent_meshes(
                physical_before,
                self.physical_mesh(),
                exact_triangles=True
            )

        self.assert_empty_mesh(
            self.domain_mesh()
        )

        self.assert_operation_counts(
            additions=1,
            subtractions=1
        )

        self.delete_geometry(
            subtraction["id"]
        )

        self.assert_physical_result(
            expected_volume=498.0
        )

        self.delete_geometry(
            addition["id"]
        )

        self.assert_baseline_restored()

    def test_boolean_persistence(self):
        """Verify persistence of physical boolean results after IFC roundtrips."""

        first_addition = self.create_box(
            operation="ADD",
            name="Persistent first addition",
            x=1.0,
            y=1.0,
            z=5.0,
            width=4.0,
            length=3.0,
            depth=3.0
        )

        second_addition = self.create_box(
            operation="ADD",
            name="Persistent second addition",
            x=3.0,
            y=2.0,
            z=5.0,
            width=4.0,
            length=3.0,
            depth=3.0
        )

        first_subtraction = self.create_box(
            operation="SUBTRACT",
            name="Persistent through opening",
            x=8.0,
            y=1.0,
            z=-1.0,
            width=1.0,
            length=2.0,
            depth=8.0
        )

        second_subtraction = self.create_box(
            operation="SUBTRACT",
            name="Persistent blind opening",
            x=8.0,
            y=5.0,
            z=4.0,
            width=1.0,
            length=2.0,
            depth=4.0
        )

        domain = self.create_box(
            operation="ADD",
            kind="DOMAIN",
            name="Persistent independent domain",
            x=-5.0,
            y=-5.0,
            z=-5.0,
            width=20.0,
            length=18.0,
            depth=16.0
        )

        # Additive union above base: 24 + 24 - 8 = 40.
        # Through opening: 1 x 2 x 6 = 12.
        # Blind opening: 1 x 2 x 2 = 4.
        # Expected physical volume: 480 + 40 - 12 - 4 = 504.

        original_mesh = self.assert_physical_result(
            expected_volume=504.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            ),
            inside_points=[
                (2.0, 2.0, 7.0),
                (4.0, 3.0, 7.0),
                (6.0, 4.0, 7.0),
                (8.5, 6.0, 2.0)
            ],
            outside_points=[
                (8.5, 2.0, 3.0),
                (8.5, 6.0, 5.0),
                (8.0, 4.0, 7.0)
            ]
        )

        original_domain_mesh = self.domain_mesh(
            domain["id"]
        )

        self.assert_mesh(
            original_domain_mesh,
            expected_volume=5760.0
        )

        self.assert_operation_counts(
            additions=2,
            subtractions=2,
            domains=1
        )

        original_geometries = {
            geometry["globalId"]: self.get_geometry(
                geometry["id"]
            )
            for geometry in (
                first_addition,
                second_addition,
                first_subtraction,
                second_subtraction,
                domain
            )
        }

        for iteration in range(5):
            with self.subTest(
                roundtrip=iteration
            ):
                result = self.save_and_reload()

                self.assertEqual(
                    result["transformCount"],
                    2
                )

                self.assertEqual(
                    result["geometryCount"],
                    6
                )

                self.assert_operation_counts(
                    additions=2,
                    subtractions=2,
                    domains=1
                )

                for global_id, before in original_geometries.items():
                    reloaded = self.find_geometry(
                        global_id
                    )

                    for field in (
                        "globalId",
                        "name",
                        "source",
                        "editable",
                        "kind",
                        "operation",
                        "ifcClass",
                        "materials"
                    ):
                        self.assertEqual(
                            reloaded[field],
                            before[field]
                        )

                    self.assertEqual(
                        reloaded["parentId"],
                        self.parent_id
                    )

                    self.assert_profile(
                        reloaded["profile"],
                        before["profile"]
                    )

                    self.assertAlmostEqual(
                        reloaded["depth"],
                        before["depth"],
                        delta=MESH_TOLERANCE
                    )

                    self.assert_matrix(
                        reloaded["transforms"]["local"],
                        before["transforms"]["local"]
                    )

                    self.assert_matrix(
                        reloaded["transforms"]["world"],
                        before["transforms"]["world"]
                    )

                reloaded_mesh = self.assert_physical_result(
                    expected_volume=504.0,
                    expected_bounds=(
                        0, 0, 0,
                        10, 8, 8
                    ),
                    inside_points=[
                        (2.0, 2.0, 7.0),
                        (4.0, 3.0, 7.0),
                        (6.0, 4.0, 7.0)
                    ],
                    outside_points=[
                        (8.5, 2.0, 3.0),
                        (8.5, 6.0, 5.0)
                    ]
                )

                self.assert_equivalent_meshes(
                    original_mesh,
                    reloaded_mesh
                )

                reloaded_domain = self.find_geometry(
                    domain["globalId"]
                )

                self.assert_equivalent_meshes(
                    original_domain_mesh,
                    self.domain_mesh(
                        reloaded_domain["id"]
                    )
                )

        reloaded_subtraction = self.find_geometry(
            first_subtraction["globalId"]
        )

        self.update_geometry(
            reloaded_subtraction["id"],
            profile=rectangular_profile(
                2.0,
                2.0
            )
        )

        # Through opening volume changes from 12 to 24.

        updated_mesh = self.assert_physical_result(
            expected_volume=492.0,
            expected_bounds=(
                0, 0, 0,
                10, 8, 8
            )
        )

        self.save_and_reload()

        self.assert_equivalent_meshes(
            updated_mesh,
            self.assert_physical_result(
                expected_volume=492.0
            )
        )

        for global_id in original_geometries:
            reloaded = self.find_geometry(
                global_id
            )

            self.delete_geometry(
                reloaded["id"]
            )

        self.assert_operation_counts()

        self.assert_baseline_restored()

        self.assert_empty_mesh(
            self.domain_mesh()
        )

        self.save_and_reload()

        self.assert_operation_counts()

        self.assert_baseline_restored()

        self.assert_empty_mesh(
            self.domain_mesh()
        )

        self.assertEqual(
            len(self.list_geometries()),
            1
        )

        remaining = self.list_geometries()[0]

        self.assertEqual(
            remaining["source"],
            "ifc"
        )

        self.assertEqual(
            remaining["operation"],
            "BASE"
        )

        self.assertEqual(
            remaining["kind"],
            "SOLID"
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