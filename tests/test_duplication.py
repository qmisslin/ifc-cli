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
# Transform and geometry copies must have independent IFC identities.
# Materials remain shared between source entities and their copies.
# Internal parent references must be remapped, while external parent
# references must continue to reference their original entities.
#
# Boolean tessellation is compared between sources and copies.
# Analytical boolean correctness is covered by test_boolean_operations.py.
#
# Preservation of complex occurrence properties is intentionally tested
# as a regression requirement. The current duplication implementation
# may omit complex properties from exclusively owned property sets.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 60
NUMERIC_TOLERANCE = 1e-7
MESH_TOLERANCE = 1e-6

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

ROOT_GLOBAL_ID = "0000000000000000000001"
CHILD_GLOBAL_ID = "0000000000000000000002"
GRANDCHILD_GLOBAL_ID = "0000000000000000000003"
UNRELATED_GLOBAL_ID = "0000000000000000000004"
ANCHOR_GLOBAL_ID = "0000000000000000000005"
STOREY_GLOBAL_ID = "0000000000000000000006"
PROJECT_GLOBAL_ID = "0000000000000000000007"

GLOBAL_ID_PATTERN = re.compile(r"^[0-3][0-9A-Za-z_$]{21}$")

PARENT_FIELDS = (
    "spatial",
    "placement",
    "decomposition"
)

DEFAULT_PROFILE = [
    [0, 0],
    [2, 0],
    [2, 3],
    [0, 3]
]

DEFAULT_VISUAL = {
    "color": [0.4, 0.6, 0.8],
    "opacity": 1.0,
    "metallic": 0.2,
    "roughness": 0.5
}

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('duplication-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#8=IFCPROJECT('0000000000000000000007',$,'Duplication test project',$,$,$,$,(#5),#7);
#9=IFCCARTESIANPOINT((10.,20.,30.));
#10=IFCAXIS2PLACEMENT3D(#9,#2,#3);
#11=IFCLOCALPLACEMENT($,#10);
#12=IFCBUILDINGELEMENTPROXY('0000000000000000000005',$,'External anchor',$,$,#11,$,$,.NOTDEFINED.);
#13=IFCCARTESIANPOINT((2.,3.,4.));
#14=IFCAXIS2PLACEMENT3D(#13,#2,#3);
#15=IFCLOCALPLACEMENT(#11,#14);
#16=IFCCARTESIANPOINT((0.,0.));
#17=IFCAXIS2PLACEMENT2D(#16,$);
#18=IFCRECTANGLEPROFILEDEF(.AREA.,$,#17,2.,3.);
#19=IFCEXTRUDEDAREASOLID(#18,#4,#2,4.);
#20=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#19));
#21=IFCPRODUCTDEFINITIONSHAPE($,$,(#20));
#22=IFCBUILDINGELEMENTPROXY('0000000000000000000001',$,'Root product','Native description','Native object type',#15,#21,'ROOT-TAG',.NOTDEFINED.);
#23=IFCCARTESIANPOINT((10.,0.,0.));
#24=IFCAXIS2PLACEMENT3D(#23,#2,#3);
#25=IFCLOCALPLACEMENT(#15,#24);
#26=IFCEXTRUDEDAREASOLID(#18,#4,#2,4.);
#27=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#26));
#28=IFCPRODUCTDEFINITIONSHAPE($,$,(#27));
#29=IFCBUILDINGELEMENTPROXY('0000000000000000000002',$,'Child product',$,$,#25,#28,$,.NOTDEFINED.);
#30=IFCCARTESIANPOINT((0.,10.,0.));
#31=IFCAXIS2PLACEMENT3D(#30,#2,#3);
#32=IFCLOCALPLACEMENT(#25,#31);
#33=IFCEXTRUDEDAREASOLID(#18,#4,#2,4.);
#34=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#33));
#35=IFCPRODUCTDEFINITIONSHAPE($,$,(#34));
#36=IFCBUILDINGELEMENTPROXY('0000000000000000000003',$,'Grandchild product',$,$,#32,#35,$,.NOTDEFINED.);
#37=IFCCARTESIANPOINT((100.,100.,100.));
#38=IFCAXIS2PLACEMENT3D(#37,#2,#3);
#39=IFCLOCALPLACEMENT($,#38);
#40=IFCEXTRUDEDAREASOLID(#18,#4,#2,4.);
#41=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#40));
#42=IFCPRODUCTDEFINITIONSHAPE($,$,(#41));
#43=IFCBUILDINGELEMENTPROXY('0000000000000000000004',$,'Unrelated product',$,$,#39,#42,$,.NOTDEFINED.);
#44=IFCLOCALPLACEMENT($,#4);
#45=IFCBUILDINGSTOREY('0000000000000000000006',$,'External storey',$,$,#44,$,$,.ELEMENT.,0.);
#46=IFCRELCONTAINEDINSPATIALSTRUCTURE('0000000000000000000008',$,$,$,(#22,#29,#36),#45);
#47=IFCRELAGGREGATES('0000000000000000000009',$,$,$,#12,(#22));
#48=IFCRELAGGREGATES('0000000000000000000010',$,$,$,#22,(#29,#36));
#49=IFCPROPERTYSINGLEVALUE('Reference',$,IFCLABEL('NATIVE-ROOT'),$);
#50=IFCPROPERTYSET('0000000000000000000011',$,'Pset_Native',$,(#49));
#51=IFCRELDEFINESBYPROPERTIES('0000000000000000000012',$,$,$,(#22),#50);
#52=IFCQUANTITYLENGTH('NativeLength',$,#6,2.,$);
#53=IFCELEMENTQUANTITY('0000000000000000000013',$,'Qto_Native',$,$,(#52));
#54=IFCRELDEFINESBYPROPERTIES('0000000000000000000014',$,$,$,(#22),#53);
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


def transform_input(matrix=None, space="parent"):
    return {
        "space": space,
        "matrix": translation_matrix() if matrix is None else matrix
    }


def parents_input(spatial=None, placement=None, decomposition=None):
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


def property_input(
    name,
    value,
    value_type="IfcLabel",
    property_set="Pset_Duplication",
    unit=None
):
    return {
        "propertySet": property_set,
        "name": name,
        "valueType": value_type,
        "value": value,
        "unit": unit,
        "source": "occurrence"
    }


def property_signature(properties):
    return sorted(
        json.dumps(
            item,
            sort_keys=True,
            ensure_ascii=False
        )
        for item in properties
    )


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
        function(point[axis] for point in points)
        for function in (min, max)
        for axis in range(3)
    )


def mesh_signature(mesh, precision=7):
    return Counter(
        tuple(sorted(
            tuple(round(value, precision) for value in point)
            for point in triangle
        ))
        for triangle in mesh_triangles(mesh)
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


def mesh_surface_area(mesh):
    terms = []

    for a, b, c in mesh_triangles(mesh):
        u = tuple(b[axis] - a[axis] for axis in range(3))
        v = tuple(c[axis] - a[axis] for axis in range(3))

        cross = (
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0]
        )

        terms.append(
            math.sqrt(
                math.fsum(value * value for value in cross)
            ) / 2.0
        )

    return math.fsum(terms)


def mesh_edge_counts(mesh, precision=7):
    edges = Counter()

    for triangle in mesh_triangles(mesh):
        a, b, c = [
            tuple(round(value, precision) for value in point)
            for point in triangle
        ]

        if len({a, b, c}) != 3:
            raise AssertionError(
                f"Degenerate triangle: {triangle}"
            )

        for first, second in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted((first, second)))] += 1

    return edges


def parse_step_entities(content):
    # Targeted STEP parser for the controlled IFC fixture.
    # This is not a general-purpose STEP parser.

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


def step_global_ids(entities):
    result = []

    for entity in entities.values():
        arguments = split_step_arguments(entity["arguments"])

        if not arguments:
            continue

        first = arguments[0]

        if not first.startswith("'") or not first.endswith("'"):
            continue

        value = first[1:-1]

        if GLOBAL_ID_PATTERN.fullmatch(value):
            result.append(value)

    return result


