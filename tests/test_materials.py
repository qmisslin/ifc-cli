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
# Expected results must be validated against the compiled CLI.
# Native IFC materials and complex material definitions are read-only.
# Visual metadata and IFC surface rendering are validated independently.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30
NUMERIC_TOLERANCE = 1e-9

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

PROJECT_GLOBAL_ID = "0000000000000000000001"
PRODUCT_GLOBAL_ID = "0000000000000000000002"

MATERIAL_SUMMARY_FIELDS = {
    "id",
    "ifcEntityId",
    "globalId"
}

MATERIAL_FIELDS = MATERIAL_SUMMARY_FIELDS | {
    "name",
    "category",
    "editable",
    "visual",
    "properties",
    "definition"
}

VISUAL_FIELDS = {
    "color",
    "opacity",
    "metallic",
    "roughness"
}

DEFAULT_VISUAL = {
    "color": [0.6, 0.6, 0.6],
    "opacity": 1.0,
    "metallic": 0.0,
    "roughness": 0.8
}

INTERNAL_PROPERTY_SETS = {
    "Pset_IfcEditorVisual",
    "Pset_IfcEditorMaterial"
}

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('materials-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#8=IFCPROJECT('0000000000000000000001',$,'Material test project',$,$,$,$,(#5),#7);
#9=IFCLOCALPLACEMENT($,#4);
#10=IFCBUILDINGELEMENTPROXY('0000000000000000000002',$,'Material target',$,$,#9,$,$,.NOTDEFINED.);
#11=IFCMATERIAL('Native concrete',$,'concrete');
#12=IFCMATERIAL('Native steel',$,'metal');
#13=IFCMATERIALLIST((#11,#12));
#14=IFCMATERIALLAYER(#11,0.2,$,'Native concrete layer',$,'concrete',$);
#15=IFCMATERIALLAYER(#12,0.01,$,'Native steel layer',$,'metal',$);
#16=IFCMATERIALLAYERSET((#14,#15),'Native composite layer set',$);
#17=IFCPROPERTYSINGLEVALUE('NativeReference',$,IFCLABEL('NATIVE-CONCRETE'),$);
#18=IFCMATERIALPROPERTIES('Pset_NativeMaterial',$,(#17),#11);
ENDSEC;
END-ISO-10303-21;
"""


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
    property_set="Pset_MaterialTest",
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


def property_key(property_value):
    return (
        property_value["propertySet"],
        property_value["name"]
    )


def material_create_params(
    name="Created material",
    category="concrete",
    visual=None,
    properties=None
):
    return {
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
            else copy.deepcopy(properties)
        )
    }


def parse_step_entities(content):
    # Targeted STEP parser for the controlled test fixture.
    # This is not a general-purpose IFC parser.

    pattern = re.compile(
        r"#(\d+)\s*=\s*(IFC[A-Z0-9_]+)\s*\((.*?)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL
    )

    entities = {}

    for match in pattern.finditer(content):
        identifier = int(match.group(1))

        if identifier in entities:
            raise AssertionError(
                f"Duplicate STEP entity #{identifier}"
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


def step_string(value):
    return "'" + value.replace("'", "''") + "'"


def split_step_arguments(arguments):
    # Split top-level STEP arguments while preserving nested
    # aggregates, typed values and escaped string literals.

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
                result.append(
                    "".join(current).strip()
                )
                current = []

            else:
                current.append(character)

        else:
            current.append(character)

        index += 1

    result.append(
        "".join(current).strip()
    )

    return result


def read_step_typed_number(value, expected_type):
    match = re.fullmatch(
        rf"{re.escape(expected_type)}\s*\((.*?)\)",
        value,
        flags=re.IGNORECASE
    )

    if match is None:
        raise AssertionError(
            f"Expected {expected_type}, received {value}"
        )

    return float(match.group(1))


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

            if set(error) != {
                "code",
                "message",
                "details"
            }:
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
        expected_code="INVALID_PARAMETERS"
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


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-materials-"
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

        self.output_path = (
            self.root_path / "output.ifc"
        )

        self.input_path.write_text(
            IFC_FIXTURE,
            encoding="utf-8"
        )

        self.client = Client(EXECUTABLE)

        self.addCleanup(
            self.client.terminate
        )

        self.load_model()

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
            {"path": str(path)}
        )

        self.assertEqual(
            result["path"],
            str(path)
        )

        self.assertEqual(
            result["schema"].upper(),
            "IFC4"
        )

        self.assertEqual(
            result["materialCount"],
            len(self.list_materials())
        )

        transforms = self.client.request(
            "transformGetAll"
        )

        matches = [
            transform
            for transform in transforms
            if transform["globalId"] == PRODUCT_GLOBAL_ID
        ]

        self.assertEqual(
            len(matches),
            1
        )

        self.product_id = matches[0]["id"]

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

        self.assertTrue(
            self.output_path.is_file()
        )

        self.assertGreater(
            self.output_path.stat().st_size,
            0
        )

        return self.output_path.read_text(
            encoding="utf-8"
        )

    def save_and_reload(self):
        self.save_model()

        return self.load_model(
            self.output_path
        )

    def list_materials(self):
        return self.client.request(
            "materialGetAll"
        )

    def get_material(self, identifier):
        return self.client.request(
            "materialGet",
            {"id": identifier}
        )

    def get_all_materials(self):
        return [
            self.get_material(summary["id"])
            for summary in self.list_materials()
        ]

    def find_material(self, name, ifc_class=None):
        matches = [
            material
            for material in self.get_all_materials()
            if (
                material["name"] == name
                and (
                    ifc_class is None
                    or material["definition"]["ifcClass"] == ifc_class
                )
            )
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one material named {name}"
        )

        return matches[0]

    def create_material(
        self,
        name="Created material",
        category="concrete",
        visual=None,
        properties=None
    ):
        return self.client.request(
            "materialCreate",
            material_create_params(
                name=name,
                category=category,
                visual=visual,
                properties=properties
            )
        )

    def update_material(
        self,
        identifier,
        **changes
    ):
        result = self.client.request(
            "materialUpdate",
            {
                "id": identifier,
                **changes
            }
        )

        self.assertEqual(
            result["id"],
            identifier
        )

        self.assertEqual(
            self.get_material(identifier),
            result
        )

        return result

    def delete_material(self, identifier):
        result = self.client.request(
            "materialDelete",
            {"id": identifier}
        )

        self.assertEqual(
            result,
            {"id": identifier}
        )

        self.assertNotIn(
            identifier,
            {
                material["id"]
                for material in self.list_materials()
            }
        )

        self.client.expect_error(
            "materialGet",
            {"id": identifier},
            "ENTITY_NOT_FOUND"
        )

    def assign_material(
        self,
        material_id,
        target_id
    ):
        params = {
            "materialId": material_id,
            "targetId": target_id
        }

        result = self.client.request(
            "materialAssign",
            params
        )

        self.assertEqual(
            result,
            params
        )

    def get_product(self):
        return self.client.request(
            "transformGet",
            {"id": self.product_id}
        )

    def snapshot_materials(self):
        return {
            material["id"]: material
            for material in self.get_all_materials()
        }

    def material_signature(self, material):
        return (
            material["definition"]["ifcClass"],
            material["name"],
            material["category"],
            material["editable"]
        )

    def assert_number(
        self,
        actual,
        expected,
        tolerance=NUMERIC_TOLERANCE
    ):
        self.assertIn(
            type(actual),
            (int, float)
        )

        self.assertTrue(
            math.isfinite(actual)
        )

        self.assertAlmostEqual(
            actual,
            expected,
            delta=tolerance
        )

    def assert_visual(self, actual, expected):
        self.assertIsInstance(
            actual,
            dict
        )

        self.assertEqual(
            set(actual),
            VISUAL_FIELDS
        )

        self.assertIsInstance(
            actual["color"],
            list
        )

        self.assertEqual(
            len(actual["color"]),
            3
        )

        for index in range(3):
            with self.subTest(color_component=index):
                self.assert_number(
                    actual["color"][index],
                    expected["color"][index]
                )

                self.assertGreaterEqual(
                    actual["color"][index],
                    0.0
                )

                self.assertLessEqual(
                    actual["color"][index],
                    1.0
                )

        for field in (
            "opacity",
            "metallic",
            "roughness"
        ):
            with self.subTest(visual_field=field):
                self.assert_number(
                    actual[field],
                    expected[field]
                )

                self.assertGreaterEqual(
                    actual[field],
                    0.0
                )

                self.assertLessEqual(
                    actual[field],
                    1.0
                )

    def assert_properties(self, actual, expected):
        self.assertIsInstance(
            actual,
            list
        )

        actual_index = {
            property_key(property_value): property_value
            for property_value in actual
        }

        expected_index = {
            property_key(property_value): property_value
            for property_value in expected
        }

        self.assertEqual(
            len(actual),
            len(actual_index),
            "Duplicate material properties"
        )

        self.assertEqual(
            len(expected),
            len(expected_index)
        )

        self.assertEqual(
            set(actual_index),
            set(expected_index)
        )

        for key, reference in expected_index.items():
            with self.subTest(property=key):
                value = actual_index[key]

                self.assertEqual(
                    set(value),
                    {
                        "propertySet",
                        "name",
                        "valueType",
                        "value",
                        "unit",
                        "source"
                    }
                )

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

                if type(reference["value"]) is float:
                    self.assert_number(
                        value["value"],
                        reference["value"]
                    )

                else:
                    self.assertIs(
                        type(value["value"]),
                        type(reference["value"])
                    )

                    self.assertEqual(
                        value["value"],
                        reference["value"]
                    )

                self.assertEqual(
                    value["source"],
                    "material"
                )

                self.assertNotIn(
                    value["propertySet"],
                    INTERNAL_PROPERTY_SETS
                )

    def assert_identity(self, before, after):
        for field in MATERIAL_SUMMARY_FIELDS:
            self.assertEqual(
                before[field],
                after[field],
                f"Material identity changed: {field}"
            )

    def assert_material_summary(self, summary):
        self.assertIsInstance(
            summary,
            dict
        )

        self.assertEqual(
            set(summary),
            MATERIAL_SUMMARY_FIELDS
        )

        self.assertIsInstance(
            summary["id"],
            str
        )

        self.assertTrue(
            summary["id"].startswith("material:")
        )

        self.assertIs(
            type(summary["ifcEntityId"]),
            int
        )

        self.assertGreater(
            summary["ifcEntityId"],
            0
        )

        # IFC material definitions do not inherit from IfcRoot.

        self.assertIsNone(
            summary["globalId"]
        )

    def assert_material_structure(self, material):
        self.assertIsInstance(
            material,
            dict
        )

        self.assertEqual(
            set(material),
            MATERIAL_FIELDS
        )

        self.assert_material_summary({
            field: material[field]
            for field in MATERIAL_SUMMARY_FIELDS
        })

        self.assertIsInstance(
            material["name"],
            str
        )

        self.assertIsInstance(
            material["category"],
            str
        )

        self.assertIs(
            type(material["editable"]),
            bool
        )

        self.assert_visual(
            material["visual"],
            material["visual"]
        )

        self.assertIsInstance(
            material["properties"],
            list
        )

        definition = material["definition"]

        self.assertIsInstance(
            definition,
            dict
        )

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

        if material["editable"]:
            self.assertEqual(
                definition["ifcClass"],
                "IfcMaterial"
            )

        for property_value in material["properties"]:
            self.assertEqual(
                property_value["source"],
                "material"
            )

            self.assertNotIn(
                property_value["propertySet"],
                INTERNAL_PROPERTY_SETS
            )

    def assert_material(
        self,
        material,
        name,
        category,
        visual=None,
        properties=None,
        editable=True,
        ifc_class="IfcMaterial"
    ):
        visual = (
            DEFAULT_VISUAL
            if visual is None
            else visual
        )

        properties = (
            []
            if properties is None
            else properties
        )

        self.assert_material_structure(
            material
        )

        self.assertEqual(
            material["name"],
            name
        )

        self.assertEqual(
            material["category"],
            category
        )

        self.assertIs(
            material["editable"],
            editable
        )

        self.assertEqual(
            material["definition"]["ifcClass"],
            ifc_class
        )

        self.assert_visual(
            material["visual"],
            visual
        )

        self.assert_properties(
            material["properties"],
            properties
        )

        self.assertEqual(
            self.get_material(material["id"]),
            material
        )

    def assert_preserved_fields(
        self,
        before,
        after,
        modified_fields
    ):
        self.assert_identity(
            before,
            after
        )

        for field in MATERIAL_FIELDS:
            if field in modified_fields:
                continue

            # IFC definition references may change when the
            # corresponding native representation is replaced.

            if field == "definition":
                continue

            self.assertEqual(
                before[field],
                after[field],
                f"Unexpected modification of {field}"
            )

    def assert_material_index_consistency(self):
        summaries = self.list_materials()

        identifiers = [
            summary["id"]
            for summary in summaries
        ]

        entity_ids = [
            summary["ifcEntityId"]
            for summary in summaries
        ]

        self.assertEqual(
            len(identifiers),
            len(set(identifiers))
        )

        self.assertEqual(
            len(entity_ids),
            len(set(entity_ids))
        )

        for summary in summaries:
            with self.subTest(
                material_id=summary["id"]
            ):
                self.assert_material_summary(
                    summary
                )

                material = self.get_material(
                    summary["id"]
                )

                self.assert_material_structure(
                    material
                )

                self.assertEqual(
                    summary,
                    {
                        field: material[field]
                        for field in MATERIAL_SUMMARY_FIELDS
                    }
                )

    def assert_ifc_material(
        self,
        content,
        material
    ):
        # Validate the native IFC material independently from
        # the simplified JSON material representation.

        entities = parse_step_entities(content)

        matches = [
            identifier
            for identifier, entity in entities.items()
            if (
                entity["class"] == "IFCMATERIAL"
                and split_step_arguments(
                    entity["arguments"]
                )[0] == step_string(material["name"])
            )
        ]

        self.assertEqual(
            len(matches),
            1
        )

        material_id = matches[0]

        arguments = split_step_arguments(
            entities[material_id]["arguments"]
        )

        self.assertEqual(
            len(arguments),
            3
        )

        self.assertEqual(
            arguments[0],
            step_string(material["name"])
        )

        self.assertEqual(
            arguments[2],
            step_string(material["category"])
        )

        property_containers = []

        for entity in entities.values():
            if entity["class"] != "IFCMATERIALPROPERTIES":
                continue

            arguments = split_step_arguments(
                entity["arguments"]
            )

            if arguments[-1] == f"#{material_id}":
                property_containers.append(arguments)

        property_set_names = {
            arguments[0]
            for arguments in property_containers
        }

        for property_set in INTERNAL_PROPERTY_SETS:
            self.assertIn(
                step_string(property_set),
                property_set_names
            )

        for property_value in material["properties"]:
            self.assertIn(
                step_string(
                    property_value["propertySet"]
                ),
                property_set_names
            )

        return material_id

    def assert_ifc_visual(
        self,
        content,
        material
    ):
        # Verify actual IFC surface rendering, not only the
        # editor-specific visual property set.

        entities = parse_step_entities(content)

        material_id = self.assert_ifc_material(
            content,
            material
        )

        definitions = []

        for entity in entities.values():
            if (
                entity["class"]
                != "IFCMATERIALDEFINITIONREPRESENTATION"
            ):
                continue

            arguments = split_step_arguments(
                entity["arguments"]
            )

            if arguments[-1] == f"#{material_id}":
                definitions.append(entity)

        self.assertEqual(
            len(definitions),
            1,
            "Expected one material definition representation"
        )

        allowed_classes = {
            "IFCMATERIALDEFINITIONREPRESENTATION",
            "IFCSTYLEDREPRESENTATION",
            "IFCSTYLEDITEM",
            "IFCPRESENTATIONSTYLEASSIGNMENT",
            "IFCSURFACESTYLE"
        }

        pending = step_references(
            definitions[0]["arguments"]
        )

        visited = set()
        surface_styles = []

        while pending:
            identifier = pending.pop()

            if identifier in visited:
                continue

            visited.add(identifier)

            self.assertIn(
                identifier,
                entities
            )

            entity = entities[identifier]

            if entity["class"] not in allowed_classes:
                continue

            if entity["class"] == "IFCSURFACESTYLE":
                surface_styles.append(entity)
                continue

            pending.extend(
                step_references(
                    entity["arguments"]
                )
            )

        self.assertEqual(
            len(surface_styles),
            1
        )

        style = surface_styles[0]

        style_arguments = split_step_arguments(
            style["arguments"]
        )

        self.assertEqual(
            style_arguments[0],
            step_string(material["name"])
        )

        self.assertEqual(
            style_arguments[1],
            ".BOTH."
        )

        renderings = [
            entities[identifier]
            for identifier in step_references(
                style["arguments"]
            )
            if (
                entities[identifier]["class"]
                == "IFCSURFACESTYLERENDERING"
            )
        ]

        self.assertEqual(
            len(renderings),
            1
        )

        rendering = renderings[0]

        arguments = split_step_arguments(
            rendering["arguments"]
        )

        self.assertEqual(
            len(arguments),
            9
        )

        color_reference = arguments[0]

        self.assertRegex(
            color_reference,
            r"^#\d+$"
        )

        color_id = int(
            color_reference[1:]
        )

        color = entities[color_id]

        self.assertEqual(
            color["class"],
            "IFCCOLOURRGB"
        )

        color_arguments = split_step_arguments(
            color["arguments"]
        )

        self.assertEqual(
            len(color_arguments),
            4
        )

        actual_color = [
            float(value)
            for value in color_arguments[1:]
        ]

        expected_visual = material["visual"]

        for actual, expected in zip(
            actual_color,
            expected_visual["color"]
        ):
            self.assert_number(
                actual,
                expected
            )

        transparency = float(
            arguments[1]
        )

        self.assert_number(
            transparency,
            1.0 - expected_visual["opacity"]
        )

        diffuse_color_reference = arguments[2]

        self.assertRegex(
            diffuse_color_reference,
            r"^#\d+$"
        )

        diffuse_color = entities[
            int(diffuse_color_reference[1:])
        ]

        self.assertEqual(
            diffuse_color["class"],
            "IFCCOLOURRGB"
        )

        diffuse_arguments = split_step_arguments(
            diffuse_color["arguments"]
        )

        for actual, expected in zip(
            diffuse_arguments[1:],
            expected_visual["color"]
        ):
            self.assert_number(
                float(actual),
                expected
            )

        metallic = read_step_typed_number(
            arguments[6],
            "IFCNORMALISEDRATIOMEASURE"
        )

        self.assert_number(
            metallic,
            expected_visual["metallic"]
        )

        roughness = read_step_typed_number(
            arguments[7],
            "IFCSPECULARROUGHNESS"
        )

        self.assert_number(
            roughness,
            expected_visual["roughness"]
        )

    def assert_visual_roundtrip(self, material):
        content = self.save_model()

        self.assert_ifc_visual(
            content,
            material
        )

        self.load_model(
            self.output_path
        )

        reloaded = self.find_material(
            material["name"]
        )

        self.assert_material(
            reloaded,
            name=material["name"],
            category=material["category"],
            visual=material["visual"],
            properties=material["properties"]
        )

        self.assert_ifc_visual(
            self.save_model(),
            reloaded
        )

        return reloaded

    def assert_visual_component_updates(
        self,
        field,
        values
    ):
        initial_visual = visual_input(
            color=[0.125, 0.375, 0.875],
            opacity=0.625,
            metallic=0.25,
            roughness=0.75
        )

        material = self.create_material(
            name=f"Visual component {field}",
            category="test",
            visual=initial_visual
        )

        identifier = material["id"]

        expected_visual = copy.deepcopy(
            initial_visual
        )

        for value in values:
            with self.subTest(
                component=field,
                value=value
            ):
                before = self.get_material(
                    identifier
                )

                expected_visual[field] = copy.deepcopy(
                    value
                )

                updated = self.update_material(
                    identifier,
                    visual=expected_visual
                )

                self.assert_preserved_fields(
                    before,
                    updated,
                    {"visual"}
                )

                self.assert_visual(
                    updated["visual"],
                    expected_visual
                )

                for preserved_field in VISUAL_FIELDS - {field}:
                    self.assertEqual(
                        before["visual"][preserved_field],
                        updated["visual"][preserved_field]
                    )

                self.assert_ifc_visual(
                    self.save_model(),
                    updated
                )

        final = self.get_material(
            identifier
        )

        self.assert_visual_roundtrip(
            final
        )

    def assert_invalid_visuals(self, cases):
        material = self.create_material(
            name="Preserved validation material",
            category="validation",
            visual=visual_input(
                color=[0.2, 0.4, 0.6],
                opacity=0.8,
                metallic=0.1,
                roughness=0.3
            ),
            properties=[
                property_input(
                    "Reference",
                    "PRESERVED-MATERIAL"
                )
            ]
        )

        initial = self.snapshot_materials()
        product_before = self.get_product()

        for label, visual in cases:
            with self.subTest(case=label):
                params = material_create_params(
                    name="Rejected visual material",
                    category="validation"
                )

                params["visual"] = visual

                self.client.expect_error(
                    "materialCreate",
                    params
                )

                self.assertEqual(
                    self.snapshot_materials(),
                    initial
                )

                self.client.expect_error(
                    "materialUpdate",
                    {
                        "id": material["id"],
                        "visual": visual
                    }
                )

                self.assertEqual(
                    self.snapshot_materials(),
                    initial
                )

                # Validation must occur before any other field
                # in the same update is modified.

                self.client.expect_error(
                    "materialUpdate",
                    {
                        "id": material["id"],
                        "name": "Invalid renamed material",
                        "category": "invalid category",
                        "visual": visual
                    }
                )

                self.assertEqual(
                    self.snapshot_materials(),
                    initial
                )

                self.assertEqual(
                    self.get_product(),
                    product_before
                )

        self.assertEqual(
            self.get_material(material["id"]),
            material
        )

        content = self.save_model()

        self.assertNotIn(
            "Rejected visual material",
            content
        )

        self.assertNotIn(
            "Invalid renamed material",
            content
        )

        self.assert_ifc_visual(
            content,
            material
        )

        valid_visual = visual_input(
            color=[0.9, 0.8, 0.7],
            opacity=0.6,
            metallic=0.5,
            roughness=0.4
        )

        updated = self.update_material(
            material["id"],
            visual=valid_visual
        )

        self.assert_visual(
            updated["visual"],
            valid_visual
        )

        self.assert_properties(
            updated["properties"],
            material["properties"]
        )

        self.assert_visual_roundtrip(
            updated
        )

    def test_material_creation(self):
        """Verify creation of a simple editable IFC material."""

        initial_materials = self.list_materials()

        expected_visual = visual_input(
            color=[0.25, 0.5, 0.75],
            opacity=0.9,
            metallic=0.1,
            roughness=0.6
        )

        material = self.create_material(
            name="Created concrete",
            category="concrete",
            visual=expected_visual
        )

        self.assert_material(
            material,
            name="Created concrete",
            category="concrete",
            visual=expected_visual
        )

        self.assertEqual(
            material["definition"]["ifcClass"],
            "IfcMaterial"
        )

        self.assertIsNone(
            material["globalId"]
        )

        self.assertEqual(
            len(self.list_materials()),
            len(initial_materials) + 1
        )

        self.assertNotIn(
            material["id"],
            {
                summary["id"]
                for summary in initial_materials
            }
        )

        self.assertNotIn(
            material["ifcEntityId"],
            {
                summary["ifcEntityId"]
                for summary in initial_materials
            }
        )

        self.assertEqual(
            self.get_product()["materials"],
            []
        )

        self.assert_material_index_consistency()

        self.assert_visual_roundtrip(
            material
        )

    def test_material_listing(self):
        """Verify listing of all simple and complex IFC materials."""

        initial_materials = self.get_all_materials()

        self.assertEqual(
            len(initial_materials),
            6
        )

        expected_classes = {
            "IfcMaterial": 2,
            "IfcMaterialList": 1,
            "IfcMaterialLayer": 2,
            "IfcMaterialLayerSet": 1
        }

        actual_classes = {}

        for material in initial_materials:
            ifc_class = material[
                "definition"
            ]["ifcClass"]

            actual_classes[ifc_class] = (
                actual_classes.get(ifc_class, 0) + 1
            )

            self.assertIs(
                material["editable"],
                False
            )

        self.assertEqual(
            actual_classes,
            expected_classes
        )

        initial_ids = {
            material["id"]
            for material in initial_materials
        }

        created = []

        for index in range(5):
            material = self.create_material(
                name=f"Listed material {index}",
                category="test"
            )

            created.append(material)

        expected_ids = initial_ids | {
            material["id"]
            for material in created
        }

        summaries = self.list_materials()

        self.assertEqual(
            len(summaries),
            11
        )

        self.assertEqual(
            {
                summary["id"]
                for summary in summaries
            },
            expected_ids
        )

        self.assert_material_index_consistency()

        # Duplicate names must not merge distinct materials.

        duplicate_name = self.create_material(
            name=created[0]["name"],
            category="different category"
        )

        self.assertNotEqual(
            duplicate_name["id"],
            created[0]["id"]
        )

        self.assertNotEqual(
            duplicate_name["ifcEntityId"],
            created[0]["ifcEntityId"]
        )

        self.assertEqual(
            len(self.list_materials()),
            12
        )

        self.delete_material(
            duplicate_name["id"]
        )

        self.assertEqual(
            len(self.list_materials()),
            11
        )

        for material in initial_materials:
            self.assertEqual(
                self.get_material(material["id"]),
                material
            )

        for material in created:
            self.delete_material(
                material["id"]
            )

        self.assertEqual(
            {
                material["id"]
                for material in self.list_materials()
            },
            initial_ids
        )

        self.save_and_reload()

        self.assertEqual(
            len(self.list_materials()),
            6
        )

        self.assert_material_index_consistency()

    def test_material_get(self):
        """Verify the complete materialGet response structure."""

        native_concrete = self.find_material(
            "Native concrete",
            ifc_class="IfcMaterial"
        )

        self.assert_material(
            native_concrete,
            name="Native concrete",
            category="concrete",
            visual=DEFAULT_VISUAL,
            properties=[
                property_input(
                    "NativeReference",
                    "NATIVE-CONCRETE",
                    property_set="Pset_NativeMaterial"
                )
            ],
            editable=False
        )

        native_steel = self.find_material(
            "Native steel",
            ifc_class="IfcMaterial"
        )

        self.assert_material(
            native_steel,
            name="Native steel",
            category="metal",
            visual=DEFAULT_VISUAL,
            editable=False
        )

        expected_visual = visual_input(
            color=[0.1, 0.2, 0.3],
            opacity=0.4,
            metallic=0.5,
            roughness=0.6
        )

        expected_properties = [
            property_input(
                "Reference",
                "MATERIAL-001"
            ),
            property_input(
                "Density",
                2500.0,
                value_type="IfcReal",
                property_set="Pset_Physical"
            )
        ]

        created = self.create_material(
            name="Complete material",
            category="custom",
            visual=expected_visual,
            properties=expected_properties
        )

        self.assert_material(
            created,
            name="Complete material",
            category="custom",
            visual=expected_visual,
            properties=expected_properties
        )

        for material in self.get_all_materials():
            with self.subTest(
                material_id=material["id"]
            ):
                self.assert_material_structure(
                    material
                )

                if (
                    material["definition"]["ifcClass"]
                    != "IfcMaterial"
                ):
                    self.assertIs(
                        material["editable"],
                        False
                    )

                    self.assertEqual(
                        material["category"],
                        material["definition"]["ifcClass"]
                    )

                    self.assertEqual(
                        material["properties"],
                        []
                    )

                    self.assert_visual(
                        material["visual"],
                        DEFAULT_VISUAL
                    )

        self.client.expect_error(
            "materialGet",
            {"id": "material:999999999"},
            "ENTITY_NOT_FOUND"
        )

        self.assert_material_index_consistency()

    def test_material_rename(self):
        """Verify material renaming without changing its identity."""

        material = self.create_material(
            name="Original material name",
            category="metal",
            visual=visual_input(
                color=[0.75, 0.8, 0.85],
                metallic=0.9,
                roughness=0.25
            ),
            properties=[
                property_input(
                    "Reference",
                    "RENAME-001"
                )
            ]
        )

        self.assign_material(
            material["id"],
            self.product_id
        )

        names = [
            "Renamed material",
            "Material with spaces",
            "Material's quoted name",
            "Material with \"double quotes\"",
            "Matériau métallique",
            "Werkstoff — 材料",
            "Final material name"
        ]

        for name in names:
            with self.subTest(name=name):
                before = self.get_material(
                    material["id"]
                )

                updated = self.update_material(
                    material["id"],
                    name=name
                )

                self.assert_preserved_fields(
                    before,
                    updated,
                    {"name"}
                )

                self.assertEqual(
                    updated["name"],
                    name
                )

                self.assertIn(
                    material["id"],
                    self.get_product()["materials"]
                )

        final = self.get_material(
            material["id"]
        )

        content = self.save_model()

        self.assert_ifc_visual(
            content,
            final
        )

        self.assertNotIn(
            "Original material name",
            content
        )

        self.load_model(
            self.output_path
        )

        reloaded = self.find_material(
            names[-1]
        )

        self.assert_material(
            reloaded,
            name=names[-1],
            category=material["category"],
            visual=material["visual"],
            properties=material["properties"]
        )

        self.assertIn(
            reloaded["id"],
            self.get_product()["materials"]
        )

    def test_material_category(self):
        """Verify creation and modification of material categories."""

        categories = [
            "concrete",
            "metal",
            "wood",
            "glass",
            "plastic",
            "insulation",
            "ceramic",
            "composite",
            "custom category",
            "CustomCategory",
            ""
        ]

        material = self.create_material(
            name="Category material",
            category=categories[0]
        )

        self.assertEqual(
            material["category"],
            categories[0]
        )

        for category in categories:
            with self.subTest(category=category):
                before = self.get_material(
                    material["id"]
                )

                updated = self.update_material(
                    material["id"],
                    category=category
                )

                self.assert_preserved_fields(
                    before,
                    updated,
                    {"category"}
                )

                self.assertEqual(
                    updated["category"],
                    category
                )

                material = self.assert_visual_roundtrip(
                    updated
                )

                self.assertEqual(
                    material["category"],
                    category
                )

        invalid_categories = [
            None,
            42,
            0.5,
            True,
            False,
            [],
            {}
        ]

        before = self.get_material(
            material["id"]
        )

        index_before = self.list_materials()

        for category in invalid_categories:
            with self.subTest(
                invalid_category=category
            ):
                params = material_create_params()

                params["category"] = category

                self.client.expect_error(
                    "materialCreate",
                    params
                )

                self.assertEqual(
                    self.list_materials(),
                    index_before
                )

                self.client.expect_error(
                    "materialUpdate",
                    {
                        "id": material["id"],
                        "category": category
                    }
                )

                self.assertEqual(
                    self.get_material(material["id"]),
                    before
                )

        final = self.update_material(
            material["id"],
            category="final category"
        )

        self.assertEqual(
            final["category"],
            "final category"
        )

        self.assert_visual_roundtrip(
            final
        )

    def test_material_color(self):
        """Verify preservation of RGB color components."""

        colors = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 1.0],
            [1.0, 0.0, 1.0],
            [0.0, 0.0, 0.0],
            [1.0, 1.0, 1.0],
            [0.5, 0.5, 0.5],
            [0.125, 0.375, 0.875],
            [0.123456789, 0.987654321, 0.456789123]
        ]

        self.assert_visual_component_updates(
            "color",
            colors
        )

    def test_material_opacity(self):
        """Verify opacity and its conversion to IFC transparency."""

        values = [
            0.0,
            0.001,
            0.125,
            0.25,
            0.5,
            0.75,
            0.875,
            0.999,
            1.0,
            0.123456789
        ]

        self.assert_visual_component_updates(
            "opacity",
            values
        )

    def test_material_metallic(self):
        """Verify preservation of metallic visual values."""

        values = [
            0.0,
            0.001,
            0.125,
            0.25,
            0.5,
            0.75,
            0.875,
            0.999,
            1.0,
            0.876543219
        ]

        self.assert_visual_component_updates(
            "metallic",
            values
        )

    def test_material_roughness(self):
        """Verify preservation of surface roughness values."""

        values = [
            0.0,
            0.001,
            0.125,
            0.25,
            0.5,
            0.75,
            0.875,
            0.999,
            1.0,
            0.345678912
        ]

        self.assert_visual_component_updates(
            "roughness",
            values
        )

    def test_visual_boundaries(self):
        """Verify acceptance of inclusive visual boundaries."""

        cases = [
            visual_input(
                color=[0.0, 0.0, 0.0],
                opacity=0.0,
                metallic=0.0,
                roughness=0.0
            ),
            visual_input(
                color=[1.0, 1.0, 1.0],
                opacity=1.0,
                metallic=1.0,
                roughness=1.0
            ),
            visual_input(
                color=[0.0, 1.0, 0.0],
                opacity=1.0,
                metallic=0.0,
                roughness=1.0
            ),
            visual_input(
                color=[1.0, 0.0, 1.0],
                opacity=0.0,
                metallic=1.0,
                roughness=0.0
            ),
            visual_input(
                color=[0, 1, 0.5],
                opacity=1,
                metallic=0,
                roughness=1
            ),
            visual_input(
                color=[1e-9, 1.0 - 1e-9, 0.5],
                opacity=1e-9,
                metallic=1.0 - 1e-9,
                roughness=1e-9
            )
        ]

        created = []

        for index, visual in enumerate(cases):
            with self.subTest(visual=visual):
                material = self.create_material(
                    name=f"Visual boundary {index}",
                    category="boundary",
                    visual=visual
                )

                self.assert_material(
                    material,
                    name=f"Visual boundary {index}",
                    category="boundary",
                    visual=visual
                )

                created.append(material)

        target = created[0]

        for visual in cases:
            with self.subTest(update_visual=visual):
                updated = self.update_material(
                    target["id"],
                    visual=visual
                )

                self.assert_identity(
                    target,
                    updated
                )

                self.assert_visual(
                    updated["visual"],
                    visual
                )

        created[0] = self.get_material(
            target["id"]
        )

        content = self.save_model()

        for material in created:
            self.assert_ifc_visual(
                content,
                material
            )

        self.load_model(
            self.output_path
        )

        for material in created:
            reloaded = self.find_material(
                material["name"]
            )

            self.assert_visual(
                reloaded["visual"],
                material["visual"]
            )

        self.assert_material_index_consistency()

    def test_invalid_visual(self):
        """Verify rejection of invalid visual values and structures."""

        invalid_numbers = [
            -1.0,
            -0.000001,
            1.000001,
            2.0,
            100.0
        ]

        invalid_types = [
            None,
            "0.5",
            "",
            True,
            False,
            [],
            {},
            [0.5]
        ]

        cases = []

        for field in (
            "opacity",
            "metallic",
            "roughness"
        ):
            for value in invalid_numbers + invalid_types:
                visual = visual_input()
                visual[field] = value

                cases.append((
                    f"{field}={value!r}",
                    visual
                ))

        for index in range(3):
            for value in invalid_numbers + invalid_types:
                visual = visual_input()
                visual["color"][index] = value

                cases.append((
                    f"color[{index}]={value!r}",
                    visual
                ))

        for field in VISUAL_FIELDS:
            visual = visual_input()

            del visual[field]

            cases.append((
                f"Missing visual field {field}",
                visual
            ))

        cases.extend([
            (
                "Unexpected visual field",
                {
                    **visual_input(),
                    "emissive": [0.0, 0.0, 0.0]
                }
            ),
            (
                "Unexpected visual field casing",
                {
                    **visual_input(),
                    "Opacity": 0.5
                }
            ),
            (
                "Empty visual object",
                {}
            ),
            (
                "Null visual",
                None
            ),
            (
                "Array visual",
                []
            ),
            (
                "String visual",
                "default"
            ),
            (
                "Numeric visual",
                0.5
            ),
            (
                "Boolean visual",
                True
            )
        ])

        self.assert_invalid_visuals(
            cases
        )

    def test_invalid_color(self):
        """Verify rejection of malformed RGB color arrays."""

        invalid_colors = [
            [],
            [0.5],
            [0.5, 0.5],
            [0.5, 0.5, 0.5, 1.0],
            [0.5, 0.5, 0.5, 0.5, 0.5],
            [0.5] * 100,
            None,
            "#FFFFFF",
            "0.5,0.5,0.5",
            0.5,
            True,
            False,
            {
                "r": 0.5,
                "g": 0.5,
                "b": 0.5
            },
            [
                [0.5],
                [0.5],
                [0.5]
            ],
            [0.5, None, 0.5],
            [0.5, "0.5", 0.5],
            [True, 0.5, 0.5]
        ]

        cases = []

        for color in invalid_colors:
            visual = visual_input()

            visual["color"] = color

            cases.append((
                f"Invalid color {color!r}",
                visual
            ))

        self.assert_invalid_visuals(
            cases
        )

    def test_material_properties(self):
        """Verify creation, replacement and clearing of material properties."""

        initial_properties = [
            property_input(
                "Reference",
                "CONCRETE-001",
                property_set="Pset_Identity"
            ),
            property_input(
                "Description",
                "Structural concrete",
                value_type="IfcText",
                property_set="Pset_Identity"
            ),
            property_input(
                "Density",
                2500.0,
                value_type="IfcReal",
                property_set="Pset_Physical"
            ),
            property_input(
                "StrengthClass",
                "C30/37",
                property_set="Pset_Mechanical"
            ),
            property_input(
                "LayerCount",
                3,
                value_type="IfcInteger",
                property_set="Pset_Configuration"
            ),
            property_input(
                "Recyclable",
                True,
                value_type="IfcBoolean",
                property_set="Pset_Configuration"
            ),
            property_input(
                "ReferenceLength",
                1.25,
                value_type="IfcLengthMeasure",
                property_set="Pset_Physical",
                unit="METRE"
            )
        ]

        visual = visual_input(
            color=[0.3, 0.4, 0.5],
            opacity=0.85,
            metallic=0.15,
            roughness=0.65
        )

        material = self.create_material(
            name="Material with properties",
            category="concrete",
            visual=visual,
            properties=initial_properties
        )

        self.assert_material(
            material,
            name="Material with properties",
            category="concrete",
            visual=visual,
            properties=initial_properties
        )

        replacement = [
            property_input(
                "Reference",
                "CONCRETE-002",
                property_set="Pset_Identity"
            ),
            property_input(
                "Density",
                2400.0,
                value_type="IfcReal",
                property_set="Pset_Physical"
            ),
            property_input(
                "ThermalConductivity",
                1.8,
                value_type="IfcReal",
                property_set="Pset_Thermal"
            ),
            property_input(
                "Recyclable",
                False,
                value_type="IfcBoolean",
                property_set="Pset_Configuration"
            )
        ]

        updated = self.update_material(
            material["id"],
            properties=replacement
        )

        self.assert_preserved_fields(
            material,
            updated,
            {"properties"}
        )

        self.assert_properties(
            updated["properties"],
            replacement
        )

        self.assertNotIn(
            "Pset_Mechanical",
            {
                property_value["propertySet"]
                for property_value in updated["properties"]
            }
        )

        self.assertNotIn(
            "Description",
            {
                property_value["name"]
                for property_value in updated["properties"]
            }
        )

        repeated = self.update_material(
            material["id"],
            properties=replacement
        )

        self.assert_properties(
            repeated["properties"],
            replacement
        )

        reloaded = self.assert_visual_roundtrip(
            repeated
        )

        self.assert_properties(
            reloaded["properties"],
            replacement
        )

        invalid_properties = [
            None,
            {},
            "invalid properties",
            [
                {
                    **property_input(
                        "InvalidSource",
                        "value"
                    ),
                    "source": "occurrence"
                }
            ],
            [
                property_input(
                    "Duplicate",
                    "First value"
                ),
                property_input(
                    "Duplicate",
                    "Second value"
                )
            ],
            [
                {
                    "propertySet": "Pset_Invalid",
                    "name": "MissingValue"
                }
            ]
        ]

        before = self.get_material(
            reloaded["id"]
        )

        for properties in invalid_properties:
            with self.subTest(
                invalid_properties=properties
            ):
                self.client.expect_error(
                    "materialUpdate",
                    {
                        "id": reloaded["id"],
                        "properties": properties
                    }
                )

                self.assertEqual(
                    self.get_material(reloaded["id"]),
                    before
                )

        cleared = self.update_material(
            reloaded["id"],
            properties=[]
        )

        self.assert_preserved_fields(
            before,
            cleared,
            {"properties"}
        )

        self.assertEqual(
            cleared["properties"],
            []
        )

        self.assert_visual(
            cleared["visual"],
            visual
        )

        content = self.save_model()

        for removed_value in (
            "CONCRETE-001",
            "CONCRETE-002",
            "Structural concrete",
            "StrengthClass",
            "LayerCount",
            "ThermalConductivity"
        ):
            self.assertNotIn(
                removed_value,
                content
            )

        self.assert_ifc_visual(
            content,
            cleared
        )

        self.load_model(
            self.output_path
        )

        reloaded = self.find_material(
            cleared["name"]
        )

        self.assertEqual(
            reloaded["properties"],
            []
        )

        self.assertIs(
            reloaded["editable"],
            True
        )

        restored = self.update_material(
            reloaded["id"],
            properties=initial_properties
        )

        self.assert_properties(
            restored["properties"],
            initial_properties
        )

        self.assert_visual_roundtrip(
            restored
        )

    def test_material_partial_update(self):
        """Verify preservation of material fields omitted from updates."""

        initial_properties = [
            property_input(
                "Reference",
                "PARTIAL-001"
            ),
            property_input(
                "Density",
                7800.0,
                value_type="IfcReal",
                property_set="Pset_Physical"
            )
        ]

        material = self.create_material(
            name="Partial update material",
            category="metal",
            visual=visual_input(
                color=[0.2, 0.3, 0.4],
                opacity=0.7,
                metallic=0.9,
                roughness=0.2
            ),
            properties=initial_properties
        )

        self.assign_material(
            material["id"],
            self.product_id
        )

        replacement_visual = visual_input(
            color=[0.8, 0.7, 0.6],
            opacity=0.9,
            metallic=0.75,
            roughness=0.35
        )

        replacement_properties = [
            property_input(
                "Reference",
                "PARTIAL-002"
            ),
            property_input(
                "Conductivity",
                45.5,
                value_type="IfcReal",
                property_set="Pset_Thermal"
            )
        ]

        updates = [
            {
                "name": "Renamed partial material"
            },
            {
                "category": "alloy"
            },
            {
                "visual": replacement_visual
            },
            {
                "properties": replacement_properties
            }
        ]

        for changes in updates:
            with self.subTest(changes=changes):
                before = self.get_material(
                    material["id"]
                )

                updated = self.update_material(
                    material["id"],
                    **changes
                )

                self.assert_preserved_fields(
                    before,
                    updated,
                    set(changes)
                )

                for field, expected in changes.items():
                    if field == "visual":
                        self.assert_visual(
                            updated[field],
                            expected
                        )

                    elif field == "properties":
                        self.assert_properties(
                            updated[field],
                            expected
                        )

                    else:
                        self.assertEqual(
                            updated[field],
                            expected
                        )

                self.assertEqual(
                    self.get_product()["materials"],
                    [material["id"]]
                )

        final = self.get_material(
            material["id"]
        )

        self.assert_material(
            final,
            name="Renamed partial material",
            category="alloy",
            visual=replacement_visual,
            properties=replacement_properties
        )

        invalid_updates = [
            {},
            {"editable": False},
            {"globalId": "invalid"},
            {"ifcEntityId": 42},
            {"definition": {}},
            {"name": None},
            {"category": None},
            {"visual": {"opacity": 0.5}},
            {"properties": None}
        ]

        for changes in invalid_updates:
            with self.subTest(
                invalid_update=changes
            ):
                self.client.expect_error(
                    "materialUpdate",
                    {
                        "id": material["id"],
                        **changes
                    }
                )

                self.assertEqual(
                    self.get_material(material["id"]),
                    final
                )

        reloaded = self.assert_visual_roundtrip(
            final
        )

        self.assertEqual(
            self.get_product()["materials"],
            [reloaded["id"]]
        )

    def test_material_readonly(self):
        """Verify rejection of modifications to native and complex materials."""

        readonly_materials = [
            material
            for material in self.get_all_materials()
            if not material["editable"]
        ]

        self.assertEqual(
            len(readonly_materials),
            6
        )

        self.assertEqual(
            {
                material["definition"]["ifcClass"]
                for material in readonly_materials
            },
            {
                "IfcMaterial",
                "IfcMaterialList",
                "IfcMaterialLayer",
                "IfcMaterialLayerSet"
            }
        )

        initial = self.snapshot_materials()

        invalid_updates = [
            {
                "name": "Invalid readonly name"
            },
            {
                "category": "invalid readonly category"
            },
            {
                "visual": visual_input(
                    color=[1.0, 0.0, 0.0],
                    opacity=0.5,
                    metallic=1.0,
                    roughness=0.0
                )
            },
            {
                "properties": [
                    property_input(
                        "InvalidReadonlyProperty",
                        "Invalid value"
                    )
                ]
            },
            {
                "properties": []
            },
            {
                "name": "Invalid combined update",
                "category": "invalid",
                "visual": visual_input(),
                "properties": []
            }
        ]

        for material in readonly_materials:
            for changes in invalid_updates:
                with self.subTest(
                    material=material["name"],
                    ifc_class=material["definition"]["ifcClass"],
                    changes=changes
                ):
                    self.client.expect_error(
                        "materialUpdate",
                        {
                            "id": material["id"],
                            **changes
                        },
                        "MATERIAL_NOT_EDITABLE"
                    )

                    self.assertEqual(
                        self.get_material(material["id"]),
                        material
                    )

                    self.assertEqual(
                        self.snapshot_materials(),
                        initial
                    )

        original_signatures = sorted(
            self.material_signature(material)
            for material in readonly_materials
        )

        self.save_and_reload()

        reloaded_materials = self.get_all_materials()

        self.assertEqual(
            sorted(
                self.material_signature(material)
                for material in reloaded_materials
            ),
            original_signatures
        )

        for material in reloaded_materials:
            self.assertIs(
                material["editable"],
                False
            )

        concrete = self.find_material(
            "Native concrete",
            ifc_class="IfcMaterial"
        )

        self.assert_properties(
            concrete["properties"],
            [
                property_input(
                    "NativeReference",
                    "NATIVE-CONCRETE",
                    property_set="Pset_NativeMaterial"
                )
            ]
        )

        content = self.save_model()

        self.assertNotIn(
            "Invalid readonly name",
            content
        )

        self.assertNotIn(
            "InvalidReadonlyProperty",
            content
        )

        self.assertIn(
            "IFCMATERIALLAYERSET(",
            content.upper()
        )

        self.assertIn(
            "IFCMATERIALLIST(",
            content.upper()
        )

        editable = self.create_material(
            name="Editable recovery material",
            category="test"
        )

        updated = self.update_material(
            editable["id"],
            name="Successfully edited recovery material"
        )

        self.assertEqual(
            updated["name"],
            "Successfully edited recovery material"
        )

        self.assertIs(
            updated["editable"],
            True
        )

    def test_material_deletion(self):
        """Verify deletion of a simple material and its references."""

        initial_materials = self.get_all_materials()

        initial_signatures = sorted(
            self.material_signature(material)
            for material in initial_materials
        )

        deleted = self.create_material(
            name="Material to delete",
            category="deleted category",
            visual=visual_input(
                color=[0.1, 0.2, 0.3],
                opacity=0.4,
                metallic=0.5,
                roughness=0.6
            ),
            properties=[
                property_input(
                    "DeletedReference",
                    "DELETED-MATERIAL-VALUE",
                    property_set="Pset_DeletedMaterial"
                )
            ]
        )

        preserved = self.create_material(
            name="Preserved material",
            category="preserved",
            visual=visual_input(
                color=[0.9, 0.8, 0.7],
                opacity=0.6,
                metallic=0.5,
                roughness=0.4
            ),
            properties=[
                property_input(
                    "PreservedReference",
                    "PRESERVED-MATERIAL-VALUE"
                )
            ]
        )

        self.assign_material(
            deleted["id"],
            self.product_id
        )

        self.assertIn(
            deleted["id"],
            self.get_product()["materials"]
        )

        self.assertEqual(
            len(self.list_materials()),
            len(initial_materials) + 2
        )

        self.delete_material(
            deleted["id"]
        )

        self.assertNotIn(
            deleted["id"],
            self.get_product()["materials"]
        )

        self.assertEqual(
            self.get_material(preserved["id"]),
            preserved
        )

        self.assertEqual(
            len(self.list_materials()),
            len(initial_materials) + 1
        )

        for material in initial_materials:
            self.assertEqual(
                self.get_material(material["id"]),
                material
            )

        self.client.expect_error(
            "materialDelete",
            {"id": deleted["id"]},
            "ENTITY_NOT_FOUND"
        )

        self.client.expect_error(
            "materialUpdate",
            {
                "id": deleted["id"],
                "name": "Deleted material update"
            },
            "ENTITY_NOT_FOUND"
        )

        self.client.expect_error(
            "materialAssign",
            {
                "materialId": deleted["id"],
                "targetId": self.product_id
            },
            "ENTITY_NOT_FOUND"
        )

        content = self.save_model()

        for removed_value in (
            "Material to delete",
            "deleted category",
            "DeletedReference",
            "DELETED-MATERIAL-VALUE",
            "Pset_DeletedMaterial"
        ):
            self.assertNotIn(
                removed_value,
                content
            )

        self.assert_ifc_visual(
            content,
            preserved
        )

        self.load_model(
            self.output_path
        )

        self.assertEqual(
            self.get_product()["materials"],
            []
        )

        self.assertEqual(
            len(self.list_materials()),
            len(initial_materials) + 1
        )

        self.assertNotIn(
            "Material to delete",
            {
                material["name"]
                for material in self.get_all_materials()
            }
        )

        reloaded = self.find_material(
            preserved["name"]
        )

        self.assert_material(
            reloaded,
            name=preserved["name"],
            category=preserved["category"],
            visual=preserved["visual"],
            properties=preserved["properties"]
        )

        self.delete_material(
            reloaded["id"]
        )

        self.save_and_reload()

        self.assertEqual(
            sorted(
                self.material_signature(material)
                for material in self.get_all_materials()
            ),
            initial_signatures
        )

        self.assertEqual(
            len(self.list_materials()),
            6
        )

        self.assert_material_index_consistency()

    def test_material_persistence(self):
        """Verify material characteristics after repeated IFC roundtrips."""

        specifications = [
            {
                "name": "Persistent concrete",
                "category": "concrete",
                "visual": visual_input(
                    color=[0.6, 0.6, 0.6],
                    opacity=1.0,
                    metallic=0.0,
                    roughness=0.9
                ),
                "properties": [
                    property_input(
                        "Reference",
                        "CONCRETE-001",
                        property_set="Pset_Identity"
                    ),
                    property_input(
                        "Density",
                        2500.0,
                        value_type="IfcReal",
                        property_set="Pset_Physical"
                    ),
                    property_input(
                        "CompressiveStrength",
                        30.0,
                        value_type="IfcReal",
                        property_set="Pset_Mechanical"
                    )
                ]
            },
            {
                "name": "Persistent steel",
                "category": "metal",
                "visual": visual_input(
                    color=[0.75, 0.8, 0.85],
                    opacity=1.0,
                    metallic=1.0,
                    roughness=0.2
                ),
                "properties": [
                    property_input(
                        "Reference",
                        "STEEL-001",
                        property_set="Pset_Identity"
                    ),
                    property_input(
                        "Density",
                        7850.0,
                        value_type="IfcReal",
                        property_set="Pset_Physical"
                    ),
                    property_input(
                        "Recyclable",
                        True,
                        value_type="IfcBoolean"
                    )
                ]
            },
            {
                "name": "Persistent glass",
                "category": "glass",
                "visual": visual_input(
                    color=[0.8, 0.9, 1.0],
                    opacity=0.25,
                    metallic=0.0,
                    roughness=0.05
                ),
                "properties": [
                    property_input(
                        "Reference",
                        "GLASS-001",
                        property_set="Pset_Identity"
                    ),
                    property_input(
                        "Thickness",
                        0.012,
                        value_type="IfcLengthMeasure",
                        property_set="Pset_Physical",
                        unit="METRE"
                    ),
                    property_input(
                        "LayerCount",
                        2,
                        value_type="IfcInteger"
                    )
                ]
            },
            {
                "name": "Persistent insulation",
                "category": "insulation",
                "visual": visual_input(
                    color=[0.95, 0.8, 0.2],
                    opacity=1.0,
                    metallic=0.0,
                    roughness=1.0
                ),
                "properties": [
                    property_input(
                        "ThermalConductivity",
                        0.035,
                        value_type="IfcReal",
                        property_set="Pset_Thermal"
                    ),
                    property_input(
                        "Description",
                        "Isolant thermique — Dämmstoff — 保温材料",
                        value_type="IfcText"
                    )
                ]
            },
            {
                "name": "Persistent precision material",
                "category": "custom",
                "visual": visual_input(
                    color=[
                        0.123456789,
                        0.987654321,
                        0.456789123
                    ],
                    opacity=0.765432198,
                    metallic=0.234567891,
                    roughness=0.345678912
                ),
                "properties": [
                    property_input(
                        "Coefficient",
                        0.123456789,
                        value_type="IfcReal"
                    ),
                    property_input(
                        "Enabled",
                        False,
                        value_type="IfcBoolean"
                    ),
                    property_input(
                        "Reference",
                        "PRECISION-001",
                        value_type="IfcIdentifier"
                    )
                ]
            }
        ]

        initial_count = len(
            self.list_materials()
        )

        created = {}

        for specification in specifications:
            material = self.create_material(
                **specification
            )

            self.assert_material(
                material,
                **specification
            )

            created[material["name"]] = material

        self.assign_material(
            created["Persistent steel"]["id"],
            self.product_id
        )

        expected_count = (
            initial_count + len(specifications)
        )

        self.assertEqual(
            len(self.list_materials()),
            expected_count
        )

        expected = {
            specification["name"]: copy.deepcopy(
                specification
            )
            for specification in specifications
        }

        for iteration in range(10):
            with self.subTest(
                roundtrip=iteration
            ):
                content = self.save_model()

                for name in expected:
                    material = self.find_material(
                        name
                    )

                    self.assert_ifc_visual(
                        content,
                        material
                    )

                result = self.load_model(
                    self.output_path
                )

                self.assertEqual(
                    result["materialCount"],
                    expected_count
                )

                self.assertEqual(
                    len(self.list_materials()),
                    expected_count
                )

                for name, specification in expected.items():
                    reloaded = self.find_material(
                        name
                    )

                    self.assert_material(
                        reloaded,
                        **specification
                    )

                    self.assertIs(
                        reloaded["editable"],
                        True
                    )

                    self.assertIsNone(
                        reloaded["globalId"]
                    )

                steel = self.find_material(
                    "Persistent steel"
                )

                self.assertEqual(
                    self.get_product()["materials"],
                    [steel["id"]]
                )

                self.assert_material_index_consistency()

        precision = self.find_material(
            "Persistent precision material"
        )

        replacement = {
            "name": "Updated persistent precision material",
            "category": "updated custom",
            "visual": visual_input(
                color=[
                    0.876543219,
                    0.234567891,
                    0.654321987
                ],
                opacity=0.125,
                metallic=0.875,
                roughness=0.375
            ),
            "properties": [
                property_input(
                    "Reference",
                    "PRECISION-002",
                    value_type="IfcIdentifier"
                ),
                property_input(
                    "Coefficient",
                    0.987654321,
                    value_type="IfcReal"
                ),
                property_input(
                    "Enabled",
                    True,
                    value_type="IfcBoolean"
                ),
                property_input(
                    "AdditionalLength",
                    12.3456789,
                    value_type="IfcLengthMeasure",
                    unit="METRE"
                )
            ]
        }

        updated = self.update_material(
            precision["id"],
            **replacement
        )

        self.assert_identity(
            precision,
            updated
        )

        self.assert_material(
            updated,
            **replacement
        )

        del expected["Persistent precision material"]

        expected[replacement["name"]] = replacement

        for iteration in range(5):
            with self.subTest(
                updated_roundtrip=iteration
            ):
                self.save_and_reload()

                self.assertEqual(
                    len(self.list_materials()),
                    expected_count
                )

                for name, specification in expected.items():
                    material = self.find_material(
                        name
                    )

                    self.assert_material(
                        material,
                        **specification
                    )

        final_content = self.save_model()

        self.assertNotIn(
            "PRECISION-001",
            final_content
        )

        self.assertNotIn(
            "'Persistent precision material'",
            final_content
        )

        self.assertIn(
            "PRECISION-002",
            final_content
        )

        for name in expected:
            material = self.find_material(
                name
            )

            self.assert_ifc_visual(
                final_content,
                material
            )

        final_material = self.find_material(
            replacement["name"]
        )

        self.assert_material(
            final_material,
            **replacement
        )

        readonly_materials = [
            material
            for material in self.get_all_materials()
            if not material["editable"]
        ]

        self.assertEqual(
            len(readonly_materials),
            initial_count
        )

        steel = self.find_material(
            "Persistent steel"
        )

        self.assertEqual(
            self.get_product()["materials"],
            [steel["id"]]
        )

        self.assert_material_index_consistency()


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