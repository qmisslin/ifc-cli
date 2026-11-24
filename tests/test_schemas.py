import json
import math
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest

from pathlib import Path


# Standalone integration suite using only the Python standard library.
# Not executed against the compiled CLI.
#
# Optional schemas are tested only when advertised by getCapabilities.
# Every advertised schema must successfully load a corresponding fixture.
#
# IFC2X3 fixtures include the mandatory IfcOwnerHistory dependencies.
# IFC4X3 variants are tested individually when advertised.
#
# IFC2X3 material creation may expose a compatibility issue if the CLI
# selects the abstract IfcMaterialProperties class instead of
# IfcExtendedMaterialProperties.
#
# The IFC2X3 domain rejection must return
# GEOMETRY_OPERATION_NOT_SUPPORTED because IfcSpatialZone is unavailable.
#
# Run:
# python3 tests/test_schemas.py ./build/ifc-cli

DEFAULT_EXECUTABLE = "./build/ifc-cli"
EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

REQUEST_TIMEOUT = 60
NUMERIC_TOLERANCE = 1e-7
MESH_TOLERANCE = 1e-6

PROJECT_GLOBAL_ID = "0000000000000000000001"
BUILDING_GLOBAL_ID = "0000000000000000000002"
STOREY_GLOBAL_ID = "0000000000000000000003"
PRODUCT_GLOBAL_ID = "0000000000000000000004"

UNSUPPORTED_SCHEMA = "IFC999"

DEFAULT_PROFILE = [
    [0.0, 0.0],
    [2.0, 0.0],
    [2.0, 3.0],
    [0.0, 3.0]
]

DEFAULT_VISUAL = {
    "color": [0.4, 0.6, 0.8],
    "opacity": 0.85,
    "metallic": 0.2,
    "roughness": 0.5
}

IDENTITY_MATRIX = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0
]

BASE_BOUNDS = (
    -2.0, -1.5, 0.0,
    2.0, 1.5, 2.0
)

BASE_VOLUME = 24.0


def translation_matrix(x=0.0, y=0.0, z=0.0):
    matrix = IDENTITY_MATRIX.copy()

    matrix[3] = x
    matrix[7] = y
    matrix[11] = z

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
    property_set="Pset_SchemaTest",
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


def rectangular_profile(width, length, x=0.0, y=0.0):
    return [
        [x, y],
        [x + width, y],
        [x + width, y + length],
        [x, y + length]
    ]


def is_ifc2x3(schema):
    return schema.upper().startswith("IFC2X3")


def is_ifc4x3(schema):
    return schema.upper().startswith("IFC4X3")


def build_fixture(schema):
    schema = schema.upper()

    # IFC2X3 requires OwnerHistory for every IfcRoot instance.
    # The same valid ownership graph is retained in IFC4 fixtures.

    proxy_type = ".ELEMENT." if is_ifc2x3(schema) else ".NOTDEFINED."

    return f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('schema-{schema}.ifc','2026-01-01T00:00:00',('Test Author'),('Test Organization'),'Test','Test','');
