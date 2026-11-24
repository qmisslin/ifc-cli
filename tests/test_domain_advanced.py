import itertools
import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from collections import Counter, defaultdict
from pathlib import Path


# Standalone integration suite using only the Python standard library.
#
# Run:
# python3 tests/test_domain_advanced.py ./build/ifc-cli
#
# Not executed against the compiled CLI.
#
# Domain meshes are evaluated independently from physical geometry.
# Overlapping domains retain their individual boundaries and volumes.
#
# Spatial reconstruction uses triangle-based solid angle classification.
# Voxel occupancy is computed independently from the CLI using cell centres.
# Sampling points are deliberately placed away from mesh boundaries.
#
# The failed update test enforces atomicity for combined updates.
# The current CLI applies some changes before validating subsequent
# fields, which may cause this test to expose partial mutations.

EXECUTABLE = Path("./build/ifc-cli").resolve()

REQUEST_TIMEOUT = 60
MESH_TOLERANCE = 1e-6
VOLUME_TOLERANCE = 1e-5

ROOT_GLOBAL_ID = "0000000000000000000001"
PROJECT_GLOBAL_ID = "0000000000000000000002"
NATIVE_SPACE_GLOBAL_ID = "0000000000000000000003"
VIRTUAL_PRODUCT_GLOBAL_ID = "0000000000000000000004"
ORPHAN_DOMAIN_GLOBAL_ID = "0000000000000000000005"

OUTSIDE = "OUTSIDE"
FREE = "FREE"
SOLID = "SOLID"

IDENTITY_MATRIX = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0
]

DEFAULT_PROFILE = [
    [0.0, 0.0],
    [4.0, 0.0],
    [4.0, 3.0],
    [0.0, 3.0]
]

DEFAULT_VISUAL = {
    "color": [0.4, 0.6, 0.8],
    "opacity": 1.0,
    "metallic": 0.0,
    "roughness": 0.5
}

PHYSICAL_BOUNDS = (
    -2.0, -2.0, 0.0,
    2.0, 2.0, 4.0
)

PHYSICAL_VOLUME = 64.0

BASE_ENTITIES = """
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCDIRECTION((0.,0.,1.));
#3=IFCDIRECTION((1.,0.,0.));
#4=IFCAXIS2PLACEMENT3D(#1,#2,#3);
#5=IFCLOCALPLACEMENT($,#4);
#6=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-6,#4,$);
#7=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#8=IFCUNITASSIGNMENT((#7));
#9=IFCCARTESIANPOINT((0.,0.));
#10=IFCAXIS2PLACEMENT2D(#9,$);
#11=IFCRECTANGLEPROFILEDEF(.AREA.,$,#10,4.,4.);
#12=IFCEXTRUDEDAREASOLID(#11,#4,#2,4.);
#13=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#12));
#14=IFCPRODUCTDEFINITIONSHAPE($,$,(#13));
#15=IFCBUILDINGELEMENTPROXY('0000000000000000000001',$,'Physical cube',$,$,#5,#14,$,.NOTDEFINED.);
#16=IFCPROJECT('0000000000000000000002',$,'Advanced domain project',$,$,$,$,(#6),#8);
#17=IFCBUILDINGELEMENTPROXY('0000000000000000000004',$,'Virtual product',$,$,$,$,$,.NOTDEFINED.);
"""

NATIVE_SPACE_ENTITIES = """
#30=IFCLOCALPLACEMENT($,#4);
#31=IFCRECTANGLEPROFILEDEF(.AREA.,$,#10,6.,5.);
#32=IFCEXTRUDEDAREASOLID(#31,#4,#2,3.);
#33=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#32));
#34=IFCPRODUCTDEFINITIONSHAPE($,$,(#33));
#35=IFCSPACE('0000000000000000000003',$,'Native space',$,$,#30,#34,$,.ELEMENT.,.INTERNAL.,$);
"""

ORPHAN_DOMAIN_ENTITIES = """
#40=IFCLOCALPLACEMENT(#5,#4);
#41=IFCRECTANGLEPROFILEDEF(.AREA.,$,#10,2.,2.);
#42=IFCEXTRUDEDAREASOLID(#41,#4,#2,2.);
#43=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#42));
#44=IFCPRODUCTDEFINITIONSHAPE($,$,(#43));
#45=IFCSPATIALZONE('0000000000000000000005',$,'Orphan domain',$,'ifc-cli:domain',#40,#44,$,.USERDEFINED.);
"""

ORPHAN_INVALID_PARENT_ENTITIES = """
#46=IFCOPENINGELEMENT('0000000000000000000006',$,'Unindexed opening',$,$,#40,$,$,.OPENING.);
#47=IFCRELASSIGNSTOPRODUCT('0000000000000000000007',$,'ifc-cli:domain-parent',$,(#45),$,#46);
"""

ORPHAN_INVALID_MARKER_ENTITIES = """
#47=IFCRELASSIGNSTOPRODUCT('0000000000000000000007',$,'invalid-domain-parent',$,(#45),$,#15);
"""


