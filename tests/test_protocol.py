import copy
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

EXPECTED_COMMANDS = {
    "help",
    "getCapabilities",
    "load",
    "save",
    "exit",
    "transformGetAll",
    "transformGet",
    "transformCreate",
    "transformDuplicate",
    "transformUpdate",
    "transformDelete",
    "transformTessellate",
    "geometryGetAll",
    "geometryGet",
    "geometryCreate",
    "geometryUpdate",
    "geometryDelete",
    "materialGetAll",
    "materialGet",
    "materialCreate",
    "materialUpdate",
    "materialDelete",
    "materialAssign",
    "materialUnassign"
}

EXPECTED_CAPABILITY_FIELDS = {
    "protocolVersion",
    "ifcOpenShellVersion",
    "supportedSchemas",
    "supportedProfiles",
    "supportedGeometryOperations",
    "supportedGeometryKinds"
}

IDENTITY_MATRIX = [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1
]

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('protocol-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#16=IFCPROJECT('0000000000000000000002',$,'Protocol test project',$,$,$,$,(#6),#8);
ENDSEC;
END-ISO-10303-21;
"""


def encode_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":")
    )


def make_request(command, params=None, request_id="request-1"):
    return {
        "id": request_id,
        "command": command,
        "params": {} if params is None else params
    }


def reject_nonfinite(value):
    raise ValueError(
        f"Non-finite JSON constant: {value}"
    )


def reject_duplicate_keys(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"Duplicate JSON field: {key}"
            )

        result[key] = value

    return result


def decode_json(line):
    return json.loads(
        line,
        parse_constant=reject_nonfinite,
        object_pairs_hook=reject_duplicate_keys
    )


def transform_input(space="parent"):
    return {
        "space": space,
        "matrix": IDENTITY_MATRIX.copy()
    }


def parents_input():
    return {
        "spatial": None,
        "placement": None,
        "decomposition": None
    }


def tessellation_options():
    return {
        "space": "world",
        "includeNormals": True,
        "includeMaterials": False,
        "includeChildren": False
    }


def visual_input():
    return {
        "color": [0.5, 0.5, 0.5],
        "opacity": 1.0,
        "metallic": 0.0,
        "roughness": 0.5
    }


def property_input(source="occurrence"):
    return {
        "propertySet": "Pset_Test",
        "name": "TestProperty",
        "valueType": "IfcLabel",
        "value": "Test value",
        "unit": None,
        "source": source
    }


def transform_create_params():
    return {
        "name": "Test transform",
        "parents": parents_input(),
        "transform": transform_input(),
        "properties": []
    }


def geometry_create_params():
    return {
        "parentId": "transform:1",
        "name": "Test geometry",
        "transform": transform_input(),
        "profile": [
            [0, 0],
            [2, 0],
            [2, 1],
            [0, 1]
        ],
        "depth": 1.0,
        "operation": "ADD"
    }


def material_create_params():
    return {
        "name": "Test material",
        "category": "Test category",
        "visual": visual_input(),
        "properties": []
    }


def command_parameters():
    return {
        "help": {},
        "getCapabilities": {},
        "load": {
            "path": "model.ifc"
        },
        "save": {
            "path": "output.ifc"
        },
        "exit": {},
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
        "geometryCreate": geometry_create_params(),
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


def replace_nested_value(document, path, value):
    result = copy.deepcopy(document)
    target = result

    for key in path[:-1]:
        target = target[key]

    target[path[-1]] = value

    return result


class Client:
    def __init__(self, executable, timeout=REQUEST_TIMEOUT):
        self.timeout = timeout
        self.counter = 0
        self.responses = queue.Queue()
        self.stdout_closed = False

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

    def write_raw(self, payload):
        if self.process.poll() is not None:
            raise RuntimeError(
                f"CLI is not running: {self.process.returncode}"
            )

        if self.process.stdin.closed:
            raise RuntimeError(
                "CLI stdin is already closed"
            )

        self.process.stdin.write(payload)
        self.process.stdin.flush()

    def send_raw(self, line):
        self.write_raw(
            line if line.endswith("\n") else line + "\n"
        )

    def send_document(self, document):
        self.send_raw(
            encode_json(document)
        )

    def send(self, command, params=None, request_id=None):
        if request_id is None:
            self.counter += 1
            request_id = f"request-{self.counter}"

        self.send_document(
            make_request(
                command,
                params,
                request_id
            )
        )

        return request_id

    def read_line(self):
        if self.stdout_closed:
            raise EOFError(
                "CLI stdout is closed"
            )

        try:
            item = self.responses.get(
                timeout=self.timeout
            )
        except queue.Empty as error:
            raise TimeoutError(
                "CLI response timeout"
            ) from error

        if item is None:
            self.stdout_closed = True

            raise EOFError(
                "CLI stdout is closed"
            )

        if isinstance(item, Exception):
            raise RuntimeError(
                "Could not read CLI stdout"
            ) from item

        return item

    def read(self):
        line = self.read_line()

        try:
            response = decode_json(line)
        except (json.JSONDecodeError, ValueError) as error:
            raise AssertionError(
                f"Invalid JSONL response: {line!r}"
            ) from error

        if not isinstance(response, dict):
            raise AssertionError(
                f"Response must be an object: {response}"
            )

        return response

    def request(self, command, params=None, request_id=None):
        expected_id = self.send(
            command,
            params,
            request_id
        )

        response = self.read()

        if response.get("id") != expected_id:
            raise AssertionError(
                f"Unexpected response identifier: {response}"
            )

        return response

    def close_input(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()

    def wait(self):
        return self.process.wait(
            timeout=self.timeout
        )

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


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-protocol-"
        )

        self.addCleanup(
            self.directory.cleanup
        )

        self.input_path = (
            Path(self.directory.name) / "input.ifc"
        )

        self.output_path = (
            Path(self.directory.name) / "output.ifc"
        )

        self.input_path.write_text(
            IFC_FIXTURE,
            encoding="utf-8"
        )

        self.client = Client(
            EXECUTABLE
        )

        self.addCleanup(
            self.client.terminate
        )

    def tearDown(self):
        if (
            self.client.process.poll() is None
            and not self.client.process.stdin.closed
        ):
            self.client.send(
                "exit",
                request_id="cleanup-exit"
            )

            response = self.client.read()

            self.assert_success(
                response,
                "cleanup-exit"
            )

            self.assertEqual(
                self.client.wait(),
                0
            )

    def assert_response(self, response, expected_id):
        self.assertIsInstance(
            response,
            dict
        )

        self.assertIn(
            "id",
            response
        )

        self.assertEqual(
            response["id"],
            expected_id
        )

        if response["id"] is not None:
            self.assertIsInstance(
                response["id"],
                str
            )

            self.assertTrue(
                response["id"]
            )

        self.assertIn(
            "ok",
            response
        )

        self.assertIs(
            type(response["ok"]),
            bool
        )

        expected_fields = (
            {"id", "ok", "result"}
            if response["ok"]
            else {"id", "ok", "error"}
        )

        self.assertEqual(
            set(response),
            expected_fields
        )

        if not response["ok"]:
            error = response["error"]

            self.assertIsInstance(
                error,
                dict
            )

            self.assertEqual(
                set(error),
                {"code", "message", "details"}
            )

            self.assertIsInstance(
                error["code"],
                str
            )

            self.assertTrue(
                error["code"]
            )

            self.assertIsInstance(
                error["message"],
                str
            )

            self.assertTrue(
                error["message"]
            )

            self.assertIsInstance(
                error["details"],
                dict
            )

    def assert_success(self, response, expected_id):
        self.assert_response(
            response,
            expected_id
        )

        self.assertIs(
            response["ok"],
            True,
            response
        )

        return response["result"]

    def assert_error(self, response, expected_id, expected_code):
        self.assert_response(
            response,
            expected_id
        )

        self.assertIs(
            response["ok"],
            False,
            response
        )

        self.assertEqual(
            response["error"]["code"],
            expected_code,
            response
        )

        return response["error"]

    def request_success(self, command, params=None):
        request_id = self.client.send(
            command,
            params
        )

        response = self.client.read()

        return self.assert_success(
            response,
            request_id
        )

    def request_error(
        self,
        command,
        params,
        expected_code="INVALID_PARAMETERS"
    ):
        request_id = self.client.send(
            command,
            params
        )

        response = self.client.read()

        return self.assert_error(
            response,
            request_id,
            expected_code
        )

    def raw_error(
        self,
        payload,
        expected_id=None,
        expected_code="INVALID_JSON"
    ):
        self.client.send_raw(
            payload
        )

        response = self.client.read()

        return self.assert_error(
            response,
            expected_id,
            expected_code
        )

    def load_model(self):
        return self.request_success(
            "load",
            {
                "path": str(self.input_path)
            }
        )

    def find_parent(self):
        transforms = self.request_success(
            "transformGetAll"
        )

        matches = [
            transform
            for transform in transforms
            if transform["globalId"] == PARENT_GLOBAL_ID
        ]

        self.assertEqual(
            len(matches),
            1
        )

        return matches[0]["id"]

    def test_help(self):
        """Verify that help documents all 24 supported commands."""

        result = self.request_success(
            "help"
        )

        self.assertEqual(
            set(result),
            {"documentation"}
        )

        documentation = result["documentation"]

        self.assertIsInstance(
            documentation,
            str
        )

        self.assertTrue(
            documentation
        )

        match = re.search(
            r"^Commands:\s*([^\n]+)",
            documentation,
            flags=re.MULTILINE
        )

        self.assertIsNotNone(
            match,
            "Missing command list in help documentation"
        )

        commands = [
            command.strip().rstrip(".")
            for command in match.group(1).split(",")
        ]

        self.assertEqual(
            len(commands),
            24
        )

        self.assertEqual(
            len(commands),
            len(set(commands)),
            "Duplicate commands in help documentation"
        )

        self.assertEqual(
            set(commands),
            EXPECTED_COMMANDS
        )

        self.assertIn(
            "SOLID",
            documentation
        )

        self.assertIn(
            "DOMAIN",
            documentation
        )

        self.assertIn(
            "geometryId",
            documentation
        )

        self.assertIn(
            "JSONL",
            documentation
        )

    def test_capabilities(self):
        """Verify the getCapabilities response structure and values."""

        result = self.request_success(
            "getCapabilities"
        )

        self.assertIsInstance(
            result,
            dict
        )

        self.assertEqual(
            set(result),
            EXPECTED_CAPABILITY_FIELDS
        )

        self.assertEqual(
            result["protocolVersion"],
            "1.0"
        )

        self.assertIsInstance(
            result["ifcOpenShellVersion"],
            str
        )

        self.assertTrue(
            result["ifcOpenShellVersion"]
        )

        schemas = result["supportedSchemas"]

        self.assertIsInstance(
            schemas,
            list
        )

        self.assertTrue(
            schemas
        )

        for schema in schemas:
            self.assertIsInstance(
                schema,
                str
            )

            self.assertTrue(
                schema
            )

        self.assertEqual(
            len(schemas),
            len(set(schemas))
        )

        self.assertEqual(
            schemas,
            sorted(schemas)
        )

        expected_values = {
            "supportedProfiles": {"polygon"},
            "supportedGeometryOperations": {
                "ADD",
                "SUBTRACT"
            },
            "supportedGeometryKinds": {
                "SOLID",
                "DOMAIN"
            }
        }

        for field, expected in expected_values.items():
            with self.subTest(field=field):
                values = result[field]

                self.assertIsInstance(
                    values,
                    list
                )

                self.assertEqual(
                    len(values),
                    len(set(values))
                )

                self.assertEqual(
                    set(values),
                    expected
                )

        repeated = self.request_success(
            "getCapabilities"
        )

        self.assertEqual(
            repeated,
            result
        )

    def test_request_id(self):
        """Verify that responses preserve request identifiers."""

        identifiers = [
            "1",
            "request-42",
            "000001",
            "request:with:colons",
            "request with spaces",
            "a" * 1024,
            "requête-élément-测试"
        ]

        for request_id in identifiers:
            with self.subTest(request_id=request_id):
                response = self.client.request(
                    "help",
                    request_id=request_id
                )

                self.assert_success(
                    response,
                    request_id
                )

                response = self.client.request(
                    "unknownCommand",
                    request_id=request_id
                )

                self.assert_error(
                    response,
                    request_id,
                    "UNKNOWN_COMMAND"
                )

    def test_invalid_json(self):
        """Verify rejection of syntactically invalid JSON lines."""

        invalid_payloads = [
            "{",
            "}",
            '{"id":',
            '{"id":"bad","command":"help","params":{}',
            '{"id":"bad","command":"help","params":{},}',
            '{"id":"bad","command":"help","params":{}} trailing',
            "{'id':'bad','command':'help','params':{}}",
            '{"id":"bad","command":"help","params":NaN}',
            '{"id":"bad","command":"help","params":Infinity}',
            '{"id":"bad","command":"help","params":-Infinity}',
            '{"id":"bad","command":"help","params":1e9999}',
            r'{"id":"\q","command":"help","params":{}}',
            '{"id":"bad","command":"help","params":{}}{}',
            "// Invalid JSON comment",
            "undefined"
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.raw_error(
                    payload,
                    expected_id=None,
                    expected_code="INVALID_JSON"
                )

        self.request_success(
            "help"
        )

    def test_invalid_root(self):
        """Verify rejection of non-object JSON request roots."""

        invalid_roots = [
            [],
            [1, 2, 3],
            [
                make_request("help")
            ],
            "help",
            "",
            42,
            3.14159,
            True,
            False,
            None
        ]

        for value in invalid_roots:
            with self.subTest(value=value):
                self.raw_error(
                    encode_json(value),
                    expected_id=None,
                    expected_code="INVALID_JSON"
                )

        self.request_success(
            "getCapabilities"
        )

    def test_missing_fields(self):
        """Verify rejection of missing required request fields."""

        complete = make_request(
            "help",
            request_id="missing-fields"
        )

        for field in ("id", "command", "params"):
            with self.subTest(field=field):
                document = complete.copy()
                del document[field]

                self.client.send_document(
                    document
                )

                expected_id = (
                    None
                    if field == "id"
                    else "missing-fields"
                )

                self.assert_error(
                    self.client.read(),
                    expected_id,
                    "INVALID_PARAMETERS"
                )

        self.raw_error(
            "{}",
            expected_id=None,
            expected_code="INVALID_PARAMETERS"
        )

        parameters = command_parameters()

        self.assertEqual(
            set(parameters),
            EXPECTED_COMMANDS
        )

        for command, params in parameters.items():
            for field in params:
                with self.subTest(
                    command=command,
                    missing_field=field
                ):
                    incomplete = copy.deepcopy(
                        params
                    )

                    del incomplete[field]

                    self.request_error(
                        command,
                        incomplete
                    )

        nested_cases = [
            (
                "transformCreate",
                "parents",
                parents_input()
            ),
            (
                "transformCreate",
                "transform",
                transform_input()
            ),
            (
                "transformTessellate",
                "options",
                tessellation_options()
            ),
            (
                "geometryCreate",
                "transform",
                transform_input()
            ),
            (
                "materialCreate",
                "visual",
                visual_input()
            )
        ]

        for command, field, nested in nested_cases:
            for nested_field in nested:
                with self.subTest(
                    command=command,
                    field=field,
                    missing_nested_field=nested_field
                ):
                    params = copy.deepcopy(
                        parameters[command]
                    )

                    del params[field][nested_field]

                    self.request_error(
                        command,
                        params
                    )

        for source, command in (
            ("occurrence", "transformCreate"),
            ("material", "materialCreate")
        ):
            property_value = property_input(
                source
            )

            for field in property_value:
                with self.subTest(
                    command=command,
                    missing_property_field=field
                ):
                    incomplete = copy.deepcopy(
                        property_value
                    )

                    del incomplete[field]

                    params = copy.deepcopy(
                        parameters[command]
                    )

                    params["properties"] = [
                        incomplete
                    ]

                    self.request_error(
                        command,
                        params
                    )

        self.request_success(
            "help"
        )

    def test_unknown_fields(self):
        """Verify rejection of undocumented JSON fields."""

        document = make_request(
            "help",
            request_id="unknown-root-field"
        )

        document["debug"] = True

        self.client.send_document(
            document
        )

        self.assert_error(
            self.client.read(),
            "unknown-root-field",
            "INVALID_PARAMETERS"
        )

        for command, params in command_parameters().items():
            with self.subTest(command=command):
                invalid = copy.deepcopy(
                    params
                )

                invalid["debug"] = True

                self.request_error(
                    command,
                    invalid
                )

        nested_cases = [
            (
                "transformCreate",
                "parents"
            ),
            (
                "transformCreate",
                "transform"
            ),
            (
                "transformTessellate",
                "options"
            ),
            (
                "geometryCreate",
                "transform"
            ),
            (
                "materialCreate",
                "visual"
            )
        ]

        for command, field in nested_cases:
            with self.subTest(
                command=command,
                field=field
            ):
                params = command_parameters()[command]

                params[field]["unknownField"] = True

                self.request_error(
                    command,
                    params
                )

        for source, command in (
            ("occurrence", "transformCreate"),
            ("material", "materialCreate")
        ):
            with self.subTest(command=command):
                property_value = property_input(
                    source
                )

                property_value["unknownField"] = True

                params = command_parameters()[command]

                params["properties"] = [
                    property_value
                ]

                self.request_error(
                    command,
                    params
                )

        self.request_error(
            "geometryUpdate",
            {
                "id": "geometry:1",
                "kind": "DOMAIN"
            }
        )

        self.request_error(
            "transformTessellate",
            {
                "id": "transform:1",
                "options": {
                    **tessellation_options(),
                    "geometryId": "geometry:1"
                }
            }
        )

        self.request_success(
            "help"
        )

    def test_invalid_id(self):
        """Verify rejection of invalid request identifiers."""

        invalid_identifiers = [
            "",
            None,
            0,
            42,
            -1,
            1.5,
            True,
            False,
            [],
            {},
            ["request-1"],
            {"id": "request-1"}
        ]

        for value in invalid_identifiers:
            with self.subTest(value=value):
                document = make_request(
                    "help",
                    request_id=value
                )

                self.client.send_document(
                    document
                )

                self.assert_error(
                    self.client.read(),
                    None,
                    "INVALID_PARAMETERS"
                )

        self.request_success(
            "help"
        )

    def test_invalid_params(self):
        """Verify that params must be a JSON object."""

        invalid_parameters = [
            None,
            [],
            [1, 2, 3],
            "",
            "model.ifc",
            42,
            3.14,
            True,
            False
        ]

        for value in invalid_parameters:
            with self.subTest(value=value):
                document = {
                    "id": "invalid-params",
                    "command": "help",
                    "params": value
                }

                self.client.send_document(
                    document
                )

                self.assert_error(
                    self.client.read(),
                    "invalid-params",
                    "INVALID_PARAMETERS"
                )

        invalid_objects = [
            {
                "command": "help",
                "params": {
                    "unexpected": True
                }
            },
            {
                "command": "load",
                "params": {
                    "path": ""
                }
            },
            {
                "command": "geometryGetAll",
                "params": {}
            }
        ]

        for case in invalid_objects:
            with self.subTest(case=case):
                self.request_error(
                    case["command"],
                    case["params"]
                )

        self.request_success(
            "getCapabilities"
        )

    def test_unknown_command(self):
        """Verify explicit UNKNOWN_COMMAND errors."""

        commands = [
            "unknownCommand",
            "getAll",
            "geometryTessellate",
            "transformRemove",
            "materialRemove",
            "load(path)",
            "help()",
            " help",
            "help ",
            "notImplemented"
        ]

        for command in commands:
            with self.subTest(command=command):
                error = self.request_error(
                    command,
                    {},
                    "UNKNOWN_COMMAND"
                )

                self.assertEqual(
                    error["details"].get("command"),
                    command
                )

        self.request_success(
            "help"
        )

    def test_command_case(self):
        """Verify case sensitivity of commands and JSON fields."""

        invalid_commands = [
            "Help",
            "HELP",
            "getcapabilities",
            "GetCapabilities",
            "TransformGetAll",
            "transformgetall",
            "geometrygetall",
            "GeometryCreate",
            "MaterialCreate",
            "EXIT"
        ]

        for command in invalid_commands:
            with self.subTest(command=command):
                self.request_error(
                    command,
                    {},
                    "UNKNOWN_COMMAND"
                )

        document = make_request(
            "help",
            request_id="field-case"
        )

        for field, replacement in (
            ("id", "Id"),
            ("command", "Command"),
            ("params", "Params")
        ):
            with self.subTest(field=field):
                invalid = document.copy()

                invalid[replacement] = invalid.pop(
                    field
                )

                self.client.send_document(
                    invalid
                )

                expected_id = (
                    None
                    if field == "id"
                    else "field-case"
                )

                self.assert_error(
                    self.client.read(),
                    expected_id,
                    "INVALID_PARAMETERS"
                )

        self.request_error(
            "load",
            {
                "Path": "model.ifc"
            }
        )

        self.request_error(
            "transformGet",
            {
                "ID": "transform:1"
            }
        )

        self.request_error(
            "geometryGetAll",
            {
                "TransformId": None
            }
        )

        for kind in (
            "solid",
            "domain",
            "all",
            "Solid",
            "Domain"
        ):
            with self.subTest(kind=kind):
                self.request_error(
                    "geometryGetAll",
                    {
                        "transformId": None,
                        "kind": kind
                    }
                )

        for operation in (
            "add",
            "subtract",
            "Add"
        ):
            with self.subTest(operation=operation):
                params = geometry_create_params()

                params["operation"] = operation

                self.request_error(
                    "geometryCreate",
                    params
                )

        self.request_success(
            "help"
        )

    def test_invalid_types(self):
        """Verify strict JSON types without implicit conversions."""

        invalid_commands = [
            None,
            "",
            42,
            True,
            False,
            [],
            {}
        ]

        for command in invalid_commands:
            with self.subTest(command=command):
                self.client.send_document(
                    make_request(
                        command,
                        request_id="invalid-command-type"
                    )
                )

                self.assert_error(
                    self.client.read(),
                    "invalid-command-type",
                    "INVALID_PARAMETERS"
                )

        cases = [
            (
                "load",
                ("path",),
                [None, 42, True, [], {}]
            ),
            (
                "save",
                ("path",),
                [None, 42, False, [], {}]
            ),
            (
                "transformGet",
                ("id",),
                [None, 1, True, [], {}, ""]
            ),
            (
                "geometryGet",
                ("id",),
                [None, 1, True, [], {}, ""]
            ),
            (
                "materialGet",
                ("id",),
                [None, 1, True, [], {}, ""]
            ),
            (
                "transformDuplicate",
                ("includeChildren",),
                [0, 1, "true", "false", None, [], {}]
            ),
            (
                "transformCreate",
                ("name",),
                [None, 42, True, [], {}]
            ),
            (
                "transformCreate",
                ("parents",),
                [None, [], True, "parent"]
            ),
            (
                "transformCreate",
                ("parents", "spatial"),
                [42, True, [], {}, ""]
            ),
            (
                "transformCreate",
                ("parents", "placement"),
                [42, True, [], {}, ""]
            ),
            (
                "transformCreate",
                ("parents", "decomposition"),
                [42, True, [], {}, ""]
            ),
            (
                "transformCreate",
                ("transform",),
                [None, [], True, "matrix"]
            ),
            (
                "transformCreate",
                ("transform", "space"),
                [None, 42, True, [], {}, "local"]
            ),
            (
                "transformCreate",
                ("transform", "matrix"),
                [None, "matrix", {}, [], [1, 2, 3]]
            ),
            (
                "transformCreate",
                ("transform", "matrix", 0),
                ["1", True, None, [], {}]
            ),
            (
                "transformCreate",
                ("properties",),
                [None, {}, "properties", 42, True]
            ),
            (
                "transformTessellate",
                ("options",),
                [None, [], True, "world"]
            ),
            (
                "transformTessellate",
                ("options", "space"),
                [None, 42, True, "parent"]
            ),
            (
                "transformTessellate",
                ("options", "includeNormals"),
                [0, 1, "true", None, [], {}]
            ),
            (
                "transformTessellate",
                ("options", "includeMaterials"),
                [0, 1, "false", None, [], {}]
            ),
            (
                "transformTessellate",
                ("options", "includeChildren"),
                [0, 1, "true", None, [], {}]
            ),
            (
                "transformTessellate",
                ("options", "kind"),
                [None, 42, True, [], {}, "ALL", "unknown"]
            ),
            (
                "geometryGetAll",
                ("transformId",),
                [42, True, [], {}, ""]
            ),
            (
                "geometryGetAll",
                ("kind",),
                [None, 42, True, [], {}, "unknown"]
            ),
            (
                "geometryCreate",
                ("parentId",),
                [None, 42, True, [], {}, ""]
            ),
            (
                "geometryCreate",
                ("name",),
                [None, 42, True, [], {}]
            ),
            (
                "geometryCreate",
                ("profile",),
                [None, {}, "profile", 42, True]
            ),
            (
                "geometryCreate",
                ("profile", 0),
                [None, "0,0", {}, [], [0], [0, 0, 0]]
            ),
            (
                "geometryCreate",
                ("profile", 0, 0),
                ["0", True, None, [], {}]
            ),
            (
                "geometryCreate",
                ("depth",),
                ["1", True, False, None, [], {}, 0, -1]
            ),
            (
                "geometryCreate",
                ("operation",),
                [None, 42, True, [], {}, "BASE", ""]
            ),
            (
                "geometryCreate",
                ("kind",),
                [None, 42, True, [], {}, "ALL", "unknown"]
            ),
            (
                "geometryUpdate",
                ("depth",),
                ["2", True, None, [], {}, 0, -1]
            ),
            (
                "materialCreate",
                ("name",),
                [None, 42, True, [], {}]
            ),
            (
                "materialCreate",
                ("category",),
                [None, 42, True, [], {}]
            ),
            (
                "materialCreate",
                ("visual",),
                [None, [], True, "visual"]
            ),
            (
                "materialCreate",
                ("visual", "color"),
                [None, {}, "red", [], [0, 0], [0, 0, 0, 0]]
            ),
            (
                "materialCreate",
                ("visual", "color", 0),
                ["0.5", True, None, [], {}, -0.1, 1.1]
            ),
            (
                "materialCreate",
                ("visual", "opacity"),
                ["1", True, None, [], {}, -0.1, 1.1]
            ),
            (
                "materialCreate",
                ("visual", "metallic"),
                ["0", False, None, [], {}, -0.1, 1.1]
            ),
            (
                "materialCreate",
                ("visual", "roughness"),
                ["0.5", True, None, [], {}, -0.1, 1.1]
            ),
            (
                "materialCreate",
                ("properties",),
                [None, {}, "properties", 42, True]
            ),
            (
                "materialAssign",
                ("materialId",),
                [None, 42, True, [], {}, ""]
            ),
            (
                "materialAssign",
                ("targetId",),
                [None, 42, True, [], {}, ""]
            ),
            (
                "materialUnassign",
                ("materialId",),
                [None, 42, True, [], {}, ""]
            ),
            (
                "materialUnassign",
                ("targetId",),
                [None, 42, True, [], {}, ""]
            )
        ]

        parameters = command_parameters()

        for command, path, values in cases:
            for value in values:
                with self.subTest(
                    command=command,
                    field=path,
                    value=value
                ):
                    params = replace_nested_value(
                        parameters[command],
                        path,
                        value
                    )

                    self.request_error(
                        command,
                        params
                    )

        for source, command in (
            ("occurrence", "transformCreate"),
            ("material", "materialCreate")
        ):
            property_cases = {
                "propertySet": [
                    None, 42, True, [], {}, ""
                ],
                "name": [
                    None, 42, True, [], {}, ""
                ],
                "valueType": [
                    None, 42, True, [], {}, ""
                ],
                "value": [
                    [], {}, [1, 2]
                ],
                "unit": [
                    42, True, [], {}, ""
                ],
                "source": [
                    None, 42, True, [], {}, "", "invalid"
                ]
            }

            for field, values in property_cases.items():
                for value in values:
                    with self.subTest(
                        command=command,
                        property_field=field,
                        value=value
                    ):
                        property_value = property_input(
                            source
                        )

                        property_value[field] = value

                        params = copy.deepcopy(
                            parameters[command]
                        )

                        params["properties"] = [
                            property_value
                        ]

                        self.request_error(
                            command,
                            params
                        )

        self.request_success(
            "help"
        )

    def test_blank_lines(self):
        """Verify that whitespace-only lines produce no responses."""

        blank_lines = [
            "",
            " ",
            "     ",
            "\t",
            "\t\t",
            " \t ",
            "\r",
            "\r\n",
            " \t\r"
        ]

        for line in blank_lines:
            self.client.send_raw(
                line
            )

        request_id = self.client.send(
            "help",
            request_id="after-blank-lines"
        )

        response = self.client.read()

        self.assert_success(
            response,
            request_id
        )

        self.client.write_raw(
            "\n\n \t\n\r\n"
        )

        request_id = self.client.send(
            "getCapabilities",
            request_id="after-more-blank-lines"
        )

        self.assert_success(
            self.client.read(),
            request_id
        )

        self.client.send(
            "exit",
            request_id="blank-lines-exit"
        )

        self.assert_success(
            self.client.read(),
            "blank-lines-exit"
        )

        self.assertEqual(
            self.client.wait(),
            0
        )

        with self.assertRaises(EOFError):
            self.client.read_line()

    def test_multiple_requests(self):
        """Verify sequential processing of multiple queued requests."""

        documents = [
            make_request(
                "help",
                request_id="batch-1"
            ),
            make_request(
                "getCapabilities",
                request_id="batch-2"
            ),
            make_request(
                "unknownCommand",
                request_id="batch-3"
            ),
            make_request(
                "help",
                request_id="batch-4"
            ),
            make_request(
                "geometryGetAll",
                {"transformId": None},
                request_id="batch-5"
            ),
            make_request(
                "getCapabilities",
                request_id="batch-6"
            )
        ]

        payload = "\n".join(
            encode_json(document)
            for document in documents
        ) + "\n"

        self.client.write_raw(
            payload
        )

        expected = [
            ("batch-1", None),
            ("batch-2", None),
            ("batch-3", "UNKNOWN_COMMAND"),
            ("batch-4", None),
            ("batch-5", "NO_MODEL_LOADED"),
            ("batch-6", None)
        ]

        for request_id, error_code in expected:
            with self.subTest(request_id=request_id):
                response = self.client.read()

                if error_code is None:
                    self.assert_success(
                        response,
                        request_id
                    )
                else:
                    self.assert_error(
                        response,
                        request_id,
                        error_code
                    )

        self.request_success(
            "help"
        )

    def test_response_structure(self):
        """Verify exact success and error JSON response envelopes."""

        success = self.client.request(
            "help",
            request_id="success-response"
        )

        result = self.assert_success(
            success,
            "success-response"
        )

        self.assertIsInstance(
            result,
            dict
        )

        self.assertEqual(
            set(success),
            {"id", "ok", "result"}
        )

        self.assertNotIn(
            "error",
            success
        )

        cases = [
            (
                "unknownCommand",
                {},
                "UNKNOWN_COMMAND"
            ),
            (
                "help",
                {"invalid": True},
                "INVALID_PARAMETERS"
            ),
            (
                "transformGetAll",
                {},
                "NO_MODEL_LOADED"
            ),
            (
                "load",
                {
                    "path": str(
                        Path(self.directory.name) / "missing.ifc"
                    )
                },
                "FILE_NOT_FOUND"
            ),
            (
                "transformGet",
                {"id": "geometry:1"},
                "INVALID_ENTITY_TYPE"
            )
        ]

        for command, params, code in cases:
            with self.subTest(
                command=command,
                code=code
            ):
                response = self.client.request(
                    command,
                    params,
                    request_id=f"error-{code}"
                )

                self.assert_error(
                    response,
                    f"error-{code}",
                    code
                )

                self.assertEqual(
                    set(response),
                    {"id", "ok", "error"}
                )

                self.assertNotIn(
                    "result",
                    response
                )

        self.raw_error(
            "{invalid}",
            expected_id=None,
            expected_code="INVALID_JSON"
        )

        self.load_model()

        self.request_error(
            "transformGet",
            {
                "id": "transform:999999"
            },
            "ENTITY_NOT_FOUND"
        )

    def test_unicode(self):
        """Verify Unicode identifiers, names, properties and paths."""

        identifiers = [
            "requête-française",
            "Überprüfung",
            "Δοκιμή",
            "测试请求",
            "日本語",
            "اختبار"
        ]

        for request_id in identifiers:
            with self.subTest(request_id=request_id):
                response = self.client.request(
                    "help",
                    request_id=request_id
                )

                self.assert_success(
                    response,
                    request_id
                )

        self.input_path = (
            Path(self.directory.name) / "modèle bâtiment 测试.ifc"
        )

        self.input_path.write_text(
            IFC_FIXTURE,
            encoding="utf-8"
        )

        self.load_model()

        parent_id = self.find_parent()

        name = "Élément géométrique — Gebäude — 建筑"

        property_value = {
            "propertySet": "Pset_Unicode",
            "name": "Description",
            "valueType": "IfcText",
            "value": "Données de simulation — Δοκιμή — 日本語",
            "unit": None,
            "source": "occurrence"
        }

        updated = self.request_success(
            "transformUpdate",
            {
                "id": parent_id,
                "name": name,
                "properties": [
                    property_value
                ]
            }
        )

        self.assertEqual(
            updated["name"],
            name
        )

        retrieved = self.request_success(
            "transformGet",
            {
                "id": parent_id
            }
        )

        self.assertEqual(
            retrieved["name"],
            name
        )

        properties = [
            item
            for item in retrieved["properties"]
            if (
                item["propertySet"] == property_value["propertySet"]
                and item["name"] == property_value["name"]
            )
        ]

        self.assertEqual(
            len(properties),
            1
        )

        self.assertEqual(
            properties[0]["value"],
            property_value["value"]
        )

        self.output_path = (
            Path(self.directory.name) / "résultat simulation 建筑.ifc"
        )

        saved = self.request_success(
            "save",
            {
                "path": str(self.output_path)
            }
        )

        self.assertEqual(
            saved["path"],
            str(self.output_path)
        )

        self.assertTrue(
            self.output_path.is_file()
        )

        self.request_success(
            "load",
            {
                "path": str(self.output_path)
            }
        )

        parent_id = self.find_parent()

        reloaded = self.request_success(
            "transformGet",
            {
                "id": parent_id
            }
        )

        self.assertEqual(
            reloaded["name"],
            name
        )

        properties = [
            item
            for item in reloaded["properties"]
            if (
                item["propertySet"] == property_value["propertySet"]
                and item["name"] == property_value["name"]
            )
        ]

        self.assertEqual(
            len(properties),
            1
        )

        self.assertEqual(
            properties[0]["value"],
            property_value["value"]
        )

    def test_stdout_integrity(self):
        """Verify that stdout contains only valid JSONL responses."""

        self.request_success(
            "help"
        )

        self.raw_error(
            "{invalid-json}",
            expected_id=None,
            expected_code="INVALID_JSON"
        )

        self.request_error(
            "unknownCommand",
            {},
            "UNKNOWN_COMMAND"
        )

        self.load_model()

        parent_id = self.find_parent()

        mesh_request_id = self.client.send(
            "transformTessellate",
            {
                "id": parent_id,
                "options": {
                    "space": "world",
                    "includeNormals": True,
                    "includeMaterials": True,
                    "includeChildren": False,
                    "kind": "SOLID"
                }
            },
            request_id="stdout-mesh"
        )

        events = []
        vertex_count = None
        index_count = None
        vertices = []
        indices = []

        while True:
            response = self.client.read()

            event = self.assert_success(
                response,
                mesh_request_id
            )

            self.assertIsInstance(
                event,
                dict
            )

            event_name = event.get("event")

            self.assertIn(
                event_name,
                {
                    "mesh.begin",
                    "mesh.vertices",
                    "mesh.indices",
                    "mesh.normals",
                    "mesh.materials",
                    "mesh.end"
                }
            )

            events.append(
                event_name
            )

            if event_name == "mesh.begin":
                vertex_count = event["vertexCount"]
                index_count = event["indexCount"]

            elif event_name == "mesh.vertices":
                self.assertEqual(
                    event["offset"],
                    len(vertices)
                )

                vertices.extend(
                    event["values"]
                )

            elif event_name == "mesh.indices":
                self.assertEqual(
                    event["offset"],
                    len(indices)
                )

                indices.extend(
                    event["values"]
                )

            elif event_name == "mesh.end":
                break

        self.assertEqual(
            events[0],
            "mesh.begin"
        )

        self.assertEqual(
            events[-1],
            "mesh.end"
        )

        self.assertEqual(
            events.count("mesh.begin"),
            1
        )

        self.assertEqual(
            events.count("mesh.end"),
            1
        )

        self.assertEqual(
            events.count("mesh.materials"),
            1
        )

        self.assertIn(
            "mesh.vertices",
            events
        )

        self.assertIn(
            "mesh.indices",
            events
        )

        self.assertIsNotNone(
            vertex_count
        )

        self.assertIsNotNone(
            index_count
        )

        self.assertGreater(
            vertex_count,
            0
        )

        self.assertGreater(
            index_count,
            0
        )

        self.assertEqual(
            len(vertices),
            vertex_count * 3
        )

        self.assertEqual(
            len(indices),
            index_count
        )

        self.request_success(
            "save",
            {
                "path": str(self.output_path)
            }
        )

        self.request_success(
            "getCapabilities"
        )

        self.client.send(
            "exit",
            request_id="stdout-exit"
        )

        self.assert_success(
            self.client.read(),
            "stdout-exit"
        )

        self.assertEqual(
            self.client.wait(),
            0
        )

        with self.assertRaises(EOFError):
            self.client.read_line()

    def test_exit(self):
        """Verify that exit returns a response and terminates the CLI."""

        self.request_success(
            "help"
        )

        self.assertIsNone(
            self.client.process.poll()
        )

        payload = "\n".join([
            encode_json(
                make_request(
                    "exit",
                    request_id="exit-request"
                )
            ),
            encode_json(
                make_request(
                    "help",
                    request_id="after-exit"
                )
            )
        ]) + "\n"

        self.client.write_raw(
            payload
        )

        response = self.client.read()

        result = self.assert_success(
            response,
            "exit-request"
        )

        self.assertEqual(
            result,
            {}
        )

        self.assertEqual(
            self.client.wait(),
            0
        )

        self.assertFalse(
            self.client.process.stdin.closed,
            "The CLI must exit without requiring stdin to be closed"
        )

        with self.assertRaises(EOFError):
            self.client.read_line()

    def test_end_of_input(self):
        """Verify graceful process termination when stdin is closed."""

        self.load_model()

        self.assertIsNone(
            self.client.process.poll()
        )

        self.client.send(
            "getCapabilities",
            request_id="before-eof"
        )

        self.client.close_input()

        response = self.client.read()

        self.assert_success(
            response,
            "before-eof"
        )

        self.assertEqual(
            self.client.wait(),
            0
        )

        with self.assertRaises(EOFError):
            self.client.read_line()

        empty_client = Client(
            EXECUTABLE
        )

        self.addCleanup(
            empty_client.terminate
        )

        empty_client.close_input()

        self.assertEqual(
            empty_client.wait(),
            0
        )

        with self.assertRaises(EOFError):
            empty_client.read_line()


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