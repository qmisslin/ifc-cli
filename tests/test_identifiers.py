import json
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

PARENT_GLOBAL_ID = "0000000000000000000001"
PROJECT_GLOBAL_ID = "0000000000000000000002"

SECOND_PARENT_GLOBAL_ID = "0000000000000000000011"
SECOND_PROJECT_GLOBAL_ID = "0000000000000000000012"

GLOBAL_ID_PATTERN = re.compile(
    r"^[0-3][0-9A-Za-z_$]{21}$"
)

ENTITY_COMMANDS = {
    "transform": "transformGet",
    "geometry": "geometryGet",
    "material": "materialGet"
}

DEFAULT_PROFILE = [
    [0, 0],
    [5, 0],
    [5, 3],
    [0, 3]
]


def make_ifc_fixture(
    parent_global_id=PARENT_GLOBAL_ID,
    project_global_id=PROJECT_GLOBAL_ID,
    parent_name="Physical solid",
    project_name="Identifier test project"
):
    return f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('identifiers-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#15=IFCBUILDINGELEMENTPROXY('{parent_global_id}',$,'{parent_name}',$,$,#4,#14,$,.NOTDEFINED.);
#16=IFCPROJECT('{project_global_id}',$,'{project_name}',$,$,$,$,(#6),#8);
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


def visual_input():
    return {
        "color": [0.5, 0.5, 0.5],
        "opacity": 1.0,
        "metallic": 0.0,
        "roughness": 0.5
    }


def tessellation_options():
    return {
        "space": "world",
        "includeNormals": True,
        "includeMaterials": False,
        "includeChildren": False,
        "kind": "SOLID"
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
                f"Expected {expected_code}, received {response['error']}"
            )

        return response["error"]

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


class IdentifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-identifiers-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)
        self.input_path = self.root_path / "input.ifc"
        self.second_input_path = self.root_path / "second.ifc"
        self.output_path = self.root_path / "output.ifc"

        self.input_path.write_text(
            make_ifc_fixture(),
            encoding="utf-8"
        )

        self.second_input_path.write_text(
            make_ifc_fixture(
                parent_global_id=SECOND_PARENT_GLOBAL_ID,
                project_global_id=SECOND_PROJECT_GLOBAL_ID,
                parent_name="Second physical solid",
                project_name="Second identifier project"
            ),
            encoding="utf-8"
        )

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.load_model()

    def tearDown(self):
        self.client.close()

    def load_model(self, path=None, parent_global_id=PARENT_GLOBAL_ID):
        path = self.input_path if path is None else path

        result = self.client.request(
            "load",
            {"path": str(path)}
        )

        self.assertEqual(
            result["schema"].upper(),
            "IFC4"
        )

        self.parent_id = self.find_by_global_id(
            "transform",
            parent_global_id
        )["id"]

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

        self.assertGreater(
            self.output_path.stat().st_size,
            0
        )

    def save_and_reload(self):
        self.save_model()

        return self.load_model(self.output_path)

    def list_transforms(self):
        return self.client.request(
            "transformGetAll"
        )

    def list_geometries(self, kind=None, parent_id=None):
        params = {
            "transformId": parent_id
        }

        if kind is not None:
            params["kind"] = kind

        return self.client.request(
            "geometryGetAll",
            params
        )

    def list_materials(self):
        return self.client.request(
            "materialGetAll"
        )

    def list_entities(self, category):
        functions = {
            "transform": self.list_transforms,
            "geometry": self.list_geometries,
            "material": self.list_materials
        }

        return functions[category]()

    def get_entity(self, category, identifier):
        return self.client.request(
            ENTITY_COMMANDS[category],
            {"id": identifier}
        )

    def get_transform(self, identifier):
        return self.get_entity(
            "transform",
            identifier
        )

    def get_geometry(self, identifier):
        return self.get_entity(
            "geometry",
            identifier
        )

    def get_material(self, identifier):
        return self.get_entity(
            "material",
            identifier
        )

    def find_by_global_id(self, category, global_id):
        self.assertIsNotNone(global_id)

        matches = [
            item
            for item in self.list_entities(category)
            if item["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one {category} with GlobalId {global_id}"
        )

        return self.get_entity(
            category,
            matches[0]["id"]
        )

    def find_material_by_name(self, name):
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

    def create_transform(
        self,
        name="Created transform",
        placement_parent=None,
        decomposition_parent=None,
        x=0
    ):
        return self.client.request(
            "transformCreate",
            {
                "name": name,
                "parents": parents_input(
                    placement=placement_parent,
                    decomposition=decomposition_parent
                ),
                "transform": transform_input(x=x),
                "properties": []
            }
        )

    def create_geometry(
        self,
        name="Created geometry",
        parent_id=None,
        kind="DOMAIN",
        operation="ADD",
        x=0
    ):
        return self.client.request(
            "geometryCreate",
            {
                "parentId": (
                    self.parent_id
                    if parent_id is None
                    else parent_id
                ),
                "name": name,
                "kind": kind,
                "transform": transform_input(x=x),
                "profile": DEFAULT_PROFILE,
                "depth": 2.0,
                "operation": operation
            }
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

    def delete_entity(self, category, identifier):
        result = self.client.request(
            f"{category}Delete",
            {"id": identifier}
        )

        self.assertEqual(
            result,
            {"id": identifier}
        )

    def snapshot_indexes(self):
        return {
            category: {
                item["id"]: {
                    "ifcEntityId": item["ifcEntityId"],
                    "globalId": item["globalId"]
                }
                for item in self.list_entities(category)
            }
            for category in ENTITY_COMMANDS
        }

    def assert_identifier(self, identifier, category):
        self.assertIsInstance(identifier, str)

        self.assertRegex(
            identifier,
            rf"^{category}:.+$"
        )

    def assert_global_id(self, global_id):
        self.assertIsInstance(global_id, str)

        self.assertRegex(
            global_id,
            GLOBAL_ID_PATTERN
        )

    def assert_identity(self, before, after):
        for field in (
            "id",
            "ifcEntityId",
            "globalId"
        ):
            self.assertEqual(
                before[field],
                after[field],
                f"Entity identity changed: {field}"
            )

    def assert_snapshot_preserved(
        self,
        snapshot,
        excluded_ids=None
    ):
        excluded_ids = set(excluded_ids or [])
        current = self.snapshot_indexes()

        for category, entities in snapshot.items():
            for identifier, identity in entities.items():
                if identifier in excluded_ids:
                    continue

                with self.subTest(
                    category=category,
                    identifier=identifier
                ):
                    self.assertIn(
                        identifier,
                        current[category]
                    )

                    self.assertEqual(
                        current[category][identifier],
                        identity
                    )

    def assert_index_consistency(self):
        summaries = {
            category: self.list_entities(category)
            for category in ENTITY_COMMANDS
        }

        indexes = {}

        for category, entities in summaries.items():
            identifiers = [
                item["id"]
                for item in entities
            ]

            self.assertEqual(
                len(identifiers),
                len(set(identifiers)),
                f"Duplicate {category} identifiers"
            )

            indexes[category] = {}

            for summary in entities:
                identifier = summary["id"]

                self.assert_identifier(
                    identifier,
                    category
                )

                entity = self.get_entity(
                    category,
                    identifier
                )

                self.assert_identity(
                    summary,
                    entity
                )

                indexes[category][identifier] = entity

        transforms = indexes["transform"]
        geometries = indexes["geometry"]
        materials = indexes["material"]

        all_step_ids = []

        for entities in indexes.values():
            all_step_ids.extend(
                entity["ifcEntityId"]
                for entity in entities.values()
            )

        self.assertEqual(
            len(all_step_ids),
            len(set(all_step_ids)),
            "An IFC entity is indexed more than once"
        )

        for transform_id, transform in transforms.items():
            for parent_type, parent_id in transform["parents"].items():
                if parent_id is None:
                    continue

                self.assertIn(
                    parent_id,
                    transforms,
                    f"Invalid {parent_type} parent"
                )

                self.assertNotEqual(
                    parent_id,
                    transform_id
                )

            geometry_ids = transform["geometries"]

            self.assertEqual(
                len(geometry_ids),
                len(set(geometry_ids))
            )

            expected_geometry_ids = {
                geometry_id
                for geometry_id, geometry in geometries.items()
                if geometry["parentId"] == transform_id
            }

            self.assertEqual(
                set(geometry_ids),
                expected_geometry_ids
            )

            filtered_geometry_ids = {
                geometry["id"]
                for geometry in self.list_geometries(
                    parent_id=transform_id
                )
            }

            self.assertEqual(
                filtered_geometry_ids,
                expected_geometry_ids
            )

            self.assert_material_references(
                transform["materials"],
                materials
            )

        for geometry_id, geometry in geometries.items():
            parent_id = geometry["parentId"]

            self.assertIn(
                parent_id,
                transforms
            )

            self.assertIn(
                geometry_id,
                transforms[parent_id]["geometries"]
            )

            self.assertIn(
                geometry["kind"],
                {"SOLID", "DOMAIN"}
            )

            if geometry["source"] == "editor":
                self.assertIn(
                    "materials",
                    geometry
                )

            self.assert_material_references(
                geometry.get("materials", []),
                materials
            )

        all_geometry_ids = set(geometries)

        solid_ids = {
            item["id"]
            for item in self.list_geometries(kind="SOLID")
        }

        domain_ids = {
            item["id"]
            for item in self.list_geometries(kind="DOMAIN")
        }

        self.assertFalse(
            solid_ids & domain_ids
        )

        self.assertEqual(
            solid_ids | domain_ids,
            all_geometry_ids
        )

        self.assertEqual(
            {
                item["id"]
                for item in self.list_geometries(kind="ALL")
            },
            all_geometry_ids
        )

        return indexes

    def assert_material_references(
        self,
        material_ids,
        material_index
    ):
        self.assertEqual(
            len(material_ids),
            len(set(material_ids))
        )

        for material_id in material_ids:
            self.assertIn(
                material_id,
                material_index
            )

    def test_unique_identifiers(self):
        """Verify unique session identifiers within each entity category."""

        initial = self.snapshot_indexes()

        for index in range(10):
            self.create_transform(
                name=f"Transform {index}",
                x=index * 10
            )

            self.create_geometry(
                name=f"Domain {index}",
                x=index * 10
            )

            self.create_material(
                name=f"Material {index}"
            )

        expected_counts = {
            "transform": 12,
            "geometry": 11,
            "material": 10
        }

        all_identifiers = []
        all_step_ids = []

        for category, expected_count in expected_counts.items():
            with self.subTest(category=category):
                entities = self.list_entities(category)

                self.assertEqual(
                    len(entities),
                    expected_count
                )

                identifiers = [
                    entity["id"]
                    for entity in entities
                ]

                step_ids = [
                    entity["ifcEntityId"]
                    for entity in entities
                ]

                self.assertEqual(
                    len(identifiers),
                    len(set(identifiers))
                )

                self.assertEqual(
                    len(step_ids),
                    len(set(step_ids))
                )

                all_identifiers.extend(identifiers)
                all_step_ids.extend(step_ids)

        self.assertEqual(
            len(all_identifiers),
            len(set(all_identifiers))
        )

        self.assertEqual(
            len(all_step_ids),
            len(set(all_step_ids))
        )

        self.assert_snapshot_preserved(initial)

        self.assert_index_consistency()

    def test_identifier_prefixes(self):
        """Verify transform, geometry and material identifier prefixes."""

        transform = self.create_transform(
            name="Prefix transform"
        )

        domain = self.create_geometry(
            name="Prefix domain"
        )

        material = self.create_material(
            name="Prefix material"
        )

        created = {
            "transform": transform,
            "geometry": domain,
            "material": material
        }

        for category, entity in created.items():
            with self.subTest(category=category):
                self.assert_identifier(
                    entity["id"],
                    category
                )

                self.assertIs(
                    type(entity["ifcEntityId"]),
                    int
                )

                self.assertGreater(
                    entity["ifcEntityId"],
                    0
                )

        for category in ENTITY_COMMANDS:
            for entity in self.list_entities(category):
                self.assert_identifier(
                    entity["id"],
                    category
                )

        duplicated = self.client.request(
            "transformDuplicate",
            {
                "id": self.parent_id,
                "includeChildren": False
            }
        )

        self.assert_identifier(
            duplicated["rootId"],
            "transform"
        )

        for source_id, duplicate_id in duplicated["transforms"].items():
            self.assert_identifier(source_id, "transform")
            self.assert_identifier(duplicate_id, "transform")

        for source_id, duplicate_id in duplicated["geometries"].items():
            self.assert_identifier(source_id, "geometry")
            self.assert_identifier(duplicate_id, "geometry")

        self.save_and_reload()

        self.assert_index_consistency()

    def test_invalid_entity_type(self):
        """Verify rejection of identifiers from incompatible entity categories."""

        domain = self.create_geometry(
            name="Type validation domain"
        )

        material = self.create_material(
            name="Type validation material"
        )

        identifiers = {
            "transform": self.parent_id,
            "geometry": domain["id"],
            "material": material["id"]
        }

        initial = self.snapshot_indexes()

        for expected_category in ENTITY_COMMANDS:
            for actual_category, identifier in identifiers.items():
                if expected_category == actual_category:
                    continue

                with self.subTest(
                    expected=expected_category,
                    actual=actual_category
                ):
                    self.client.expect_error(
                        ENTITY_COMMANDS[expected_category],
                        {"id": identifier},
                        "INVALID_ENTITY_TYPE"
                    )

        cases = [
            (
                "transformUpdate",
                {
                    "id": domain["id"],
                    "name": "Invalid update"
                }
            ),
            (
                "transformDelete",
                {"id": material["id"]}
            ),
            (
                "transformDuplicate",
                {
                    "id": domain["id"],
                    "includeChildren": False
                }
            ),
            (
                "transformTessellate",
                {
                    "id": material["id"],
                    "options": tessellation_options()
                }
            ),
            (
                "geometryGetAll",
                {"transformId": material["id"]}
            ),
            (
                "geometryUpdate",
                {
                    "id": self.parent_id,
                    "depth": 3.0
                }
            ),
            (
                "geometryDelete",
                {"id": self.parent_id}
            ),
            (
                "materialUpdate",
                {
                    "id": domain["id"],
                    "name": "Invalid material"
                }
            ),
            (
                "materialDelete",
                {"id": domain["id"]}
            ),
            (
                "materialAssign",
                {
                    "materialId": self.parent_id,
                    "targetId": domain["id"]
                }
            ),
            (
                "materialAssign",
                {
                    "materialId": material["id"],
                    "targetId": material["id"]
                }
            ),
            (
                "materialUnassign",
                {
                    "materialId": domain["id"],
                    "targetId": self.parent_id
                }
            ),
            (
                "transformCreate",
                {
                    "name": "Invalid parent type",
                    "parents": parents_input(
                        placement=domain["id"]
                    ),
                    "transform": transform_input(),
                    "properties": []
                }
            ),
            (
                "geometryCreate",
                {
                    "parentId": material["id"],
                    "name": "Invalid geometry parent",
                    "kind": "DOMAIN",
                    "transform": transform_input(),
                    "profile": DEFAULT_PROFILE,
                    "depth": 2.0,
                    "operation": "ADD"
                }
            ),
            (
                "transformTessellate",
                {
                    "id": self.parent_id,
                    "options": {
                        **tessellation_options(),
                        "kind": "DOMAIN",
                        "geometryId": material["id"]
                    }
                }
            )
        ]

        for command, params in cases:
            with self.subTest(command=command, params=params):
                self.client.expect_error(
                    command,
                    params,
                    "INVALID_ENTITY_TYPE"
                )

        self.assertEqual(
            self.snapshot_indexes(),
            initial
        )

        self.assert_index_consistency()

    def test_unknown_identifier(self):
        """Verify ENTITY_NOT_FOUND for nonexistent session identifiers."""

        unknown = {
            "transform": "transform:999999999",
            "geometry": "geometry:999999999",
            "material": "material:999999999"
        }

        material = self.create_material(
            name="Existing material"
        )

        initial = self.snapshot_indexes()

        for category, identifier in unknown.items():
            with self.subTest(category=category):
                self.client.expect_error(
                    ENTITY_COMMANDS[category],
                    {"id": identifier},
                    "ENTITY_NOT_FOUND"
                )

                self.client.expect_error(
                    f"{category}Delete",
                    {"id": identifier},
                    "ENTITY_NOT_FOUND"
                )

        cases = [
            (
                "transformUpdate",
                {
                    "id": unknown["transform"],
                    "name": "Missing transform"
                }
            ),
            (
                "transformDuplicate",
                {
                    "id": unknown["transform"],
                    "includeChildren": True
                }
            ),
            (
                "transformTessellate",
                {
                    "id": unknown["transform"],
                    "options": tessellation_options()
                }
            ),
            (
                "geometryGetAll",
                {"transformId": unknown["transform"]}
            ),
            (
                "geometryUpdate",
                {
                    "id": unknown["geometry"],
                    "depth": 4.0
                }
            ),
            (
                "materialUpdate",
                {
                    "id": unknown["material"],
                    "name": "Missing material"
                }
            ),
            (
                "materialAssign",
                {
                    "materialId": unknown["material"],
                    "targetId": self.parent_id
                }
            ),
            (
                "materialAssign",
                {
                    "materialId": material["id"],
                    "targetId": unknown["geometry"]
                }
            ),
            (
                "materialUnassign",
                {
                    "materialId": material["id"],
                    "targetId": unknown["transform"]
                }
            ),
            (
                "geometryCreate",
                {
                    "parentId": unknown["transform"],
                    "name": "Missing parent geometry",
                    "kind": "DOMAIN",
                    "transform": transform_input(),
                    "profile": DEFAULT_PROFILE,
                    "depth": 2.0,
                    "operation": "ADD"
                }
            ),
            (
                "transformTessellate",
                {
                    "id": self.parent_id,
                    "options": {
                        **tessellation_options(),
                        "kind": "DOMAIN",
                        "geometryId": unknown["geometry"]
                    }
                }
            )
        ]

        for command, params in cases:
            with self.subTest(command=command, params=params):
                self.client.expect_error(
                    command,
                    params,
                    "ENTITY_NOT_FOUND"
                )

        self.assertEqual(
            self.snapshot_indexes(),
            initial
        )

        self.assert_index_consistency()

    def test_stable_identifiers(self):
        """Verify identifier stability during modifications and index updates."""

        transform = self.create_transform(
            name="Stable transform"
        )

        domain = self.create_geometry(
            name="Stable domain"
        )

        solid = self.create_geometry(
            name="Stable solid",
            kind="SOLID"
        )

        material = self.create_material(
            name="Stable material"
        )

        initial = self.snapshot_indexes()

        updated_transform = self.client.request(
            "transformUpdate",
            {
                "id": transform["id"],
                "name": "Updated transform",
                "transform": transform_input(
                    x=10,
                    y=20,
                    z=30
                )
            }
        )

        self.assert_identity(
            transform,
            updated_transform
        )

        updated_domain = self.client.request(
            "geometryUpdate",
            {
                "id": domain["id"],
                "name": "Updated domain",
                "depth": 5.0,
                "profile": [
                    [0, 0],
                    [8, 0],
                    [8, 4],
                    [0, 4]
                ],
                "transform": transform_input(
                    x=5,
                    y=2,
                    z=1
                )
            }
        )

        self.assert_identity(
            domain,
            updated_domain
        )

        updated_material = self.client.request(
            "materialUpdate",
            {
                "id": material["id"],
                "name": "Updated material",
                "visual": {
                    "color": [0.1, 0.2, 0.3],
                    "opacity": 0.8,
                    "metallic": 0.5,
                    "roughness": 0.4
                }
            }
        )

        self.assert_identity(
            material,
            updated_material
        )

        self.assign_material(
            material["id"],
            transform["id"]
        )

        self.assign_material(
            material["id"],
            solid["id"]
        )

        self.assert_snapshot_preserved(initial)

        self.unassign_material(
            material["id"],
            transform["id"]
        )

        self.unassign_material(
            material["id"],
            solid["id"]
        )

        temporary_transform = self.create_transform(
            name="Temporary transform"
        )

        temporary_domain = self.create_geometry(
            name="Temporary domain"
        )

        temporary_material = self.create_material(
            name="Temporary material"
        )

        self.delete_entity(
            "transform",
            temporary_transform["id"]
        )

        self.delete_entity(
            "geometry",
            temporary_domain["id"]
        )

        self.delete_entity(
            "material",
            temporary_material["id"]
        )

        self.assertEqual(
            self.snapshot_indexes(),
            initial
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

        restored = self.client.request(
            "geometryUpdate",
            {
                "id": solid["id"],
                "operation": "ADD"
            }
        )

        self.assertEqual(
            restored["id"],
            solid["id"]
        )

        self.assertEqual(
            restored["operation"],
            "ADD"
        )

        self.assertEqual(
            restored["ifcClass"],
            "IfcProjectionElement"
        )

        self.assert_snapshot_preserved(
            initial,
            excluded_ids={solid["id"]}
        )

        self.assert_index_consistency()

    def test_deleted_identifier(self):
        """Verify that deleted identifiers cannot access former entities."""

        created = {
            "transform": self.create_transform(
                name="Deleted transform"
            ),
            "geometry": self.create_geometry(
                name="Deleted domain"
            ),
            "material": self.create_material(
                name="Deleted material"
            )
        }

        for category, entity in created.items():
            with self.subTest(category=category):
                identifier = entity["id"]

                self.delete_entity(
                    category,
                    identifier
                )

                self.client.expect_error(
                    ENTITY_COMMANDS[category],
                    {"id": identifier},
                    "ENTITY_NOT_FOUND"
                )

                self.client.expect_error(
                    f"{category}Delete",
                    {"id": identifier},
                    "ENTITY_NOT_FOUND"
                )

                current_ids = {
                    item["id"]
                    for item in self.list_entities(category)
                }

                self.assertNotIn(
                    identifier,
                    current_ids
                )

        replacements = {
            "transform": self.create_transform(
                name="Replacement transform"
            ),
            "geometry": self.create_geometry(
                name="Replacement domain"
            ),
            "material": self.create_material(
                name="Replacement material"
            )
        }

        for category, deleted in created.items():
            with self.subTest(category=category):
                replacement = replacements[category]

                self.assertNotEqual(
                    replacement["id"],
                    deleted["id"],
                    "Deleted session identifier was reused"
                )

                self.client.expect_error(
                    ENTITY_COMMANDS[category],
                    {"id": deleted["id"]},
                    "ENTITY_NOT_FOUND"
                )

                self.assertEqual(
                    self.get_entity(
                        category,
                        replacement["id"]
                    ),
                    replacement
                )

                if deleted["globalId"] is not None:
                    self.assertNotEqual(
                        replacement["globalId"],
                        deleted["globalId"]
                    )

        self.assert_index_consistency()

    def test_global_id_uniqueness(self):
        """Verify unique and valid GlobalIds for newly created IFC roots."""

        for index in range(10):
            self.create_transform(
                name=f"GlobalId transform {index}"
            )

            self.create_geometry(
                name=f"GlobalId domain {index}",
                x=index * 10
            )

            self.create_geometry(
                name=f"GlobalId solid {index}",
                kind="SOLID",
                x=index * 10
            )

        material = self.create_material(
            name="Material without GlobalId"
        )

        self.assertIsNone(
            material["globalId"]
        )

        native_geometries = [
            geometry
            for geometry in self.list_geometries()
            if geometry["source"] == "ifc"
        ]

        self.assertEqual(
            len(native_geometries),
            1
        )

        self.assertIsNone(
            native_geometries[0]["globalId"]
        )

        self.client.request(
            "transformDuplicate",
            {
                "id": self.parent_id,
                "includeChildren": False
            }
        )

        global_ids = []

        for category in ENTITY_COMMANDS:
            for entity in self.list_entities(category):
                global_id = entity["globalId"]

                if global_id is None:
                    if category == "transform":
                        self.fail(
                            f"Missing transform GlobalId: {entity}"
                        )

                    if (
                        category == "geometry"
                        and entity["source"] == "editor"
                    ):
                        self.fail(
                            f"Missing editor geometry GlobalId: {entity}"
                        )

                    continue

                self.assert_global_id(global_id)

                global_ids.append(global_id)

        self.assertEqual(
            len(global_ids),
            len(set(global_ids)),
            "Duplicate IFC GlobalIds"
        )

        self.assertIn(
            PARENT_GLOBAL_ID,
            global_ids
        )

        self.assertIn(
            PROJECT_GLOBAL_ID,
            global_ids
        )

        self.save_model()

        content = self.output_path.read_text(
            encoding="utf-8"
        )

        root_global_ids = re.findall(
            r"(?im)^\s*#\d+\s*=\s*IFC[A-Z0-9_]+\(\s*'([0-3][0-9A-Za-z_$]{21})'",
            content
        )

        self.assertTrue(
            root_global_ids
        )

        self.assertEqual(
            len(root_global_ids),
            len(set(root_global_ids)),
            "Duplicate root GlobalIds in saved IFC"
        )

        self.assertTrue(
            set(global_ids).issubset(
                set(root_global_ids)
            )
        )

        self.assert_index_consistency()

    def test_global_id_persistence(self):
        """Verify GlobalId preservation after saving and reloading IFC."""

        transform = self.create_transform(
            name="Persistent transform",
            placement_parent=self.parent_id,
            decomposition_parent=self.parent_id,
            x=10
        )

        domain = self.create_geometry(
            name="Persistent domain",
            parent_id=transform["id"]
        )

        solid = self.create_geometry(
            name="Persistent solid",
            parent_id=transform["id"],
            kind="SOLID"
        )

        material = self.create_material(
            name="Persistent material"
        )

        self.assign_material(
            material["id"],
            transform["id"]
        )

        self.assign_material(
            material["id"],
            solid["id"]
        )

        global_ids_before = {
            category: {
                item["globalId"]
                for item in self.list_entities(category)
                if item["globalId"] is not None
            }
            for category in ENTITY_COMMANDS
        }

        self.save_and_reload()

        for category in ENTITY_COMMANDS:
            global_ids_after = {
                item["globalId"]
                for item in self.list_entities(category)
                if item["globalId"] is not None
            }

            self.assertEqual(
                global_ids_after,
                global_ids_before[category]
            )

        reloaded_transform = self.find_by_global_id(
            "transform",
            transform["globalId"]
        )

        reloaded_domain = self.find_by_global_id(
            "geometry",
            domain["globalId"]
        )

        reloaded_solid = self.find_by_global_id(
            "geometry",
            solid["globalId"]
        )

        reloaded_material = self.find_material_by_name(
            material["name"]
        )

        self.assertEqual(
            reloaded_transform["name"],
            transform["name"]
        )

        self.assertEqual(
            reloaded_domain["name"],
            domain["name"]
        )

        self.assertEqual(
            reloaded_solid["name"],
            solid["name"]
        )

        self.assertEqual(
            reloaded_domain["kind"],
            "DOMAIN"
        )

        self.assertEqual(
            reloaded_solid["kind"],
            "SOLID"
        )

        self.assertEqual(
            reloaded_domain["parentId"],
            reloaded_transform["id"]
        )

        self.assertEqual(
            reloaded_solid["parentId"],
            reloaded_transform["id"]
        )

        self.assertEqual(
            reloaded_transform["parents"]["placement"],
            self.parent_id
        )

        self.assertEqual(
            reloaded_transform["parents"]["decomposition"],
            self.parent_id
        )

        self.assertIn(
            reloaded_material["id"],
            reloaded_transform["materials"]
        )

        self.assertIn(
            reloaded_material["id"],
            reloaded_solid["materials"]
        )

        self.assertIsNone(
            reloaded_material["globalId"]
        )

        self.assert_index_consistency()

        self.save_and_reload()

        for category in ENTITY_COMMANDS:
            global_ids = {
                item["globalId"]
                for item in self.list_entities(category)
                if item["globalId"] is not None
            }

            self.assertEqual(
                global_ids,
                global_ids_before[category]
            )

        self.assert_index_consistency()

    def test_session_reset(self):
        """Verify index reconstruction when another IFC session is loaded."""

        first_transform = self.create_transform(
            name="First session transform"
        )

        first_domain = self.create_geometry(
            name="First session domain"
        )

        first_material = self.create_material(
            name="First session material"
        )

        self.assign_material(
            first_material["id"],
            first_transform["id"]
        )

        self.assign_material(
            first_material["id"],
            self.parent_id
        )

        self.assert_index_consistency()

        previous_global_ids = {
            entity["globalId"]
            for category in ENTITY_COMMANDS
            for entity in self.list_entities(category)
            if entity["globalId"] is not None
        }

        result = self.load_model(
            self.second_input_path,
            parent_global_id=SECOND_PARENT_GLOBAL_ID
        )

        self.assertEqual(
            result["transformCount"],
            2
        )

        self.assertEqual(
            result["geometryCount"],
            1
        )

        self.assertEqual(
            result["materialCount"],
            0
        )

        current_global_ids = {
            entity["globalId"]
            for category in ENTITY_COMMANDS
            for entity in self.list_entities(category)
            if entity["globalId"] is not None
        }

        self.assertEqual(
            current_global_ids,
            {
                SECOND_PARENT_GLOBAL_ID,
                SECOND_PROJECT_GLOBAL_ID
            }
        )

        self.assertFalse(
            previous_global_ids & current_global_ids
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assertEqual(
            self.list_materials(),
            []
        )

        stale_identifiers = {
            "transform": first_transform["id"],
            "geometry": first_domain["id"],
            "material": first_material["id"]
        }

        for category, identifier in stale_identifiers.items():
            current_ids = {
                entity["id"]
                for entity in self.list_entities(category)
            }

            self.assertNotIn(
                identifier,
                current_ids
            )

            self.client.expect_error(
                ENTITY_COMMANDS[category],
                {"id": identifier},
                "ENTITY_NOT_FOUND"
            )

        parent = self.get_transform(self.parent_id)

        self.assertEqual(
            parent["globalId"],
            SECOND_PARENT_GLOBAL_ID
        )

        self.assertEqual(
            parent["materials"],
            []
        )

        self.assertEqual(
            len(parent["geometries"]),
            1
        )

        self.assert_index_consistency()

        second_transform = self.create_transform(
            name="Second session transform"
        )

        second_domain = self.create_geometry(
            name="Second session domain"
        )

        second_material = self.create_material(
            name="Second session material"
        )

        self.assertEqual(
            second_transform["id"],
            first_transform["id"]
        )

        self.assertEqual(
            second_domain["id"],
            first_domain["id"]
        )

        self.assertEqual(
            second_material["id"],
            first_material["id"]
        )

        self.assertNotEqual(
            second_transform["globalId"],
            first_transform["globalId"]
        )

        self.assertNotEqual(
            second_domain["globalId"],
            first_domain["globalId"]
        )

        self.assertEqual(
            self.get_transform(second_transform["id"])["name"],
            "Second session transform"
        )

        self.assertEqual(
            self.get_geometry(second_domain["id"])["name"],
            "Second session domain"
        )

        self.assertEqual(
            self.get_material(second_material["id"])["name"],
            "Second session material"
        )

        self.assert_index_consistency()

        self.load_model(self.input_path)

        self.assertEqual(
            len(self.list_transforms()),
            2
        )

        self.assertEqual(
            len(self.list_geometries()),
            1
        )

        self.assertEqual(
            len(self.list_materials()),
            0
        )

        self.assert_index_consistency()

    def test_index_consistency(self):
        """Verify cross-index consistency throughout the entity lifecycle."""

        self.assert_index_consistency()

        parent = self.create_transform(
            name="Index parent",
            placement_parent=self.parent_id,
            decomposition_parent=self.parent_id
        )

        child = self.create_transform(
            name="Index child",
            placement_parent=parent["id"],
            decomposition_parent=parent["id"],
            x=5
        )

        domain = self.create_geometry(
            name="Index domain",
            parent_id=child["id"]
        )

        solid = self.create_geometry(
            name="Index solid",
            parent_id=child["id"],
            kind="SOLID"
        )

        material = self.create_material(
            name="Index material"
        )

        self.assign_material(
            material["id"],
            parent["id"]
        )

        self.assign_material(
            material["id"],
            child["id"]
        )

        self.assign_material(
            material["id"],
            solid["id"]
        )

        indexes = self.assert_index_consistency()

        self.assertIn(
            domain["id"],
            indexes["transform"][child["id"]]["geometries"]
        )

        self.assertIn(
            solid["id"],
            indexes["transform"][child["id"]]["geometries"]
        )

        self.assertIn(
            material["id"],
            indexes["geometry"][solid["id"]]["materials"]
        )

        self.assertEqual(
            indexes["geometry"][domain["id"]]["materials"],
            []
        )

        duplicate = self.client.request(
            "transformDuplicate",
            {
                "id": parent["id"],
                "includeChildren": True
            }
        )

        duplicate_parent_id = duplicate["rootId"]
        duplicate_child_id = duplicate["transforms"][child["id"]]

        duplicate_domain_id = duplicate["geometries"][domain["id"]]
        duplicate_solid_id = duplicate["geometries"][solid["id"]]

        indexes = self.assert_index_consistency()

        self.assertEqual(
            indexes["transform"][duplicate_child_id]["parents"]["placement"],
            duplicate_parent_id
        )

        self.assertEqual(
            indexes["transform"][duplicate_child_id]["parents"]["decomposition"],
            duplicate_parent_id
        )

        self.assertEqual(
            indexes["geometry"][duplicate_domain_id]["parentId"],
            duplicate_child_id
        )

        self.assertEqual(
            indexes["geometry"][duplicate_solid_id]["parentId"],
            duplicate_child_id
        )

        self.assertIn(
            material["id"],
            indexes["transform"][duplicate_child_id]["materials"]
        )

        self.assertIn(
            material["id"],
            indexes["geometry"][duplicate_solid_id]["materials"]
        )

        self.unassign_material(
            material["id"],
            child["id"]
        )

        indexes = self.assert_index_consistency()

        self.assertNotIn(
            material["id"],
            indexes["transform"][child["id"]]["materials"]
        )

        self.assertIn(
            material["id"],
            indexes["transform"][duplicate_child_id]["materials"]
        )

        self.delete_entity(
            "geometry",
            domain["id"]
        )

        indexes = self.assert_index_consistency()

        self.assertNotIn(
            domain["id"],
            indexes["geometry"]
        )

        self.assertNotIn(
            domain["id"],
            indexes["transform"][child["id"]]["geometries"]
        )

        self.assertIn(
            duplicate_domain_id,
            indexes["geometry"]
        )

        self.delete_entity(
            "material",
            material["id"]
        )

        indexes = self.assert_index_consistency()

        self.assertNotIn(
            material["id"],
            indexes["material"]
        )

        for transform in indexes["transform"].values():
            self.assertNotIn(
                material["id"],
                transform["materials"]
            )

        for geometry in indexes["geometry"].values():
            self.assertNotIn(
                material["id"],
                geometry.get("materials", [])
            )

        self.delete_entity(
            "transform",
            duplicate_parent_id
        )

        indexes = self.assert_index_consistency()

        self.assertNotIn(
            duplicate_parent_id,
            indexes["transform"]
        )

        self.assertNotIn(
            duplicate_child_id,
            indexes["transform"]
        )

        self.assertNotIn(
            duplicate_domain_id,
            indexes["geometry"]
        )

        self.assertNotIn(
            duplicate_solid_id,
            indexes["geometry"]
        )

        self.assertIn(
            parent["id"],
            indexes["transform"]
        )

        self.assertIn(
            child["id"],
            indexes["transform"]
        )

        self.assertIn(
            solid["id"],
            indexes["geometry"]
        )

        self.save_and_reload()

        indexes = self.assert_index_consistency()

        self.assertEqual(
            len(indexes["transform"]),
            4
        )

        self.assertEqual(
            len(indexes["geometry"]),
            2
        )

        self.assertEqual(
            len(indexes["material"]),
            0
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        reloaded_parent = self.find_by_global_id(
            "transform",
            parent["globalId"]
        )

        reloaded_child = self.find_by_global_id(
            "transform",
            child["globalId"]
        )

        reloaded_solid = self.find_by_global_id(
            "geometry",
            solid["globalId"]
        )

        self.assertEqual(
            reloaded_child["parents"]["placement"],
            reloaded_parent["id"]
        )

        self.assertEqual(
            reloaded_child["parents"]["decomposition"],
            reloaded_parent["id"]
        )

        self.assertEqual(
            reloaded_solid["parentId"],
            reloaded_child["id"]
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