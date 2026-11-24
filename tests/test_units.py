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
# Unit conventions:
# - Profile coordinates use IFC model units.
# - Extrusion depths use IFC model units.
# - Placement translations use IFC model units.
# - Tessellated vertices always use metres.
# - Tessellated surfaces and volumes are therefore expressed in m² and m³.
#
# Boolean tests intentionally enforce analytical physical correctness,
# including additive operations, regardless of current implementation limits.
#
# Run:
# python3 tests/test_units.py ./build/ifc-cli

DEFAULT_EXECUTABLE = "./build/ifc-cli"
EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

REQUEST_TIMEOUT = 60
GEOMETRY_TOLERANCE = 1e-6
VOLUME_TOLERANCE = 1e-5
SURFACE_TOLERANCE = 1e-5
MATRIX_TOLERANCE = 1e-8

ROOT_GLOBAL_ID = "0000000000000000000001"
CHILD_GLOBAL_ID = "0000000000000000000002"
PROJECT_GLOBAL_ID = "0000000000000000000003"

UNITS = {
    "metre": {
        "scale": 1.0,
        "prefix": "$",
        "name": "METRE"
    },
    "millimetre": {
        "scale": 0.001,
        "prefix": ".MILLI.",
        "name": "METRE"
    },
    "centimetre": {
        "scale": 0.01,
        "prefix": ".CENTI.",
        "name": "METRE"
    },
    "foot": {
        "scale": 0.3048,
        "prefix": None,
        "name": "foot"
    }
}

ROOT_BOUNDS = (
    -2.0, -1.5, 0.0,
    2.0, 1.5, 2.0
)

ROOT_WORLD_BOUNDS = (
    8.5, 18.0, 30.0,
    11.5, 22.0, 32.0
)

CHILD_WORLD_BOUNDS = (
    8.5, 24.0, 31.0,
    11.5, 28.0, 33.0
)

HIERARCHY_LOCAL_BOUNDS = (
    -2.0, -1.5, 0.0,
    8.0, 1.5, 3.0
)

HIERARCHY_WORLD_BOUNDS = (
    8.5, 18.0, 30.0,
    11.5, 28.0, 33.0
)

ROOT_VOLUME = 24.0
ROOT_SURFACE = 52.0

DOMAIN_PROFILE = [
    [0.0, 0.0],
    [3.0, 0.0],
    [3.0, 2.0],
    [0.0, 2.0]
]

IDENTITY_MATRIX = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0
]

ROOT_MATRIX = [
    0.0, -1.0, 0.0, 10.0,
    1.0, 0.0, 0.0, 20.0,
    0.0, 0.0, 1.0, 30.0,
    0.0, 0.0, 0.0, 1.0
]


def step_real(value):
    return f"{value:.16E}"


def step_coordinates(*coordinates):
    return "(" + ",".join(
        step_real(value) for value in coordinates
    ) + ")"


def translation_matrix(x=0.0, y=0.0, z=0.0):
    matrix = IDENTITY_MATRIX.copy()
    matrix[3] = x
    matrix[7] = y
    matrix[11] = z

    return matrix


