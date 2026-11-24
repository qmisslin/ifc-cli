import json
import math
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import unittest

from pathlib import Path


# Standalone integration suite using only the Python standard library.
#
# Run:
# python3 tests/test_robustness.py ./build/ifc-cli
#
# Not executed against the compiled CLI.
#
# Transactional tests intentionally require failed operations to leave
# both session indexes and serialized IFC entities unchanged.
#
# These tests may expose existing implementation defects:
# - Orphan entities after failed physical geometry creation.
# - Incomplete cleanup after failed transform or material creation.
# - Partial mutations during multi-field updates.
# - Incomplete rollback after recursive duplication failures.
#
# Such failures must not be treated as expected successes.

EXECUTABLE = Path("./build/ifc-cli").resolve()

REQUEST_TIMEOUT = 60
PROCESS_TIMEOUT = 10

ROUNDTRIP_COUNT = 50
LONG_SESSION_REQUEST_COUNT = 5000
LONG_REQUEST_SIZE = 262144
FUZZ_REQUEST_COUNT = 1000
RANDOM_EDIT_COUNT = 300

FUZZ_SEED = 20260101
EDIT_SEED = 20260102

ROOT_GLOBAL_ID = "0000000000000000000001"
PROJECT_GLOBAL_ID = "0000000000000000000002"
STOREY_GLOBAL_ID = "0000000000000000000003"
INVALID_DOMAIN_GLOBAL_ID = "0000000000000000000004"
GRID_CHILD_GLOBAL_ID = "0000000000000000000005"

IDENTITY_MATRIX = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0
]

DEFAULT_PROFILE = [
    [0.0, 0.0],
    [2.0, 0.0],
    [2.0, 3.0],
    [0.0, 3.0]
]

