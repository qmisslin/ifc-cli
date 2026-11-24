import copy
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


# Standalone integration suite.
# Not executed against the compiled CLI.
#
# Semantic assignments use IfcRelAssociatesMaterial.
# Native representation items use IfcStyledItem.
#
# Native geometries do not expose a materials array in geometryGet.
# Their assignments are therefore validated directly in the saved IFC.
#
# Tessellation material groups currently operate at transform level.
# Per-face material resolution and native style attribution are not
# implemented by the current CLI.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30
MESH_TOLERANCE = 1e-6
NUMERIC_TOLERANCE = 1e-9

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

ROOT_GLOBAL_ID = "0000000000000000000001"
SECOND_GLOBAL_ID = "0000000000000000000002"
THIRD_GLOBAL_ID = "0000000000000000000003"
PROJECT_GLOBAL_ID = "0000000000000000000004"

DEFAULT_VISUAL = {
    "color": [0.6, 0.6, 0.6],
    "opacity": 1.0,
    "metallic": 0.0,
    "roughness": 0.8
}

DEFAULT_PROFILE = [
    [0, 0],
    [2, 0],
    [2, 3],
    [0, 3]
]

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('material-assignments-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#8=IFCPROJECT('0000000000000000000004',$,'Material assignment test project',$,$,$,$,(#5),#7);
#9=IFCLOCALPLACEMENT($,#4);
#10=IFCCARTESIANPOINT((0.,0.));
#11=IFCAXIS2PLACEMENT2D(#10,$);
#12=IFCRECTANGLEPROFILEDEF(.AREA.,$,#11,2.,2.);
#13=IFCEXTRUDEDAREASOLID(#12,#4,#2,2.);
#14=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#13));
#15=IFCPRODUCTDEFINITIONSHAPE($,$,(#14));
#16=IFCBUILDINGELEMENTPROXY('0000000000000000000001',$,'Root product',$,$,#9,#15,$,.NOTDEFINED.);
#17=IFCCARTESIANPOINT((10.,0.,0.));
#18=IFCAXIS2PLACEMENT3D(#17,#2,#3);
#19=IFCLOCALPLACEMENT(#9,#18);
#20=IFCEXTRUDEDAREASOLID(#12,#4,#2,2.);
#21=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#20));
#22=IFCPRODUCTDEFINITIONSHAPE($,$,(#21));
#23=IFCBUILDINGELEMENTPROXY('0000000000000000000002',$,'Second product',$,$,#19,#22,$,.NOTDEFINED.);
#24=IFCCARTESIANPOINT((20.,0.,0.));
#25=IFCAXIS2PLACEMENT3D(#24,#2,#3);
#26=IFCLOCALPLACEMENT(#9,#25);
#27=IFCEXTRUDEDAREASOLID(#12,#4,#2,2.);
#28=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#27));
#29=IFCPRODUCTDEFINITIONSHAPE($,$,(#28));
#30=IFCBUILDINGELEMENTPROXY('0000000000000000000003',$,'Third product',$,$,#26,#29,$,.NOTDEFINED.);
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


def transform_input(x=0.0, y=0.0, z=0.0):
    return {
        "space": "parent",
        "matrix": translation_matrix(x, y, z)
    }


def visual_input(
    color=None,
    opacity=1.0,
    metallic=0.0,
    roughness=0.8
):
    return {
        "color": (
            [0.6, 0.6, 0.6]
            if color is None
            else list(color)
        ),
        "opacity": opacity,
        "metallic": metallic,
        "roughness": roughness
    }


def property_input(
    name,
    value,
    value_type="IfcLabel",
    property_set="Pset_MaterialAssignmentTest",
    unit=None
):
    return {
        "propertySet": property_set,
        "name": name,
        "valueType": value_type,
        "value": value,
        "unit": unit,
        "source": "material"
    }


def mesh_points(mesh):
    vertices = mesh["vertices"]

    return [
        tuple(vertices[offset:offset + 3])
        for offset in range(0, len(vertices), 3)
    ]


def mesh_volume(mesh):
    points = mesh_points(mesh)
    indices = mesh["indices"]

    if not points:
        return 0.0

    origin = points[0]
    terms = []

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

        terms.append(
            sum(a[axis] * cross[axis] for axis in range(3))
        )

    return abs(math.fsum(terms)) / 6.0


def mesh_signature(mesh, precision=7):
    points = [
        tuple(round(value, precision) for value in point)
        for point in mesh_points(mesh)
    ]

    indices = mesh["indices"]

    return sorted(
        tuple(sorted(
            points[index]
            for index in indices[offset:offset + 3]
        ))
        for offset in range(0, len(indices), 3)
    )


def parse_step_entities(content):
    # Targeted STEP parser for the controlled fixture.
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
                f"Duplicate STEP identifier: #{identifier}"
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

    def expect_error(
        self,
        command,
        params,
        expected_code="INVALID_RELATION"
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
        space="world",
        geometry_id=None,
        include_children=False,
        include_normals=False,
        include_materials=True
    ):
        options = {
            "space": space,
            "kind": kind,
            "includeNormals": include_normals,
            "includeMaterials": include_materials,
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
                    "Mesh event received before mesh.begin"
                )

            if event_name == "mesh.end":
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


class MaterialAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-material-assignments-"
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
        self.second_id = self.find_transform(SECOND_GLOBAL_ID)["id"]
        self.third_id = self.find_transform(THIRD_GLOBAL_ID)["id"]

        self.root_native_id = self.native_geometry(self.root_id)["id"]
        self.second_native_id = self.native_geometry(self.second_id)["id"]
        self.third_native_id = self.native_geometry(self.third_id)["id"]

        self.assertEqual(
            result["materialCount"],
            len(self.list_materials())
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

        self.assertTrue(self.output_path.is_file())
        self.assertGreater(self.output_path.stat().st_size, 0)

        return self.output_path.read_text(encoding="utf-8")

    def save_and_reload(self):
        self.save_model()

        return self.load_model(self.output_path)

    def list_transforms(self):
        return self.client.request("transformGetAll")

    def list_geometries(self, parent_id=None):
        return self.client.request(
            "geometryGetAll",
            {"transformId": parent_id}
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

    def get_target(self, identifier):
        if identifier.startswith("transform:"):
            return self.get_transform(identifier)

        if identifier.startswith("geometry:"):
            return self.get_geometry(identifier)

        raise ValueError(
            f"Unsupported material target: {identifier}"
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

    def native_geometry(self, parent_id):
        matches = [
            geometry
            for geometry in self.list_geometries(parent_id)
            if geometry["source"] == "ifc"
        ]

        self.assertEqual(len(matches), 1)

        return self.get_geometry(matches[0]["id"])

    def create_material(
        self,
        name="Created material",
        category="concrete",
        visual=None,
        properties=None
    ):
        material = self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": category,
                "visual": (
                    copy.deepcopy(DEFAULT_VISUAL)
                    if visual is None
                    else copy.deepcopy(visual)
                ),
                "properties": (
                    []
                    if properties is None
                    else properties
                )
            }
        )

        self.assertTrue(material["id"].startswith("material:"))
        self.assertIs(material["editable"], True)
        self.assertEqual(material["name"], name)
        self.assertEqual(material["category"], category)
        self.assertIsNone(material["globalId"])

        return material

    def create_geometry(
        self,
        parent_id=None,
        name="Created geometry",
        kind="SOLID",
        operation="ADD",
        profile=None,
        depth=2.0,
        x=0.0,
        y=0.0,
        z=0.0
    ):
        parent_id = (
            self.root_id
            if parent_id is None
            else parent_id
        )

        geometry = self.client.request(
            "geometryCreate",
            {
                "parentId": parent_id,
                "name": name,
                "kind": kind,
                "operation": operation,
                "profile": (
                    DEFAULT_PROFILE
                    if profile is None
                    else profile
                ),
                "depth": depth,
                "transform": transform_input(x, y, z)
            }
        )

        self.assertEqual(geometry["parentId"], parent_id)
        self.assertEqual(geometry["kind"], kind)
        self.assertEqual(geometry["operation"], operation)
        self.assertEqual(geometry["materials"], [])
        self.assertIs(geometry["editable"], True)

        return geometry

    def update_material(self, identifier, **changes):
        result = self.client.request(
            "materialUpdate",
            {
                "id": identifier,
                **changes
            }
        )

        self.assertEqual(result["id"], identifier)
        self.assertEqual(self.get_material(identifier), result)

        return result

    def assign_material(self, material_id, target_id):
        params = {
            "materialId": material_id,
            "targetId": target_id
        }

        result = self.client.request(
            "materialAssign",
            params
        )

        self.assertEqual(result, params)

        return result

    def unassign_material(self, material_id, target_id):
        params = {
            "materialId": material_id,
            "targetId": target_id
        }

        result = self.client.request(
            "materialUnassign",
            params
        )

        self.assertEqual(result, params)

        return result

    def delete_material(self, identifier):
        result = self.client.request(
            "materialDelete",
            {"id": identifier}
        )

        self.assertEqual(result, {"id": identifier})

        self.assertNotIn(
            identifier,
            {item["id"] for item in self.list_materials()}
        )

        self.client.expect_error(
            "materialGet",
            {"id": identifier},
            "ENTITY_NOT_FOUND"
        )

    def delete_geometry(self, identifier):
        result = self.client.request(
            "geometryDelete",
            {"id": identifier}
        )

        self.assertEqual(result, {"id": identifier})

        self.assertNotIn(
            identifier,
            {item["id"] for item in self.list_geometries()}
        )

        self.client.expect_error(
            "geometryGet",
            {"id": identifier},
            "ENTITY_NOT_FOUND"
        )

    def assert_target_materials(self, target_id, expected_ids):
        target = self.get_target(target_id)

        self.assertIn(
            "materials",
            target,
            "Target does not expose semantic material assignments"
        )

        actual_ids = target["materials"]
        expected_ids = set(expected_ids)

        self.assertIsInstance(actual_ids, list)

        self.assertEqual(
            len(actual_ids),
            len(set(actual_ids)),
            "Duplicate material identifiers"
        )

        self.assertEqual(
            set(actual_ids),
            expected_ids
        )

        indexed_ids = {
            material["id"]
            for material in self.list_materials()
        }

        self.assertTrue(
            expected_ids.issubset(indexed_ids)
        )

    def assert_material_unchanged(self, before, after):
        for field in (
            "id",
            "ifcEntityId",
            "globalId",
            "name",
            "category",
            "editable",
            "visual",
            "properties"
        ):
            self.assertEqual(
                before[field],
                after[field],
                f"Unexpected material modification: {field}"
            )

    def assert_target_unchanged(
        self,
        before,
        after,
        ignored_fields=None
    ):
        ignored_fields = set(ignored_fields or [])

        for field in before:
            if field in ignored_fields:
                continue

            self.assertEqual(
                before[field],
                after[field],
                f"Unexpected target modification: {field}"
            )

    def assert_index_consistency(self):
        material_summaries = self.list_materials()

        material_ids = {
            item["id"]
            for item in material_summaries
        }

        self.assertEqual(
            len(material_summaries),
            len(material_ids)
        )

        targets = [
            self.get_transform(item["id"])
            for item in self.list_transforms()
        ]

        targets.extend(
            self.get_geometry(item["id"])
            for item in self.list_geometries()
        )

        for target in targets:
            with self.subTest(target_id=target["id"]):
                if "materials" not in target:
                    continue

                assignments = target["materials"]

                self.assertEqual(
                    len(assignments),
                    len(set(assignments))
                )

                self.assertTrue(
                    set(assignments).issubset(material_ids),
                    f"Dangling material assignment: {target}"
                )

                if target.get("kind") == "DOMAIN":
                    self.assertEqual(assignments, [])

    def assert_step_references_valid(self, entities):
        for identifier, entity in entities.items():
            references = step_references(
                entity["arguments"]
            )

            for reference in references:
                self.assertIn(
                    reference,
                    entities,
                    f"Dangling STEP reference #{identifier} -> #{reference}"
                )

    def semantic_relations(self, entities, material_step_id):
        result = []

        for identifier, entity in entities.items():
            if entity["class"] != "IFCRELASSOCIATESMATERIAL":
                continue

            arguments = split_step_arguments(
                entity["arguments"]
            )

            self.assertEqual(len(arguments), 6)

            if arguments[-1] != f"#{material_step_id}":
                continue

            targets = step_references(arguments[-2])

            result.append({
                "id": identifier,
                "targets": targets
            })

        return result

    def assert_semantic_assignments(
        self,
        entities,
        material,
        target_ids
    ):
        material_step_id = material["ifcEntityId"]

        self.assertIn(material_step_id, entities)

        self.assertEqual(
            entities[material_step_id]["class"],
            "IFCMATERIAL"
        )

        expected_targets = {
            self.get_target(identifier)["ifcEntityId"]
            for identifier in target_ids
        }

        relations = self.semantic_relations(
            entities,
            material_step_id
        )

        if not expected_targets:
            self.assertEqual(relations, [])
            return

        actual_targets = [
            target
            for relation in relations
            for target in relation["targets"]
        ]

        self.assertEqual(
            len(actual_targets),
            len(set(actual_targets)),
            "Duplicate semantic material relation"
        )

        self.assertEqual(
            set(actual_targets),
            expected_targets
        )

        for target_step_id in actual_targets:
            self.assertIn(target_step_id, entities)

        # The CLI groups assignments to several targets into
        # a single IfcRelAssociatesMaterial per material.

        self.assertEqual(len(relations), 1)

    def resolve_surface_styles(self, entities, identifiers):
        pending = list(identifiers)
        visited = set()
        styles = set()

        traversable_classes = {
            "IFCMATERIALDEFINITIONREPRESENTATION",
            "IFCSTYLEDREPRESENTATION",
            "IFCSTYLEDITEM",
            "IFCPRESENTATIONSTYLEASSIGNMENT"
        }

        while pending:
            identifier = pending.pop()

            if identifier in visited:
                continue

            visited.add(identifier)

            self.assertIn(identifier, entities)

            entity = entities[identifier]

            if entity["class"] == "IFCSURFACESTYLE":
                styles.add(identifier)

            elif entity["class"] in traversable_classes:
                pending.extend(
                    step_references(entity["arguments"])
                )

        return styles

    def material_surface_styles(self, entities, material):
        material_step_id = material["ifcEntityId"]

        definitions = [
            identifier
            for identifier, entity in entities.items()
            if (
                entity["class"]
                == "IFCMATERIALDEFINITIONREPRESENTATION"
                and split_step_arguments(
                    entity["arguments"]
                )[-1] == f"#{material_step_id}"
            )
        ]

        self.assertEqual(
            len(definitions),
            1,
            "Expected one material definition representation"
        )

        styles = self.resolve_surface_styles(
            entities,
            definitions
        )

        self.assertEqual(
            len(styles),
            1,
            "Expected one material surface style"
        )

        return styles

    def native_style_assignments(
        self,
        entities,
        geometry_id
    ):
        geometry = self.get_geometry(geometry_id)
        geometry_step_id = geometry["ifcEntityId"]

        self.assertEqual(geometry["source"], "ifc")
        self.assertIn(geometry_step_id, entities)

        result = []

        for identifier, entity in entities.items():
            if entity["class"] != "IFCSTYLEDITEM":
                continue

            arguments = split_step_arguments(
                entity["arguments"]
            )

            self.assertEqual(len(arguments), 3)

            if arguments[0] != f"#{geometry_step_id}":
                continue

            styles = self.resolve_surface_styles(
                entities,
                step_references(arguments[1])
            )

            result.append({
                "id": identifier,
                "styles": styles
            })

        return result

    def assert_native_style_assignments(
        self,
        entities,
        geometry_id,
        materials
    ):
        expected_styles = set()

        for material in materials:
            expected_styles.update(
                self.material_surface_styles(
                    entities,
                    material
                )
            )

        assignments = self.native_style_assignments(
            entities,
            geometry_id
        )

        actual_styles = [
            style
            for assignment in assignments
            for style in assignment["styles"]
        ]

        self.assertEqual(
            len(actual_styles),
            len(set(actual_styles)),
            "Duplicate native style assignment"
        )

        self.assertEqual(
            set(actual_styles),
            expected_styles
        )

        if not materials:
            self.assertEqual(assignments, [])

    def assert_surface_style_visual(
        self,
        entities,
        material,
        expected_visual
    ):
        style_ids = self.material_surface_styles(
            entities,
            material
        )

        style = entities[next(iter(style_ids))]

        arguments = split_step_arguments(
            style["arguments"]
        )

        self.assertEqual(
            arguments[0],
            "'" + material["name"].replace("'", "''") + "'"
        )

        rendering_ids = [
            identifier
            for identifier in step_references(style["arguments"])
            if (
                entities[identifier]["class"]
                == "IFCSURFACESTYLERENDERING"
            )
        ]

        self.assertEqual(len(rendering_ids), 1)

        rendering = entities[rendering_ids[0]]

        arguments = split_step_arguments(
            rendering["arguments"]
        )

        self.assertEqual(len(arguments), 9)
        self.assertRegex(arguments[0], r"^#\d+$")

        color = entities[int(arguments[0][1:])]

        self.assertEqual(color["class"], "IFCCOLOURRGB")

        color_arguments = split_step_arguments(
            color["arguments"]
        )

        self.assertEqual(len(color_arguments), 4)

        for actual, expected in zip(
            color_arguments[1:],
            expected_visual["color"]
        ):
            self.assertAlmostEqual(
                float(actual),
                expected,
                delta=NUMERIC_TOLERANCE
            )

        self.assertAlmostEqual(
            float(arguments[1]),
            1.0 - expected_visual["opacity"],
            delta=NUMERIC_TOLERANCE
        )

        metallic_match = re.fullmatch(
            r"IFCNORMALISEDRATIOMEASURE\((.*?)\)",
            arguments[6],
            flags=re.IGNORECASE
        )

        self.assertIsNotNone(metallic_match)

        self.assertAlmostEqual(
            float(metallic_match.group(1)),
            expected_visual["metallic"],
            delta=NUMERIC_TOLERANCE
        )

        roughness_match = re.fullmatch(
            r"IFCSPECULARROUGHNESS\((.*?)\)",
            arguments[7],
            flags=re.IGNORECASE
        )

        self.assertIsNotNone(roughness_match)

        self.assertAlmostEqual(
            float(roughness_match.group(1)),
            expected_visual["roughness"],
            delta=NUMERIC_TOLERANCE
        )

    def assert_material_groups(
        self,
        mesh,
        expected_material_ids=None
    ):
        groups = mesh["groups"]

        self.assertIsInstance(groups, list)

        indexed_material_ids = {
            item["id"]
            for item in self.list_materials()
        }

        occupied_indices = set()
        actual_material_ids = set()

        for group in groups:
            self.assertEqual(
                set(group),
                {
                    "materialId",
                    "indexOffset",
                    "indexCount"
                }
            )

            material_id = group["materialId"]
            offset = group["indexOffset"]
            count = group["indexCount"]

            self.assertIn(material_id, indexed_material_ids)

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

            indices = set(range(offset, offset + count))

            self.assertFalse(
                occupied_indices & indices,
                "Overlapping material groups"
            )

            occupied_indices.update(indices)
            actual_material_ids.add(material_id)

        if expected_material_ids is not None:
            self.assertEqual(
                actual_material_ids,
                set(expected_material_ids)
            )

    def assert_full_material_group(self, mesh, material_id):
        self.assertGreater(mesh["indexCount"], 0)

        self.assert_material_groups(
            mesh,
            [material_id]
        )

        self.assertEqual(
            mesh["groups"],
            [{
                "materialId": material_id,
                "indexOffset": 0,
                "indexCount": mesh["indexCount"]
            }]
        )

    def assert_group_x_bounds(
        self,
        mesh,
        group,
        expected_minimum,
        expected_maximum
    ):
        points = mesh_points(mesh)
        indices = mesh["indices"]

        offset = group["indexOffset"]
        count = group["indexCount"]

        group_points = [
            points[index]
            for index in indices[offset:offset + count]
        ]

        self.assertTrue(group_points)

        self.assertAlmostEqual(
            min(point[0] for point in group_points),
            expected_minimum,
            delta=MESH_TOLERANCE
        )

        self.assertAlmostEqual(
            max(point[0] for point in group_points),
            expected_maximum,
            delta=MESH_TOLERANCE
        )

    def assert_mesh_unchanged(self, before, after):
        self.assertEqual(
            before["indexCount"],
            after["indexCount"]
        )

        self.assertEqual(
            mesh_signature(before),
            mesh_signature(after)
        )

        self.assertAlmostEqual(
            mesh_volume(before),
            mesh_volume(after),
            delta=MESH_TOLERANCE
        )

    def test_assign_transform(self):
        """Verify semantic material assignment to a transform."""

        material = self.create_material(
            name="Transform concrete"
        )

        target_before = self.get_transform(self.root_id)

        mesh_before = self.client.tessellate(
            self.root_id,
            include_materials=False
        )

        self.assert_target_materials(self.root_id, [])

        self.assign_material(
            material["id"],
            self.root_id
        )

        self.assert_target_materials(
            self.root_id,
            [material["id"]]
        )

        self.assert_target_materials(self.second_id, [])
        self.assert_target_materials(self.third_id, [])

        target_after = self.get_transform(self.root_id)

        self.assert_target_unchanged(
            target_before,
            target_after,
            ignored_fields={"materials", "definition"}
        )

        self.assert_material_unchanged(
            material,
            self.get_material(material["id"])
        )

        mesh_after = self.client.tessellate(self.root_id)

        self.assert_mesh_unchanged(
            mesh_before,
            mesh_after
        )

        self.assert_full_material_group(
            mesh_after,
            material["id"]
        )

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            material,
            [self.root_id]
        )

        self.assert_index_consistency()

    def test_assign_geometry(self):
        """Verify semantic material assignment to editable geometry."""

        material = self.create_material(
            name="Geometry material",
            category="metal"
        )

        geometries = [
            self.create_geometry(
                name="Material addition",
                operation="ADD",
                x=0,
                y=0,
                z=1
            ),
            self.create_geometry(
                name="Material opening",
                operation="SUBTRACT",
                x=0,
                y=0,
                z=-1,
                depth=4.0
            )
        ]

        for geometry in geometries:
            with self.subTest(operation=geometry["operation"]):
                self.assign_material(
                    material["id"],
                    geometry["id"]
                )

                self.assert_target_materials(
                    geometry["id"],
                    [material["id"]]
                )

                self.assert_target_unchanged(
                    geometry,
                    self.get_geometry(geometry["id"]),
                    ignored_fields={"materials", "definition"}
                )

        # Assignments to features must remain independent
        # from assignments to the owning transform.

        self.assert_target_materials(self.root_id, [])

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            material,
            [geometry["id"] for geometry in geometries]
        )

        self.assert_index_consistency()

        self.save_and_reload()

        reloaded_material = self.find_material(material["name"])

        for geometry in geometries:
            reloaded = self.find_geometry(geometry["globalId"])

            self.assert_target_materials(
                reloaded["id"],
                [reloaded_material["id"]]
            )

        self.assert_target_materials(self.root_id, [])

    def test_assign_native_geometry(self):
        """Verify visual assignment to a native IFC representation item."""

        material = self.create_material(
            name="Native geometry style",
            category="metal",
            visual=visual_input(
                color=[0.8, 0.2, 0.1],
                opacity=0.75,
                metallic=0.6,
                roughness=0.3
            )
        )

        native = self.get_geometry(self.root_native_id)

        self.assertEqual(native["source"], "ifc")
        self.assertIs(native["editable"], False)

        baseline = self.client.tessellate(
            self.root_id,
            include_materials=False
        )

        self.assign_material(
            material["id"],
            native["id"]
        )

        entities = parse_step_entities(self.save_model())

        self.assert_native_style_assignments(
            entities,
            native["id"],
            [material]
        )

        self.assert_semantic_assignments(
            entities,
            material,
            []
        )

        self.assert_target_materials(self.root_id, [])

        self.assert_mesh_unchanged(
            baseline,
            self.client.tessellate(
                self.root_id,
                include_materials=False
            )
        )

        self.save_and_reload()

        reloaded_material = self.find_material(material["name"])

        entities = parse_step_entities(self.save_model())

        self.assert_native_style_assignments(
            entities,
            self.root_native_id,
            [reloaded_material]
        )

        self.unassign_material(
            reloaded_material["id"],
            self.root_native_id
        )

        entities = parse_step_entities(self.save_model())

        self.assert_native_style_assignments(
            entities,
            self.root_native_id,
            []
        )

        self.assert_mesh_unchanged(
            baseline,
            self.client.tessellate(
                self.root_id,
                include_materials=False
            )
        )

        self.assert_index_consistency()

    def test_assign_multiple_targets(self):
        """Verify sharing one material between several target types."""

        material = self.create_material(
            name="Shared material",
            category="concrete"
        )

        first_geometry = self.create_geometry(
            parent_id=self.root_id,
            name="First shared feature"
        )

        second_geometry = self.create_geometry(
            parent_id=self.second_id,
            name="Second shared feature"
        )

        semantic_targets = [
            self.root_id,
            self.second_id,
            self.third_id,
            first_geometry["id"],
            second_geometry["id"]
        ]

        native_targets = [
            self.root_native_id,
            self.second_native_id
        ]

        for target_id in semantic_targets + native_targets:
            self.assign_material(
                material["id"],
                target_id
            )

        for target_id in semantic_targets:
            self.assert_target_materials(
                target_id,
                [material["id"]]
            )

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            material,
            semantic_targets
        )

        for target_id in native_targets:
            self.assert_native_style_assignments(
                entities,
                target_id,
                [material]
            )

        self.assertEqual(len(self.list_materials()), 1)

        # Removing one target must preserve all other assignments.

        self.unassign_material(
            material["id"],
            self.second_id
        )

        semantic_targets.remove(self.second_id)

        self.assert_target_materials(self.second_id, [])

        for target_id in semantic_targets:
            self.assert_target_materials(
                target_id,
                [material["id"]]
            )

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            material,
            semantic_targets
        )

        for target_id in native_targets:
            self.assert_native_style_assignments(
                entities,
                target_id,
                [material]
            )

        self.assert_index_consistency()

    def test_multiple_materials(self):
        """Verify independent materials assigned to the same entity."""

        materials = [
            self.create_material(
                name="Multiple concrete",
                category="concrete"
            ),
            self.create_material(
                name="Multiple steel",
                category="metal"
            ),
            self.create_material(
                name="Multiple insulation",
                category="insulation"
            )
        ]

        geometry = self.create_geometry(
            name="Multiple material geometry"
        )

        targets = [
            self.root_id,
            geometry["id"]
        ]

        for material in materials:
            for target_id in targets:
                self.assign_material(
                    material["id"],
                    target_id
                )

        expected_ids = [
            material["id"]
            for material in materials
        ]

        for target_id in targets:
            self.assert_target_materials(
                target_id,
                expected_ids
            )

        entities = parse_step_entities(self.save_model())

        for material in materials:
            self.assert_semantic_assignments(
                entities,
                material,
                targets
            )

        removed = materials[1]

        self.unassign_material(
            removed["id"],
            self.root_id
        )

        self.assert_target_materials(
            self.root_id,
            [
                materials[0]["id"],
                materials[2]["id"]
            ]
        )

        self.assert_target_materials(
            geometry["id"],
            expected_ids
        )

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            removed,
            [geometry["id"]]
        )

        self.assertEqual(len(self.list_materials()), 3)

        self.save_and_reload()

        reloaded_geometry = self.find_geometry(
            geometry["globalId"]
        )

        reloaded_materials = [
            self.find_material(material["name"])
            for material in materials
        ]

        self.assert_target_materials(
            self.root_id,
            [
                reloaded_materials[0]["id"],
                reloaded_materials[2]["id"]
            ]
        )

        self.assert_target_materials(
            reloaded_geometry["id"],
            [
                material["id"]
                for material in reloaded_materials
            ]
        )

        self.assert_index_consistency()

    def test_duplicate_assignment(self):
        """Verify idempotent semantic and visual material assignments."""

        material = self.create_material(
            name="Idempotent material"
        )

        geometry = self.create_geometry(
            name="Idempotent feature"
        )

        semantic_targets = [
            self.root_id,
            self.second_id,
            geometry["id"]
        ]

        native_targets = [
            self.root_native_id,
            self.second_native_id
        ]

        targets = semantic_targets + native_targets

        for target_id in targets:
            self.assign_material(
                material["id"],
                target_id
            )

        initial_content = self.save_model()

        initial_entities = parse_step_entities(
            initial_content
        )

        initial_relations = self.semantic_relations(
            initial_entities,
            material["ifcEntityId"]
        )

        initial_styles = {
            target_id: self.native_style_assignments(
                initial_entities,
                target_id
            )
            for target_id in native_targets
        }

        for iteration in range(10):
            with self.subTest(iteration=iteration):
                for target_id in targets:
                    self.assign_material(
                        material["id"],
                        target_id
                    )

                for target_id in semantic_targets:
                    self.assert_target_materials(
                        target_id,
                        [material["id"]]
                    )

        final_entities = parse_step_entities(
            self.save_model()
        )

        self.assertEqual(
            self.semantic_relations(
                final_entities,
                material["ifcEntityId"]
            ),
            initial_relations
        )

        for target_id in native_targets:
            self.assertEqual(
                self.native_style_assignments(
                    final_entities,
                    target_id
                ),
                initial_styles[target_id]
            )

        self.assertEqual(
            set(final_entities),
            set(initial_entities),
            "Repeated assignments created additional IFC entities"
        )

        self.assert_semantic_assignments(
            final_entities,
            material,
            semantic_targets
        )

        for target_id in native_targets:
            self.assert_native_style_assignments(
                final_entities,
                target_id,
                [material]
            )

        self.assert_index_consistency()

    def test_unassign_material(self):
        """Verify removal of an existing material assignment."""

        material = self.create_material(
            name="Unassigned material"
        )

        geometry = self.create_geometry(
            name="Unassigned geometry"
        )

        semantic_targets = [
            self.root_id,
            self.second_id,
            geometry["id"]
        ]

        native_targets = [
            self.root_native_id,
            self.second_native_id
        ]

        for target_id in semantic_targets + native_targets:
            self.assign_material(
                material["id"],
                target_id
            )

        for target_id in list(semantic_targets):
            with self.subTest(semantic_target=target_id):
                self.unassign_material(
                    material["id"],
                    target_id
                )

                semantic_targets.remove(target_id)

                self.assert_target_materials(target_id, [])

                entities = parse_step_entities(
                    self.save_model()
                )

                self.assert_semantic_assignments(
                    entities,
                    material,
                    semantic_targets
                )

                for remaining in semantic_targets:
                    self.assert_target_materials(
                        remaining,
                        [material["id"]]
                    )

                for native_target in native_targets:
                    self.assert_native_style_assignments(
                        entities,
                        native_target,
                        [material]
                    )

        for target_id in list(native_targets):
            with self.subTest(native_target=target_id):
                self.unassign_material(
                    material["id"],
                    target_id
                )

                native_targets.remove(target_id)

                entities = parse_step_entities(
                    self.save_model()
                )

                self.assert_native_style_assignments(
                    entities,
                    target_id,
                    []
                )

                for remaining in native_targets:
                    self.assert_native_style_assignments(
                        entities,
                        remaining,
                        [material]
                    )

        self.assert_material_unchanged(
            material,
            self.get_material(material["id"])
        )

        self.assertEqual(len(self.list_materials()), 1)

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            material,
            []
        )

        self.assertEqual(
            [
                entity
                for entity in entities.values()
                if entity["class"] == "IFCRELASSOCIATESMATERIAL"
            ],
            []
        )

        self.save_and_reload()

        reloaded_material = self.find_material(material["name"])

        reloaded_geometry = self.find_geometry(
            geometry["globalId"]
        )

        self.assert_target_materials(self.root_id, [])
        self.assert_target_materials(self.second_id, [])
        self.assert_target_materials(reloaded_geometry["id"], [])

        self.assertEqual(
            self.client.tessellate(self.root_id)["groups"],
            []
        )

        self.assertIs(reloaded_material["editable"], True)

        self.assert_index_consistency()

    def test_unassign_missing_relation(self):
        """Verify INVALID_RELATION when an assignment does not exist."""

        first = self.create_material(
            name="Existing assignment material"
        )

        second = self.create_material(
            name="Missing assignment material"
        )

        geometry = self.create_geometry(
            name="Missing relation geometry"
        )

        self.assign_material(first["id"], self.root_id)

        self.assign_material(
            first["id"],
            geometry["id"]
        )

        self.assign_material(
            first["id"],
            self.root_native_id
        )

        cases = [
            (second["id"], self.root_id),
            (second["id"], geometry["id"]),
            (second["id"], self.root_native_id),
            (first["id"], self.second_id),
            (first["id"], self.second_native_id),
            (first["id"], self.third_id)
        ]

        initial_entities = parse_step_entities(
            self.save_model()
        )

        for material_id, target_id in cases:
            with self.subTest(
                material_id=material_id,
                target_id=target_id
            ):
                self.client.expect_error(
                    "materialUnassign",
                    {
                        "materialId": material_id,
                        "targetId": target_id
                    },
                    "INVALID_RELATION"
                )

                self.assert_target_materials(
                    self.root_id,
                    [first["id"]]
                )

                self.assert_target_materials(
                    geometry["id"],
                    [first["id"]]
                )

                self.assert_target_materials(
                    self.second_id,
                    []
                )

        self.assertEqual(
            parse_step_entities(self.save_model()),
            initial_entities
        )

        self.unassign_material(
            first["id"],
            self.root_id
        )

        self.client.expect_error(
            "materialUnassign",
            {
                "materialId": first["id"],
                "targetId": self.root_id
            },
            "INVALID_RELATION"
        )

        self.unassign_material(
            first["id"],
            self.root_native_id
        )

        self.client.expect_error(
            "materialUnassign",
            {
                "materialId": first["id"],
                "targetId": self.root_native_id
            },
            "INVALID_RELATION"
        )

        self.assert_target_materials(
            geometry["id"],
            [first["id"]]
        )

        self.assert_target_materials(self.root_id, [])

        self.assert_index_consistency()

    def test_domain_material_rejection(self):
        """Verify rejection of physical material assignments to managed domains."""

        material = self.create_material(
            name="Physical domain rejection material"
        )

        second_material = self.create_material(
            name="Second domain rejection material"
        )

        solid = self.create_geometry(
            name="Physical feature",
            kind="SOLID"
        )

        domain = self.create_geometry(
            name="Independent simulation domain",
            kind="DOMAIN",
            depth=4.0
        )

        domain_before = self.get_geometry(domain["id"])

        domain_mesh_before = self.client.tessellate(
            self.root_id,
            kind="DOMAIN",
            geometry_id=domain["id"]
        )

        physical_mesh_before = self.client.tessellate(
            self.root_id
        )

        self.assertEqual(domain_mesh_before["groups"], [])

        self.assertAlmostEqual(
            mesh_volume(domain_mesh_before),
            24.0,
            delta=MESH_TOLERANCE
        )

        for candidate in (material, second_material):
            with self.subTest(material=candidate["name"]):
                self.client.expect_error(
                    "materialAssign",
                    {
                        "materialId": candidate["id"],
                        "targetId": domain["id"]
                    },
                    "INVALID_RELATION"
                )

                self.assertEqual(
                    self.get_geometry(domain["id"]),
                    domain_before
                )

                self.assert_target_materials(
                    domain["id"],
                    []
                )

                self.client.expect_error(
                    "materialUnassign",
                    {
                        "materialId": candidate["id"],
                        "targetId": domain["id"]
                    },
                    "INVALID_RELATION"
                )

        self.assert_mesh_unchanged(
            domain_mesh_before,
            self.client.tessellate(
                self.root_id,
                kind="DOMAIN",
                geometry_id=domain["id"]
            )
        )

        self.assert_mesh_unchanged(
            physical_mesh_before,
            self.client.tessellate(self.root_id)
        )

        self.assign_material(
            material["id"],
            self.root_id
        )

        self.assign_material(
            material["id"],
            solid["id"]
        )

        self.assert_target_materials(
            self.root_id,
            [material["id"]]
        )

        self.assert_target_materials(
            solid["id"],
            [material["id"]]
        )

        self.assert_target_materials(domain["id"], [])

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            material,
            [
                self.root_id,
                solid["id"]
            ]
        )

        self.assert_semantic_assignments(
            entities,
            second_material,
            []
        )

        domain_step_id = domain["ifcEntityId"]

        for entity in entities.values():
            if entity["class"] != "IFCRELASSOCIATESMATERIAL":
                continue

            self.assertNotIn(
                domain_step_id,
                step_references(entity["arguments"])
            )

        mesh = self.client.tessellate(
            self.root_id,
            kind="DOMAIN",
            geometry_id=domain["id"]
        )

        self.assertEqual(mesh["groups"], [])

        self.assert_mesh_unchanged(
            domain_mesh_before,
            mesh
        )

        self.save_and_reload()

        reloaded_domain = self.find_geometry(
            domain["globalId"]
        )

        reloaded_material = self.find_material(
            material["name"]
        )

        self.assert_target_materials(
            reloaded_domain["id"],
            []
        )

        self.client.expect_error(
            "materialAssign",
            {
                "materialId": reloaded_material["id"],
                "targetId": reloaded_domain["id"]
            },
            "INVALID_RELATION"
        )

        self.assert_index_consistency()

    def test_material_update_propagation(self):
        """Verify updates to materials shared by several targets."""

        material = self.create_material(
            name="Shared original material",
            category="concrete",
            visual=visual_input(
                color=[0.3, 0.4, 0.5],
                opacity=0.8,
                metallic=0.1,
                roughness=0.7
            ),
            properties=[
                property_input(
                    "Reference",
                    "SHARED-001"
                )
            ]
        )

        geometry = self.create_geometry(
            name="Shared updated geometry"
        )

        semantic_targets = [
            self.root_id,
            self.second_id,
            geometry["id"]
        ]

        native_targets = [
            self.root_native_id,
            self.second_native_id
        ]

        for target_id in semantic_targets + native_targets:
            self.assign_material(
                material["id"],
                target_id
            )

        initial_entities = parse_step_entities(
            self.save_model()
        )

        initial_relations = self.semantic_relations(
            initial_entities,
            material["ifcEntityId"]
        )

        initial_styles = self.material_surface_styles(
            initial_entities,
            material
        )

        initial_native_assignments = {
            target_id: self.native_style_assignments(
                initial_entities,
                target_id
            )
            for target_id in native_targets
        }

        initial_meshes = {
            target_id: self.client.tessellate(target_id)
            for target_id in (self.root_id, self.second_id)
        }

        replacement_visual = visual_input(
            color=[0.9, 0.25, 0.1],
            opacity=0.55,
            metallic=0.85,
            roughness=0.15
        )

        replacement_properties = [
            property_input(
                "Reference",
                "SHARED-002"
            ),
            property_input(
                "Density",
                7800.0,
                value_type="IfcReal",
                property_set="Pset_Physical"
            ),
            property_input(
                "Recyclable",
                True,
                value_type="IfcBoolean"
            )
        ]

        updated = self.update_material(
            material["id"],
            name="Shared updated material",
            category="metal",
            visual=replacement_visual,
            properties=replacement_properties
        )

        self.assertEqual(updated["id"], material["id"])

        self.assertEqual(
            updated["ifcEntityId"],
            material["ifcEntityId"]
        )

        self.assertEqual(
            updated["name"],
            "Shared updated material"
        )

        self.assertEqual(updated["category"], "metal")
        self.assertEqual(updated["visual"], replacement_visual)

        self.assertEqual(
            sorted(
                updated["properties"],
                key=lambda item: (
                    item["propertySet"],
                    item["name"]
                )
            ),
            sorted(
                replacement_properties,
                key=lambda item: (
                    item["propertySet"],
                    item["name"]
                )
            )
        )

        for target_id in semantic_targets:
            self.assert_target_materials(
                target_id,
                [updated["id"]]
            )

            target = self.get_target(target_id)

            resolved = self.get_material(
                target["materials"][0]
            )

            self.assertEqual(resolved, updated)

        final_entities = parse_step_entities(
            self.save_model()
        )

        self.assert_semantic_assignments(
            final_entities,
            updated,
            semantic_targets
        )

        self.assertEqual(
            self.semantic_relations(
                final_entities,
                updated["ifcEntityId"]
            ),
            initial_relations
        )

        self.assertEqual(
            self.material_surface_styles(
                final_entities,
                updated
            ),
            initial_styles,
            "Updating rendering replaced the shared surface style"
        )

        for target_id in native_targets:
            self.assertEqual(
                self.native_style_assignments(
                    final_entities,
                    target_id
                ),
                initial_native_assignments[target_id]
            )

            self.assert_native_style_assignments(
                final_entities,
                target_id,
                [updated]
            )

        self.assert_surface_style_visual(
            final_entities,
            updated,
            replacement_visual
        )

        for target_id, before in initial_meshes.items():
            after = self.client.tessellate(target_id)

            self.assert_mesh_unchanged(before, after)

            self.assert_full_material_group(
                after,
                updated["id"]
            )

        self.save_and_reload()

        reloaded = self.find_material(updated["name"])

        self.assertEqual(
            reloaded["visual"],
            replacement_visual
        )

        reloaded_geometry = self.find_geometry(
            geometry["globalId"]
        )

        for target_id in (
            self.root_id,
            self.second_id,
            reloaded_geometry["id"]
        ):
            self.assert_target_materials(
                target_id,
                [reloaded["id"]]
            )

        entities = parse_step_entities(self.save_model())

        self.assert_surface_style_visual(
            entities,
            reloaded,
            replacement_visual
        )

        for target_id in (
            self.root_native_id,
            self.second_native_id
        ):
            self.assert_native_style_assignments(
                entities,
                target_id,
                [reloaded]
            )

        self.assert_index_consistency()

    def test_assigned_material_deletion(self):
        """Verify removal of assignments when deleting their material."""

        deleted = self.create_material(
            name="Deleted shared material",
            category="metal"
        )

        preserved = self.create_material(
            name="Preserved shared material",
            category="concrete"
        )

        geometry = self.create_geometry(
            name="Preserved assigned geometry"
        )

        deleted_targets = [
            self.root_id,
            self.second_id,
            geometry["id"]
        ]

        for target_id in deleted_targets:
            self.assign_material(
                deleted["id"],
                target_id
            )

        self.assign_material(
            deleted["id"],
            self.root_native_id
        )

        self.assign_material(
            deleted["id"],
            self.second_native_id
        )

        preserved_targets = [
            self.root_id,
            geometry["id"]
        ]

        for target_id in preserved_targets:
            self.assign_material(
                preserved["id"],
                target_id
            )

        self.assign_material(
            preserved["id"],
            self.third_native_id
        )

        entities_before = parse_step_entities(
            self.save_model()
        )

        self.assert_semantic_assignments(
            entities_before,
            deleted,
            deleted_targets
        )

        deleted_relation_ids = {
            relation["id"]
            for relation in self.semantic_relations(
                entities_before,
                deleted["ifcEntityId"]
            )
        }

        deleted_native_style_ids = {
            assignment["id"]
            for target_id in (
                self.root_native_id,
                self.second_native_id
            )
            for assignment in self.native_style_assignments(
                entities_before,
                target_id
            )
        }

        baseline = self.client.tessellate(
            self.second_id,
            include_materials=False
        )

        self.delete_material(deleted["id"])

        self.assert_target_materials(
            self.root_id,
            [preserved["id"]]
        )

        self.assert_target_materials(
            geometry["id"],
            [preserved["id"]]
        )

        self.assert_target_materials(self.second_id, [])

        self.assert_material_unchanged(
            preserved,
            self.get_material(preserved["id"])
        )

        self.assertEqual(len(self.list_materials()), 1)

        after = self.client.tessellate(self.second_id)

        self.assertEqual(after["groups"], [])

        self.assert_mesh_unchanged(
            baseline,
            after
        )

        content = self.save_model()

        entities_after = parse_step_entities(content)

        self.assertNotIn(
            deleted["ifcEntityId"],
            entities_after
        )

        for identifier in (
            deleted_relation_ids | deleted_native_style_ids
        ):
            self.assertNotIn(identifier, entities_after)

        self.assert_native_style_assignments(
            entities_after,
            self.root_native_id,
            []
        )

        self.assert_native_style_assignments(
            entities_after,
            self.second_native_id,
            []
        )

        self.assert_native_style_assignments(
            entities_after,
            self.third_native_id,
            [preserved]
        )

        self.assert_semantic_assignments(
            entities_after,
            preserved,
            preserved_targets
        )

        self.assert_step_references_valid(entities_after)

        self.save_and_reload()

        reloaded_material = self.find_material(
            preserved["name"]
        )

        reloaded_geometry = self.find_geometry(
            geometry["globalId"]
        )

        self.assertEqual(len(self.list_materials()), 1)

        self.assert_target_materials(
            self.root_id,
            [reloaded_material["id"]]
        )

        self.assert_target_materials(
            reloaded_geometry["id"],
            [reloaded_material["id"]]
        )

        self.assert_target_materials(self.second_id, [])

        self.assert_index_consistency()

    def test_assigned_geometry_deletion(self):
        """Verify material preservation after deleting assigned geometries."""

        material = self.create_material(
            name="Geometry deletion material"
        )

        first_geometry = self.create_geometry(
            name="Deleted assigned feature"
        )

        second_geometry = self.create_geometry(
            parent_id=self.second_id,
            name="Preserved assigned feature"
        )

        targets = [
            self.root_id,
            first_geometry["id"],
            second_geometry["id"]
        ]

        for target_id in targets:
            self.assign_material(
                material["id"],
                target_id
            )

        self.assign_material(
            material["id"],
            self.root_native_id
        )

        self.assign_material(
            material["id"],
            self.second_native_id
        )

        self.delete_geometry(first_geometry["id"])

        self.assertNotIn(
            first_geometry["id"],
            self.get_transform(self.root_id)["geometries"]
        )

        self.assert_material_unchanged(
            material,
            self.get_material(material["id"])
        )

        self.assert_target_materials(
            self.root_id,
            [material["id"]]
        )

        self.assert_target_materials(
            second_geometry["id"],
            [material["id"]]
        )

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            material,
            [
                self.root_id,
                second_geometry["id"]
            ]
        )

        self.assert_native_style_assignments(
            entities,
            self.root_native_id,
            [material]
        )

        self.assert_native_style_assignments(
            entities,
            self.second_native_id,
            [material]
        )

        self.delete_geometry(self.root_native_id)

        self.assertNotIn(
            self.root_native_id,
            self.get_transform(self.root_id)["geometries"]
        )

        self.assert_material_unchanged(
            material,
            self.get_material(material["id"])
        )

        entities = parse_step_entities(self.save_model())

        self.assert_native_style_assignments(
            entities,
            self.second_native_id,
            [material]
        )

        self.assert_semantic_assignments(
            entities,
            material,
            [
                self.root_id,
                second_geometry["id"]
            ]
        )

        self.assert_step_references_valid(entities)

        self.delete_geometry(second_geometry["id"])

        self.assert_material_unchanged(
            material,
            self.get_material(material["id"])
        )

        self.unassign_material(
            material["id"],
            self.root_id
        )

        self.unassign_material(
            material["id"],
            self.second_native_id
        )

        entities = parse_step_entities(self.save_model())

        self.assert_semantic_assignments(
            entities,
            material,
            []
        )

        self.assert_native_style_assignments(
            entities,
            self.second_native_id,
            []
        )

        self.assertEqual(len(self.list_materials()), 1)

        # The root native geometry was deliberately deleted.
        # Reload without the fixture helper that expects all
        # three native geometries to remain present.

        result = self.client.request(
            "load",
            {"path": str(self.output_path)}
        )

        self.assertEqual(result["materialCount"], 1)

        reloaded = self.find_material(material["name"])

        self.assertEqual(reloaded["name"], material["name"])
        self.assertEqual(reloaded["category"], material["category"])
        self.assertEqual(reloaded["visual"], material["visual"])
        self.assertIs(reloaded["editable"], True)

        reloaded_root = self.find_transform(ROOT_GLOBAL_ID)

        self.assertEqual(reloaded_root["geometries"], [])
        self.assertEqual(reloaded_root["materials"], [])

        self.assert_index_consistency()

    def test_tessellation_material_groups(self):
        """Verify material group identifiers and triangle index ranges."""

        concrete = self.create_material(
            name="Tessellation concrete",
            category="concrete",
            visual=visual_input(
                color=[0.6, 0.6, 0.6]
            )
        )

        steel = self.create_material(
            name="Tessellation steel",
            category="metal",
            visual=visual_input(
                color=[0.2, 0.3, 0.8],
                metallic=0.9,
                roughness=0.2
            )
        )

        glass = self.create_material(
            name="Tessellation glass",
            category="glass",
            visual=visual_input(
                color=[0.8, 0.9, 1.0],
                opacity=0.25
            )
        )

        baseline = self.client.tessellate(
            self.root_id,
            include_children=True,
            include_materials=False
        )

        self.assertEqual(baseline["groups"], [])

        self.assertAlmostEqual(
            mesh_volume(baseline),
            24.0,
            delta=MESH_TOLERANCE
        )

        self.assign_material(
            concrete["id"],
            self.root_id
        )

        self.assign_material(
            steel["id"],
            self.second_id
        )

        root_mesh = self.client.tessellate(
            self.root_id,
            include_children=False
        )

        self.assert_full_material_group(
            root_mesh,
            concrete["id"]
        )

        second_mesh = self.client.tessellate(
            self.second_id
        )

        self.assert_full_material_group(
            second_mesh,
            steel["id"]
        )

        third_mesh = self.client.tessellate(
            self.third_id
        )

        self.assertGreater(third_mesh["indexCount"], 0)
        self.assertEqual(third_mesh["groups"], [])

        combined = self.client.tessellate(
            self.root_id,
            include_children=True
        )

        self.assert_mesh_unchanged(
            baseline,
            combined
        )

        self.assert_material_groups(
            combined,
            [
                concrete["id"],
                steel["id"]
            ]
        )

        self.assertEqual(len(combined["groups"]), 2)

        group_by_material = {
            group["materialId"]: group
            for group in combined["groups"]
        }

        concrete_group = group_by_material[concrete["id"]]
        steel_group = group_by_material[steel["id"]]

        self.assertEqual(
            concrete_group["indexCount"],
            root_mesh["indexCount"]
        )

        self.assertEqual(
            steel_group["indexCount"],
            second_mesh["indexCount"]
        )

        self.assert_group_x_bounds(
            combined,
            concrete_group,
            -1.0,
            1.0
        )

        self.assert_group_x_bounds(
            combined,
            steel_group,
            9.0,
            11.0
        )

        assigned_index_count = sum(
            group["indexCount"]
            for group in combined["groups"]
        )

        self.assertEqual(
            combined["indexCount"] - assigned_index_count,
            third_mesh["indexCount"]
        )

        self.assign_material(
            glass["id"],
            self.third_id
        )

        fully_assigned = self.client.tessellate(
            self.root_id,
            include_children=True
        )

        self.assert_material_groups(
            fully_assigned,
            [
                concrete["id"],
                steel["id"],
                glass["id"]
            ]
        )

        self.assertEqual(
            len(fully_assigned["groups"]),
            3
        )

        self.assertEqual(
            sum(
                group["indexCount"]
                for group in fully_assigned["groups"]
            ),
            fully_assigned["indexCount"]
        )

        group_by_material = {
            group["materialId"]: group
            for group in fully_assigned["groups"]
        }

        self.assert_group_x_bounds(
            fully_assigned,
            group_by_material[glass["id"]],
            19.0,
            21.0
        )

        without_materials = self.client.tessellate(
            self.root_id,
            include_children=True,
            include_materials=False
        )

        self.assertEqual(without_materials["groups"], [])

        self.assert_mesh_unchanged(
            fully_assigned,
            without_materials
        )

        self.unassign_material(
            steel["id"],
            self.second_id
        )

        partially_assigned = self.client.tessellate(
            self.root_id,
            include_children=True
        )

        self.assert_material_groups(
            partially_assigned,
            [
                concrete["id"],
                glass["id"]
            ]
        )

        self.assert_mesh_unchanged(
            baseline,
            partially_assigned
        )

        # DOMAIN tessellation must never expose physical materials,
        # even when its parent has a semantic material assignment.

        domain = self.create_geometry(
            name="Tessellation material exclusion domain",
            kind="DOMAIN"
        )

        domain_mesh = self.client.tessellate(
            self.root_id,
            kind="DOMAIN",
            geometry_id=domain["id"],
            include_materials=True
        )

        self.assertGreater(domain_mesh["indexCount"], 0)
        self.assertEqual(domain_mesh["groups"], [])

        self.assertAlmostEqual(
            mesh_volume(domain_mesh),
            12.0,
            delta=MESH_TOLERANCE
        )

        self.assert_mesh_unchanged(
            baseline,
            self.client.tessellate(
                self.root_id,
                include_children=True
            )
        )

        self.save_and_reload()

        reloaded_concrete = self.find_material(
            concrete["name"]
        )

        reloaded_glass = self.find_material(
            glass["name"]
        )

        reloaded_mesh = self.client.tessellate(
            self.root_id,
            include_children=True
        )

        self.assert_material_groups(
            reloaded_mesh,
            [
                reloaded_concrete["id"],
                reloaded_glass["id"]
            ]
        )

        self.assert_mesh_unchanged(
            baseline,
            reloaded_mesh
        )

        reloaded_domain = self.find_geometry(
            domain["globalId"]
        )

        reloaded_domain_mesh = self.client.tessellate(
            self.root_id,
            kind="DOMAIN",
            geometry_id=reloaded_domain["id"]
        )

        self.assertEqual(
            reloaded_domain_mesh["groups"],
            []
        )

        self.assert_index_consistency()

    def test_assignment_persistence(self):
        """Verify semantic and visual assignments after repeated roundtrips."""

        concrete = self.create_material(
            name="Persistent assignment concrete",
            category="concrete",
            properties=[
                property_input(
                    "Reference",
                    "CONCRETE-001"
                )
            ]
        )

        steel = self.create_material(
            name="Persistent assignment steel",
            category="metal",
            visual=visual_input(
                color=[0.7, 0.8, 0.9],
                metallic=0.95,
                roughness=0.2
            )
        )

        glass = self.create_material(
            name="Persistent assignment glass",
            category="glass",
            visual=visual_input(
                color=[0.8, 0.9, 1.0],
                opacity=0.3
            )
        )

        first_geometry = self.create_geometry(
            name="Persistent first feature",
            parent_id=self.root_id,
            operation="ADD"
        )

        second_geometry = self.create_geometry(
            name="Persistent second feature",
            parent_id=self.second_id,
            operation="SUBTRACT"
        )

        domain = self.create_geometry(
            name="Persistent independent domain",
            kind="DOMAIN"
        )

        # Resolve entities by persistent identity after every reload.
        # Session identifiers are never assumed to survive reloading.

        material_names = [
            concrete["name"],
            steel["name"],
            glass["name"]
        ]

        transform_global_ids = [
            ROOT_GLOBAL_ID,
            SECOND_GLOBAL_ID,
            THIRD_GLOBAL_ID
        ]

        feature_global_ids = [
            first_geometry["globalId"],
            second_geometry["globalId"]
        ]

        expected_semantic = {
            concrete["name"]: [
                ("transform", ROOT_GLOBAL_ID),
                ("transform", SECOND_GLOBAL_ID),
                ("geometry", first_geometry["globalId"])
            ],
            steel["name"]: [
                ("transform", ROOT_GLOBAL_ID),
                ("geometry", second_geometry["globalId"])
            ],
            glass["name"]: [
                ("transform", THIRD_GLOBAL_ID)
            ]
        }

        expected_native = {
            ROOT_GLOBAL_ID: [concrete["name"]],
            SECOND_GLOBAL_ID: [steel["name"]],
            THIRD_GLOBAL_ID: [glass["name"]]
        }

        def resolve_target(category, global_id):
            if category == "transform":
                return self.find_transform(global_id)["id"]

            return self.find_geometry(global_id)["id"]

        for material_name, targets in expected_semantic.items():
            material = self.find_material(material_name)

            for category, global_id in targets:
                self.assign_material(
                    material["id"],
                    resolve_target(category, global_id)
                )

        for global_id, names in expected_native.items():
            transform = self.find_transform(global_id)
            native = self.native_geometry(transform["id"])

            for material_name in names:
                material = self.find_material(material_name)

                self.assign_material(
                    material["id"],
                    native["id"]
                )

        expected_material_count = len(
            self.list_materials()
        )

        expected_geometry_count = len(
            self.list_geometries()
        )

        def assert_persistent_assignments():
            current_materials = {
                name: self.find_material(name)
                for name in material_names
            }

            all_semantic_targets = [
                ("transform", global_id)
                for global_id in transform_global_ids
            ]

            all_semantic_targets.extend(
                ("geometry", global_id)
                for global_id in feature_global_ids
            )

            for category, global_id in all_semantic_targets:
                target_id = resolve_target(
                    category,
                    global_id
                )

                expected_ids = [
                    current_materials[name]["id"]
                    for name, targets in expected_semantic.items()
                    if (category, global_id) in targets
                ]

                self.assert_target_materials(
                    target_id,
                    expected_ids
                )

            reloaded_domain = self.find_geometry(
                domain["globalId"]
            )

            self.assert_target_materials(
                reloaded_domain["id"],
                []
            )

            entities = parse_step_entities(
                self.save_model()
            )

            for name, targets in expected_semantic.items():
                self.assert_semantic_assignments(
                    entities,
                    current_materials[name],
                    [
                        resolve_target(category, global_id)
                        for category, global_id in targets
                    ]
                )

            for global_id, names in expected_native.items():
                transform = self.find_transform(global_id)

                native = self.native_geometry(
                    transform["id"]
                )

                self.assert_native_style_assignments(
                    entities,
                    native["id"],
                    [
                        current_materials[name]
                        for name in names
                    ]
                )

            self.assert_step_references_valid(entities)

            self.assert_index_consistency()

        assert_persistent_assignments()

        for iteration in range(5):
            with self.subTest(roundtrip=iteration):
                result = self.save_and_reload()

                self.assertEqual(
                    result["materialCount"],
                    expected_material_count
                )

                self.assertEqual(
                    result["geometryCount"],
                    expected_geometry_count
                )

                assert_persistent_assignments()

        # Modify one persistent assignment and verify that the
        # other semantic and visual assignments remain intact.

        reloaded_concrete = self.find_material(
            concrete["name"]
        )

        self.unassign_material(
            reloaded_concrete["id"],
            self.second_id
        )

        expected_semantic[concrete["name"]].remove(
            ("transform", SECOND_GLOBAL_ID)
        )

        self.unassign_material(
            reloaded_concrete["id"],
            self.root_native_id
        )

        expected_native[ROOT_GLOBAL_ID].remove(
            concrete["name"]
        )

        reloaded_glass = self.find_material(
            glass["name"]
        )

        reloaded_first_geometry = self.find_geometry(
            first_geometry["globalId"]
        )

        self.assign_material(
            reloaded_glass["id"],
            reloaded_first_geometry["id"]
        )

        expected_semantic[glass["name"]].append(
            (
                "geometry",
                first_geometry["globalId"]
            )
        )

        assert_persistent_assignments()

        for iteration in range(3):
            with self.subTest(modified_roundtrip=iteration):
                self.save_and_reload()

                assert_persistent_assignments()

        # Reassign existing relations after loading to ensure
        # persistence does not break assignment idempotency.

        for material_name, targets in expected_semantic.items():
            material = self.find_material(material_name)

            for category, global_id in targets:
                self.assign_material(
                    material["id"],
                    resolve_target(category, global_id)
                )

        for global_id, names in expected_native.items():
            transform = self.find_transform(global_id)

            native = self.native_geometry(
                transform["id"]
            )

            for material_name in names:
                material = self.find_material(material_name)

                self.assign_material(
                    material["id"],
                    native["id"]
                )

        assert_persistent_assignments()

        # Remove every assignment while retaining all materials
        # and geometric definitions.

        for material_name, targets in expected_semantic.items():
            material = self.find_material(material_name)

            for category, global_id in targets:
                self.unassign_material(
                    material["id"],
                    resolve_target(category, global_id)
                )

            targets.clear()

        for global_id, names in expected_native.items():
            transform = self.find_transform(global_id)

            native = self.native_geometry(
                transform["id"]
            )

            for material_name in names:
                material = self.find_material(material_name)

                self.unassign_material(
                    material["id"],
                    native["id"]
                )

            names.clear()

        assert_persistent_assignments()

        self.save_and_reload()

        assert_persistent_assignments()

        self.assertEqual(
            len(self.list_materials()),
            expected_material_count
        )

        self.assertEqual(
            len(self.list_geometries()),
            expected_geometry_count
        )

        entities = parse_step_entities(self.save_model())

        self.assertFalse(
            any(
                entity["class"] == "IFCRELASSOCIATESMATERIAL"
                for entity in entities.values()
            )
        )

        # Material-owned styled representations are preserved.
        # Only styles attached to actual native representation
        # items must have been removed.

        for entity in entities.values():
            if entity["class"] != "IFCSTYLEDITEM":
                continue

            arguments = split_step_arguments(
                entity["arguments"]
            )

            self.assertEqual(
                arguments[0],
                "$",
                "Unexpected remaining native style assignment"
            )

        self.assert_index_consistency()


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