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


DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

PHYSICAL_GLOBAL_ID = "0000000000000000000001"
PROJECT_GLOBAL_ID = "0000000000000000000002"
VIRTUAL_GLOBAL_ID = "0000000000000000000003"
STOREY_GLOBAL_ID = "0000000000000000000004"

GLOBAL_ID_PATTERN = re.compile(r"^[0-3][0-9A-Za-z_$]{21}$")

EXPECTED_TRANSFORM_FIELDS = {
    "id",
    "ifcEntityId",
    "globalId",
    "name",
    "parents",
    "transforms",
    "properties",
    "geometries",
    "materials",
    "definition"
}

EXPECTED_SUMMARY_FIELDS = {
    "id",
    "ifcEntityId",
    "globalId"
}

EXPECTED_PARENT_FIELDS = {
    "spatial",
    "placement",
    "decomposition"
}

EXPECTED_PLACEMENT_FIELDS = {
    "local",
    "world",
    "editable",
    "sourceType"
}

DEFAULT_PROFILE = [
    [0, 0],
    [5, 0],
    [5, 3],
    [0, 3]
]

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('transforms-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#16=IFCPROJECT('0000000000000000000002',$,'Transform test project',$,$,$,$,(#6),#8);
#17=IFCBUILDINGELEMENTPROXY('0000000000000000000003',$,'Virtual product',$,$,$,$,$,.NOTDEFINED.);
#18=IFCBUILDINGSTOREY('0000000000000000000004',$,'Ground floor',$,$,#19,$,$,.ELEMENT.,0.);
#19=IFCLOCALPLACEMENT($,#5);
ENDSEC;
END-ISO-10303-21;
"""


def matrix(x=0, y=0, z=0):
    return [
        1, 0, 0, x,
        0, 1, 0, y,
        0, 0, 1, z,
        0, 0, 0, 1
    ]


def transform_input(x=0, y=0, z=0, space="parent"):
    return {
        "space": space,
        "matrix": matrix(x, y, z)
    }


def parents_input(
    spatial=None,
    placement=None,
    decomposition=None
):
    return {
        "spatial": spatial,
        "placement": placement,
        "decomposition": decomposition
    }


def property_input(
    name,
    value,
    value_type="IfcLabel",
    property_set="Pset_Test",
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


def visual_input():
    return {
        "color": [0.5, 0.5, 0.5],
        "opacity": 1.0,
        "metallic": 0.0,
        "roughness": 0.5
    }


def mesh_bounds(mesh):
    vertices = mesh["vertices"]

    if not vertices:
        raise AssertionError("Mesh has no vertices")

    return tuple(
        function(vertices[axis::3])
        for function in (min, max)
        for axis in range(3)
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
        kind="SOLID",
        space="world",
        include_children=False,
        geometry_id=None
    ):
        options = {
            "space": space,
            "kind": kind,
            "includeNormals": True,
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

        begin = None

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.normals": 3,
            "mesh.end": 4
        }

        last_event = -1

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

        if begin is None:
            raise AssertionError("Missing mesh.begin event")

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

        if len(mesh["normals"]) % 3 != 0:
            raise AssertionError("Invalid normal array length")

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


class TransformTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-transforms-"
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

        self.assertEqual(
            result["transformCount"],
            len(self.list_transforms())
        )

        self.physical_id = self.find_transform(
            PHYSICAL_GLOBAL_ID
        )["id"]

        self.project_id = self.find_transform(
            PROJECT_GLOBAL_ID
        )["id"]

        self.virtual_id = self.find_transform(
            VIRTUAL_GLOBAL_ID
        )["id"]

        self.storey_id = self.find_transform(
            STOREY_GLOBAL_ID
        )["id"]

        return result

    def save_and_reload(self):
        result = self.client.request(
            "save",
            {"path": str(self.output_path)}
        )

        self.assertEqual(
            result,
            {"path": str(self.output_path)}
        )

        self.assertTrue(self.output_path.is_file())

        self.assertGreater(
            self.output_path.stat().st_size,
            0
        )

        return self.load_model(self.output_path)

    def list_transforms(self):
        return self.client.request("transformGetAll")

    def get_transform(self, identifier):
        return self.client.request(
            "transformGet",
            {"id": identifier}
        )

    def find_transform(self, global_id):
        matches = [
            transform
            for transform in self.list_transforms()
            if transform["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one transform with GlobalId {global_id}"
        )

        return self.get_transform(matches[0]["id"])

    def create_transform(
        self,
        name="Created transform",
        parents=None,
        transform=None,
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
                "transform": (
                    transform_input()
                    if transform is None
                    else transform
                ),
                "properties": (
                    []
                    if properties is None
                    else properties
                )
            }
        )

    def update_transform(self, identifier, **changes):
        return self.client.request(
            "transformUpdate",
            {
                "id": identifier,
                **changes
            }
        )

    def delete_transform(self, identifier):
        result = self.client.request(
            "transformDelete",
            {"id": identifier}
        )

        self.assertEqual(
            result,
            {"id": identifier}
        )

    def list_geometries(self, parent_id=None, kind=None):
        params = {
            "transformId": parent_id
        }

        if kind is not None:
            params["kind"] = kind

        return self.client.request(
            "geometryGetAll",
            params
        )

    def create_geometry(
        self,
        parent_id,
        name="Created geometry",
        kind="DOMAIN",
        operation="ADD",
        transform=None
    ):
        return self.client.request(
            "geometryCreate",
            {
                "parentId": parent_id,
                "name": name,
                "kind": kind,
                "transform": (
                    transform_input()
                    if transform is None
                    else transform
                ),
                "profile": DEFAULT_PROFILE,
                "depth": 2.0,
                "operation": operation
            }
        )

    def get_geometry(self, identifier):
        return self.client.request(
            "geometryGet",
            {"id": identifier}
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

    def create_material(self, name="Created material"):
        return self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": "concrete",
                "visual": visual_input(),
                "properties": []
            }
        )

    def list_materials(self):
        return self.client.request("materialGetAll")

    def get_material(self, identifier):
        return self.client.request(
            "materialGet",
            {"id": identifier}
        )

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

    def delete_material(self, material_id):
        result = self.client.request(
            "materialDelete",
            {"id": material_id}
        )

        self.assertEqual(
            result,
            {"id": material_id}
        )

    def assert_matrix(self, actual, expected):
        self.assertIsInstance(actual, list)
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertIn(type(value), (int, float))
            self.assertTrue(math.isfinite(value))

            self.assertAlmostEqual(
                value,
                reference,
                places=7,
                msg=f"Invalid matrix component {index}"
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
                places=6,
                msg=f"Invalid bounding box component {index}"
            )

    def assert_identity(self, before, after):
        for field in EXPECTED_SUMMARY_FIELDS:
            self.assertEqual(
                before[field],
                after[field],
                f"Transform identity changed: {field}"
            )

    def assert_reference_list(self, references, category):
        self.assertIsInstance(references, list)

        self.assertEqual(
            len(references),
            len(set(references))
        )

        for identifier in references:
            self.assertIsInstance(identifier, str)

            self.assertTrue(
                identifier.startswith(f"{category}:")
            )

    def assert_transform_structure(self, transform):
        self.assertIsInstance(transform, dict)

        self.assertEqual(
            set(transform),
            EXPECTED_TRANSFORM_FIELDS
        )

        self.assertIsInstance(transform["id"], str)

        self.assertTrue(
            transform["id"].startswith("transform:")
        )

        self.assertIs(
            type(transform["ifcEntityId"]),
            int
        )

        self.assertGreater(
            transform["ifcEntityId"],
            0
        )

        if transform["globalId"] is not None:
            self.assertIsInstance(
                transform["globalId"],
                str
            )

            self.assertRegex(
                transform["globalId"],
                GLOBAL_ID_PATTERN
            )

        self.assertIsInstance(transform["name"], str)
        self.assertIsInstance(transform["parents"], dict)

        self.assertEqual(
            set(transform["parents"]),
            EXPECTED_PARENT_FIELDS
        )

        for parent_id in transform["parents"].values():
            if parent_id is not None:
                self.assertIsInstance(parent_id, str)

                self.assertTrue(
                    parent_id.startswith("transform:")
                )

        placement = transform["transforms"]

        self.assertIsInstance(placement, dict)

        self.assertEqual(
            set(placement),
            EXPECTED_PLACEMENT_FIELDS
        )

        self.assertIs(
            type(placement["editable"]),
            bool
        )

        self.assertIsInstance(
            placement["sourceType"],
            str
        )

        for field in ("local", "world"):
            values = placement[field]

            self.assertIsInstance(values, list)
            self.assertEqual(len(values), 16)

            for value in values:
                self.assertIn(type(value), (int, float))
                self.assertTrue(math.isfinite(value))

        self.assertIsInstance(
            transform["properties"],
            list
        )

        self.assert_reference_list(
            transform["geometries"],
            "geometry"
        )

        self.assert_reference_list(
            transform["materials"],
            "material"
        )

        definition = transform["definition"]

        self.assertIsInstance(definition, dict)

        self.assertTrue(
            {
                "ifcClass",
                "attributes",
                "references"
            }.issubset(definition)
        )

        self.assertIsInstance(
            definition["ifcClass"],
            str
        )

        self.assertTrue(
            definition["ifcClass"]
        )

        self.assertIsInstance(
            definition["attributes"],
            dict
        )

        self.assertIsInstance(
            definition["references"],
            dict
        )

    def assert_properties(self, actual, expected):
        self.assertIsInstance(actual, list)

        def property_key(item):
            return (
                item["propertySet"],
                item["name"],
                item["source"]
            )

        actual_index = {
            property_key(item): item
            for item in actual
        }

        expected_index = {
            property_key(item): item
            for item in expected
        }

        self.assertEqual(
            len(actual),
            len(actual_index),
            "Duplicate properties"
        )

        self.assertEqual(
            set(actual_index),
            set(expected_index)
        )

        for key, reference in expected_index.items():
            with self.subTest(property=key):
                value = actual_index[key]

                self.assertEqual(
                    value,
                    reference
                )

                if reference["valueType"] == "IfcBoolean":
                    self.assertIs(
                        type(value["value"]),
                        bool
                    )

                elif reference["valueType"] == "IfcInteger":
                    self.assertIs(
                        type(value["value"]),
                        int
                    )

    def assert_empty_mesh(self, mesh):
        self.assertEqual(mesh["vertexCount"], 0)
        self.assertEqual(mesh["indexCount"], 0)
        self.assertEqual(mesh["vertices"], [])
        self.assertEqual(mesh["indices"], [])
        self.assertEqual(mesh["normals"], [])

    def assert_geometry_links(self, transform_id, expected_ids):
        transform = self.get_transform(transform_id)

        actual_ids = transform["geometries"]

        self.assertEqual(
            len(actual_ids),
            len(set(actual_ids))
        )

        self.assertEqual(
            set(actual_ids),
            set(expected_ids)
        )

        filtered_ids = {
            geometry["id"]
            for geometry in self.list_geometries(
                parent_id=transform_id
            )
        }

        self.assertEqual(
            filtered_ids,
            set(expected_ids)
        )

        for geometry_id in actual_ids:
            geometry = self.get_geometry(geometry_id)

            self.assertEqual(
                geometry["parentId"],
                transform_id
            )

    def assert_material_links(self, transform_id, expected_ids):
        transform = self.get_transform(transform_id)

        actual_ids = transform["materials"]

        self.assertEqual(
            len(actual_ids),
            len(set(actual_ids))
        )

        self.assertEqual(
            set(actual_ids),
            set(expected_ids)
        )

        available_ids = {
            material["id"]
            for material in self.list_materials()
        }

        for material_id in actual_ids:
            self.assertIn(
                material_id,
                available_ids
            )

            material = self.get_material(material_id)

            self.assertEqual(
                material["id"],
                material_id
            )

    def test_transform_listing(self):
        """Verify listing of all indexed transforms."""

        transforms = self.list_transforms()

        self.assertIsInstance(transforms, list)
        self.assertEqual(len(transforms), 4)

        expected_global_ids = {
            PHYSICAL_GLOBAL_ID,
            PROJECT_GLOBAL_ID,
            VIRTUAL_GLOBAL_ID,
            STOREY_GLOBAL_ID
        }

        actual_global_ids = {
            transform["globalId"]
            for transform in transforms
        }

        self.assertEqual(
            actual_global_ids,
            expected_global_ids
        )

        identifiers = [
            transform["id"]
            for transform in transforms
        ]

        self.assertEqual(
            len(identifiers),
            len(set(identifiers))
        )

        for summary in transforms:
            with self.subTest(identifier=summary["id"]):
                self.assertEqual(
                    set(summary),
                    EXPECTED_SUMMARY_FIELDS
                )

                self.assertTrue(
                    summary["id"].startswith("transform:")
                )

                full = self.get_transform(summary["id"])

                self.assert_identity(summary, full)
                self.assert_transform_structure(full)

        created = self.create_transform(
            name="Additional transform"
        )

        updated = self.list_transforms()

        self.assertEqual(len(updated), 5)

        self.assertIn(
            created["globalId"],
            {
                transform["globalId"]
                for transform in updated
            }
        )

        domain = self.create_geometry(
            parent_id=self.physical_id,
            name="Unindexed domain"
        )

        self.assertEqual(
            domain["kind"],
            "DOMAIN"
        )

        self.assertEqual(
            self.list_transforms(),
            updated,
            "Managed domains must not appear as transforms"
        )

        self.delete_transform(created["id"])

        self.assertEqual(
            self.list_transforms(),
            transforms
        )

    def test_transform_get(self):
        """Verify the complete transformGet response structure."""

        expected = {
            self.physical_id: {
                "name": "Physical solid",
                "ifcClass": "IfcBuildingElementProxy",
                "sourceType": "IfcLocalPlacement",
                "editable": True,
                "geometryCount": 1
            },
            self.project_id: {
                "name": "Transform test project",
                "ifcClass": "IfcProject",
                "sourceType": "virtual",
                "editable": False,
                "geometryCount": 0
            },
            self.virtual_id: {
                "name": "Virtual product",
                "ifcClass": "IfcBuildingElementProxy",
                "sourceType": "virtual",
                "editable": True,
                "geometryCount": 0
            },
            self.storey_id: {
                "name": "Ground floor",
                "ifcClass": "IfcBuildingStorey",
                "sourceType": "IfcLocalPlacement",
                "editable": True,
                "geometryCount": 0
            }
        }

        for identifier, reference in expected.items():
            with self.subTest(identifier=identifier):
                transform = self.get_transform(identifier)

                self.assert_transform_structure(transform)

                self.assertEqual(
                    transform["id"],
                    identifier
                )

                self.assertEqual(
                    transform["name"],
                    reference["name"]
                )

                self.assertEqual(
                    transform["definition"]["ifcClass"],
                    reference["ifcClass"]
                )

                self.assertEqual(
                    transform["transforms"]["sourceType"],
                    reference["sourceType"]
                )

                self.assertIs(
                    transform["transforms"]["editable"],
                    reference["editable"]
                )

                self.assertEqual(
                    len(transform["geometries"]),
                    reference["geometryCount"]
                )

                self.assertEqual(
                    transform["parents"],
                    parents_input()
                )

                self.assertEqual(
                    transform["properties"],
                    []
                )

                self.assertEqual(
                    transform["materials"],
                    []
                )

                self.assert_matrix(
                    transform["transforms"]["local"],
                    matrix()
                )

                self.assert_matrix(
                    transform["transforms"]["world"],
                    matrix()
                )

    def test_transform_creation(self):
        """Verify creation of an IfcBuildingElementProxy transform."""

        original_transforms = self.list_transforms()
        original_geometries = self.list_geometries()

        self.update_transform(
            self.physical_id,
            transform=transform_input(
                x=10,
                y=20,
                z=30,
                space="world"
            )
        )

        parents = parents_input(
            spatial=self.storey_id,
            placement=self.physical_id
        )

        created = self.create_transform(
            name="Created proxy",
            parents=parents,
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        self.assert_transform_structure(created)

        self.assertEqual(
            created["definition"]["ifcClass"],
            "IfcBuildingElementProxy"
        )

        self.assertRegex(
            created["globalId"],
            GLOBAL_ID_PATTERN
        )

        self.assertEqual(
            created["name"],
            "Created proxy"
        )

        self.assertEqual(
            created["parents"],
            parents
        )

        self.assertEqual(
            created["transforms"]["sourceType"],
            "IfcLocalPlacement"
        )

        self.assertIs(
            created["transforms"]["editable"],
            True
        )

        self.assert_matrix(
            created["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            created["transforms"]["world"],
            matrix(12, 23, 34)
        )

        self.assertEqual(created["properties"], [])
        self.assertEqual(created["geometries"], [])
        self.assertEqual(created["materials"], [])

        self.assertEqual(
            self.get_transform(created["id"]),
            created
        )

        self.assertEqual(
            len(self.list_transforms()),
            len(original_transforms) + 1
        )

        self.assertEqual(
            self.list_geometries(),
            original_geometries
        )

        world_created = self.create_transform(
            name="World-space proxy",
            parents=parents,
            transform=transform_input(
                x=100,
                y=200,
                z=300,
                space="world"
            )
        )

        self.assert_matrix(
            world_created["transforms"]["local"],
            matrix(90, 180, 270)
        )

        self.assert_matrix(
            world_created["transforms"]["world"],
            matrix(100, 200, 300)
        )

        self.assertNotEqual(
            created["id"],
            world_created["id"]
        )

        self.assertNotEqual(
            created["globalId"],
            world_created["globalId"]
        )

        result = self.save_and_reload()

        self.assertEqual(
            result["transformCount"],
            6
        )

        reloaded = self.find_transform(
            created["globalId"]
        )

        self.assertEqual(
            reloaded["name"],
            "Created proxy"
        )

        self.assertEqual(
            reloaded["parents"],
            parents_input(
                spatial=self.storey_id,
                placement=self.physical_id
            )
        )

        self.assert_matrix(
            reloaded["transforms"]["world"],
            matrix(12, 23, 34)
        )

        reloaded_world = self.find_transform(
            world_created["globalId"]
        )

        self.assert_matrix(
            reloaded_world["transforms"]["world"],
            matrix(100, 200, 300)
        )

    def test_transform_rename(self):
        """Verify renaming native and newly created transforms."""

        created = self.create_transform(
            name="Original created name"
        )

        cases = [
            (
                self.physical_id,
                "Renamed physical solid"
            ),
            (
                created["id"],
                "Renamed created transform"
            ),
            (
                self.virtual_id,
                "Renamed virtual product"
            )
        ]

        expected_names = {}

        for identifier, name in cases:
            with self.subTest(identifier=identifier):
                before = self.get_transform(identifier)

                updated = self.update_transform(
                    identifier,
                    name=name
                )

                self.assert_identity(before, updated)

                expected = {
                    **before,
                    "name": name
                }

                self.assertEqual(updated, expected)

                self.assertEqual(
                    self.get_transform(identifier),
                    expected
                )

                expected_names[before["globalId"]] = name

        self.save_and_reload()

        for global_id, name in expected_names.items():
            with self.subTest(global_id=global_id):
                reloaded = self.find_transform(global_id)

                self.assertEqual(
                    reloaded["name"],
                    name
                )

    def test_transform_partial_update(self):
        """Verify preservation of omitted transform fields."""

        properties = [
            property_input(
                "Reference",
                "Initial reference"
            ),
            property_input(
                "Count",
                12,
                value_type="IfcInteger"
            )
        ]

        created = self.create_transform(
            name="Partial update transform",
            parents=parents_input(
                spatial=self.storey_id,
                placement=self.physical_id
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            ),
            properties=properties
        )

        identifier = created["id"]

        domain = self.create_geometry(
            parent_id=identifier,
            name="Preserved domain"
        )

        material = self.create_material(
            name="Preserved material"
        )

        self.assign_material(
            material["id"],
            identifier
        )

        before = self.get_transform(identifier)

        renamed = self.update_transform(
            identifier,
            name="Updated name"
        )

        self.assertEqual(
            renamed,
            {
                **before,
                "name": "Updated name"
            }
        )

        moved = self.update_transform(
            identifier,
            transform=transform_input(
                x=10,
                y=20,
                z=30
            )
        )

        self.assert_identity(renamed, moved)

        for field in (
            "name",
            "parents",
            "properties",
            "geometries",
            "materials",
            "definition"
        ):
            self.assertEqual(
                moved[field],
                renamed[field],
                f"Unexpected modification of {field}"
            )

        self.assert_matrix(
            moved["transforms"]["local"],
            matrix(10, 20, 30)
        )

        self.assert_matrix(
            moved["transforms"]["world"],
            matrix(10, 20, 30)
        )

        updated_properties = [
            property_input(
                "Reference",
                "Updated reference"
            )
        ]

        updated = self.update_transform(
            identifier,
            properties=updated_properties
        )

        self.assert_identity(moved, updated)

        self.assert_properties(
            updated["properties"],
            updated_properties
        )

        for field in (
            "name",
            "parents",
            "transforms",
            "geometries",
            "materials",
            "definition"
        ):
            self.assertEqual(
                updated[field],
                moved[field]
            )

        cleared = self.update_transform(
            identifier,
            properties=[]
        )

        self.assertEqual(
            cleared["properties"],
            []
        )

        self.assertIn(
            domain["id"],
            cleared["geometries"]
        )

        self.assertIn(
            material["id"],
            cleared["materials"]
        )

        self.assertEqual(
            cleared["name"],
            "Updated name"
        )

        self.assert_matrix(
            cleared["transforms"]["world"],
            matrix(10, 20, 30)
        )

        self.assertEqual(
            self.get_transform(identifier),
            cleared
        )

    def test_empty_update(self):
        """Verify rejection of updates without modifiable fields."""

        created = self.create_transform(
            name="Unchanged transform"
        )

        before = self.get_transform(created["id"])

        invalid_updates = [
            {},
            {"id": created["id"]},
            {
                "id": created["id"],
                "debug": True
            },
            {
                "id": created["id"],
                "definition": {}
            },
            {
                "id": created["id"],
                "ifcClass": "IfcWall"
            },
            {
                "id": created["id"],
                "globalId": PROJECT_GLOBAL_ID
            },
            {
                "id": created["id"],
                "name": None
            },
            {
                "id": created["id"],
                "transform": None
            },
            {
                "id": created["id"],
                "parents": None
            },
            {
                "id": created["id"],
                "properties": None
            }
        ]

        for params in invalid_updates:
            with self.subTest(params=params):
                self.client.expect_error(
                    "transformUpdate",
                    params,
                    "INVALID_PARAMETERS"
                )

                self.assertEqual(
                    self.get_transform(created["id"]),
                    before
                )

        valid = self.update_transform(
            created["id"],
            name="Valid update"
        )

        self.assertEqual(
            valid["name"],
            "Valid update"
        )

        self.assert_identity(before, valid)

    def test_transform_without_geometry(self):
        """Verify creation and use of a transform without geometry."""

        created = self.create_transform(
            name="Empty transform",
            transform=transform_input(
                x=10,
                y=20,
                z=30
            )
        )

        identifier = created["id"]

        self.assert_geometry_links(identifier, [])

        self.assertEqual(
            created["materials"],
            []
        )

        for kind in ("SOLID", "DOMAIN"):
            for space in ("local", "world"):
                with self.subTest(kind=kind, space=space):
                    mesh = self.client.tessellate(
                        identifier,
                        kind=kind,
                        space=space
                    )

                    self.assert_empty_mesh(mesh)

        updated = self.update_transform(
            identifier,
            name="Updated empty transform",
            transform=transform_input(
                x=50,
                y=60,
                z=70
            )
        )

        self.assertEqual(
            updated["name"],
            "Updated empty transform"
        )

        self.assert_matrix(
            updated["transforms"]["world"],
            matrix(50, 60, 70)
        )

        self.assert_geometry_links(identifier, [])

        self.save_and_reload()

        reloaded = self.find_transform(
            created["globalId"]
        )

        identifier = reloaded["id"]

        self.assert_geometry_links(identifier, [])

        self.assert_matrix(
            reloaded["transforms"]["world"],
            matrix(50, 60, 70)
        )

        self.assert_empty_mesh(
            self.client.tessellate(
                identifier,
                kind="SOLID"
            )
        )

        self.delete_transform(identifier)

        self.client.expect_error(
            "transformGet",
            {"id": identifier},
            "ENTITY_NOT_FOUND"
        )

    def test_transform_with_properties(self):
        """Verify transform creation with typed occurrence properties."""

        properties = [
            property_input(
                "Reference",
                "TR-001"
            ),
            property_input(
                "Description",
                "Transform with typed properties",
                value_type="IfcText"
            ),
            property_input(
                "Count",
                42,
                value_type="IfcInteger"
            ),
            property_input(
                "Coefficient",
                3.125,
                value_type="IfcReal"
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
                "Simulation reference",
                property_set="Pset_Simulation"
            )
        ]

        created = self.create_transform(
            name="Transform with properties",
            properties=properties
        )

        self.assert_transform_structure(created)

        self.assert_properties(
            created["properties"],
            properties
        )

        retrieved = self.get_transform(created["id"])

        self.assert_properties(
            retrieved["properties"],
            properties
        )

        self.assertEqual(
            retrieved["geometries"],
            []
        )

        self.assertEqual(
            retrieved["materials"],
            []
        )

        self.save_and_reload()

        reloaded = self.find_transform(
            created["globalId"]
        )

        self.assert_properties(
            reloaded["properties"],
            properties
        )

        replacement = [
            property_input(
                "Reference",
                "TR-002"
            ),
            property_input(
                "Enabled",
                False,
                value_type="IfcBoolean"
            )
        ]

        updated = self.update_transform(
            reloaded["id"],
            properties=replacement
        )

        self.assert_properties(
            updated["properties"],
            replacement
        )

        self.assertEqual(
            updated["globalId"],
            created["globalId"]
        )

        self.save_and_reload()

        reloaded = self.find_transform(
            created["globalId"]
        )

        self.assert_properties(
            reloaded["properties"],
            replacement
        )

    def test_transform_virtual(self):
        """Verify virtual transforms without IFC object placements."""

        cases = [
            (self.project_id, False),
            (self.virtual_id, True)
        ]

        for identifier, editable in cases:
            with self.subTest(identifier=identifier):
                transform = self.get_transform(identifier)

                self.assert_transform_structure(transform)

                self.assertEqual(
                    transform["transforms"]["sourceType"],
                    "virtual"
                )

                self.assertIs(
                    transform["transforms"]["editable"],
                    editable
                )

                self.assert_matrix(
                    transform["transforms"]["local"],
                    matrix()
                )

                self.assert_matrix(
                    transform["transforms"]["world"],
                    matrix()
                )

                self.assertIsNone(
                    transform["parents"]["placement"]
                )

                self.assertEqual(
                    transform["geometries"],
                    []
                )

        virtual = self.get_transform(self.virtual_id)

        renamed = self.update_transform(
            self.virtual_id,
            name="Renamed virtual product"
        )

        self.assert_identity(virtual, renamed)

        self.assertEqual(
            renamed["transforms"]["sourceType"],
            "virtual"
        )

        self.assertIs(
            renamed["transforms"]["editable"],
            True
        )

        self.assert_empty_mesh(
            self.client.tessellate(
                self.virtual_id,
                kind="SOLID"
            )
        )

        self.save_and_reload()

        reloaded = self.get_transform(self.virtual_id)

        self.assertEqual(
            reloaded["name"],
            "Renamed virtual product"
        )

        self.assertEqual(
            reloaded["transforms"]["sourceType"],
            "virtual"
        )

        self.assert_matrix(
            reloaded["transforms"]["world"],
            matrix()
        )

    def test_virtual_placement_creation(self):
        """Verify materialization of a compatible virtual placement."""

        before = self.get_transform(self.virtual_id)

        self.assertEqual(
            before["transforms"]["sourceType"],
            "virtual"
        )

        self.assertIs(
            before["transforms"]["editable"],
            True
        )

        updated = self.update_transform(
            self.virtual_id,
            transform=transform_input(
                x=10,
                y=20,
                z=30
            )
        )

        self.assert_identity(before, updated)

        self.assertEqual(
            updated["transforms"]["sourceType"],
            "IfcLocalPlacement"
        )

        self.assertIs(
            updated["transforms"]["editable"],
            True
        )

        self.assert_matrix(
            updated["transforms"]["local"],
            matrix(10, 20, 30)
        )

        self.assert_matrix(
            updated["transforms"]["world"],
            matrix(10, 20, 30)
        )

        self.assertEqual(
            updated["parents"],
            parents_input()
        )

        self.assert_geometry_links(self.virtual_id, [])

        domain = self.create_geometry(
            parent_id=self.virtual_id,
            name="Domain on materialized transform"
        )

        self.assert_geometry_links(
            self.virtual_id,
            [domain["id"]]
        )

        world_mesh = self.client.tessellate(
            self.virtual_id,
            kind="DOMAIN",
            space="world"
        )

        local_mesh = self.client.tessellate(
            self.virtual_id,
            kind="DOMAIN",
            space="local"
        )

        self.assert_bounds(
            world_mesh,
            (10, 20, 30, 15, 23, 32)
        )

        self.assert_bounds(
            local_mesh,
            (0, 0, 0, 5, 3, 2)
        )

        self.save_and_reload()

        reloaded = self.get_transform(self.virtual_id)

        self.assertEqual(
            reloaded["globalId"],
            VIRTUAL_GLOBAL_ID
        )

        self.assertEqual(
            reloaded["transforms"]["sourceType"],
            "IfcLocalPlacement"
        )

        self.assert_matrix(
            reloaded["transforms"]["world"],
            matrix(10, 20, 30)
        )

        domains = self.list_geometries(
            parent_id=self.virtual_id,
            kind="DOMAIN"
        )

        self.assertEqual(len(domains), 1)

        self.assertEqual(
            domains[0]["globalId"],
            domain["globalId"]
        )

        self.assert_bounds(
            self.client.tessellate(
                self.virtual_id,
                kind="DOMAIN"
            ),
            (10, 20, 30, 15, 23, 32)
        )

    def test_readonly_transform(self):
        """Verify rejection of unsupported transform placement updates."""

        before = self.get_transform(self.project_id)

        self.assertEqual(
            before["definition"]["ifcClass"],
            "IfcProject"
        )

        self.assertEqual(
            before["transforms"]["sourceType"],
            "virtual"
        )

        self.assertIs(
            before["transforms"]["editable"],
            False
        )

        for space in ("parent", "world"):
            with self.subTest(space=space):
                self.client.expect_error(
                    "transformUpdate",
                    {
                        "id": self.project_id,
                        "transform": transform_input(
                            x=10,
                            y=20,
                            z=30,
                            space=space
                        )
                    },
                    "TRANSFORM_NOT_EDITABLE"
                )

                self.assertEqual(
                    self.get_transform(self.project_id),
                    before
                )

        self.client.expect_error(
            "transformUpdate",
            {
                "id": self.project_id,
                "definition": {
                    "ifcClass": "IfcBuildingElementProxy"
                }
            },
            "INVALID_PARAMETERS"
        )

        self.assertEqual(
            self.get_transform(self.project_id),
            before
        )

        renamed = self.update_transform(
            self.project_id,
            name="Renamed project"
        )

        self.assert_identity(before, renamed)

        self.assertEqual(
            renamed["name"],
            "Renamed project"
        )

        self.assertEqual(
            renamed["transforms"],
            before["transforms"]
        )

        self.assertIs(
            renamed["transforms"]["editable"],
            False
        )

        self.save_and_reload()

        reloaded = self.get_transform(self.project_id)

        self.assertEqual(
            reloaded["name"],
            "Renamed project"
        )

        self.assertEqual(
            reloaded["definition"]["ifcClass"],
            "IfcProject"
        )

        self.assertIs(
            reloaded["transforms"]["editable"],
            False
        )

        self.assertEqual(
            reloaded["transforms"]["sourceType"],
            "virtual"
        )

    def test_transform_geometry_links(self):
        """Verify transform geometry references throughout their lifecycle."""

        original = self.get_transform(self.physical_id)

        original_geometry_ids = set(
            original["geometries"]
        )

        self.assertEqual(
            len(original_geometry_ids),
            1
        )

        domain = self.create_geometry(
            parent_id=self.physical_id,
            name="Linked domain"
        )

        solid = self.create_geometry(
            parent_id=self.physical_id,
            name="Linked solid",
            kind="SOLID",
            operation="ADD"
        )

        opening = self.create_geometry(
            parent_id=self.physical_id,
            name="Linked opening",
            kind="SOLID",
            operation="SUBTRACT"
        )

        expected_ids = original_geometry_ids | {
            domain["id"],
            solid["id"],
            opening["id"]
        }

        self.assert_geometry_links(
            self.physical_id,
            expected_ids
        )

        solid_ids = {
            geometry["id"]
            for geometry in self.list_geometries(
                parent_id=self.physical_id,
                kind="SOLID"
            )
        }

        domain_ids = {
            geometry["id"]
            for geometry in self.list_geometries(
                parent_id=self.physical_id,
                kind="DOMAIN"
            )
        }

        self.assertEqual(
            solid_ids,
            original_geometry_ids | {
                solid["id"],
                opening["id"]
            }
        )

        self.assertEqual(
            domain_ids,
            {domain["id"]}
        )

        self.assertFalse(solid_ids & domain_ids)

        self.update_transform(
            self.physical_id,
            name="Renamed geometry parent"
        )

        self.assert_geometry_links(
            self.physical_id,
            expected_ids
        )

        self.client.request(
            "geometryUpdate",
            {
                "id": domain["id"],
                "name": "Updated linked domain",
                "depth": 5.0
            }
        )

        self.assert_geometry_links(
            self.physical_id,
            expected_ids
        )

        converted = self.client.request(
            "geometryUpdate",
            {
                "id": solid["id"],
                "operation": "SUBTRACT"
            }
        )

        self.assertEqual(
            converted["id"],
            solid["id"]
        )

        self.assert_geometry_links(
            self.physical_id,
            expected_ids
        )

        self.delete_geometry(opening["id"])
        expected_ids.remove(opening["id"])

        self.assert_geometry_links(
            self.physical_id,
            expected_ids
        )

        self.delete_geometry(solid["id"])
        expected_ids.remove(solid["id"])

        self.assert_geometry_links(
            self.physical_id,
            expected_ids
        )

        self.save_and_reload()

        geometries = self.list_geometries(
            parent_id=self.physical_id
        )

        self.assertEqual(len(geometries), 2)

        domains = [
            geometry
            for geometry in geometries
            if geometry["kind"] == "DOMAIN"
        ]

        self.assertEqual(len(domains), 1)

        self.assertEqual(
            domains[0]["globalId"],
            domain["globalId"]
        )

        reloaded_domain = self.get_geometry(
            domains[0]["id"]
        )

        self.assertEqual(
            reloaded_domain["name"],
            "Updated linked domain"
        )

        self.assertAlmostEqual(
            reloaded_domain["depth"],
            5.0
        )

        self.delete_geometry(reloaded_domain["id"])

        remaining = self.list_geometries(
            parent_id=self.physical_id
        )

        self.assertEqual(len(remaining), 1)

        self.assertEqual(
            remaining[0]["source"],
            "ifc"
        )

        self.assert_geometry_links(
            self.physical_id,
            [remaining[0]["id"]]
        )

        self.assert_bounds(
            self.client.tessellate(
                self.physical_id,
                kind="SOLID"
            ),
            (-1, -0.5, 0, 1, 0.5, 1)
        )

    def test_transform_material_links(self):
        """Verify transform material references throughout their lifecycle."""

        first = self.create_transform(
            name="First material target"
        )

        second = self.create_transform(
            name="Second material target"
        )

        material = self.create_material(
            name="Shared transform material"
        )

        self.assert_material_links(first["id"], [])
        self.assert_material_links(second["id"], [])

        self.assign_material(
            material["id"],
            first["id"]
        )

        self.assert_material_links(
            first["id"],
            [material["id"]]
        )

        self.assert_material_links(
            second["id"],
            []
        )

        self.assign_material(
            material["id"],
            first["id"]
        )

        self.assert_material_links(
            first["id"],
            [material["id"]]
        )

        self.assign_material(
            material["id"],
            second["id"]
        )

        self.assert_material_links(
            first["id"],
            [material["id"]]
        )

        self.assert_material_links(
            second["id"],
            [material["id"]]
        )

        self.update_transform(
            first["id"],
            name="Renamed material target",
            transform=transform_input(
                x=10,
                y=20,
                z=30
            )
        )

        self.assert_material_links(
            first["id"],
            [material["id"]]
        )

        updated_material = self.client.request(
            "materialUpdate",
            {
                "id": material["id"],
                "name": "Updated shared material"
            }
        )

        self.assertEqual(
            updated_material["id"],
            material["id"]
        )

        self.assert_material_links(
            first["id"],
            [material["id"]]
        )

        self.assert_material_links(
            second["id"],
            [material["id"]]
        )

        self.unassign_material(
            material["id"],
            first["id"]
        )

        self.assert_material_links(
            first["id"],
            []
        )

        self.assert_material_links(
            second["id"],
            [material["id"]]
        )

        self.save_and_reload()

        first = self.find_transform(first["globalId"])
        second = self.find_transform(second["globalId"])

        material = self.find_material(
            "Updated shared material"
        )

        self.assert_material_links(
            first["id"],
            []
        )

        self.assert_material_links(
            second["id"],
            [material["id"]]
        )

        self.assign_material(
            material["id"],
            first["id"]
        )

        self.assert_material_links(
            first["id"],
            [material["id"]]
        )

        deleted_material_id = material["id"]

        self.delete_material(deleted_material_id)

        self.assert_material_links(first["id"], [])
        self.assert_material_links(second["id"], [])

        self.client.expect_error(
            "materialGet",
            {"id": deleted_material_id},
            "ENTITY_NOT_FOUND"
        )

        self.assertEqual(
            self.list_materials(),
            []
        )

        self.save_and_reload()

        first = self.find_transform(first["globalId"])
        second = self.find_transform(second["globalId"])

        self.assert_material_links(first["id"], [])
        self.assert_material_links(second["id"], [])

        self.assertEqual(
            self.list_materials(),
            []
        )

        self.assertEqual(
            first["name"],
            "Renamed material target"
        )

        self.assert_matrix(
            first["transforms"]["world"],
            matrix(10, 20, 30)
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