def build_complex_property_fixture():
    fixture = IFC_FIXTURE.replace(
        "#50=IFCPROPERTYSET('0000000000000000000011',$,'Pset_Native',$,(#49));",
        "#50=IFCPROPERTYSET('0000000000000000000011',$,'Pset_Native',$,(#49,#56));"
    )

    entities = [
        "#55=IFCPROPERTYSINGLEVALUE('ComplexReference',$,IFCLABEL('COMPLEX-NATIVE-VALUE'),$);",
        "#56=IFCCOMPLEXPROPERTY('NativeComplexProperty',$,'DuplicationTest',(#55));"
    ]

    return fixture.replace(
        "ENDSEC;\nEND-ISO-10303-21;",
        "\n".join(entities) + "\nENDSEC;\nEND-ISO-10303-21;"
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
        space="local",
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
            event_name = event.get("event")

            if event_name not in event_order:
                raise AssertionError(
                    f"Unexpected mesh event: {event}"
                )

            current_order = event_order[event_name]

            if current_order < last_event:
                raise AssertionError(
                    f"Invalid mesh event order: {event}"
                )

            last_event = current_order

            if event_name == "mesh.begin":
                if begin is not None:
                    raise AssertionError("Duplicate mesh.begin event")

                begin = event
                continue

            if begin is None:
                raise AssertionError(
                    "Mesh data received before mesh.begin"
                )

            if event_name == "mesh.end":
                break

            if event_name == "mesh.materials":
                if not include_materials or materials_received:
                    raise AssertionError(
                        "Unexpected mesh.materials event"
                    )

                if not isinstance(event["groups"], list):
                    raise AssertionError(event)

                materials_received = True
                mesh["groups"] = event["groups"]
                continue

            if event_name == "mesh.normals" and not include_normals:
                raise AssertionError("Unexpected mesh.normals event")

            key = event_name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(
                    f"Invalid mesh chunk offset: {event}"
                )

            if not isinstance(event["values"], list):
                raise AssertionError(event)

            mesh[key].extend(event["values"])

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

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError("Invalid triangle indices")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(
                    f"Invalid vertex index: {index}"
                )

        for value in mesh["vertices"] + mesh["normals"]:
            if type(value) not in (int, float) or not math.isfinite(value):
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


class DuplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-duplication-"
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
        self.grandchild_id = self.find_transform(GRANDCHILD_GLOBAL_ID)["id"]
        self.unrelated_id = self.find_transform(UNRELATED_GLOBAL_ID)["id"]
        self.anchor_id = self.find_transform(ANCHOR_GLOBAL_ID)["id"]
        self.storey_id = self.find_transform(STOREY_GLOBAL_ID)["id"]
        self.project_id = self.find_transform(PROJECT_GLOBAL_ID)["id"]

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

        return result

    def load_fixture(self, content, filename):
        path = self.root_path / filename

        path.write_text(content, encoding="utf-8")

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

        return self.output_path.read_text(encoding="utf-8")

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

        self.assertEqual(
            len(matches),
            1,
            f"Expected one transform with GlobalId {global_id}"
        )

        return self.get_transform(matches[0]["id"])

    def find_geometry(self, global_id):
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

        self.assertEqual(
            len(matches),
            1,
            f"Expected one material named {name}"
        )

        return matches[0]

    def native_geometries(self, parent_id):
        return [
            self.get_geometry(item["id"])
            for item in self.list_geometries(parent_id)
            if item["source"] == "ifc"
        ]

    def native_geometry(self, parent_id):
        geometries = self.native_geometries(parent_id)

        self.assertEqual(len(geometries), 1)

        return geometries[0]

    def snapshot_transforms(self):
        return {
            item["id"]: self.get_transform(item["id"])
            for item in self.list_transforms()
        }

    def snapshot_geometries(self):
        return {
            item["id"]: self.get_geometry(item["id"])
            for item in self.list_geometries()
        }

    def snapshot_materials(self):
        return {
            item["id"]: self.get_material(item["id"])
            for item in self.list_materials()
        }

    def create_transform(
        self,
        name="Created transform",
        parents=None,
        matrix=None,
        properties=None
    ):
        return self.client.request(
            "transformCreate",
            {
                "name": name,
                "parents": parents_input() if parents is None else parents,
                "transform": transform_input(matrix),
                "properties": [] if properties is None else properties
            }
        )

    def create_geometry(
        self,
        parent_id=None,
        name="Created geometry",
        kind="SOLID",
        operation="ADD",
        profile=None,
        depth=2.0,
        matrix=None
    ):
        return self.client.request(
            "geometryCreate",
            {
                "parentId": self.root_id if parent_id is None else parent_id,
                "name": name,
                "kind": kind,
                "operation": operation,
                "profile": DEFAULT_PROFILE if profile is None else profile,
                "depth": depth,
                "transform": transform_input(matrix)
            }
        )

    def create_domain(
        self,
        parent_id=None,
        name="Created domain",
        profile=None,
        depth=2.0,
        matrix=None
    ):
        return self.create_geometry(
            parent_id=parent_id,
            name=name,
            kind="DOMAIN",
            operation="ADD",
            profile=profile,
            depth=depth,
            matrix=matrix
        )

    def create_material(
        self,
        name="Shared duplication material",
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

    def unassign_material(self, material_id, target_id):
        params = {
            "materialId": material_id,
            "targetId": target_id
        }

        self.assertEqual(
            self.client.request("materialUnassign", params),
            params
        )

    def update_transform(self, identifier, **changes):
        result = self.client.request(
            "transformUpdate",
            {
                "id": identifier,
                **changes
            }
        )

        self.assertEqual(result["id"], identifier)

        return result

    def update_geometry(self, identifier, **changes):
        result = self.client.request(
            "geometryUpdate",
            {
                "id": identifier,
                **changes
            }
        )

        self.assertEqual(result["id"], identifier)

        return result

    def delete_transform(self, identifier):
        result = self.client.request(
            "transformDelete",
            {"id": identifier}
        )

        self.assertEqual(result, {"id": identifier})

        self.client.expect_error(
            "transformGet",
            {"id": identifier},
            "ENTITY_NOT_FOUND"
        )

    def delete_geometry(self, identifier):
        result = self.client.request(
            "geometryDelete",
            {"id": identifier}
        )

        self.assertEqual(result, {"id": identifier})

        self.client.expect_error(
            "geometryGet",
            {"id": identifier},
            "ENTITY_NOT_FOUND"
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

        self.assertIsInstance(result["transforms"], dict)
        self.assertIsInstance(result["geometries"], dict)

        self.assertIn(identifier, result["transforms"])

        self.assertEqual(
            result["rootId"],
            result["transforms"][identifier]
        )

        return result

    def tessellate(self, identifier, **options):
        return self.client.tessellate(identifier, **options)

    def tessellate_domain(self, geometry, **options):
        return self.client.tessellate(
            geometry["parentId"],
            kind="DOMAIN",
            geometry_id=geometry["id"],
            **options
        )

    def assert_matrix(self, actual, expected):
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertAlmostEqual(
                value,
                reference,
                delta=NUMERIC_TOLERANCE,
                msg=f"Invalid matrix component {index}"
            )

    def assert_profile(self, actual, expected):
        self.assertEqual(len(actual), len(expected))

        for point, reference in zip(actual, expected):
            self.assertEqual(len(point), 2)

            for value, expected_value in zip(point, reference):
                self.assertAlmostEqual(
                    value,
                    expected_value,
                    delta=NUMERIC_TOLERANCE
                )

    def assert_bounds(self, mesh, expected):
        actual = mesh_bounds(mesh)

        self.assertEqual(len(actual), len(expected))

        for value, reference in zip(actual, expected):
            self.assertAlmostEqual(
                value,
                reference,
                delta=MESH_TOLERANCE
            )

    def assert_mesh_equivalent(
        self,
        source,
        duplicate,
        exact_triangles=True
    ):
        self.assertAlmostEqual(
            mesh_volume(source),
            mesh_volume(duplicate),
            delta=MESH_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_surface_area(source),
            mesh_surface_area(duplicate),
            delta=MESH_TOLERANCE
        )

        if source["indexCount"] == 0:
            self.assertEqual(duplicate["vertexCount"], 0)
            self.assertEqual(duplicate["indexCount"], 0)
            self.assertEqual(duplicate["vertices"], [])
            self.assertEqual(duplicate["indices"], [])
            return

        self.assertGreater(duplicate["indexCount"], 0)

        self.assert_bounds(
            duplicate,
            mesh_bounds(source)
        )

        if exact_triangles:
            self.assertEqual(
                source["indexCount"],
                duplicate["indexCount"]
            )

            self.assertEqual(
                mesh_signature(source),
                mesh_signature(duplicate)
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
            f"Invalid mesh edges: {list(invalid_edges.items())[:10]}"
        )

    def assert_distinct_identity(self, source, duplicate):
        self.assertNotEqual(
            source["id"],
            duplicate["id"]
        )

        self.assertNotEqual(
            source["ifcEntityId"],
            duplicate["ifcEntityId"]
        )

        if source["globalId"] is None:
            self.assertIsNone(duplicate["globalId"])
        else:
            self.assertIsInstance(duplicate["globalId"], str)

            self.assertRegex(
                duplicate["globalId"],
                GLOBAL_ID_PATTERN
            )

            self.assertNotEqual(
                source["globalId"],
                duplicate["globalId"]
            )

    def assert_transform_copy(
        self,
        source,
        duplicate,
        mapping,
        is_root=False
    ):
        self.assert_distinct_identity(source, duplicate)

        expected_name = source["name"]

        if is_root:
            expected_name = (
                f"{source['name']} Copy"
                if source["name"]
                else "Copy"
            )

        self.assertEqual(
            duplicate["name"],
            expected_name
        )

        self.assertEqual(
            duplicate["definition"]["ifcClass"],
            source["definition"]["ifcClass"]
        )

        self.assertEqual(
            duplicate["definition"]["attributes"],
            source["definition"]["attributes"]
        )

        expected_parents = {
            field: mapping.get(
                source["parents"][field],
                source["parents"][field]
            )
            for field in PARENT_FIELDS
        }

        self.assertEqual(
            duplicate["parents"],
            expected_parents
        )

        self.assert_matrix(
            duplicate["transforms"]["local"],
            source["transforms"]["local"]
        )

        self.assert_matrix(
            duplicate["transforms"]["world"],
            source["transforms"]["world"]
        )

        for field in ("editable", "sourceType"):
            self.assertEqual(
                duplicate["transforms"][field],
                source["transforms"][field]
            )

        self.assertEqual(
            property_signature(duplicate["properties"]),
            property_signature(source["properties"])
        )

        self.assertEqual(
            set(duplicate["materials"]),
            set(source["materials"])
        )

    def assert_geometry_copy(
        self,
        source,
        duplicate,
        transform_mapping
    ):
        self.assert_distinct_identity(source, duplicate)

        self.assertEqual(
            duplicate["parentId"],
            transform_mapping[source["parentId"]]
        )

        for field in (
            "source",
            "editable",
            "kind",
            "operation",
            "ifcClass"
        ):
            self.assertEqual(
                duplicate[field],
                source[field]
            )

        self.assertEqual(
            duplicate["definition"]["ifcClass"],
            source["definition"]["ifcClass"]
        )

        if source["source"] == "editor":
            self.assertEqual(
                duplicate["name"],
                source["name"]
            )

            self.assert_profile(
                duplicate["profile"],
                source["profile"]
            )

            self.assertAlmostEqual(
                duplicate["depth"],
                source["depth"],
                delta=NUMERIC_TOLERANCE
            )

            self.assert_matrix(
                duplicate["transforms"]["local"],
                source["transforms"]["local"]
            )

            self.assert_matrix(
                duplicate["transforms"]["world"],
                source["transforms"]["world"]
            )

            for field in ("editable", "sourceType"):
                self.assertEqual(
                    duplicate["transforms"][field],
                    source["transforms"][field]
                )

            self.assertEqual(
                set(duplicate["materials"]),
                set(source["materials"])
            )

    def assert_duplication_mapping(
        self,
        result,
        source_transforms,
        source_geometries,
        selected_ids,
        root_id
    ):
        transform_mapping = result["transforms"]
        geometry_mapping = result["geometries"]

        selected_ids = set(selected_ids)

        self.assertEqual(
            set(transform_mapping),
            selected_ids
        )

        self.assertEqual(
            result["rootId"],
            transform_mapping[root_id]
        )

        expected_geometry_ids = {
            identifier
            for identifier, geometry in source_geometries.items()
            if geometry["parentId"] in selected_ids
        }

        self.assertEqual(
            set(geometry_mapping),
            expected_geometry_ids
        )

        self.assertEqual(
            len(transform_mapping),
            len(set(transform_mapping.values()))
        )

        self.assertEqual(
            len(geometry_mapping),
            len(set(geometry_mapping.values()))
        )

        self.assertFalse(
            set(transform_mapping.values()) & set(source_transforms)
        )

        self.assertFalse(
            set(geometry_mapping.values()) & set(source_geometries)
        )

        current_transforms = self.snapshot_transforms()
        current_geometries = self.snapshot_geometries()

        self.assertEqual(
            set(current_transforms),
            set(source_transforms) | set(transform_mapping.values())
        )

        self.assertEqual(
            set(current_geometries),
            set(source_geometries) | set(geometry_mapping.values())
        )

        for source_id, duplicate_id in transform_mapping.items():
            self.assertTrue(duplicate_id.startswith("transform:"))

            source = source_transforms[source_id]
            duplicate = current_transforms[duplicate_id]

            self.assert_transform_copy(
                source,
                duplicate,
                transform_mapping,
                is_root=source_id == root_id
            )

            expected_geometry_links = {
                geometry_mapping[geometry_id]
                for geometry_id in source["geometries"]
            }

            self.assertEqual(
                set(duplicate["geometries"]),
                expected_geometry_links
            )

        for source_id, duplicate_id in geometry_mapping.items():
            self.assertTrue(duplicate_id.startswith("geometry:"))

            self.assert_geometry_copy(
                source_geometries[source_id],
                current_geometries[duplicate_id],
                transform_mapping
            )

        # Every source entity must remain valid and unchanged.

        for identifier, source in source_transforms.items():
            self.assertEqual(
                current_transforms[identifier],
                source
            )

        for identifier, source in source_geometries.items():
            self.assertEqual(
                current_geometries[identifier],
                source
            )

        self.assert_index_consistency()

    def duplicate_checked(
        self,
        root_id,
        selected_ids=None,
        include_children=False
    ):
        selected_ids = (
            {root_id}
            if selected_ids is None
            else set(selected_ids)
        )

        transforms_before = self.snapshot_transforms()
        geometries_before = self.snapshot_geometries()
        materials_before = self.snapshot_materials()

        result = self.duplicate_transform(
            root_id,
            include_children=include_children
        )

        self.assert_duplication_mapping(
            result,
            transforms_before,
            geometries_before,
            selected_ids,
            root_id
        )

        self.assertEqual(
            self.snapshot_materials(),
            materials_before,
            "Duplication unexpectedly created or modified materials"
        )

        return result

    def assert_index_consistency(self):
        transforms = self.snapshot_transforms()
        geometries = self.snapshot_geometries()
        materials = self.snapshot_materials()

        expected_geometries = {
            identifier: set()
            for identifier in transforms
        }

        for identifier, geometry in geometries.items():
            self.assertIn(
                geometry["parentId"],
                transforms
            )

            expected_geometries[geometry["parentId"]].add(identifier)

            if "materials" in geometry:
                self.assertEqual(
                    len(geometry["materials"]),
                    len(set(geometry["materials"]))
                )

                self.assertTrue(
                    set(geometry["materials"]).issubset(materials)
                )

                if geometry["kind"] == "DOMAIN":
                    self.assertEqual(geometry["materials"], [])

        for identifier, transform in transforms.items():
            self.assertEqual(
                len(transform["geometries"]),
                len(set(transform["geometries"]))
            )

            self.assertEqual(
                set(transform["geometries"]),
                expected_geometries[identifier]
            )

            self.assertEqual(
                len(transform["materials"]),
                len(set(transform["materials"]))
            )

            self.assertTrue(
                set(transform["materials"]).issubset(materials)
            )

            for parent_id in transform["parents"].values():
                if parent_id is not None:
                    self.assertIn(parent_id, transforms)
                    self.assertNotEqual(parent_id, identifier)

        global_ids = [
            entity["globalId"]
            for entity in [
                *transforms.values(),
                *geometries.values(),
                *materials.values()
            ]
            if entity["globalId"] is not None
        ]

        self.assertEqual(
            len(global_ids),
            len(set(global_ids))
        )

    def assert_step_references_valid(self, entities):
        for identifier, entity in entities.items():
            for reference in step_references(entity["arguments"]):
                self.assertIn(
                    reference,
                    entities,
                    f"Dangling STEP reference #{identifier} -> #{reference}"
                )

    def assert_step_global_ids_unique(self, entities):
        global_ids = step_global_ids(entities)

        duplicates = {
            global_id: count
            for global_id, count in Counter(global_ids).items()
            if count > 1
        }

        self.assertFalse(
            duplicates,
            f"Duplicate IFC GlobalIds: {duplicates}"
        )

    def representation_graph(self, entities, transform):
        product = entities[transform["ifcEntityId"]]

        references = step_references(product["arguments"])

        representations = [
            identifier
            for identifier in references
            if (
                entities[identifier]["class"]
                == "IFCPRODUCTDEFINITIONSHAPE"
            )
        ]

        self.assertEqual(len(representations), 1)

        shared_classes = {
            "IFCGEOMETRICREPRESENTATIONCONTEXT",
            "IFCGEOMETRICREPRESENTATIONSUBCONTEXT",
            "IFCSIUNIT",
            "IFCUNITASSIGNMENT"
        }

        pending = representations.copy()
        visited = set()

        while pending:
            identifier = pending.pop()

            if identifier in visited:
                continue

            entity = entities[identifier]

            if entity["class"] in shared_classes:
                continue

            visited.add(identifier)

            pending.extend(
                step_references(entity["arguments"])
            )

        return visited

    def native_style_references(self, entities, geometry):
        result = set()

        for entity in entities.values():
            if entity["class"] != "IFCSTYLEDITEM":
                continue

            arguments = split_step_arguments(entity["arguments"])

            if arguments[0] != f"#{geometry['ifcEntityId']}":
                continue

            result.update(
                step_references(arguments[1])
            )

        return result

    def semantic_material_targets(self, entities, material):
        result = []

        for entity in entities.values():
            if entity["class"] != "IFCRELASSOCIATESMATERIAL":
                continue

            arguments = split_step_arguments(entity["arguments"])

            if arguments[-1] != f"#{material['ifcEntityId']}":
                continue

            result.extend(
                step_references(arguments[-2])
            )

        self.assertEqual(len(result), len(set(result)))

        return set(result)

    def property_definitions(self, entities, transform):
        result = set()

        for entity in entities.values():
            if entity["class"] != "IFCRELDEFINESBYPROPERTIES":
                continue

            arguments = split_step_arguments(entity["arguments"])

            related_objects = step_references(arguments[-2])

            if transform["ifcEntityId"] not in related_objects:
                continue

            result.update(
                step_references(arguments[-1])
            )

        return result

    def property_graph(self, entities, transform):
        pending = list(
            self.property_definitions(entities, transform)
        )

        visited = set()

        while pending:
            identifier = pending.pop()

            if identifier in visited:
                continue

            visited.add(identifier)

            pending.extend(
                step_references(
                    entities[identifier]["arguments"]
                )
            )

        return visited

    def feature_relations(self, entities, geometry):
        relation_classes = {
            "IFCRELPROJECTSELEMENT",
            "IFCRELVOIDSELEMENT",
            "IFCRELASSIGNSTOPRODUCT"
        }

        return [
            entity
            for entity in entities.values()
            if (
                entity["class"] in relation_classes
                and geometry["ifcEntityId"] in step_references(
                    entity["arguments"]
                )
            )
        ]

    def assert_feature_relation(self, entities, geometry):
        relations = self.feature_relations(
            entities,
            geometry
        )

        self.assertEqual(len(relations), 1)

        relation = relations[0]

        expected_class = (
            "IFCRELASSIGNSTOPRODUCT"
            if geometry["kind"] == "DOMAIN"
            else "IFCRELPROJECTSELEMENT"
            if geometry["operation"] == "ADD"
            else "IFCRELVOIDSELEMENT"
        )

        self.assertEqual(
            relation["class"],
            expected_class
        )

        parent = self.get_transform(
            geometry["parentId"]
        )

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
                f"#{parent['ifcEntityId']}"
            )

            self.assertIn(
                geometry["ifcEntityId"],
                step_references(arguments[-3])
            )
        else:
            self.assertEqual(
                arguments[-2],
                f"#{parent['ifcEntityId']}"
            )

            self.assertEqual(
                arguments[-1],
                f"#{geometry['ifcEntityId']}"
            )

    def test_duplicate_transform(self):
        """Verify duplication of a simple transform."""

        properties = [
            property_input(
                "Reference",
                "SIMPLE-001"
            ),
            property_input(
                "Enabled",
                True,
                value_type="IfcBoolean"
            )
        ]

        matrix = multiply_matrices(
            translation_matrix(2.0, 3.0, 4.0),
            rotation_z(35.0)
        )

        source = self.create_transform(
            name="Simple transform",
            matrix=matrix,
            properties=properties
        )

        result = self.duplicate_checked(source["id"])

        duplicate = self.get_transform(result["rootId"])

        self.assertEqual(
            result["transforms"],
            {source["id"]: duplicate["id"]}
        )

        self.assertEqual(result["geometries"], {})

        self.assertEqual(
            duplicate["name"],
            "Simple transform Copy"
        )

        self.assertEqual(
            duplicate["definition"]["ifcClass"],
            "IfcBuildingElementProxy"
        )

        self.assertEqual(
            duplicate["parents"],
            parents_input()
        )

        self.assertEqual(duplicate["geometries"], [])
        self.assertEqual(duplicate["materials"], [])

        self.assert_matrix(
            duplicate["transforms"]["local"],
            matrix
        )

        self.assert_matrix(
            duplicate["transforms"]["world"],
            matrix
        )

        self.assertEqual(
            property_signature(duplicate["properties"]),
            property_signature(properties)
        )

        mesh = self.tessellate(duplicate["id"])

        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)

        self.assertEqual(
            self.get_transform(source["id"]),
            source
        )

        entities = parse_step_entities(self.save_model())

        self.assert_step_references_valid(entities)
        self.assert_step_global_ids_unique(entities)

    def test_duplicate_without_children(self):
        """Verify that non-recursive duplication excludes descendants."""

        root_domain = self.create_domain(
            parent_id=self.root_id,
            name="Root domain"
        )

        child_domain = self.create_domain(
            parent_id=self.child_id,
            name="Child domain"
        )

        grandchild_domain = self.create_domain(
            parent_id=self.grandchild_id,
            name="Grandchild domain"
        )

        source = self.get_transform(self.root_id)

        child_before = self.get_transform(self.child_id)
        grandchild_before = self.get_transform(self.grandchild_id)

        source_mesh = self.tessellate(
            self.root_id,
            include_children=False
        )

        result = self.duplicate_checked(
            self.root_id,
            selected_ids={self.root_id},
            include_children=False
        )

        duplicate_id = result["rootId"]

        self.assertEqual(
            set(result["transforms"]),
            {self.root_id}
        )

        self.assertIn(
            root_domain["id"],
            result["geometries"]
        )

        self.assertNotIn(
            child_domain["id"],
            result["geometries"]
        )

        self.assertNotIn(
            grandchild_domain["id"],
            result["geometries"]
        )

        descendants = [
            transform
            for transform in self.snapshot_transforms().values()
            if duplicate_id in transform["parents"].values()
        ]

        self.assertEqual(descendants, [])

        duplicate_mesh = self.tessellate(
            duplicate_id,
            include_children=True
        )

        self.assert_mesh_equivalent(
            source_mesh,
            duplicate_mesh
        )

        self.assertEqual(
            self.get_transform(self.root_id),
            source
        )

        self.assertEqual(
            self.get_transform(self.child_id),
            child_before
        )

        self.assertEqual(
            self.get_transform(self.grandchild_id),
            grandchild_before
        )

    def test_duplicate_with_children(self):
        """Verify recursive duplication of a transform hierarchy."""

        selected_ids = {
            self.root_id,
            self.child_id,
            self.grandchild_id
        }

        domains = [
            self.create_domain(
                parent_id=identifier,
                name=f"Recursive domain {index}",
                matrix=translation_matrix(0.0, 0.0, 5.0)
            )
            for index, identifier in enumerate((
                self.root_id,
                self.child_id,
                self.grandchild_id
            ))
        ]

        source_mesh = self.tessellate(
            self.root_id,
            include_children=True
        )

        source_domain_mesh = self.tessellate(
            self.root_id,
            kind="DOMAIN",
            include_children=True
        )

        result = self.duplicate_checked(
            self.root_id,
            selected_ids=selected_ids,
            include_children=True
        )

        mapping = result["transforms"]

        duplicate_root = self.get_transform(
            mapping[self.root_id]
        )

        duplicate_child = self.get_transform(
            mapping[self.child_id]
        )

        duplicate_grandchild = self.get_transform(
            mapping[self.grandchild_id]
        )

        self.assertEqual(
            duplicate_child["parents"]["placement"],
            duplicate_root["id"]
        )

        self.assertEqual(
            duplicate_grandchild["parents"]["placement"],
            duplicate_child["id"]
        )

        self.assertEqual(
            duplicate_child["parents"]["decomposition"],
            duplicate_root["id"]
        )

        self.assertEqual(
            duplicate_grandchild["parents"]["decomposition"],
            duplicate_root["id"]
        )

        self.assertNotIn(self.unrelated_id, mapping)
        self.assertNotIn(self.anchor_id, mapping)
        self.assertNotIn(self.storey_id, mapping)

        self.assertEqual(
            duplicate_root["name"],
            "Root product Copy"
        )

        self.assertEqual(
            duplicate_child["name"],
            "Child product"
        )

        self.assertEqual(
            duplicate_grandchild["name"],
            "Grandchild product"
        )

        for domain in domains:
            self.assertIn(
                domain["id"],
                result["geometries"]
            )

        self.assert_mesh_equivalent(
            source_mesh,
            self.tessellate(
                duplicate_root["id"],
                include_children=True
            )
        )

        self.assert_mesh_equivalent(
            source_domain_mesh,
            self.tessellate(
                duplicate_root["id"],
                kind="DOMAIN",
                include_children=True
            )
        )

        self.assertAlmostEqual(
            mesh_volume(source_mesh),
            72.0,
            delta=MESH_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_volume(source_domain_mesh),
            36.0,
            delta=MESH_TOLERANCE
        )

    def test_duplicate_geometry(self):
        """Verify duplication of physical geometries associated with a transform."""

        geometries = [
            self.create_geometry(
                name="First physical addition",
                profile=rectangular_profile(2.0, 3.0),
                depth=4.0,
                matrix=translation_matrix(0.0, 0.0, 3.0)
            ),
            self.create_geometry(
                name="Second physical addition",
                profile=rectangular_profile(3.0, 2.0),
                depth=2.0,
                matrix=multiply_matrices(
                    translation_matrix(0.5, 0.5, 3.0),
                    rotation_z(45.0)
                )
            )
        ]

        native = self.native_geometry(self.root_id)

        result = self.duplicate_checked(self.root_id)

        duplicate_id = result["rootId"]
        geometry_mapping = result["geometries"]

        self.assertEqual(
            set(geometry_mapping),
            {
                native["id"],
                *(geometry["id"] for geometry in geometries)
            }
        )

        duplicated_geometries = self.list_geometries(
            duplicate_id,
            kind="SOLID"
        )

        self.assertEqual(len(duplicated_geometries), 3)

        for source in geometries:
            duplicate = self.get_geometry(
                geometry_mapping[source["id"]]
            )

            self.assertEqual(duplicate["kind"], "SOLID")
            self.assertEqual(duplicate["operation"], "ADD")

            self.assertEqual(
                duplicate["ifcClass"],
                "IfcProjectionElement"
            )

            self.assertEqual(
                duplicate["name"],
                source["name"]
            )

            self.assert_profile(
                duplicate["profile"],
                source["profile"]
            )

        self.assert_mesh_equivalent(
            self.tessellate(self.root_id),
            self.tessellate(duplicate_id),
            exact_triangles=False
        )

        entities = parse_step_entities(self.save_model())

        for source in geometries:
            duplicate = self.get_geometry(
                geometry_mapping[source["id"]]
            )

            self.assert_feature_relation(entities, source)
            self.assert_feature_relation(entities, duplicate)

        self.assert_step_references_valid(entities)

    def test_duplicate_domains(self):
        """Verify duplication of independent managed spatial domains."""

        physical_before = self.tessellate(self.root_id)

        specifications = [
            {
                "name": "Rectangular domain",
                "profile": rectangular_profile(5.0, 3.0),
                "depth": 2.0,
                "matrix": translation_matrix(2.0, 3.0, 4.0)
            },
            {
                "name": "Concave domain",
                "profile": [
                    [0, 0],
                    [6, 0],
                    [6, 2],
                    [2, 2],
                    [2, 5],
                    [0, 5]
                ],
                "depth": 3.0,
                "matrix": multiply_matrices(
                    translation_matrix(20.0, 0.0, 0.0),
                    rotation_z(35.0)
                )
            }
        ]

        domains = [
            self.create_domain(**specification)
            for specification in specifications
        ]

        source_meshes = {
            domain["id"]: self.tessellate_domain(domain)
            for domain in domains
        }

        source_combined = self.tessellate(
            self.root_id,
            kind="DOMAIN"
        )

        result = self.duplicate_checked(self.root_id)

        duplicate_id = result["rootId"]

        self.assertEqual(
            len(self.list_geometries(duplicate_id, kind="DOMAIN")),
            2
        )

        for domain, expected_volume in zip(
            domains,
            (30.0, 54.0)
        ):
            duplicate = self.get_geometry(
                result["geometries"][domain["id"]]
            )

            self.assertEqual(duplicate["kind"], "DOMAIN")
            self.assertEqual(duplicate["operation"], "ADD")

            self.assertEqual(
                duplicate["ifcClass"],
                "IfcSpatialZone"
            )

            self.assertEqual(duplicate["materials"], [])

            mesh = self.tessellate_domain(duplicate)

            self.assert_mesh_equivalent(
                source_meshes[domain["id"]],
                mesh,
                exact_triangles=False
            )

            self.assertAlmostEqual(
                mesh_volume(mesh),
                expected_volume,
                delta=MESH_TOLERANCE
            )

            self.assert_watertight(mesh)

            self.assertEqual(mesh["groups"], [])

        self.assert_mesh_equivalent(
            source_combined,
            self.tessellate(
                duplicate_id,
                kind="DOMAIN"
            ),
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            physical_before,
            self.tessellate(self.root_id)
        )

        self.assert_mesh_equivalent(
            physical_before,
            self.tessellate(duplicate_id)
        )

        first_copy = self.get_geometry(
            result["geometries"][domains[0]["id"]]
        )

        self.update_geometry(
            first_copy["id"],
            depth=10.0
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(first_copy)
            ),
            150.0,
            delta=MESH_TOLERANCE
        )

        self.assertEqual(
            self.get_geometry(domains[0]["id"]),
            domains[0]
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(domains[0])
            ),
            30.0,
            delta=MESH_TOLERANCE
        )

        entities = parse_step_entities(self.save_model())

        for domain in domains:
            self.assert_feature_relation(
                entities,
                domain
            )

            self.assert_feature_relation(
                entities,
                self.get_geometry(
                    result["geometries"][domain["id"]]
                )
            )

    def test_duplicate_properties(self):
        """Verify duplication of editable properties and preserved native data."""

        self.load_fixture(
            build_complex_property_fixture(),
            "complex-properties.ifc"
        )

        properties = [
            property_input(
                "Reference",
                "DUPLICATED-001"
            ),
            property_input(
                "Description",
                "Independent property copy",
                value_type="IfcText"
            ),
            property_input(
                "Count",
                42,
                value_type="IfcInteger"
            ),
            property_input(
                "Density",
                7850.5,
                value_type="IfcReal",
                property_set="Pset_Physical"
            ),
            property_input(
                "Enabled",
                True,
                value_type="IfcBoolean"
            ),
            property_input(
                "OptionalValue",
                None,
                value_type="IfcLabel"
            ),
            property_input(
                "Reference",
                "NATIVE-ROOT",
                property_set="Pset_Native"
            )
        ]

        self.update_transform(
            self.root_id,
            properties=properties
        )

        source = self.get_transform(self.root_id)

        result = self.duplicate_checked(self.root_id)

        duplicate = self.get_transform(result["rootId"])

        self.assertEqual(
            property_signature(duplicate["properties"]),
            property_signature(source["properties"])
        )

        occurrence_properties = [
            item
            for item in duplicate["properties"]
            if item["source"] == "occurrence"
        ]

        self.assertEqual(
            property_signature(occurrence_properties),
            property_signature(properties)
        )

        source_quantities = [
            item
            for item in source["properties"]
            if item["source"] == "quantity"
        ]

        duplicate_quantities = [
            item
            for item in duplicate["properties"]
            if item["source"] == "quantity"
        ]

        self.assertTrue(source_quantities)

        self.assertEqual(
            property_signature(source_quantities),
            property_signature(duplicate_quantities)
        )

        entities = parse_step_entities(self.save_model())

        source_arguments = split_step_arguments(
            entities[source["ifcEntityId"]]["arguments"]
        )

        duplicate_arguments = split_step_arguments(
            entities[duplicate["ifcEntityId"]]["arguments"]
        )

        # Description, ObjectType, Tag and PredefinedType
        # are preserved outside the simplified transform API.

        for index in (3, 4, 7, 8):
            self.assertEqual(
                source_arguments[index],
                duplicate_arguments[index]
            )

        source_graph = self.property_graph(
            entities,
            source
        )

        duplicate_graph = self.property_graph(
            entities,
            duplicate
        )

        source_property_sets = {
            identifier
            for identifier in source_graph
            if entities[identifier]["class"] == "IFCPROPERTYSET"
        }

        duplicate_property_sets = {
            identifier
            for identifier in duplicate_graph
            if entities[identifier]["class"] == "IFCPROPERTYSET"
        }

        self.assertFalse(
            source_property_sets & duplicate_property_sets,
            "Editable property sets must not be shared"
        )

        source_single_values = {
            identifier
            for identifier in source_graph
            if entities[identifier]["class"] == "IFCPROPERTYSINGLEVALUE"
        }

        duplicate_single_values = {
            identifier
            for identifier in duplicate_graph
            if entities[identifier]["class"] == "IFCPROPERTYSINGLEVALUE"
        }

        source_complex = [
            entities[identifier]
            for identifier in source_graph
            if entities[identifier]["class"] == "IFCCOMPLEXPROPERTY"
        ]

        duplicate_complex = [
            entities[identifier]
            for identifier in duplicate_graph
            if entities[identifier]["class"] == "IFCCOMPLEXPROPERTY"
        ]

        with self.subTest(data="complex native properties"):
            self.assertTrue(source_complex)

            self.assertEqual(
                len(duplicate_complex),
                len(source_complex),
                "Complex native properties were lost during duplication"
            )

            self.assertTrue(
                any(
                    "COMPLEX-NATIVE-VALUE"
                    in entities[identifier]["arguments"]
                    for identifier in duplicate_graph
                ),
                "Complex native property value was lost"
            )

        with self.subTest(data="editable property independence"):
            editable_names = {
                item["name"]
                for item in properties
            }

            shared_editable_values = [
                identifier
                for identifier in (
                    source_single_values & duplicate_single_values
                )
                if (
                    split_step_arguments(
                        entities[identifier]["arguments"]
                    )[0][1:-1]
                    in editable_names
                )
            ]

            self.assertEqual(
                shared_editable_values,
                [],
                "Editable property values are shared between copies"
            )

        replacement = [
            property_input(
                "Reference",
                "COPY-ONLY"
            ),
            property_input(
                "Count",
                100,
                value_type="IfcInteger"
            )
        ]

        updated = self.update_transform(
            duplicate["id"],
            properties=replacement
        )

        self.assertEqual(
            property_signature([
                item
                for item in updated["properties"]
                if item["source"] == "occurrence"
            ]),
            property_signature(replacement)
        )

        self.assertEqual(
            property_signature([
                item
                for item in updated["properties"]
                if item["source"] == "quantity"
            ]),
            property_signature(source_quantities)
        )

        self.assertEqual(
            self.get_transform(source["id"]),
            source
        )

        self.save_and_reload()

        reloaded_source = self.find_transform(
            source["globalId"]
        )

        reloaded_duplicate = self.find_transform(
            duplicate["globalId"]
        )

        self.assertEqual(
            property_signature(reloaded_source["properties"]),
            property_signature(source["properties"])
        )

        self.assertEqual(
            property_signature(reloaded_duplicate["properties"]),
            property_signature(updated["properties"])
        )

    def test_duplicate_materials(self):
        """Verify preservation of shared semantic and visual material assignments."""

        concrete = self.create_material(
            name="Shared concrete",
            category="concrete"
        )

        steel = self.create_material(
            name="Shared steel",
            category="metal",
            visual={
                "color": [0.7, 0.8, 0.9],
                "opacity": 1.0,
                "metallic": 0.95,
                "roughness": 0.2
            }
        )

        geometry = self.create_geometry(
            name="Material assigned feature"
        )

        native = self.native_geometry(self.root_id)

        self.assign_material(concrete["id"], self.root_id)
        self.assign_material(steel["id"], self.child_id)
        self.assign_material(steel["id"], geometry["id"])
        self.assign_material(concrete["id"], native["id"])

        materials_before = self.snapshot_materials()

        result = self.duplicate_checked(
            self.root_id,
            selected_ids={
                self.root_id,
                self.child_id,
                self.grandchild_id
            },
            include_children=True
        )

        duplicate_root = self.get_transform(
            result["rootId"]
        )

        duplicate_child = self.get_transform(
            result["transforms"][self.child_id]
        )

        duplicate_geometry = self.get_geometry(
            result["geometries"][geometry["id"]]
        )

        duplicate_native = self.get_geometry(
            result["geometries"][native["id"]]
        )

        self.assertEqual(
            duplicate_root["materials"],
            [concrete["id"]]
        )

        self.assertEqual(
            duplicate_child["materials"],
            [steel["id"]]
        )

        self.assertEqual(
            duplicate_geometry["materials"],
            [steel["id"]]
        )

        self.assertEqual(
            self.snapshot_materials(),
            materials_before
        )

        entities = parse_step_entities(self.save_model())

        self.assertEqual(
            self.semantic_material_targets(
                entities,
                concrete
            ),
            {
                self.get_transform(self.root_id)["ifcEntityId"],
                duplicate_root["ifcEntityId"]
            }
        )

        self.assertEqual(
            self.semantic_material_targets(
                entities,
                steel
            ),
            {
                self.get_transform(self.child_id)["ifcEntityId"],
                duplicate_child["ifcEntityId"],
                geometry["ifcEntityId"],
                duplicate_geometry["ifcEntityId"]
            }
        )

        source_styles = self.native_style_references(
            entities,
            native
        )

        duplicate_styles = self.native_style_references(
            entities,
            duplicate_native
        )

        self.assertTrue(source_styles)

        self.assertEqual(
            source_styles,
            duplicate_styles,
            "Native surface styles were not shared with the copy"
        )

        self.unassign_material(
            concrete["id"],
            duplicate_root["id"]
        )

        self.assertEqual(
            self.get_transform(duplicate_root["id"])["materials"],
            []
        )

        self.assertEqual(
            self.get_transform(self.root_id)["materials"],
            [concrete["id"]]
        )

        self.unassign_material(
            steel["id"],
            duplicate_geometry["id"]
        )

        self.assertEqual(
            self.get_geometry(duplicate_geometry["id"])["materials"],
            []
        )

        self.assertEqual(
            self.get_geometry(geometry["id"])["materials"],
            [steel["id"]]
        )

        self.assertEqual(
            self.snapshot_materials(),
            materials_before
        )

        self.assert_index_consistency()

    def test_duplicate_operations(self):
        """Verify duplication of additive and subtractive feature definitions."""

        addition = self.create_geometry(
            name="Duplicated addition",
            operation="ADD",
            profile=rectangular_profile(2.0, 2.0),
            depth=3.0,
            matrix=translation_matrix(-0.5, -0.5, 3.0)
        )

        subtraction = self.create_geometry(
            name="Duplicated subtraction",
            operation="SUBTRACT",
            profile=rectangular_profile(0.5, 0.5),
            depth=6.0,
            matrix=translation_matrix(-0.25, -0.25, -1.0)
        )

        source_mesh = self.tessellate(self.root_id)

        result = self.duplicate_checked(self.root_id)

        duplicated_addition = self.get_geometry(
            result["geometries"][addition["id"]]
        )

        duplicated_subtraction = self.get_geometry(
            result["geometries"][subtraction["id"]]
        )

        self.assertEqual(
            duplicated_addition["operation"],
            "ADD"
        )

        self.assertEqual(
            duplicated_addition["ifcClass"],
            "IfcProjectionElement"
        )

        self.assertEqual(
            duplicated_subtraction["operation"],
            "SUBTRACT"
        )

        self.assertEqual(
            duplicated_subtraction["ifcClass"],
            "IfcOpeningElement"
        )

        operations = Counter(
            geometry["operation"]
            for geometry in self.list_geometries(
                result["rootId"],
                kind="SOLID"
            )
        )

        self.assertEqual(
            operations,
            Counter({
                "BASE": 1,
                "ADD": 1,
                "SUBTRACT": 1
            })
        )

        self.assert_mesh_equivalent(
            source_mesh,
            self.tessellate(result["rootId"]),
            exact_triangles=False
        )

        entities = parse_step_entities(self.save_model())

        for geometry in (
            addition,
            subtraction,
            duplicated_addition,
            duplicated_subtraction
        ):
            self.assert_feature_relation(
                entities,
                geometry
            )

        # Updating or converting an operation on the copy
        # must not modify its source feature.

        self.update_geometry(
            duplicated_addition["id"],
            operation="SUBTRACT"
        )

        self.assertEqual(
            self.get_geometry(addition["id"]),
            addition
        )

        self.assertEqual(
            self.get_geometry(subtraction["id"]),
            subtraction
        )

        self.assertEqual(
            self.get_geometry(
                duplicated_addition["id"]
            )["operation"],
            "SUBTRACT"
        )

        self.assert_mesh_equivalent(
            source_mesh,
            self.tessellate(self.root_id),
            exact_triangles=False
        )

        self.assert_index_consistency()

    def test_duplicate_native_representation(self):
        """Verify deep copying of a native IFC geometric representation."""

        source = self.get_transform(self.root_id)
        native = self.native_geometry(self.root_id)

        source_mesh = self.tessellate(self.root_id)

        self.assertAlmostEqual(
            mesh_volume(source_mesh),
            24.0,
            delta=MESH_TOLERANCE
        )

        result = self.duplicate_checked(self.root_id)

        duplicate = self.get_transform(result["rootId"])

        duplicate_native = self.get_geometry(
            result["geometries"][native["id"]]
        )

        self.assertEqual(
            duplicate_native["source"],
            "ifc"
        )

        self.assertIs(
            duplicate_native["editable"],
            False
        )

        self.assertEqual(
            duplicate_native["ifcClass"],
            "IfcExtrudedAreaSolid"
        )

        self.assertIsNone(native["globalId"])
        self.assertIsNone(duplicate_native["globalId"])

        self.assert_mesh_equivalent(
            source_mesh,
            self.tessellate(duplicate["id"])
        )

        entities = parse_step_entities(self.save_model())

        source_graph = self.representation_graph(
            entities,
            source
        )

        duplicate_graph = self.representation_graph(
            entities,
            duplicate
        )

        self.assertTrue(source_graph)
        self.assertTrue(duplicate_graph)

        self.assertFalse(
            source_graph & duplicate_graph,
            "Native representation entities are shared"
        )

        source_classes = Counter(
            entities[identifier]["class"]
            for identifier in source_graph
        )

        duplicate_classes = Counter(
            entities[identifier]["class"]
            for identifier in duplicate_graph
        )

        self.assertEqual(
            source_classes,
            duplicate_classes
        )

        self.assertIn(
            native["ifcEntityId"],
            source_graph
        )

        self.assertIn(
            duplicate_native["ifcEntityId"],
            duplicate_graph
        )

        # Deleting a copied native representation must not
        # remove its source representation.

        self.delete_geometry(duplicate_native["id"])

        self.assertEqual(
            self.get_transform(duplicate["id"])["geometries"],
            []
        )

        empty_mesh = self.tessellate(duplicate["id"])

        self.assertEqual(empty_mesh["vertexCount"], 0)
        self.assertEqual(empty_mesh["indexCount"], 0)

        self.assertEqual(
            self.get_geometry(native["id"]),
            native
        )

        self.assert_mesh_equivalent(
            source_mesh,
            self.tessellate(self.root_id)
        )

        entities_after = parse_step_entities(self.save_model())

        self.assertTrue(
            source_graph.issubset(entities_after)
        )

        self.assert_step_references_valid(entities_after)

    def test_duplicate_parent_relations(self):
        """Verify remapping of internal and preservation of external parents."""

        source_root = self.get_transform(self.root_id)
        source_child = self.get_transform(self.child_id)
        source_grandchild = self.get_transform(self.grandchild_id)

        self.assertEqual(
            source_root["parents"],
            parents_input(
                spatial=self.storey_id,
                placement=self.anchor_id,
                decomposition=self.anchor_id
            )
        )

        self.assertEqual(
            source_child["parents"],
            parents_input(
                spatial=self.storey_id,
                placement=self.root_id,
                decomposition=self.root_id
            )
        )

        self.assertEqual(
            source_grandchild["parents"],
            parents_input(
                spatial=self.storey_id,
                placement=self.child_id,
                decomposition=self.root_id
            )
        )

        result = self.duplicate_checked(
            self.root_id,
            selected_ids={
                self.root_id,
                self.child_id,
                self.grandchild_id
            },
            include_children=True
        )

        mapping = result["transforms"]

        duplicate_root = self.get_transform(
            mapping[self.root_id]
        )

        duplicate_child = self.get_transform(
            mapping[self.child_id]
        )

        duplicate_grandchild = self.get_transform(
            mapping[self.grandchild_id]
        )

        # External parents remain unchanged.

        self.assertEqual(
            duplicate_root["parents"],
            source_root["parents"]
        )

        # Internal parents are replaced with duplicated entities.

        self.assertEqual(
            duplicate_child["parents"],
            parents_input(
                spatial=self.storey_id,
                placement=duplicate_root["id"],
                decomposition=duplicate_root["id"]
            )
        )

        self.assertEqual(
            duplicate_grandchild["parents"],
            parents_input(
                spatial=self.storey_id,
                placement=duplicate_child["id"],
                decomposition=duplicate_root["id"]
            )
        )

        # Remove the first duplicated hierarchy before testing
        # duplication of the spatial container itself.

        self.delete_transform(duplicate_root["id"])

        spatial_result = self.duplicate_checked(
            self.storey_id,
            selected_ids={
                self.storey_id,
                self.root_id,
                self.child_id,
                self.grandchild_id
            },
            include_children=True
        )

        spatial_mapping = spatial_result["transforms"]

        duplicate_storey = spatial_mapping[self.storey_id]

        for source_id in (
            self.root_id,
            self.child_id,
            self.grandchild_id
        ):
            duplicate = self.get_transform(
                spatial_mapping[source_id]
            )

            self.assertEqual(
                duplicate["parents"]["spatial"],
                duplicate_storey
            )

        spatial_root = self.get_transform(
            spatial_mapping[self.root_id]
        )

        self.assertEqual(
            spatial_root["parents"]["placement"],
            self.anchor_id
        )

        self.assertEqual(
            spatial_root["parents"]["decomposition"],
            self.anchor_id
        )

        self.assertNotIn(
            self.anchor_id,
            spatial_mapping
        )

        self.assertNotIn(
            self.unrelated_id,
            spatial_mapping
        )

        entities = parse_step_entities(self.save_model())

        self.assert_step_references_valid(entities)
        self.assert_step_global_ids_unique(entities)

        self.assert_index_consistency()

    def test_duplicate_identifiers(self):
        """Verify unique session identifiers, IFC entity IDs and GlobalIds."""

        self.create_geometry(
            name="Identifier addition",
            operation="ADD"
        )

        self.create_geometry(
            name="Identifier subtraction",
            operation="SUBTRACT"
        )

        self.create_domain(
            name="Identifier root domain"
        )

        self.create_domain(
            parent_id=self.child_id,
            name="Identifier child domain"
        )

        initial_transforms = self.snapshot_transforms()
        initial_geometries = self.snapshot_geometries()

        created_transform_ids = set()
        created_geometry_ids = set()
        created_global_ids = set()

        for iteration in range(5):
            with self.subTest(iteration=iteration):
                result = self.duplicate_checked(
                    self.root_id,
                    selected_ids={
                        self.root_id,
                        self.child_id,
                        self.grandchild_id
                    },
                    include_children=True
                )

                transform_ids = set(
                    result["transforms"].values()
                )

                geometry_ids = set(
                    result["geometries"].values()
                )

                self.assertFalse(
                    created_transform_ids & transform_ids
                )

                self.assertFalse(
                    created_geometry_ids & geometry_ids
                )

                created_transform_ids.update(transform_ids)
                created_geometry_ids.update(geometry_ids)

                copies = [
                    self.get_transform(identifier)
                    for identifier in transform_ids
                ]

                copies.extend(
                    self.get_geometry(identifier)
                    for identifier in geometry_ids
                )

                for entity in copies:
                    global_id = entity["globalId"]

                    if global_id is None:
                        continue

                    self.assertRegex(
                        global_id,
                        GLOBAL_ID_PATTERN
                    )

                    self.assertNotIn(
                        global_id,
                        created_global_ids
                    )

                    created_global_ids.add(global_id)

        transforms = self.snapshot_transforms()
        geometries = self.snapshot_geometries()

        self.assertEqual(
            set(transforms),
            set(initial_transforms) | created_transform_ids
        )

        self.assertEqual(
            set(geometries),
            set(initial_geometries) | created_geometry_ids
        )

        ifc_entity_ids = [
            entity["ifcEntityId"]
            for entity in [
                *transforms.values(),
                *geometries.values()
            ]
        ]

        self.assertEqual(
            len(ifc_entity_ids),
            len(set(ifc_entity_ids))
        )

        entities = parse_step_entities(self.save_model())

        self.assert_step_global_ids_unique(entities)
        self.assert_step_references_valid(entities)

        all_global_ids = set(step_global_ids(entities))

        self.assertTrue(
            created_global_ids.issubset(all_global_ids)
        )

        self.assert_index_consistency()

    def test_duplicate_mapping(self):
        """Verify exact source-to-copy mappings returned by transformDuplicate."""

        self.create_geometry(
            parent_id=self.root_id,
            name="Mapped root addition"
        )

        self.create_geometry(
            parent_id=self.root_id,
            name="Mapped root subtraction",
            operation="SUBTRACT"
        )

        self.create_domain(
            parent_id=self.root_id,
            name="Mapped root domain"
        )

        self.create_domain(
            parent_id=self.child_id,
            name="Mapped child domain"
        )

        self.create_geometry(
            parent_id=self.grandchild_id,
            name="Mapped grandchild geometry"
        )

        unrelated_domain = self.create_domain(
            parent_id=self.unrelated_id,
            name="Unrelated unmapped domain"
        )

        source_transforms = self.snapshot_transforms()
        source_geometries = self.snapshot_geometries()

        selected_ids = {
            self.root_id,
            self.child_id,
            self.grandchild_id
        }

        result = self.duplicate_transform(
            self.root_id,
            include_children=True
        )

        self.assert_duplication_mapping(
            result,
            source_transforms,
            source_geometries,
            selected_ids,
            self.root_id
        )

        self.assertEqual(
            len(result["transforms"]),
            3
        )

        self.assertEqual(
            len(result["geometries"]),
            8
        )

        self.assertNotIn(
            unrelated_domain["id"],
            result["geometries"]
        )

        self.assertNotIn(
            self.unrelated_id,
            result["transforms"]
        )

        for source_id, duplicate_id in result["transforms"].items():
            source = self.get_transform(source_id)
            duplicate = self.get_transform(duplicate_id)

            for source_geometry_id in source["geometries"]:
                self.assertIn(
                    source_geometry_id,
                    result["geometries"]
                )

                duplicate_geometry_id = result[
                    "geometries"
                ][source_geometry_id]

                self.assertIn(
                    duplicate_geometry_id,
                    duplicate["geometries"]
                )

                self.assertEqual(
                    self.get_geometry(
                        duplicate_geometry_id
                    )["parentId"],
                    duplicate_id
                )

        # Duplicating an existing copy must return mappings
        # based on that copy, not the initial source entities.

        first_copy_ids = set(
            result["transforms"].values()
        )

        first_copy_geometry_ids = set(
            result["geometries"].values()
        )

        second = self.duplicate_checked(
            result["rootId"],
            selected_ids=first_copy_ids,
            include_children=True
        )

        self.assertEqual(
            set(second["transforms"]),
            first_copy_ids
        )

        self.assertEqual(
            set(second["geometries"]),
            first_copy_geometry_ids
        )

        self.assertFalse(
            set(second["transforms"].values())
            & first_copy_ids
        )

        self.assertFalse(
            set(second["geometries"].values())
            & first_copy_geometry_ids
        )

        self.assert_index_consistency()

    def test_duplicate_independence(self):
        """Verify independent editing of duplicated transforms and geometries."""

        properties = [
            property_input(
                "Reference",
                "ORIGINAL"
            ),
            property_input(
                "Count",
                10,
                value_type="IfcInteger"
            )
        ]

        self.update_transform(
            self.root_id,
            properties=properties
        )

        source_domain = self.create_domain(
            name="Independent source domain",
            profile=rectangular_profile(5.0, 3.0),
            depth=2.0,
            matrix=translation_matrix(2.0, 3.0, 4.0)
        )

        source_geometry = self.create_geometry(
            name="Independent source addition",
            depth=3.0,
            matrix=translation_matrix(0.0, 0.0, 3.0)
        )

        source_material = self.create_material(
            name="Original shared material"
        )

        replacement_material = self.create_material(
            name="Copy material"
        )

        self.assign_material(
            source_material["id"],
            source_geometry["id"]
        )

        self.assign_material(
            source_material["id"],
            self.root_id
        )

        source_transforms = self.snapshot_transforms()
        source_geometries = self.snapshot_geometries()

        source_domain_mesh = self.tessellate_domain(
            source_domain
        )

        source_physical_mesh = self.tessellate(
            self.root_id
        )

        result = self.duplicate_checked(
            self.root_id,
            selected_ids={
                self.root_id,
                self.child_id,
                self.grandchild_id
            },
            include_children=True
        )

        duplicate_root_id = result["rootId"]

        duplicate_child_id = result[
            "transforms"
        ][self.child_id]

        duplicate_domain_id = result[
            "geometries"
        ][source_domain["id"]]

        duplicate_geometry_id = result[
            "geometries"
        ][source_geometry["id"]]

        self.update_transform(
            duplicate_root_id,
            name="Modified duplicate root",
            properties=[
                property_input(
                    "Reference",
                    "COPY"
                )
            ],
            transform=transform_input(
                translation_matrix(100.0, 200.0, 300.0),
                space="world"
            )
        )

        self.update_transform(
            duplicate_child_id,
            name="Modified duplicate child",
            transform=transform_input(
                translation_matrix(30.0, 40.0, 50.0)
            )
        )

        updated_domain = self.update_geometry(
            duplicate_domain_id,
            name="Modified duplicate domain",
            profile=rectangular_profile(8.0, 6.0),
            depth=4.0,
            transform=transform_input(
                translation_matrix(20.0, 30.0, 40.0)
            )
        )

        self.update_geometry(
            duplicate_geometry_id,
            name="Modified duplicate geometry",
            depth=10.0,
            profile=rectangular_profile(3.0, 4.0)
        )

        self.unassign_material(
            source_material["id"],
            duplicate_geometry_id
        )

        self.assign_material(
            replacement_material["id"],
            duplicate_geometry_id
        )

        self.unassign_material(
            source_material["id"],
            duplicate_root_id
        )

        self.assign_material(
            replacement_material["id"],
            duplicate_root_id
        )

        for identifier, source in source_transforms.items():
            self.assertEqual(
                self.get_transform(identifier),
                source
            )

        for identifier, source in source_geometries.items():
            self.assertEqual(
                self.get_geometry(identifier),
                source
            )

        self.assert_mesh_equivalent(
            source_domain_mesh,
            self.tessellate_domain(source_domain)
        )

        self.assert_mesh_equivalent(
            source_physical_mesh,
            self.tessellate(self.root_id),
            exact_triangles=False
        )

        duplicate_mesh = self.tessellate_domain(
            updated_domain
        )

        self.assertAlmostEqual(
            mesh_volume(source_domain_mesh),
            30.0,
            delta=MESH_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_volume(duplicate_mesh),
            192.0,
            delta=MESH_TOLERANCE
        )

        self.assert_bounds(
            duplicate_mesh,
            (
                20.0, 30.0, 40.0,
                28.0, 36.0, 44.0
            )
        )

        duplicate_world_mesh = self.tessellate_domain(
            updated_domain,
            space="world"
        )

        self.assert_bounds(
            duplicate_world_mesh,
            (
                120.0, 230.0, 340.0,
                128.0, 236.0, 344.0
            )
        )

        self.assertEqual(
            self.get_geometry(source_geometry["id"])["materials"],
            [source_material["id"]]
        )

        self.assertEqual(
            self.get_geometry(duplicate_geometry_id)["materials"],
            [replacement_material["id"]]
        )

        self.assertEqual(
            self.get_transform(self.root_id)["materials"],
            [source_material["id"]]
        )

        self.assertEqual(
            self.get_transform(duplicate_root_id)["materials"],
            [replacement_material["id"]]
        )

        # Editing the source after duplication must also
        # leave the already modified copy unchanged.

        duplicate_before = self.get_geometry(
            duplicate_domain_id
        )

        self.update_geometry(
            source_domain["id"],
            depth=7.0
        )

        self.assertEqual(
            self.get_geometry(duplicate_domain_id),
            duplicate_before
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(source_domain)
            ),
            105.0,
            delta=MESH_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(duplicate_before)
            ),
            192.0,
            delta=MESH_TOLERANCE
        )

        self.assert_index_consistency()

    def test_duplicate_deletion(self):
        """Verify that deleting copies preserves every original entity."""

        material = self.create_material(
            name="Preserved source material"
        )

        self.create_geometry(
            name="Preserved source addition"
        )

        self.create_geometry(
            parent_id=self.child_id,
            name="Preserved source subtraction",
            operation="SUBTRACT"
        )

        self.create_domain(
            name="Preserved source domain"
        )

        self.create_domain(
            parent_id=self.grandchild_id,
            name="Preserved descendant domain"
        )

        self.assign_material(
            material["id"],
            self.root_id
        )

        initial_transforms = self.snapshot_transforms()
        initial_geometries = self.snapshot_geometries()
        initial_materials = self.snapshot_materials()

        physical_before = self.tessellate(
            self.root_id,
            include_children=True
        )

        domains_before = self.tessellate(
            self.root_id,
            kind="DOMAIN",
            include_children=True
        )

        first = self.duplicate_checked(
            self.root_id,
            selected_ids={
                self.root_id,
                self.child_id,
                self.grandchild_id
            },
            include_children=True
        )

        second = self.duplicate_checked(
            self.root_id,
            selected_ids={
                self.root_id,
                self.child_id,
                self.grandchild_id
            },
            include_children=True
        )

        first_transform_ids = set(
            first["transforms"].values()
        )

        first_geometry_ids = set(
            first["geometries"].values()
        )

        deleted_global_ids = {
            self.get_transform(identifier)["globalId"]
            for identifier in first_transform_ids
        }

        deleted_global_ids.update(
            self.get_geometry(identifier)["globalId"]
            for identifier in first_geometry_ids
        )

        deleted_global_ids.discard(None)

        second_transforms = {
            identifier: self.get_transform(identifier)
            for identifier in second["transforms"].values()
        }

        second_geometries = {
            identifier: self.get_geometry(identifier)
            for identifier in second["geometries"].values()
        }

        self.delete_transform(first["rootId"])

        for identifier in first_transform_ids:
            self.client.expect_error(
                "transformGet",
                {"id": identifier},
                "ENTITY_NOT_FOUND"
            )

        for identifier in first_geometry_ids:
            self.client.expect_error(
                "geometryGet",
                {"id": identifier},
                "ENTITY_NOT_FOUND"
            )

        self.assertEqual(
            self.snapshot_transforms(),
            {
                **initial_transforms,
                **second_transforms
            }
        )

        self.assertEqual(
            self.snapshot_geometries(),
            {
                **initial_geometries,
                **second_geometries
            }
        )

        self.assertEqual(
            self.snapshot_materials(),
            initial_materials
        )

        self.assert_mesh_equivalent(
            physical_before,
            self.tessellate(
                self.root_id,
                include_children=True
            ),
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            domains_before,
            self.tessellate(
                self.root_id,
                kind="DOMAIN",
                include_children=True
            ),
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            physical_before,
            self.tessellate(
                second["rootId"],
                include_children=True
            ),
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            domains_before,
            self.tessellate(
                second["rootId"],
                kind="DOMAIN",
                include_children=True
            ),
            exact_triangles=False
        )

        content = self.save_model()

        for global_id in deleted_global_ids:
            self.assertNotIn(global_id, content)

        entities = parse_step_entities(content)

        self.assert_step_references_valid(entities)
        self.assert_step_global_ids_unique(entities)

        self.delete_transform(second["rootId"])

        self.assertEqual(
            self.snapshot_transforms(),
            initial_transforms
        )

        self.assertEqual(
            self.snapshot_geometries(),
            initial_geometries
        )

        self.assertEqual(
            self.snapshot_materials(),
            initial_materials
        )

        result = self.save_and_reload()

        self.assertEqual(
            result["transformCount"],
            len(initial_transforms)
        )

        self.assertEqual(
            result["geometryCount"],
            len(initial_geometries)
        )

        self.assertEqual(
            result["materialCount"],
            len(initial_materials)
        )

        self.assert_mesh_equivalent(
            physical_before,
            self.tessellate(
                self.root_id,
                include_children=True
            ),
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            domains_before,
            self.tessellate(
                self.root_id,
                kind="DOMAIN",
                include_children=True
            ),
            exact_triangles=False
        )

        self.assert_index_consistency()

    def test_duplicate_persistence(self):
        """Verify persistence of duplicated entities, geometry and relationships."""

        self.update_transform(
            self.root_id,
            properties=[
                property_input(
                    "Reference",
                    "PERSISTENT-ROOT"
                ),
                property_input(
                    "Count",
                    42,
                    value_type="IfcInteger"
                )
            ]
        )

        material = self.create_material(
            name="Persistent shared material",
            category="metal"
        )

        addition = self.create_geometry(
            name="Persistent addition",
            operation="ADD",
            profile=rectangular_profile(2.0, 3.0),
            depth=4.0,
            matrix=translation_matrix(0.0, 0.0, 3.0)
        )

        subtraction = self.create_geometry(
            parent_id=self.child_id,
            name="Persistent subtraction",
            operation="SUBTRACT",
            profile=rectangular_profile(0.5, 0.5),
            depth=6.0,
            matrix=translation_matrix(-0.25, -0.25, -1.0)
        )

        domain = self.create_domain(
            name="Persistent root domain",
            profile=rectangular_profile(5.0, 3.0),
            depth=2.0,
            matrix=translation_matrix(2.0, 3.0, 4.0)
        )

        self.create_domain(
            parent_id=self.grandchild_id,
            name="Persistent descendant domain",
            profile=rectangular_profile(4.0, 2.0),
            depth=3.0,
            matrix=multiply_matrices(
                translation_matrix(0.0, 0.0, 5.0),
                rotation_z(30.0)
            )
        )

        native = self.native_geometry(self.root_id)

        self.assign_material(material["id"], self.root_id)
        self.assign_material(material["id"], addition["id"])
        self.assign_material(material["id"], subtraction["id"])
        self.assign_material(material["id"], native["id"])

        result = self.duplicate_checked(
            self.root_id,
            selected_ids={
                self.root_id,
                self.child_id,
                self.grandchild_id
            },
            include_children=True
        )

        duplicate_root = self.get_transform(
            result["rootId"]
        )

        source_root = self.get_transform(self.root_id)

        duplicated_domain = self.get_geometry(
            result["geometries"][domain["id"]]
        )

        source_global_id = source_root["globalId"]
        duplicate_global_id = duplicate_root["globalId"]

        def persistent_snapshot():
            transforms = self.snapshot_transforms()
            geometries = self.snapshot_geometries()
            materials = self.snapshot_materials()

            material_names = {
                identifier: value["name"]
                for identifier, value in materials.items()
            }

            transform_global_ids = {
                identifier: value["globalId"]
                for identifier, value in transforms.items()
            }

            persistent_transforms = {}

            for transform in transforms.values():
                global_id = transform["globalId"]

                persistent_transforms[global_id] = {
                    "name": transform["name"],
                    "ifcClass": transform["definition"]["ifcClass"],
                    "parents": {
                        field: (
                            transform_global_ids[parent_id]
                            if parent_id is not None
                            else None
                        )
                        for field, parent_id in transform["parents"].items()
                    },
                    "local": transform["transforms"]["local"],
                    "world": transform["transforms"]["world"],
                    "properties": property_signature(
                        transform["properties"]
                    ),
                    "materials": sorted(
                        material_names[identifier]
                        for identifier in transform["materials"]
                    ),
                    "geometryCount": len(transform["geometries"])
                }

            persistent_geometries = {}
            native_geometries = Counter()

            for geometry in geometries.values():
                parent_global_id = transform_global_ids[
                    geometry["parentId"]
                ]

                if geometry["globalId"] is None:
                    native_geometries[(
                        parent_global_id,
                        geometry["ifcClass"],
                        geometry["source"],
                        geometry["kind"],
                        geometry["operation"],
                        geometry["editable"]
                    )] += 1

                    continue

                persistent_geometries[geometry["globalId"]] = {
                    "parentGlobalId": parent_global_id,
                    "name": geometry["name"],
                    "kind": geometry["kind"],
                    "operation": geometry["operation"],
                    "source": geometry["source"],
                    "editable": geometry["editable"],
                    "ifcClass": geometry["ifcClass"],
                    "profile": geometry["profile"],
                    "depth": geometry["depth"],
                    "local": geometry["transforms"]["local"],
                    "world": geometry["transforms"]["world"],
                    "materials": sorted(
                        material_names[identifier]
                        for identifier in geometry["materials"]
                    )
                }

            return {
                "transforms": persistent_transforms,
                "geometries": persistent_geometries,
                "nativeGeometries": native_geometries,
                "materialCount": len(materials)
            }

        def assert_persistent_snapshot(actual, expected):
            self.assertEqual(
                set(actual["transforms"]),
                set(expected["transforms"])
            )

            self.assertEqual(
                set(actual["geometries"]),
                set(expected["geometries"])
            )

            self.assertEqual(
                actual["nativeGeometries"],
                expected["nativeGeometries"]
            )

            self.assertEqual(
                actual["materialCount"],
                expected["materialCount"]
            )

            for category in ("transforms", "geometries"):
                for global_id, reference in expected[category].items():
                    candidate = actual[category][global_id]

                    for field, value in reference.items():
                        if field in ("local", "world"):
                            self.assert_matrix(
                                candidate[field],
                                value
                            )

                        elif field == "profile":
                            self.assert_profile(
                                candidate[field],
                                value
                            )

                        elif field == "depth":
                            self.assertAlmostEqual(
                                candidate[field],
                                value,
                                delta=NUMERIC_TOLERANCE
                            )

                        else:
                            self.assertEqual(
                                candidate[field],
                                value,
                                f"Persistent field changed: "
                                f"{category}/{global_id}/{field}"
                            )

        expected = persistent_snapshot()

        physical_meshes = {
            global_id: self.tessellate(
                self.find_transform(global_id)["id"],
                include_children=True
            )
            for global_id in (
                source_global_id,
                duplicate_global_id
            )
        }

        domain_meshes = {
            global_id: self.tessellate(
                self.find_transform(global_id)["id"],
                kind="DOMAIN",
                include_children=True
            )
            for global_id in (
                source_global_id,
                duplicate_global_id
            )
        }

        self.assert_mesh_equivalent(
            physical_meshes[source_global_id],
            physical_meshes[duplicate_global_id],
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            domain_meshes[source_global_id],
            domain_meshes[duplicate_global_id],
            exact_triangles=False
        )

        expected_transform_count = len(
            self.list_transforms()
        )

        expected_geometry_count = len(
            self.list_geometries()
        )

        for iteration in range(5):
            with self.subTest(roundtrip=iteration):
                content = self.save_model()

                entities = parse_step_entities(content)

                self.assert_step_references_valid(entities)
                self.assert_step_global_ids_unique(entities)

                loaded = self.load_model(self.output_path)

                self.assertEqual(
                    loaded["transformCount"],
                    expected_transform_count
                )

                self.assertEqual(
                    loaded["geometryCount"],
                    expected_geometry_count
                )

                assert_persistent_snapshot(
                    persistent_snapshot(),
                    expected
                )

                for global_id in (
                    source_global_id,
                    duplicate_global_id
                ):
                    current = self.find_transform(global_id)

                    self.assert_mesh_equivalent(
                        physical_meshes[global_id],
                        self.tessellate(
                            current["id"],
                            include_children=True
                        ),
                        exact_triangles=False
                    )

                    self.assert_mesh_equivalent(
                        domain_meshes[global_id],
                        self.tessellate(
                            current["id"],
                            kind="DOMAIN",
                            include_children=True
                        ),
                        exact_triangles=False
                    )

                current_source = self.find_transform(
                    source_global_id
                )

                current_duplicate = self.find_transform(
                    duplicate_global_id
                )

                source_native = self.native_geometry(
                    current_source["id"]
                )

                duplicate_native = self.native_geometry(
                    current_duplicate["id"]
                )

                entities = parse_step_entities(
                    self.save_model()
                )

                source_styles = self.native_style_references(
                    entities,
                    source_native
                )

                duplicate_styles = self.native_style_references(
                    entities,
                    duplicate_native
                )

                self.assertTrue(source_styles)

                self.assertEqual(
                    source_styles,
                    duplicate_styles
                )

                self.assert_index_consistency()

        # Copies must remain independently editable after reload.

        current_domain = self.find_geometry(
            duplicated_domain["globalId"]
        )

        updated_domain = self.update_geometry(
            current_domain["id"],
            name="Updated persistent copied domain",
            depth=8.0
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(updated_domain)
            ),
            120.0,
            delta=MESH_TOLERANCE
        )

        original_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(original_domain)
            ),
            30.0,
            delta=MESH_TOLERANCE
        )

        self.assertEqual(
            original_domain["name"],
            domain["name"]
        )

        updated_expected = persistent_snapshot()

        self.save_and_reload()

        assert_persistent_snapshot(
            persistent_snapshot(),
            updated_expected
        )

        reloaded_domain = self.find_geometry(
            duplicated_domain["globalId"]
        )

        self.assertEqual(
            reloaded_domain["name"],
            "Updated persistent copied domain"
        )

        self.assertAlmostEqual(
            reloaded_domain["depth"],
            8.0,
            delta=NUMERIC_TOLERANCE
        )

        # Deleting the persisted copy must not affect
        # the original hierarchy or its shared material.

        current_duplicate = self.find_transform(
            duplicate_global_id
        )

        self.delete_transform(
            current_duplicate["id"]
        )

        current_source = self.find_transform(
            source_global_id
        )

        self.assert_mesh_equivalent(
            physical_meshes[source_global_id],
            self.tessellate(
                current_source["id"],
                include_children=True
            ),
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            domain_meshes[source_global_id],
            self.tessellate(
                current_source["id"],
                kind="DOMAIN",
                include_children=True
            ),
            exact_triangles=False
        )

        self.assertEqual(
            len(self.list_materials()),
            1
        )

        reloaded_material = self.find_material(
            material["name"]
        )

        self.assertIn(
            reloaded_material["id"],
            current_source["materials"]
        )

        content = self.save_model()

        self.assertNotIn(
            duplicate_global_id,
            content
        )

        self.assertNotIn(
            duplicated_domain["globalId"],
            content
        )

        self.assertIn(
            source_global_id,
            content
        )

        entities = parse_step_entities(content)

        self.assert_step_references_valid(entities)
        self.assert_step_global_ids_unique(entities)

        self.save_and_reload()

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