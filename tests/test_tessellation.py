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


# Standalone integration suite using only the Python standard library.
# Not executed against the compiled CLI.
#
# Mesh topology is checked after geometrically identical vertices
# have been identified, because the CLI disables vertex welding.
#
# Large meshes are generated from independent native IFC solids
# to test JSONL streaming independently of polygon validation.
#
# Tessellation error handling is tested using a managed domain
# without a representation, which cannot produce triangles.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30
LARGE_MESH_TIMEOUT = 180

CHUNK_SIZE = 16384

MESH_TOLERANCE = 1e-6
VOLUME_TOLERANCE = 1e-5
SURFACE_TOLERANCE = 1e-5
NORMAL_TOLERANCE = 1e-6
WELD_PRECISION = 7

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

ROOT_GLOBAL_ID = "0000000000000000000001"
CHILD_GLOBAL_ID = "0000000000000000000002"
GRANDCHILD_GLOBAL_ID = "0000000000000000000003"
UNRELATED_GLOBAL_ID = "0000000000000000000004"
EMPTY_GLOBAL_ID = "0000000000000000000005"
PROJECT_GLOBAL_ID = "0000000000000000000006"
INVALID_DOMAIN_GLOBAL_ID = "0000000000000000000007"

CUBE_SIDE = 2.0
CUBE_VOLUME = CUBE_SIDE ** 3
CUBE_SURFACE_AREA = 6.0 * CUBE_SIDE ** 2

CUBE_BOUNDS = (
    -1.0, -1.0, 0.0,
    1.0, 1.0, 2.0
)

IDENTITY_MATRIX = [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1
]

ROOT_MATRIX = [
    0, -1, 0, 10,
    1, 0, 0, 20,
    0, 0, 1, 30,
    0, 0, 0, 1
]

DEFAULT_VISUAL = {
    "color": [0.3, 0.5, 0.7],
    "opacity": 1.0,
    "metallic": 0.2,
    "roughness": 0.6
}

EVENT_ORDER = {
    "mesh.begin": 0,
    "mesh.vertices": 1,
    "mesh.indices": 2,
    "mesh.normals": 3,
    "mesh.materials": 4,
    "mesh.end": 5
}

CHUNK_EVENTS = {
    "mesh.vertices": "vertices",
    "mesh.indices": "indices",
    "mesh.normals": "normals"
}

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('tessellation-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCDIRECTION((0.,0.,1.));
#3=IFCDIRECTION((1.,0.,0.));
#4=IFCDIRECTION((0.,1.,0.));
#5=IFCAXIS2PLACEMENT3D(#1,#2,#3);
#6=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-5,#5,$);
#7=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#8=IFCUNITASSIGNMENT((#7));
#9=IFCPROJECT('0000000000000000000006',$,'Tessellation test project',$,$,$,$,(#6),#8);
#10=IFCCARTESIANPOINT((10.,20.,30.));
#11=IFCAXIS2PLACEMENT3D(#10,#2,#4);
#12=IFCLOCALPLACEMENT($,#11);
#13=IFCCARTESIANPOINT((0.,0.));
#14=IFCAXIS2PLACEMENT2D(#13,$);
#15=IFCRECTANGLEPROFILEDEF(.AREA.,$,#14,2.,2.);
#16=IFCEXTRUDEDAREASOLID(#15,#5,#2,2.);
#17=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#16));
#18=IFCPRODUCTDEFINITIONSHAPE($,$,(#17));
#19=IFCBUILDINGELEMENTPROXY('0000000000000000000001',$,'Root cube',$,$,#12,#18,$,.NOTDEFINED.);
#20=IFCCARTESIANPOINT((4.,0.,0.));
#21=IFCAXIS2PLACEMENT3D(#20,#2,#3);
#22=IFCLOCALPLACEMENT(#12,#21);
#23=IFCEXTRUDEDAREASOLID(#15,#5,#2,2.);
#24=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#23));
#25=IFCPRODUCTDEFINITIONSHAPE($,$,(#24));
#26=IFCBUILDINGELEMENTPROXY('0000000000000000000002',$,'Child cube',$,$,#22,#25,$,.NOTDEFINED.);
#27=IFCCARTESIANPOINT((0.,5.,0.));
#28=IFCAXIS2PLACEMENT3D(#27,#2,#3);
#29=IFCLOCALPLACEMENT(#22,#28);
#30=IFCEXTRUDEDAREASOLID(#15,#5,#2,2.);
#31=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#30));
#32=IFCPRODUCTDEFINITIONSHAPE($,$,(#31));
#33=IFCBUILDINGELEMENTPROXY('0000000000000000000003',$,'Grandchild cube',$,$,#29,#32,$,.NOTDEFINED.);
#34=IFCCARTESIANPOINT((100.,100.,100.));
#35=IFCAXIS2PLACEMENT3D(#34,#2,#3);
#36=IFCLOCALPLACEMENT($,#35);
#37=IFCEXTRUDEDAREASOLID(#15,#5,#2,2.);
#38=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#37));
#39=IFCPRODUCTDEFINITIONSHAPE($,$,(#38));
#40=IFCBUILDINGELEMENTPROXY('0000000000000000000004',$,'Unrelated cube',$,$,#36,#39,$,.NOTDEFINED.);
#41=IFCLOCALPLACEMENT($,#5);
#42=IFCBUILDINGELEMENTPROXY('0000000000000000000005',$,'Empty transform',$,$,#41,$,$,.NOTDEFINED.);
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


def transform_point(matrix, point):
    vector = (*point, 1.0)

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


def transform_input(matrix=None, space="parent"):
    return {
        "space": space,
        "matrix": (
            IDENTITY_MATRIX.copy()
            if matrix is None
            else matrix
        )
    }


def rectangular_profile(width, length):
    return [
        [0, 0],
        [width, 0],
        [width, length],
        [0, length]
    ]


def regular_polygon(count, radius):
    return [
        [
            radius * math.cos(2.0 * math.pi * index / count),
            radius * math.sin(2.0 * math.pi * index / count)
        ]
        for index in range(count)
    ]


def polygon_area(profile):
    return abs(
        math.fsum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(
                profile,
                profile[1:] + profile[:1]
            )
        )
    ) / 2.0


def polygon_perimeter(profile):
    return math.fsum(
        math.dist(first, second)
        for first, second in zip(
            profile,
            profile[1:] + profile[:1]
        )
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
        dot_product(vector, vector)
    )


def triangle_normal(a, b, c):
    return cross_product(
        subtract_vectors(b, a),
        subtract_vectors(c, a)
    )


def triangle_area(a, b, c):
    return vector_length(
        triangle_normal(a, b, c)
    ) / 2.0


def triangle_centroid(a, b, c):
    return tuple(
        (a[axis] + b[axis] + c[axis]) / 3.0
        for axis in range(3)
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
        raise AssertionError("Mesh contains no vertices")

    return tuple(
        function(point[axis] for point in points)
        for function in (min, max)
        for axis in range(3)
    )


def points_bounds(points):
    return tuple(
        function(point[axis] for point in points)
        for function in (min, max)
        for axis in range(3)
    )


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
            dot_product(a, cross_product(b, c))
        )

    return math.fsum(terms) / 6.0


def mesh_volume(mesh):
    return abs(mesh_signed_volume(mesh))