DEFAULT_VISUAL = {
    "color": [0.4, 0.6, 0.8],
    "opacity": 1.0,
    "metallic": 0.2,
    "roughness": 0.5
}

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
#15=IFCBUILDINGELEMENTPROXY('0000000000000000000001',$,'Physical product',$,$,#5,#14,$,.NOTDEFINED.);
#16=IFCPROJECT('0000000000000000000002',$,'Robustness project',$,$,$,$,(#6),#8);
#17=IFCLOCALPLACEMENT($,#4);
#18=IFCBUILDINGSTOREY('0000000000000000000003',$,'Robustness storey',$,$,#17,$,$,.ELEMENT.,0.);
#19=IFCRELCONTAINEDINSPATIALSTRUCTURE('0000000000000000000010',$,$,$,(#15),#18);
#20=IFCRELAGGREGATES('0000000000000000000011',$,$,$,#16,(#18));
"""

# A managed domain with a valid parent but no geometric representation.
# It can be indexed, but domain tessellation must fail explicitly.

INVALID_DOMAIN_ENTITIES = """
#30=IFCLOCALPLACEMENT(#5,#4);
#31=IFCSPATIALZONE('0000000000000000000004',$,'Non-tessellable domain',$,'ifc-cli:domain',#30,$,$,.USERDEFINED.);
#32=IFCRELASSIGNSTOPRODUCT('0000000000000000000012',$,'ifc-cli:domain-parent',$,(#31),$,#15);
"""

# A valid alternative placement that cannot be duplicated through the
# simplified API. The unsupported child is reached after the root copy
# and the first child copy have already been created.

DUPLICATION_FAILURE_ENTITIES = """
#40=IFCCARTESIANPOINT((-10.,0.));
#41=IFCCARTESIANPOINT((10.,0.));
#42=IFCPOLYLINE((#40,#41));
#43=IFCGRIDAXIS('U1',#42,.T.);
#44=IFCCARTESIANPOINT((0.,-10.));
#45=IFCCARTESIANPOINT((0.,10.));
#46=IFCPOLYLINE((#44,#45));
#47=IFCGRIDAXIS('V1',#46,.T.);
#48=IFCCARTESIANPOINT((-10.,5.));
#49=IFCCARTESIANPOINT((10.,5.));
#50=IFCPOLYLINE((#48,#49));
#51=IFCGRIDAXIS('U2',#50,.T.);
#52=IFCCARTESIANPOINT((5.,-10.));
#53=IFCCARTESIANPOINT((5.,10.));
#54=IFCPOLYLINE((#52,#53));
#55=IFCGRIDAXIS('V2',#54,.T.);
#56=IFCLOCALPLACEMENT($,#4);
#57=IFCGRID('0000000000000000000013',$,'Native grid',$,$,#56,$,(#43,#51),(#47,#55),$,.RECTANGULAR.);
#58=IFCVIRTUALGRIDINTERSECTION((#43,#47),(0.,0.));
#59=IFCGRIDPLACEMENT(#58,$);
#60=IFCLOCALPLACEMENT(#5,#4);
#61=IFCBUILDINGELEMENTPROXY('0000000000000000000014',$,'Duplicable child',$,$,#60,$,$,.NOTDEFINED.);
#62=IFCBUILDINGELEMENTPROXY('0000000000000000000005',$,'Unsupported grid child',$,$,#59,$,$,.NOTDEFINED.);
#63=IFCRELAGGREGATES('0000000000000000000015',$,$,$,#15,(#61,#62));
"""


def build_fixture(extra_entities=""):
    return f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('robustness-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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


def invalid_scaling_matrix():
    matrix = IDENTITY_MATRIX.copy()
    matrix[0] = 2.0
    return matrix


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
    unit=None,
    source="occurrence",
    property_set="Pset_Robustness"
):
    return {
        "propertySet": property_set,
        "name": name,
        "valueType": value_type,
        "value": value,
        "unit": unit,
        "source": source
    }


def rectangular_profile(width, length):
    return [
        [0.0, 0.0],
        [width, 0.0],
        [width, length],
        [0.0, length]
    ]


def sorted_properties(properties):
    return sorted(
        properties,
        key=lambda item: (
            item["source"],
            item["propertySet"],
            item["name"]
        )
    )


def parse_step_entities(content):
    # Targeted parser for the controlled integration fixtures.
    # STEP strings are preserved, including escaped apostrophes.

    pattern = re.compile(
        r"#(\d+)\s*=\s*(IFC[A-Z0-9_]+)\s*"
        r"\(((?:'(?:[^']|'')*'|[^';])*)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL
    )

    entities = {}

    for match in pattern.finditer(content):
        identifier = int(match.group(1))

        if identifier in entities:
            raise AssertionError(f"Duplicate STEP identifier #{identifier}")

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

    return {
        int(identifier)
        for identifier in re.findall(r"#(\d+)", without_strings)
    }


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
            points[index]
            for index in indices[offset:offset + 3]
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
            cross(
                subtract(b, origin),
                subtract(c, origin)
            )
        )
        for a, b, c in mesh_triangles(mesh)
    )) / 6.0


def mesh_bounds(mesh):
    points = mesh_points(mesh)

    if not points:
        return None

    return tuple(
        operation(point[axis] for point in points)
        for operation in (min, max)
        for axis in range(3)
    )


def mesh_signature(mesh):
    return sorted(
        tuple(sorted(
            tuple(round(value, 6) for value in point)
            for point in triangle
        ))
        for triangle in mesh_triangles(mesh)
    )


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

    def write_raw(self, line):
        if self.process.poll() is not None:
            raise RuntimeError(
                f"CLI terminated with code {self.process.returncode}"
            )

        if not line.endswith("\n"):
            line += "\n"

        self.process.stdin.write(line)
        self.process.stdin.flush()

    def send(self, command, params=None):
        self.counter += 1
        request_id = f"request-{self.counter}"

        request = {
            "id": request_id,
            "command": command,
            "params": {} if params is None else params
        }

        self.write_raw(
            json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":")
            )
        )

        return request_id

    def read(self, request_id):
        try:
            item = self.responses.get(timeout=REQUEST_TIMEOUT)
        except queue.Empty as error:
            raise TimeoutError(
                f"No CLI response for request {request_id}"
            ) from error

        if item is None:
            raise RuntimeError(
                f"CLI terminated before responding to {request_id}"
            )

        if isinstance(item, Exception):
            raise RuntimeError("Could not read CLI stdout") from item

        try:
            response = json.loads(item)
        except json.JSONDecodeError as error:
            raise AssertionError(
                f"Invalid JSONL response: {item[:500]!r}"
            ) from error

        if not isinstance(response, dict):
            raise AssertionError(response)

        if response.get("id") != request_id:
            raise AssertionError(
                f"Expected response ID {request_id!r}, "
                f"received {response.get('id')!r}"
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
            error = response["error"]

            if set(error) != {"code", "message", "details"}:
                raise AssertionError(response)

            if not isinstance(error["code"], str) or not error["code"]:
                raise AssertionError(response)

            if not isinstance(error["message"], str) or not error["message"]:
                raise AssertionError(response)

            if not isinstance(error["details"], dict):
                raise AssertionError(response)

        return response

    def request(self, command, params=None):
        response = self.read(self.send(command, params))

        if not response["ok"]:
            raise RuntimeError(f"{command}: {response['error']}")

        return response["result"]

    def expect_error(self, command, params, expected_code):
        response = self.read(self.send(command, params))

        return self.assert_error(response, expected_code)

    def expect_raw_error(self, line, expected_code, request_id=None):
        self.write_raw(line)
        response = self.read(request_id)

        return self.assert_error(response, expected_code)

    @staticmethod
    def assert_error(response, expected_code):
        if response["ok"]:
            raise AssertionError(
                f"Request unexpectedly succeeded: {response}"
            )

        if response["error"]["code"] != expected_code:
            raise AssertionError(
                f"Expected {expected_code}, received {response['error']}"
            )

        return response["error"]

    @staticmethod
    def tessellation_parameters(
        transform_id,
        kind="SOLID",
        geometry_id=None,
        include_children=False,
        space="local"
    ):
        options = {
            "kind": kind,
            "space": space,
            "includeChildren": include_children,
            "includeNormals": False,
            "includeMaterials": False
        }

        if geometry_id is not None:
            options["geometryId"] = geometry_id

        return {
            "id": transform_id,
            "options": options
        }

    def tessellate(self, transform_id, **options):
        request_id = self.send(
            "transformTessellate",
            self.tessellation_parameters(transform_id, **options)
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

            key = name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(f"Invalid mesh offset: {event}")

            mesh[key].extend(event["values"])

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        for field in ("vertexCount", "indexCount"):
            if type(mesh[field]) is not int or mesh[field] < 0:
                raise AssertionError(f"Invalid mesh count: {begin}")

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if mesh["indexCount"] % 3:
            raise AssertionError("Invalid triangle indices")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(f"Invalid vertex index: {index}")

        for value in mesh["vertices"]:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise AssertionError(f"Invalid mesh coordinate: {value}")

        return mesh

    def assert_alive(self):
        if self.process.poll() is not None:
            raise AssertionError(
                f"CLI is no longer running: {self.process.returncode}"
            )

    def close(self):
        try:
            if self.process.poll() is None:
                result = self.request("exit")

                if result != {}:
                    raise AssertionError(result)

                self.process.wait(timeout=PROCESS_TIMEOUT)

            if self.process.returncode != 0:
                raise AssertionError(
                    f"CLI exited with code {self.process.returncode}"
                )

        finally:
            self.terminate()

    def terminate(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=PROCESS_TIMEOUT)

        if not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass

        self.reader.join(timeout=PROCESS_TIMEOUT)

        if not self.process.stdout.closed:
            self.process.stdout.close()


class RobustnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-robustness-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)
        self.input_path = self.root_path / "input.ifc"

        self.save_counter = 0

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.process_id = self.client.process.pid
        self.capabilities = self.client.request("getCapabilities")

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

        self.assert_process_alive()

        return result

    def save_model(self, path=None):
        if path is None:
            self.save_counter += 1
            path = self.root_path / f"snapshot-{self.save_counter}.ifc"

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

        self.assert_step_integrity(
            parse_step_entities(
                path.read_text(encoding="utf-8")
            )
        )

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
    def root_id(self):
        return self.find_transform(ROOT_GLOBAL_ID)["id"]

    @property
    def project_id(self):
        return self.find_transform(PROJECT_GLOBAL_ID)["id"]

    @property
    def storey_id(self):
        return self.find_transform(STOREY_GLOBAL_ID)["id"]

    def transform_parameters(
        self,
        name="Robustness transform",
        parents=None,
        matrix=None,
        properties=None
    ):
        return {
            "name": name,
            "parents": parents_input() if parents is None else parents,
            "transform": transform_input(matrix),
            "properties": [] if properties is None else properties
        }

    def geometry_parameters(
        self,
        name="Robustness geometry",
        parent_id=None,
        kind="DOMAIN",
        operation="ADD",
        profile=None,
        depth=2.0,
        matrix=None
    ):
        return {
            "parentId": self.root_id if parent_id is None else parent_id,
            "name": name,
            "kind": kind,
            "operation": operation,
            "profile": DEFAULT_PROFILE if profile is None else profile,
            "depth": depth,
            "transform": transform_input(matrix)
        }

    def material_parameters(
        self,
        name="Robustness material",
        category="concrete",
        visual=None,
        properties=None
    ):
        return {
            "name": name,
            "category": category,
            "visual": DEFAULT_VISUAL if visual is None else visual,
            "properties": [] if properties is None else properties
        }

    def create_transform(self, **options):
        return self.client.request(
            "transformCreate",
            self.transform_parameters(**options)
        )

    def create_geometry(self, **options):
        return self.client.request(
            "geometryCreate",
            self.geometry_parameters(**options)
        )

    def create_material(self, **options):
        return self.client.request(
            "materialCreate",
            self.material_parameters(**options)
        )

    def update_transform(self, identifier, **changes):
        return self.client.request(
            "transformUpdate",
            {"id": identifier, **changes}
        )

    def update_geometry(self, identifier, **changes):
        return self.client.request(
            "geometryUpdate",
            {"id": identifier, **changes}
        )

    def update_material(self, identifier, **changes):
        return self.client.request(
            "materialUpdate",
            {"id": identifier, **changes}
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

    def physical_mesh(self):
        return self.client.tessellate(
            self.root_id,
            kind="SOLID"
        )

    def domain_mesh(self, geometry):
        return self.client.tessellate(
            geometry["parentId"],
            kind="DOMAIN",
            geometry_id=geometry["id"]
        )

    def snapshot_indexes(self):
        return {
            "transforms": self.list_transforms(),
            "geometries": self.list_geometries(),
            "materials": self.list_materials()
        }

    def snapshot_model(self):
        return {
            "transforms": {
                item["id"]: self.get_transform(item["id"])
                for item in self.list_transforms()
            },
            "geometries": {
                item["id"]: self.get_geometry(item["id"])
                for item in self.list_geometries()
            },
            "materials": {
                item["id"]: self.get_material(item["id"])
                for item in self.list_materials()
            }
        }

    def snapshot_transaction(self):
        return {
            "model": self.snapshot_model(),
            "entities": self.save_entities()
        }

    def persistent_snapshot(self):
        # Session IDs and STEP identifiers are not persistent identities.

        model = self.snapshot_model()

        transforms = model["transforms"]
        geometries = model["geometries"]
        materials = model["materials"]

        persistent_transforms = {}

        for item in transforms.values():
            persistent_transforms[item["globalId"]] = {
                "name": item["name"],
                "class": item["definition"]["ifcClass"],
                "parents": {
                    key: (
                        transforms[value]["globalId"]
                        if value is not None
                        else None
                    )
                    for key, value in item["parents"].items()
                },
                "transforms": item["transforms"],
                "properties": sorted_properties(item["properties"]),
                "materials": sorted(
                    materials[identifier]["name"]
                    for identifier in item["materials"]
                )
            }

        persistent_geometries = []

        for item in geometries.values():
            geometry = {
                "globalId": item["globalId"],
                "parentGlobalId": transforms[item["parentId"]]["globalId"],
                "source": item["source"],
                "kind": item["kind"],
                "operation": item["operation"],
                "editable": item["editable"],
                "ifcClass": item["ifcClass"]
            }

            if item["source"] == "editor":
                geometry.update({
                    "name": item["name"],
                    "profile": item["profile"],
                    "depth": item["depth"],
                    "transforms": item["transforms"],
                    "materials": sorted(
                        materials[identifier]["name"]
                        for identifier in item["materials"]
                    )
                })

            persistent_geometries.append(geometry)

        persistent_materials = [
            {
                "name": item["name"],
                "category": item["category"],
                "visual": item["visual"],
                "editable": item["editable"],
                "properties": sorted_properties(item["properties"]),
                "ifcClass": item["definition"]["ifcClass"]
            }
            for item in materials.values()
        ]

        return {
            "transforms": persistent_transforms,
            "geometries": sorted(
                persistent_geometries,
                key=lambda item: json.dumps(item, sort_keys=True)
            ),
            "materials": sorted(
                persistent_materials,
                key=lambda item: json.dumps(item, sort_keys=True)
            )
        }

    def assert_process_alive(self):
        self.client.assert_alive()

        self.assertEqual(
            self.client.process.pid,
            self.process_id,
            "The persistent CLI process was replaced"
        )

        self.assertEqual(
            self.client.request("getCapabilities"),
            self.capabilities
        )

    def assert_step_integrity(self, entities):
        global_ids = []

        for identifier, entity in entities.items():
            references = step_references(entity["arguments"])

            missing = references - set(entities)

            self.assertFalse(
                missing,
                f"Dangling references from #{identifier}: {sorted(missing)}"
            )

            match = re.match(
                r"\s*'([0-3][0-9A-Za-z_$]{21})'\s*,",
                entity["arguments"]
            )

            if match is not None:
                global_ids.append(match.group(1))

            if entity["class"] in {
                "IFCRELAGGREGATES",
                "IFCRELNESTS",
                "IFCRELCONTAINEDINSPATIALSTRUCTURE",
                "IFCRELDEFINESBYPROPERTIES",
                "IFCRELASSOCIATESMATERIAL",
                "IFCRELASSIGNSTOPRODUCT"
            }:
                self.assertNotRegex(
                    entity["arguments"],
                    r",\s*\(\s*\)\s*[,)]",
                    f"Empty relation aggregate in #{identifier}"
                )

        self.assertEqual(
            len(global_ids),
            len(set(global_ids)),
            "Duplicate IFC GlobalIds"
        )

    def assert_index_consistency(self):
        model = self.snapshot_model()

        transforms = model["transforms"]
        geometries = model["geometries"]
        materials = model["materials"]

        expected_geometry_links = {
            identifier: set()
            for identifier in transforms
        }

        for identifier, geometry in geometries.items():
            self.assertEqual(geometry["id"], identifier)

            parent_id = geometry["parentId"]

            self.assertIn(parent_id, transforms)

            expected_geometry_links[parent_id].add(identifier)

            if "materials" in geometry:
                self.assertEqual(
                    len(geometry["materials"]),
                    len(set(geometry["materials"]))
                )

                self.assertTrue(
                    set(geometry["materials"]).issubset(materials)
                )

            if geometry["kind"] == "DOMAIN" and geometry["source"] == "editor":
                self.assertEqual(geometry["operation"], "ADD")
                self.assertEqual(geometry["materials"], [])

        for identifier, transform in transforms.items():
            self.assertEqual(transform["id"], identifier)

            self.assertEqual(
                set(transform["geometries"]),
                expected_geometry_links[identifier]
            )

            self.assertEqual(
                len(transform["geometries"]),
                len(set(transform["geometries"]))
            )

            self.assertTrue(
                set(transform["materials"]).issubset(materials)
            )

            self.assertEqual(
                len(transform["materials"]),
                len(set(transform["materials"]))
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

        identifiers = [
            entity["id"]
            for entity in all_entities
        ]

        step_ids = [
            entity["ifcEntityId"]
            for entity in all_entities
        ]

        global_ids = [
            entity["globalId"]
            for entity in all_entities
            if entity["globalId"] is not None
        ]

        self.assertEqual(
            len(identifiers),
            len(set(identifiers))
        )

        self.assertEqual(
            len(step_ids),
            len(set(step_ids))
        )

        self.assertEqual(
            len(global_ids),
            len(set(global_ids))
        )

        self.assertEqual(
            set(self.list_geometry_ids(kind="ALL")),
            set(self.list_geometry_ids(kind="SOLID"))
            | set(self.list_geometry_ids(kind="DOMAIN"))
        )

        self.assertFalse(
            set(self.list_geometry_ids(kind="SOLID"))
            & set(self.list_geometry_ids(kind="DOMAIN"))
        )

    def list_geometry_ids(self, kind="ALL"):
        return [
            item["id"]
            for item in self.list_geometries(kind=kind)
        ]

    def assert_transaction_unchanged(self, before):
        # Capture all checks before reporting failure so orphan entities
        # remain visible even when the session indexes also changed.

        after_model = self.snapshot_model()
        after_entities = self.save_entities()

        with self.subTest(invariant="session state"):
            self.assertEqual(
                after_model,
                before["model"],
                "Failed operation modified the indexed model"
            )

        with self.subTest(invariant="IFC entities"):
            added = set(after_entities) - set(before["entities"])
            removed = set(before["entities"]) - set(after_entities)

            modified = {
                identifier
                for identifier in set(before["entities"]) & set(after_entities)
                if before["entities"][identifier] != after_entities[identifier]
            }

            self.assertFalse(
                added or removed or modified,
                {
                    "added": {
                        identifier: after_entities[identifier]["class"]
                        for identifier in sorted(added)
                    },
                    "removed": {
                        identifier: before["entities"][identifier]["class"]
                        for identifier in sorted(removed)
                    },
                    "modified": sorted(modified)
                }
            )

        self.assert_process_alive()
        self.assert_index_consistency()

    def assert_failed_transaction(self, command, params, expected_code):
        before = self.snapshot_transaction()

        self.client.expect_error(
            command,
            params,
            expected_code
        )

        self.assert_transaction_unchanged(before)

    def assert_mesh_equivalent(self, first, second):
        self.assertEqual(
            mesh_signature(first),
            mesh_signature(second)
        )

        self.assertEqual(
            first["indexCount"],
            second["indexCount"]
        )

        self.assertAlmostEqual(
            mesh_volume(first),
            mesh_volume(second),
            delta=1e-6
        )

    def assert_volume(self, mesh, expected):
        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected,
            delta=1e-6
        )

    def assert_baseline_operational(self):
        self.assert_process_alive()

        root = self.find_transform(ROOT_GLOBAL_ID)

        self.assertEqual(
            root["name"],
            "Physical product"
        )

        self.assert_volume(
            self.physical_mesh(),
            64.0
        )

        self.assert_index_consistency()

    def test_recovery_after_invalid_json(self):
        """Verify recovery after malformed JSON and invalid JSON roots."""

        before = self.snapshot_model()
        baseline_mesh = self.physical_mesh()

        invalid_requests = [
            "{",
            "}",
            "{invalid-json}",
            '{"id":"broken","command":"help","params":',
            '{"id":"broken","command":"help","params":{}} trailing',
            '{"id":"broken","command":"help","params":{},}',
            '{"id":"broken","command":"help","params":NaN}',
            '{"id":"broken","command":"help","params":Infinity}',
            '{"id":"broken","command":"help","params":-Infinity}',
            '{"id":"broken","command":"help","params":1e999}',
            '{"id":"\\q","command":"help","params":{}}',
            "null",
            "true",
            "false",
            "42",
            '"text"',
            "[]",
            "[1,2,3]"
        ]

        for index, line in enumerate(invalid_requests):
            with self.subTest(case=index, request=line):
                self.client.expect_raw_error(
                    line,
                    "INVALID_JSON"
                )

                self.assert_process_alive()

                self.assertEqual(
                    self.get_transform(self.root_id)["name"],
                    "Physical product"
                )

        # Verify several malformed and valid requests submitted
        # without waiting for each intermediate response.

        self.client.write_raw("{")
        self.client.write_raw("[]")

        valid_id = self.client.send("getCapabilities")

        self.client.assert_error(
            self.client.read(None),
            "INVALID_JSON"
        )

        self.client.assert_error(
            self.client.read(None),
            "INVALID_JSON"
        )

        valid_response = self.client.read(valid_id)

        self.assertIs(valid_response["ok"], True)
        self.assertEqual(valid_response["result"], self.capabilities)

        self.assertEqual(
            self.snapshot_model(),
            before
        )

        self.assert_mesh_equivalent(
            baseline_mesh,
            self.physical_mesh()
        )

    def test_recovery_after_invalid_parameters(self):
        """Verify recovery after protocol and command parameter errors."""

        before = self.snapshot_transaction()

        cases = [
            ("load", {}, "INVALID_PARAMETERS"),
            ("save", {"path": 123}, "INVALID_PARAMETERS"),
            ("transformGet", {}, "INVALID_PARAMETERS"),
            ("transformGet", {"id": None}, "INVALID_PARAMETERS"),
            ("transformGet", {"id": ""}, "INVALID_PARAMETERS"),
            ("transformGet", {"id": 42}, "INVALID_PARAMETERS"),
            ("transformGet", {"id": "invalid:42"}, "INVALID_PARAMETERS"),
            ("transformGet", {"id": "geometry:1"}, "INVALID_ENTITY_TYPE"),
            ("transformGet", {"id": "transform:999999999"}, "ENTITY_NOT_FOUND"),
            ("transformGetAll", {"unexpected": True}, "INVALID_PARAMETERS"),
            ("transformUpdate", {"id": self.root_id}, "INVALID_PARAMETERS"),
            (
                "transformUpdate",
                {"id": self.root_id, "name": ["invalid"]},
                "INVALID_PARAMETERS"
            ),
            (
                "geometryGetAll",
                {"transformId": None, "kind": "UNKNOWN"},
                "INVALID_PARAMETERS"
            ),
            ("geometryCreate", {}, "INVALID_PARAMETERS"),
            ("materialCreate", {}, "INVALID_PARAMETERS"),
            ("materialGet", {"id": self.root_id}, "INVALID_ENTITY_TYPE"),
            ("unknownCommand", {}, "UNKNOWN_COMMAND")
        ]

        for command, params, expected_code in cases:
            with self.subTest(command=command, params=params):
                self.client.expect_error(
                    command,
                    params,
                    expected_code
                )

                self.assert_process_alive()

                self.assertEqual(
                    self.get_transform(self.root_id)["name"],
                    "Physical product"
                )

        raw_cases = [
            (
                '{"id":"missing","command":"help"}',
                "missing"
            ),
            (
                '{"id":"extra","command":"help","params":{},"extra":true}',
                "extra"
            ),
            (
                '{"id":null,"command":"help","params":{}}',
                None
            ),
            (
                '{"id":"","command":"help","params":{}}',
                None
            ),
            (
                '{"id":"params","command":"help","params":[]}',
                "params"
            )
        ]

        for line, request_id in raw_cases:
            with self.subTest(raw=line):
                self.client.expect_raw_error(
                    line,
                    "INVALID_PARAMETERS",
                    request_id=request_id
                )

                self.assert_process_alive()

        self.assert_transaction_unchanged(before)

    def test_recovery_after_failed_load(self):
        """Verify that a failed load does not prevent subsequent loading."""

        missing = self.root_path / "missing.ifc"
        empty = self.root_path / "empty.ifc"
        corrupted = self.root_path / "corrupted.ifc"
        unsupported = self.root_path / "unsupported.ifc"

        empty.write_text("", encoding="utf-8")

        corrupted.write_text(
            "This content is not an IFC file.",
            encoding="utf-8"
        )

        unsupported.write_text(
            build_fixture().replace(
                "FILE_SCHEMA(('IFC4'));",
                "FILE_SCHEMA(('IFC999'));"
            ),
            encoding="utf-8"
        )

        cases = [
            (missing, "FILE_NOT_FOUND"),
            (self.root_path, "FILE_NOT_FOUND"),
            (empty, "IFC_PARSE_ERROR"),
            (corrupted, "IFC_PARSE_ERROR"),
            (unsupported, "UNSUPPORTED_SCHEMA")
        ]

        for path, expected_code in cases:
            with self.subTest(path=path.name):
                self.create_geometry(
                    name="Unsaved domain before failed load"
                )

                self.client.expect_error(
                    "load",
                    {"path": str(path)},
                    expected_code
                )

                self.assert_process_alive()

                # Failed loads clear the previously loaded model.

                for command, params in (
                    ("transformGetAll", {}),
                    ("geometryGetAll", {"transformId": None}),
                    ("materialGetAll", {}),
                    ("save", {"path": str(self.root_path / "invalid.ifc")})
                ):
                    self.client.expect_error(
                        command,
                        params,
                        "NO_MODEL_LOADED"
                    )

                self.load_fixture()

                self.assert_baseline_operational()

                self.assertEqual(
                    self.list_geometries(kind="DOMAIN"),
                    []
                )

                recovered = self.create_geometry(
                    name="Domain after failed load"
                )

                self.assert_volume(
                    self.domain_mesh(recovered),
                    12.0
                )

                self.delete_geometry(recovered["id"])

    def test_recovery_after_failed_save(self):
        """Verify that failed saves do not alter the loaded model."""

        domain = self.create_geometry(
            name="Domain preserved after failed save",
            depth=5.0
        )

        material = self.create_material(
            name="Material preserved after failed save"
        )

        self.assign_material(
            material["id"],
            self.root_id
        )

        before = self.snapshot_transaction()
        domain_before = self.domain_mesh(domain)
        physical_before = self.physical_mesh()

        blocker = self.root_path / "not-a-directory"
        blocker.write_text("Existing file", encoding="utf-8")

        invalid_paths = [
            self.root_path,
            self.root_path / "missing-directory" / "output.ifc",
            blocker / "output.ifc"
        ]

        for path in invalid_paths:
            with self.subTest(path=str(path)):
                error = self.client.expect_error(
                    "save",
                    {"path": str(path)},
                    "IFC_WRITE_ERROR"
                )

                self.assertEqual(
                    error["details"]["path"],
                    str(path)
                )

                self.assert_transaction_unchanged(before)

                self.assert_mesh_equivalent(
                    domain_before,
                    self.domain_mesh(domain)
                )

                self.assert_mesh_equivalent(
                    physical_before,
                    self.physical_mesh()
                )

        self.assertEqual(
            blocker.read_text(encoding="utf-8"),
            "Existing file"
        )

        expected = self.persistent_snapshot()

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

        current = self.find_geometry(domain["globalId"])

        self.assert_volume(
            self.domain_mesh(current),
            30.0
        )

    def test_recovery_after_failed_tessellation(self):
        """Verify recovery after an actual geometry processing failure."""

        self.load_fixture(INVALID_DOMAIN_ENTITIES)

        matches = [
            geometry
            for geometry in self.list_geometries(kind="DOMAIN")
            if geometry["globalId"] == INVALID_DOMAIN_GLOBAL_ID
        ]

        self.assertEqual(len(matches), 1)

        invalid_domain = matches[0]

        healthy_domain = self.create_geometry(
            name="Valid domain",
            depth=3.0,
            matrix=translation_matrix(20, 0, 0)
        )

        baseline_indexes = self.snapshot_indexes()
        baseline_physical = self.physical_mesh()
        baseline_domain = self.domain_mesh(healthy_domain)

        self.assert_volume(baseline_physical, 64.0)
        self.assert_volume(baseline_domain, 18.0)

        cases = [
            self.client.tessellation_parameters(
                self.root_id,
                kind="DOMAIN",
                geometry_id=invalid_domain["id"]
            ),
            self.client.tessellation_parameters(
                self.root_id,
                kind="DOMAIN"
            )
        ]

        for iteration in range(5):
            for params in cases:
                with self.subTest(iteration=iteration, params=params):
                    self.client.expect_error(
                        "transformTessellate",
                        params,
                        "TESSELLATION_FAILED"
                    )

                    self.assert_process_alive()

                    self.assertEqual(
                        self.snapshot_indexes(),
                        baseline_indexes
                    )

                    self.assert_mesh_equivalent(
                        baseline_physical,
                        self.physical_mesh()
                    )

                    self.assert_mesh_equivalent(
                        baseline_domain,
                        self.domain_mesh(healthy_domain)
                    )

        self.delete_geometry(invalid_domain["id"])

        recovered = self.client.tessellate(
            self.root_id,
            kind="DOMAIN"
        )

        self.assert_mesh_equivalent(
            baseline_domain,
            recovered
        )

        self.save_and_reload()

        current = self.find_geometry(
            healthy_domain["globalId"]
        )

        self.assert_mesh_equivalent(
            baseline_domain,
            self.domain_mesh(current)
        )

        self.assert_index_consistency()

    def test_failed_geometry_creation(self):
        """Verify cleanup after invalid geometry creation."""

        cases = [
            (
                {
                    "profile": [[0, 0], [1, 1]]
                },
                "INVALID_PARAMETERS"
            ),
            (
                {
                    "profile": [[0, 0], [1, 0], [2, 0]]
                },
                "INVALID_PROFILE"
            ),
            (
                {
                    "profile": [
                        [0, 0],
                        [4, 0],
                        [0, 3],
                        [4, 3]
                    ]
                },
                "INVALID_PROFILE"
            ),
            (
                {"depth": 0.0},
                "INVALID_PARAMETERS"
            ),
            (
                {"depth": -1.0},
                "INVALID_PARAMETERS"
            ),
            (
                {"matrix": invalid_scaling_matrix()},
                "INVALID_PLACEMENT"
            ),
            (
                {"kind": "UNKNOWN"},
                "INVALID_PARAMETERS"
            ),
            (
                {"kind": "DOMAIN", "operation": "SUBTRACT"},
                "INVALID_PARAMETERS"
            ),
            (
                {"parent_id": "transform:999999999"},
                "ENTITY_NOT_FOUND"
            ),
            (
                {"parent_id": self.project_id},
                "INVALID_RELATION"
            ),
            (
                {
                    "parent_id": self.storey_id,
                    "kind": "SOLID",
                    "operation": "ADD"
                },
                "GEOMETRY_OPERATION_NOT_SUPPORTED"
            ),
            (
                {
                    "parent_id": self.storey_id,
                    "kind": "SOLID",
                    "operation": "SUBTRACT"
                },
                "GEOMETRY_OPERATION_NOT_SUPPORTED"
            )
        ]

        for index, (options, expected_code) in enumerate(cases):
            with self.subTest(case=index, options=options):
                self.load_fixture()

                params = self.geometry_parameters(
                    name=f"Rejected geometry {index}",
                    **options
                )

                self.assert_failed_transaction(
                    "geometryCreate",
                    params,
                    expected_code
                )

        self.load_fixture()

        recovered = self.create_geometry(
            name="Recovered geometry",
            depth=4.0
        )

        self.assert_volume(
            self.domain_mesh(recovered),
            24.0
        )

        self.assert_index_consistency()

    def test_failed_transform_creation(self):
        """Verify that failed transform creation leaves no IFC relations."""

        invalid_property = property_input(
            "InvalidValue",
            "value",
            value_type="IfcNonexistentValue"
        )

        invalid_unit_property = property_input(
            "InvalidUnit",
            10.0,
            value_type="IfcLengthMeasure",
            unit="NONEXISTENT_UNIT"
        )

        cases = [
            (
                {
                    "parents": parents_input(
                        spatial=self.root_id
                    )
                },
                "INVALID_RELATION"
            ),
            (
                {
                    "parents": parents_input(
                        placement=self.project_id
                    )
                },
                "INVALID_RELATION"
            ),
            (
                {
                    "parents": parents_input(
                        decomposition="transform:999999999"
                    )
                },
                "ENTITY_NOT_FOUND"
            ),
            (
                {"matrix": invalid_scaling_matrix()},
                "INVALID_PLACEMENT"
            ),
            (
                {
                    "parents": parents_input(
                        spatial=self.storey_id,
                        placement=self.root_id,
                        decomposition=self.root_id
                    ),
                    "properties": [invalid_property]
                },
                "INVALID_PARAMETERS"
            ),
            (
                {
                    "parents": parents_input(
                        spatial=self.storey_id,
                        placement=self.root_id
                    ),
                    "properties": [
                        property_input(
                            "AValidProperty",
                            "Already created"
                        ),
                        invalid_unit_property
                    ]
                },
                "INVALID_PARAMETERS"
            )
        ]

        for index, (options, expected_code) in enumerate(cases):
            with self.subTest(case=index, options=options):
                self.load_fixture()

                params = self.transform_parameters(
                    name=f"Rejected transform {index}",
                    **options
                )

                self.assert_failed_transaction(
                    "transformCreate",
                    params,
                    expected_code
                )

        self.load_fixture()

        recovered = self.create_transform(
            name="Recovered transform",
            parents=parents_input(
                spatial=self.storey_id,
                placement=self.root_id,
                decomposition=self.root_id
            ),
            properties=[
                property_input("Reference", "RECOVERED")
            ]
        )

        self.assertEqual(
            recovered["parents"],
            parents_input(
                spatial=self.storey_id,
                placement=self.root_id,
                decomposition=self.root_id
            )
        )

        self.assert_index_consistency()

        expected = self.persistent_snapshot()

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

    def test_failed_material_creation(self):
        """Verify cleanup after invalid material creation."""

        cases = [
            {
                "visual": {
                    **DEFAULT_VISUAL,
                    "opacity": -0.1
                }
            },
            {
                "visual": {
                    **DEFAULT_VISUAL,
                    "metallic": 1.1
                }
            },
            {
                "visual": {
                    **DEFAULT_VISUAL,
                    "roughness": 2.0
                }
            },
            {
                "visual": {
                    **DEFAULT_VISUAL,
                    "color": [1.0, 0.5]
                }
            },
            {
                "visual": {
                    **DEFAULT_VISUAL,
                    "color": [1.0, -0.5, 0.5]
                }
            },
            {
                "properties": [
                    property_input(
                        "InvalidMaterialValue",
                        10.0,
                        value_type="IfcNonexistentMeasure",
                        source="material"
                    )
                ]
            },
            {
                "properties": [
                    property_input(
                        "InvalidMaterialUnit",
                        10.0,
                        value_type="IfcLengthMeasure",
                        unit="NONEXISTENT_UNIT",
                        source="material"
                    )
                ]
            },
            {
                "properties": [
                    property_input(
                        "ValidProperty",
                        "VALID",
                        property_set="Pset_A_Valid",
                        source="material"
                    ),
                    property_input(
                        "InvalidProperty",
                        1.0,
                        value_type="IfcNonexistentMeasure",
                        property_set="Pset_Z_Invalid",
                        source="material"
                    )
                ]
            }
        ]

        for index, options in enumerate(cases):
            with self.subTest(case=index):
                self.load_fixture()

                params = self.material_parameters(
                    name=f"Rejected material {index}",
                    **options
                )

                self.assert_failed_transaction(
                    "materialCreate",
                    params,
                    "INVALID_PARAMETERS"
                )

                self.assertEqual(
                    self.list_materials(),
                    []
                )

        self.load_fixture()

        recovered = self.create_material(
            name="Recovered material"
        )

        self.assertIs(recovered["editable"], True)

        self.assign_material(
            recovered["id"],
            self.root_id
        )

        self.assertEqual(
            self.get_transform(self.root_id)["materials"],
            [recovered["id"]]
        )

        self.assert_index_consistency()

    def test_failed_partial_update(self):
        """Verify atomicity of multi-field updates with late validation errors."""

        for category in ("transform", "geometry", "material"):
            with self.subTest(category=category):
                self.load_fixture()

                if category == "transform":
                    target = self.create_transform(
                        name="Original transform",
                        matrix=translation_matrix(1, 2, 3),
                        properties=[
                            property_input("Reference", "ORIGINAL")
                        ]
                    )

                    command = "transformUpdate"

                    params = {
                        "id": target["id"],
                        "name": "Rejected transform rename",
                        "parents": parents_input(
                            spatial=self.storey_id,
                            placement=self.root_id
                        ),
                        "transform": transform_input(
                            invalid_scaling_matrix()
                        )
                    }

                    expected_code = "INVALID_PLACEMENT"

                elif category == "geometry":
                    target = self.create_geometry(
                        name="Original domain",
                        depth=3.0
                    )

                    command = "geometryUpdate"

                    params = {
                        "id": target["id"],
                        "name": "Rejected geometry rename",
                        "transform": transform_input(
                            translation_matrix(100, 200, 300)
                        ),
                        "profile": [[0, 0], [1, 0], [2, 0]],
                        "depth": 10.0
                    }

                    expected_code = "INVALID_PROFILE"

                else:
                    target = self.create_material(
                        name="Original material",
                        properties=[
                            property_input(
                                "Reference",
                                "ORIGINAL",
                                source="material"
                            )
                        ]
                    )

                    self.assign_material(
                        target["id"],
                        self.root_id
                    )

                    command = "materialUpdate"

                    params = {
                        "id": target["id"],
                        "name": "Rejected material rename",
                        "category": "metal",
                        "visual": {
                            "color": [0.1, 0.2, 0.3],
                            "opacity": 0.5,
                            "metallic": 0.9,
                            "roughness": 0.1
                        },
                        "properties": [
                            property_input(
                                "InvalidValue",
                                1.0,
                                value_type="IfcNonexistentMeasure",
                                source="material"
                            )
                        ]
                    }

                    expected_code = "INVALID_PARAMETERS"

                before = self.snapshot_transaction()
                physical_before = self.physical_mesh()

                self.client.expect_error(
                    command,
                    params,
                    expected_code
                )

                self.assert_transaction_unchanged(before)

                self.assert_mesh_equivalent(
                    physical_before,
                    self.physical_mesh()
                )

        self.load_fixture()

        # Validation errors detected before dispatch must also be atomic.

        domain = self.create_geometry(
            name="Domain with pre-validation error"
        )

        self.assert_failed_transaction(
            "geometryUpdate",
            {
                "id": domain["id"],
                "name": "Rejected rename",
                "depth": -1.0
            },
            "INVALID_PARAMETERS"
        )

        self.assert_volume(
            self.domain_mesh(domain),
            12.0
        )

    def test_failed_duplication(self):
        """Verify rollback when recursive duplication fails partway through."""

        self.load_fixture(DUPLICATION_FAILURE_ENTITIES)

        unsupported_child = self.find_transform(
            GRID_CHILD_GLOBAL_ID
        )

        self.assertEqual(
            unsupported_child["transforms"]["sourceType"],
            "IfcGridPlacement"
        )

        self.assertEqual(
            unsupported_child["parents"]["decomposition"],
            self.root_id
        )

        duplicable_child = self.find_transform(
            "0000000000000000000014"
        )

        self.assertEqual(
            duplicable_child["parents"]["decomposition"],
            self.root_id
        )

        self.create_geometry(
            name="Source domain",
            depth=3.0
        )

        source_material = self.create_material(
            name="Source material"
        )

        self.assign_material(
            source_material["id"],
            self.root_id
        )

        before = self.snapshot_transaction()
        physical_before = self.physical_mesh()

        # Failure before any duplicate is created.

        self.assert_failed_transaction(
            "transformDuplicate",
            {
                "id": self.project_id,
                "includeChildren": False
            },
            "TRANSFORM_NOT_DUPLICABLE"
        )

        # Failure after duplicating the root and a supported child.

        error = self.client.expect_error(
            "transformDuplicate",
            {
                "id": self.root_id,
                "includeChildren": True
            },
            "TRANSFORM_NOT_DUPLICABLE"
        )

        self.assertEqual(
            error["details"]["sourceType"],
            "IfcGridPlacement"
        )

        self.assert_transaction_unchanged(before)

        self.assert_mesh_equivalent(
            physical_before,
            self.physical_mesh()
        )

        self.assertEqual(
            self.find_transform(GRID_CHILD_GLOBAL_ID)["globalId"],
            GRID_CHILD_GLOBAL_ID
        )

        # Reload a clean fixture before checking successful recovery.

        self.load_fixture()

        duplicate = self.client.request(
            "transformDuplicate",
            {
                "id": self.root_id,
                "includeChildren": False
            }
        )

        self.assertNotEqual(
            duplicate["rootId"],
            self.root_id
        )

        self.assertEqual(
            set(duplicate["transforms"]),
            {self.root_id}
        )

        self.assert_volume(
            self.client.tessellate(duplicate["rootId"]),
            64.0
        )

        self.assert_index_consistency()

    def test_repeated_load_save(self):
        """Verify stability over many consecutive IFC roundtrips."""

        child = self.create_transform(
            name="Persistent roundtrip child",
            parents=parents_input(
                placement=self.root_id,
                decomposition=self.root_id
            ),
            matrix=translation_matrix(10, 20, 30),
            properties=[
                property_input("Reference", "ROUNDTRIP")
            ]
        )

        domain = self.create_geometry(
            parent_id=child["id"],
            name="Persistent roundtrip domain",
            depth=4.0
        )

        material = self.create_material(
            name="Persistent roundtrip material"
        )

        self.assign_material(
            material["id"],
            self.root_id
        )

        self.assign_material(
            material["id"],
            child["id"]
        )

        expected = self.persistent_snapshot()

        reference_entities = self.save_entities()
        reference_entity_count = len(reference_entities)

        physical_before = self.physical_mesh()
        domain_before = self.domain_mesh(domain)

        paths = [
            self.root_path / "roundtrip-a.ifc",
            self.root_path / "roundtrip-b.ifc"
        ]

        for iteration in range(ROUNDTRIP_COUNT):
            with self.subTest(iteration=iteration):
                path = paths[iteration % len(paths)]

                self.save_model(path)

                entities = parse_step_entities(
                    path.read_text(encoding="utf-8")
                )

                self.assert_step_integrity(entities)

                self.assertEqual(
                    len(entities),
                    reference_entity_count,
                    "Unexpected IFC entity growth"
                )

                self.load_model(path)

                self.assertEqual(
                    self.persistent_snapshot(),
                    expected
                )

                self.assert_process_alive()
                self.assert_index_consistency()

                if iteration % 5 == 0:
                    current_domain = self.find_geometry(
                        domain["globalId"]
                    )

                    self.assert_mesh_equivalent(
                        domain_before,
                        self.domain_mesh(current_domain)
                    )

                    self.assert_mesh_equivalent(
                        physical_before,
                        self.physical_mesh()
                    )

        current_domain = self.find_geometry(domain["globalId"])

        updated = self.update_geometry(
            current_domain["id"],
            depth=8.0
        )

        self.assert_volume(
            self.domain_mesh(updated),
            48.0
        )

        self.save_and_reload()
        self.assert_index_consistency()

    def test_long_request(self):
        """Verify processing of a large JSONL request and response."""

        large_text = (
            "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            * (LONG_REQUEST_SIZE // 36 + 1)
        )[:LONG_REQUEST_SIZE]

        self.assertEqual(
            len(large_text),
            LONG_REQUEST_SIZE
        )

        properties = [
            property_input(
                "LargeText",
                large_text,
                value_type="IfcText"
            )
        ]

        params = self.transform_parameters(
            name="Large JSONL request",
            properties=properties
        )

        encoded_request = json.dumps(
            {
                "id": "long-request",
                "command": "transformCreate",
                "params": params
            },
            ensure_ascii=False
        )

        self.assertGreater(
            len(encoded_request.encode("utf-8")),
            LONG_REQUEST_SIZE
        )

        self.client.write_raw(encoded_request)

        response = self.client.read("long-request")

        self.assertIs(response["ok"], True)

        created = response["result"]

        self.assertEqual(
            created["properties"],
            properties
        )

        self.assertEqual(
            self.get_transform(created["id"])["properties"],
            properties
        )

        # Verify large collection handling as well as large scalar values.

        many_properties = [
            property_input(
                f"Property_{index:04d}",
                index,
                value_type="IfcInteger"
            )
            for index in range(1000)
        ]

        updated = self.update_transform(
            created["id"],
            properties=[*properties, *many_properties]
        )

        self.assertEqual(
            len(updated["properties"]),
            1001
        )

        self.assertEqual(
            sorted_properties(updated["properties"]),
            sorted_properties([*properties, *many_properties])
        )

        self.save_and_reload()

        current = self.find_transform(
            created["globalId"]
        )

        self.assertEqual(
            sorted_properties(current["properties"]),
            sorted_properties([*properties, *many_properties])
        )

        self.assert_process_alive()

        self.update_transform(
            current["id"],
            properties=[]
        )

        self.assertEqual(
            self.get_transform(current["id"])["properties"],
            []
        )

        self.assert_index_consistency()

    def test_long_session(self):
        """Verify persistent process stability over thousands of requests."""

        domain = self.create_geometry(
            name="Long session domain"
        )

        material = self.create_material(
            name="Long session material"
        )

        root_id = self.root_id

        initial_indexes = self.snapshot_indexes()
        physical_before = self.physical_mesh()

        commands = [
            ("getCapabilities", {}),
            ("transformGetAll", {}),
            ("transformGet", {"id": root_id}),
            (
                "geometryGetAll",
                {
                    "transformId": root_id,
                    "kind": "ALL"
                }
            ),
            ("geometryGet", {"id": domain["id"]}),
            ("materialGetAll", {}),
            ("materialGet", {"id": material["id"]})
        ]

        initial_request_count = self.client.counter

        # Submit requests in bounded batches to exercise JSONL ordering
        # without filling the subprocess input or output pipes.

        batch_size = 25

        for batch_start in range(
            0,
            LONG_SESSION_REQUEST_COUNT,
            batch_size
        ):
            pending = []

            batch_end = min(
                batch_start + batch_size,
                LONG_SESSION_REQUEST_COUNT
            )

            for index in range(batch_start, batch_end):
                command, params = commands[index % len(commands)]

                request_id = self.client.send(
                    command,
                    params
                )

                pending.append((request_id, command))

            for request_id, command in pending:
                response = self.client.read(request_id)

                self.assertIs(
                    response["ok"],
                    True,
                    f"Long session failure for {command}: {response}"
                )

                if command == "getCapabilities":
                    self.assertEqual(
                        response["result"],
                        self.capabilities
                    )

            if batch_start % 250 == 0:
                self.assert_process_alive()

                self.assertEqual(
                    self.snapshot_indexes(),
                    initial_indexes
                )

                self.assert_mesh_equivalent(
                    physical_before,
                    self.physical_mesh()
                )

        self.assertGreaterEqual(
            self.client.counter - initial_request_count,
            LONG_SESSION_REQUEST_COUNT
        )

        # Interleave successful edits and failed requests after the
        # long read-only sequence.

        for index in range(100):
            depth = 1.0 + index / 10.0

            updated = self.update_geometry(
                domain["id"],
                depth=depth
            )

            self.assertAlmostEqual(
                updated["depth"],
                depth
            )

            self.client.expect_error(
                "geometryUpdate",
                {
                    "id": domain["id"],
                    "depth": -1.0
                },
                "INVALID_PARAMETERS"
            )

            self.assertAlmostEqual(
                self.get_geometry(domain["id"])["depth"],
                depth
            )

        self.assert_volume(
            self.domain_mesh(domain),
            6.0 * 10.9
        )

        self.assert_process_alive()
        self.assert_index_consistency()

        expected = self.persistent_snapshot()

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

    def test_deterministic_fuzzing(self):
        """Verify resistance to reproducible valid and invalid requests."""

        rng = random.Random(FUZZ_SEED)

        domain = self.create_geometry(
            name="Fuzzing reference domain"
        )

        material = self.create_material(
            name="Fuzzing reference material"
        )

        root_id = self.root_id

        before = self.snapshot_transaction()

        invalid_values = [
            None,
            False,
            True,
            0,
            -1,
            1.5,
            "",
            [],
            {},
            ["invalid"],
            {"invalid": True}
        ]

        valid_cases = [
            ("help", {}),
            ("getCapabilities", {}),
            ("transformGetAll", {}),
            ("transformGet", {"id": root_id}),
            (
                "geometryGetAll",
                {
                    "transformId": root_id,
                    "kind": "DOMAIN"
                }
            ),
            ("geometryGet", {"id": domain["id"]}),
            ("materialGetAll", {}),
            ("materialGet", {"id": material["id"]})
        ]

        successful_requests = 0
        rejected_requests = 0

        for index in range(FUZZ_REQUEST_COUNT):
            mode = rng.randrange(12)

            with self.subTest(
                seed=FUZZ_SEED,
                iteration=index,
                mode=mode
            ):
                if mode == 0:
                    command, params = rng.choice(valid_cases)

                    self.client.request(command, params)
                    successful_requests += 1

                elif mode == 1:
                    line = json.dumps({
                        "id": f"fuzz-{index}",
                        "command": "help",
                        "params": {}
                    })

                    cut = rng.randrange(1, len(line))

                    self.client.expect_raw_error(
                        line[:cut],
                        "INVALID_JSON"
                    )

                    rejected_requests += 1

                elif mode == 2:
                    self.client.expect_error(
                        f"unknown_{rng.getrandbits(64)}",
                        {},
                        "UNKNOWN_COMMAND"
                    )

                    rejected_requests += 1

                elif mode == 3:
                    self.client.expect_error(
                        "transformGet",
                        {"id": rng.choice(invalid_values)},
                        "INVALID_PARAMETERS"
                    )

                    rejected_requests += 1

                elif mode == 4:
                    self.client.expect_error(
                        "geometryGet",
                        {"id": root_id},
                        "INVALID_ENTITY_TYPE"
                    )

                    rejected_requests += 1

                elif mode == 5:
                    self.client.expect_error(
                        "transformGet",
                        {
                            "id": f"transform:{rng.randint(10**8, 10**9)}"
                        },
                        "ENTITY_NOT_FOUND"
                    )

                    rejected_requests += 1

                elif mode == 6:
                    self.client.expect_error(
                        "geometryUpdate",
                        {
                            "id": domain["id"],
                            "depth": -rng.uniform(0.01, 1000)
                        },
                        "INVALID_PARAMETERS"
                    )

                    rejected_requests += 1

                elif mode == 7:
                    self.client.expect_error(
                        "geometryUpdate",
                        {
                            "id": domain["id"],
                            "profile": [
                                [0, 0],
                                [rng.uniform(0.1, 100), 0]
                            ]
                        },
                        "INVALID_PARAMETERS"
                    )

                    rejected_requests += 1

                elif mode == 8:
                    visual = DEFAULT_VISUAL.copy()

                    field = rng.choice(
                        ["opacity", "metallic", "roughness"]
                    )

                    visual[field] = 1.0 + rng.uniform(0.01, 100)

                    self.client.expect_error(
                        "materialUpdate",
                        {
                            "id": material["id"],
                            "visual": visual
                        },
                        "INVALID_PARAMETERS"
                    )

                    rejected_requests += 1

                elif mode == 9:
                    self.client.expect_error(
                        "materialAssign",
                        {
                            "materialId": material["id"],
                            "targetId": domain["id"]
                        },
                        "INVALID_RELATION"
                    )

                    rejected_requests += 1

                elif mode == 10:
                    self.client.expect_error(
                        "geometryGetAll",
                        {
                            "transformId": None,
                            "kind": f"INVALID_{rng.randrange(10000)}"
                        },
                        "INVALID_PARAMETERS"
                    )

                    rejected_requests += 1

                else:
                    self.client.expect_error(
                        "transformGetAll",
                        {
                            f"extra_{rng.randrange(10000)}": rng.choice(
                                invalid_values
                            )
                        },
                        "INVALID_PARAMETERS"
                    )

                    rejected_requests += 1

                if index % 25 == 0:
                    self.assert_process_alive()

                    self.assertEqual(
                        self.snapshot_model(),
                        before["model"]
                    )

        self.assertGreater(successful_requests, 0)
        self.assertGreater(rejected_requests, 0)

        self.assertEqual(
            successful_requests + rejected_requests,
            FUZZ_REQUEST_COUNT
        )

        self.assert_transaction_unchanged(before)

        self.assert_volume(
            self.domain_mesh(domain),
            12.0
        )

        self.assert_volume(
            self.physical_mesh(),
            64.0
        )

    def test_random_edit_sequence(self):
        """Verify index consistency after reproducible random model edits."""

        rng = random.Random(EDIT_SEED)

        root_id = self.root_id

        baseline_transform_ids = {
            item["id"]
            for item in self.list_transforms()
        }

        baseline_geometry_ids = {
            item["id"]
            for item in self.list_geometries()
        }

        baseline_material_ids = {
            item["id"]
            for item in self.list_materials()
        }

        transforms = {}
        geometries = {}
        materials = {}
        assignments = set()

        retired_ids = set()
        executed_operations = set()

        physical_before = self.physical_mesh()

        def choose(mapping):
            return rng.choice(sorted(mapping))

        def remember_created(mapping, item):
            identifier = item["id"]

            self.assertNotIn(identifier, mapping)
            self.assertNotIn(identifier, retired_ids)

            mapping[identifier] = item

        def assert_expected_indexes():
            self.assertEqual(
                {
                    item["id"]
                    for item in self.list_transforms()
                },
                baseline_transform_ids | set(transforms)
            )

            self.assertEqual(
                {
                    item["id"]
                    for item in self.list_geometries()
                },
                baseline_geometry_ids | set(geometries)
            )

            self.assertEqual(
                {
                    item["id"]
                    for item in self.list_materials()
                },
                baseline_material_ids | set(materials)
            )

        def assert_expected_assignments():
            targets = {
                root_id,
                *transforms.keys(),
                *(
                    identifier
                    for identifier, geometry in geometries.items()
                    if geometry["kind"] == "SOLID"
                )
            }

            for target_id in targets:
                target = (
                    self.get_geometry(target_id)
                    if target_id.startswith("geometry:")
                    else self.get_transform(target_id)
                )

                expected = {
                    material_id
                    for material_id, assigned_target in assignments
                    if assigned_target == target_id
                }

                self.assertEqual(
                    set(target["materials"]),
                    expected
                )

        for iteration in range(RANDOM_EDIT_COUNT):
            leaf_transforms = [
                identifier
                for identifier in transforms
                if not any(
                    identifier in item["parents"].values()
                    for item in transforms.values()
                )
            ]

            available_operations = [
                "create_transform",
                "create_domain",
                "create_solid",
                "create_material"
            ]

            if transforms:
                available_operations.append("update_transform")

            if leaf_transforms:
                available_operations.append("delete_transform")

            if geometries:
                available_operations.extend([
                    "update_geometry",
                    "delete_geometry"
                ])

            if materials:
                available_operations.extend([
                    "update_material",
                    "delete_material",
                    "assign_material"
                ])

            if assignments:
                available_operations.append("unassign_material")

            operation = rng.choice(available_operations)

            executed_operations.add(operation)

            with self.subTest(
                seed=EDIT_SEED,
                iteration=iteration,
                operation=operation
            ):
                if operation == "create_transform":
                    possible_parents = [
                        None,
                        root_id,
                        *sorted(transforms)
                    ]

                    parent_id = rng.choice(possible_parents)

                    created = self.create_transform(
                        name=f"Random transform {iteration}",
                        parents=parents_input(
                            placement=parent_id,
                            decomposition=parent_id
                        ),
                        matrix=translation_matrix(
                            rng.randint(-100, 100),
                            rng.randint(-100, 100),
                            rng.randint(-100, 100)
                        ),
                        properties=[
                            property_input(
                                "Sequence",
                                iteration,
                                value_type="IfcInteger"
                            )
                        ]
                    )

                    remember_created(transforms, created)

                elif operation == "create_domain":
                    parent_id = rng.choice([
                        root_id,
                        *sorted(transforms)
                    ])

                    width = rng.randint(1, 10)
                    length = rng.randint(1, 10)
                    depth = rng.randint(1, 10)

                    created = self.create_geometry(
                        parent_id=parent_id,
                        name=f"Random domain {iteration}",
                        profile=rectangular_profile(width, length),
                        depth=depth,
                        matrix=translation_matrix(
                            rng.randint(-50, 50),
                            rng.randint(-50, 50),
                            rng.randint(-50, 50)
                        )
                    )

                    remember_created(geometries, created)

                    self.assert_volume(
                        self.domain_mesh(created),
                        width * length * depth
                    )

                elif operation == "create_solid":
                    # Keep physical features outside the baseline cube.
                    # This exercises IFC editing without introducing
                    # overlapping boolean operations into the random model.

                    created = self.create_geometry(
                        parent_id=root_id,
                        name=f"Random opening {iteration}",
                        kind="SOLID",
                        operation="SUBTRACT",
                        profile=rectangular_profile(
                            rng.randint(1, 3),
                            rng.randint(1, 3)
                        ),
                        depth=rng.randint(1, 5),
                        matrix=translation_matrix(
                            100 + iteration * 10,
                            100,
                            100
                        )
                    )

                    remember_created(geometries, created)

                elif operation == "create_material":
                    created = self.create_material(
                        name=f"Random material {iteration}",
                        category=rng.choice([
                            "concrete",
                            "metal",
                            "wood",
                            "glass"
                        ]),
                        visual={
                            "color": [
                                rng.randint(0, 100) / 100
                                for _ in range(3)
                            ],
                            "opacity": rng.randint(0, 100) / 100,
                            "metallic": rng.randint(0, 100) / 100,
                            "roughness": rng.randint(0, 100) / 100
                        }
                    )

                    remember_created(materials, created)

                elif operation == "update_transform":
                    identifier = choose(transforms)

                    updated = self.update_transform(
                        identifier,
                        name=f"Updated transform {iteration}",
                        transform=transform_input(
                            translation_matrix(
                                rng.randint(-100, 100),
                                rng.randint(-100, 100),
                                rng.randint(-100, 100)
                            )
                        )
                    )

                    self.assertEqual(
                        updated["globalId"],
                        transforms[identifier]["globalId"]
                    )

                    transforms[identifier] = updated

                elif operation == "update_geometry":
                    identifier = choose(geometries)
                    previous = geometries[identifier]

                    depth = rng.randint(1, 20)

                    updated = self.update_geometry(
                        identifier,
                        name=f"Updated geometry {iteration}",
                        depth=depth
                    )

                    self.assertEqual(
                        updated["globalId"],
                        previous["globalId"]
                    )

                    self.assertEqual(
                        updated["kind"],
                        previous["kind"]
                    )

                    self.assertAlmostEqual(
                        updated["depth"],
                        depth
                    )

                    geometries[identifier] = updated

                    if updated["kind"] == "DOMAIN":
                        profile = updated["profile"]

                        area = abs(math.fsum(
                            a[0] * b[1] - b[0] * a[1]
                            for a, b in zip(
                                profile,
                                profile[1:] + profile[:1]
                            )
                        )) / 2

                        self.assert_volume(
                            self.domain_mesh(updated),
                            area * depth
                        )

                elif operation == "update_material":
                    identifier = choose(materials)

                    updated = self.update_material(
                        identifier,
                        name=f"Updated material {iteration}",
                        category=rng.choice([
                            "metal",
                            "concrete",
                            "ceramic"
                        ])
                    )

                    self.assertEqual(
                        updated["ifcEntityId"],
                        materials[identifier]["ifcEntityId"]
                    )

                    materials[identifier] = updated

                elif operation == "assign_material":
                    material_id = choose(materials)

                    targets = [
                        root_id,
                        *sorted(transforms),
                        *sorted(
                            identifier
                            for identifier, geometry in geometries.items()
                            if geometry["kind"] == "SOLID"
                        )
                    ]

                    target_id = rng.choice(targets)

                    self.assign_material(
                        material_id,
                        target_id
                    )

                    assignments.add(
                        (material_id, target_id)
                    )

                elif operation == "unassign_material":
                    material_id, target_id = rng.choice(
                        sorted(assignments)
                    )

                    self.unassign_material(
                        material_id,
                        target_id
                    )

                    assignments.remove(
                        (material_id, target_id)
                    )

                elif operation == "delete_geometry":
                    identifier = choose(geometries)

                    self.delete_geometry(identifier)

                    geometries.pop(identifier)
                    retired_ids.add(identifier)

                    assignments = {
                        assignment
                        for assignment in assignments
                        if assignment[1] != identifier
                    }

                    self.client.expect_error(
                        "geometryGet",
                        {"id": identifier},
                        "ENTITY_NOT_FOUND"
                    )

                elif operation == "delete_transform":
                    identifier = rng.choice(
                        sorted(leaf_transforms)
                    )

                    owned_geometries = {
                        geometry_id
                        for geometry_id, geometry in geometries.items()
                        if geometry["parentId"] == identifier
                    }

                    self.delete_transform(identifier)

                    transforms.pop(identifier)
                    retired_ids.add(identifier)

                    for geometry_id in owned_geometries:
                        geometries.pop(geometry_id)
                        retired_ids.add(geometry_id)

                        self.client.expect_error(
                            "geometryGet",
                            {"id": geometry_id},
                            "ENTITY_NOT_FOUND"
                        )

                    removed_targets = {
                        identifier,
                        *owned_geometries
                    }

                    assignments = {
                        assignment
                        for assignment in assignments
                        if assignment[1] not in removed_targets
                    }

                    self.client.expect_error(
                        "transformGet",
                        {"id": identifier},
                        "ENTITY_NOT_FOUND"
                    )

                elif operation == "delete_material":
                    identifier = choose(materials)

                    self.delete_material(identifier)

                    materials.pop(identifier)
                    retired_ids.add(identifier)

                    assignments = {
                        assignment
                        for assignment in assignments
                        if assignment[0] != identifier
                    }

                    self.client.expect_error(
                        "materialGet",
                        {"id": identifier},
                        "ENTITY_NOT_FOUND"
                    )

                assert_expected_indexes()

                if iteration % 10 == 0:
                    self.assert_process_alive()
                    self.assert_index_consistency()
                    assert_expected_assignments()

                if iteration % 25 == 0:
                    self.save_entities()

                    self.assert_mesh_equivalent(
                        physical_before,
                        self.physical_mesh()
                    )

        self.assertEqual(
            executed_operations,
            {
                "create_transform",
                "update_transform",
                "delete_transform",
                "create_domain",
                "create_solid",
                "update_geometry",
                "delete_geometry",
                "create_material",
                "update_material",
                "delete_material",
                "assign_material",
                "unassign_material"
            },
            "Random sequence did not exercise every operation category"
        )

        assert_expected_indexes()
        assert_expected_assignments()

        self.assert_index_consistency()

        expected = self.persistent_snapshot()

        reference_domain_meshes = {
            geometry["globalId"]: self.domain_mesh(
                self.get_geometry(identifier)
            )
            for identifier, geometry in geometries.items()
            if geometry["kind"] == "DOMAIN"
        }

        self.save_and_reload()

        self.assertEqual(
            self.persistent_snapshot(),
            expected
        )

        self.assert_index_consistency()

        for global_id, reference in reference_domain_meshes.items():
            current = self.find_geometry(global_id)

            self.assert_mesh_equivalent(
                reference,
                self.domain_mesh(current)
            )

        self.assert_mesh_equivalent(
            physical_before,
            self.physical_mesh()
        )

        self.assert_process_alive()


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