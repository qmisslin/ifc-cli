import json
import queue
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
MATERIAL_RELATION_GLOBAL_ID = "0000000000000000000003"

SECOND_PARENT_GLOBAL_ID = "0000000000000000000011"
SECOND_PROJECT_GLOBAL_ID = "0000000000000000000012"

IDENTITY_MATRIX = [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1
]

EXPECTED_LOAD_FIELDS = {
    "path",
    "schema",
    "transformCount",
    "geometryCount",
    "materialCount"
}


def make_ifc_fixture(
    parent_global_id=PARENT_GLOBAL_ID,
    project_global_id=PROJECT_GLOBAL_ID,
    parent_name="Physical solid",
    project_name="Session test project",
    include_material=True
):
    material_entities = ""

    if include_material:
        material_entities = f"""
#17=IFCMATERIAL('Concrete',$,'concrete');
#18=IFCRELASSOCIATESMATERIAL('{MATERIAL_RELATION_GLOBAL_ID}',$,$,$,(#15),#17);
"""

    return f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('session-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
{material_entities}
ENDSEC;
END-ISO-10303-21;
"""


def transform_input(space="parent", matrix=None):
    return {
        "space": space,
        "matrix": (
            IDENTITY_MATRIX.copy()
            if matrix is None
            else matrix
        )
    }


def parents_input():
    return {
        "spatial": None,
        "placement": None,
        "decomposition": None
    }


def transform_create_params(name="Test transform"):
    return {
        "name": name,
        "parents": parents_input(),
        "transform": transform_input(),
        "properties": []
    }


def geometry_create_params(parent_id, kind="DOMAIN"):
    return {
        "parentId": parent_id,
        "name": "Test geometry",
        "kind": kind,
        "transform": transform_input(),
        "profile": [
            [0, 0],
            [5, 0],
            [5, 3],
            [0, 3]
        ],
        "depth": 2.0,
        "operation": "ADD"
    }


def material_create_params(name="Test material"):
    return {
        "name": name,
        "category": "concrete",
        "visual": {
            "color": [0.5, 0.5, 0.5],
            "opacity": 1.0,
            "metallic": 0.0,
            "roughness": 0.5
        },
        "properties": []
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
            raise AssertionError(
                f"Invalid response: {response}"
            )

        if response.get("id") != request_id:
            raise AssertionError(
                f"Unexpected response identifier: {response}"
            )

        if type(response.get("ok")) is not bool:
            raise AssertionError(
                f"Invalid response status: {response}"
            )

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
        request_id = self.send(
            command,
            params
        )

        response = self.read(
            request_id
        )

        if not response["ok"]:
            raise RuntimeError(
                f"{command}: {response['error']}"
            )

        return response["result"]

    def expect_error(self, command, params, expected_code):
        request_id = self.send(
            command,
            params
        )

        response = self.read(
            request_id
        )

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


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-session-"
        )

        self.addCleanup(
            self.directory.cleanup
        )

        self.root_path = Path(
            self.directory.name
        )

        self.input_path = (
            self.root_path / "input.ifc"
        )

        self.second_input_path = (
            self.root_path / "second.ifc"
        )

        self.output_path = (
            self.root_path / "output.ifc"
        )

        self.input_path.write_text(
            make_ifc_fixture(),
            encoding="utf-8"
        )

        self.second_input_path.write_text(
            make_ifc_fixture(
                parent_global_id=SECOND_PARENT_GLOBAL_ID,
                project_global_id=SECOND_PROJECT_GLOBAL_ID,
                parent_name="Second physical solid",
                project_name="Second session project",
                include_material=False
            ),
            encoding="utf-8"
        )

        self.client = Client(
            EXECUTABLE
        )

        self.addCleanup(
            self.client.terminate
        )

    def tearDown(self):
        self.client.close()

    def load_model(self, path=None):
        path = (
            self.input_path
            if path is None
            else path
        )

        result = self.client.request(
            "load",
            {
                "path": str(path)
            }
        )

        self.assert_load_result(
            result,
            path
        )

        return result

    def save_model(self, path=None):
        path = (
            self.output_path
            if path is None
            else path
        )

        result = self.client.request(
            "save",
            {
                "path": str(path)
            }
        )

        self.assertEqual(
            result,
            {"path": str(path)}
        )

        self.assertTrue(
            path.is_file()
        )

        self.assertGreater(
            path.stat().st_size,
            0
        )

        return result

    def assert_load_result(self, result, path):
        self.assertIsInstance(
            result,
            dict
        )

        self.assertEqual(
            set(result),
            EXPECTED_LOAD_FIELDS
        )

        self.assertEqual(
            result["path"],
            str(path)
        )

        self.assertEqual(
            result["schema"].upper(),
            "IFC4"
        )

        for field in (
            "transformCount",
            "geometryCount",
            "materialCount"
        ):
            self.assertIs(
                type(result[field]),
                int
            )

            self.assertGreaterEqual(
                result[field],
                0
            )

    def assert_counters(
        self,
        result,
        transform_count,
        geometry_count,
        material_count
    ):
        expected = {
            "transformCount": transform_count,
            "geometryCount": geometry_count,
            "materialCount": material_count
        }

        for field, value in expected.items():
            with self.subTest(counter=field):
                self.assertEqual(
                    result[field],
                    value
                )

        self.assert_index_counts(
            transform_count,
            geometry_count,
            material_count
        )

    def assert_index_counts(
        self,
        transform_count,
        geometry_count,
        material_count
    ):
        self.assertEqual(
            len(self.list_transforms()),
            transform_count
        )

        self.assertEqual(
            len(self.list_geometries()),
            geometry_count
        )

        self.assertEqual(
            len(self.list_materials()),
            material_count
        )

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

        return matches[0]["id"]

    def get_transform(self, transform_id):
        return self.client.request(
            "transformGet",
            {
                "id": transform_id
            }
        )

    def get_geometry(self, geometry_id):
        return self.client.request(
            "geometryGet",
            {
                "id": geometry_id
            }
        )

    def get_material(self, material_id):
        return self.client.request(
            "materialGet",
            {
                "id": material_id
            }
        )

    def create_transform(self, name="Created transform"):
        return self.client.request(
            "transformCreate",
            transform_create_params(name)
        )

    def create_domain(self, parent_id):
        return self.client.request(
            "geometryCreate",
            geometry_create_params(
                parent_id,
                kind="DOMAIN"
            )
        )

    def create_material(self, name="Created material"):
        return self.client.request(
            "materialCreate",
            material_create_params(name)
        )

    def rename_transform(self, transform_id, name):
        return self.client.request(
            "transformUpdate",
            {
                "id": transform_id,
                "name": name
            }
        )

    def assert_no_model(self):
        commands = [
            (
                "transformGetAll",
                {}
            ),
            (
                "geometryGetAll",
                {"transformId": None}
            ),
            (
                "materialGetAll",
                {}
            )
        ]

        for command, params in commands:
            with self.subTest(command=command):
                self.client.expect_error(
                    command,
                    params,
                    "NO_MODEL_LOADED"
                )

    def assert_ifc_file(self, path):
        content = path.read_text(
            encoding="utf-8"
        )

        normalized = content.strip()

        self.assertTrue(
            normalized.startswith("ISO-10303-21;")
        )

        self.assertTrue(
            normalized.endswith("END-ISO-10303-21;")
        )

        self.assertIn(
            "HEADER;",
            content
        )

        self.assertIn(
            "DATA;",
            content
        )

        self.assertIn(
            "FILE_SCHEMA",
            content
        )

        return content

    def test_load_valid_file(self):
        """Verify loading a valid IFC file."""

        result = self.load_model()

        self.assert_counters(
            result,
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        project_id = self.find_transform(
            PROJECT_GLOBAL_ID
        )

        parent = self.get_transform(
            parent_id
        )

        project = self.get_transform(
            project_id
        )

        self.assertEqual(
            parent["name"],
            "Physical solid"
        )

        self.assertEqual(
            project["name"],
            "Session test project"
        )

        self.assertEqual(
            parent["definition"]["ifcClass"],
            "IfcBuildingElementProxy"
        )

        self.assertEqual(
            project["definition"]["ifcClass"],
            "IfcProject"
        )

        geometries = self.list_geometries(
            parent_id=parent_id
        )

        self.assertEqual(
            len(geometries),
            1
        )

        geometry = self.get_geometry(
            geometries[0]["id"]
        )

        self.assertEqual(
            geometry["kind"],
            "SOLID"
        )

        self.assertEqual(
            geometry["source"],
            "ifc"
        )

        self.assertIs(
            geometry["editable"],
            False
        )

        materials = self.list_materials()

        self.assertEqual(
            len(materials),
            1
        )

        material = self.get_material(
            materials[0]["id"]
        )

        self.assertEqual(
            material["name"],
            "Concrete"
        )

        self.assertIn(
            material["id"],
            parent["materials"]
        )

    def test_load_missing_file(self):
        """Verify FILE_NOT_FOUND for a nonexistent IFC file."""

        missing_paths = [
            self.root_path / "missing.ifc",
            self.root_path / "missing-directory" / "model.ifc"
        ]

        for path in missing_paths:
            with self.subTest(path=str(path)):
                self.assertFalse(
                    path.exists()
                )

                error = self.client.expect_error(
                    "load",
                    {
                        "path": str(path)
                    },
                    "FILE_NOT_FOUND"
                )

                self.assertEqual(
                    error["details"].get("path"),
                    str(path)
                )

                self.assert_no_model()

        result = self.load_model()

        self.assert_counters(
            result,
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

    def test_load_directory(self):
        """Verify rejection of a directory passed as an IFC file."""

        directory_path = (
            self.root_path / "directory.ifc"
        )

        directory_path.mkdir()

        self.assertTrue(
            directory_path.is_dir()
        )

        error = self.client.expect_error(
            "load",
            {
                "path": str(directory_path)
            },
            "FILE_NOT_FOUND"
        )

        self.assertEqual(
            error["details"].get("path"),
            str(directory_path)
        )

        self.assert_no_model()

        self.load_model()

    def test_load_empty_file(self):
        """Verify rejection of an empty IFC file."""

        empty_path = (
            self.root_path / "empty.ifc"
        )

        empty_path.touch()

        self.assertEqual(
            empty_path.stat().st_size,
            0
        )

        self.client.expect_error(
            "load",
            {
                "path": str(empty_path)
            },
            "IFC_PARSE_ERROR"
        )

        self.assert_no_model()

        self.load_model()

    def test_load_corrupted_file(self):
        """Verify rejection of corrupted IFC files."""

        corrupted_fixtures = {
            "invalid-content.ifc": (
                "This is not an IFC file."
            ),
            "invalid-header.ifc": (
                "ISO-10303-21;\n"
                "HEADER;\n"
                "THIS IS AN INVALID IFC HEADER\n"
                "ENDSEC;\n"
                "DATA;\n"
                "ENDSEC;\n"
                "END-ISO-10303-21;\n"
            ),
            "truncated-header.ifc": (
                "ISO-10303-21;\n"
                "HEADER;\n"
                "FILE_DESCRIPTION("
            )
        }

        for filename, content in corrupted_fixtures.items():
            with self.subTest(filename=filename):
                path = self.root_path / filename

                path.write_text(
                    content,
                    encoding="utf-8"
                )

                self.client.expect_error(
                    "load",
                    {
                        "path": str(path)
                    },
                    "IFC_PARSE_ERROR"
                )

                self.assert_no_model()

        binary_path = (
            self.root_path / "binary.ifc"
        )

        binary_path.write_bytes(
            b"\x00\x01\x02\xff\xfe\xfd\x00\x00"
        )

        self.client.expect_error(
            "load",
            {
                "path": str(binary_path)
            },
            "IFC_PARSE_ERROR"
        )

        self.assert_no_model()

        self.load_model()

    def test_load_counters(self):
        """Verify load counters against all indexed entity categories."""

        initial = self.load_model()

        self.assert_counters(
            initial,
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        created_transform = self.create_transform(
            name="Additional transform"
        )

        domain = self.create_domain(
            parent_id
        )

        material = self.create_material(
            name="Additional material"
        )

        self.assertTrue(
            created_transform["id"].startswith("transform:")
        )

        self.assertEqual(
            domain["kind"],
            "DOMAIN"
        )

        self.assertTrue(
            material["id"].startswith("material:")
        )

        self.assert_index_counts(
            transform_count=3,
            geometry_count=2,
            material_count=2
        )

        self.assertEqual(
            len(self.list_geometries(kind="SOLID")),
            1
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            1
        )

        self.save_model()

        reloaded = self.load_model(
            self.output_path
        )

        self.assert_counters(
            reloaded,
            transform_count=3,
            geometry_count=2,
            material_count=2
        )

        domain_ids = {
            geometry["globalId"]
            for geometry in self.list_geometries(
                kind="DOMAIN"
            )
        }

        self.assertIn(
            domain["globalId"],
            domain_ids
        )

        transform_global_ids = {
            transform["globalId"]
            for transform in self.list_transforms()
        }

        self.assertIn(
            created_transform["globalId"],
            transform_global_ids
        )

        material_names = {
            self.get_material(item["id"])["name"]
            for item in self.list_materials()
        }

        self.assertEqual(
            material_names,
            {
                "Concrete",
                "Additional material"
            }
        )

    def test_no_model_loaded(self):
        """Verify that model-dependent commands fail without a loaded model."""

        commands = {
            "save": {
                "path": str(self.output_path)
            },
            "transformGetAll": {},
            "transformGet": {
                "id": "transform:1"
            },
            "transformCreate": transform_create_params(),
            "transformDuplicate": {
                "id": "transform:1",
                "includeChildren": False
            },
            "transformUpdate": {
                "id": "transform:1",
                "name": "Updated transform"
            },
            "transformDelete": {
                "id": "transform:1"
            },
            "transformTessellate": {
                "id": "transform:1",
                "options": tessellation_options()
            },
            "geometryGetAll": {
                "transformId": None
            },
            "geometryGet": {
                "id": "geometry:1"
            },
            "geometryCreate": geometry_create_params(
                "transform:1"
            ),
            "geometryUpdate": {
                "id": "geometry:1",
                "depth": 2.0
            },
            "geometryDelete": {
                "id": "geometry:1"
            },
            "materialGetAll": {},
            "materialGet": {
                "id": "material:1"
            },
            "materialCreate": material_create_params(),
            "materialUpdate": {
                "id": "material:1",
                "name": "Updated material"
            },
            "materialDelete": {
                "id": "material:1"
            },
            "materialAssign": {
                "materialId": "material:1",
                "targetId": "transform:1"
            },
            "materialUnassign": {
                "materialId": "material:1",
                "targetId": "transform:1"
            }
        }

        self.assertEqual(
            len(commands),
            20
        )

        for command, params in commands.items():
            with self.subTest(command=command):
                self.client.expect_error(
                    command,
                    params,
                    "NO_MODEL_LOADED"
                )

        self.assertFalse(
            self.output_path.exists()
        )

        help_result = self.client.request(
            "help"
        )

        self.assertIn(
            "documentation",
            help_result
        )

        capabilities = self.client.request(
            "getCapabilities"
        )

        self.assertIn(
            "supportedSchemas",
            capabilities
        )

        self.load_model()

        self.assert_index_counts(
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

    def test_save_valid_file(self):
        """Verify saving a loaded IFC model to a new file."""

        self.load_model()

        self.assertFalse(
            self.output_path.exists()
        )

        original_content = (
            self.input_path.read_bytes()
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        self.rename_transform(
            parent_id,
            "Saved physical solid"
        )

        domain = self.create_domain(
            parent_id
        )

        self.save_model()

        saved_content = self.assert_ifc_file(
            self.output_path
        )

        self.assertIn(
            PARENT_GLOBAL_ID,
            saved_content
        )

        self.assertIn(
            domain["globalId"],
            saved_content
        )

        self.assertIn(
            "IFCSPATIALZONE(",
            saved_content.upper()
        )

        self.assertEqual(
            self.input_path.read_bytes(),
            original_content,
            "Saving to another file must not modify the source"
        )

        result = self.load_model(
            self.output_path
        )

        self.assert_counters(
            result,
            transform_count=2,
            geometry_count=2,
            material_count=1
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        parent = self.get_transform(
            parent_id
        )

        self.assertEqual(
            parent["name"],
            "Saved physical solid"
        )

        domains = self.list_geometries(
            kind="DOMAIN",
            parent_id=parent_id
        )

        self.assertEqual(
            len(domains),
            1
        )

        self.assertEqual(
            domains[0]["globalId"],
            domain["globalId"]
        )

    def test_save_overwrite(self):
        """Verify overwriting an existing writable IFC file."""

        self.load_model()

        sentinel = "PREVIOUS_FILE_CONTENT"

        self.output_path.write_text(
            sentinel * 10000,
            encoding="utf-8"
        )

        old_size = (
            self.output_path.stat().st_size
        )

        self.save_model()

        new_size = (
            self.output_path.stat().st_size
        )

        self.assertLess(
            new_size,
            old_size,
            "The previous file content must be truncated"
        )

        content = self.assert_ifc_file(
            self.output_path
        )

        self.assertNotIn(
            sentinel,
            content
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        self.rename_transform(
            parent_id,
            "First saved name"
        )

        self.save_model()

        self.rename_transform(
            parent_id,
            "Second saved name"
        )

        self.save_model()

        result = self.load_model(
            self.output_path
        )

        self.assert_counters(
            result,
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        parent = self.get_transform(
            parent_id
        )

        self.assertEqual(
            parent["name"],
            "Second saved name"
        )

        content = self.assert_ifc_file(
            self.output_path
        )

        self.assertNotIn(
            "First saved name",
            content
        )

    def test_save_invalid_path(self):
        """Verify IFC_WRITE_ERROR for inaccessible output destinations."""

        self.load_model()

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        self.rename_transform(
            parent_id,
            "Unsaved model state"
        )

        directory_path = (
            self.root_path / "output-directory.ifc"
        )

        directory_path.mkdir()

        regular_file = (
            self.root_path / "regular-file"
        )

        regular_file.write_text(
            "Not a directory",
            encoding="utf-8"
        )

        invalid_paths = [
            directory_path,
            self.root_path / "missing-directory" / "output.ifc",
            regular_file / "output.ifc"
        ]

        for path in invalid_paths:
            with self.subTest(path=str(path)):
                error = self.client.expect_error(
                    "save",
                    {
                        "path": str(path)
                    },
                    "IFC_WRITE_ERROR"
                )

                self.assertEqual(
                    error["details"].get("path"),
                    str(path)
                )

                self.assert_index_counts(
                    transform_count=2,
                    geometry_count=1,
                    material_count=1
                )

                parent = self.get_transform(
                    parent_id
                )

                self.assertEqual(
                    parent["name"],
                    "Unsaved model state"
                )

        self.assertFalse(
            (
                self.root_path
                / "missing-directory"
            ).exists()
        )

        self.assertEqual(
            regular_file.read_text(encoding="utf-8"),
            "Not a directory"
        )

        self.save_model()

        self.load_model(
            self.output_path
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        self.assertEqual(
            self.get_transform(parent_id)["name"],
            "Unsaved model state"
        )

    def test_reload_model(self):
        """Verify complete session replacement when loading another IFC file."""

        first_result = self.load_model()

        self.assert_counters(
            first_result,
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        extra_transform = self.create_transform(
            name="Temporary transform"
        )

        domain = self.create_domain(
            parent_id
        )

        extra_material = self.create_material(
            name="Temporary material"
        )

        self.rename_transform(
            parent_id,
            "Modified first model"
        )

        self.assert_index_counts(
            transform_count=3,
            geometry_count=2,
            material_count=2
        )

        second_result = self.load_model(
            self.second_input_path
        )

        self.assert_counters(
            second_result,
            transform_count=2,
            geometry_count=1,
            material_count=0
        )

        transforms = self.list_transforms()

        global_ids = {
            transform["globalId"]
            for transform in transforms
        }

        self.assertEqual(
            global_ids,
            {
                SECOND_PARENT_GLOBAL_ID,
                SECOND_PROJECT_GLOBAL_ID
            }
        )

        self.assertNotIn(
            PARENT_GLOBAL_ID,
            global_ids
        )

        self.assertNotIn(
            PROJECT_GLOBAL_ID,
            global_ids
        )

        self.assertNotIn(
            extra_transform["globalId"],
            global_ids
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assertEqual(
            self.list_materials(),
            []
        )

        stale_identifiers = [
            (
                "transformGet",
                extra_transform["id"]
            ),
            (
                "geometryGet",
                domain["id"]
            ),
            (
                "materialGet",
                extra_material["id"]
            )
        ]

        for command, identifier in stale_identifiers:
            with self.subTest(
                command=command,
                identifier=identifier
            ):
                self.client.expect_error(
                    command,
                    {
                        "id": identifier
                    },
                    "ENTITY_NOT_FOUND"
                )

        second_parent_id = self.find_transform(
            SECOND_PARENT_GLOBAL_ID
        )

        self.assertEqual(
            self.get_transform(second_parent_id)["name"],
            "Second physical solid"
        )

        geometries = self.list_geometries(
            parent_id=second_parent_id
        )

        self.assertEqual(
            len(geometries),
            1
        )

        self.assertEqual(
            geometries[0]["kind"],
            "SOLID"
        )

        self.assertEqual(
            geometries[0]["parentId"],
            second_parent_id
        )

        restored = self.load_model(
            self.input_path
        )

        self.assert_counters(
            restored,
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

        restored_parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        restored_parent = self.get_transform(
            restored_parent_id
        )

        self.assertEqual(
            restored_parent["name"],
            "Physical solid"
        )

    def test_failed_reload(self):
        """Verify that a failed reload clears the previous IFC session."""

        corrupted_path = (
            self.root_path / "corrupted-reload.ifc"
        )

        corrupted_path.write_text(
            "Invalid IFC content",
            encoding="utf-8"
        )

        directory_path = (
            self.root_path / "reload-directory"
        )

        directory_path.mkdir()

        empty_path = (
            self.root_path / "empty-reload.ifc"
        )

        empty_path.touch()

        cases = [
            (
                self.root_path / "missing-reload.ifc",
                "FILE_NOT_FOUND"
            ),
            (
                directory_path,
                "FILE_NOT_FOUND"
            ),
            (
                corrupted_path,
                "IFC_PARSE_ERROR"
            ),
            (
                empty_path,
                "IFC_PARSE_ERROR"
            )
        ]

        for path, expected_code in cases:
            with self.subTest(
                path=str(path),
                code=expected_code
            ):
                self.load_model()

                parent_id = self.find_transform(
                    PARENT_GLOBAL_ID
                )

                self.rename_transform(
                    parent_id,
                    "Modified before failed reload"
                )

                domain = self.create_domain(
                    parent_id
                )

                self.assert_index_counts(
                    transform_count=2,
                    geometry_count=2,
                    material_count=1
                )

                self.client.expect_error(
                    "load",
                    {
                        "path": str(path)
                    },
                    expected_code
                )

                self.assert_no_model()

                self.client.expect_error(
                    "transformGet",
                    {
                        "id": parent_id
                    },
                    "NO_MODEL_LOADED"
                )

                self.client.expect_error(
                    "geometryGet",
                    {
                        "id": domain["id"]
                    },
                    "NO_MODEL_LOADED"
                )

                self.client.expect_error(
                    "save",
                    {
                        "path": str(self.output_path)
                    },
                    "NO_MODEL_LOADED"
                )

                self.assertFalse(
                    self.output_path.exists()
                )

                capabilities = self.client.request(
                    "getCapabilities"
                )

                self.assertIn(
                    "supportedSchemas",
                    capabilities
                )

        recovered = self.load_model()

        self.assert_counters(
            recovered,
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        self.assertEqual(
            self.get_transform(parent_id)["name"],
            "Physical solid"
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

    def test_unsaved_changes(self):
        """Verify that unsaved changes never modify the source IFC file."""

        original_content = (
            self.input_path.read_bytes()
        )

        original_stat = (
            self.input_path.stat()
        )

        self.load_model()

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        updated = self.rename_transform(
            parent_id,
            "Unsaved transform name"
        )

        self.assertEqual(
            updated["name"],
            "Unsaved transform name"
        )

        domain = self.create_domain(
            parent_id
        )

        created_transform = self.create_transform(
            name="Unsaved additional transform"
        )

        created_material = self.create_material(
            name="Unsaved additional material"
        )

        self.assert_index_counts(
            transform_count=3,
            geometry_count=2,
            material_count=2
        )

        self.assertEqual(
            self.input_path.read_bytes(),
            original_content
        )

        self.assertEqual(
            self.input_path.stat().st_mtime_ns,
            original_stat.st_mtime_ns
        )

        self.assertFalse(
            self.output_path.exists()
        )

        result = self.load_model(
            self.input_path
        )

        self.assert_counters(
            result,
            transform_count=2,
            geometry_count=1,
            material_count=1
        )

        parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        self.assertEqual(
            self.get_transform(parent_id)["name"],
            "Physical solid"
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assertNotIn(
            created_transform["globalId"],
            {
                transform["globalId"]
                for transform in self.list_transforms()
            }
        )

        self.assertNotIn(
            domain["globalId"],
            {
                geometry["globalId"]
                for geometry in self.list_geometries()
            }
        )

        self.assertNotIn(
            created_material["name"],
            {
                self.get_material(material["id"])["name"]
                for material in self.list_materials()
            }
        )

        self.rename_transform(
            parent_id,
            "Unsaved change before exit"
        )

        self.create_domain(
            parent_id
        )

        self.client.close()

        self.assertEqual(
            self.input_path.read_bytes(),
            original_content
        )

        self.assertEqual(
            self.input_path.stat().st_mtime_ns,
            original_stat.st_mtime_ns
        )

        self.assertFalse(
            self.output_path.exists()
        )

    def test_path_encoding(self):
        """Verify IFC loading and saving with spaces and Unicode paths."""

        directories = [
            "directory with spaces",
            "modèles IFC",
            "Gebäude",
            "Δοκιμή",
            "建筑模型",
            "日本語"
        ]

        filenames = [
            "input model.ifc",
            "modèle bâtiment.ifc",
            "résultat simulation.ifc",
            "Gebäude Modell.ifc",
            "δοκιμή.ifc",
            "建筑模型.ifc"
        ]

        for directory_name, filename in zip(
            directories,
            filenames
        ):
            with self.subTest(
                directory=directory_name,
                filename=filename
            ):
                directory = (
                    self.root_path / directory_name
                )

                directory.mkdir()

                input_path = (
                    directory / filename
                )

                output_path = (
                    directory / f"saved {filename}"
                )

                input_path.write_text(
                    make_ifc_fixture(),
                    encoding="utf-8"
                )

                original_content = (
                    input_path.read_bytes()
                )

                result = self.load_model(
                    input_path
                )

                self.assert_counters(
                    result,
                    transform_count=2,
                    geometry_count=1,
                    material_count=1
                )

                parent_id = self.find_transform(
                    PARENT_GLOBAL_ID
                )

                updated_name = "Saved path test"

                self.rename_transform(
                    parent_id,
                    updated_name
                )

                domain = self.create_domain(
                    parent_id
                )

                self.save_model(
                    output_path
                )

                self.assert_ifc_file(
                    output_path
                )

                self.assertEqual(
                    input_path.read_bytes(),
                    original_content
                )

                reloaded = self.load_model(
                    output_path
                )

                self.assert_counters(
                    reloaded,
                    transform_count=2,
                    geometry_count=2,
                    material_count=1
                )

                parent_id = self.find_transform(
                    PARENT_GLOBAL_ID
                )

                self.assertEqual(
                    self.get_transform(parent_id)["name"],
                    updated_name
                )

                domains = self.list_geometries(
                    kind="DOMAIN",
                    parent_id=parent_id
                )

                self.assertEqual(
                    len(domains),
                    1
                )

                self.assertEqual(
                    domains[0]["globalId"],
                    domain["globalId"]
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