def mesh_surface_area(mesh):
    return math.fsum(
        triangle_area(*triangle)
        for triangle in mesh_triangles(mesh)
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


def rounded_point(point, precision=WELD_PRECISION):
    return tuple(
        round(value, precision)
        for value in point
    )


def mesh_signature(
    mesh,
    matrix=None,
    precision=WELD_PRECISION
):
    triangles = Counter()

    for triangle in mesh_triangles(mesh):
        points = [
            rounded_point(
                transform_point(matrix, point)
                if matrix is not None
                else point,
                precision
            )
            for point in triangle
        ]

        triangles[
            tuple(sorted(points))
        ] += 1

    return triangles


def mesh_topology(mesh):
    welded_vertices = set()
    undirected_edges = Counter()
    directed_edges = Counter()
    faces = Counter()

    for triangle in mesh_triangles(mesh):
        a, b, c = [
            rounded_point(point)
            for point in triangle
        ]

        if len({a, b, c}) != 3:
            raise AssertionError(
                f"Degenerate welded triangle: {triangle}"
            )

        welded_vertices.update((a, b, c))
        faces[tuple(sorted((a, b, c)))] += 1

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

    return {
        "vertices": welded_vertices,
        "edges": undirected_edges,
        "directedEdges": directed_edges,
        "faces": faces
    }


def topology_component_count(topology):
    adjacency = {
        vertex: set()
        for vertex in topology["vertices"]
    }

    for first, second in topology["edges"]:
        adjacency[first].add(second)
        adjacency[second].add(first)

    remaining = set(adjacency)
    component_count = 0

    while remaining:
        component_count += 1

        pending = [remaining.pop()]

        while pending:
            vertex = pending.pop()

            for neighbor in adjacency[vertex]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    pending.append(neighbor)

    return component_count


def topology_euler_characteristic(topology):
    return (
        len(topology["vertices"])
        - len(topology["edges"])
        + len(topology["faces"])
    )


def extrusion_corners(profile, depth):
    return [
        (x, y, z)
        for x, y in profile
        for z in (0.0, depth)
    ]


def build_large_fixture(cube_count):
    if cube_count < 1:
        raise ValueError("cube_count must be positive")

    # Every cube is a distinct representation item.
    # Cubes are separated by one metre along the local X axis.

    entities = []
    solid_ids = [16]

    next_id = 100

    for index in range(1, cube_count):
        point_id = next_id
        placement_id = next_id + 1
        solid_id = next_id + 2

        next_id += 3

        x = index * 3.0

        entities.extend([
            (
                f"#{point_id}=IFCCARTESIANPOINT"
                f"(({x:.1f},0.,0.));"
            ),
            (
                f"#{placement_id}=IFCAXIS2PLACEMENT3D"
                f"(#{point_id},#2,#3);"
            ),
            (
                f"#{solid_id}=IFCEXTRUDEDAREASOLID"
                f"(#15,#{placement_id},#2,2.);"
            )
        ])

        solid_ids.append(solid_id)

    representation_items = ",".join(
        f"#{identifier}"
        for identifier in solid_ids
    )

    fixture = IFC_FIXTURE.replace(
        "#17=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#16));",
        (
            "#17=IFCSHAPEREPRESENTATION"
            f"(#6,'Body','SweptSolid',({representation_items}));"
        )
    )

    return fixture.replace(
        "ENDSEC;\nEND-ISO-10303-21;",
        "\n".join(entities)
        + "\nENDSEC;\nEND-ISO-10303-21;"
    )


def build_invalid_domain_fixture():
    # The domain is correctly indexed through its managed relation,
    # but has no Representation and cannot produce a nonempty mesh.
    # Object placement and parent relation remain valid.

    entities = [
        "#100=IFCLOCALPLACEMENT(#12,#5);",
        (
            "#101=IFCSPATIALZONE("
            "'0000000000000000000007',$,"
            "'Untessellatable managed domain',$,"
            "'ifc-cli:domain',#100,$,$,.USERDEFINED.);"
        ),
        (
            "#102=IFCRELASSIGNSTOPRODUCT("
            "'0000000000000000000008',$,"
            "'ifc-cli:domain-parent',$,(#101),$,#19);"
        )
    ]

    return IFC_FIXTURE.replace(
        "ENDSEC;\nEND-ISO-10303-21;",
        "\n".join(entities)
        + "\nENDSEC;\nEND-ISO-10303-21;"
    )


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
        space="local",
        kind="SOLID",
        geometry_id=None,
        include_children=False,
        include_normals=True,
        include_materials=True
    ):
        options = {
            "space": space,
            "kind": kind,
            "includeChildren": include_children,
            "includeNormals": include_normals,
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
            "normals": [],
            "groups": [],
            "events": [],
            "chunks": {
                "vertices": [],
                "indices": [],
                "normals": []
            }
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

            if not isinstance(event, dict):
                raise AssertionError(
                    f"Invalid mesh event: {event}"
                )

            event_name = event.get("event")

            if event_name not in EVENT_ORDER:
                raise AssertionError(
                    f"Unexpected mesh event: {event}"
                )

            current_order = EVENT_ORDER[event_name]

            if current_order < last_event:
                raise AssertionError(
                    f"Invalid mesh event order: {event_name}"
                )

            last_event = current_order

            mesh["events"].append(event_name)

            if event_name == "mesh.begin":
                if begin is not None:
                    raise AssertionError(
                        "Duplicate mesh.begin event"
                    )

                if set(event) != {
                    "event",
                    "vertexCount",
                    "indexCount"
                }:
                    raise AssertionError(
                        f"Invalid mesh.begin structure: {event}"
                    )

                begin = event
                continue

            if begin is None:
                raise AssertionError(
                    "Mesh data received before mesh.begin"
                )

            if event_name == "mesh.end":
                if set(event) != {"event"}:
                    raise AssertionError(
                        f"Invalid mesh.end structure: {event}"
                    )

                break

            if event_name == "mesh.materials":
                if not include_materials:
                    raise AssertionError(
                        "Unexpected mesh.materials event"
                    )

                if materials_received:
                    raise AssertionError(
                        "Duplicate mesh.materials event"
                    )

                if set(event) != {"event", "groups"}:
                    raise AssertionError(
                        f"Invalid material event: {event}"
                    )

                if not isinstance(event["groups"], list):
                    raise AssertionError(
                        f"Invalid material groups: {event}"
                    )

                materials_received = True
                mesh["groups"] = event["groups"]
                continue

            if (
                event_name == "mesh.normals"
                and not include_normals
            ):
                raise AssertionError(
                    "Unexpected mesh.normals event"
                )

            if set(event) != {"event", "offset", "values"}:
                raise AssertionError(
                    f"Invalid mesh chunk structure: {event}"
                )

            key = CHUNK_EVENTS[event_name]
            offset = event["offset"]
            values = event["values"]

            if type(offset) is not int or offset < 0:
                raise AssertionError(
                    f"Invalid chunk offset: {offset}"
                )

            if offset != len(mesh[key]):
                raise AssertionError(
                    f"Discontinuous {key} chunk: "
                    f"expected offset {len(mesh[key])}, "
                    f"received {offset}"
                )

            if not isinstance(values, list) or not values:
                raise AssertionError(
                    f"Invalid {key} chunk values"
                )

            mesh["chunks"][key].append({
                "offset": offset,
                "length": len(values)
            })

            mesh[key].extend(values)

        if materials_received != include_materials:
            raise AssertionError(
                "Incorrect presence of mesh.materials event"
            )

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        for field in ("vertexCount", "indexCount"):
            if type(mesh[field]) is not int or mesh[field] < 0:
                raise AssertionError(
                    f"Invalid mesh count: {begin}"
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


class TessellationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-tessellation-"
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

        self.assertEqual(result["path"], str(path))
        self.assertEqual(result["schema"].upper(), "IFC4")

        self.root_id = self.find_transform(ROOT_GLOBAL_ID)["id"]
        self.child_id = self.find_transform(CHILD_GLOBAL_ID)["id"]
        self.grandchild_id = self.find_transform(
            GRANDCHILD_GLOBAL_ID
        )["id"]
        self.unrelated_id = self.find_transform(
            UNRELATED_GLOBAL_ID
        )["id"]
        self.empty_id = self.find_transform(EMPTY_GLOBAL_ID)["id"]

        return result

    def load_fixture(self, content, filename):
        path = self.root_path / filename

        path.write_text(
            content,
            encoding="utf-8"
        )

        return self.load_model(path)

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

    def save_and_reload(self):
        self.save_model()

        return self.load_model(self.output_path)

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

        self.assertEqual(
            len(matches),
            1,
            f"Expected one transform with GlobalId {global_id}"
        )

        return self.get_transform(matches[0]["id"])

    def find_geometry_summary(self, global_id):
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

        return matches[0]

    def find_geometry(self, global_id):
        summary = self.find_geometry_summary(global_id)

        return self.get_geometry(summary["id"])

    def create_domain(
        self,
        parent_id=None,
        name="Tessellation domain",
        profile=None,
        depth=2.0,
        matrix=None
    ):
        parent_id = (
            self.root_id
            if parent_id is None
            else parent_id
        )

        profile = (
            rectangular_profile(2.0, 3.0)
            if profile is None
            else profile
        )

        return self.client.request(
            "geometryCreate",
            {
                "parentId": parent_id,
                "name": name,
                "kind": "DOMAIN",
                "operation": "ADD",
                "profile": profile,
                "depth": depth,
                "transform": transform_input(matrix)
            }
        )

    def create_subtraction(
        self,
        parent_id=None,
        profile=None,
        depth=4.0,
        matrix=None
    ):
        parent_id = (
            self.root_id
            if parent_id is None
            else parent_id
        )

        profile = (
            rectangular_profile(1.0, 1.0)
            if profile is None
            else profile
        )

        matrix = (
            translation_matrix(-0.5, -0.5, -1.0)
            if matrix is None
            else matrix
        )

        return self.client.request(
            "geometryCreate",
            {
                "parentId": parent_id,
                "name": "Tessellation subtraction",
                "kind": "SOLID",
                "operation": "SUBTRACT",
                "profile": profile,
                "depth": depth,
                "transform": transform_input(matrix)
            }
        )

    def update_geometry(self, identifier, **changes):
        updated = self.client.request(
            "geometryUpdate",
            {
                "id": identifier,
                **changes
            }
        )

        self.assertEqual(updated["id"], identifier)

        return updated

    def update_transform(self, identifier, **changes):
        updated = self.client.request(
            "transformUpdate",
            {
                "id": identifier,
                **changes
            }
        )

        self.assertEqual(updated["id"], identifier)

        return updated

    def delete_geometry(self, identifier):
        result = self.client.request(
            "geometryDelete",
            {"id": identifier}
        )

        self.assertEqual(result, {"id": identifier})

    def create_material(self, name="Tessellation material"):
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

    def tessellate(self, transform_id=None, **options):
        return self.client.tessellate(
            self.root_id if transform_id is None else transform_id,
            **options
        )

    def tessellate_domain(self, geometry, **options):
        return self.tessellate(
            geometry["parentId"],
            kind="DOMAIN",
            geometry_id=geometry["id"],
            **options
        )

    def assert_number(
        self,
        actual,
        expected,
        tolerance=MESH_TOLERANCE
    ):
        self.assertIn(type(actual), (int, float))
        self.assertTrue(math.isfinite(actual))

        self.assertAlmostEqual(
            actual,
            expected,
            delta=tolerance
        )

    def assert_bounds(self, mesh, expected):
        actual = mesh_bounds(mesh)

        self.assertEqual(len(actual), len(expected))

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertAlmostEqual(
                value,
                reference,
                delta=MESH_TOLERANCE,
                msg=f"Invalid bounding box component {index}"
            )

    def assert_vertex_count(self, mesh):
        self.assertIs(type(mesh["vertexCount"]), int)
        self.assertGreaterEqual(mesh["vertexCount"], 0)

        self.assertEqual(
            len(mesh["vertices"]) % 3,
            0
        )

        self.assertEqual(
            mesh["vertexCount"],
            len(mesh["vertices"]) // 3
        )

        for value in mesh["vertices"]:
            self.assertIn(type(value), (int, float))
            self.assertTrue(math.isfinite(value))

    def assert_index_count(self, mesh):
        self.assertIs(type(mesh["indexCount"]), int)
        self.assertGreaterEqual(mesh["indexCount"], 0)

        self.assertEqual(
            mesh["indexCount"],
            len(mesh["indices"])
        )

    def assert_index_bounds(self, mesh):
        for index in mesh["indices"]:
            self.assertIs(type(index), int)

            self.assertGreaterEqual(index, 0)

            self.assertLess(
                index,
                mesh["vertexCount"],
                f"Vertex index out of bounds: {index}"
            )

    def assert_triangle_structure(self, mesh):
        self.assertEqual(mesh["indexCount"] % 3, 0)

        indices = mesh["indices"]

        for offset in range(0, len(indices), 3):
            triangle = indices[offset:offset + 3]

            self.assertEqual(len(triangle), 3)

            self.assertEqual(
                len(set(triangle)),
                3,
                f"Repeated index in triangle {offset // 3}"
            )

    def assert_nondegenerate_triangles(self, mesh):
        for index, triangle in enumerate(mesh_triangles(mesh)):
            area = triangle_area(*triangle)

            self.assertTrue(math.isfinite(area))

            self.assertGreater(
                area,
                1e-12,
                f"Degenerate triangle {index}: {triangle}"
            )

    def assert_mesh_normals(self, mesh):
        normals = mesh["normals"]

        if mesh["vertexCount"] == 0:
            self.assertEqual(normals, [])
            return

        self.assertTrue(normals)

        self.assertEqual(
            len(normals),
            mesh["vertexCount"] * 3
        )

        for offset in range(0, len(normals), 3):
            normal = normals[offset:offset + 3]

            for component in normal:
                self.assertIn(type(component), (int, float))
                self.assertTrue(math.isfinite(component))

                self.assertGreaterEqual(
                    component,
                    -1.0 - NORMAL_TOLERANCE
                )

                self.assertLessEqual(
                    component,
                    1.0 + NORMAL_TOLERANCE
                )

            self.assertAlmostEqual(
                vector_length(normal),
                1.0,
                delta=NORMAL_TOLERANCE,
                msg=f"Invalid normal at vertex {offset // 3}"
            )

    def assert_normals_match_triangles(self, mesh):
        points = mesh_points(mesh)
        normals = mesh["normals"]
        indices = mesh["indices"]

        for offset in range(0, len(indices), 3):
            triangle_indices = indices[offset:offset + 3]

            a, b, c = [
                points[index]
                for index in triangle_indices
            ]

            normal = triangle_normal(a, b, c)
            length = vector_length(normal)

            self.assertGreater(length, 0.0)

            normal = tuple(
                value / length
                for value in normal
            )

            for index in triangle_indices:
                vertex_normal = normals[index * 3:index * 3 + 3]

                self.assertGreater(
                    dot_product(normal, vertex_normal),
                    0.0,
                    f"Normal opposes triangle winding at vertex {index}"
                )

    def assert_watertight(
        self,
        mesh,
        expected_components=None,
        expected_euler=None
    ):
        topology = mesh_topology(mesh)

        self.assertTrue(topology["vertices"])
        self.assertTrue(topology["edges"])
        self.assertTrue(topology["faces"])

        invalid_edges = {
            edge: count
            for edge, count in topology["edges"].items()
            if count != 2
        }

        self.assertFalse(
            invalid_edges,
            f"Open or non-manifold edges: "
            f"{list(invalid_edges.items())[:10]}"
        )

        duplicate_faces = {
            face: count
            for face, count in topology["faces"].items()
            if count != 1
        }

        self.assertFalse(
            duplicate_faces,
            f"Duplicate triangles: "
            f"{list(duplicate_faces.items())[:10]}"
        )

        if expected_components is not None:
            self.assertEqual(
                topology_component_count(topology),
                expected_components
            )

        if expected_euler is not None:
            self.assertEqual(
                topology_euler_characteristic(topology),
                expected_euler
            )

        return topology

    def assert_consistent_orientation(self, mesh):
        topology = mesh_topology(mesh)
        directed_edges = topology["directedEdges"]

        for first, second in topology["edges"]:
            self.assertEqual(
                directed_edges[(first, second)],
                1,
                f"Inconsistent edge orientation: {first}, {second}"
            )

            self.assertEqual(
                directed_edges[(second, first)],
                1,
                f"Inconsistent edge orientation: {second}, {first}"
            )

        self.assertGreater(
            mesh_signed_volume(mesh),
            0.0,
            "Mesh winding is not oriented outwards"
        )

    def assert_outward_orientation(self, mesh, interior_point):
        # This criterion is suitable for convex solids.
        # Concave solids require local surface-side checks.

        for triangle in mesh_triangles(mesh):
            normal = triangle_normal(*triangle)
            centroid = triangle_centroid(*triangle)

            direction = subtract_vectors(
                centroid,
                interior_point
            )

            self.assertGreater(
                dot_product(normal, direction),
                0.0,
                f"Inward-facing triangle: {triangle}"
            )

    def assert_material_groups(self, mesh):
        material_ids = {
            item["id"]
            for item in self.client.request("materialGetAll")
        }

        occupied = set()

        for group in mesh["groups"]:
            self.assertEqual(
                set(group),
                {
                    "materialId",
                    "indexOffset",
                    "indexCount"
                }
            )

            self.assertIn(group["materialId"], material_ids)

            offset = group["indexOffset"]
            count = group["indexCount"]

            self.assertIs(type(offset), int)
            self.assertIs(type(count), int)

            self.assertGreaterEqual(offset, 0)
            self.assertGreater(count, 0)

            self.assertEqual(offset % 3, 0)
            self.assertEqual(count % 3, 0)

            self.assertLessEqual(
                offset + count,
                mesh["indexCount"]
            )

            current = set(
                range(offset, offset + count)
            )

            self.assertFalse(
                occupied & current,
                "Overlapping material groups"
            )

            occupied.update(current)

    def assert_mesh_structure(
        self,
        mesh,
        include_normals=True,
        include_materials=True
    ):
        self.assert_vertex_count(mesh)
        self.assert_index_count(mesh)
        self.assert_index_bounds(mesh)
        self.assert_triangle_structure(mesh)
        self.assert_nondegenerate_triangles(mesh)

        if include_normals:
            self.assert_mesh_normals(mesh)
        else:
            self.assertEqual(mesh["normals"], [])
            self.assertNotIn("mesh.normals", mesh["events"])

        if include_materials:
            self.assert_material_groups(mesh)
            self.assertIn("mesh.materials", mesh["events"])
        else:
            self.assertEqual(mesh["groups"], [])
            self.assertNotIn("mesh.materials", mesh["events"])

    def assert_empty_mesh(
        self,
        mesh,
        include_materials=True
    ):
        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)

        self.assertEqual(mesh["vertices"], [])
        self.assertEqual(mesh["indices"], [])
        self.assertEqual(mesh["normals"], [])
        self.assertEqual(mesh["groups"], [])

        self.assertEqual(
            mesh["chunks"],
            {
                "vertices": [],
                "indices": [],
                "normals": []
            }
        )

        expected_events = ["mesh.begin"]

        if include_materials:
            expected_events.append("mesh.materials")

        expected_events.append("mesh.end")

        self.assertEqual(
            mesh["events"],
            expected_events
        )

    def assert_mesh_equivalent(self, first, second):
        self.assertEqual(
            first["indexCount"],
            second["indexCount"]
        )

        self.assertEqual(
            mesh_signature(first),
            mesh_signature(second)
        )

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

    def assert_chunk_offsets(self, mesh, key):
        chunks = mesh["chunks"][key]

        expected_offset = 0

        for index, chunk in enumerate(chunks):
            self.assertEqual(
                chunk["offset"],
                expected_offset
            )

            self.assertGreater(chunk["length"], 0)
            self.assertLessEqual(chunk["length"], CHUNK_SIZE)

            if index < len(chunks) - 1:
                self.assertEqual(
                    chunk["length"],
                    CHUNK_SIZE
                )

            expected_offset += chunk["length"]

        self.assertEqual(
            expected_offset,
            len(mesh[key])
        )

        expected_count = (
            len(mesh[key]) + CHUNK_SIZE - 1
        ) // CHUNK_SIZE

        self.assertEqual(
            len(chunks),
            expected_count
        )

    def assert_event_order(
        self,
        mesh,
        include_normals=True,
        include_materials=True
    ):
        events = mesh["events"]

        self.assertTrue(events)

        self.assertEqual(events[0], "mesh.begin")
        self.assertEqual(events[-1], "mesh.end")

        self.assertEqual(events.count("mesh.begin"), 1)
        self.assertEqual(events.count("mesh.end"), 1)

        self.assertEqual(
            [EVENT_ORDER[event] for event in events],
            sorted(EVENT_ORDER[event] for event in events)
        )

        self.assertEqual(
            events.count("mesh.vertices"),
            len(mesh["chunks"]["vertices"])
        )

        self.assertEqual(
            events.count("mesh.indices"),
            len(mesh["chunks"]["indices"])
        )

        self.assertEqual(
            events.count("mesh.normals"),
            len(mesh["chunks"]["normals"])
        )

        self.assertEqual(
            events.count("mesh.materials"),
            1 if include_materials else 0
        )

        if mesh["vertexCount"] > 0:
            self.assertIn("mesh.vertices", events)

            if include_normals:
                self.assertIn("mesh.normals", events)

        if mesh["indexCount"] > 0:
            self.assertIn("mesh.indices", events)

        if not include_normals:
            self.assertNotIn("mesh.normals", events)

    def assert_extrusion(
        self,
        mesh,
        profile,
        depth,
        matrix=None,
        include_normals=True
    ):
        self.assert_mesh_structure(
            mesh,
            include_normals=include_normals
        )

        expected_volume = polygon_area(profile) * depth

        expected_surface = (
            2.0 * polygon_area(profile)
            + polygon_perimeter(profile) * depth
        )

        corners = extrusion_corners(profile, depth)

        if matrix is not None:
            corners = [
                transform_point(matrix, point)
                for point in corners
            ]

        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected_volume,
            delta=VOLUME_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_surface_area(mesh),
            expected_surface,
            delta=SURFACE_TOLERANCE
        )

        self.assert_bounds(
            mesh,
            points_bounds(corners)
        )

        self.assert_watertight(
            mesh,
            expected_components=1,
            expected_euler=2
        )

        self.assert_consistent_orientation(mesh)

    def test_cube_mesh(self):
        """Verify valid triangulation of a native IFC cube."""

        mesh = self.tessellate()

        self.assert_mesh_structure(mesh)

        self.assertGreaterEqual(mesh["vertexCount"], 8)

        self.assertEqual(
            mesh["indexCount"],
            36,
            "A simple cube must contain twelve triangles"
        )

        topology = self.assert_watertight(
            mesh,
            expected_components=1,
            expected_euler=2
        )

        self.assertEqual(len(topology["vertices"]), 8)
        self.assertEqual(len(topology["edges"]), 18)
        self.assertEqual(len(topology["faces"]), 12)

        self.assert_bounds(mesh, CUBE_BOUNDS)

        self.assertAlmostEqual(
            mesh_volume(mesh),
            CUBE_VOLUME,
            delta=VOLUME_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_surface_area(mesh),
            CUBE_SURFACE_AREA,
            delta=SURFACE_TOLERANCE
        )

        expected_vertices = {
            (x, y, z)
            for x in (-1.0, 1.0)
            for y in (-1.0, 1.0)
            for z in (0.0, 2.0)
        }

        self.assertEqual(
            topology["vertices"],
            expected_vertices
        )

        self.assert_consistent_orientation(mesh)

        self.assertEqual(mesh["groups"], [])

    def test_vertex_count(self):
        """Verify consistency between vertexCount and received coordinates."""

        domain = self.create_domain(
            profile=rectangular_profile(4.0, 3.0),
            depth=5.0
        )

        cases = [
            self.tessellate(),
            self.tessellate(space="world"),
            self.tessellate(include_children=True),
            self.tessellate(self.child_id),
            self.tessellate(self.empty_id),
            self.tessellate_domain(domain)
        ]

        for index, mesh in enumerate(cases):
            with self.subTest(mesh=index):
                self.assert_vertex_count(mesh)

                self.assert_chunk_offsets(
                    mesh,
                    "vertices"
                )

        self.assertEqual(
            cases[4]["vertexCount"],
            0
        )

        self.assertGreater(
            cases[2]["vertexCount"],
            cases[0]["vertexCount"]
        )

        self.assertEqual(
            cases[0]["vertexCount"],
            cases[1]["vertexCount"]
        )

    def test_index_count(self):
        """Verify consistency between indexCount and received indices."""

        root = self.tessellate()
        child = self.tessellate(self.child_id)
        grandchild = self.tessellate(self.grandchild_id)

        combined = self.tessellate(
            include_children=True
        )

        empty = self.tessellate(self.empty_id)

        for mesh in (
            root,
            child,
            grandchild,
            combined,
            empty
        ):
            self.assert_index_count(mesh)

            self.assert_chunk_offsets(
                mesh,
                "indices"
            )

        self.assertEqual(root["indexCount"], 36)
        self.assertEqual(child["indexCount"], 36)
        self.assertEqual(grandchild["indexCount"], 36)

        self.assertEqual(
            combined["indexCount"],
            (
                root["indexCount"]
                + child["indexCount"]
                + grandchild["indexCount"]
            )
        )

        self.assertEqual(empty["indexCount"], 0)

    def test_index_bounds(self):
        """Verify that all triangle indices reference existing vertices."""

        domain = self.create_domain(
            profile=regular_polygon(32, 4.0),
            depth=3.0
        )

        cases = [
            self.tessellate(),
            self.tessellate(space="world"),
            self.tessellate(include_children=True),
            self.tessellate_domain(domain),
            self.tessellate_domain(domain, space="world")
        ]

        for index, mesh in enumerate(cases):
            with self.subTest(mesh=index):
                self.assert_vertex_count(mesh)
                self.assert_index_count(mesh)
                self.assert_index_bounds(mesh)

                self.assertTrue(mesh["indices"])

                self.assertGreaterEqual(
                    min(mesh["indices"]),
                    0
                )

                self.assertLess(
                    max(mesh["indices"]),
                    mesh["vertexCount"]
                )

                referenced_vertices = set(
                    mesh["indices"]
                )

                self.assertEqual(
                    referenced_vertices,
                    set(range(mesh["vertexCount"])),
                    "Mesh contains unreferenced vertices"
                )

    def test_triangle_structure(self):
        """Verify that mesh indices are organized into valid triplets."""

        profiles = [
            [
                [0, 0],
                [4, 0],
                [0, 3]
            ],
            rectangular_profile(4.0, 3.0),
            regular_polygon(8, 3.0),
            [
                [0, 0],
                [6, 0],
                [6, 2],
                [2, 2],
                [2, 5],
                [0, 5]
            ]
        ]

        for index, profile in enumerate(profiles):
            with self.subTest(profile=index):
                domain = self.create_domain(
                    name=f"Triangle structure {index}",
                    profile=profile,
                    depth=2.0
                )

                mesh = self.tessellate_domain(domain)

                self.assert_index_count(mesh)
                self.assert_index_bounds(mesh)
                self.assert_triangle_structure(mesh)

                expected_triangle_count = (
                    4 * len(profile) - 4
                )

                self.assertEqual(
                    mesh["indexCount"] // 3,
                    expected_triangle_count
                )

    def test_triangle_degeneracy(self):
        """Verify absence of zero-area triangles in valid meshes."""

        profiles = [
            rectangular_profile(2.0, 3.0),
            rectangular_profile(0.125, 0.25),
            regular_polygon(16, 5.0),
            [
                [0, 0],
                [8, 0],
                [8, 8],
                [6, 8],
                [6, 2],
                [2, 2],
                [2, 8],
                [0, 8]
            ]
        ]

        self.assert_nondegenerate_triangles(
            self.tessellate()
        )

        for index, profile in enumerate(profiles):
            with self.subTest(profile=index):
                domain = self.create_domain(
                    name=f"Nondegenerate domain {index}",
                    profile=profile,
                    depth=1.5
                )

                mesh = self.tessellate_domain(domain)

                self.assert_triangle_structure(mesh)
                self.assert_nondegenerate_triangles(mesh)

                topology = mesh_topology(mesh)

                self.assertTrue(
                    all(
                        count == 1
                        for count in topology["faces"].values()
                    )
                )

    def test_watertight_mesh(self):
        """Verify closed topology after welding geometrically identical vertices."""

        cube = self.tessellate()

        self.assert_watertight(
            cube,
            expected_components=1,
            expected_euler=2
        )

        domain = self.create_domain(
            profile=[
                [0, 0],
                [6, 0],
                [6, 2],
                [2, 2],
                [2, 5],
                [0, 5]
            ],
            depth=3.0
        )

        self.assert_watertight(
            self.tessellate_domain(domain),
            expected_components=1,
            expected_euler=2
        )

        hierarchy_mesh = self.tessellate(
            include_children=True
        )

        self.assert_watertight(
            hierarchy_mesh,
            expected_components=3,
            expected_euler=6
        )

        # A through opening creates a genus-one boundary surface.
        # Its Euler characteristic must therefore be zero.

        opening = self.create_subtraction()

        opened_cube = self.tessellate()

        self.assert_watertight(
            opened_cube,
            expected_components=1,
            expected_euler=0
        )

        self.assertAlmostEqual(
            mesh_volume(opened_cube),
            6.0,
            delta=VOLUME_TOLERANCE
        )

        self.delete_geometry(opening["id"])

        self.assert_watertight(
            self.tessellate(),
            expected_components=1,
            expected_euler=2
        )

    def test_mesh_orientation(self):
        """Verify consistent outward-facing triangle orientation."""

        local_cube = self.tessellate(space="local")
        world_cube = self.tessellate(space="world")

        self.assert_consistent_orientation(local_cube)
        self.assert_consistent_orientation(world_cube)

        self.assert_outward_orientation(
            local_cube,
            (0.0, 0.0, 1.0)
        )

        self.assert_outward_orientation(
            world_cube,
            (10.0, 20.0, 31.0)
        )

        profile = rectangular_profile(4.0, 3.0)

        for orientation in (
            profile,
            list(reversed(profile))
        ):
            with self.subTest(profile=orientation):
                domain = self.create_domain(
                    profile=orientation,
                    depth=2.0
                )

                mesh = self.tessellate_domain(domain)

                self.assert_consistent_orientation(mesh)

                self.assert_outward_orientation(
                    mesh,
                    (2.0, 1.5, 1.0)
                )

                self.assertAlmostEqual(
                    mesh_signed_volume(mesh),
                    24.0,
                    delta=VOLUME_TOLERANCE
                )

        opening = self.create_subtraction()

        opened_cube = self.tessellate()

        self.assert_consistent_orientation(
            opened_cube
        )

        self.assertAlmostEqual(
            mesh_signed_volume(opened_cube),
            6.0,
            delta=VOLUME_TOLERANCE
        )

        self.delete_geometry(opening["id"])

    def test_mesh_volume(self):
        """Verify mesh volumes against analytic extrusion volumes."""

        cube = self.tessellate()

        self.assertAlmostEqual(
            mesh_volume(cube),
            8.0,
            delta=VOLUME_TOLERANCE
        )

        cases = [
            (
                "Rectangular cuboid",
                rectangular_profile(4.0, 3.0),
                5.0,
                60.0
            ),
            (
                "Triangular prism",
                [
                    [0, 0],
                    [4, 0],
                    [0, 3]
                ],
                2.0,
                12.0
            ),
            (
                "Concave prism",
                [
                    [0, 0],
                    [6, 0],
                    [6, 2],
                    [2, 2],
                    [2, 5],
                    [0, 5]
                ],
                3.0,
                54.0
            ),
            (
                "Fractional cuboid",
                rectangular_profile(0.125, 0.25),
                0.75,
                0.0234375
            )
        ]

        for name, profile, depth, expected_volume in cases:
            with self.subTest(profile=name):
                matrix = compose_matrices(
                    translation_matrix(3.0, 4.0, 5.0),
                    rotation_z(35.0),
                    rotation_y(25.0),
                    rotation_x(15.0)
                )

                domain = self.create_domain(
                    name=name,
                    profile=profile,
                    depth=depth,
                    matrix=matrix
                )

                for space in ("local", "world"):
                    mesh = self.tessellate_domain(
                        domain,
                        space=space
                    )

                    self.assertAlmostEqual(
                        mesh_volume(mesh),
                        expected_volume,
                        delta=VOLUME_TOLERANCE
                    )

                    self.assertAlmostEqual(
                        mesh_volume(mesh),
                        polygon_area(profile) * depth,
                        delta=VOLUME_TOLERANCE
                    )

        combined = self.tessellate(
            include_children=True
        )

        self.assertAlmostEqual(
            mesh_volume(combined),
            3.0 * CUBE_VOLUME,
            delta=VOLUME_TOLERANCE
        )

    def test_mesh_surface_area(self):
        """Verify mesh surface areas against analytic extrusion surfaces."""

        cube = self.tessellate()

        self.assertAlmostEqual(
            mesh_surface_area(cube),
            24.0,
            delta=SURFACE_TOLERANCE
        )

        cases = [
            (
                rectangular_profile(4.0, 3.0),
                5.0,
                94.0
            ),
            (
                [
                    [0, 0],
                    [4, 0],
                    [0, 3]
                ],
                2.0,
                36.0
            ),
            (
                [
                    [0, 0],
                    [6, 0],
                    [6, 2],
                    [2, 2],
                    [2, 5],
                    [0, 5]
                ],
                3.0,
                102.0
            )
        ]

        for index, (profile, depth, surface) in enumerate(cases):
            with self.subTest(profile=index):
                domain = self.create_domain(
                    profile=profile,
                    depth=depth
                )

                mesh = self.tessellate_domain(domain)

                self.assertAlmostEqual(
                    mesh_surface_area(mesh),
                    surface,
                    delta=SURFACE_TOLERANCE
                )

                self.assertAlmostEqual(
                    mesh_surface_area(mesh),
                    (
                        2.0 * polygon_area(profile)
                        + polygon_perimeter(profile) * depth
                    ),
                    delta=SURFACE_TOLERANCE
                )

                for coordinate in (0.0, depth):
                    self.assertAlmostEqual(
                        mesh_planar_area(
                            mesh,
                            axis=2,
                            coordinate=coordinate
                        ),
                        polygon_area(profile),
                        delta=SURFACE_TOLERANCE
                    )

        opening = self.create_subtraction()

        opened_cube = self.tessellate()

        # Two unit-square faces are removed.
        # Four internal walls of area two are added.

        self.assertAlmostEqual(
            mesh_surface_area(opened_cube),
            30.0,
            delta=SURFACE_TOLERANCE
        )

        self.delete_geometry(opening["id"])

    def test_mesh_normals(self):
        """Verify finite, normalized and consistently oriented vertex normals."""

        cube = self.tessellate(
            include_normals=True
        )

        self.assert_mesh_normals(cube)
        self.assert_normals_match_triangles(cube)

        normal_directions = {
            rounded_point(
                cube["normals"][offset:offset + 3]
            )
            for offset in range(0, len(cube["normals"]), 3)
        }

        self.assertEqual(
            normal_directions,
            {
                (-1.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, -1.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, -1.0),
                (0.0, 0.0, 1.0)
            }
        )

        matrix = compose_matrices(
            translation_matrix(2.0, 3.0, 4.0),
            rotation_z(35.0),
            rotation_y(25.0),
            rotation_x(15.0)
        )

        domain = self.create_domain(
            profile=rectangular_profile(4.0, 3.0),
            depth=5.0,
            matrix=matrix
        )

        local_mesh = self.tessellate_domain(
            domain,
            space="local",
            include_normals=True
        )

        world_mesh = self.tessellate_domain(
            domain,
            space="world",
            include_normals=True
        )

        for mesh in (local_mesh, world_mesh):
            self.assert_mesh_normals(mesh)
            self.assert_normals_match_triangles(mesh)

        expected_world_normals = Counter(
            rounded_point(
                transform_direction(
                    ROOT_MATRIX,
                    local_mesh["normals"][offset:offset + 3]
                )
            )
            for offset in range(
                0,
                len(local_mesh["normals"]),
                3
            )
        )

        actual_world_normals = Counter(
            rounded_point(
                world_mesh["normals"][offset:offset + 3]
            )
            for offset in range(
                0,
                len(world_mesh["normals"]),
                3
            )
        )

        self.assertEqual(
            actual_world_normals,
            expected_world_normals
        )

    def test_without_normals(self):
        """Verify omission of normal events when normals are disabled."""

        with_normals = self.tessellate(
            include_normals=True
        )

        without_normals = self.tessellate(
            include_normals=False
        )

        self.assertIn(
            "mesh.normals",
            with_normals["events"]
        )

        self.assertNotIn(
            "mesh.normals",
            without_normals["events"]
        )

        self.assertEqual(
            without_normals["normals"],
            []
        )

        self.assertEqual(
            without_normals["chunks"]["normals"],
            []
        )

        self.assert_mesh_equivalent(
            with_normals,
            without_normals
        )

        self.assert_mesh_structure(
            without_normals,
            include_normals=False
        )

        domain = self.create_domain()

        domain_mesh = self.tessellate_domain(
            domain,
            include_normals=False
        )

        self.assertNotIn(
            "mesh.normals",
            domain_mesh["events"]
        )

        self.assertEqual(domain_mesh["normals"], [])

        self.assertGreater(
            domain_mesh["indexCount"],
            0
        )

    def test_without_materials(self):
        """Verify omission of material events when materials are disabled."""

        material = self.create_material()

        self.assign_material(
            material["id"],
            self.root_id
        )

        with_materials = self.tessellate(
            include_materials=True
        )

        self.assertEqual(
            with_materials["groups"],
            [{
                "materialId": material["id"],
                "indexOffset": 0,
                "indexCount": with_materials["indexCount"]
            }]
        )

        without_materials = self.tessellate(
            include_materials=False
        )

        self.assertNotIn(
            "mesh.materials",
            without_materials["events"]
        )

        self.assertEqual(
            without_materials["groups"],
            []
        )

        self.assert_mesh_equivalent(
            with_materials,
            without_materials
        )

        self.assert_mesh_structure(
            without_materials,
            include_materials=False
        )

        domain = self.create_domain()

        domain_mesh = self.tessellate_domain(
            domain,
            include_materials=False
        )

        self.assertNotIn(
            "mesh.materials",
            domain_mesh["events"]
        )

        self.assertEqual(domain_mesh["groups"], [])

    def test_world_mesh(self):
        """Verify vertex coordinates in the global coordinate system."""

        root_world = self.tessellate(
            space="world"
        )

        self.assert_bounds(
            root_world,
            (
                9.0, 19.0, 30.0,
                11.0, 21.0, 32.0
            )
        )

        child_world = self.tessellate(
            self.child_id,
            space="world"
        )

        self.assert_bounds(
            child_world,
            (
                9.0, 23.0, 30.0,
                11.0, 25.0, 32.0
            )
        )

        grandchild_world = self.tessellate(
            self.grandchild_id,
            space="world"
        )

        self.assert_bounds(
            grandchild_world,
            (
                4.0, 23.0, 30.0,
                6.0, 25.0, 32.0
            )
        )

        profile = rectangular_profile(4.0, 3.0)

        matrix = translation_matrix(
            2.0,
            3.0,
            4.0
        )

        domain = self.create_domain(
            profile=profile,
            depth=5.0,
            matrix=matrix
        )

        mesh = self.tessellate_domain(
            domain,
            space="world"
        )

        self.assert_extrusion(
            mesh,
            profile,
            5.0,
            matrix=multiply_matrices(
                ROOT_MATRIX,
                matrix
            )
        )

        self.assert_bounds(
            mesh,
            (
                4.0, 22.0, 34.0,
                7.0, 26.0, 39.0
            )
        )

    def test_local_mesh(self):
        """Verify coordinates relative to the requested root transform."""

        for identifier in (
            self.root_id,
            self.child_id,
            self.grandchild_id,
            self.unrelated_id
        ):
            with self.subTest(transform=identifier):
                mesh = self.tessellate(
                    identifier,
                    space="local"
                )

                self.assert_bounds(mesh, CUBE_BOUNDS)

                self.assertAlmostEqual(
                    mesh_volume(mesh),
                    CUBE_VOLUME,
                    delta=VOLUME_TOLERANCE
                )

        hierarchy_local = self.tessellate(
            space="local",
            include_children=True
        )

        hierarchy_world = self.tessellate(
            space="world",
            include_children=True
        )

        self.assert_bounds(
            hierarchy_local,
            (
                -1.0, -1.0, 0.0,
                5.0, 6.0, 2.0
            )
        )

        self.assertEqual(
            mesh_signature(
                hierarchy_local,
                matrix=ROOT_MATRIX
            ),
            mesh_signature(hierarchy_world)
        )

        child_hierarchy = self.tessellate(
            self.child_id,
            space="local",
            include_children=True
        )

        self.assert_bounds(
            child_hierarchy,
            (
                -1.0, -1.0, 0.0,
                1.0, 6.0, 2.0
            )
        )

        domain = self.create_domain(
            parent_id=self.child_id,
            profile=rectangular_profile(4.0, 3.0),
            depth=2.0,
            matrix=translation_matrix(2.0, 3.0, 4.0)
        )

        domain_local = self.tessellate_domain(
            domain,
            space="local"
        )

        self.assert_bounds(
            domain_local,
            (
                2.0, 3.0, 4.0,
                6.0, 6.0, 6.0
            )
        )

        domain_from_root = self.tessellate(
            self.root_id,
            space="local",
            kind="DOMAIN",
            geometry_id=domain["id"],
            include_children=True
        )

        self.assert_bounds(
            domain_from_root,
            (
                6.0, 3.0, 4.0,
                10.0, 6.0, 6.0
            )
        )

    def test_include_children(self):
        """Verify inclusion of all descendants without duplicate geometry."""

        individual_meshes = [
            self.tessellate(
                identifier,
                space="world"
            )
            for identifier in (
                self.root_id,
                self.child_id,
                self.grandchild_id
            )
        ]

        combined = self.tessellate(
            space="world",
            include_children=True
        )

        expected_signature = Counter()

        for mesh in individual_meshes:
            expected_signature.update(
                mesh_signature(mesh)
            )

        self.assertEqual(
            mesh_signature(combined),
            expected_signature
        )

        self.assertEqual(
            combined["indexCount"],
            sum(
                mesh["indexCount"]
                for mesh in individual_meshes
            )
        )

        self.assertAlmostEqual(
            mesh_volume(combined),
            24.0,
            delta=VOLUME_TOLERANCE
        )

        self.assert_watertight(
            combined,
            expected_components=3,
            expected_euler=6
        )

        self.assert_bounds(
            combined,
            (
                4.0, 19.0, 30.0,
                11.0, 25.0, 32.0
            )
        )

        # Add a second hierarchy path to the same descendant.
        # The descendant must not be tessellated twice.

        self.update_transform(
            self.child_id,
            parents={
                "spatial": None,
                "placement": self.root_id,
                "decomposition": self.root_id
            }
        )

        multiple_paths = self.tessellate(
            space="world",
            include_children=True
        )

        self.assert_mesh_equivalent(
            combined,
            multiple_paths
        )

        domains = [
            self.create_domain(
                parent_id=identifier,
                name=f"Descendant domain {index}",
                profile=rectangular_profile(1.0, 1.0),
                depth=1.0,
                matrix=translation_matrix(0.0, 0.0, 3.0)
            )
            for index, identifier in enumerate((
                self.root_id,
                self.child_id,
                self.grandchild_id
            ))
        ]

        domain_mesh = self.tessellate(
            kind="DOMAIN",
            include_children=True,
            space="world"
        )

        self.assertAlmostEqual(
            mesh_volume(domain_mesh),
            3.0,
            delta=VOLUME_TOLERANCE
        )

        expected_domain_signature = Counter()

        for domain in domains:
            expected_domain_signature.update(
                mesh_signature(
                    self.tessellate_domain(
                        domain,
                        space="world"
                    )
                )
            )

        self.assertEqual(
            mesh_signature(domain_mesh),
            expected_domain_signature
        )

        self.assert_watertight(
            domain_mesh,
            expected_components=3,
            expected_euler=6
        )

        self.assertEqual(domain_mesh["groups"], [])

    def test_exclude_children(self):
        """Verify exclusion of descendant and unrelated geometries."""

        root_only = self.tessellate(
            include_children=False,
            space="local"
        )

        with_children = self.tessellate(
            include_children=True,
            space="local"
        )

        self.assert_bounds(root_only, CUBE_BOUNDS)

        self.assertAlmostEqual(
            mesh_volume(root_only),
            8.0,
            delta=VOLUME_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_volume(with_children),
            24.0,
            delta=VOLUME_TOLERANCE
        )

        self.assertEqual(
            root_only["indexCount"],
            36
        )

        self.assertEqual(
            with_children["indexCount"],
            108
        )

        root_domain = self.create_domain(
            parent_id=self.root_id,
            name="Root domain",
            profile=rectangular_profile(2.0, 2.0),
            depth=2.0
        )

        child_domain = self.create_domain(
            parent_id=self.child_id,
            name="Excluded child domain",
            profile=rectangular_profile(3.0, 3.0),
            depth=3.0
        )

        root_domain_mesh = self.tessellate(
            kind="DOMAIN",
            include_children=False
        )

        self.assert_mesh_equivalent(
            root_domain_mesh,
            self.tessellate_domain(root_domain)
        )

        self.assertAlmostEqual(
            mesh_volume(root_domain_mesh),
            8.0,
            delta=VOLUME_TOLERANCE
        )

        combined_domains = self.tessellate(
            kind="DOMAIN",
            include_children=True
        )

        self.assertAlmostEqual(
            mesh_volume(combined_domains),
            35.0,
            delta=VOLUME_TOLERANCE
        )

        self.client.expect_error(
            "transformTessellate",
            {
                "id": self.root_id,
                "options": {
                    "space": "local",
                    "kind": "DOMAIN",
                    "geometryId": child_domain["id"],
                    "includeChildren": False,
                    "includeNormals": True,
                    "includeMaterials": True
                }
            },
            "INVALID_PARAMETERS"
        )

        self.assert_mesh_equivalent(
            root_only,
            self.tessellate(
                include_children=False
            )
        )

    def test_empty_mesh(self):
        """Verify successful empty mesh streams for geometry-free transforms."""

        for space in ("local", "world"):
            for kind in ("SOLID", "DOMAIN"):
                for include_normals in (False, True):
                    for include_materials in (False, True):
                        with self.subTest(
                            space=space,
                            kind=kind,
                            include_normals=include_normals,
                            include_materials=include_materials
                        ):
                            mesh = self.tessellate(
                                self.empty_id,
                                space=space,
                                kind=kind,
                                include_children=False,
                                include_normals=include_normals,
                                include_materials=include_materials
                            )

                            self.assert_empty_mesh(
                                mesh,
                                include_materials=include_materials
                            )

        # A physical product with no domains also has an
        # empty DOMAIN mesh.

        self.assert_empty_mesh(
            self.tessellate(
                kind="DOMAIN"
            )
        )

        domain = self.create_domain(
            parent_id=self.empty_id
        )

        self.assert_empty_mesh(
            self.tessellate(
                self.empty_id,
                kind="SOLID"
            )
        )

        self.assertGreater(
            self.tessellate_domain(domain)["indexCount"],
            0
        )

        self.delete_geometry(domain["id"])

        self.assert_empty_mesh(
            self.tessellate(
                self.empty_id,
                kind="DOMAIN"
            )
        )

    def test_chunk_offsets(self):
        """Verify contiguous offsets across multiple JSONL chunks."""

        self.client.timeout = LARGE_MESH_TIMEOUT

        cube_count = 768

        self.load_fixture(
            build_large_fixture(cube_count),
            "chunk-offsets.ifc"
        )

        mesh = self.tessellate(
            include_normals=True,
            include_materials=True
        )

        self.assert_mesh_structure(mesh)

        for key in (
            "vertices",
            "indices",
            "normals"
        ):
            with self.subTest(stream=key):
                self.assertGreater(
                    len(mesh[key]),
                    CHUNK_SIZE
                )

                self.assertGreaterEqual(
                    len(mesh["chunks"][key]),
                    2
                )

                self.assert_chunk_offsets(mesh, key)

                self.assertEqual(
                    mesh["chunks"][key][0],
                    {
                        "offset": 0,
                        "length": CHUNK_SIZE
                    }
                )

                self.assertEqual(
                    mesh["chunks"][key][1]["offset"],
                    CHUNK_SIZE
                )

                last_chunk = mesh["chunks"][key][-1]

                self.assertEqual(
                    last_chunk["offset"]
                    + last_chunk["length"],
                    len(mesh[key])
                )

        # Chunk offsets count scalar values, not vertices
        # or triangles. Chunk boundaries may split triplets.

        self.assertEqual(
            CHUNK_SIZE % 3,
            1
        )

        self.assertEqual(
            mesh["indexCount"],
            cube_count * 36
        )

        self.assertAlmostEqual(
            mesh_volume(mesh),
            cube_count * CUBE_VOLUME,
            delta=VOLUME_TOLERANCE
        )

        self.assert_event_order(mesh)

    def test_event_order(self):
        """Verify ordering and presence of all mesh stream events."""

        material = self.create_material()

        self.assign_material(
            material["id"],
            self.root_id
        )

        for include_normals in (False, True):
            for include_materials in (False, True):
                with self.subTest(
                    normals=include_normals,
                    materials=include_materials
                ):
                    mesh = self.tessellate(
                        include_normals=include_normals,
                        include_materials=include_materials
                    )

                    self.assert_event_order(
                        mesh,
                        include_normals=include_normals,
                        include_materials=include_materials
                    )

                    expected_events = [
                        "mesh.begin",
                        "mesh.vertices",
                        "mesh.indices"
                    ]

                    if include_normals:
                        expected_events.append(
                            "mesh.normals"
                        )

                    if include_materials:
                        expected_events.append(
                            "mesh.materials"
                        )

                    expected_events.append("mesh.end")

                    self.assertEqual(
                        mesh["events"],
                        expected_events
                    )

                    # A subsequent request must not receive
                    # residual events from the previous stream.

                    transform = self.get_transform(
                        self.root_id
                    )

                    self.assertEqual(
                        transform["globalId"],
                        ROOT_GLOBAL_ID
                    )

        empty = self.tessellate(self.empty_id)

        self.assert_event_order(empty)

        self.assertEqual(
            empty["events"],
            [
                "mesh.begin",
                "mesh.materials",
                "mesh.end"
            ]
        )

        domain = self.create_domain()

        domain_mesh = self.tessellate_domain(domain)

        self.assert_event_order(domain_mesh)
        self.assertEqual(domain_mesh["groups"], [])

    def test_large_mesh(self):
        """Verify complete reconstruction of meshes larger than one JSONL chunk."""

        self.client.timeout = LARGE_MESH_TIMEOUT

        cube_count = 1024

        result = self.load_fixture(
            build_large_fixture(cube_count),
            "large-mesh.ifc"
        )

        self.assertGreaterEqual(
            result["geometryCount"],
            cube_count
        )

        root_geometries = self.list_geometries(
            self.root_id,
            kind="SOLID"
        )

        self.assertEqual(
            len(root_geometries),
            cube_count
        )

        mesh = self.tessellate(
            include_normals=True,
            include_materials=False
        )

        self.assert_mesh_structure(
            mesh,
            include_materials=False
        )

        self.assertGreater(
            len(mesh["vertices"]),
            CHUNK_SIZE
        )

        self.assertGreater(
            len(mesh["indices"]),
            CHUNK_SIZE
        )

        self.assertEqual(
            mesh["indexCount"],
            cube_count * 36
        )

        self.assertGreaterEqual(
            mesh["vertexCount"],
            cube_count * 8
        )

        self.assertAlmostEqual(
            mesh_volume(mesh),
            cube_count * CUBE_VOLUME,
            delta=VOLUME_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_surface_area(mesh),
            cube_count * CUBE_SURFACE_AREA,
            delta=SURFACE_TOLERANCE
        )

        self.assert_bounds(
            mesh,
            (
                -1.0, -1.0, 0.0,
                (cube_count - 1) * 3.0 + 1.0,
                1.0,
                2.0
            )
        )

        topology = self.assert_watertight(
            mesh,
            expected_components=cube_count,
            expected_euler=cube_count * 2
        )

        self.assertEqual(
            len(topology["vertices"]),
            cube_count * 8
        )

        self.assertEqual(
            len(topology["edges"]),
            cube_count * 18
        )

        self.assertEqual(
            len(topology["faces"]),
            cube_count * 12
        )

        for key in ("vertices", "indices", "normals"):
            self.assert_chunk_offsets(mesh, key)

        self.assert_consistent_orientation(mesh)

        material = self.create_material(
            name="Large mesh material"
        )

        self.assign_material(
            material["id"],
            self.root_id
        )

        material_mesh = self.tessellate(
            include_materials=True
        )

        self.assert_mesh_equivalent(
            mesh,
            material_mesh
        )

        self.assert_material_groups(material_mesh)

        self.assertEqual(
            {
                group["materialId"]
                for group in material_mesh["groups"]
            },
            {material["id"]}
        )

        self.assertEqual(
            sum(
                group["indexCount"]
                for group in material_mesh["groups"]
            ),
            material_mesh["indexCount"]
        )

        self.save_and_reload()

        reloaded = self.tessellate(
            include_materials=False
        )

        self.assert_mesh_equivalent(
            mesh,
            reloaded
        )

        self.assert_watertight(
            reloaded,
            expected_components=cube_count,
            expected_euler=cube_count * 2
        )

        for key in ("vertices", "indices", "normals"):
            self.assert_chunk_offsets(
                reloaded,
                key
            )

    def test_repeatability(self):
        """Verify equivalent geometry across successive tessellations."""

        domain = self.create_domain(
            profile=regular_polygon(32, 5.0),
            depth=3.0,
            matrix=compose_matrices(
                translation_matrix(2.0, 3.0, 4.0),
                rotation_z(35.0),
                rotation_y(20.0)
            )
        )

        material = self.create_material()

        self.assign_material(
            material["id"],
            self.root_id
        )

        cases = [
            {
                "space": "local",
                "kind": "SOLID",
                "include_children": False
            },
            {
                "space": "world",
                "kind": "SOLID",
                "include_children": True
            },
            {
                "space": "local",
                "kind": "DOMAIN",
                "geometry_id": domain["id"],
                "include_children": False
            }
        ]

        for options in cases:
            with self.subTest(options=options):
                reference = self.tessellate(**options)

                for iteration in range(10):
                    with self.subTest(iteration=iteration):
                        candidate = self.tessellate(
                            **options
                        )

                        self.assert_mesh_structure(candidate)

                        self.assert_mesh_equivalent(
                            reference,
                            candidate
                        )

                        self.assertEqual(
                            reference["vertexCount"],
                            candidate["vertexCount"]
                        )

                        self.assertEqual(
                            reference["chunks"],
                            candidate["chunks"]
                        )

                        self.assertEqual(
                            reference["events"],
                            candidate["events"]
                        )

                        self.assertEqual(
                            reference["groups"],
                            candidate["groups"]
                        )

                        self.assertEqual(
                            Counter(
                                rounded_point(
                                    reference["normals"][offset:offset + 3]
                                )
                                for offset in range(
                                    0,
                                    len(reference["normals"]),
                                    3
                                )
                            ),
                            Counter(
                                rounded_point(
                                    candidate["normals"][offset:offset + 3]
                                )
                                for offset in range(
                                    0,
                                    len(candidate["normals"]),
                                    3
                                )
                            )
                        )

    def test_tessellation_after_update(self):
        """Verify immediate mesh recalculation following geometry updates."""

        initial_physical_mesh = self.tessellate()

        profile = rectangular_profile(4.0, 3.0)

        domain = self.create_domain(
            profile=profile,
            depth=2.0
        )

        initial_domain_mesh = self.tessellate_domain(
            domain
        )

        self.assert_extrusion(
            initial_domain_mesh,
            profile,
            2.0
        )

        self.assertAlmostEqual(
            mesh_volume(initial_domain_mesh),
            24.0,
            delta=VOLUME_TOLERANCE
        )

        updated = self.update_geometry(
            domain["id"],
            depth=5.0
        )

        self.assert_extrusion(
            self.tessellate_domain(updated),
            profile,
            5.0
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(updated)
            ),
            60.0,
            delta=VOLUME_TOLERANCE
        )

        replacement_profile = rectangular_profile(
            8.0,
            6.0
        )

        updated = self.update_geometry(
            domain["id"],
            profile=replacement_profile
        )

        profile_mesh = self.tessellate_domain(updated)

        self.assert_extrusion(
            profile_mesh,
            replacement_profile,
            5.0
        )

        self.assertAlmostEqual(
            mesh_volume(profile_mesh),
            240.0,
            delta=VOLUME_TOLERANCE
        )

        matrix = compose_matrices(
            translation_matrix(10.0, 20.0, 30.0),
            rotation_z(90.0)
        )

        updated = self.update_geometry(
            domain["id"],
            transform=transform_input(matrix)
        )

        moved_mesh = self.tessellate_domain(updated)

        self.assert_extrusion(
            moved_mesh,
            replacement_profile,
            5.0,
            matrix=matrix
        )

        self.assert_bounds(
            moved_mesh,
            (
                4.0, 20.0, 30.0,
                10.0, 28.0, 35.0
            )
        )

        self.assertEqual(
            mesh_signature(
                profile_mesh,
                matrix=matrix
            ),
            mesh_signature(moved_mesh)
        )

        # Domain updates must never alter physical geometry.

        self.assert_mesh_equivalent(
            initial_physical_mesh,
            self.tessellate()
        )

        # Verify physical mesh recalculation independently
        # through a native solid and its subtractive feature.

        opening = self.create_subtraction()

        opened_mesh = self.tessellate()

        self.assertAlmostEqual(
            mesh_volume(opened_mesh),
            6.0,
            delta=VOLUME_TOLERANCE
        )

        self.update_geometry(
            opening["id"],
            depth=1.0,
            transform=transform_input(
                translation_matrix(-0.5, -0.5, 1.0)
            )
        )

        blind_opening_mesh = self.tessellate()

        self.assertAlmostEqual(
            mesh_volume(blind_opening_mesh),
            7.0,
            delta=VOLUME_TOLERANCE
        )

        self.delete_geometry(opening["id"])

        self.assert_mesh_equivalent(
            initial_physical_mesh,
            self.tessellate()
        )

        parent_matrix = compose_matrices(
            translation_matrix(100.0, 200.0, 300.0),
            rotation_z(45.0),
            rotation_y(30.0)
        )

        self.update_transform(
            self.root_id,
            transform=transform_input(
                parent_matrix,
                space="world"
            )
        )

        # Moving the parent must preserve its local mesh
        # while updating the world-space mesh.

        self.assert_mesh_equivalent(
            initial_physical_mesh,
            self.tessellate(space="local")
        )

        current_world = self.tessellate(
            space="world"
        )

        self.assertEqual(
            mesh_signature(
                initial_physical_mesh,
                matrix=parent_matrix
            ),
            mesh_signature(current_world)
        )

        current_domain = self.get_geometry(
            domain["id"]
        )

        current_domain_local = self.tessellate_domain(
            current_domain,
            space="local"
        )

        self.assert_mesh_equivalent(
            moved_mesh,
            current_domain_local
        )

        current_domain_world = self.tessellate_domain(
            current_domain,
            space="world"
        )

        self.assertEqual(
            mesh_signature(
                moved_mesh,
                matrix=parent_matrix
            ),
            mesh_signature(current_domain_world)
        )

        self.save_and_reload()

        reloaded_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assert_mesh_equivalent(
            current_domain_local,
            self.tessellate_domain(
                reloaded_domain,
                space="local"
            )
        )

        self.assert_mesh_equivalent(
            current_domain_world,
            self.tessellate_domain(
                reloaded_domain,
                space="world"
            )
        )

    def test_tessellation_error(self):
        """Verify explicit tessellation errors and subsequent session recovery."""

        baseline = self.tessellate()

        domain = self.create_domain(
            parent_id=self.child_id
        )

        native_geometry = self.list_geometries(
            self.root_id,
            kind="SOLID"
        )[0]

        base_options = {
            "space": "local",
            "kind": "DOMAIN",
            "includeChildren": False,
            "includeNormals": True,
            "includeMaterials": True
        }

        invalid_selections = [
            (
                self.root_id,
                {
                    **base_options,
                    "geometryId": domain["id"]
                },
                "INVALID_PARAMETERS"
            ),
            (
                self.root_id,
                {
                    **base_options,
                    "geometryId": native_geometry["id"]
                },
                "INVALID_PARAMETERS"
            ),
            (
                self.root_id,
                {
                    **base_options,
                    "kind": "SOLID",
                    "geometryId": domain["id"]
                },
                "INVALID_PARAMETERS"
            ),
            (
                self.root_id,
                {
                    **base_options,
                    "geometryId": "geometry:999999999"
                },
                "ENTITY_NOT_FOUND"
            ),
            (
                "transform:999999999",
                base_options,
                "ENTITY_NOT_FOUND"
            )
        ]

        for transform_id, options, expected_code in invalid_selections:
            with self.subTest(
                transform_id=transform_id,
                options=options
            ):
                error = self.client.expect_error(
                    "transformTessellate",
                    {
                        "id": transform_id,
                        "options": options
                    },
                    expected_code
                )

                self.assertTrue(error["message"])

                self.assert_mesh_equivalent(
                    baseline,
                    self.tessellate()
                )

        # Distinguish an invalid request from a valid request
        # selecting a geometry that cannot produce triangles.

        self.load_fixture(
            build_invalid_domain_fixture(),
            "untessellatable-domain.ifc"
        )

        invalid_domain = self.find_geometry_summary(
            INVALID_DOMAIN_GLOBAL_ID
        )

        self.assertEqual(
            invalid_domain["kind"],
            "DOMAIN"
        )

        self.assertEqual(
            invalid_domain["source"],
            "editor"
        )

        self.assertEqual(
            invalid_domain["parentId"],
            self.root_id
        )

        for space in ("local", "world"):
            with self.subTest(space=space):
                error = self.client.expect_error(
                    "transformTessellate",
                    {
                        "id": self.root_id,
                        "options": {
                            **base_options,
                            "space": space,
                            "geometryId": invalid_domain["id"]
                        }
                    },
                    "TESSELLATION_FAILED"
                )

                self.assertTrue(error["message"])

                self.assertIsInstance(
                    error["details"],
                    dict
                )

        self.client.expect_error(
            "transformTessellate",
            {
                "id": self.root_id,
                "options": base_options
            },
            "TESSELLATION_FAILED"
        )

        # Failed tessellation must not emit a partial mesh stream
        # or prevent unrelated physical geometry from being read.

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate(
                kind="SOLID"
            )
        )

        self.assert_empty_mesh(
            self.tessellate(self.empty_id)
        )

        recovery_domain = self.create_domain(
            parent_id=self.child_id,
            name="Tessellation recovery domain",
            profile=rectangular_profile(4.0, 3.0),
            depth=2.0
        )

        recovery_mesh = self.tessellate_domain(
            recovery_domain
        )

        self.assert_extrusion(
            recovery_mesh,
            rectangular_profile(4.0, 3.0),
            2.0
        )

        self.assertAlmostEqual(
            mesh_volume(recovery_mesh),
            24.0,
            delta=VOLUME_TOLERANCE
        )

        # Loading a valid model must restore a fully usable session.

        self.load_model(self.input_path)

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate()
        )

        self.assert_empty_mesh(
            self.tessellate(kind="DOMAIN")
        )

        capabilities = self.client.request(
            "getCapabilities"
        )

        self.assertIn(
            "supportedGeometryKinds",
            capabilities
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