import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from collections import Counter
from itertools import product
from pathlib import Path


# Standalone integration suite using only the Python standard library.
#
# Run:
# python3 tests/test_integration.py ./build/ifc-cli
#
# Not executed against the compiled CLI.
#
# The initial IFC4 fixture contains a building, two storeys and native
# geometric prototypes. Physical construction elements are created by
# duplicating these prototypes because transformCreate does not expose
# arbitrary IFC product classes.
#
# Managed simulation domains and subtractive features are created
# directly through geometryCreate.
#
# Voxelization is performed independently from the CLI using triangle
# meshes and solid angle classification.


EXECUTABLE = Path("./build/ifc-cli").resolve()
REQUEST_TIMEOUT = 120
TOLERANCE = 1e-6

PROJECT_GUID = "0000000000000000000001"
BUILDING_GUID = "0000000000000000000002"
GROUND_STOREY_GUID = "0000000000000000000003"
UPPER_STOREY_GUID = "0000000000000000000004"
WALL_PROTOTYPE_GUID = "0000000000000000000005"
SLAB_PROTOTYPE_GUID = "0000000000000000000006"
OBJECT_PROTOTYPE_GUID = "0000000000000000000007"

OUTSIDE = "OUTSIDE"
FREE = "FREE"
SOLID = "SOLID"

ROOM_WIDTH = 6
ROOM_LENGTH = 6
ROOM_HEIGHT = 3
WALL_THICKNESS = 0.25
SLAB_THICKNESS = 0.25

WALL_VOLUME = 4.5
SLAB_VOLUME = 9.0
OBJECT_VOLUME = 1.0
ROOM_PHYSICAL_VOLUME = 27.0
ROOM_DOMAIN_VOLUME = 108.0

IDENTITY_MATRIX = [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1
]

CONCRETE_VISUAL = {
    "color": [0.65, 0.65, 0.65],
    "opacity": 1.0,
    "metallic": 0.0,
    "roughness": 0.85
}

WOOD_VISUAL = {
    "color": [0.55, 0.32, 0.15],
    "opacity": 1.0,
    "metallic": 0.0,
    "roughness": 0.6
}

STEEL_VISUAL = {
    "color": [0.45, 0.48, 0.52],
    "opacity": 1.0,
    "metallic": 0.9,
    "roughness": 0.25
}


def placement_matrix(x=0, y=0, z=0, angle=0):
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)

    return [
        cosine, -sine, 0, x,
        sine, cosine, 0, y,
        0, 0, 1, z,
        0, 0, 0, 1
    ]


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


def property_input(
    name,
    value,
    value_type="IfcLabel",
    property_set="Pset_Integration",
    source="occurrence",
    unit=None
):
    return {
        "propertySet": property_set,
        "name": name,
        "valueType": value_type,
        "value": value,
        "unit": unit,
        "source": source
    }


def rectangular_profile(width, length, x=0, y=0):
    return [
        [x, y],
        [x + width, y],
        [x + width, y + length],
        [x, y + length]
    ]


def step_real(value):
    return f"{float(value):.16E}"


def step_coordinates(values):
    return "(" + ",".join(step_real(value) for value in values) + ")"


def step_ref(identifier):
    return "$" if identifier is None else f"#{identifier}"


def step_refs(identifiers):
    return "(" + ",".join(step_ref(identifier) for identifier in identifiers) + ")"


def step_string(value):
    return "'" + value.replace("'", "''") + "'"


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
    vertices = mesh["vertices"]

    return [
        tuple(vertices[offset:offset + 3])
        for offset in range(0, len(vertices), 3)
    ]


def mesh_triangles(mesh):
    points = mesh_points(mesh)
    indices = mesh["indices"]

    return [
        tuple(points[index] for index in indices[offset:offset + 3])
        for offset in range(0, len(indices), 3)
    ]


def mesh_volume(mesh):
    points = mesh_points(mesh)

    if not points:
        return 0.0

    origin = points[0]

    return abs(math.fsum(
        dot(
            subtract(a, origin),
            cross(
                subtract(b, origin),
                subtract(c, origin)
            )
        )
        for a, b, c in mesh_triangles(mesh)
    )) / 6


def mesh_surface_area(mesh):
    return math.fsum(
        norm(cross(subtract(b, a), subtract(c, a))) / 2
        for a, b, c in mesh_triangles(mesh)
    )


def mesh_bounds(mesh):
    points = mesh_points(mesh)

    if not points:
        return None

    return tuple(
        operation(point[axis] for point in points)
        for operation in (min, max)
        for axis in range(3)
    )


def mesh_signature(mesh, translation=(0, 0, 0)):
    return Counter(
        tuple(sorted(
            tuple(
                round(point[axis] + translation[axis], 6)
                for axis in range(3)
            )
            for point in triangle
        ))
        for triangle in mesh_triangles(mesh)
    )


def mesh_edge_counts(mesh):
    edges = Counter()

    for triangle in mesh_triangles(mesh):
        a, b, c = [
            tuple(round(value, 6) for value in point)
            for point in triangle
        ]

        if len({a, b, c}) != 3:
            raise AssertionError(f"Degenerate triangle: {triangle}")

        for first, second in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted((first, second)))] += 1

    return edges


class ClosedSurface:
    def __init__(self, mesh):
        self.triangles = mesh_triangles(mesh)
        self.bounds = mesh_bounds(mesh)

        if not self.triangles:
            raise ValueError("A closed surface requires triangles")

    def contains(self, point):
        minimum = self.bounds[:3]
        maximum = self.bounds[3:]

        if any(
            value < lower - TOLERANCE
            or value > upper + TOLERANCE
            for value, lower, upper in zip(point, minimum, maximum)
        ):
            return False

        angles = []

        for first, second, third in self.triangles:
            a = subtract(first, point)
            b = subtract(second, point)
            c = subtract(third, point)

            length_a = norm(a)
            length_b = norm(b)
            length_c = norm(c)

            if min(length_a, length_b, length_c) <= 1e-12:
                raise ValueError(f"Point lies on a mesh vertex: {point}")

            numerator = dot(a, cross(b, c))

            denominator = (
                length_a * length_b * length_c
                + dot(a, b) * length_c
                + dot(b, c) * length_a
                + dot(c, a) * length_b
            )

            angles.append(
                2 * math.atan2(numerator, denominator)
            )

        winding_number = math.fsum(angles) / (4 * math.pi)

        return abs(winding_number) > 0.5


def classify_point(point, domain_surfaces, physical_surfaces):
    if not any(surface.contains(point) for surface in domain_surfaces):
        return OUTSIDE

    if any(surface.contains(point) for surface in physical_surfaces):
        return SOLID

    return FREE


def voxelize(origin, dimensions, spacing, domain_meshes, physical_meshes):
    domain_surfaces = [
        ClosedSurface(mesh)
        for mesh in domain_meshes
    ]

    physical_surfaces = [
        ClosedSurface(mesh)
        for mesh in physical_meshes
        if mesh["indices"]
    ]

    occupancy = {}

    for index in product(*(range(size) for size in dimensions)):
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

            for axis in range(3):
                for direction in (-1, 1):
                    neighbour = list(index)
                    neighbour[axis] += direction
                    neighbour = tuple(neighbour)

                    if neighbour not in remaining:
                        continue

                    remaining.remove(neighbour)
                    component.add(neighbour)
                    pending.append(neighbour)

        components.append(component)

    return components