def build_fixture(extra_entities=""):
    return f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('domain-advanced.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
{BASE_ENTITIES}
{extra_entities}
ENDSEC;
END-ISO-10303-21;
"""


def translation_matrix(x=0.0, y=0.0, z=0.0):
    matrix = IDENTITY_MATRIX.copy()
    matrix[3] = x
    matrix[7] = y
    matrix[11] = z
    return matrix


def rotation_x(angle):
    radians = math.radians(angle)
    c = math.cos(radians)
    s = math.sin(radians)

    return [
        1, 0, 0, 0,
        0, c, -s, 0,
        0, s, c, 0,
        0, 0, 0, 1
    ]


def rotation_y(angle):
    radians = math.radians(angle)
    c = math.cos(radians)
    s = math.sin(radians)

    return [
        c, 0, s, 0,
        0, 1, 0, 0,
        -s, 0, c, 0,
        0, 0, 0, 1
    ]


def rotation_z(angle):
    radians = math.radians(angle)
    c = math.cos(radians)
    s = math.sin(radians)

    return [
        c, -s, 0, 0,
        s, c, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1
    ]


def multiply_matrices(left, right):
    return [
        math.fsum(
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


def transform_point(matrix, point):
    vector = (*point, 1.0)

    return tuple(
        math.fsum(
            matrix[row * 4 + column] * vector[column]
            for column in range(4)
        )
        for row in range(3)
    )


def transform_input(matrix=None, space="parent"):
    return {
        "space": space,
        "matrix": IDENTITY_MATRIX.copy() if matrix is None else matrix
    }


def parents_input(spatial=None, placement=None, decomposition=None):
    return {
        "spatial": spatial,
        "placement": placement,
        "decomposition": decomposition
    }


def rectangular_profile(width, length, x=0.0, y=0.0):
    return [
        [x, y],
        [x + width, y],
        [x + width, y + length],
        [x, y + length]
    ]


def polygon_area(profile):
    return abs(math.fsum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(profile, profile[1:] + profile[:1])
    )) / 2.0


def polygon_perimeter(profile):
    return math.fsum(
        math.dist(first, second)
        for first, second in zip(profile, profile[1:] + profile[:1])
    )


def point_in_polygon(point, polygon):
    # Reference two-dimensional ray crossing algorithm.

    x, y = point
    inside = False

    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        x1, y1 = first
        x2, y2 = second

        if (y1 > y) == (y2 > y):
            continue

        intersection = x1 + (y - y1) * (x2 - x1) / (y2 - y1)

        if x < intersection:
            inside = not inside

    return inside


def subtract(first, second):
    return tuple(a - b for a, b in zip(first, second))


def dot(first, second):
    return math.fsum(a * b for a, b in zip(first, second))


def cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0]
    )


def norm(vector):
    return math.sqrt(dot(vector, vector))


def mesh_points(mesh):
    return [
        tuple(mesh["vertices"][offset:offset + 3])
        for offset in range(0, len(mesh["vertices"]), 3)
    ]


def mesh_triangles(mesh):
    points = mesh_points(mesh)

    return [
        tuple(points[index] for index in mesh["indices"][offset:offset + 3])
        for offset in range(0, len(mesh["indices"]), 3)
    ]


def bounds_from_points(points):
    if not points:
        raise AssertionError("No points available")

    return tuple(
        operation(point[axis] for point in points)
        for operation in (min, max)
        for axis in range(3)
    )


def mesh_bounds(mesh):
    return bounds_from_points(mesh_points(mesh))


def mesh_signed_volume(mesh):
    points = mesh_points(mesh)

    if not points:
        return 0.0

    origin = points[0]

    return math.fsum(
        dot(
            subtract(a, origin),
            cross(subtract(b, origin), subtract(c, origin))
        )
        for a, b, c in mesh_triangles(mesh)
    ) / 6.0


def mesh_volume(mesh):
    return abs(mesh_signed_volume(mesh))


def mesh_surface_area(mesh):
    return math.fsum(
        norm(cross(subtract(b, a), subtract(c, a))) / 2.0
        for a, b, c in mesh_triangles(mesh)
    )


def quantize(point, precision=6):
    return tuple(round(value, precision) for value in point)


def mesh_vertex_set(mesh):
    return {
        quantize(point)
        for point in mesh_points(mesh)
    }


def mesh_signature(mesh):
    # Preserve triangle multiplicity but ignore vertex and triangle order.

    return Counter(
        tuple(sorted(quantize(point) for point in triangle))
        for triangle in mesh_triangles(mesh)
    )


def mesh_edge_counts(mesh):
    undirected = Counter()
    directed = Counter()

    for triangle in mesh_triangles(mesh):
        a, b, c = [quantize(point) for point in triangle]

        if len({a, b, c}) != 3:
            raise AssertionError(f"Degenerate triangle: {triangle}")

        for first, second in ((a, b), (b, c), (c, a)):
            undirected[tuple(sorted((first, second)))] += 1
            directed[(first, second)] += 1

    return undirected, directed


def mesh_connected_components(mesh):
    # Weld geometrically equivalent vertices for connectivity analysis.

    adjacency = defaultdict(set)

    for triangle in mesh_triangles(mesh):
        a, b, c = [quantize(point) for point in triangle]

        for first, second in ((a, b), (b, c), (c, a)):
            adjacency[first].add(second)
            adjacency[second].add(first)

    unvisited = set(adjacency)
    components = []

    while unvisited:
        start = unvisited.pop()
        pending = [start]
        component = {start}

        while pending:
            current = pending.pop()

            for neighbour in adjacency[current]:
                if neighbour not in unvisited:
                    continue

                unvisited.remove(neighbour)
                component.add(neighbour)
                pending.append(neighbour)

        components.append(component)

    return components


def mesh_euler_characteristic(mesh):
    vertices = mesh_vertex_set(mesh)
    edges, _ = mesh_edge_counts(mesh)
    faces = len(mesh["indices"]) // 3

    return len(vertices) - len(edges) + faces


class ClosedSurface:
    def __init__(self, mesh):
        self.triangles = mesh_triangles(mesh)
        self.bounds = mesh_bounds(mesh)

        if not self.triangles:
            raise ValueError("Closed surface requires triangles")

    def winding_number(self, point):
        # Compute the signed solid angle of every triangle.
        # The sum is approximately +/-4*pi inside a closed surface
        # and approximately zero outside.

        angles = []

        for first, second, third in self.triangles:
            a = subtract(first, point)
            b = subtract(second, point)
            c = subtract(third, point)

            length_a = norm(a)
            length_b = norm(b)
            length_c = norm(c)

            if min(length_a, length_b, length_c) <= 1e-12:
                raise ValueError(
                    f"Point lies on a mesh vertex: {point}"
                )

            numerator = dot(a, cross(b, c))

            denominator = (
                length_a * length_b * length_c
                + dot(a, b) * length_c
                + dot(b, c) * length_a
                + dot(c, a) * length_b
            )

            angles.append(
                2.0 * math.atan2(numerator, denominator)
            )

        return math.fsum(angles) / (4.0 * math.pi)

    def contains(self, point):
        minimum = self.bounds[:3]
        maximum = self.bounds[3:]

        if any(
            value < lower - MESH_TOLERANCE
            or value > upper + MESH_TOLERANCE
            for value, lower, upper in zip(point, minimum, maximum)
        ):
            return False

        winding = abs(self.winding_number(point))

        return winding > 0.5


def classify_point(point, domain_surfaces, physical_surfaces):
    if not any(surface.contains(point) for surface in domain_surfaces):
        return OUTSIDE

    if any(surface.contains(point) for surface in physical_surfaces):
        return SOLID

    return FREE


def voxelize(
    origin,
    dimensions,
    spacing,
    domain_surfaces,
    physical_surfaces
):
    occupancy = {}

    for index in itertools.product(
        *(range(dimension) for dimension in dimensions)
    ):
        centre = tuple(
            origin[axis] + (index[axis] + 0.5) * spacing
            for axis in range(3)
        )

        occupancy[index] = classify_point(
            centre,
            domain_surfaces,
            physical_surfaces
        )

    return occupancy


def six_connected_neighbours(index):
    for axis in range(3):
        for direction in (-1, 1):
            neighbour = list(index)
            neighbour[axis] += direction
            yield tuple(neighbour)


def voxel_components(occupancy, state):
    remaining = {
        index
        for index, value in occupancy.items()
        if value == state
    }

    components = []

    while remaining:
        first = remaining.pop()
        pending = [first]
        component = {first}

        while pending:
            index = pending.pop()

            for neighbour in six_connected_neighbours(index):
                if neighbour not in remaining:
                    continue

                remaining.remove(neighbour)
                component.add(neighbour)
                pending.append(neighbour)

        components.append(component)

    return components


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
                f"No response for request {request_id}"
            ) from error

        if item is None:
            raise RuntimeError("CLI terminated unexpectedly")

        if isinstance(item, Exception):
            raise RuntimeError("Could not read CLI stdout") from item

        response = json.loads(item)

        if not isinstance(response, dict):
            raise AssertionError(response)

        if response.get("id") != request_id:
            raise AssertionError(f"Unexpected response: {response}")

        if type(response.get("ok")) is not bool:
            raise AssertionError(response)

        expected_fields = (
            {"id", "ok", "result"}
            if response["ok"]
            else {"id", "ok", "error"}
        )

        if set(response) != expected_fields:
            raise AssertionError(response)

        if not response["ok"]:
            error = response["error"]

            if set(error) != {"code", "message", "details"}:
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
            raise AssertionError(
                f"{command} unexpectedly succeeded: {response}"
            )

        if response["error"]["code"] != expected_code:
            raise AssertionError(
                f"Expected {expected_code}, received {response['error']}"
            )

        return response["error"]

    def tessellation_parameters(
        self,
        transform_id,
        kind="DOMAIN",
        geometry_id=None,
        space="local",
        include_children=False,
        include_normals=False,
        include_materials=True
    ):
        options = {
            "kind": kind,
            "space": space,
            "includeChildren": include_children,
            "includeNormals": include_normals,
            "includeMaterials": include_materials
        }

        if geometry_id is not None:
            options["geometryId"] = geometry_id

        return {
            "id": transform_id,
            "options": options
        }

    def tessellate(self, transform_id, **options):
        params = self.tessellation_parameters(transform_id, **options)
        request_id = self.send("transformTessellate", params)

        mesh = {
            "vertices": [],
            "indices": [],
            "normals": [],
            "groups": [],
            "events": []
        }

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.normals": 3,
            "mesh.materials": 4,
            "mesh.end": 5
        }

        begin = None
        previous_order = -1

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
                raise AssertionError(f"Incorrect event order: {event}")

            previous_order = order
            mesh["events"].append(name)

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
                if mesh["events"].count(name) != 1:
                    raise AssertionError("Duplicate mesh.materials")

                mesh["groups"] = event["groups"]
                continue

            key = name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(f"Invalid mesh offset: {event}")

            mesh[key].extend(event["values"])

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if mesh["indexCount"] % 3:
            raise AssertionError("Invalid triangle indices")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(f"Invalid mesh index: {index}")

        for value in mesh["vertices"] + mesh["normals"]:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise AssertionError(f"Invalid mesh coordinate: {value}")

        include_materials = params["options"]["includeMaterials"]
        include_normals = params["options"]["includeNormals"]

        if ("mesh.materials" in mesh["events"]) != include_materials:
            raise AssertionError("Incorrect material event presence")

        if not include_normals and "mesh.normals" in mesh["events"]:
            raise AssertionError("Unexpected normal event")

        return mesh

    def close(self):
        try:
            if self.process.poll() is None:
                result = self.request("exit")

                if result != {}:
                    raise AssertionError(result)

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


class AdvancedDomainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-domain-advanced-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)
        self.input_path = self.root_path / "input.ifc"
        self.save_counter = 0

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.load_fixture()

    def tearDown(self):
        self.client.close()

    def load_fixture(self, extra_entities=""):
        self.input_path.write_text(
            build_fixture(extra_entities),
            encoding="utf-8"
        )

        return self.load_model(self.input_path)

    def load_model(self, path):
        result = self.client.request("load", {"path": str(path)})

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

        return result

    def save_and_reload(self):
        self.save_counter += 1

        path = self.root_path / f"roundtrip-{self.save_counter}.ifc"

        self.assertEqual(
            self.client.request("save", {"path": str(path)}),
            {"path": str(path)}
        )

        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)

        self.load_model(path)

        return path

    @property
    def root_id(self):
        return self.find_transform(ROOT_GLOBAL_ID)["id"]

    @property
    def project_id(self):
        return self.find_transform(PROJECT_GLOBAL_ID)["id"]

    @property
    def virtual_id(self):
        return self.find_transform(VIRTUAL_PRODUCT_GLOBAL_ID)["id"]

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

    def find_transform(self, global_id):
        matches = [
            item
            for item in self.list_transforms()
            if item["globalId"] == global_id
        ]

        self.assertEqual(len(matches), 1)

        return self.get_transform(matches[0]["id"])

    def find_geometry(self, global_id):
        matches = [
            item
            for item in self.list_geometries()
            if item["globalId"] == global_id
        ]

        self.assertEqual(len(matches), 1)

        return self.get_geometry(matches[0]["id"])

    def create_transform(
        self,
        name="Domain hierarchy transform",
        parent_id=None,
        matrix=None
    ):
        return self.client.request(
            "transformCreate",
            {
                "name": name,
                "parents": parents_input(
                    placement=parent_id,
                    decomposition=parent_id
                ),
                "transform": transform_input(matrix),
                "properties": []
            }
        )

    def geometry_parameters(
        self,
        name="Advanced domain",
        parent_id=None,
        profile=None,
        depth=2.0,
        matrix=None,
        kind="DOMAIN",
        operation="ADD",
        space="parent"
    ):
        return {
            "parentId": self.root_id if parent_id is None else parent_id,
            "name": name,
            "kind": kind,
            "operation": operation,
            "profile": DEFAULT_PROFILE if profile is None else profile,
            "depth": depth,
            "transform": transform_input(matrix, space)
        }

    def create_domain(self, **options):
        domain = self.client.request(
            "geometryCreate",
            self.geometry_parameters(**options)
        )

        self.assertEqual(domain["kind"], "DOMAIN")
        self.assertEqual(domain["operation"], "ADD")
        self.assertEqual(domain["source"], "editor")
        self.assertEqual(domain["ifcClass"], "IfcSpatialZone")
        self.assertIs(domain["editable"], True)
        self.assertEqual(domain["materials"], [])

        return domain

    def create_solid(self, **options):
        return self.client.request(
            "geometryCreate",
            self.geometry_parameters(
                kind="SOLID",
                **options
            )
        )

    def create_material(self, name="Physical material"):
        return self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": "concrete",
                "visual": DEFAULT_VISUAL,
                "properties": []
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

    def update_geometry(self, identifier, **changes):
        return self.client.request(
            "geometryUpdate",
            {
                "id": identifier,
                **changes
            }
        )

    def delete_geometry(self, identifier):
        self.assertEqual(
            self.client.request("geometryDelete", {"id": identifier}),
            {"id": identifier}
        )

    def tessellate(self, transform_id=None, **options):
        return self.client.tessellate(
            self.root_id if transform_id is None else transform_id,
            **options
        )

    def domain_mesh(self, domain, **options):
        return self.tessellate(
            domain["parentId"],
            kind="DOMAIN",
            geometry_id=domain["id"],
            **options
        )

    def physical_mesh(self, transform_id=None, **options):
        return self.tessellate(
            transform_id,
            kind="SOLID",
            **options
        )

    def assert_bounds(self, mesh, expected):
        for value, reference in zip(mesh_bounds(mesh), expected):
            self.assertAlmostEqual(
                value,
                reference,
                delta=MESH_TOLERANCE
            )

    def assert_volume(self, mesh, expected):
        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected,
            delta=VOLUME_TOLERANCE
        )

    def assert_surface(self, mesh, expected):
        self.assertAlmostEqual(
            mesh_surface_area(mesh),
            expected,
            delta=VOLUME_TOLERANCE
        )

    def assert_empty_mesh(self, mesh):
        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)
        self.assertEqual(mesh["vertices"], [])
        self.assertEqual(mesh["indices"], [])
        self.assertEqual(mesh["groups"], [])

    def assert_closed_mesh(self, mesh):
        self.assertGreater(mesh["vertexCount"], 0)
        self.assertGreater(mesh["indexCount"], 0)

        for a, b, c in mesh_triangles(mesh):
            self.assertGreater(
                norm(cross(subtract(b, a), subtract(c, a))),
                1e-12
            )

        undirected, directed = mesh_edge_counts(mesh)

        self.assertTrue(undirected)

        invalid_edges = {
            edge: count
            for edge, count in undirected.items()
            if count != 2
        }

        self.assertFalse(
            invalid_edges,
            f"Open or non-manifold edges: {list(invalid_edges.items())[:10]}"
        )

        for first, second in undirected:
            self.assertEqual(
                directed[(first, second)],
                directed[(second, first)],
                f"Inconsistent face orientation at edge {first}, {second}"
            )

        self.assertGreater(mesh_volume(mesh), 0.0)

    def assert_mesh_equivalent(self, before, after, exact_triangles=True):
        self.assert_bounds(after, mesh_bounds(before))
        self.assert_volume(after, mesh_volume(before))
        self.assert_surface(after, mesh_surface_area(before))

        self.assertEqual(
            mesh_vertex_set(before),
            mesh_vertex_set(after)
        )

        if exact_triangles:
            self.assertEqual(
                mesh_signature(before),
                mesh_signature(after)
            )

    def assert_concatenation(self, combined, individual_meshes):
        self.assertEqual(
            combined["vertexCount"],
            sum(mesh["vertexCount"] for mesh in individual_meshes)
        )

        self.assertEqual(
            combined["indexCount"],
            sum(mesh["indexCount"] for mesh in individual_meshes)
        )

        expected_triangles = Counter()

        for mesh in individual_meshes:
            expected_triangles.update(mesh_signature(mesh))

        self.assertEqual(
            mesh_signature(combined),
            expected_triangles,
            "Combined domain mesh must preserve all individual triangles"
        )

        self.assert_volume(
            combined,
            math.fsum(mesh_volume(mesh) for mesh in individual_meshes)
        )

        self.assert_surface(
            combined,
            math.fsum(mesh_surface_area(mesh) for mesh in individual_meshes)
        )

        self.assertEqual(combined["groups"], [])

    def assert_extrusion(self, mesh, profile, depth, matrix=None):
        self.assert_closed_mesh(mesh)

        area = polygon_area(profile)
        perimeter = polygon_perimeter(profile)

        self.assert_volume(mesh, area * depth)
        self.assert_surface(mesh, 2 * area + perimeter * depth)

        expected_points = [
            (x, y, z)
            for x, y in profile
            for z in (0.0, depth)
        ]

        if matrix is not None:
            expected_points = [
                transform_point(matrix, point)
                for point in expected_points
            ]

        self.assert_bounds(
            mesh,
            bounds_from_points(expected_points)
        )

        self.assertEqual(
            mesh_vertex_set(mesh),
            {quantize(point) for point in expected_points}
        )

    def test_overlapping_domains(self):
        """Verify that overlapping domains remain independent."""

        first = self.create_domain(
            name="First overlapping domain",
            profile=rectangular_profile(4, 4),
            depth=4
        )

        second = self.create_domain(
            name="Second overlapping domain",
            profile=rectangular_profile(4, 4),
            depth=4,
            matrix=translation_matrix(2, 0, 0)
        )

        first_mesh = self.domain_mesh(first)
        second_mesh = self.domain_mesh(second)

        self.assert_closed_mesh(first_mesh)
        self.assert_closed_mesh(second_mesh)

        self.assert_volume(first_mesh, 64)
        self.assert_volume(second_mesh, 64)

        self.assert_bounds(
            first_mesh,
            (0, 0, 0, 4, 4, 4)
        )

        self.assert_bounds(
            second_mesh,
            (2, 0, 0, 6, 4, 4)
        )

        first_surface = ClosedSurface(first_mesh)
        second_surface = ClosedSurface(second_mesh)

        overlap_point = (3, 2, 2)

        self.assertTrue(first_surface.contains(overlap_point))
        self.assertTrue(second_surface.contains(overlap_point))

        self.assertTrue(first_surface.contains((1, 2, 2)))
        self.assertFalse(second_surface.contains((1, 2, 2)))

        self.assertFalse(first_surface.contains((5, 2, 2)))
        self.assertTrue(second_surface.contains((5, 2, 2)))

        combined = self.tessellate(kind="DOMAIN")

        self.assert_concatenation(
            combined,
            [first_mesh, second_mesh]
        )

        # Independent volume is 128 m³ rather than the 96 m³ union.

        self.assert_volume(combined, 128)

        updated = self.update_geometry(
            first["id"],
            depth=6
        )

        self.assert_volume(self.domain_mesh(updated), 96)

        self.assert_mesh_equivalent(
            second_mesh,
            self.domain_mesh(second)
        )

        self.delete_geometry(first["id"])

        self.assert_mesh_equivalent(
            second_mesh,
            self.tessellate(kind="DOMAIN")
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            1
        )

    def test_nested_domains(self):
        """Verify complete containment of one domain inside another."""

        outer = self.create_domain(
            name="Outer domain",
            profile=rectangular_profile(10, 10),
            depth=10
        )

        inner = self.create_domain(
            name="Inner domain",
            profile=rectangular_profile(2, 3),
            depth=4,
            matrix=translation_matrix(2, 3, 1)
        )

        outer_mesh = self.domain_mesh(outer)
        inner_mesh = self.domain_mesh(inner)

        self.assert_closed_mesh(outer_mesh)
        self.assert_closed_mesh(inner_mesh)

        self.assert_volume(outer_mesh, 1000)
        self.assert_volume(inner_mesh, 24)

        outer_surface = ClosedSurface(outer_mesh)

        for point in mesh_vertex_set(inner_mesh):
            self.assertTrue(
                outer_surface.contains(point),
                f"Inner domain vertex outside outer domain: {point}"
            )

        combined = self.tessellate(kind="DOMAIN")

        self.assert_concatenation(
            combined,
            [outer_mesh, inner_mesh]
        )

        self.assert_volume(combined, 1024)

        self.save_and_reload()

        outer = self.find_geometry(outer["globalId"])
        inner = self.find_geometry(inner["globalId"])

        self.assert_mesh_equivalent(
            outer_mesh,
            self.domain_mesh(outer)
        )

        self.assert_mesh_equivalent(
            inner_mesh,
            self.domain_mesh(inner)
        )

        self.delete_geometry(outer["id"])

        self.assert_mesh_equivalent(
            inner_mesh,
            self.tessellate(kind="DOMAIN")
        )

        self.assert_volume(self.domain_mesh(inner), 24)

    def test_disconnected_domains(self):
        """Verify multiple spatially disconnected simulation domains."""

        domains = [
            self.create_domain(
                name=f"Disconnected domain {index}",
                profile=rectangular_profile(2, 2),
                depth=2,
                matrix=translation_matrix(index * 20, 0, 0)
            )
            for index in range(4)
        ]

        meshes = [
            self.domain_mesh(domain)
            for domain in domains
        ]

        for mesh in meshes:
            self.assert_closed_mesh(mesh)
            self.assert_volume(mesh, 8)

        combined = self.tessellate(kind="DOMAIN")

        self.assert_concatenation(combined, meshes)
        self.assert_closed_mesh(combined)

        self.assert_volume(combined, 32)

        self.assert_bounds(
            combined,
            (0, 0, 0, 62, 2, 2)
        )

        components = mesh_connected_components(combined)

        self.assertEqual(len(components), 4)

        for component in components:
            self.assertEqual(len(component), 8)

        self.assertEqual(
            mesh_euler_characteristic(combined),
            8
        )

        surfaces = [ClosedSurface(mesh) for mesh in meshes]

        for index, surface in enumerate(surfaces):
            centre = (index * 20 + 1, 1, 1)

            self.assertTrue(surface.contains(centre))

            self.assertEqual(
                sum(item.contains(centre) for item in surfaces),
                1
            )

        self.assertFalse(
            any(surface.contains((10, 1, 1)) for surface in surfaces)
        )

    def test_domain_solid_intersection(self):
        """Verify that physical solids crossing a domain do not modify it."""

        domain = self.create_domain(
            profile=rectangular_profile(6, 2, x=-3, y=-1),
            depth=2,
            matrix=translation_matrix(z=1)
        )

        domain_before = self.domain_mesh(domain)

        self.assert_closed_mesh(domain_before)
        self.assert_volume(domain_before, 24)

        physical = self.physical_mesh()

        self.assert_volume(physical, PHYSICAL_VOLUME)

        domain_surface = ClosedSurface(domain_before)
        physical_surface = ClosedSurface(physical)

        self.assertTrue(domain_surface.contains((0, 0, 2)))
        self.assertTrue(physical_surface.contains((0, 0, 2)))

        self.assertTrue(domain_surface.contains((2.5, 0, 2)))
        self.assertFalse(physical_surface.contains((2.5, 0, 2)))

        opening = self.create_solid(
            name="Opening intersecting domain",
            profile=rectangular_profile(1, 1, x=-0.5, y=-0.5),
            depth=6,
            matrix=translation_matrix(z=-1),
            operation="SUBTRACT"
        )

        physical_after = self.physical_mesh()

        self.assert_volume(physical_after, 60)

        self.assert_mesh_equivalent(
            domain_before,
            self.domain_mesh(domain)
        )

        addition = self.create_solid(
            name="Addition intersecting domain",
            profile=rectangular_profile(3, 2, x=1, y=-1),
            depth=2,
            matrix=translation_matrix(z=1)
        )

        self.assert_mesh_equivalent(
            domain_before,
            self.domain_mesh(domain)
        )

        self.delete_geometry(opening["id"])
        self.delete_geometry(addition["id"])

        self.assert_mesh_equivalent(
            domain_before,
            self.domain_mesh(domain)
        )

        self.assert_mesh_equivalent(
            physical,
            self.physical_mesh()
        )

    def test_domain_inside_solid(self):
        """Verify that a domain can exist entirely inside physical matter."""

        physical_before = self.physical_mesh()

        domain = self.create_domain(
            profile=rectangular_profile(2, 2, x=-1, y=-1),
            depth=2,
            matrix=translation_matrix(z=1)
        )

        mesh = self.domain_mesh(domain)

        self.assert_closed_mesh(mesh)
        self.assert_volume(mesh, 8)

        self.assert_bounds(
            mesh,
            (-1, -1, 1, 1, 1, 3)
        )

        physical_surface = ClosedSurface(physical_before)

        for point in mesh_vertex_set(mesh):
            self.assertTrue(
                physical_surface.contains(point)
            )

        domain_surface = ClosedSurface(mesh)

        self.assertTrue(domain_surface.contains((0, 0, 2)))

        self.assertEqual(
            classify_point(
                (0, 0, 2),
                [domain_surface],
                [physical_surface]
            ),
            SOLID
        )

        self.assert_mesh_equivalent(
            physical_before,
            self.physical_mesh()
        )

        self.save_and_reload()

        current = self.find_geometry(domain["globalId"])

        self.assert_mesh_equivalent(
            mesh,
            self.domain_mesh(current)
        )

    def test_solid_inside_domain(self):
        """Verify a domain enclosing multiple physical geometries."""

        first = self.create_solid(
            name="First contained physical feature",
            profile=rectangular_profile(2, 2),
            depth=2,
            matrix=translation_matrix(5, 0, 0)
        )

        second = self.create_solid(
            name="Second contained physical feature",
            profile=rectangular_profile(2, 2),
            depth=2,
            matrix=translation_matrix(-7, 0, 0)
        )

        physical_before = self.physical_mesh()

        self.assertGreater(
            len(self.list_geometries(kind="SOLID")),
            2
        )

        domain = self.create_domain(
            name="Enclosing domain",
            profile=rectangular_profile(20, 10, x=-10, y=-5),
            depth=10,
            matrix=translation_matrix(z=-2)
        )

        mesh = self.domain_mesh(domain)

        self.assert_closed_mesh(mesh)
        self.assert_volume(mesh, 2000)

        domain_surface = ClosedSurface(mesh)

        for point in mesh_vertex_set(physical_before):
            self.assertTrue(
                domain_surface.contains(point),
                f"Physical vertex outside domain: {point}"
            )

        for point in (
            (0, 0, 2),
            (6, 1, 1),
            (-6, 1, 1)
        ):
            self.assertTrue(domain_surface.contains(point))

        self.assert_mesh_equivalent(
            physical_before,
            self.physical_mesh()
        )

        self.delete_geometry(first["id"])
        self.delete_geometry(second["id"])

        self.assert_mesh_equivalent(
            mesh,
            self.domain_mesh(domain)
        )

    def test_rotated_domain(self):
        """Verify arbitrary three-dimensional domain rotations."""

        profile = rectangular_profile(4, 3)
        depth = 2.5

        matrix = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(37),
            rotation_y(29),
            rotation_x(53)
        )

        domain = self.create_domain(
            profile=profile,
            depth=depth,
            matrix=matrix
        )

        mesh = self.domain_mesh(domain)

        self.assert_extrusion(
            mesh,
            profile,
            depth,
            matrix
        )

        self.assert_volume(mesh, 30)
        self.assert_surface(mesh, 59)

        surface = ClosedSurface(mesh)

        self.assertTrue(
            surface.contains(
                transform_point(matrix, (2, 1.5, 1.25))
            )
        )

        self.assertFalse(
            surface.contains(
                transform_point(matrix, (4.5, 1.5, 1.25))
            )
        )

        parent_matrix = compose_matrices(
            translation_matrix(100, 200, 300),
            rotation_z(21),
            rotation_y(17)
        )

        self.client.request(
            "transformUpdate",
            {
                "id": self.root_id,
                "transform": transform_input(
                    parent_matrix,
                    space="world"
                )
            }
        )

        self.assert_mesh_equivalent(
            mesh,
            self.domain_mesh(domain, space="local")
        )

        world_mesh = self.domain_mesh(
            domain,
            space="world"
        )

        self.assert_extrusion(
            world_mesh,
            profile,
            depth,
            multiply_matrices(parent_matrix, matrix)
        )

        self.save_and_reload()

        current = self.find_geometry(domain["globalId"])

        self.assert_mesh_equivalent(
            world_mesh,
            self.domain_mesh(current, space="world")
        )

    def test_concave_domain(self):
        """Verify watertight extrusion of concave polygonal domains."""

        profiles = {
            "L": [
                [0, 0],
                [6, 0],
                [6, 2],
                [2, 2],
                [2, 5],
                [0, 5]
            ],
            "U": [
                [0, 0],
                [6, 0],
                [6, 6],
                [4, 6],
                [4, 2],
                [2, 2],
                [2, 6],
                [0, 6]
            ],
            "T": [
                [0, 0],
                [6, 0],
                [6, 2],
                [4, 2],
                [4, 6],
                [2, 6],
                [2, 2],
                [0, 2]
            ]
        }

        expected_areas = {
            "L": 18,
            "U": 28,
            "T": 20
        }

        for name, profile in profiles.items():
            for reverse in (False, True):
                with self.subTest(profile=name, reverse=reverse):
                    points = list(reversed(profile)) if reverse else profile

                    domain = self.create_domain(
                        name=f"Concave domain {name}",
                        profile=points,
                        depth=3
                    )

                    mesh = self.domain_mesh(domain)

                    self.assert_extrusion(mesh, points, 3)

                    self.assert_volume(
                        mesh,
                        expected_areas[name] * 3
                    )

                    self.assertEqual(
                        len(mesh_connected_components(mesh)),
                        1
                    )

                    self.assertEqual(
                        mesh_euler_characteristic(mesh),
                        2
                    )

                    surface = ClosedSurface(mesh)

                    for x, y in itertools.product(
                        (0.5, 1.5, 2.5, 3.5, 4.5, 5.5),
                        repeat=2
                    ):
                        expected = point_in_polygon((x, y), points)

                        self.assertEqual(
                            surface.contains((x, y, 1.5)),
                            expected,
                            f"Incorrect concave classification at {(x, y)}"
                        )

                    self.delete_geometry(domain["id"])

        self.assert_empty_mesh(
            self.tessellate(kind="DOMAIN")
        )

    def test_domain_volume(self):
        """Verify analytical domain volume and surface area."""

        circle = [
            [
                3 * math.cos(2 * math.pi * index / 128),
                3 * math.sin(2 * math.pi * index / 128)
            ]
            for index in range(128)
        ]

        cases = [
            (
                "Triangle",
                [[0, 0], [4, 0], [0, 3]],
                2.5,
                15.0
            ),
            (
                "Rectangle",
                rectangular_profile(5, 3),
                4,
                60.0
            ),
            (
                "Concave",
                [[0, 0], [6, 0], [6, 2], [2, 2], [2, 5], [0, 5]],
                3,
                54.0
            ),
            (
                "Fractional",
                rectangular_profile(0.125, 0.375),
                0.625,
                0.125 * 0.375 * 0.625
            ),
            (
                "Negative coordinates",
                rectangular_profile(4, 3, x=-5, y=-6),
                2,
                24.0
            ),
            (
                "Circle approximation",
                circle,
                5,
                128 * 9 * math.sin(2 * math.pi / 128) * 5 / 2
            )
        ]

        for name, profile, depth, expected_volume in cases:
            with self.subTest(profile=name):
                domain = self.create_domain(
                    name=f"Volume verification {name}",
                    profile=profile,
                    depth=depth
                )

                mesh = self.domain_mesh(domain)

                self.assert_extrusion(mesh, profile, depth)
                self.assert_volume(mesh, expected_volume)

                updated = self.update_geometry(
                    domain["id"],
                    depth=depth * 2
                )

                updated_mesh = self.domain_mesh(updated)

                self.assert_extrusion(
                    updated_mesh,
                    profile,
                    depth * 2
                )

                self.assert_volume(
                    updated_mesh,
                    expected_volume * 2
                )

                self.delete_geometry(domain["id"])

    def test_domain_children(self):
        """Verify domains associated with multiple hierarchy levels."""

        child = self.create_transform(
            name="First child",
            parent_id=self.root_id,
            matrix=translation_matrix(10, 0, 0)
        )

        grandchild = self.create_transform(
            name="Grandchild",
            parent_id=child["id"],
            matrix=translation_matrix(0, 20, 0)
        )

        sibling = self.create_transform(
            name="Second child",
            parent_id=self.root_id,
            matrix=translation_matrix(30, 0, 0)
        )

        independent = self.create_transform(
            name="Independent transform",
            matrix=translation_matrix(100, 0, 0)
        )

        owners = [
            self.root_id,
            child["id"],
            grandchild["id"],
            sibling["id"],
            independent["id"]
        ]

        domains = [
            self.create_domain(
                parent_id=owner,
                name=f"Hierarchy domain {index}",
                profile=rectangular_profile(2, 2),
                depth=index + 1
            )
            for index, owner in enumerate(owners)
        ]

        root_mesh = self.tessellate(
            kind="DOMAIN",
            include_children=False
        )

        self.assert_volume(root_mesh, 4)

        child_mesh = self.tessellate(
            child["id"],
            kind="DOMAIN",
            include_children=True
        )

        self.assert_volume(child_mesh, 20)

        combined = self.tessellate(
            kind="DOMAIN",
            include_children=True
        )

        self.assert_volume(combined, 40)

        self.assert_bounds(
            combined,
            (0, 0, 0, 32, 22, 4)
        )

        individual_meshes = [
            self.tessellate(
                self.root_id,
                kind="DOMAIN",
                geometry_id=domain["id"],
                include_children=True
            )
            for domain in domains[:4]
        ]

        self.assert_concatenation(
            combined,
            individual_meshes
        )

        self.assertEqual(
            len(mesh_connected_components(combined)),
            4
        )

        self.assert_volume(
            self.domain_mesh(domains[4]),
            20
        )

        self.save_and_reload()

        self.assert_mesh_equivalent(
            combined,
            self.tessellate(
                kind="DOMAIN",
                include_children=True
            )
        )

    def test_domain_isolation(self):
        """Verify complete exclusion of domain triangles from SOLID meshes."""

        baseline = self.physical_mesh()

        self.assert_volume(baseline, PHYSICAL_VOLUME)
        self.assert_bounds(baseline, PHYSICAL_BOUNDS)

        domains = [
            self.create_domain(
                name=f"Isolated domain {index}",
                profile=rectangular_profile(6, 6),
                depth=6,
                matrix=translation_matrix(index * 20, 10, 20)
            )
            for index in range(3)
        ]

        physical_after = self.physical_mesh()

        self.assert_mesh_equivalent(
            baseline,
            physical_after
        )

        physical_triangles = mesh_signature(physical_after)

        for domain in domains:
            mesh = self.domain_mesh(domain)

            self.assertFalse(
                set(physical_triangles) & set(mesh_signature(mesh)),
                "Domain triangles leaked into physical tessellation"
            )

        combined_domains = self.tessellate(kind="DOMAIN")

        self.assert_volume(combined_domains, 648)

        self.assertEqual(
            len(self.list_geometries(kind="SOLID")),
            1
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            3
        )

        # Omitting kind must retain the default SOLID behaviour.

        params = {
            "id": self.root_id,
            "options": {
                "space": "local",
                "includeChildren": False,
                "includeNormals": False,
                "includeMaterials": False
            }
        }

        request_id = self.client.send(
            "transformTessellate",
            params
        )

        vertices = []
        indices = []

        while True:
            response = self.client.read(request_id)

            self.assertIs(response["ok"], True)

            event = response["result"]
            name = event["event"]

            if name == "mesh.vertices":
                vertices.extend(event["values"])

            elif name == "mesh.indices":
                indices.extend(event["values"])

            elif name == "mesh.end":
                break

        self.assertEqual(
            mesh_signature({
                "vertices": vertices,
                "indices": indices
            }),
            mesh_signature(baseline)
        )

    def test_domain_mesh_concatenation(self):
        """Verify concatenation without boolean union or deduplication."""

        first = self.create_domain(
            name="First identical domain",
            profile=rectangular_profile(4, 3),
            depth=2
        )

        second = self.create_domain(
            name="Second identical domain",
            profile=rectangular_profile(4, 3),
            depth=2
        )

        third = self.create_domain(
            name="Partially overlapping domain",
            profile=rectangular_profile(4, 3),
            depth=2,
            matrix=translation_matrix(2, 0, 0)
        )

        meshes = [
            self.domain_mesh(domain)
            for domain in (first, second, third)
        ]

        for mesh in meshes:
            self.assert_closed_mesh(mesh)
            self.assert_volume(mesh, 24)

        self.assert_mesh_equivalent(meshes[0], meshes[1])

        combined = self.tessellate(kind="DOMAIN")

        self.assert_concatenation(combined, meshes)
        self.assert_volume(combined, 72)

        self.assertGreater(
            combined["vertexCount"],
            len(mesh_vertex_set(combined))
        )

        combined_signature = mesh_signature(combined)
        first_signature = mesh_signature(meshes[0])

        for triangle, multiplicity in first_signature.items():
            self.assertGreaterEqual(
                combined_signature[triangle],
                multiplicity * 2
            )

        self.delete_geometry(second["id"])

        self.assert_concatenation(
            self.tessellate(kind="DOMAIN"),
            [meshes[0], meshes[2]]
        )

    def test_invalid_domain_selection(self):
        """Verify rejection of domain IDs outside the selected hierarchy."""

        independent = self.create_transform(
            name="Independent domain owner"
        )

        child = self.create_transform(
            name="Child domain owner",
            parent_id=self.root_id
        )

        own = self.create_domain(
            name="Root domain"
        )

        foreign = self.create_domain(
            parent_id=independent["id"],
            name="Foreign domain"
        )

        descendant = self.create_domain(
            parent_id=child["id"],
            name="Descendant domain"
        )

        physical = self.list_geometries(
            self.root_id,
            kind="SOLID"
        )[0]

        cases = [
            (
                foreign["id"],
                "DOMAIN",
                False,
                "INVALID_PARAMETERS"
            ),
            (
                foreign["id"],
                "DOMAIN",
                True,
                "INVALID_PARAMETERS"
            ),
            (
                descendant["id"],
                "DOMAIN",
                False,
                "INVALID_PARAMETERS"
            ),
            (
                physical["id"],
                "DOMAIN",
                True,
                "INVALID_PARAMETERS"
            ),
            (
                own["id"],
                "SOLID",
                False,
                "INVALID_PARAMETERS"
            ),
            (
                "geometry:999999999",
                "DOMAIN",
                True,
                "ENTITY_NOT_FOUND"
            )
        ]

        for geometry_id, kind, children, code in cases:
            with self.subTest(
                geometry=geometry_id,
                kind=kind,
                include_children=children
            ):
                params = self.client.tessellation_parameters(
                    self.root_id,
                    kind=kind,
                    geometry_id=geometry_id,
                    include_children=children
                )

                self.client.expect_error(
                    "transformTessellate",
                    params,
                    code
                )

        valid = self.tessellate(
            kind="DOMAIN",
            geometry_id=descendant["id"],
            include_children=True
        )

        self.assert_volume(valid, 24)

        self.assert_volume(
            self.domain_mesh(own),
            24
        )

        self.assert_volume(
            self.domain_mesh(foreign),
            24
        )

    def test_native_domain_selection(self):
        """Verify native space tessellation versus managed domain selection."""

        self.load_fixture(NATIVE_SPACE_ENTITIES)

        native_space = self.find_transform(
            NATIVE_SPACE_GLOBAL_ID
        )

        self.assertEqual(
            native_space["definition"]["ifcClass"],
            "IfcSpace"
        )

        native_geometries = self.list_geometries(
            native_space["id"],
            kind="DOMAIN"
        )

        self.assertEqual(len(native_geometries), 1)

        native_geometry = self.get_geometry(
            native_geometries[0]["id"]
        )

        self.assertEqual(native_geometry["source"], "ifc")
        self.assertEqual(native_geometry["kind"], "DOMAIN")
        self.assertEqual(native_geometry["operation"], "BASE")
        self.assertIs(native_geometry["editable"], False)

        native_mesh = self.tessellate(
            native_space["id"],
            kind="DOMAIN"
        )

        self.assert_closed_mesh(native_mesh)
        self.assert_volume(native_mesh, 90)

        self.assert_empty_mesh(
            self.physical_mesh(native_space["id"])
        )

        self.client.expect_error(
            "transformTessellate",
            self.client.tessellation_parameters(
                native_space["id"],
                kind="DOMAIN",
                geometry_id=native_geometry["id"]
            ),
            "INVALID_PARAMETERS"
        )

        managed = self.create_domain(
            parent_id=native_space["id"],
            name="Managed domain attached to native space",
            profile=rectangular_profile(2, 2),
            depth=2,
            matrix=translation_matrix(10, 0, 0)
        )

        managed_mesh = self.domain_mesh(managed)

        self.assert_volume(managed_mesh, 8)

        self.assert_concatenation(
            self.tessellate(
                native_space["id"],
                kind="DOMAIN"
            ),
            [native_mesh, managed_mesh]
        )

        self.assert_empty_mesh(
            self.physical_mesh(native_space["id"])
        )

        self.save_and_reload()

        current_space = self.find_transform(
            NATIVE_SPACE_GLOBAL_ID
        )

        current_managed = self.find_geometry(
            managed["globalId"]
        )

        self.assertEqual(
            current_managed["parentId"],
            current_space["id"]
        )

        self.assert_mesh_equivalent(
            managed_mesh,
            self.domain_mesh(current_managed)
        )

        self.assert_volume(
            self.tessellate(
                current_space["id"],
                kind="DOMAIN"
            ),
            98
        )

    def test_domain_material_groups(self):
        """Verify that simulation domains never emit physical material groups."""

        material = self.create_material()

        self.assign_material(
            material["id"],
            self.root_id
        )

        physical_mesh = self.physical_mesh(
            include_materials=True
        )

        self.assertEqual(
            physical_mesh["groups"],
            [{
                "materialId": material["id"],
                "indexOffset": 0,
                "indexCount": physical_mesh["indexCount"]
            }]
        )

        domains = [
            self.create_domain(
                name=f"Material-free domain {index}",
                matrix=translation_matrix(index * 10, 0, 0)
            )
            for index in range(2)
        ]

        for domain in domains:
            mesh = self.domain_mesh(
                domain,
                include_materials=True
            )

            self.assertIn(
                "mesh.materials",
                mesh["events"]
            )

            self.assertEqual(mesh["groups"], [])

            self.client.expect_error(
                "materialAssign",
                {
                    "materialId": material["id"],
                    "targetId": domain["id"]
                },
                "INVALID_RELATION"
            )

            self.assertEqual(
                self.get_geometry(domain["id"])["materials"],
                []
            )

            without_materials = self.domain_mesh(
                domain,
                include_materials=False
            )

            self.assertNotIn(
                "mesh.materials",
                without_materials["events"]
            )

            self.assert_mesh_equivalent(
                mesh,
                without_materials
            )

        combined = self.tessellate(
            kind="DOMAIN",
            include_materials=True
        )

        self.assertEqual(combined["groups"], [])

        self.assertEqual(
            self.physical_mesh()["groups"],
            physical_mesh["groups"]
        )

        self.save_and_reload()

        self.assertEqual(
            self.tessellate(kind="DOMAIN")["groups"],
            []
        )

    def test_domain_invalid_parent(self):
        """Verify rejection of incompatible domain parents."""

        baseline = self.list_geometries()

        cases = [
            (self.project_id, "INVALID_RELATION"),
            (self.virtual_id, "INVALID_RELATION"),
            ("transform:999999999", "ENTITY_NOT_FOUND"),
            (baseline[0]["id"], "INVALID_ENTITY_TYPE")
        ]

        for parent_id, error_code in cases:
            with self.subTest(parent=parent_id):
                self.client.expect_error(
                    "geometryCreate",
                    self.geometry_parameters(
                        parent_id=parent_id
                    ),
                    error_code
                )

                self.assertEqual(
                    self.list_geometries(),
                    baseline
                )

                self.assertEqual(
                    self.list_geometries(kind="DOMAIN"),
                    []
                )

        self.assert_empty_mesh(
            self.tessellate(kind="DOMAIN")
        )

        # A virtual product becomes a valid parent after receiving
        # a compatible local placement.

        updated = self.client.request(
            "transformUpdate",
            {
                "id": self.virtual_id,
                "transform": transform_input(
                    translation_matrix(10, 20, 30)
                )
            }
        )

        self.assertEqual(
            updated["transforms"]["sourceType"],
            "IfcLocalPlacement"
        )

        domain = self.create_domain(
            parent_id=updated["id"],
            depth=3
        )

        self.assert_volume(
            self.domain_mesh(domain),
            36
        )

        self.assert_bounds(
            self.domain_mesh(domain, space="world"),
            (10, 20, 30, 14, 23, 33)
        )

    def test_orphan_domain(self):
        """Verify rejection of managed domains without a valid parent relation."""

        cases = {
            "missing_relation": ORPHAN_DOMAIN_ENTITIES,
            "invalid_relation_marker": (
                ORPHAN_DOMAIN_ENTITIES
                + ORPHAN_INVALID_MARKER_ENTITIES
            ),
            "unindexed_parent": (
                ORPHAN_DOMAIN_ENTITIES
                + ORPHAN_INVALID_PARENT_ENTITIES
            )
        }

        for name, extra_entities in cases.items():
            with self.subTest(case=name):
                path = self.root_path / f"{name}.ifc"

                path.write_text(
                    build_fixture(extra_entities),
                    encoding="utf-8"
                )

                self.client.expect_error(
                    "load",
                    {"path": str(path)},
                    "INVALID_RELATION"
                )

                self.client.expect_error(
                    "transformGetAll",
                    {},
                    "NO_MODEL_LOADED"
                )

                self.client.expect_error(
                    "geometryGetAll",
                    {
                        "transformId": None,
                        "kind": "DOMAIN"
                    },
                    "NO_MODEL_LOADED"
                )

                self.load_fixture()

                self.assertEqual(
                    self.list_geometries(kind="DOMAIN"),
                    []
                )

                self.assert_volume(
                    self.physical_mesh(),
                    PHYSICAL_VOLUME
                )

        # Verify that the same native zone becomes loadable when its
        # marked relation points to an indexed product.

        valid_relation = """
#47=IFCRELASSIGNSTOPRODUCT('0000000000000000000007',$,'ifc-cli:domain-parent',$,(#45),$,#15);
"""

        self.load_fixture(
            ORPHAN_DOMAIN_ENTITIES + valid_relation
        )

        domain = self.find_geometry(
            ORPHAN_DOMAIN_GLOBAL_ID
        )

        self.assertEqual(domain["kind"], "DOMAIN")
        self.assertEqual(domain["parentId"], self.root_id)

        self.assert_volume(
            self.domain_mesh(domain),
            8
        )

    def test_domain_failed_update(self):
        """Verify that rejected updates do not corrupt domain state."""

        domain = self.create_domain(
            name="Stable domain",
            profile=rectangular_profile(5, 3),
            depth=2,
            matrix=translation_matrix(2, 3, 4)
        )

        reference = self.get_geometry(domain["id"])
        reference_mesh = self.domain_mesh(domain)

        scaled_matrix = IDENTITY_MATRIX.copy()
        scaled_matrix[0] = 2.0

        cases = [
            (
                {"operation": "SUBTRACT"},
                "INVALID_PARAMETERS"
            ),
            (
                {"kind": "SOLID"},
                "INVALID_PARAMETERS"
            ),
            (
                {"depth": 0},
                "INVALID_PARAMETERS"
            ),
            (
                {"depth": -2},
                "INVALID_PARAMETERS"
            ),
            (
                {"profile": [[0, 0], [1, 1]]},
                "INVALID_PARAMETERS"
            ),
            (
                {"profile": [[0, 0], [1, 0], [2, 0]]},
                "INVALID_PROFILE"
            ),
            (
                {
                    "profile": [
                        [0, 0],
                        [4, 0],
                        [4, 3],
                        [0, 3],
                        [0, 0]
                    ]
                },
                "INVALID_PROFILE"
            ),
            (
                {"transform": transform_input(scaled_matrix)},
                "INVALID_PLACEMENT"
            )
        ]

        for changes, error_code in cases:
            with self.subTest(changes=changes):
                self.client.expect_error(
                    "geometryUpdate",
                    {
                        "id": domain["id"],
                        **changes
                    },
                    error_code
                )

                self.assertEqual(
                    self.get_geometry(domain["id"]),
                    reference
                )

                self.assert_mesh_equivalent(
                    reference_mesh,
                    self.domain_mesh(domain)
                )

                self.assertEqual(
                    len(self.list_geometries(kind="DOMAIN")),
                    1
                )

        self.save_and_reload()

        domain = self.find_geometry(domain["globalId"])

        self.assert_mesh_equivalent(
            reference_mesh,
            self.domain_mesh(domain)
        )

        updated = self.update_geometry(
            domain["id"],
            depth=4
        )

        self.assert_volume(
            self.domain_mesh(updated),
            60
        )

        # Combined updates must be atomic even when a field that is
        # validated later in the update sequence is invalid.

        atomic_reference = self.get_geometry(updated["id"])
        atomic_mesh = self.domain_mesh(updated)

        self.client.expect_error(
            "geometryUpdate",
            {
                "id": updated["id"],
                "name": "Rejected partial rename",
                "transform": transform_input(
                    translation_matrix(100, 200, 300)
                ),
                "profile": [[0, 0], [1, 0], [2, 0]],
                "depth": 10
            },
            "INVALID_PROFILE"
        )

        self.assertEqual(
            self.get_geometry(updated["id"]),
            atomic_reference,
            "Rejected combined update partially modified the domain"
        )

        self.assert_mesh_equivalent(
            atomic_mesh,
            self.domain_mesh(updated)
        )

    def test_domain_spatial_reconstruction(self):
        """Verify closed boundary reconstruction and point classification."""

        profile = [
            [0, 0],
            [6, 0],
            [6, 2],
            [2, 2],
            [2, 5],
            [0, 5]
        ]

        depth = 4

        domain = self.create_domain(
            name="Spatial reconstruction domain",
            profile=profile,
            depth=depth
        )

        mesh = self.domain_mesh(domain)

        self.assert_closed_mesh(mesh)
        self.assert_volume(mesh, 72)

        self.assertEqual(
            len(mesh_connected_components(mesh)),
            1
        )

        self.assertEqual(
            mesh_euler_characteristic(mesh),
            2
        )

        surface = ClosedSurface(mesh)

        inside_points = [
            (1, 1, 1),
            (5, 1, 2),
            (1, 4, 3),
            (1.5, 1.5, 0.5)
        ]

        outside_points = [
            (-1, 1, 1),
            (3, 3, 2),
            (5, 4, 2),
            (1, 1, -1),
            (1, 1, 5),
            (7, 1, 2)
        ]

        for point in inside_points:
            with self.subTest(point=point):
                self.assertTrue(surface.contains(point))

                self.assertAlmostEqual(
                    abs(surface.winding_number(point)),
                    1.0,
                    delta=MESH_TOLERANCE
                )

        for point in outside_points:
            with self.subTest(point=point):
                self.assertFalse(surface.contains(point))

                self.assertAlmostEqual(
                    surface.winding_number(point),
                    0.0,
                    delta=MESH_TOLERANCE
                )

        coordinates = [index + 0.5 for index in range(-2, 8)]

        for point in itertools.product(coordinates, repeat=3):
            x, y, z = point

            expected = (
                0 < z < depth
                and point_in_polygon((x, y), profile)
            )

            self.assertEqual(
                surface.contains(point),
                expected,
                f"Incorrect spatial reconstruction at {point}"
            )

        matrix = compose_matrices(
            translation_matrix(10, 20, 30),
            rotation_z(37),
            rotation_y(23),
            rotation_x(19)
        )

        updated = self.update_geometry(
            domain["id"],
            transform=transform_input(matrix)
        )

        rotated_mesh = self.domain_mesh(updated)
        rotated_surface = ClosedSurface(rotated_mesh)

        self.assert_extrusion(
            rotated_mesh,
            profile,
            depth,
            matrix
        )

        for point in inside_points:
            self.assertTrue(
                rotated_surface.contains(
                    transform_point(matrix, point)
                )
            )

        for point in outside_points:
            self.assertFalse(
                rotated_surface.contains(
                    transform_point(matrix, point)
                )
            )

    def test_voxel_occupancy(self):
        """Verify voxel classification of exterior, free space and matter."""

        domain = self.create_domain(
            name="Voxelization domain",
            profile=rectangular_profile(6, 6, x=-3, y=-3),
            depth=6,
            matrix=translation_matrix(z=-1)
        )

        domain_mesh = self.domain_mesh(domain)
        physical_mesh = self.physical_mesh()

        self.assert_closed_mesh(domain_mesh)
        self.assert_closed_mesh(physical_mesh)

        self.assert_volume(domain_mesh, 216)
        self.assert_volume(physical_mesh, 64)

        domain_surface = ClosedSurface(domain_mesh)
        physical_surface = ClosedSurface(physical_mesh)

        origin = (-4, -4, -2)
        dimensions = (8, 8, 8)
        spacing = 1.0

        occupancy = voxelize(
            origin,
            dimensions,
            spacing,
            [domain_surface],
            [physical_surface]
        )

        counts = Counter(occupancy.values())

        self.assertEqual(
            counts,
            {
                OUTSIDE: 296,
                FREE: 152,
                SOLID: 64
            }
        )

        self.assertEqual(
            len(occupancy),
            math.prod(dimensions)
        )

        for index, state in occupancy.items():
            x, y, z = (
                origin[axis] + (index[axis] + 0.5) * spacing
                for axis in range(3)
            )

            inside_domain = (
                -3 < x < 3
                and -3 < y < 3
                and -1 < z < 5
            )

            inside_solid = (
                -2 < x < 2
                and -2 < y < 2
                and 0 < z < 4
            )

            expected = (
                OUTSIDE
                if not inside_domain
                else SOLID
                if inside_solid
                else FREE
            )

            self.assertEqual(
                state,
                expected,
                f"Incorrect voxel classification at {index}"
            )

        self.assertEqual(
            len(voxel_components(occupancy, FREE)),
            1
        )

        self.assertEqual(
            len(voxel_components(occupancy, SOLID)),
            1
        )

        # Create a through opening measuring 2 m x 2 m x 4 m
        # inside the original physical cube.

        opening = self.create_solid(
            name="Voxelization through opening",
            operation="SUBTRACT",
            profile=rectangular_profile(2, 2, x=-1, y=-1),
            depth=6,
            matrix=translation_matrix(z=-1)
        )

        modified_physical = self.physical_mesh()

        self.assert_closed_mesh(modified_physical)
        self.assert_volume(modified_physical, 48)

        self.assert_mesh_equivalent(
            domain_mesh,
            self.domain_mesh(domain)
        )

        modified_surface = ClosedSurface(modified_physical)

        modified_occupancy = voxelize(
            origin,
            dimensions,
            spacing,
            [domain_surface],
            [modified_surface]
        )

        modified_counts = Counter(
            modified_occupancy.values()
        )

        self.assertEqual(
            modified_counts,
            {
                OUTSIDE: 296,
                FREE: 168,
                SOLID: 48
            }
        )

        changed_voxels = {
            index
            for index in occupancy
            if occupancy[index] != modified_occupancy[index]
        }

        self.assertEqual(len(changed_voxels), 16)

        for index in changed_voxels:
            self.assertEqual(occupancy[index], SOLID)
            self.assertEqual(modified_occupancy[index], FREE)

        self.assertEqual(
            len(voxel_components(modified_occupancy, FREE)),
            1
        )

        self.assertEqual(
            classify_point(
                (0, 0, 2),
                [domain_surface],
                [modified_surface]
            ),
            FREE
        )

        self.assertEqual(
            classify_point(
                (1.5, 1.5, 2),
                [domain_surface],
                [modified_surface]
            ),
            SOLID
        )

        self.assertEqual(
            classify_point(
                (4, 4, 2),
                [domain_surface],
                [modified_surface]
            ),
            OUTSIDE
        )

        # Verify a finer grid against the same analytical volumes.

        finer_spacing = 0.5
        finer_dimensions = (16, 16, 16)

        finer_occupancy = voxelize(
            origin,
            finer_dimensions,
            finer_spacing,
            [domain_surface],
            [modified_surface]
        )

        finer_counts = Counter(
            finer_occupancy.values()
        )

        voxel_volume = finer_spacing ** 3

        self.assertAlmostEqual(
            finer_counts[SOLID] * voxel_volume,
            48.0
        )

        self.assertAlmostEqual(
            finer_counts[FREE] * voxel_volume,
            168.0
        )

        self.assertAlmostEqual(
            finer_counts[OUTSIDE] * voxel_volume,
            296.0
        )

        self.assertAlmostEqual(
            (finer_counts[FREE] + finer_counts[SOLID]) * voxel_volume,
            mesh_volume(domain_mesh)
        )

        self.save_and_reload()

        current_domain = self.find_geometry(
            domain["globalId"]
        )

        current_opening = self.find_geometry(
            opening["globalId"]
        )

        persisted_occupancy = voxelize(
            origin,
            dimensions,
            spacing,
            [ClosedSurface(self.domain_mesh(current_domain))],
            [ClosedSurface(self.physical_mesh())]
        )

        self.assertEqual(
            persisted_occupancy,
            modified_occupancy
        )

        self.delete_geometry(current_opening["id"])

        restored_occupancy = voxelize(
            origin,
            dimensions,
            spacing,
            [ClosedSurface(self.domain_mesh(current_domain))],
            [ClosedSurface(self.physical_mesh())]
        )

        self.assertEqual(
            restored_occupancy,
            occupancy
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