def rotation_z(angle):
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)

    return [
        cosine, -sine, 0.0, 0.0,
        sine, cosine, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0
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


def transform_point(matrix, point):
    vector = (*point, 1.0)

    return tuple(
        math.fsum(
            matrix[row * 4 + column] * vector[column]
            for column in range(4)
        )
        for row in range(3)
    )


def to_model_matrix(matrix, scale):
    result = list(matrix)

    for index in (3, 7, 11):
        result[index] /= scale

    return result


def to_metre_matrix(matrix, scale):
    result = list(matrix)

    for index in (3, 7, 11):
        result[index] *= scale

    return result


def to_model_profile(profile, scale):
    return [
        [x / scale, y / scale]
        for x, y in profile
    ]


def to_metre_profile(profile, scale):
    return [
        [x * scale, y * scale]
        for x, y in profile
    ]


def transform_input(matrix=None, space="parent"):
    return {
        "space": space,
        "matrix": (
            IDENTITY_MATRIX.copy()
            if matrix is None
            else list(matrix)
        )
    }


def parents_input(spatial=None, placement=None, decomposition=None):
    return {
        "spatial": spatial,
        "placement": placement,
        "decomposition": decomposition
    }


def property_input(
    name,
    value,
    value_type="IfcLengthMeasure",
    unit=None,
    source="occurrence",
    property_set="Pset_UnitTest"
):
    return {
        "propertySet": property_set,
        "name": name,
        "valueType": value_type,
        "value": value,
        "unit": unit,
        "source": source
    }


def rectangular_profile(width, length, x=0.0, y=0.0):
    return [
        [x, y],
        [x + width, y],
        [x + width, y + length],
        [x, y + length]
    ]


def build_fixture(unit):
    specification = UNITS[unit]
    scale = specification["scale"]

    def length(value):
        return step_real(value / scale)

    def coordinates(*values):
        return step_coordinates(
            *(value / scale for value in values)
        )

    if unit == "foot":
        length_entities = """
#7=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#8=IFCDIMENSIONALEXPONENTS(1,0,0,0,0,0,0);
#9=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(0.3048),#7);
#10=IFCCONVERSIONBASEDUNIT(#8,.LENGTHUNIT.,'foot',#9);
"""
        length_unit_id = 10
    else:
        prefix = specification["prefix"]

        length_entities = (
            f"#7=IFCSIUNIT(*,.LENGTHUNIT.,{prefix},.METRE.);"
        )

        length_unit_id = 7

    return f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('units-{unit}.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCDIRECTION((0.,0.,1.));
#3=IFCDIRECTION((1.,0.,0.));
#4=IFCDIRECTION((0.,1.,0.));
#5=IFCAXIS2PLACEMENT3D(#1,#2,#3);
#6=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-8,#5,$);
{length_entities}
#11=IFCUNITASSIGNMENT((#{length_unit_id},#12));
#12=IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.);
#13=IFCPROJECT('{PROJECT_GLOBAL_ID}',$,'Units test project',$,$,$,$,(#6),#11);
#14=IFCCARTESIANPOINT({coordinates(10.0, 20.0, 30.0)});
#15=IFCAXIS2PLACEMENT3D(#14,#2,#4);
#16=IFCLOCALPLACEMENT($,#15);
#17=IFCCARTESIANPOINT((0.,0.));
#18=IFCAXIS2PLACEMENT2D(#17,$);
#19=IFCRECTANGLEPROFILEDEF(.AREA.,$,#18,{length(4.0)},{length(3.0)});
#20=IFCEXTRUDEDAREASOLID(#19,#5,#2,{length(2.0)});
#21=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#20));
#22=IFCPRODUCTDEFINITIONSHAPE($,$,(#21));
#23=IFCBUILDINGELEMENTPROXY('{ROOT_GLOBAL_ID}',$,'Root solid',$,$,#16,#22,$,.NOTDEFINED.);
#24=IFCCARTESIANPOINT({coordinates(6.0, 0.0, 1.0)});
#25=IFCAXIS2PLACEMENT3D(#24,#2,#3);
#26=IFCLOCALPLACEMENT(#16,#25);
#27=IFCEXTRUDEDAREASOLID(#19,#5,#2,{length(2.0)});
#28=IFCSHAPEREPRESENTATION(#6,'Body','SweptSolid',(#27));
#29=IFCPRODUCTDEFINITIONSHAPE($,$,(#28));
#30=IFCBUILDINGELEMENTPROXY('{CHILD_GLOBAL_ID}',$,'Child solid',$,$,#26,#29,$,.NOTDEFINED.);
ENDSEC;
END-ISO-10303-21;
"""


def parse_step_entities(content):
    # Targeted parser for the controlled unit fixtures.
    # Not intended to parse arbitrary IFC files.

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
        raise AssertionError("No IFC entities found")

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


def mesh_points(mesh):
    return [
        tuple(mesh["vertices"][offset:offset + 3])
        for offset in range(0, len(mesh["vertices"]), 3)
    ]


def mesh_triangles(mesh):
    points = mesh_points(mesh)

    for offset in range(0, len(mesh["indices"]), 3):
        yield tuple(
            points[index]
            for index in mesh["indices"][offset:offset + 3]
        )


def subtract_vectors(first, second):
    return tuple(
        first[index] - second[index]
        for index in range(3)
    )


def cross_product(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0]
    )


def dot_product(first, second):
    return math.fsum(
        first[index] * second[index]
        for index in range(3)
    )


def triangle_area(a, b, c):
    normal = cross_product(
        subtract_vectors(b, a),
        subtract_vectors(c, a)
    )

    return math.sqrt(
        dot_product(normal, normal)
    ) / 2.0


def mesh_volume(mesh):
    points = mesh_points(mesh)

    if not points:
        return 0.0

    origin = points[0]
    terms = []

    for triangle in mesh_triangles(mesh):
        a, b, c = [
            subtract_vectors(point, origin)
            for point in triangle
        ]

        terms.append(
            dot_product(a, cross_product(b, c))
        )

    return abs(math.fsum(terms)) / 6.0


def mesh_surface_area(mesh):
    return math.fsum(
        triangle_area(*triangle)
        for triangle in mesh_triangles(mesh)
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


def rounded_point(point, precision=6):
    return tuple(
        round(value, precision)
        for value in point
    )


def mesh_vertex_set(mesh, matrix=None):
    return {
        rounded_point(
            transform_point(matrix, point)
            if matrix is not None
            else point
        )
        for point in mesh_points(mesh)
    }


def mesh_signature(mesh, matrix=None):
    return Counter(
        tuple(sorted(
            rounded_point(
                transform_point(matrix, point)
                if matrix is not None
                else point
            )
            for point in triangle
        ))
        for triangle in mesh_triangles(mesh)
    )


def mesh_edge_counts(mesh):
    edges = Counter()

    for triangle in mesh_triangles(mesh):
        a, b, c = [
            rounded_point(point)
            for point in triangle
        ]

        if len({a, b, c}) != 3:
            raise AssertionError(
                f"Degenerate triangle: {triangle}"
            )

        for first, second in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted((first, second)))] += 1

    return edges


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

            if set(error) != {"code", "message", "details"}:
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

    def tessellate(
        self,
        transform_id,
        kind="SOLID",
        space="local",
        geometry_id=None,
        include_children=False
    ):
        options = {
            "space": space,
            "kind": kind,
            "includeChildren": include_children,
            "includeNormals": False,
            "includeMaterials": False
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
            "indices": []
        }

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.end": 3
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
            name = event.get("event")

            if name not in event_order:
                raise AssertionError(
                    f"Unexpected mesh event: {event}"
                )

            order = event_order[name]

            if order < previous_order:
                raise AssertionError(
                    f"Invalid mesh event order: {event}"
                )

            previous_order = order

            if name == "mesh.begin":
                if begin is not None:
                    raise AssertionError("Duplicate mesh.begin")

                begin = event
                continue

            if begin is None:
                raise AssertionError(
                    "Mesh data received before mesh.begin"
                )

            if name == "mesh.end":
                break

            key = name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(
                    f"Invalid mesh chunk offset: {event}"
                )

            if not isinstance(event["values"], list):
                raise AssertionError(event)

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

        if mesh["indexCount"] % 3 != 0:
            raise AssertionError("Invalid triangle indices")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(
                    f"Invalid vertex index: {index}"
                )

        for value in mesh["vertices"]:
            if type(value) not in (int, float) or not math.isfinite(value):
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


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-units-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.save_counter = 0
        self.unit = None
        self.scale = None

    def tearDown(self):
        self.client.close()

    def load_unit(self, unit):
        self.unit = unit
        self.scale = UNITS[unit]["scale"]

        path = self.root_path / f"{unit}.ifc"

        path.write_text(
            build_fixture(unit),
            encoding="utf-8"
        )

        return self.load_model(path)

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

        self.root_id = self.find_transform(ROOT_GLOBAL_ID)["id"]
        self.child_id = self.find_transform(CHILD_GLOBAL_ID)["id"]

        return result

    def save_model(self):
        self.save_counter += 1

        path = self.root_path / (
            f"{self.unit}-roundtrip-{self.save_counter}.ifc"
        )

        result = self.client.request(
            "save",
            {"path": str(path)}
        )

        self.assertEqual(
            result,
            {"path": str(path)}
        )

        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)

        return path

    def save_and_reload(self):
        path = self.save_model()
        self.load_model(path)

        return path

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
        name="Unit test transform",
        parent_id=None,
        matrix=None,
        space="parent",
        properties=None
    ):
        matrix = (
            IDENTITY_MATRIX
            if matrix is None
            else matrix
        )

        return self.client.request(
            "transformCreate",
            {
                "name": name,
                "parents": parents_input(
                    placement=parent_id
                ),
                "transform": transform_input(
                    to_model_matrix(matrix, self.scale),
                    space=space
                ),
                "properties": (
                    []
                    if properties is None
                    else properties
                )
            }
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
                    to_model_matrix(matrix, self.scale),
                    space=space
                )
            }
        )

    def create_geometry(
        self,
        parent_id=None,
        name="Unit test geometry",
        kind="SOLID",
        operation="ADD",
        profile=None,
        depth=2.0,
        matrix=None,
        space="parent"
    ):
        parent_id = (
            self.root_id
            if parent_id is None
            else parent_id
        )

        profile = (
            DOMAIN_PROFILE
            if profile is None
            else profile
        )

        matrix = (
            IDENTITY_MATRIX
            if matrix is None
            else matrix
        )

        return self.client.request(
            "geometryCreate",
            {
                "parentId": parent_id,
                "name": name,
                "kind": kind,
                "operation": operation,
                "profile": to_model_profile(
                    profile,
                    self.scale
                ),
                "depth": depth / self.scale,
                "transform": transform_input(
                    to_model_matrix(matrix, self.scale),
                    space=space
                )
            }
        )

    def create_domain(self, **options):
        return self.create_geometry(
            kind="DOMAIN",
            operation="ADD",
            **options
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
            self.client.request(
                "geometryDelete",
                {"id": identifier}
            ),
            {"id": identifier}
        )

    def tessellate(self, identifier=None, **options):
        return self.client.tessellate(
            self.root_id if identifier is None else identifier,
            **options
        )

    def tessellate_domain(self, geometry, **options):
        return self.tessellate(
            geometry["parentId"],
            kind="DOMAIN",
            geometry_id=geometry["id"],
            **options
        )

    def assert_matrix(self, actual, expected, tolerance=MATRIX_TOLERANCE):
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertAlmostEqual(
                value,
                reference,
                delta=tolerance,
                msg=f"Invalid matrix component {index}"
            )

    def assert_model_matrix(self, actual, expected_metres):
        self.assert_matrix(
            to_metre_matrix(actual, self.scale),
            expected_metres
        )

    def assert_profile(self, actual, expected):
        self.assertEqual(len(actual), len(expected))

        for point, reference in zip(actual, expected):
            self.assertEqual(len(point), 2)

            for value, coordinate in zip(point, reference):
                self.assertAlmostEqual(
                    value,
                    coordinate,
                    delta=GEOMETRY_TOLERANCE
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
                delta=GEOMETRY_TOLERANCE,
                msg=f"Invalid bounding box component {index}"
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
            delta=SURFACE_TOLERANCE
        )

    def assert_watertight(self, mesh):
        edges = mesh_edge_counts(mesh)

        self.assertTrue(edges)

        invalid_edges = {
            edge: count
            for edge, count in edges.items()
            if count != 2
        }

        self.assertFalse(
            invalid_edges,
            f"Open or non-manifold edges: "
            f"{list(invalid_edges.items())[:10]}"
        )

    def assert_valid_mesh(self, mesh):
        self.assertGreater(mesh["vertexCount"], 0)
        self.assertGreater(mesh["indexCount"], 0)

        self.assertEqual(
            len(mesh["vertices"]),
            mesh["vertexCount"] * 3
        )

        self.assertEqual(
            len(mesh["indices"]),
            mesh["indexCount"]
        )

        self.assertEqual(mesh["indexCount"] % 3, 0)

        for triangle in mesh_triangles(mesh):
            self.assertGreater(
                triangle_area(*triangle),
                1e-12
            )

        self.assert_watertight(mesh)

    def assert_box(self, mesh, expected_bounds):
        self.assert_valid_mesh(mesh)
        self.assert_bounds(mesh, expected_bounds)

        minimum = expected_bounds[:3]
        maximum = expected_bounds[3:]

        width, length, height = [
            maximum[index] - minimum[index]
            for index in range(3)
        ]

        self.assert_volume(
            mesh,
            width * length * height
        )

        self.assert_surface(
            mesh,
            2.0 * (
                width * length
                + width * height
                + length * height
            )
        )

        expected_vertices = {
            rounded_point((x, y, z))
            for x in (minimum[0], maximum[0])
            for y in (minimum[1], maximum[1])
            for z in (minimum[2], maximum[2])
        }

        self.assertEqual(
            mesh_vertex_set(mesh),
            expected_vertices
        )

    def assert_mesh_equivalent(
        self,
        first,
        second,
        exact_triangles=False
    ):
        self.assert_bounds(
            second,
            mesh_bounds(first)
        )

        self.assert_volume(
            second,
            mesh_volume(first)
        )

        self.assert_surface(
            second,
            mesh_surface_area(first)
        )

        self.assertEqual(
            mesh_vertex_set(first),
            mesh_vertex_set(second)
        )

        if exact_triangles:
            self.assertEqual(
                mesh_signature(first),
                mesh_signature(second)
            )

    def assert_geometry_parameters(
        self,
        geometry,
        expected_profile,
        expected_depth,
        expected_matrix=None
    ):
        self.assert_profile(
            geometry["profile"],
            to_model_profile(
                expected_profile,
                self.scale
            )
        )

        self.assert_profile(
            to_metre_profile(
                geometry["profile"],
                self.scale
            ),
            expected_profile
        )

        self.assertAlmostEqual(
            geometry["depth"],
            expected_depth / self.scale,
            delta=GEOMETRY_TOLERANCE
        )

        self.assertAlmostEqual(
            geometry["depth"] * self.scale,
            expected_depth,
            delta=GEOMETRY_TOLERANCE
        )

        if expected_matrix is not None:
            self.assert_model_matrix(
                geometry["transforms"]["local"],
                expected_matrix
            )

    def assert_extrusion(
        self,
        mesh,
        profile,
        depth,
        matrix=None
    ):
        self.assert_valid_mesh(mesh)

        expected_volume = polygon_area(profile) * depth

        expected_surface = (
            2.0 * polygon_area(profile)
            + polygon_perimeter(profile) * depth
        )

        expected_vertices = [
            (x, y, z)
            for x, y in profile
            for z in (0.0, depth)
        ]

        if matrix is not None:
            expected_vertices = [
                transform_point(matrix, point)
                for point in expected_vertices
            ]

        expected_bounds = tuple(
            operation(
                point[axis]
                for point in expected_vertices
            )
            for operation in (min, max)
            for axis in range(3)
        )

        self.assert_bounds(mesh, expected_bounds)
        self.assert_volume(mesh, expected_volume)
        self.assert_surface(mesh, expected_surface)

        self.assertEqual(
            mesh_vertex_set(mesh),
            {
                rounded_point(point)
                for point in expected_vertices
            }
        )

    def assert_native_model(self):
        root = self.get_transform(self.root_id)
        child = self.get_transform(self.child_id)

        self.assertEqual(
            child["parents"]["placement"],
            root["id"]
        )

        self.assert_model_matrix(
            root["transforms"]["local"],
            ROOT_MATRIX
        )

        self.assert_model_matrix(
            root["transforms"]["world"],
            ROOT_MATRIX
        )

        child_local = translation_matrix(
            6.0,
            0.0,
            1.0
        )

        self.assert_model_matrix(
            child["transforms"]["local"],
            child_local
        )

        self.assert_model_matrix(
            child["transforms"]["world"],
            multiply_matrices(
                ROOT_MATRIX,
                child_local
            )
        )

        root_local_mesh = self.tessellate(
            space="local"
        )

        root_world_mesh = self.tessellate(
            space="world"
        )

        child_local_mesh = self.tessellate(
            self.child_id,
            space="local"
        )

        child_world_mesh = self.tessellate(
            self.child_id,
            space="world"
        )

        self.assert_box(root_local_mesh, ROOT_BOUNDS)
        self.assert_box(root_world_mesh, ROOT_WORLD_BOUNDS)
        self.assert_box(child_local_mesh, ROOT_BOUNDS)
        self.assert_box(child_world_mesh, CHILD_WORLD_BOUNDS)

        self.assertEqual(
            mesh_vertex_set(
                root_local_mesh,
                matrix=ROOT_MATRIX
            ),
            mesh_vertex_set(root_world_mesh)
        )

        combined_local = self.tessellate(
            include_children=True,
            space="local"
        )

        combined_world = self.tessellate(
            include_children=True,
            space="world"
        )

        self.assert_valid_mesh(combined_local)
        self.assert_valid_mesh(combined_world)

        self.assert_bounds(
            combined_local,
            HIERARCHY_LOCAL_BOUNDS
        )

        self.assert_bounds(
            combined_world,
            HIERARCHY_WORLD_BOUNDS
        )

        for mesh in (combined_local, combined_world):
            self.assert_volume(mesh, 48.0)
            self.assert_surface(mesh, 104.0)

        self.assertEqual(
            mesh_vertex_set(
                combined_local,
                matrix=ROOT_MATRIX
            ),
            mesh_vertex_set(combined_world)
        )

    def assert_saved_length_unit(self, path):
        entities = parse_step_entities(
            path.read_text(encoding="utf-8")
        )

        assignments = [
            entity
            for entity in entities.values()
            if entity["class"] == "IFCUNITASSIGNMENT"
        ]

        self.assertEqual(len(assignments), 1)

        assigned_ids = step_references(
            assignments[0]["arguments"]
        )

        length_units = [
            identifier
            for identifier in assigned_ids
            if (
                ".LENGTHUNIT."
                in entities[identifier]["arguments"].upper()
            )
        ]

        self.assertEqual(len(length_units), 1)

        identifier = length_units[0]
        entity = entities[identifier]

        if self.unit == "foot":
            self.assertEqual(
                entity["class"],
                "IFCCONVERSIONBASEDUNIT"
            )

            self.assertIn(
                "'foot'",
                entity["arguments"]
            )

            measure_ids = [
                reference
                for reference in step_references(
                    entity["arguments"]
                )
                if (
                    entities[reference]["class"]
                    == "IFCMEASUREWITHUNIT"
                )
            ]

            self.assertEqual(len(measure_ids), 1)

            measure = entities[measure_ids[0]]

            factor = re.search(
                r"IFCLENGTHMEASURE\(([^)]+)\)",
                measure["arguments"],
                flags=re.IGNORECASE
            )

            self.assertIsNotNone(factor)

            self.assertAlmostEqual(
                float(factor.group(1)),
                0.3048,
                delta=1e-12
            )

            base_units = step_references(
                measure["arguments"]
            )

            self.assertEqual(len(base_units), 1)

            base_unit = entities[base_units[0]]

            self.assertEqual(
                base_unit["class"],
                "IFCSIUNIT"
            )

            self.assertEqual(
                re.sub(
                    r"\s+",
                    "",
                    base_unit["arguments"]
                ).upper(),
                "*,.LENGTHUNIT.,$,.METRE."
            )

        else:
            self.assertEqual(
                entity["class"],
                "IFCSIUNIT"
            )

            prefix = UNITS[self.unit]["prefix"]

            self.assertEqual(
                re.sub(
                    r"\s+",
                    "",
                    entity["arguments"]
                ).upper(),
                f"*,.LENGTHUNIT.,{prefix},.METRE."
            )

        return identifier, entities

    def test_metre_model(self):
        """Verify geometric consistency of an IFC model using metres."""

        result = self.load_unit("metre")

        self.assertEqual(result["transformCount"], 3)
        self.assertEqual(result["geometryCount"], 2)

        root = self.get_transform(self.root_id)

        self.assert_matrix(
            root["transforms"]["world"],
            ROOT_MATRIX
        )

        self.assert_native_model()

        path = self.save_and_reload()

        self.assert_saved_length_unit(path)
        self.assert_native_model()

    def test_millimetre_model(self):
        """Verify conversion of millimetre model dimensions into metres."""

        self.load_unit("millimetre")

        root = self.get_transform(self.root_id)
        matrix = root["transforms"]["world"]

        self.assertAlmostEqual(matrix[3], 10000.0)
        self.assertAlmostEqual(matrix[7], 20000.0)
        self.assertAlmostEqual(matrix[11], 30000.0)

        self.assert_native_model()

        mesh = self.tessellate()

        self.assert_volume(mesh, 24.0)
        self.assert_surface(mesh, 52.0)

        domain = self.create_domain(
            profile=rectangular_profile(2.0, 3.0),
            depth=4.0
        )

        self.assert_profile(
            domain["profile"],
            [
                [0.0, 0.0],
                [2000.0, 0.0],
                [2000.0, 3000.0],
                [0.0, 3000.0]
            ]
        )

        self.assertAlmostEqual(
            domain["depth"],
            4000.0
        )

        self.assert_box(
            self.tessellate_domain(domain),
            (
                0.0, 0.0, 0.0,
                2.0, 3.0, 4.0
            )
        )

        path = self.save_and_reload()

        self.assert_saved_length_unit(path)
        self.assert_native_model()

        current = self.find_geometry(domain["globalId"])

        self.assert_geometry_parameters(
            current,
            rectangular_profile(2.0, 3.0),
            4.0
        )

    def test_centimetre_model(self):
        """Verify conversion of centimetre model dimensions into metres."""

        self.load_unit("centimetre")

        root = self.get_transform(self.root_id)
        matrix = root["transforms"]["world"]

        self.assertAlmostEqual(matrix[3], 1000.0)
        self.assertAlmostEqual(matrix[7], 2000.0)
        self.assertAlmostEqual(matrix[11], 3000.0)

        self.assert_native_model()

        domain = self.create_domain(
            profile=rectangular_profile(2.5, 1.25),
            depth=0.75
        )

        self.assert_profile(
            domain["profile"],
            [
                [0.0, 0.0],
                [250.0, 0.0],
                [250.0, 125.0],
                [0.0, 125.0]
            ]
        )

        self.assertAlmostEqual(
            domain["depth"],
            75.0
        )

        self.assert_box(
            self.tessellate_domain(domain),
            (
                0.0, 0.0, 0.0,
                2.5, 1.25, 0.75
            )
        )

        self.assert_volume(
            self.tessellate_domain(domain),
            2.34375
        )

        path = self.save_and_reload()

        self.assert_saved_length_unit(path)
        self.assert_native_model()

    def test_imperial_model(self):
        """Verify conversion of international feet into metres."""

        self.load_unit("foot")

        self.assert_native_model()

        # Create a domain directly using raw foot-based API values.
        # Its dimensions are exactly 10 ft x 5 ft x 2 ft.

        profile = [
            [0.0, 0.0],
            [10.0, 0.0],
            [10.0, 5.0],
            [0.0, 5.0]
        ]

        domain = self.client.request(
            "geometryCreate",
            {
                "parentId": self.root_id,
                "name": "Foot-based domain",
                "kind": "DOMAIN",
                "operation": "ADD",
                "profile": profile,
                "depth": 2.0,
                "transform": transform_input(
                    translation_matrix(
                        3.0,
                        4.0,
                        5.0
                    )
                )
            }
        )

        self.assert_profile(
            domain["profile"],
            profile
        )

        self.assertAlmostEqual(domain["depth"], 2.0)

        self.assert_matrix(
            domain["transforms"]["local"],
            translation_matrix(3.0, 4.0, 5.0)
        )

        mesh = self.tessellate_domain(domain)

        self.assert_box(
            mesh,
            (
                0.9144, 1.2192, 1.524,
                3.9624, 2.7432, 2.1336
            )
        )

        self.assert_volume(
            mesh,
            100.0 * 0.3048 ** 3
        )

        self.assert_surface(
            mesh,
            160.0 * 0.3048 ** 2
        )

        path = self.save_and_reload()

        self.assert_saved_length_unit(path)

        current = self.find_geometry(
            domain["globalId"]
        )

        self.assert_mesh_equivalent(
            mesh,
            self.tessellate_domain(current)
        )

        self.assert_native_model()

    def test_profile_units(self):
        """Verify that profile coordinates remain in IFC model units."""

        profile = [
            [-1.25, -0.5],
            [2.75, -0.5],
            [2.75, 1.5],
            [0.75, 1.5],
            [0.75, 3.5],
            [-1.25, 3.5]
        ]

        replacement_profile = [
            [-2.0, -1.0],
            [3.0, -1.0],
            [3.0, 2.0],
            [1.0, 2.0],
            [1.0, 4.0],
            [-2.0, 4.0]
        ]

        for unit in UNITS:
            with self.subTest(unit=unit):
                self.load_unit(unit)

                domain = self.create_domain(
                    name="Profile unit verification",
                    profile=profile,
                    depth=2.0
                )

                solid = self.create_geometry(
                    name="Physical profile unit verification",
                    profile=profile,
                    depth=2.0
                )

                for geometry in (domain, solid):
                    current = self.get_geometry(
                        geometry["id"]
                    )

                    self.assert_geometry_parameters(
                        current,
                        profile,
                        2.0
                    )

                self.assert_extrusion(
                    self.tessellate_domain(domain),
                    profile,
                    2.0
                )

                self.assert_volume(
                    self.tessellate_domain(domain),
                    24.0
                )

                for geometry in (domain, solid):
                    updated = self.update_geometry(
                        geometry["id"],
                        profile=to_model_profile(
                            replacement_profile,
                            self.scale
                        )
                    )

                    self.assert_geometry_parameters(
                        updated,
                        replacement_profile,
                        2.0
                    )

                domain = self.get_geometry(domain["id"])

                self.assert_extrusion(
                    self.tessellate_domain(domain),
                    replacement_profile,
                    2.0
                )

                self.assert_volume(
                    self.tessellate_domain(domain),
                    42.0
                )

                self.save_and_reload()

                for geometry in (domain, solid):
                    current = self.find_geometry(
                        geometry["globalId"]
                    )

                    self.assert_geometry_parameters(
                        current,
                        replacement_profile,
                        2.0
                    )

    def test_depth_units(self):
        """Verify that extrusion depths remain in IFC model units."""

        depths = [
            0.125,
            0.75,
            2.5,
            10.0
        ]

        for unit in UNITS:
            with self.subTest(unit=unit):
                self.load_unit(unit)

                profile = rectangular_profile(
                    2.0,
                    3.0
                )

                domain = self.create_domain(
                    profile=profile,
                    depth=depths[0]
                )

                solid = self.create_geometry(
                    profile=profile,
                    depth=depths[0]
                )

                for depth in depths:
                    with self.subTest(depth_metres=depth):
                        for geometry in (domain, solid):
                            current = self.update_geometry(
                                geometry["id"],
                                depth=depth / self.scale
                            )

                            self.assert_geometry_parameters(
                                current,
                                profile,
                                depth
                            )

                        mesh = self.tessellate_domain(domain)

                        self.assert_box(
                            mesh,
                            (
                                0.0, 0.0, 0.0,
                                2.0, 3.0, depth
                            )
                        )

                        self.assert_volume(
                            mesh,
                            6.0 * depth
                        )

                        self.assert_surface(
                            mesh,
                            12.0 + 10.0 * depth
                        )

                self.save_and_reload()

                for geometry in (domain, solid):
                    current = self.find_geometry(
                        geometry["globalId"]
                    )

                    self.assert_geometry_parameters(
                        current,
                        profile,
                        depths[-1]
                    )

    def test_placement_units(self):
        """Verify model-unit translations and unit-independent rotations."""

        local_matrix = multiply_matrices(
            translation_matrix(2.0, 3.0, 4.0),
            rotation_z(35.0)
        )

        for unit in UNITS:
            with self.subTest(unit=unit):
                self.load_unit(unit)

                created = self.create_transform(
                    name="Placement unit verification",
                    parent_id=self.root_id,
                    matrix=local_matrix
                )

                self.assert_matrix(
                    created["transforms"]["local"],
                    to_model_matrix(
                        local_matrix,
                        self.scale
                    ),
                    tolerance=GEOMETRY_TOLERANCE
                )

                self.assert_model_matrix(
                    created["transforms"]["world"],
                    multiply_matrices(
                        ROOT_MATRIX,
                        local_matrix
                    )
                )

                self.assertAlmostEqual(
                    created["transforms"]["world"][3]
                    * self.scale,
                    7.0,
                    delta=GEOMETRY_TOLERANCE
                )

                self.assertAlmostEqual(
                    created["transforms"]["world"][7]
                    * self.scale,
                    22.0,
                    delta=GEOMETRY_TOLERANCE
                )

                self.assertAlmostEqual(
                    created["transforms"]["world"][11]
                    * self.scale,
                    34.0,
                    delta=GEOMETRY_TOLERANCE
                )

                domain_matrix = translation_matrix(
                    1.0,
                    2.0,
                    3.0
                )

                domain = self.create_domain(
                    parent_id=created["id"],
                    matrix=domain_matrix,
                    depth=4.0
                )

                self.assert_model_matrix(
                    domain["transforms"]["local"],
                    domain_matrix
                )

                self.assert_model_matrix(
                    domain["transforms"]["world"],
                    multiply_matrices(
                        multiply_matrices(
                            ROOT_MATRIX,
                            local_matrix
                        ),
                        domain_matrix
                    )
                )

                self.assert_box(
                    self.tessellate_domain(domain),
                    (
                        1.0, 2.0, 3.0,
                        4.0, 4.0, 7.0
                    )
                )

                expected_world = translation_matrix(
                    17.0,
                    24.0,
                    35.0
                )

                updated = self.update_transform(
                    created["id"],
                    expected_world,
                    space="world"
                )

                expected_local = multiply_matrices(
                    translation_matrix(
                        4.0,
                        -7.0,
                        5.0
                    ),
                    rotation_z(-90.0)
                )

                self.assert_model_matrix(
                    updated["transforms"]["local"],
                    expected_local
                )

                self.assert_model_matrix(
                    updated["transforms"]["world"],
                    expected_world
                )

                self.assert_box(
                    self.tessellate_domain(
                        domain,
                        space="world"
                    ),
                    (
                        18.0, 26.0, 38.0,
                        21.0, 28.0, 42.0
                    )
                )

                self.save_and_reload()

                current = self.find_transform(
                    created["globalId"]
                )

                self.assert_model_matrix(
                    current["transforms"]["world"],
                    expected_world
                )

                current_domain = self.find_geometry(
                    domain["globalId"]
                )

                self.assert_box(
                    self.tessellate_domain(
                        current_domain,
                        space="world"
                    ),
                    (
                        18.0, 26.0, 38.0,
                        21.0, 28.0, 42.0
                    )
                )

    def test_world_mesh_units(self):
        """Verify that world-space tessellated coordinates use metres."""

        for unit in UNITS:
            with self.subTest(unit=unit):
                self.load_unit(unit)

                root = self.tessellate(
                    space="world"
                )

                child = self.tessellate(
                    self.child_id,
                    space="world"
                )

                self.assert_box(
                    root,
                    ROOT_WORLD_BOUNDS
                )

                self.assert_box(
                    child,
                    CHILD_WORLD_BOUNDS
                )

                combined = self.tessellate(
                    include_children=True,
                    space="world"
                )

                self.assert_bounds(
                    combined,
                    HIERARCHY_WORLD_BOUNDS
                )

                self.assert_volume(combined, 48.0)
                self.assert_surface(combined, 104.0)

                domain = self.create_domain(
                    depth=4.0,
                    matrix=translation_matrix(
                        1.0,
                        2.0,
                        3.0
                    )
                )

                domain_mesh = self.tessellate_domain(
                    domain,
                    space="world"
                )

                self.assert_box(
                    domain_mesh,
                    (
                        6.0, 21.0, 33.0,
                        8.0, 24.0, 37.0
                    )
                )

                self.update_transform(
                    self.root_id,
                    multiply_matrices(
                        translation_matrix(
                            100.0,
                            200.0,
                            300.0
                        ),
                        rotation_z(90.0)
                    ),
                    space="world"
                )

                self.assert_box(
                    self.tessellate(space="world"),
                    (
                        98.5, 198.0, 300.0,
                        101.5, 202.0, 302.0
                    )
                )

                self.assert_box(
                    self.tessellate_domain(
                        domain,
                        space="world"
                    ),
                    (
                        96.0, 201.0, 303.0,
                        98.0, 204.0, 307.0
                    )
                )

    def test_local_mesh_units(self):
        """Verify metre-based local coordinates after inverse placement."""

        for unit in UNITS:
            with self.subTest(unit=unit):
                self.load_unit(unit)

                root = self.tessellate(
                    space="local"
                )

                child = self.tessellate(
                    self.child_id,
                    space="local"
                )

                self.assert_box(root, ROOT_BOUNDS)
                self.assert_box(child, ROOT_BOUNDS)

                combined = self.tessellate(
                    space="local",
                    include_children=True
                )

                self.assert_bounds(
                    combined,
                    HIERARCHY_LOCAL_BOUNDS
                )

                self.assert_volume(combined, 48.0)
                self.assert_surface(combined, 104.0)

                domain = self.create_domain(
                    parent_id=self.child_id,
                    matrix=translation_matrix(
                        1.0,
                        2.0,
                        3.0
                    ),
                    depth=4.0
                )

                child_domain = self.tessellate_domain(
                    domain,
                    space="local"
                )

                self.assert_box(
                    child_domain,
                    (
                        1.0, 2.0, 3.0,
                        4.0, 4.0, 7.0
                    )
                )

                root_domain = self.tessellate(
                    self.root_id,
                    kind="DOMAIN",
                    geometry_id=domain["id"],
                    include_children=True,
                    space="local"
                )

                self.assert_box(
                    root_domain,
                    (
                        7.0, 2.0, 4.0,
                        10.0, 4.0, 8.0
                    )
                )

                # Moving the root must not alter local dimensions.

                new_root_matrix = multiply_matrices(
                    translation_matrix(
                        100.0,
                        200.0,
                        300.0
                    ),
                    rotation_z(35.0)
                )

                self.update_transform(
                    self.root_id,
                    new_root_matrix,
                    space="world"
                )

                self.assert_mesh_equivalent(
                    root,
                    self.tessellate(space="local")
                )

                self.assert_mesh_equivalent(
                    combined,
                    self.tessellate(
                        space="local",
                        include_children=True
                    )
                )

                self.assert_mesh_equivalent(
                    root_domain,
                    self.tessellate(
                        self.root_id,
                        kind="DOMAIN",
                        geometry_id=domain["id"],
                        include_children=True,
                        space="local"
                    )
                )

                world = self.tessellate(space="world")

                self.assertEqual(
                    mesh_vertex_set(
                        root,
                        matrix=new_root_matrix
                    ),
                    mesh_vertex_set(world)
                )

    def test_domain_units(self):
        """Verify consistent simulation domain dimensions across IFC units."""

        profile = [
            [0.0, 0.0],
            [6.0, 0.0],
            [6.0, 2.0],
            [2.0, 2.0],
            [2.0, 5.0],
            [0.0, 5.0]
        ]

        matrix = multiply_matrices(
            translation_matrix(
                2.0,
                3.0,
                4.0
            ),
            rotation_z(35.0)
        )

        for unit in UNITS:
            with self.subTest(unit=unit):
                self.load_unit(unit)

                physical_before = self.tessellate()

                domain = self.create_domain(
                    name="Unit-independent simulation domain",
                    profile=profile,
                    depth=3.0,
                    matrix=matrix
                )

                self.assert_geometry_parameters(
                    domain,
                    profile,
                    3.0,
                    expected_matrix=matrix
                )

                local_mesh = self.tessellate_domain(
                    domain,
                    space="local"
                )

                world_mesh = self.tessellate_domain(
                    domain,
                    space="world"
                )

                self.assert_extrusion(
                    local_mesh,
                    profile,
                    3.0,
                    matrix=matrix
                )

                self.assert_extrusion(
                    world_mesh,
                    profile,
                    3.0,
                    matrix=multiply_matrices(
                        ROOT_MATRIX,
                        matrix
                    )
                )

                self.assert_volume(local_mesh, 54.0)
                self.assert_volume(world_mesh, 54.0)

                self.assert_surface(local_mesh, 102.0)
                self.assert_surface(world_mesh, 102.0)

                self.assert_mesh_equivalent(
                    physical_before,
                    self.tessellate()
                )

                self.update_geometry(
                    domain["id"],
                    depth=6.0 / self.scale
                )

                updated_mesh = self.tessellate_domain(
                    domain
                )

                self.assert_volume(
                    updated_mesh,
                    108.0
                )

                self.assert_surface(
                    updated_mesh,
                    168.0
                )

                path = self.save_and_reload()

                self.assert_saved_length_unit(path)

                current = self.find_geometry(
                    domain["globalId"]
                )

                self.assertEqual(current["kind"], "DOMAIN")
                self.assertEqual(current["operation"], "ADD")

                self.assert_geometry_parameters(
                    current,
                    profile,
                    6.0,
                    expected_matrix=matrix
                )

                self.assert_mesh_equivalent(
                    updated_mesh,
                    self.tessellate_domain(current)
                )

                self.assert_mesh_equivalent(
                    physical_before,
                    self.tessellate()
                )

    def test_boolean_units(self):
        """Verify analytical boolean accuracy in millimetre-based models."""

        self.load_unit("millimetre")

        baseline = self.tessellate()

        self.assert_box(
            baseline,
            ROOT_BOUNDS
        )

        self.assert_volume(baseline, 24.0)

        # A 1000 mm x 1000 mm opening crosses a 2000 mm solid.
        # The removed physical volume is exactly 2 m³.

        opening = self.create_geometry(
            name="Millimetre through opening",
            operation="SUBTRACT",
            profile=rectangular_profile(
                1.0,
                1.0,
                x=-0.5,
                y=-0.5
            ),
            depth=4.0,
            matrix=translation_matrix(
                0.0,
                0.0,
                -1.0
            )
        )

        self.assertAlmostEqual(
            opening["depth"],
            4000.0
        )

        through_mesh = self.tessellate()

        self.assert_valid_mesh(through_mesh)
        self.assert_volume(through_mesh, 22.0)
        self.assert_surface(through_mesh, 58.0)
        self.assert_bounds(through_mesh, ROOT_BOUNDS)

        # Convert the through opening into a blind cavity.
        # Removed volume: 1 m² x 0.75 m = 0.75 m³.

        opening = self.update_geometry(
            opening["id"],
            depth=750.0,
            transform=transform_input(
                translation_matrix(
                    0.0,
                    0.0,
                    1250.0
                )
            )
        )

        blind_mesh = self.tessellate()

        self.assert_valid_mesh(blind_mesh)
        self.assert_volume(blind_mesh, 23.25)
        self.assert_surface(blind_mesh, 55.0)

        self.assertAlmostEqual(
            opening["depth"] * self.scale,
            0.75
        )

        # Add a partially overlapping extrusion.
        # Extrusion volume: 2 m x 1 m x 2 m = 4 m³.
        # Existing overlap: 1 m x 1 m x 2 m = 2 m³.
        # Effective additional volume: 2 m³.

        addition = self.create_geometry(
            name="Millimetre additive extrusion",
            operation="ADD",
            profile=rectangular_profile(
                2.0,
                1.0,
                x=1.0,
                y=-0.5
            ),
            depth=2.0
        )

        mixed_mesh = self.tessellate()

        self.assert_valid_mesh(mixed_mesh)
        self.assert_volume(mixed_mesh, 25.25)

        self.assert_bounds(
            mixed_mesh,
            (
                -2.0, -1.5, 0.0,
                3.0, 1.5, 2.0
            )
        )

        domain = self.create_domain(
            name="Non-physical intersecting domain",
            profile=rectangular_profile(
                10.0,
                10.0,
                x=-5.0,
                y=-5.0
            ),
            depth=10.0
        )

        self.assert_volume(
            self.tessellate_domain(domain),
            1000.0
        )

        self.assert_mesh_equivalent(
            mixed_mesh,
            self.tessellate()
        )

        path = self.save_and_reload()

        self.assert_saved_length_unit(path)

        current_opening = self.find_geometry(
            opening["globalId"]
        )

        current_addition = self.find_geometry(
            addition["globalId"]
        )

        self.assert_volume(
            self.tessellate(),
            25.25
        )

        self.delete_geometry(
            current_opening["id"]
        )

        self.assert_volume(
            self.tessellate(),
            26.0
        )

        self.delete_geometry(
            current_addition["id"]
        )

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate()
        )

        # Verify fractional millimetre dimensions independently.

        fractional = self.create_geometry(
            name="Fractional millimetre opening",
            operation="SUBTRACT",
            profile=rectangular_profile(
                0.7505,
                0.50025,
                x=-0.37525,
                y=-0.250125
            ),
            depth=4.0,
            matrix=translation_matrix(
                0.0,
                0.0,
                -1.0
            )
        )

        expected_volume = (
            ROOT_VOLUME
            - 0.7505 * 0.50025 * 2.0
        )

        fractional_mesh = self.tessellate()

        self.assert_valid_mesh(fractional_mesh)

        self.assert_volume(
            fractional_mesh,
            expected_volume
        )

        self.delete_geometry(fractional["id"])

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate()
        )

    def test_property_units_persistence(self):
        """Verify persistence of explicit IFC property units and raw values."""

        for unit, specification in UNITS.items():
            with self.subTest(unit=unit):
                self.load_unit(unit)

                length_value = 2.75 / self.scale
                length_unit = specification["name"]

                properties = [
                    property_input(
                        "MeasuredLength",
                        length_value,
                        value_type="IfcLengthMeasure",
                        unit=length_unit
                    ),
                    property_input(
                        "MeasuredAngle",
                        math.pi / 4.0,
                        value_type="IfcPlaneAngleMeasure",
                        unit="RADIAN"
                    ),
                    property_input(
                        "UnitlessValue",
                        1.23456789,
                        value_type="IfcReal",
                        unit=None
                    ),
                    property_input(
                        "OptionalLength",
                        None,
                        value_type="IfcLengthMeasure",
                        unit=length_unit
                    )
                ]

                created = self.create_transform(
                    name="Property unit verification",
                    properties=properties
                )

                def assert_properties(current):
                    actual = {
                        item["name"]: item
                        for item in current["properties"]
                    }

                    expected = {
                        item["name"]: item
                        for item in properties
                    }

                    self.assertEqual(
                        set(actual),
                        set(expected)
                    )

                    for name, reference in expected.items():
                        value = actual[name]

                        for field in (
                            "propertySet",
                            "name",
                            "valueType",
                            "unit",
                            "source"
                        ):
                            self.assertEqual(
                                value[field],
                                reference[field]
                            )

                        if reference["value"] is None:
                            self.assertIsNone(value["value"])
                        else:
                            self.assertAlmostEqual(
                                value["value"],
                                reference["value"],
                                delta=GEOMETRY_TOLERANCE
                            )

                    self.assertAlmostEqual(
                        actual["MeasuredLength"]["value"]
                        * self.scale,
                        2.75,
                        delta=GEOMETRY_TOLERANCE
                    )

                assert_properties(created)

                for iteration in range(3):
                    with self.subTest(roundtrip=iteration):
                        path = self.save_and_reload()

                        current = self.find_transform(
                            created["globalId"]
                        )

                        assert_properties(current)

                        length_unit_id, entities = (
                            self.assert_saved_length_unit(path)
                        )

                        saved_properties = {}

                        for entity in entities.values():
                            if entity["class"] != "IFCPROPERTYSINGLEVALUE":
                                continue

                            match = re.match(
                                r"\s*'([^']*)'\s*,",
                                entity["arguments"]
                            )

                            if match is not None:
                                saved_properties[
                                    match.group(1)
                                ] = entity

                        # Verify that the actual property references
                        # the assigned length unit, not merely another
                        # SI unit with the same display name.

                        for name in (
                            "MeasuredLength",
                            "OptionalLength"
                        ):
                            references = step_references(
                                saved_properties[name]["arguments"]
                            )

                            self.assertEqual(
                                references,
                                [length_unit_id]
                            )

                        angle_references = step_references(
                            saved_properties[
                                "MeasuredAngle"
                            ]["arguments"]
                        )

                        self.assertEqual(
                            len(angle_references),
                            1
                        )

                        angle_unit = entities[
                            angle_references[0]
                        ]

                        self.assertEqual(
                            angle_unit["class"],
                            "IFCSIUNIT"
                        )

                        self.assertIn(
                            ".PLANEANGLEUNIT.",
                            angle_unit["arguments"].upper()
                        )

                        self.assertIn(
                            ".RADIAN.",
                            angle_unit["arguments"].upper()
                        )

                        self.assertEqual(
                            step_references(
                                saved_properties[
                                    "UnitlessValue"
                                ]["arguments"]
                            ),
                            []
                        )

    def test_equivalent_models(self):
        """Verify physical equivalence of models using different IFC units."""

        reference_meshes = None

        for unit in UNITS:
            with self.subTest(unit=unit):
                self.load_unit(unit)

                profile = rectangular_profile(
                    3.25,
                    2.75
                )

                matrix = multiply_matrices(
                    translation_matrix(
                        2.0,
                        3.0,
                        4.0
                    ),
                    rotation_z(35.0)
                )

                domain = self.create_domain(
                    name="Equivalent physical domain",
                    profile=profile,
                    depth=2.5,
                    matrix=matrix
                )

                child_domain = self.create_domain(
                    parent_id=self.child_id,
                    name="Equivalent descendant domain",
                    profile=rectangular_profile(
                        1.5,
                        2.0
                    ),
                    depth=3.0,
                    matrix=translation_matrix(
                        0.0,
                        0.0,
                        5.0
                    )
                )

                meshes = {
                    "rootLocal": self.tessellate(
                        space="local"
                    ),
                    "rootWorld": self.tessellate(
                        space="world"
                    ),
                    "childLocal": self.tessellate(
                        self.child_id,
                        space="local"
                    ),
                    "childWorld": self.tessellate(
                        self.child_id,
                        space="world"
                    ),
                    "hierarchyLocal": self.tessellate(
                        include_children=True,
                        space="local"
                    ),
                    "hierarchyWorld": self.tessellate(
                        include_children=True,
                        space="world"
                    ),
                    "domainLocal": self.tessellate_domain(
                        domain,
                        space="local"
                    ),
                    "domainWorld": self.tessellate_domain(
                        domain,
                        space="world"
                    ),
                    "childDomainLocal": self.tessellate_domain(
                        child_domain,
                        space="local"
                    ),
                    "childDomainWorld": self.tessellate_domain(
                        child_domain,
                        space="world"
                    ),
                    "domainsLocal": self.tessellate(
                        kind="DOMAIN",
                        include_children=True,
                        space="local"
                    ),
                    "domainsWorld": self.tessellate(
                        kind="DOMAIN",
                        include_children=True,
                        space="world"
                    )
                }

                for name, mesh in meshes.items():
                    with self.subTest(mesh=name):
                        self.assert_valid_mesh(mesh)

                self.assert_volume(
                    meshes["rootLocal"],
                    24.0
                )

                self.assert_volume(
                    meshes["hierarchyLocal"],
                    48.0
                )

                self.assert_volume(
                    meshes["domainLocal"],
                    22.34375
                )

                self.assert_volume(
                    meshes["childDomainLocal"],
                    9.0
                )

                self.assert_volume(
                    meshes["domainsLocal"],
                    31.34375
                )

                # Compare geometric vertices, bounds, surface areas
                # and volumes instead of requiring identical triangle
                # ordering or identical face triangulation.

                if reference_meshes is None:
                    reference_meshes = meshes

                else:
                    self.assertEqual(
                        set(meshes),
                        set(reference_meshes)
                    )

                    for name, reference in reference_meshes.items():
                        with self.subTest(comparison=name):
                            self.assert_mesh_equivalent(
                                reference,
                                meshes[name]
                            )

                path = self.save_and_reload()

                self.assert_saved_length_unit(path)

                current_domain = self.find_geometry(
                    domain["globalId"]
                )

                self.assert_geometry_parameters(
                    current_domain,
                    profile,
                    2.5,
                    expected_matrix=matrix
                )

                self.assert_mesh_equivalent(
                    reference_meshes["domainLocal"],
                    self.tessellate_domain(
                        current_domain,
                        space="local"
                    )
                )

                self.assert_mesh_equivalent(
                    reference_meshes["domainWorld"],
                    self.tessellate_domain(
                        current_domain,
                        space="world"
                    )
                )

                self.assert_mesh_equivalent(
                    reference_meshes["hierarchyWorld"],
                    self.tessellate(
                        include_children=True,
                        space="world"
                    )
                )

                self.assert_mesh_equivalent(
                    reference_meshes["domainsLocal"],
                    self.tessellate(
                        kind="DOMAIN",
                        include_children=True,
                        space="local"
                    )
                )

                self.assert_mesh_equivalent(
                    reference_meshes["domainsWorld"],
                    self.tessellate(
                        kind="DOMAIN",
                        include_children=True,
                        space="world"
                    )
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