class Fixture:
    def __init__(self):
        self.entities = []

        self.origin = self.point(0, 0, 0)
        self.z_axis = self.add("IFCDIRECTION", "(0.,0.,1.)")
        self.x_axis = self.add("IFCDIRECTION", "(1.,0.,0.)")

        self.axis = self.add(
            "IFCAXIS2PLACEMENT3D",
            f"#{self.origin},#{self.z_axis},#{self.x_axis}"
        )

        self.context = self.add(
            "IFCGEOMETRICREPRESENTATIONCONTEXT",
            f"$,'Model',3,1.E-6,#{self.axis},$"
        )

        length_unit = self.add(
            "IFCSIUNIT",
            "*,.LENGTHUNIT.,$,.METRE."
        )

        area_unit = self.add(
            "IFCSIUNIT",
            "*,.AREAUNIT.,$,.SQUARE_METRE."
        )

        volume_unit = self.add(
            "IFCSIUNIT",
            "*,.VOLUMEUNIT.,$,.CUBIC_METRE."
        )

        units = self.add(
            "IFCUNITASSIGNMENT",
            step_refs([length_unit, area_unit, volume_unit])
        )

        self.project = self.add(
            "IFCPROJECT",
            f"'{PROJECT_GUID}',$,'Integration project',$,$,$,$,"
            f"(#{self.context}),#{units}"
        )

        building_placement = self.placement()

        self.building = self.add(
            "IFCBUILDING",
            f"'{BUILDING_GUID}',$,'Integration building',$,$,"
            f"#{building_placement},$,$,.ELEMENT.,$,$,$"
        )

        self.ground_storey = self.storey(
            GROUND_STOREY_GUID,
            "Ground floor",
            building_placement,
            elevation=0
        )

        self.upper_storey = self.storey(
            UPPER_STOREY_GUID,
            "Upper floor",
            building_placement,
            elevation=4
        )

        self.add(
            "IFCRELAGGREGATES",
            f"'0000000000000000000010',$,$,$,"
            f"#{self.project},(#{self.building})"
        )

        self.add(
            "IFCRELAGGREGATES",
            f"'0000000000000000000011',$,$,$,#{self.building},"
            f"(#{self.ground_storey},#{self.upper_storey})"
        )

        self.prototype(
            class_name="IFCWALL",
            global_id=WALL_PROTOTYPE_GUID,
            name="Wall prototype",
            width=ROOM_WIDTH,
            length=WALL_THICKNESS,
            depth=ROOM_HEIGHT,
            x=100,
            predefined_type="STANDARD"
        )

        self.prototype(
            class_name="IFCSLAB",
            global_id=SLAB_PROTOTYPE_GUID,
            name="Slab prototype",
            width=ROOM_WIDTH,
            length=ROOM_LENGTH,
            depth=SLAB_THICKNESS,
            x=120,
            predefined_type="FLOOR"
        )

        self.prototype(
            class_name="IFCBUILDINGELEMENTPROXY",
            global_id=OBJECT_PROTOTYPE_GUID,
            name="Object prototype",
            width=1,
            length=1,
            depth=1,
            x=140,
            predefined_type="NOTDEFINED"
        )

    def add(self, class_name, arguments):
        identifier = len(self.entities) + 1

        self.entities.append(
            f"#{identifier}={class_name.upper()}({arguments});"
        )

        return identifier

    def point(self, *coordinates):
        return self.add(
            "IFCCARTESIANPOINT",
            step_coordinates(coordinates)
        )

    def placement(self, x=0, y=0, z=0, parent=None):
        point = self.point(x, y, z)

        axis = self.add(
            "IFCAXIS2PLACEMENT3D",
            f"#{point},#{self.z_axis},#{self.x_axis}"
        )

        return self.add(
            "IFCLOCALPLACEMENT",
            f"{step_ref(parent)},#{axis}"
        )

    def storey(self, global_id, name, parent, elevation):
        placement = self.placement(
            z=elevation,
            parent=parent
        )

        return self.add(
            "IFCBUILDINGSTOREY",
            f"'{global_id}',$,{step_string(name)},$,$,"
            f"#{placement},$,$,.ELEMENT.,{step_real(elevation)}"
        )

    def rectangle_representation(self, width, length, depth):
        point = self.point(width / 2, length / 2)

        axis = self.add(
            "IFCAXIS2PLACEMENT2D",
            f"#{point},$"
        )

        profile = self.add(
            "IFCRECTANGLEPROFILEDEF",
            f".AREA.,$,#{axis},{step_real(width)},{step_real(length)}"
        )

        solid = self.add(
            "IFCEXTRUDEDAREASOLID",
            f"#{profile},#{self.axis},#{self.z_axis},{step_real(depth)}"
        )

        shape = self.add(
            "IFCSHAPEREPRESENTATION",
            f"#{self.context},'Body','SweptSolid',(#{solid})"
        )

        return self.add(
            "IFCPRODUCTDEFINITIONSHAPE",
            f"$,$,(#{shape})"
        )

    def prototype(
        self,
        class_name,
        global_id,
        name,
        width,
        length,
        depth,
        x,
        predefined_type
    ):
        placement = self.placement(x=x)

        representation = self.rectangle_representation(
            width,
            length,
            depth
        )

        return self.add(
            class_name,
            f"'{global_id}',$,{step_string(name)},$,$,"
            f"#{placement},#{representation},$,.{predefined_type}."
        )

    def write(self, path):
        body = "\n".join(self.entities)

        content = f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('integration.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
{body}
ENDSEC;
END-ISO-10303-21;
"""

        path.write_text(content, encoding="utf-8")


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
                f"CLI terminated with code {self.process.returncode}"
            )

        self.counter += 1
        request_id = f"request-{self.counter}"

        payload = {
            "id": request_id,
            "command": command,
            "params": {} if params is None else params
        }

        self.process.stdin.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False
            ) + "\n"
        )

        self.process.stdin.flush()

        return request_id

    def read(self, request_id):
        try:
            line = self.responses.get(timeout=REQUEST_TIMEOUT)

        except queue.Empty as error:
            raise TimeoutError(
                f"No response for request {request_id}"
            ) from error

        if line is None:
            raise RuntimeError("CLI terminated unexpectedly")

        if isinstance(line, Exception):
            raise RuntimeError("Could not read CLI stdout") from line

        response = json.loads(line)

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
            raise AssertionError(response)

        if not response["ok"]:
            if set(response["error"]) != {"code", "message", "details"}:
                raise AssertionError(response)

        return response

    def request(self, command, params=None):
        response = self.read(self.send(command, params))

        if not response["ok"]:
            raise RuntimeError(
                f"{command}: {response['error']}"
            )

        return response["result"]

    def expect_error(self, command, params, expected_code):
        response = self.read(self.send(command, params))

        if response["ok"]:
            raise AssertionError(
                f"{command} unexpectedly succeeded"
            )

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
        include_children=False,
        geometry_id=None,
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
            "groups": []
        }

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.normals": 3,
            "mesh.materials": 4,
            "mesh.end": 5
        }

        previous_order = -1
        begin = None
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
                raise AssertionError(f"Unknown mesh event: {event}")

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
                raise AssertionError("Mesh data received before mesh.begin")

            if name == "mesh.end":
                break

            if name == "mesh.materials":
                if materials_received:
                    raise AssertionError("Duplicate mesh.materials")

                materials_received = True
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
            raise AssertionError("Invalid triangle structure")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(f"Invalid vertex index: {index}")

        for value in mesh["vertices"] + mesh["normals"]:
            if not math.isfinite(value):
                raise AssertionError(f"Invalid mesh coordinate: {value}")

        if include_normals and mesh["vertexCount"]:
            if len(mesh["normals"]) != len(mesh["vertices"]):
                raise AssertionError("Invalid normal count")

        if not include_normals and mesh["normals"]:
            raise AssertionError("Unexpected mesh normals")

        if materials_received != include_materials:
            raise AssertionError("Invalid material event presence")

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


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-integration-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)
        self.input_path = self.root_path / "building.ifc"
        self.save_counter = 0

        Fixture().write(self.input_path)

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.load_model(self.input_path)

        self.assert_counts(
            transforms=7,
            geometries=3,
            materials=0
        )

    def tearDown(self):
        self.client.close()

    def load_model(self, path):
        result = self.client.request(
            "load",
            {"path": str(path)}
        )

        self.assertEqual(result["path"], str(path))
        self.assertEqual(result["schema"].upper(), "IFC4")

        self.assert_counts(
            result["transformCount"],
            result["geometryCount"],
            result["materialCount"]
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

    def save_and_reload(self):
        path = self.save_model()
        self.load_model(path)

        return path

    def list_transforms(self):
        return self.client.request("transformGetAll")

    def list_geometries(self, transform_id=None, kind="ALL"):
        return self.client.request(
            "geometryGetAll",
            {
                "transformId": transform_id,
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
            item for item in self.list_transforms()
            if item["globalId"] == global_id
        ]

        self.assertEqual(len(matches), 1)

        return self.get_transform(matches[0]["id"])

    def find_geometry(self, global_id):
        matches = [
            item for item in self.list_geometries()
            if item["globalId"] == global_id
        ]

        self.assertEqual(len(matches), 1)

        return self.get_geometry(matches[0]["id"])

    def find_material(self, name):
        matches = [
            material
            for item in self.list_materials()
            for material in [self.get_material(item["id"])]
            if material["name"] == name
        ]

        self.assertEqual(len(matches), 1)

        return matches[0]

    @property
    def building_id(self):
        return self.find_transform(BUILDING_GUID)["id"]

    @property
    def ground_storey_id(self):
        return self.find_transform(GROUND_STOREY_GUID)["id"]

    @property
    def upper_storey_id(self):
        return self.find_transform(UPPER_STOREY_GUID)["id"]

    def create_transform(
        self,
        name,
        parents=None,
        matrix=None,
        properties=None,
        space="parent"
    ):
        return self.client.request(
            "transformCreate",
            {
                "name": name,
                "parents": parents_input() if parents is None else parents,
                "transform": transform_input(matrix, space),
                "properties": [] if properties is None else properties
            }
        )

    def update_transform(self, identifier, **changes):
        return self.client.request(
            "transformUpdate",
            {"id": identifier, **changes}
        )

    def duplicate_transform(self, identifier, include_children=False):
        result = self.client.request(
            "transformDuplicate",
            {
                "id": identifier,
                "includeChildren": include_children
            }
        )

        self.assertEqual(
            set(result),
            {"rootId", "transforms", "geometries"}
        )

        self.assertEqual(
            result["rootId"],
            result["transforms"][identifier]
        )

        return result

    def clone_product(
        self,
        prototype_guid,
        name,
        storey_id,
        placement_parent=None,
        matrix=None,
        material_id=None,
        properties=None
    ):
        prototype = self.find_transform(prototype_guid)

        duplicated = self.duplicate_transform(
            prototype["id"]
        )

        self.assertEqual(
            set(duplicated["transforms"]),
            {prototype["id"]}
        )

        self.assertEqual(
            set(duplicated["geometries"]),
            set(prototype["geometries"])
        )

        identifier = duplicated["rootId"]

        updated = self.update_transform(
            identifier,
            name=name,
            parents=parents_input(
                spatial=storey_id,
                placement=(
                    storey_id
                    if placement_parent is None
                    else placement_parent
                )
            ),
            transform=transform_input(matrix),
            properties=[] if properties is None else properties
        )

        self.assertNotEqual(
            updated["globalId"],
            prototype["globalId"]
        )

        self.assertEqual(
            updated["definition"]["ifcClass"],
            prototype["definition"]["ifcClass"]
        )

        self.assertEqual(
            updated["parents"]["spatial"],
            storey_id
        )

        if material_id is not None:
            self.assign_material(material_id, identifier)

        return self.get_transform(identifier)

    def create_geometry(
        self,
        parent_id,
        name,
        profile,
        depth,
        matrix=None,
        kind="DOMAIN",
        operation="ADD"
    ):
        return self.client.request(
            "geometryCreate",
            {
                "parentId": parent_id,
                "name": name,
                "kind": kind,
                "operation": operation,
                "profile": profile,
                "depth": depth,
                "transform": transform_input(matrix)
            }
        )

    def create_domain(
        self,
        parent_id,
        name="Simulation domain",
        width=ROOM_WIDTH,
        length=ROOM_LENGTH,
        depth=ROOM_HEIGHT,
        matrix=None
    ):
        result = self.create_geometry(
            parent_id,
            name,
            rectangular_profile(width, length),
            depth,
            matrix=matrix,
            kind="DOMAIN",
            operation="ADD"
        )

        self.assertEqual(result["kind"], "DOMAIN")
        self.assertEqual(result["operation"], "ADD")
        self.assertEqual(result["ifcClass"], "IfcSpatialZone")
        self.assertEqual(result["materials"], [])
        self.assertIs(result["editable"], True)

        return result

    def create_opening(
        self,
        wall_id,
        name,
        x,
        width,
        z,
        height
    ):
        return self.create_geometry(
            wall_id,
            name,
            rectangular_profile(
                width,
                2,
                x=x,
                y=-1
            ),
            height,
            matrix=placement_matrix(z=z),
            kind="SOLID",
            operation="SUBTRACT"
        )

    def update_geometry(self, identifier, **changes):
        return self.client.request(
            "geometryUpdate",
            {"id": identifier, **changes}
        )

    def create_material(
        self,
        name,
        category,
        visual,
        density
    ):
        return self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": category,
                "visual": visual,
                "properties": [
                    property_input(
                        "Density",
                        density,
                        value_type="IfcReal",
                        property_set="Pset_Physical",
                        source="material"
                    )
                ]
            }
        )

    def create_concrete(self, name="Concrete"):
        return self.create_material(
            name,
            "concrete",
            CONCRETE_VISUAL,
            2400.0
        )

    def create_wood(self, name="Wood"):
        return self.create_material(
            name,
            "wood",
            WOOD_VISUAL,
            650.0
        )

    def create_steel(self, name="Steel"):
        return self.create_material(
            name,
            "metal",
            STEEL_VISUAL,
            7850.0
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

    def unassign_material(self, material_id, target_id):
        params = {
            "materialId": material_id,
            "targetId": target_id
        }

        self.assertEqual(
            self.client.request("materialUnassign", params),
            params
        )

    def delete_transform(self, identifier):
        self.assertEqual(
            self.client.request(
                "transformDelete",
                {"id": identifier}
            ),
            {"id": identifier}
        )

    def delete_geometry(self, identifier):
        self.assertEqual(
            self.client.request(
                "geometryDelete",
                {"id": identifier}
            ),
            {"id": identifier}
        )

    def delete_material(self, identifier):
        self.assertEqual(
            self.client.request(
                "materialDelete",
                {"id": identifier}
            ),
            {"id": identifier}
        )

    def physical_mesh(self, identifier, **options):
        return self.client.tessellate(
            identifier,
            kind="SOLID",
            **options
        )

    def domain_mesh(self, domain, **options):
        return self.client.tessellate(
            domain["parentId"],
            kind="DOMAIN",
            geometry_id=domain["id"],
            **options
        )

    def assert_counts(self, transforms, geometries, materials):
        self.assertEqual(len(self.list_transforms()), transforms)
        self.assertEqual(len(self.list_geometries()), geometries)
        self.assertEqual(len(self.list_materials()), materials)

    def assert_matrix(self, actual, expected):
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for index, (value, reference) in enumerate(zip(actual, expected)):
            self.assertAlmostEqual(
                value,
                reference,
                delta=TOLERANCE,
                msg=f"Unexpected matrix component {index}"
            )

    def assert_volume(self, mesh, expected):
        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected,
            delta=TOLERANCE
        )

    def assert_bounds(self, mesh, expected):
        bounds = mesh_bounds(mesh)

        self.assertIsNotNone(bounds)
        self.assertEqual(len(bounds), 6)

        for value, reference in zip(bounds, expected):
            self.assertAlmostEqual(
                value,
                reference,
                delta=TOLERANCE
            )

    def assert_closed_mesh(self, mesh):
        self.assertGreater(mesh["vertexCount"], 0)
        self.assertGreater(mesh["indexCount"], 0)

        for triangle in mesh_triangles(mesh):
            a, b, c = triangle

            self.assertGreater(
                norm(cross(subtract(b, a), subtract(c, a))),
                1e-12
            )

        edges = mesh_edge_counts(mesh)

        self.assertTrue(edges)

        invalid_edges = {
            edge: count
            for edge, count in edges.items()
            if count != 2
        }

        self.assertFalse(
            invalid_edges,
            f"Open or non-manifold edges: {list(invalid_edges.items())[:10]}"
        )

        self.assertGreater(mesh_volume(mesh), 0)

    def assert_same_mesh(self, first, second, exact_triangles=True):
        self.assertEqual(
            first["indexCount"],
            second["indexCount"]
        )

        self.assertAlmostEqual(
            mesh_volume(first),
            mesh_volume(second),
            delta=TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_surface_area(first),
            mesh_surface_area(second),
            delta=TOLERANCE
        )

        if first["vertices"]:
            self.assert_bounds(second, mesh_bounds(first))

        if exact_triangles:
            self.assertEqual(
                mesh_signature(first),
                mesh_signature(second)
            )

    def assert_empty_mesh(self, mesh):
        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)
        self.assertEqual(mesh["vertices"], [])
        self.assertEqual(mesh["indices"], [])
        self.assertEqual(mesh["groups"], [])

    def assert_material_assignment(self, target_id, material_ids):
        target = (
            self.get_geometry(target_id)
            if target_id.startswith("geometry:")
            else self.get_transform(target_id)
        )

        self.assertEqual(
            set(target["materials"]),
            set(material_ids)
        )

        self.assertEqual(
            len(target["materials"]),
            len(set(target["materials"]))
        )

    def assert_material_groups(self, mesh, expected_material_ids):
        self.assertEqual(
            {group["materialId"] for group in mesh["groups"]},
            set(expected_material_ids)
        )

        covered_indices = set()

        for group in mesh["groups"]:
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

            group_indices = set(range(offset, offset + count))

            self.assertFalse(
                covered_indices & group_indices,
                "Overlapping material groups"
            )

            covered_indices.update(group_indices)

        self.assertEqual(
            len(covered_indices),
            mesh["indexCount"]
        )

    def assert_index_consistency(self):
        transforms = {
            item["id"]: self.get_transform(item["id"])
            for item in self.list_transforms()
        }

        geometries = {
            item["id"]: self.get_geometry(item["id"])
            for item in self.list_geometries()
        }

        materials = {
            item["id"]: self.get_material(item["id"])
            for item in self.list_materials()
        }

        geometry_links = {
            identifier: set()
            for identifier in transforms
        }

        for identifier, geometry in geometries.items():
            self.assertIn(
                geometry["parentId"],
                transforms
            )

            geometry_links[geometry["parentId"]].add(identifier)

            if "materials" in geometry:
                self.assertTrue(
                    set(geometry["materials"]).issubset(materials)
                )

            if geometry["kind"] == "DOMAIN" and geometry["source"] == "editor":
                self.assertEqual(geometry["operation"], "ADD")
                self.assertEqual(geometry["materials"], [])

        for identifier, transform in transforms.items():
            self.assertEqual(
                set(transform["geometries"]),
                geometry_links[identifier]
            )

            self.assertTrue(
                set(transform["materials"]).issubset(materials)
            )

            for parent_id in transform["parents"].values():
                if parent_id is not None:
                    self.assertIn(parent_id, transforms)
                    self.assertNotEqual(parent_id, identifier)

        all_entities = [
            *transforms.values(),
            *geometries.values(),
            *materials.values()
        ]

        session_ids = [
            item["id"]
            for item in all_entities
        ]

        global_ids = [
            item["globalId"]
            for item in all_entities
            if item["globalId"] is not None
        ]

        self.assertEqual(
            len(session_ids),
            len(set(session_ids))
        )

        self.assertEqual(
            len(global_ids),
            len(set(global_ids))
        )

    def persistent_snapshot(self):
        # Compare persistent identities and semantic data rather
        # than transient session IDs or STEP identifiers.

        transforms = {
            item["id"]: self.get_transform(item["id"])
            for item in self.list_transforms()
        }

        materials = {
            item["id"]: self.get_material(item["id"])
            for item in self.list_materials()
        }

        def sorted_properties(properties):
            return sorted(
                properties,
                key=lambda item: (
                    item["source"],
                    item["propertySet"],
                    item["name"]
                )
            )

        result = {
            "transforms": {},
            "geometries": [],
            "materials": {}
        }

        for transform in transforms.values():
            result["transforms"][transform["globalId"]] = {
                "name": transform["name"],
                "ifcClass": transform["definition"]["ifcClass"],
                "parents": {
                    key: (
                        None
                        if value is None
                        else transforms[value]["globalId"]
                    )
                    for key, value in transform["parents"].items()
                },
                "transforms": transform["transforms"],
                "properties": sorted_properties(
                    transform["properties"]
                ),
                "materials": sorted(
                    materials[identifier]["name"]
                    for identifier in transform["materials"]
                )
            }

        for item in self.list_geometries():
            geometry = self.get_geometry(item["id"])

            semantic_geometry = {
                "globalId": geometry["globalId"],
                "parentGlobalId": transforms[
                    geometry["parentId"]
                ]["globalId"],
                "ifcClass": geometry["ifcClass"],
                "kind": geometry["kind"],
                "operation": geometry["operation"],
                "source": geometry["source"],
                "editable": geometry["editable"]
            }

            if geometry["source"] == "editor":
                semantic_geometry.update({
                    "name": geometry["name"],
                    "profile": geometry["profile"],
                    "depth": geometry["depth"],
                    "transforms": geometry["transforms"],
                    "materials": sorted(
                        materials[identifier]["name"]
                        for identifier in geometry["materials"]
                    )
                })

            result["geometries"].append(semantic_geometry)

        result["geometries"].sort(
            key=lambda item: json.dumps(item, sort_keys=True)
        )

        for material in materials.values():
            self.assertNotIn(
                material["name"],
                result["materials"]
            )

            result["materials"][material["name"]] = {
                "category": material["category"],
                "visual": material["visual"],
                "properties": sorted_properties(
                    material["properties"]
                ),
                "editable": material["editable"],
                "ifcClass": material["definition"]["ifcClass"]
            }

        return result

    def build_room(
        self,
        storey_id,
        name,
        matrix=None,
        concrete_id=None,
        furniture_material_id=None,
        furnished=False,
        with_domain=False
    ):
        room = self.create_transform(
            name,
            parents=parents_input(
                spatial=storey_id,
                placement=storey_id
            ),
            matrix=matrix,
            properties=[
                property_input("Usage", "Room"),
                property_input(
                    "FloorArea",
                    ROOM_WIDTH * ROOM_LENGTH,
                    value_type="IfcReal"
                )
            ]
        )

        room_id = room["id"]

        wall_specifications = [
            (
                "South wall",
                placement_matrix(y=-WALL_THICKNESS)
            ),
            (
                "North wall",
                placement_matrix(y=ROOM_LENGTH)
            ),
            (
                "West wall",
                placement_matrix(angle=90)
            ),
            (
                "East wall",
                placement_matrix(
                    x=ROOM_WIDTH + WALL_THICKNESS,
                    angle=90
                )
            )
        ]

        walls = [
            self.clone_product(
                WALL_PROTOTYPE_GUID,
                f"{name} - {wall_name}",
                storey_id,
                placement_parent=room_id,
                matrix=wall_matrix,
                material_id=concrete_id,
                properties=[
                    property_input("Usage", "Wall"),
                    property_input(
                        "Thickness",
                        WALL_THICKNESS,
                        value_type="IfcReal"
                    )
                ]
            )
            for wall_name, wall_matrix in wall_specifications
        ]

        floor = self.clone_product(
            SLAB_PROTOTYPE_GUID,
            f"{name} - Floor",
            storey_id,
            placement_parent=room_id,
            matrix=placement_matrix(z=-SLAB_THICKNESS),
            material_id=concrete_id,
            properties=[
                property_input("Usage", "Floor")
            ]
        )

        furniture = []

        if furnished:
            furniture_specifications = [
                ("Table", placement_matrix(1, 1, 0)),
                ("Cabinet", placement_matrix(3, 3, 0))
            ]

            for object_name, object_matrix in furniture_specifications:
                furniture.append(
                    self.clone_product(
                        OBJECT_PROTOTYPE_GUID,
                        f"{name} - {object_name}",
                        storey_id,
                        placement_parent=room_id,
                        matrix=object_matrix,
                        material_id=furniture_material_id,
                        properties=[
                            property_input("Usage", object_name)
                        ]
                    )
                )

        domain = None

        if with_domain:
            domain = self.create_domain(
                room_id,
                f"{name} - Simulation domain"
            )

        return {
            "room": self.get_transform(room_id),
            "walls": walls,
            "floor": floor,
            "furniture": furniture,
            "domain": domain
        }

    @staticmethod
    def room_products(room):
        return [
            *room["walls"],
            room["floor"],
            *room["furniture"]
        ]

    def room_physical_meshes(self, room, space="local"):
        return [
            self.physical_mesh(
                item["id"],
                space=space
            )
            for item in self.room_products(room)
        ]

    def restore_room(self, room):
        return {
            "room": self.find_transform(
                room["room"]["globalId"]
            ),
            "walls": [
                self.find_transform(item["globalId"])
                for item in room["walls"]
            ],
            "floor": self.find_transform(
                room["floor"]["globalId"]
            ),
            "furniture": [
                self.find_transform(item["globalId"])
                for item in room["furniture"]
            ],
            "domain": (
                None
                if room["domain"] is None
                else self.find_geometry(
                    room["domain"]["globalId"]
                )
            )
        }

    def assert_room(self, room, storey_id, furnished=False):
        room_transform = self.get_transform(
            room["room"]["id"]
        )

        self.assertEqual(
            room_transform["parents"]["spatial"],
            storey_id
        )

        self.assertEqual(
            room_transform["parents"]["placement"],
            storey_id
        )

        self.assertEqual(len(room["walls"]), 4)

        for wall in room["walls"]:
            current = self.get_transform(wall["id"])

            self.assertEqual(
                current["definition"]["ifcClass"],
                "IfcWall"
            )

            self.assertEqual(
                current["parents"]["spatial"],
                storey_id
            )

            self.assertEqual(
                current["parents"]["placement"],
                room_transform["id"]
            )

            mesh = self.physical_mesh(wall["id"])

            self.assert_closed_mesh(mesh)
            self.assert_volume(mesh, WALL_VOLUME)

        floor = self.get_transform(room["floor"]["id"])

        self.assertEqual(
            floor["definition"]["ifcClass"],
            "IfcSlab"
        )

        self.assertEqual(
            floor["parents"]["placement"],
            room_transform["id"]
        )

        floor_mesh = self.physical_mesh(floor["id"])

        self.assert_closed_mesh(floor_mesh)
        self.assert_volume(floor_mesh, SLAB_VOLUME)

        expected_furniture_count = 2 if furnished else 0

        self.assertEqual(
            len(room["furniture"]),
            expected_furniture_count
        )

        for item in room["furniture"]:
            current = self.get_transform(item["id"])

            self.assertEqual(
                current["parents"]["placement"],
                room_transform["id"]
            )

            mesh = self.physical_mesh(item["id"])

            self.assert_closed_mesh(mesh)
            self.assert_volume(mesh, OBJECT_VOLUME)

        combined = self.physical_mesh(
            room_transform["id"],
            include_children=True
        )

        self.assert_volume(
            combined,
            ROOM_PHYSICAL_VOLUME + expected_furniture_count
        )

        self.assert_bounds(
            combined,
            (
                -WALL_THICKNESS,
                -WALL_THICKNESS,
                -SLAB_THICKNESS,
                ROOM_WIDTH + WALL_THICKNESS,
                ROOM_LENGTH + WALL_THICKNESS,
                ROOM_HEIGHT
            )
        )

        if room["domain"] is not None:
            domain = self.get_geometry(room["domain"]["id"])

            self.assertEqual(
                domain["parentId"],
                room_transform["id"]
            )

            mesh = self.domain_mesh(domain)

            self.assert_closed_mesh(mesh)
            self.assert_volume(mesh, ROOM_DOMAIN_VOLUME)
            self.assertEqual(mesh["groups"], [])

        return combined

    def test_building_creation(self):
        """Create a multi-storey building with walls, slabs and materials."""

        concrete = self.create_concrete()
        steel = self.create_steel()

        ground = self.build_room(
            self.ground_storey_id,
            "Ground floor room",
            concrete_id=concrete["id"]
        )

        upper = self.build_room(
            self.upper_storey_id,
            "Upper floor room",
            concrete_id=steel["id"]
        )

        ground_mesh = self.assert_room(
            ground,
            self.ground_storey_id
        )

        upper_mesh = self.assert_room(
            upper,
            self.upper_storey_id
        )

        self.assert_same_mesh(
            ground_mesh,
            upper_mesh
        )

        for item in self.room_products(ground):
            self.assert_material_assignment(
                item["id"],
                [concrete["id"]]
            )

        for item in self.room_products(upper):
            self.assert_material_assignment(
                item["id"],
                [steel["id"]]
            )

        building = self.find_transform(BUILDING_GUID)

        self.assertEqual(
            building["definition"]["ifcClass"],
            "IfcBuilding"
        )

        for global_id in (
            GROUND_STOREY_GUID,
            UPPER_STOREY_GUID
        ):
            storey = self.find_transform(global_id)

            self.assertEqual(
                storey["definition"]["ifcClass"],
                "IfcBuildingStorey"
            )

            self.assertEqual(
                storey["parents"]["decomposition"],
                building["id"]
            )

        building_mesh = self.physical_mesh(
            building["id"],
            include_children=True,
            space="world"
        )

        self.assert_volume(
            building_mesh,
            2 * ROOM_PHYSICAL_VOLUME
        )

        self.assert_bounds(
            building_mesh,
            (-0.25, -0.25, -0.25, 6.25, 6.25, 7)
        )

        self.assert_material_groups(
            building_mesh,
            [concrete["id"], steel["id"]]
        )

        self.assert_counts(
            transforms=19,
            geometries=13,
            materials=2
        )

        expected = self.persistent_snapshot()

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

        ground = self.restore_room(ground)
        upper = self.restore_room(upper)

        self.assert_room(
            ground,
            self.ground_storey_id
        )

        self.assert_room(
            upper,
            self.upper_storey_id
        )

        self.assert_same_mesh(
            building_mesh,
            self.physical_mesh(
                self.building_id,
                include_children=True,
                space="world"
            )
        )

        self.assert_index_consistency()

    def test_wall_with_openings(self):
        """Create a wall with multiple openings and verify its final volume."""

        concrete = self.create_concrete()

        wall = self.clone_product(
            WALL_PROTOTYPE_GUID,
            "Wall with three openings",
            self.ground_storey_id,
            material_id=concrete["id"]
        )

        baseline = self.physical_mesh(wall["id"])

        self.assert_volume(baseline, WALL_VOLUME)

        door = self.create_opening(
            wall["id"],
            "Door opening",
            x=0.5,
            width=1,
            z=-1,
            height=3
        )

        first_window = self.create_opening(
            wall["id"],
            "First window opening",
            x=2,
            width=1.5,
            z=1,
            height=1
        )

        second_window = self.create_opening(
            wall["id"],
            "Second window opening",
            x=4,
            width=0.5,
            z=1,
            height=1
        )

        openings = [
            door,
            first_window,
            second_window
        ]

        removed_volume = (
            1 * WALL_THICKNESS * 2
            + 1.5 * WALL_THICKNESS * 1
            + 0.5 * WALL_THICKNESS * 1
        )

        expected_volume = WALL_VOLUME - removed_volume

        self.assertAlmostEqual(expected_volume, 3.5)

        for opening in openings:
            self.assertEqual(
                opening["ifcClass"],
                "IfcOpeningElement"
            )

            self.assertEqual(
                opening["operation"],
                "SUBTRACT"
            )

            self.assertEqual(
                opening["parentId"],
                wall["id"]
            )

        mesh = self.physical_mesh(wall["id"])

        self.assert_closed_mesh(mesh)
        self.assert_volume(mesh, expected_volume)

        self.assert_bounds(
            mesh,
            (0, 0, 0, 6, 0.25, 3)
        )

        surface = ClosedSurface(mesh)

        for point in (
            (1, 0.125, 1),
            (2.5, 0.125, 1.5),
            (4.25, 0.125, 1.5)
        ):
            self.assertFalse(
                surface.contains(point),
                f"Opening still contains matter: {point}"
            )

        for point in (
            (0.25, 0.125, 1),
            (1.75, 0.125, 1),
            (5.5, 0.125, 1.5)
        ):
            self.assertTrue(surface.contains(point))

        self.assertEqual(
            len(self.list_geometries(wall["id"], kind="SOLID")),
            4
        )

        self.assert_material_groups(
            mesh,
            [concrete["id"]]
        )

        self.save_and_reload()

        wall = self.find_transform(wall["globalId"])

        self.assert_same_mesh(
            mesh,
            self.physical_mesh(wall["id"])
        )

        for opening in openings:
            current = self.find_geometry(opening["globalId"])

            self.assertEqual(
                current["parentId"],
                wall["id"]
            )

            self.assertEqual(current["operation"], "SUBTRACT")

        self.delete_geometry(
            self.find_geometry(second_window["globalId"])["id"]
        )

        self.assert_volume(
            self.physical_mesh(wall["id"]),
            3.625
        )

        self.delete_geometry(
            self.find_geometry(first_window["globalId"])["id"]
        )

        self.assert_volume(
            self.physical_mesh(wall["id"]),
            4.0
        )

        self.delete_geometry(
            self.find_geometry(door["globalId"])["id"]
        )

        self.assert_same_mesh(
            baseline,
            self.physical_mesh(wall["id"])
        )

        self.assert_index_consistency()

    def test_furnished_room(self):
        """Create a room containing independent physical objects."""

        concrete = self.create_concrete()
        wood = self.create_wood()

        room = self.build_room(
            self.ground_storey_id,
            "Furnished room",
            concrete_id=concrete["id"],
            furniture_material_id=wood["id"],
            furnished=True
        )

        combined = self.assert_room(
            room,
            self.ground_storey_id,
            furnished=True
        )

        self.assert_volume(combined, 29.0)

        first, second = room["furniture"]

        first_mesh = self.physical_mesh(
            first["id"],
            space="world"
        )

        second_mesh = self.physical_mesh(
            second["id"],
            space="world"
        )

        self.assert_bounds(
            first_mesh,
            (1, 1, 0, 2, 2, 1)
        )

        self.assert_bounds(
            second_mesh,
            (3, 3, 0, 4, 4, 1)
        )

        self.assert_material_assignment(
            first["id"],
            [wood["id"]]
        )

        self.assert_material_assignment(
            second["id"],
            [wood["id"]]
        )

        self.update_transform(
            first["id"],
            transform=transform_input(
                placement_matrix(4, 1, 0)
            )
        )

        moved_mesh = self.physical_mesh(
            first["id"],
            space="world"
        )

        self.assert_bounds(
            moved_mesh,
            (4, 1, 0, 5, 2, 1)
        )

        self.assert_same_mesh(
            second_mesh,
            self.physical_mesh(
                second["id"],
                space="world"
            )
        )

        self.assert_volume(
            self.physical_mesh(
                room["room"]["id"],
                include_children=True
            ),
            29.0
        )

        self.delete_transform(first["id"])

        self.client.expect_error(
            "transformGet",
            {"id": first["id"]},
            "ENTITY_NOT_FOUND"
        )

        self.assert_volume(
            self.physical_mesh(
                room["room"]["id"],
                include_children=True
            ),
            28.0
        )

        self.assertEqual(
            self.get_material(wood["id"])["name"],
            "Wood"
        )

        self.save_and_reload()

        second = self.find_transform(second["globalId"])
        room_transform = self.find_transform(
            room["room"]["globalId"]
        )

        self.assert_volume(
            self.physical_mesh(
                room_transform["id"],
                include_children=True
            ),
            28.0
        )

        self.assert_same_mesh(
            second_mesh,
            self.physical_mesh(
                second["id"],
                space="world"
            )
        )

        self.assert_index_consistency()

    def test_room_simulation(self):
        """Create a room, physical obstacles and an enclosing simulation domain."""

        concrete = self.create_concrete()
        wood = self.create_wood()

        room = self.build_room(
            self.ground_storey_id,
            "Simulation room",
            concrete_id=concrete["id"],
            furniture_material_id=wood["id"],
            furnished=True,
            with_domain=True
        )

        self.assert_room(
            room,
            self.ground_storey_id,
            furnished=True
        )

        room_id = room["room"]["id"]
        domain = room["domain"]

        physical = self.physical_mesh(
            room_id,
            include_children=True,
            space="world"
        )

        simulation = self.domain_mesh(
            domain,
            space="world"
        )

        self.assert_volume(physical, 29.0)
        self.assert_volume(simulation, 108.0)

        self.assert_bounds(
            simulation,
            (0, 0, 0, 6, 6, 3)
        )

        self.assertEqual(simulation["groups"], [])

        domain_surface = ClosedSurface(simulation)

        physical_surfaces = [
            ClosedSurface(mesh)
            for mesh in self.room_physical_meshes(
                room,
                space="world"
            )
        ]

        cases = [
            ((1.5, 1.5, 0.5), SOLID),
            ((3.5, 3.5, 0.5), SOLID),
            ((2.5, 2.5, 0.5), FREE),
            ((1.5, 1.5, 2.5), FREE),
            ((5.5, 5.5, 2.5), FREE),
            ((-0.5, 2.5, 1.5), OUTSIDE),
            ((6.5, 2.5, 1.5), OUTSIDE),
            ((2.5, 2.5, 3.5), OUTSIDE)
        ]

        for point, expected in cases:
            with self.subTest(point=point):
                self.assertEqual(
                    classify_point(
                        point,
                        [domain_surface],
                        physical_surfaces
                    ),
                    expected
                )

        occupancy = voxelize(
            origin=(-1, -1, -1),
            dimensions=(8, 8, 5),
            spacing=1,
            domain_meshes=[simulation],
            physical_meshes=self.room_physical_meshes(
                room,
                space="world"
            )
        )

        self.assertEqual(
            Counter(occupancy.values()),
            {
                OUTSIDE: 212,
                FREE: 106,
                SOLID: 2
            }
        )

        self.assertEqual(
            len(voxel_components(occupancy, FREE)),
            1
        )

        self.assertEqual(
            len(voxel_components(occupancy, SOLID)),
            2
        )

        self.assert_material_groups(
            physical,
            [concrete["id"], wood["id"]]
        )

        self.delete_geometry(domain["id"])

        self.assert_same_mesh(
            physical,
            self.physical_mesh(
                room_id,
                include_children=True,
                space="world"
            )
        )

        self.assert_empty_mesh(
            self.client.tessellate(
                room_id,
                kind="DOMAIN",
                include_children=True
            )
        )

        self.assert_index_consistency()

    def test_multizone_simulation(self):
        """Create and independently extract multiple simulation zones."""

        concrete = self.create_concrete()
        wood = self.create_wood()

        first = self.build_room(
            self.ground_storey_id,
            "Simulation zone A",
            concrete_id=concrete["id"],
            furniture_material_id=wood["id"],
            furnished=True,
            with_domain=True
        )

        second = self.build_room(
            self.upper_storey_id,
            "Simulation zone B",
            matrix=placement_matrix(x=10),
            concrete_id=concrete["id"],
            furniture_material_id=wood["id"],
            furnished=True,
            with_domain=True
        )

        first_mesh = self.domain_mesh(
            first["domain"],
            space="world"
        )

        second_mesh = self.domain_mesh(
            second["domain"],
            space="world"
        )

        self.assert_volume(first_mesh, 108.0)
        self.assert_volume(second_mesh, 108.0)

        self.assert_bounds(
            first_mesh,
            (0, 0, 0, 6, 6, 3)
        )

        self.assert_bounds(
            second_mesh,
            (10, 0, 4, 16, 6, 7)
        )

        for room, reference in (
            (first, first_mesh),
            (second, second_mesh)
        ):
            selected = self.client.tessellate(
                self.building_id,
                kind="DOMAIN",
                geometry_id=room["domain"]["id"],
                include_children=True,
                space="world"
            )

            self.assert_same_mesh(reference, selected)

        combined = self.client.tessellate(
            self.building_id,
            kind="DOMAIN",
            include_children=True,
            space="world"
        )

        self.assertEqual(
            mesh_signature(combined),
            mesh_signature(first_mesh) + mesh_signature(second_mesh)
        )

        self.assert_volume(combined, 216.0)

        self.assertEqual(combined["groups"], [])

        first_surface = ClosedSurface(first_mesh)
        second_surface = ClosedSurface(second_mesh)

        self.assertTrue(first_surface.contains((3, 3, 1.5)))
        self.assertFalse(second_surface.contains((3, 3, 1.5)))

        self.assertTrue(second_surface.contains((13, 3, 5.5)))
        self.assertFalse(first_surface.contains((13, 3, 5.5)))

        self.client.expect_error(
            "transformTessellate",
            {
                "id": first["room"]["id"],
                "options": {
                    "kind": "DOMAIN",
                    "space": "local",
                    "geometryId": second["domain"]["id"],
                    "includeChildren": True,
                    "includeNormals": False,
                    "includeMaterials": False
                }
            },
            "INVALID_PARAMETERS"
        )

        self.update_geometry(
            first["domain"]["id"],
            depth=4
        )

        self.assert_volume(
            self.domain_mesh(first["domain"]),
            144.0
        )

        self.assert_same_mesh(
            second_mesh,
            self.domain_mesh(
                second["domain"],
                space="world"
            )
        )

        physical = self.physical_mesh(
            self.building_id,
            include_children=True
        )

        self.assert_volume(physical, 58.0)

        self.save_and_reload()

        first = self.restore_room(first)
        second = self.restore_room(second)

        self.assert_volume(
            self.domain_mesh(first["domain"]),
            144.0
        )

        self.assert_volume(
            self.domain_mesh(second["domain"]),
            108.0
        )

        self.assert_volume(
            self.client.tessellate(
                self.building_id,
                kind="DOMAIN",
                include_children=True
            ),
            252.0
        )

        self.assert_index_consistency()

    def test_building_duplication(self):
        """Duplicate a furnished storey with properties, materials and domains."""

        concrete = self.create_concrete()
        wood = self.create_wood()

        storey = self.find_transform(GROUND_STOREY_GUID)

        self.update_transform(
            storey["id"],
            properties=[
                property_input("Usage", "Residential"),
                property_input(
                    "Level",
                    0,
                    value_type="IfcInteger"
                )
            ]
        )

        room = self.build_room(
            storey["id"],
            "Duplicated room",
            concrete_id=concrete["id"],
            furniture_material_id=wood["id"],
            furnished=True,
            with_domain=True
        )

        source_transforms = [
            self.get_transform(storey["id"]),
            self.get_transform(room["room"]["id"]),
            *[
                self.get_transform(item["id"])
                for item in self.room_products(room)
            ]
        ]

        source_ids = {
            item["id"]
            for item in source_transforms
        }

        source_geometries = {
            geometry_id
            for item in source_transforms
            for geometry_id in item["geometries"]
        }

        physical_before = self.physical_mesh(
            storey["id"],
            include_children=True
        )

        domain_before = self.client.tessellate(
            storey["id"],
            kind="DOMAIN",
            include_children=True
        )

        duplication = self.duplicate_transform(
            storey["id"],
            include_children=True
        )

        transform_mapping = duplication["transforms"]
        geometry_mapping = duplication["geometries"]

        self.assertEqual(
            set(transform_mapping),
            source_ids
        )

        self.assertEqual(
            set(geometry_mapping),
            source_geometries
        )

        self.assertFalse(
            source_ids & set(transform_mapping.values())
        )

        self.assertFalse(
            source_geometries & set(geometry_mapping.values())
        )

        duplicate_storey_id = duplication["rootId"]

        duplicate_storey = self.get_transform(
            duplicate_storey_id
        )

        self.assertEqual(
            duplicate_storey["definition"]["ifcClass"],
            "IfcBuildingStorey"
        )

        self.assertEqual(
            duplicate_storey["parents"]["decomposition"],
            self.building_id
        )

        for source in source_transforms:
            copied = self.get_transform(
                transform_mapping[source["id"]]
            )

            self.assertNotEqual(
                copied["globalId"],
                source["globalId"]
            )

            self.assertEqual(
                copied["definition"]["ifcClass"],
                source["definition"]["ifcClass"]
            )

            self.assertCountEqual(
                copied["properties"],
                source["properties"]
            )

            self.assertEqual(
                set(copied["materials"]),
                set(source["materials"])
            )

            expected_parents = {
                key: transform_mapping.get(value, value)
                for key, value in source["parents"].items()
            }

            self.assertEqual(
                copied["parents"],
                expected_parents
            )

            self.assertEqual(
                set(copied["geometries"]),
                {
                    geometry_mapping[identifier]
                    for identifier in source["geometries"]
                }
            )

        copied_domain = self.get_geometry(
            geometry_mapping[room["domain"]["id"]]
        )

        self.assertNotEqual(
            copied_domain["globalId"],
            room["domain"]["globalId"]
        )

        self.assertEqual(
            copied_domain["parentId"],
            transform_mapping[room["room"]["id"]]
        )

        self.assertEqual(
            copied_domain["profile"],
            room["domain"]["profile"]
        )

        self.assertEqual(
            copied_domain["depth"],
            room["domain"]["depth"]
        )

        self.assert_same_mesh(
            physical_before,
            self.physical_mesh(
                duplicate_storey_id,
                include_children=True
            )
        )

        self.assert_same_mesh(
            domain_before,
            self.client.tessellate(
                duplicate_storey_id,
                kind="DOMAIN",
                include_children=True
            )
        )

        self.update_transform(
            duplicate_storey_id,
            name="Duplicated upper storey",
            transform=transform_input(
                placement_matrix(z=8),
                space="world"
            )
        )

        copied_world_mesh = self.physical_mesh(
            duplicate_storey_id,
            include_children=True,
            space="world"
        )

        self.assertEqual(
            mesh_signature(copied_world_mesh),
            mesh_signature(
                physical_before,
                translation=(0, 0, 8)
            )
        )

        self.assert_bounds(
            self.domain_mesh(
                copied_domain,
                space="world"
            ),
            (0, 0, 8, 6, 6, 11)
        )

        self.update_geometry(
            copied_domain["id"],
            depth=4
        )

        self.assert_volume(
            self.domain_mesh(copied_domain),
            144.0
        )

        self.assert_volume(
            self.domain_mesh(room["domain"]),
            108.0
        )

        self.assert_same_mesh(
            physical_before,
            self.physical_mesh(
                storey["id"],
                include_children=True
            )
        )

        self.assertEqual(len(self.list_materials()), 2)

        expected = self.persistent_snapshot()

        copied_storey_guid = duplicate_storey["globalId"]
        copied_domain_guid = copied_domain["globalId"]

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

        copied_storey = self.find_transform(
            copied_storey_guid
        )

        copied_domain = self.find_geometry(
            copied_domain_guid
        )

        self.assert_same_mesh(
            copied_world_mesh,
            self.physical_mesh(
                copied_storey["id"],
                include_children=True,
                space="world"
            )
        )

        self.assert_volume(
            self.domain_mesh(copied_domain),
            144.0
        )

        self.assert_index_consistency()

    def test_building_restructuring(self):
        """Restructure a building while preserving requested placements."""

        concrete = self.create_concrete()

        room = self.build_room(
            self.ground_storey_id,
            "Restructured room",
            concrete_id=concrete["id"],
            with_domain=True
        )

        room_id = room["room"]["id"]
        wall = room["walls"][0]

        wall_before = self.get_transform(wall["id"])

        wall_world_before = wall_before["transforms"]["world"]

        wall_mesh_before = self.physical_mesh(
            wall["id"],
            space="world"
        )

        # Change both containment and placement parent while
        # explicitly preserving the wall's world matrix.

        moved_wall = self.update_transform(
            wall["id"],
            parents=parents_input(
                spatial=self.upper_storey_id,
                placement=self.upper_storey_id
            ),
            transform=transform_input(
                wall_world_before,
                space="world"
            )
        )

        self.assert_matrix(
            moved_wall["transforms"]["world"],
            wall_world_before
        )

        self.assert_matrix(
            moved_wall["transforms"]["local"],
            placement_matrix(
                y=-WALL_THICKNESS,
                z=-4
            )
        )

        self.assert_same_mesh(
            wall_mesh_before,
            self.physical_mesh(
                wall["id"],
                space="world"
            )
        )

        domain_before = self.domain_mesh(
            room["domain"],
            space="local"
        )

        # Reparent the room without changing its local placement.
        # The new storey is four metres above the original storey.
        # Remaining descendants must follow the room.

        moved_room = self.update_transform(
            room_id,
            parents=parents_input(
                spatial=self.upper_storey_id,
                placement=self.upper_storey_id
            )
        )

        self.assert_matrix(
            moved_room["transforms"]["local"],
            IDENTITY_MATRIX
        )

        self.assert_matrix(
            moved_room["transforms"]["world"],
            placement_matrix(z=4)
        )

        # Update the spatial containment of the remaining physical
        # elements independently from their placement relationships.

        remaining_products = [
            item
            for item in self.room_products(room)
            if item["id"] != wall["id"]
        ]

        for item in remaining_products:
            before = self.get_transform(item["id"])

            updated = self.update_transform(
                item["id"],
                parents=parents_input(
                    spatial=self.upper_storey_id,
                    placement=room_id
                )
            )

            self.assert_matrix(
                updated["transforms"]["local"],
                before["transforms"]["local"]
            )

            self.assert_matrix(
                updated["transforms"]["world"],
                before["transforms"]["world"]
            )

        self.assert_same_mesh(
            domain_before,
            self.domain_mesh(room["domain"])
        )

        self.assert_bounds(
            self.domain_mesh(
                room["domain"],
                space="world"
            ),
            (0, 0, 4, 6, 6, 7)
        )

        self.assert_same_mesh(
            wall_mesh_before,
            self.physical_mesh(
                wall["id"],
                space="world"
            )
        )

        self.assert_empty_mesh(
            self.physical_mesh(
                self.ground_storey_id,
                include_children=True
            )
        )

        self.assert_volume(
            self.physical_mesh(
                self.upper_storey_id,
                include_children=True
            ),
            27.0
        )

        self.assert_volume(
            self.physical_mesh(
                room_id,
                include_children=True
            ),
            22.5
        )

        expected = self.persistent_snapshot()

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

        wall = self.find_transform(wall["globalId"])
        room = self.restore_room(room)

        self.assertEqual(
            wall["parents"]["placement"],
            self.upper_storey_id
        )

        self.assertEqual(
            room["room"]["parents"]["placement"],
            self.upper_storey_id
        )

        self.assert_same_mesh(
            wall_mesh_before,
            self.physical_mesh(
                wall["id"],
                space="world"
            )
        )

        self.assert_index_consistency()

    def test_material_replacement(self):
        """Replace a shared material without changing physical geometry."""

        original = self.create_concrete(
            name="Original shared concrete"
        )

        replacement = self.create_material(
            name="Replacement concrete",
            category="concrete",
            visual={
                "color": [0.35, 0.38, 0.4],
                "opacity": 1.0,
                "metallic": 0.0,
                "roughness": 0.7
            },
            density=2500.0
        )

        room = self.build_room(
            self.ground_storey_id,
            "Material replacement room",
            concrete_id=original["id"],
            with_domain=True
        )

        targets = self.room_products(room)

        physical_before = self.physical_mesh(
            room["room"]["id"],
            include_children=True
        )

        domain_before = self.domain_mesh(
            room["domain"]
        )

        self.assert_material_groups(
            physical_before,
            [original["id"]]
        )

        for target in targets:
            self.assign_material(
                replacement["id"],
                target["id"]
            )

            self.unassign_material(
                original["id"],
                target["id"]
            )

            self.assert_material_assignment(
                target["id"],
                [replacement["id"]]
            )

        self.delete_material(original["id"])

        self.client.expect_error(
            "materialGet",
            {"id": original["id"]},
            "ENTITY_NOT_FOUND"
        )

        self.assertEqual(len(self.list_materials()), 1)

        physical_after = self.physical_mesh(
            room["room"]["id"],
            include_children=True
        )

        self.assert_same_mesh(
            physical_before,
            physical_after
        )

        self.assert_material_groups(
            physical_after,
            [replacement["id"]]
        )

        self.assert_same_mesh(
            domain_before,
            self.domain_mesh(room["domain"])
        )

        self.assertEqual(
            self.get_geometry(room["domain"]["id"])["materials"],
            []
        )

        updated_visual = {
            "color": [0.25, 0.3, 0.35],
            "opacity": 1.0,
            "metallic": 0.0,
            "roughness": 0.5
        }

        updated = self.client.request(
            "materialUpdate",
            {
                "id": replacement["id"],
                "name": "Final shared concrete",
                "visual": updated_visual,
                "properties": [
                    property_input(
                        "Density",
                        2600.0,
                        value_type="IfcReal",
                        property_set="Pset_Physical",
                        source="material"
                    )
                ]
            }
        )

        self.assertEqual(
            updated["id"],
            replacement["id"]
        )

        for target in targets:
            current = self.get_transform(target["id"])

            self.assertEqual(
                current["materials"],
                [replacement["id"]]
            )

            material = self.get_material(
                current["materials"][0]
            )

            self.assertEqual(
                material["name"],
                "Final shared concrete"
            )

            self.assertEqual(
                material["visual"],
                updated_visual
            )

            self.assertEqual(
                material["properties"][0]["value"],
                2600.0
            )

        expected = self.persistent_snapshot()

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

        room = self.restore_room(room)

        replacement = self.find_material(
            "Final shared concrete"
        )

        for target in self.room_products(room):
            self.assert_material_assignment(
                target["id"],
                [replacement["id"]]
            )

        self.assert_material_groups(
            self.physical_mesh(
                room["room"]["id"],
                include_children=True
            ),
            [replacement["id"]]
        )

        self.assert_index_consistency()

    def test_complete_editing_workflow(self):
        """Execute a complete load, edit, mesh, duplicate, delete and save cycle."""

        concrete = self.create_concrete(
            name="Workflow concrete"
        )

        owner = self.create_transform(
            "Workflow owner",
            parents=parents_input(
                spatial=self.ground_storey_id,
                placement=self.ground_storey_id
            ),
            properties=[
                property_input("Reference", "WORKFLOW-001")
            ]
        )

        wall = self.clone_product(
            WALL_PROTOTYPE_GUID,
            "Workflow wall",
            self.ground_storey_id,
            placement_parent=owner["id"],
            material_id=concrete["id"],
            properties=[
                property_input("Reference", "WALL-001")
            ]
        )

        baseline = self.physical_mesh(wall["id"])

        self.assert_volume(baseline, 4.5)

        opening = self.create_opening(
            wall["id"],
            "Workflow opening",
            x=1,
            width=1,
            z=0.5,
            height=1
        )

        self.assert_volume(
            self.physical_mesh(wall["id"]),
            4.25
        )

        domain = self.create_domain(
            owner["id"],
            "Workflow domain",
            width=10,
            length=5,
            depth=4
        )

        self.assert_volume(
            self.domain_mesh(domain),
            200.0
        )

        self.update_geometry(
            opening["id"],
            name="Updated workflow opening",
            depth=2
        )

        self.assert_volume(
            self.physical_mesh(wall["id"]),
            4.0
        )

        self.update_transform(
            wall["id"],
            name="Updated workflow wall",
            transform=transform_input(
                placement_matrix(2, 1, 0)
            ),
            properties=[
                property_input("Reference", "WALL-UPDATED"),
                property_input(
                    "Revision",
                    2,
                    value_type="IfcInteger"
                )
            ]
        )

        self.update_geometry(
            domain["id"],
            depth=5
        )

        self.assert_volume(
            self.domain_mesh(domain),
            250.0
        )

        physical_reference = self.physical_mesh(
            owner["id"],
            include_children=True
        )

        domain_reference = self.domain_mesh(domain)

        duplication = self.duplicate_transform(
            owner["id"],
            include_children=True
        )

        self.assertEqual(
            set(duplication["transforms"]),
            {owner["id"], wall["id"]}
        )

        duplicated_owner_id = duplication["rootId"]

        duplicated_wall_id = duplication["transforms"][
            wall["id"]
        ]

        duplicated_opening_id = duplication["geometries"][
            opening["id"]
        ]

        duplicated_domain_id = duplication["geometries"][
            domain["id"]
        ]

        duplicated_domain = self.get_geometry(
            duplicated_domain_id
        )

        self.assert_same_mesh(
            physical_reference,
            self.physical_mesh(
                duplicated_owner_id,
                include_children=True
            )
        )

        self.assert_same_mesh(
            domain_reference,
            self.domain_mesh(duplicated_domain)
        )

        self.assert_material_assignment(
            duplicated_wall_id,
            [concrete["id"]]
        )

        self.update_geometry(
            duplicated_opening_id,
            depth=1
        )

        self.assert_volume(
            self.physical_mesh(duplicated_wall_id),
            4.25
        )

        self.assert_volume(
            self.physical_mesh(wall["id"]),
            4.0
        )

        self.update_geometry(
            duplicated_domain_id,
            depth=3
        )

        self.assert_volume(
            self.domain_mesh(duplicated_domain),
            150.0
        )

        self.assert_volume(
            self.domain_mesh(domain),
            250.0
        )

        self.delete_transform(duplicated_owner_id)

        for identifier in duplication["transforms"].values():
            self.client.expect_error(
                "transformGet",
                {"id": identifier},
                "ENTITY_NOT_FOUND"
            )

        for identifier in duplication["geometries"].values():
            self.client.expect_error(
                "geometryGet",
                {"id": identifier},
                "ENTITY_NOT_FOUND"
            )

        self.assert_same_mesh(
            physical_reference,
            self.physical_mesh(
                owner["id"],
                include_children=True
            )
        )

        self.assert_same_mesh(
            domain_reference,
            self.domain_mesh(domain)
        )

        self.assert_counts(
            transforms=9,
            geometries=6,
            materials=1
        )

        expected = self.persistent_snapshot()

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

        owner = self.find_transform(owner["globalId"])
        wall = self.find_transform(wall["globalId"])
        opening = self.find_geometry(opening["globalId"])
        domain = self.find_geometry(domain["globalId"])
        concrete = self.find_material("Workflow concrete")

        self.assertEqual(
            wall["name"],
            "Updated workflow wall"
        )

        self.assertEqual(
            opening["name"],
            "Updated workflow opening"
        )

        self.assertEqual(opening["depth"], 2)
        self.assertEqual(domain["depth"], 5)

        self.assert_material_assignment(
            wall["id"],
            [concrete["id"]]
        )

        self.assert_same_mesh(
            physical_reference,
            self.physical_mesh(
                owner["id"],
                include_children=True
            )
        )

        self.assert_same_mesh(
            domain_reference,
            self.domain_mesh(domain)
        )

        self.assert_index_consistency()

    def test_simulation_roundtrip(self):
        """Verify persistence of all data required for voxel simulation."""

        concrete = self.create_concrete(
            name="Simulation concrete"
        )

        wood = self.create_wood(
            name="Simulation wood"
        )

        room = self.build_room(
            self.ground_storey_id,
            "Persistent simulation room",
            concrete_id=concrete["id"],
            furniture_material_id=wood["id"],
            furnished=True,
            with_domain=True
        )

        room_id = room["room"]["id"]

        # Add a through opening to the first obstacle.
        # The removed volume is exactly 0.25 cubic metres.

        first_obstacle = room["furniture"][0]

        opening = self.create_geometry(
            first_obstacle["id"],
            "Obstacle ventilation opening",
            rectangular_profile(0.5, 0.5),
            depth=3,
            matrix=placement_matrix(z=-1),
            kind="SOLID",
            operation="SUBTRACT"
        )

        self.assert_volume(
            self.physical_mesh(first_obstacle["id"]),
            0.75
        )

        domain_before = self.domain_mesh(
            room["domain"],
            space="world"
        )

        physical_before = self.physical_mesh(
            room_id,
            include_children=True,
            space="world"
        )

        individual_before = {
            item["globalId"]: self.physical_mesh(
                item["id"],
                space="world"
            )
            for item in self.room_products(room)
        }

        self.assert_volume(domain_before, 108.0)
        self.assert_volume(physical_before, 28.75)

        self.assert_closed_mesh(domain_before)

        for mesh in individual_before.values():
            self.assert_closed_mesh(mesh)

        origin = (-1, -1, -1)
        dimensions = (16, 16, 10)
        spacing = 0.5
        voxel_volume = spacing ** 3

        reference_occupancy = voxelize(
            origin,
            dimensions,
            spacing,
            [domain_before],
            list(individual_before.values())
        )

        reference_counts = Counter(
            reference_occupancy.values()
        )

        self.assertEqual(
            reference_counts,
            {
                OUTSIDE: 1696,
                FREE: 850,
                SOLID: 14
            }
        )

        self.assertAlmostEqual(
            reference_counts[SOLID] * voxel_volume,
            1.75
        )

        self.assertAlmostEqual(
            reference_counts[FREE] * voxel_volume,
            106.25
        )

        self.assertAlmostEqual(
            (
                reference_counts[SOLID]
                + reference_counts[FREE]
            ) * voxel_volume,
            ROOM_DOMAIN_VOLUME
        )

        self.assertEqual(
            len(voxel_components(reference_occupancy, FREE)),
            1
        )

        self.assertEqual(
            len(voxel_components(reference_occupancy, SOLID)),
            2
        )

        self.assert_material_groups(
            physical_before,
            [concrete["id"], wood["id"]]
        )

        self.assertEqual(domain_before["groups"], [])

        expected_snapshot = self.persistent_snapshot()

        for iteration in range(3):
            with self.subTest(roundtrip=iteration):
                path = self.save_and_reload()

                self.assertEqual(
                    self.persistent_snapshot(),
                    expected_snapshot
                )

                room = self.restore_room(room)

                domain_after = self.domain_mesh(
                    room["domain"],
                    space="world"
                )

                physical_after = self.physical_mesh(
                    room["room"]["id"],
                    include_children=True,
                    space="world"
                )

                self.assert_same_mesh(
                    domain_before,
                    domain_after
                )

                self.assert_same_mesh(
                    physical_before,
                    physical_after
                )

                individual_after = {
                    item["globalId"]: self.physical_mesh(
                        item["id"],
                        space="world"
                    )
                    for item in self.room_products(room)
                }

                self.assertEqual(
                    set(individual_before),
                    set(individual_after)
                )

                for global_id, reference in individual_before.items():
                    self.assert_same_mesh(
                        reference,
                        individual_after[global_id]
                    )

                occupancy_after = voxelize(
                    origin,
                    dimensions,
                    spacing,
                    [domain_after],
                    list(individual_after.values())
                )

                self.assertEqual(
                    occupancy_after,
                    reference_occupancy
                )

                self.assertEqual(
                    Counter(occupancy_after.values()),
                    reference_counts
                )

                self.assertEqual(
                    voxel_components(occupancy_after, FREE),
                    voxel_components(reference_occupancy, FREE)
                )

                current_opening = self.find_geometry(
                    opening["globalId"]
                )

                self.assertEqual(
                    current_opening["operation"],
                    "SUBTRACT"
                )

                self.assertEqual(
                    current_opening["depth"],
                    3
                )

                self.assertEqual(
                    current_opening["profile"],
                    rectangular_profile(0.5, 0.5)
                )

                current_concrete = self.find_material(
                    "Simulation concrete"
                )

                current_wood = self.find_material(
                    "Simulation wood"
                )

                self.assert_material_groups(
                    physical_after,
                    [
                        current_concrete["id"],
                        current_wood["id"]
                    ]
                )

                self.assertEqual(domain_after["groups"], [])

                content = path.read_text(encoding="utf-8")

                self.assertIn("IFCSPATIALZONE", content.upper())
                self.assertIn("ifc-cli:domain", content)
                self.assertIn("ifc-cli:domain-parent", content)
                self.assertIn("IFCRELVOIDSELEMENT", content.upper())

                self.assert_index_consistency()

        # Verify that simulation data remains editable after
        # the repeated persistence cycle.

        first_obstacle = self.find_transform(
            first_obstacle["globalId"]
        )

        self.delete_geometry(
            self.find_geometry(opening["globalId"])["id"]
        )

        self.assert_volume(
            self.physical_mesh(first_obstacle["id"]),
            1.0
        )

        restored_occupancy = voxelize(
            origin,
            dimensions,
            spacing,
            [
                self.domain_mesh(
                    room["domain"],
                    space="world"
                )
            ],
            self.room_physical_meshes(
                room,
                space="world"
            )
        )

        restored_counts = Counter(
            restored_occupancy.values()
        )

        self.assertEqual(
            restored_counts,
            {
                OUTSIDE: 1696,
                FREE: 848,
                SOLID: 16
            }
        )

        changed_voxels = {
            index
            for index in reference_occupancy
            if reference_occupancy[index] != restored_occupancy[index]
        }

        self.assertEqual(len(changed_voxels), 2)

        for index in changed_voxels:
            self.assertEqual(
                reference_occupancy[index],
                FREE
            )

            self.assertEqual(
                restored_occupancy[index],
                SOLID
            )

        self.assert_index_consistency()


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