FILE_SCHEMA(('{schema}'));
ENDSEC;
DATA;
#1=IFCPERSON($,'Test','Author',$,$,$,$,$);
#2=IFCORGANIZATION($,'Test Organization',$,$,$);
#3=IFCPERSONANDORGANIZATION(#1,#2,$);
#4=IFCAPPLICATION(#2,'1.0','Schema Integration Tests','SCHEMA-TESTS');
#5=IFCOWNERHISTORY(#3,#4,$,.ADDED.,$,$,$,1767225600);
#6=IFCCARTESIANPOINT((0.,0.,0.));
#7=IFCDIRECTION((0.,0.,1.));
#8=IFCDIRECTION((1.,0.,0.));
#9=IFCAXIS2PLACEMENT3D(#6,#7,#8);
#10=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-5,#9,$);
#11=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#12=IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.);
#13=IFCSIUNIT(*,.VOLUMEUNIT.,$,.CUBIC_METRE.);
#14=IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.);
#15=IFCUNITASSIGNMENT((#11,#12,#13,#14));
#16=IFCPROJECT('{PROJECT_GLOBAL_ID}',#5,'Schema test project',$,$,$,$,(#10),#15);
#17=IFCLOCALPLACEMENT($,#9);
#18=IFCBUILDING('{BUILDING_GLOBAL_ID}',#5,'Schema test building',$,$,#17,$,$,.ELEMENT.,$,$,$);
#19=IFCLOCALPLACEMENT(#17,#9);
#20=IFCBUILDINGSTOREY('{STOREY_GLOBAL_ID}',#5,'Schema test storey',$,$,#19,$,$,.ELEMENT.,0.);
#21=IFCLOCALPLACEMENT(#19,#9);
#22=IFCCARTESIANPOINT((0.,0.));
#23=IFCAXIS2PLACEMENT2D(#22,$);
#24=IFCRECTANGLEPROFILEDEF(.AREA.,$,#23,4.,3.);
#25=IFCEXTRUDEDAREASOLID(#24,#9,#7,2.);
#26=IFCSHAPEREPRESENTATION(#10,'Body','SweptSolid',(#25));
#27=IFCPRODUCTDEFINITIONSHAPE($,$,(#26));
#28=IFCBUILDINGELEMENTPROXY('{PRODUCT_GLOBAL_ID}',#5,'Schema test product','Native description',$,#21,#27,'PRODUCT-001',{proxy_type});
#29=IFCRELAGGREGATES('0000000000000000000005',#5,$,$,#16,(#18));
#30=IFCRELAGGREGATES('0000000000000000000006',#5,$,$,#18,(#20));
#31=IFCRELCONTAINEDINSPATIALSTRUCTURE('0000000000000000000007',#5,$,$,(#28),#20);
#32=IFCPROPERTYSINGLEVALUE('Reference',$,IFCLABEL('SCHEMA-PRODUCT'),$);
#33=IFCPROPERTYSET('0000000000000000000008',#5,'Pset_Native',$,(#32));
#34=IFCRELDEFINESBYPROPERTIES('0000000000000000000009',#5,$,$,(#28),#33);
ENDSEC;
END-ISO-10303-21;
"""


def parse_step_entities(content):
    # Targeted STEP parser for the controlled fixtures.
    # Not intended to parse arbitrary IFC files.

    pattern = re.compile(
        r"#(\d+)\s*=\s*(IFC[A-Z0-9_]+)\s*\((.*?)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL
    )

    entities = {}

    for match in pattern.finditer(content):
        identifier = int(match.group(1))

        if identifier in entities:
            raise AssertionError(f"Duplicate STEP entity #{identifier}")

        entities[identifier] = {
            "class": match.group(2).upper(),
            "arguments": match.group(3)
        }

    if not entities:
        raise AssertionError("No IFC entities found")

    return entities


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


def step_references(arguments):
    without_strings = re.sub(
        r"'(?:[^']|'')*'",
        "''",
        arguments
    )

    return [
        int(identifier)
        for identifier in re.findall(r"#(\d+)", without_strings)
    ]


def read_file_schema(content):
    match = re.search(
        r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'\s*\)\s*\)\s*;",
        content,
        flags=re.IGNORECASE
    )

    if match is None:
        raise AssertionError("Missing or invalid FILE_SCHEMA declaration")

    return match.group(1).upper()


def entities_by_class(entities, class_name):
    return {
        identifier: entity
        for identifier, entity in entities.items()
        if entity["class"] == class_name.upper()
    }


def entity_by_global_id(entities, global_id):
    matches = []

    for identifier, entity in entities.items():
        arguments = split_step_arguments(entity["arguments"])

        if arguments and arguments[0] == f"'{global_id}'":
            matches.append((identifier, entity))

    if len(matches) != 1:
        raise AssertionError(
            f"Expected one IFC entity with GlobalId {global_id}, "
            f"received {len(matches)}"
        )

    return matches[0]


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


def mesh_bounds(mesh):
    points = mesh_points(mesh)

    if not points:
        raise AssertionError("Mesh contains no vertices")

    return tuple(
        operation(point[axis] for point in points)
        for operation in (min, max)
        for axis in range(3)
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


def mesh_vertex_set(mesh, precision=6):
    return {
        tuple(round(value, precision) for value in point)
        for point in mesh_points(mesh)
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

        if not response["error"]["message"]:
            raise AssertionError("Empty error message")

        return response["error"]

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
            "includeMaterials": True
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
        previous_order = -1
        materials_received = False

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

            if name == "mesh.materials":
                if materials_received:
                    raise AssertionError("Duplicate mesh.materials")

                mesh["groups"] = event["groups"]
                materials_received = True
                continue

            key = name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(
                    f"Invalid mesh chunk offset: {event}"
                )

            mesh[key].extend(event["values"])

        if not materials_received:
            raise AssertionError("Missing mesh.materials event")

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError("Invalid triangle indices")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(f"Invalid vertex index: {index}")

        for value in mesh["vertices"]:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise AssertionError(f"Invalid vertex coordinate: {value}")

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


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-schemas-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.capabilities = self.client.request("getCapabilities")

        self.supported_schemas = {
            schema.upper(): schema
            for schema in self.capabilities["supportedSchemas"]
        }

        self.schema = None
        self.save_counter = 0

    def tearDown(self):
        self.client.close()

    def require_schema(self, schema):
        schema = schema.upper()

        if schema not in self.supported_schemas:
            self.skipTest(f"{schema} is not advertised by the CLI")

        return schema

    def require_ifc2x3(self):
        return self.require_schema("IFC2X3")

    def require_ifc4x3(self):
        schemas = sorted(
            schema
            for schema in self.supported_schemas
            if is_ifc4x3(schema)
        )

        if not schemas:
            self.skipTest("No IFC4X3 schema is advertised by the CLI")

        return schemas

    def load_schema(self, schema):
        schema = schema.upper()

        path = self.root_path / f"{schema}.ifc"

        path.write_text(
            build_fixture(schema),
            encoding="utf-8"
        )

        return self.load_model(path, schema)

    def load_model(self, path, expected_schema):
        result = self.client.request(
            "load",
            {"path": str(path)}
        )

        self.assertEqual(result["path"], str(path))

        self.assertEqual(
            result["schema"].upper(),
            expected_schema.upper()
        )

        self.schema = result["schema"].upper()

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

        self.project_id = self.find_transform(PROJECT_GLOBAL_ID)["id"]
        self.building_id = self.find_transform(BUILDING_GLOBAL_ID)["id"]
        self.storey_id = self.find_transform(STOREY_GLOBAL_ID)["id"]
        self.product_id = self.find_transform(PRODUCT_GLOBAL_ID)["id"]

        return result

    def save_model(self):
        self.save_counter += 1

        path = self.root_path / (
            f"{self.schema}-roundtrip-{self.save_counter}.ifc"
        )

        result = self.client.request(
            "save",
            {"path": str(path)}
        )

        self.assertEqual(result, {"path": str(path)})
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)

        return path

    def save_and_reload(self):
        path = self.save_model()
        content = path.read_text(encoding="utf-8")

        self.assertEqual(
            read_file_schema(content),
            self.schema
        )

        self.assert_step_integrity(
            parse_step_entities(content)
        )

        self.load_model(path, self.schema)

        return path

    def save_entities(self):
        path = self.save_model()
        content = path.read_text(encoding="utf-8")

        self.assertEqual(
            read_file_schema(content),
            self.schema
        )

        entities = parse_step_entities(content)

        self.assert_step_integrity(entities)

        return entities

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

    def find_material(self, name):
        materials = [
            self.get_material(item["id"])
            for item in self.list_materials()
        ]

        matches = [
            material
            for material in materials
            if material["name"] == name
        ]

        self.assertEqual(len(matches), 1)

        return matches[0]

    def create_transform(
        self,
        name="Schema test transform",
        parents=None,
        matrix=None,
        properties=None
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
                "transform": transform_input(matrix),
                "properties": [] if properties is None else properties
            }
        )

    def geometry_parameters(
        self,
        name="Schema test geometry",
        parent_id=None,
        kind="SOLID",
        operation="ADD",
        profile=None,
        depth=2.0,
        matrix=None
    ):
        return {
            "parentId": (
                self.product_id
                if parent_id is None
                else parent_id
            ),
            "name": name,
            "kind": kind,
            "operation": operation,
            "profile": DEFAULT_PROFILE if profile is None else profile,
            "depth": depth,
            "transform": transform_input(matrix)
        }

    def create_geometry(self, **options):
        return self.client.request(
            "geometryCreate",
            self.geometry_parameters(**options)
        )

    def create_domain(self, **options):
        return self.create_geometry(
            kind="DOMAIN",
            operation="ADD",
            **options
        )

    def create_material(
        self,
        name="Schema test material",
        category="concrete",
        visual=None,
        properties=None
    ):
        return self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": category,
                "visual": DEFAULT_VISUAL if visual is None else visual,
                "properties": [] if properties is None else properties
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

    def tessellate(self, identifier=None, **options):
        return self.client.tessellate(
            self.product_id if identifier is None else identifier,
            **options
        )

    def tessellate_domain(self, geometry, **options):
        return self.tessellate(
            geometry["parentId"],
            kind="DOMAIN",
            geometry_id=geometry["id"],
            **options
        )

    def assert_matrix(self, actual, expected):
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for index, (value, reference) in enumerate(zip(actual, expected)):
            self.assertAlmostEqual(
                value,
                reference,
                delta=NUMERIC_TOLERANCE,
                msg=f"Invalid matrix component {index}"
            )

    def assert_bounds(self, mesh, expected):
        actual = mesh_bounds(mesh)

        self.assertEqual(len(actual), len(expected))

        for index, (value, reference) in enumerate(zip(actual, expected)):
            self.assertAlmostEqual(
                value,
                reference,
                delta=MESH_TOLERANCE,
                msg=f"Invalid bounding box component {index}"
            )

    def assert_volume(self, mesh, expected):
        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected,
            delta=MESH_TOLERANCE
        )

    def assert_mesh_equivalent(self, before, after):
        self.assertEqual(
            mesh_vertex_set(before),
            mesh_vertex_set(after)
        )

        self.assert_bounds(
            after,
            mesh_bounds(before)
        )

        self.assert_volume(
            after,
            mesh_volume(before)
        )

    def assert_empty_mesh(self, mesh):
        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)
        self.assertEqual(mesh["vertices"], [])
        self.assertEqual(mesh["indices"], [])
        self.assertEqual(mesh["groups"], [])

    def assert_visual(self, actual, expected):
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(len(actual["color"]), 3)

        for value, reference in zip(actual["color"], expected["color"]):
            self.assertAlmostEqual(
                value,
                reference,
                delta=NUMERIC_TOLERANCE
            )

        for field in ("opacity", "metallic", "roughness"):
            self.assertAlmostEqual(
                actual[field],
                expected[field],
                delta=NUMERIC_TOLERANCE
            )

    def assert_properties(self, actual, expected):
        actual_by_key = {
            (item["propertySet"], item["name"], item["source"]): item
            for item in actual
        }

        expected_by_key = {
            (item["propertySet"], item["name"], item["source"]): item
            for item in expected
        }

        self.assertEqual(len(actual_by_key), len(actual))
        self.assertEqual(len(expected_by_key), len(expected))
        self.assertEqual(set(actual_by_key), set(expected_by_key))

        for key, reference in expected_by_key.items():
            current = actual_by_key[key]

            self.assertEqual(set(current), set(reference))

            for field, value in reference.items():
                if isinstance(value, float):
                    self.assertAlmostEqual(
                        current[field],
                        value,
                        delta=NUMERIC_TOLERANCE
                    )
                else:
                    self.assertEqual(current[field], value)

    def assert_step_integrity(self, entities):
        global_ids = []

        for identifier, entity in entities.items():
            for reference in step_references(entity["arguments"]):
                self.assertIn(
                    reference,
                    entities,
                    f"Dangling STEP reference #{identifier} -> #{reference}"
                )

            arguments = split_step_arguments(entity["arguments"])

            if arguments and re.fullmatch(
                r"'[0-3][0-9A-Za-z_$]{21}'",
                arguments[0]
            ):
                global_ids.append(arguments[0])

        self.assertEqual(
            len(global_ids),
            len(set(global_ids)),
            "Duplicate IFC GlobalIds"
        )

        self.assertEqual(
            len(entities_by_class(entities, "IFCPROJECT")),
            1
        )

    def assert_owner_history(self, entities, identifier):
        arguments = split_step_arguments(
            entities[identifier]["arguments"]
        )

        self.assertGreaterEqual(len(arguments), 2)

        owner_history_ids = step_references(arguments[1])

        self.assertEqual(
            len(owner_history_ids),
            1,
            f"Missing required OwnerHistory for #{identifier}"
        )

        owner_history = entities[owner_history_ids[0]]

        self.assertEqual(
            owner_history["class"],
            "IFCOWNERHISTORY"
        )

        history_arguments = split_step_arguments(
            owner_history["arguments"]
        )

        owning_user = step_references(history_arguments[0])
        owning_application = step_references(history_arguments[1])

        self.assertEqual(len(owning_user), 1)
        self.assertEqual(len(owning_application), 1)

        self.assertEqual(
            entities[owning_user[0]]["class"],
            "IFCPERSONANDORGANIZATION"
        )

        self.assertEqual(
            entities[owning_application[0]]["class"],
            "IFCAPPLICATION"
        )

        self.assertGreater(
            int(history_arguments[-1]),
            0
        )

    def assert_native_model(self):
        transforms = self.list_transforms()

        self.assertEqual(len(transforms), 4)

        self.assertEqual(
            {item["globalId"] for item in transforms},
            {
                PROJECT_GLOBAL_ID,
                BUILDING_GLOBAL_ID,
                STOREY_GLOBAL_ID,
                PRODUCT_GLOBAL_ID
            }
        )

        building = self.get_transform(self.building_id)
        storey = self.get_transform(self.storey_id)
        product = self.get_transform(self.product_id)

        self.assertEqual(
            building["parents"]["decomposition"],
            self.project_id
        )

        self.assertEqual(
            storey["parents"]["decomposition"],
            self.building_id
        )

        self.assertEqual(
            storey["parents"]["placement"],
            self.building_id
        )

        self.assertEqual(
            product["parents"]["spatial"],
            self.storey_id
        )

        self.assertEqual(
            product["parents"]["placement"],
            self.storey_id
        )

        self.assertEqual(
            product["definition"]["ifcClass"],
            "IfcBuildingElementProxy"
        )

        self.assert_matrix(
            product["transforms"]["local"],
            IDENTITY_MATRIX
        )

        self.assert_matrix(
            product["transforms"]["world"],
            IDENTITY_MATRIX
        )

        self.assert_properties(
            product["properties"],
            [
                property_input(
                    "Reference",
                    "SCHEMA-PRODUCT",
                    property_set="Pset_Native"
                )
            ]
        )

        geometries = self.list_geometries()

        self.assertEqual(len(geometries), 1)

        geometry = self.get_geometry(geometries[0]["id"])

        self.assertEqual(geometry["parentId"], self.product_id)
        self.assertEqual(geometry["source"], "ifc")
        self.assertEqual(geometry["kind"], "SOLID")
        self.assertEqual(geometry["ifcClass"], "IfcExtrudedAreaSolid")
        self.assertIs(geometry["editable"], False)

        self.assertEqual(self.list_materials(), [])
        self.assertEqual(self.list_geometries(kind="DOMAIN"), [])

        local_mesh = self.tessellate(space="local")
        world_mesh = self.tessellate(space="world")

        self.assertGreater(local_mesh["vertexCount"], 0)
        self.assertGreater(local_mesh["indexCount"], 0)

        self.assert_bounds(local_mesh, BASE_BOUNDS)
        self.assert_volume(local_mesh, BASE_VOLUME)

        self.assert_mesh_equivalent(local_mesh, world_mesh)

        self.assert_empty_mesh(
            self.tessellate(kind="DOMAIN")
        )

        return local_mesh

    def assert_geometry_definition(
        self,
        geometry,
        expected_operation,
        expected_kind,
        expected_profile,
        expected_depth,
        expected_matrix
    ):
        self.assertEqual(geometry["operation"], expected_operation)
        self.assertEqual(geometry["kind"], expected_kind)
        self.assertEqual(geometry["source"], "editor")
        self.assertIs(geometry["editable"], True)

        self.assertEqual(
            len(geometry["profile"]),
            len(expected_profile)
        )

        for actual, expected in zip(
            geometry["profile"],
            expected_profile
        ):
            self.assertEqual(len(actual), 2)

            for value, reference in zip(actual, expected):
                self.assertAlmostEqual(
                    value,
                    reference,
                    delta=NUMERIC_TOLERANCE
                )

        self.assertAlmostEqual(
            geometry["depth"],
            expected_depth,
            delta=NUMERIC_TOLERANCE
        )

        self.assert_matrix(
            geometry["transforms"]["local"],
            expected_matrix
        )

        expected_class = (
            "IfcSpatialZone"
            if expected_kind == "DOMAIN"
            else "IfcProjectionElement"
            if expected_operation == "ADD"
            else "IfcOpeningElement"
        )

        self.assertEqual(
            geometry["ifcClass"],
            expected_class
        )

    def assert_feature_relation(self, entities, geometry):
        feature_id, feature = entity_by_global_id(
            entities,
            geometry["globalId"]
        )

        parent = self.get_transform(geometry["parentId"])

        parent_id, _ = entity_by_global_id(
            entities,
            parent["globalId"]
        )

        relation_class = (
            "IFCRELASSIGNSTOPRODUCT"
            if geometry["kind"] == "DOMAIN"
            else "IFCRELPROJECTSELEMENT"
            if geometry["operation"] == "ADD"
            else "IFCRELVOIDSELEMENT"
        )

        relations = [
            (identifier, entity)
            for identifier, entity in entities_by_class(
                entities,
                relation_class
            ).items()
            if feature_id in step_references(entity["arguments"])
        ]

        self.assertEqual(len(relations), 1)

        relation_id, relation = relations[0]

        arguments = split_step_arguments(
            relation["arguments"]
        )

        if geometry["kind"] == "DOMAIN":
            self.assertEqual(
                arguments[2],
                "'ifc-cli:domain-parent'"
            )

            self.assertEqual(
                arguments[-1],
                f"#{parent_id}"
            )

            self.assertIn(
                feature_id,
                step_references(arguments[-3])
            )

            feature_arguments = split_step_arguments(
                feature["arguments"]
            )

            self.assertEqual(
                feature_arguments[4],
                "'ifc-cli:domain'"
            )

            self.assertEqual(
                feature_arguments[-1],
                ".USERDEFINED."
            )

        else:
            self.assertEqual(
                arguments[-2],
                f"#{parent_id}"
            )

            self.assertEqual(
                arguments[-1],
                f"#{feature_id}"
            )

        if is_ifc2x3(self.schema):
            self.assert_owner_history(entities, feature_id)
            self.assert_owner_history(entities, relation_id)

    def assert_domain_schema(self, schema):
        self.load_schema(schema)

        baseline = self.assert_native_model()

        profile = rectangular_profile(5.0, 3.0)
        matrix = translation_matrix(2.0, 3.0, 4.0)

        domain = self.create_domain(
            name=f"{schema} simulation domain",
            profile=profile,
            depth=2.0,
            matrix=matrix
        )

        self.assert_geometry_definition(
            domain,
            expected_operation="ADD",
            expected_kind="DOMAIN",
            expected_profile=profile,
            expected_depth=2.0,
            expected_matrix=matrix
        )

        self.assertEqual(domain["materials"], [])

        self.assertEqual(
            len(self.list_geometries(kind="SOLID")),
            1
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            1
        )

        self.assertEqual(
            len(self.list_transforms()),
            4
        )

        self.assertNotIn(
            domain["globalId"],
            {
                item["globalId"]
                for item in self.list_transforms()
            }
        )

        domain_mesh = self.tessellate_domain(domain)

        self.assert_bounds(
            domain_mesh,
            (
                2.0, 3.0, 4.0,
                7.0, 6.0, 6.0
            )
        )

        self.assert_volume(domain_mesh, 30.0)
        self.assertEqual(domain_mesh["groups"], [])

        self.assert_mesh_equivalent(
            domain_mesh,
            self.tessellate(kind="DOMAIN")
        )

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate(kind="SOLID")
        )

        entities = self.save_entities()

        self.assertEqual(
            len(entities_by_class(entities, "IFCSPATIALZONE")),
            1
        )

        self.assert_feature_relation(entities, domain)

        domain_step_id, domain_entity = entity_by_global_id(
            entities,
            domain["globalId"]
        )

        self.assertEqual(
            domain_entity["class"],
            "IFCSPATIALZONE"
        )

        domain_arguments = split_step_arguments(
            domain_entity["arguments"]
        )

        domain_placement_id = step_references(
            domain_arguments[5]
        )[0]

        parent_step_id, parent_entity = entity_by_global_id(
            entities,
            PRODUCT_GLOBAL_ID
        )

        parent_arguments = split_step_arguments(
            parent_entity["arguments"]
        )

        parent_placement_id = step_references(
            parent_arguments[5]
        )[0]

        domain_placement_arguments = split_step_arguments(
            entities[domain_placement_id]["arguments"]
        )

        self.assertEqual(
            domain_placement_arguments[0],
            f"#{parent_placement_id}"
        )

        self.save_and_reload()

        reloaded = self.find_geometry(domain["globalId"])

        self.assert_geometry_definition(
            reloaded,
            expected_operation="ADD",
            expected_kind="DOMAIN",
            expected_profile=profile,
            expected_depth=2.0,
            expected_matrix=matrix
        )

        self.assertEqual(
            reloaded["parentId"],
            self.product_id
        )

        self.assert_mesh_equivalent(
            domain_mesh,
            self.tessellate_domain(reloaded)
        )

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate()
        )

        updated = self.client.request(
            "geometryUpdate",
            {
                "id": reloaded["id"],
                "depth": 4.0
            }
        )

        self.assert_volume(
            self.tessellate_domain(updated),
            60.0
        )

        self.save_and_reload()

        updated = self.find_geometry(domain["globalId"])

        self.assertAlmostEqual(
            updated["depth"],
            4.0,
            delta=NUMERIC_TOLERANCE
        )

        self.assert_volume(
            self.tessellate_domain(updated),
            60.0
        )

        self.assert_feature_relation(
            self.save_entities(),
            updated
        )

        self.assertEqual(
            self.client.request(
                "geometryDelete",
                {"id": updated["id"]}
            ),
            {"id": updated["id"]}
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assert_empty_mesh(
            self.tessellate(kind="DOMAIN")
        )

        self.save_and_reload()

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate()
        )

        entities = self.save_entities()

        self.assertNotIn(
            domain_step_id,
            {
                identifier
                for identifier, entity in entities.items()
                if entity["class"] == "IFCSPATIALZONE"
            }
        )

    def test_supported_schemas(self):
        """Verify that every advertised schema can load a valid IFC model."""

        schemas = self.capabilities["supportedSchemas"]

        self.assertIsInstance(schemas, list)
        self.assertTrue(schemas)

        for schema in schemas:
            self.assertIsInstance(schema, str)
            self.assertTrue(schema.strip())

            self.assertRegex(
                schema.upper(),
                r"^IFC[A-Z0-9_]+$"
            )

        self.assertEqual(
            len(schemas),
            len({schema.upper() for schema in schemas})
        )

        self.assertEqual(
            schemas,
            sorted(schemas)
        )

        self.assertIn("IFC4", self.supported_schemas)

        self.assertNotIn(
            UNSUPPORTED_SCHEMA,
            self.supported_schemas
        )

        self.assertEqual(
            set(self.capabilities["supportedGeometryKinds"]),
            {"SOLID", "DOMAIN"}
        )

        self.assertEqual(
            set(self.capabilities["supportedGeometryOperations"]),
            {"ADD", "SUBTRACT"}
        )

        loaded_schemas = set()

        for schema in sorted(self.supported_schemas):
            with self.subTest(schema=schema):
                result = self.load_schema(schema)

                self.assertEqual(
                    result["schema"].upper(),
                    schema
                )

                self.assertEqual(result["transformCount"], 4)
                self.assertEqual(result["geometryCount"], 1)
                self.assertEqual(result["materialCount"], 0)

                self.assert_native_model()

                path = self.save_and_reload()

                self.assertEqual(
                    read_file_schema(
                        path.read_text(encoding="utf-8")
                    ),
                    schema
                )

                loaded_schemas.add(schema)

        self.assertEqual(
            loaded_schemas,
            set(self.supported_schemas)
        )

        self.assertEqual(
            self.client.request("getCapabilities"),
            self.capabilities
        )

    def test_ifc2x3_loading(self):
        """Verify loading of a valid IFC2X3 model with mandatory ownership."""

        schema = self.require_ifc2x3()

        result = self.load_schema(schema)

        self.assertEqual(result["schema"].upper(), "IFC2X3")
        self.assertEqual(result["transformCount"], 4)
        self.assertEqual(result["geometryCount"], 1)

        baseline = self.assert_native_model()

        entities = self.save_entities()

        self.assertEqual(
            len(entities_by_class(entities, "IFCOWNERHISTORY")),
            1
        )

        self.assertEqual(
            len(entities_by_class(entities, "IFCPERSON")),
            1
        )

        self.assertEqual(
            len(entities_by_class(entities, "IFCORGANIZATION")),
            1
        )

        self.assertEqual(
            len(entities_by_class(entities, "IFCAPPLICATION")),
            1
        )

        for global_id in (
            PROJECT_GLOBAL_ID,
            BUILDING_GLOBAL_ID,
            STOREY_GLOBAL_ID,
            PRODUCT_GLOBAL_ID
        ):
            identifier, _ = entity_by_global_id(
                entities,
                global_id
            )

            self.assert_owner_history(
                entities,
                identifier
            )

        self.save_and_reload()

        self.assertEqual(self.schema, "IFC2X3")

        self.assert_mesh_equivalent(
            baseline,
            self.assert_native_model()
        )

    def test_ifc4_loading(self):
        """Verify loading and tessellation of a valid IFC4 model."""

        self.assertIn("IFC4", self.supported_schemas)

        result = self.load_schema("IFC4")

        self.assertEqual(result["schema"].upper(), "IFC4")
        self.assertEqual(result["transformCount"], 4)
        self.assertEqual(result["geometryCount"], 1)
        self.assertEqual(result["materialCount"], 0)

        baseline = self.assert_native_model()

        entities = self.save_entities()

        _, product = entity_by_global_id(
            entities,
            PRODUCT_GLOBAL_ID
        )

        arguments = split_step_arguments(
            product["arguments"]
        )

        self.assertEqual(
            arguments[-1],
            ".NOTDEFINED."
        )

        self.assertEqual(
            arguments[3],
            "'Native description'"
        )

        self.assertEqual(
            arguments[7],
            "'PRODUCT-001'"
        )

        self.save_and_reload()

        self.assertEqual(self.schema, "IFC4")

        self.assert_mesh_equivalent(
            baseline,
            self.assert_native_model()
        )

    def test_ifc4x3_loading(self):
        """Verify loading of every advertised IFC4X3 schema variant."""

        schemas = self.require_ifc4x3()

        for schema in schemas:
            with self.subTest(schema=schema):
                result = self.load_schema(schema)

                self.assertEqual(
                    result["schema"].upper(),
                    schema
                )

                self.assertEqual(result["transformCount"], 4)
                self.assertEqual(result["geometryCount"], 1)
                self.assertEqual(result["materialCount"], 0)

                baseline = self.assert_native_model()

                path = self.save_and_reload()

                self.assertEqual(
                    read_file_schema(
                        path.read_text(encoding="utf-8")
                    ),
                    schema
                )

                self.assert_mesh_equivalent(
                    baseline,
                    self.assert_native_model()
                )

    def test_unsupported_schema(self):
        """Verify explicit rejection of a schema unsupported by the CLI."""

        self.assertNotIn(
            UNSUPPORTED_SCHEMA,
            self.supported_schemas
        )

        self.load_schema("IFC4")

        path = self.root_path / "unsupported-schema.ifc"

        path.write_text(
            build_fixture(UNSUPPORTED_SCHEMA),
            encoding="utf-8"
        )

        error = self.client.expect_error(
            "load",
            {"path": str(path)},
            "UNSUPPORTED_SCHEMA"
        )

        self.assertEqual(
            error["details"]["path"],
            str(path)
        )

        # A failed load clears the previous session.

        for command, params in (
            ("transformGetAll", {}),
            (
                "geometryGetAll",
                {
                    "transformId": None,
                    "kind": "ALL"
                }
            ),
            ("materialGetAll", {}),
            (
                "save",
                {
                    "path": str(
                        self.root_path / "invalid-save.ifc"
                    )
                }
            )
        ):
            with self.subTest(command=command):
                self.client.expect_error(
                    command,
                    params,
                    "NO_MODEL_LOADED"
                )

        self.assertEqual(
            self.client.request("getCapabilities"),
            self.capabilities
        )

        # Loading a supported schema must restore the session.

        self.load_schema("IFC4")

        self.assert_native_model()

    def test_ifc2x3_transform(self):
        """Verify transform creation, editing and persistence in IFC2X3."""

        self.load_schema(self.require_ifc2x3())

        properties = [
            property_input(
                "Reference",
                "IFC2X3-CREATED"
            ),
            property_input(
                "Count",
                42,
                value_type="IfcInteger"
            )
        ]

        matrix = translation_matrix(10.0, 20.0, 30.0)

        created = self.create_transform(
            name="IFC2X3 created transform",
            parents=parents_input(
                spatial=self.storey_id,
                placement=self.product_id,
                decomposition=self.product_id
            ),
            matrix=matrix,
            properties=properties
        )

        self.assertEqual(
            created["definition"]["ifcClass"],
            "IfcBuildingElementProxy"
        )

        self.assertEqual(
            created["parents"],
            parents_input(
                spatial=self.storey_id,
                placement=self.product_id,
                decomposition=self.product_id
            )
        )

        self.assert_matrix(
            created["transforms"]["local"],
            matrix
        )

        self.assert_matrix(
            created["transforms"]["world"],
            matrix
        )

        self.assert_properties(
            created["properties"],
            properties
        )

        self.assertEqual(created["geometries"], [])
        self.assertEqual(created["materials"], [])

        self.assertEqual(
            len(self.list_transforms()),
            5
        )

        entities = self.save_entities()

        identifier, entity = entity_by_global_id(
            entities,
            created["globalId"]
        )

        self.assertEqual(
            entity["class"],
            "IFCBUILDINGELEMENTPROXY"
        )

        self.assert_owner_history(
            entities,
            identifier
        )

        arguments = split_step_arguments(
            entity["arguments"]
        )

        self.assertEqual(len(arguments), 9)

        # IFC2X3 uses CompositionType instead of PredefinedType.
        # CompositionType is optional for IfcBuildingElementProxy.

        self.assertIn(
            arguments[-1],
            {
                "$",
                ".ELEMENT.",
                ".COMPLEX.",
                ".PARTIAL."
            }
        )

        for relation_class in (
            "IFCRELAGGREGATES",
            "IFCRELCONTAINEDINSPATIALSTRUCTURE",
            "IFCRELDEFINESBYPROPERTIES"
        ):
            relations = [
                relation_id
                for relation_id, relation in entities_by_class(
                    entities,
                    relation_class
                ).items()
                if identifier in step_references(
                    relation["arguments"]
                )
            ]

            self.assertTrue(relations)

            for relation_id in relations:
                self.assert_owner_history(
                    entities,
                    relation_id
                )

        self.save_and_reload()

        current = self.find_transform(created["globalId"])

        self.assertEqual(
            current["name"],
            created["name"]
        )

        self.assert_properties(
            current["properties"],
            properties
        )

        self.assert_matrix(
            current["transforms"]["world"],
            matrix
        )

        updated_matrix = translation_matrix(100.0, 200.0, 300.0)

        updated = self.client.request(
            "transformUpdate",
            {
                "id": current["id"],
                "name": "Updated IFC2X3 transform",
                "transform": transform_input(
                    updated_matrix,
                    space="world"
                )
            }
        )

        self.assert_matrix(
            updated["transforms"]["world"],
            updated_matrix
        )

        self.save_and_reload()

        current = self.find_transform(created["globalId"])

        self.assertEqual(
            current["name"],
            "Updated IFC2X3 transform"
        )

        self.assert_matrix(
            current["transforms"]["world"],
            updated_matrix
        )

        self.assert_properties(
            current["properties"],
            properties
        )

        self.assertEqual(self.schema, "IFC2X3")

    def test_ifc2x3_geometry(self):
        """Verify supported physical geometry operations in IFC2X3."""

        self.load_schema(self.require_ifc2x3())

        baseline = self.tessellate()

        self.assert_volume(baseline, BASE_VOLUME)

        specifications = [
            {
                "operation": "ADD",
                "profile": rectangular_profile(2.0, 1.0),
                "depth": 3.0,
                "matrix": translation_matrix(1.0, 0.0, 1.0)
            },
            {
                "operation": "SUBTRACT",
                "profile": rectangular_profile(
                    1.0,
                    1.0,
                    x=-0.5,
                    y=-0.5
                ),
                "depth": 4.0,
                "matrix": translation_matrix(0.0, 0.0, -1.0)
            }
        ]

        geometries = []

        for specification in specifications:
            operation = specification["operation"]

            with self.subTest(operation=operation):
                geometry = self.create_geometry(
                    name=f"IFC2X3 {operation} feature",
                    **specification
                )

                geometries.append(geometry)

                self.assert_geometry_definition(
                    geometry,
                    expected_operation=operation,
                    expected_kind="SOLID",
                    expected_profile=specification["profile"],
                    expected_depth=specification["depth"],
                    expected_matrix=specification["matrix"]
                )

                self.assert_feature_relation(
                    self.save_entities(),
                    geometry
                )

        self.assertEqual(len(geometries), 2)

        self.assertEqual(
            len(self.list_geometries(kind="SOLID")),
            3
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            0
        )

        self.save_and_reload()

        for geometry, specification in zip(
            geometries,
            specifications
        ):
            current = self.find_geometry(
                geometry["globalId"]
            )

            self.assert_geometry_definition(
                current,
                expected_operation=specification["operation"],
                expected_kind="SOLID",
                expected_profile=specification["profile"],
                expected_depth=specification["depth"],
                expected_matrix=specification["matrix"]
            )

            self.assert_feature_relation(
                self.save_entities(),
                current
            )

        addition = self.find_geometry(
            geometries[0]["globalId"]
        )

        subtraction = self.find_geometry(
            geometries[1]["globalId"]
        )

        # Isolate the subtractive operation from additive behavior.

        self.client.request(
            "geometryDelete",
            {"id": addition["id"]}
        )

        self.assert_volume(
            self.tessellate(),
            22.0
        )

        self.assert_bounds(
            self.tessellate(),
            BASE_BOUNDS
        )

        updated = self.client.request(
            "geometryUpdate",
            {
                "id": subtraction["id"],
                "depth": 1.0,
                "transform": transform_input(
                    translation_matrix(0.0, 0.0, 1.0)
                )
            }
        )

        self.assertEqual(
            updated["operation"],
            "SUBTRACT"
        )

        self.assert_volume(
            self.tessellate(),
            23.0
        )

        self.save_and_reload()

        current = self.find_geometry(
            subtraction["globalId"]
        )

        self.assert_volume(
            self.tessellate(),
            23.0
        )

        self.client.request(
            "geometryDelete",
            {"id": current["id"]}
        )

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate()
        )

        self.save_and_reload()

        self.assert_native_model()

        self.assertEqual(self.schema, "IFC2X3")

    def test_ifc2x3_material(self):
        """Verify IFC2X3-compatible material creation and assignments."""

        self.load_schema(self.require_ifc2x3())

        properties = [
            property_input(
                "Density",
                7850.0,
                value_type="IfcReal",
                property_set="Pset_Physical",
                source="material"
            ),
            property_input(
                "Reference",
                "IFC2X3-STEEL",
                property_set="Pset_Physical",
                source="material"
            )
        ]

        material = self.create_material(
            name="IFC2X3 steel",
            category="metal",
            visual=DEFAULT_VISUAL,
            properties=properties
        )

        self.assertEqual(
            material["definition"]["ifcClass"],
            "IfcMaterial"
        )

        self.assertEqual(
            material["name"],
            "IFC2X3 steel"
        )

        self.assertEqual(
            material["category"],
            "metal"
        )

        self.assertIs(material["editable"], True)

        self.assert_visual(
            material["visual"],
            DEFAULT_VISUAL
        )

        self.assert_properties(
            material["properties"],
            properties
        )

        self.assign_material(
            material["id"],
            self.product_id
        )

        self.assertEqual(
            self.get_transform(self.product_id)["materials"],
            [material["id"]]
        )

        entities = self.save_entities()

        materials = entities_by_class(
            entities,
            "IFCMATERIAL"
        )

        self.assertEqual(len(materials), 1)

        material_step_id, native_material = next(
            iter(materials.items())
        )

        material_arguments = split_step_arguments(
            native_material["arguments"]
        )

        # IFC2X3 IfcMaterial contains only its Name attribute.

        self.assertEqual(
            material_arguments,
            ["'IFC2X3 steel'"]
        )

        containers = entities_by_class(
            entities,
            "IFCEXTENDEDMATERIALPROPERTIES"
        )

        self.assertTrue(
            containers,
            "IFC2X3 must use IfcExtendedMaterialProperties"
        )

        container_names = {
            split_step_arguments(
                entity["arguments"]
            )[-1]
            for entity in containers.values()
            if material_step_id in step_references(
                entity["arguments"]
            )
        }

        self.assertIn(
            "'Pset_IfcEditorMaterial'",
            container_names
        )

        self.assertIn(
            "'Pset_IfcEditorVisual'",
            container_names
        )

        self.assertIn(
            "'Pset_Physical'",
            container_names
        )

        self.assertTrue(
            entities_by_class(
                entities,
                "IFCSURFACESTYLERENDERING"
            )
        )

        self.assertTrue(
            entities_by_class(
                entities,
                "IFCPRESENTATIONSTYLEASSIGNMENT"
            )
        )

        product_step_id, _ = entity_by_global_id(
            entities,
            PRODUCT_GLOBAL_ID
        )

        relations = [
            (identifier, entity)
            for identifier, entity in entities_by_class(
                entities,
                "IFCRELASSOCIATESMATERIAL"
            ).items()
            if split_step_arguments(
                entity["arguments"]
            )[-1] == f"#{material_step_id}"
        ]

        self.assertEqual(len(relations), 1)

        relation_id, relation = relations[0]

        relation_arguments = split_step_arguments(
            relation["arguments"]
        )

        self.assertEqual(
            step_references(relation_arguments[-2]),
            [product_step_id]
        )

        self.assert_owner_history(
            entities,
            relation_id
        )

        self.save_and_reload()

        current = self.find_material("IFC2X3 steel")

        self.assertIs(current["editable"], True)

        self.assertEqual(
            current["category"],
            "metal"
        )

        self.assert_visual(
            current["visual"],
            DEFAULT_VISUAL
        )

        self.assert_properties(
            current["properties"],
            properties
        )

        self.assertEqual(
            self.get_transform(self.product_id)["materials"],
            [current["id"]]
        )

        mesh = self.tessellate()

        self.assert_volume(mesh, BASE_VOLUME)

        self.assertEqual(
            mesh["groups"],
            [{
                "materialId": current["id"],
                "indexOffset": 0,
                "indexCount": mesh["indexCount"]
            }]
        )

        updated_visual = {
            "color": [0.2, 0.3, 0.4],
            "opacity": 1.0,
            "metallic": 0.9,
            "roughness": 0.15
        }

        self.client.request(
            "materialUpdate",
            {
                "id": current["id"],
                "name": "Updated IFC2X3 steel",
                "category": "steel",
                "visual": updated_visual
            }
        )

        self.save_and_reload()

        current = self.find_material(
            "Updated IFC2X3 steel"
        )

        self.assertEqual(
            current["category"],
            "steel"
        )

        self.assert_visual(
            current["visual"],
            updated_visual
        )

        self.assert_properties(
            current["properties"],
            properties
        )

        self.assertEqual(
            self.get_transform(self.product_id)["materials"],
            [current["id"]]
        )

        self.assertEqual(self.schema, "IFC2X3")

    def test_ifc2x3_domain_rejection(self):
        """Verify explicit rejection of managed domains in IFC2X3."""

        self.load_schema(self.require_ifc2x3())

        baseline = self.tessellate()

        transforms_before = self.list_transforms()
        geometries_before = self.list_geometries()
        materials_before = self.list_materials()

        entities_before = self.save_entities()

        params = self.geometry_parameters(
            name="Unsupported IFC2X3 domain",
            kind="DOMAIN",
            operation="ADD",
            profile=rectangular_profile(5.0, 3.0),
            depth=2.0
        )

        for iteration in range(3):
            with self.subTest(iteration=iteration):
                error = self.client.expect_error(
                    "geometryCreate",
                    params,
                    "GEOMETRY_OPERATION_NOT_SUPPORTED"
                )

                self.assertEqual(
                    error["details"].get("ifcClass"),
                    "IfcSpatialZone"
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
                    self.list_materials(),
                    materials_before
                )

                self.assert_mesh_equivalent(
                    baseline,
                    self.tessellate()
                )

                self.assert_empty_mesh(
                    self.tessellate(kind="DOMAIN")
                )

        entities_after = self.save_entities()

        self.assertEqual(
            entities_after,
            entities_before,
            "Rejected domain creation modified the IFC model"
        )

        self.assertEqual(
            entities_by_class(
                entities_after,
                "IFCSPATIALZONE"
            ),
            {}
        )

        domain_relations = [
            entity
            for entity in entities_by_class(
                entities_after,
                "IFCRELASSIGNSTOPRODUCT"
            ).values()
            if "'ifc-cli:domain-parent'" in entity["arguments"]
        ]

        self.assertEqual(domain_relations, [])

        self.save_and_reload()

        self.assertEqual(self.schema, "IFC2X3")

        self.assert_native_model()

        # Rejection must not disable supported physical operations.

        opening = self.create_geometry(
            name="IFC2X3 recovery opening",
            operation="SUBTRACT",
            profile=rectangular_profile(
                1.0,
                1.0,
                x=-0.5,
                y=-0.5
            ),
            depth=4.0,
            matrix=translation_matrix(0.0, 0.0, -1.0)
        )

        self.assert_volume(
            self.tessellate(),
            22.0
        )

        self.client.request(
            "geometryDelete",
            {"id": opening["id"]}
        )

        self.assert_mesh_equivalent(
            baseline,
            self.tessellate()
        )

    def test_ifc4_domain(self):
        """Verify managed domain creation and persistence in IFC4."""

        self.assertIn("IFC4", self.supported_schemas)

        self.assert_domain_schema("IFC4")

        self.assertEqual(
            self.schema,
            "IFC4"
        )

    def test_ifc4x3_domain(self):
        """Verify managed domain support in every advertised IFC4X3 variant."""

        schemas = self.require_ifc4x3()

        for schema in schemas:
            with self.subTest(schema=schema):
                self.assert_domain_schema(schema)

                self.assertEqual(
                    self.schema,
                    schema
                )

    def test_schema_preservation(self):
        """Verify schema preservation after unmodified and modified roundtrips."""

        self.assertTrue(self.supported_schemas)

        for schema in sorted(self.supported_schemas):
            with self.subTest(schema=schema):
                self.load_schema(schema)

                baseline = self.assert_native_model()

                # First verify an unmodified roundtrip.

                path = self.save_and_reload()

                self.assertEqual(
                    read_file_schema(
                        path.read_text(encoding="utf-8")
                    ),
                    schema
                )

                self.assertEqual(
                    self.schema,
                    schema
                )

                self.assert_mesh_equivalent(
                    baseline,
                    self.tessellate()
                )

                # Modify an existing entity and create a new transform.

                name = f"Updated {schema} product"

                self.client.request(
                    "transformUpdate",
                    {
                        "id": self.product_id,
                        "name": name
                    }
                )

                created = self.create_transform(
                    name=f"Persistent {schema} transform",
                    parents=parents_input(
                        spatial=self.storey_id,
                        placement=self.product_id
                    ),
                    matrix=translation_matrix(
                        10.0,
                        20.0,
                        30.0
                    ),
                    properties=[
                        property_input(
                            "OriginalSchema",
                            schema
                        )
                    ]
                )

                expected_transform_count = len(
                    self.list_transforms()
                )

                expected_geometry_count = len(
                    self.list_geometries()
                )

                expected_global_ids = {
                    item["globalId"]
                    for item in self.list_transforms()
                }

                for iteration in range(3):
                    with self.subTest(roundtrip=iteration):
                        path = self.save_and_reload()

                        content = path.read_text(
                            encoding="utf-8"
                        )

                        self.assertEqual(
                            read_file_schema(content),
                            schema
                        )

                        self.assertEqual(
                            self.schema,
                            schema
                        )

                        self.assertEqual(
                            len(self.list_transforms()),
                            expected_transform_count
                        )

                        self.assertEqual(
                            len(self.list_geometries()),
                            expected_geometry_count
                        )

                        self.assertEqual(
                            {
                                item["globalId"]
                                for item in self.list_transforms()
                            },
                            expected_global_ids
                        )

                        current_product = self.find_transform(
                            PRODUCT_GLOBAL_ID
                        )

                        self.assertEqual(
                            current_product["name"],
                            name
                        )

                        current_transform = self.find_transform(
                            created["globalId"]
                        )

                        self.assertEqual(
                            current_transform["name"],
                            created["name"]
                        )

                        self.assert_properties(
                            current_transform["properties"],
                            [
                                property_input(
                                    "OriginalSchema",
                                    schema
                                )
                            ]
                        )

                        self.assert_matrix(
                            current_transform["transforms"]["world"],
                            translation_matrix(
                                10.0,
                                20.0,
                                30.0
                            )
                        )

                        self.assertEqual(
                            current_transform["parents"]["placement"],
                            self.product_id
                        )

                        self.assertEqual(
                            current_transform["parents"]["spatial"],
                            self.storey_id
                        )

                        self.assert_mesh_equivalent(
                            baseline,
                            self.tessellate()
                        )

                        entities = parse_step_entities(content)

                        self.assert_step_integrity(entities)

                        _, product_entity = entity_by_global_id(
                            entities,
                            PRODUCT_GLOBAL_ID
                        )

                        product_arguments = split_step_arguments(
                            product_entity["arguments"]
                        )

                        self.assertEqual(
                            product_arguments[-1],
                            ".ELEMENT."
                            if is_ifc2x3(schema)
                            else ".NOTDEFINED."
                        )

                        if is_ifc2x3(schema):
                            identifier, _ = entity_by_global_id(
                                entities,
                                created["globalId"]
                            )

                            self.assert_owner_history(
                                entities,
                                identifier
                            )

        # Loading different schemas must not modify capabilities.

        self.assertEqual(
            self.client.request("getCapabilities"),
            self.capabilities
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