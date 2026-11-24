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
# Shared property sets and incompatible IFC value types are
# intentionally tested as regression cases, not assumed to work.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30
NUMERIC_TOLERANCE = 1e-9

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

PROJECT_GLOBAL_ID = "0000000000000000000001"
FIRST_PRODUCT_GLOBAL_ID = "0000000000000000000002"
SECOND_PRODUCT_GLOBAL_ID = "0000000000000000000003"
READONLY_PRODUCT_GLOBAL_ID = "0000000000000000000004"

SHARED_PROPERTY_SET_GLOBAL_ID = "0000000000000000000005"
READONLY_PROPERTY_SET_GLOBAL_ID = "0000000000000000000006"

PROPERTY_FIELDS = {
    "propertySet",
    "name",
    "valueType",
    "value",
    "unit",
    "source"
}

ENTITY_SOURCES = {
    "transform": "occurrence",
    "material": "material"
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
FILE_NAME('properties-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCDIRECTION((0.,0.,1.));
#3=IFCDIRECTION((1.,0.,0.));
#4=IFCAXIS2PLACEMENT3D(#1,#2,#3);
#5=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-5,#4,$);
#6=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#7=IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.);
#8=IFCSIUNIT(*,.VOLUMEUNIT.,$,.CUBIC_METRE.);
#9=IFCSIUNIT(*,.MASSUNIT.,.KILO.,.GRAM.);
#10=IFCSIUNIT(*,.TIMEUNIT.,$,.SECOND.);
#11=IFCSIUNIT(*,.THERMODYNAMICTEMPERATUREUNIT.,$,.KELVIN.);
#12=IFCUNITASSIGNMENT((#6,#7,#8,#9,#10,#11));
#13=IFCPROJECT('0000000000000000000001',$,'Properties test project',$,$,$,$,(#5),#12);
#14=IFCLOCALPLACEMENT($,#4);
#15=IFCLOCALPLACEMENT($,#4);
#16=IFCLOCALPLACEMENT($,#4);
#17=IFCBUILDINGELEMENTPROXY('0000000000000000000002',$,'First product',$,$,#14,$,$,.NOTDEFINED.);
#18=IFCBUILDINGELEMENTPROXY('0000000000000000000003',$,'Second product',$,$,#15,$,$,.NOTDEFINED.);
#19=IFCBUILDINGELEMENTPROXY('0000000000000000000004',$,'Readonly properties product',$,$,#16,$,$,.NOTDEFINED.);
#20=IFCPROPERTYSINGLEVALUE('SharedReference',$,IFCLABEL('Original shared value'),$);
#21=IFCPROPERTYSINGLEVALUE('SharedCount',$,IFCINTEGER(42),$);
#22=IFCPROPERTYSET('0000000000000000000005',$,'Pset_Shared',$,(#20,#21));
#23=IFCRELDEFINESBYPROPERTIES('0000000000000000000007',$,$,$,(#17),#22);
#24=IFCRELDEFINESBYPROPERTIES('0000000000000000000008',$,$,$,(#18),#22);
#25=IFCPROPERTYSINGLEVALUE('EditableReference',$,IFCLABEL('Original editable value'),$);
#26=IFCPROPERTYLISTVALUE('ReadonlyList',$,(IFCLABEL('Alpha'),IFCLABEL('Beta')),$);
#27=IFCPROPERTYBOUNDEDVALUE('ReadonlyBounds',$,IFCREAL(10.),IFCREAL(0.),$,IFCREAL(5.));
#28=IFCPROPERTYSINGLEVALUE('NestedValue',$,IFCINTEGER(123),$);
#29=IFCCOMPLEXPROPERTY('ReadonlyComplex',$,'Complex usage',(#28));
#30=IFCPROPERTYSET('0000000000000000000006',$,'Pset_Mixed',$,(#25,#26,#27,#29));
#31=IFCRELDEFINESBYPROPERTIES('0000000000000000000009',$,$,$,(#19),#30);
#32=IFCPROPERTYSINGLEVALUE('TypeReference',$,IFCLABEL('Inherited type value'),$);
#33=IFCPROPERTYSET('0000000000000000000010',$,'Pset_Type',$,(#32));
#34=IFCBUILDINGELEMENTPROXYTYPE('0000000000000000000011',$,'Native product type',$,$,(#33),$,$,$,.NOTDEFINED.);
#35=IFCRELDEFINESBYTYPE('0000000000000000000012',$,$,$,(#19),#34);
#36=IFCQUANTITYLENGTH('NativeLength',$,$,12.5,$);
#37=IFCELEMENTQUANTITY('0000000000000000000013',$,'Qto_Native',$,$,(#36));
#38=IFCRELDEFINESBYPROPERTIES('0000000000000000000014',$,$,$,(#19),#37);
ENDSEC;
END-ISO-10303-21;
"""


def property_input(
    name,
    value,
    value_type="IfcLabel",
    property_set="Pset_Test",
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


def with_source(properties, source):
    return [
        {
            **property_value,
            "source": source
        }
        for property_value in properties
    ]


def property_key(property_value):
    return (
        property_value["propertySet"],
        property_value["name"],
        property_value["source"]
    )


def transform_create_params(
    name="Property test transform",
    properties=None
):
    return {
        "name": name,
        "parents": {
            "spatial": None,
            "placement": None,
            "decomposition": None
        },
        "transform": {
            "space": "parent",
            "matrix": IDENTITY_MATRIX.copy()
        },
        "properties": (
            []
            if properties is None
            else properties
        )
    }


def material_create_params(
    name="Property test material",
    properties=None
):
    return {
        "name": name,
        "category": "test",
        "visual": {
            "color": [0.5, 0.5, 0.5],
            "opacity": 1.0,
            "metallic": 0.0,
            "roughness": 0.5
        },
        "properties": (
            []
            if properties is None
            else properties
        )
    }


def parse_step_entities(content):
    # This targeted parser is limited to the controlled test fixture.
    # It is not a general-purpose STEP or IFC parser.

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
    # Remove STEP string literals before extracting entity references.

    without_strings = re.sub(
        r"'(?:[^']|'')*'",
        "''",
        arguments
    )

    return {
        int(identifier)
        for identifier in re.findall(
            r"#(\d+)",
            without_strings
        )
    }


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


class PropertyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-properties-"
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

        self.assertEqual(
            result["path"],
            str(path)
        )

        self.assertEqual(
            result["schema"].upper(),
            "IFC4"
        )

        self.first_product_id = self.find_transform(
            FIRST_PRODUCT_GLOBAL_ID
        )["id"]

        self.second_product_id = self.find_transform(
            SECOND_PRODUCT_GLOBAL_ID
        )["id"]

        self.readonly_product_id = self.find_transform(
            READONLY_PRODUCT_GLOBAL_ID
        )["id"]

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

    def list_transforms(self):
        return self.client.request(
            "transformGetAll"
        )

    def list_materials(self):
        return self.client.request(
            "materialGetAll"
        )

    def get_entity(self, category, identifier):
        return self.client.request(
            f"{category}Get",
            {"id": identifier}
        )

    def get_transform(self, identifier):
        return self.get_entity(
            "transform",
            identifier
        )

    def get_material(self, identifier):
        return self.get_entity(
            "material",
            identifier
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

        return self.get_transform(
            matches[0]["id"]
        )

    def find_material(self, name):
        matches = []

        for summary in self.list_materials():
            material = self.get_material(
                summary["id"]
            )

            if material["name"] == name:
                matches.append(material)

        self.assertEqual(
            len(matches),
            1,
            f"Expected one material named {name}"
        )

        return matches[0]

    def refind_entity(self, category, entity):
        if category == "transform":
            return self.find_transform(
                entity["globalId"]
            )

        return self.find_material(
            entity["name"]
        )

    def create_entity_params(
        self,
        category,
        name,
        properties
    ):
        if category == "transform":
            return transform_create_params(
                name=name,
                properties=properties
            )

        return material_create_params(
            name=name,
            properties=properties
        )

    def create_entity(
        self,
        category,
        name,
        properties=None
    ):
        source = ENTITY_SOURCES[category]

        properties = with_source(
            [] if properties is None else properties,
            source
        )

        return self.client.request(
            f"{category}Create",
            self.create_entity_params(
                category,
                name,
                properties
            )
        )

    def update_properties(
        self,
        category,
        identifier,
        properties
    ):
        source = ENTITY_SOURCES[category]

        return self.client.request(
            f"{category}Update",
            {
                "id": identifier,
                "properties": with_source(
                    properties,
                    source
                )
            }
        )

    def list_entities(self, category):
        if category == "transform":
            return self.list_transforms()

        return self.list_materials()

    def get_properties(self, category, identifier):
        entity = self.get_entity(
            category,
            identifier
        )

        return entity["properties"]

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

    def assert_property(self, actual, expected):
        self.assertIsInstance(
            actual,
            dict
        )

        self.assertEqual(
            set(actual),
            PROPERTY_FIELDS
        )

        for field in (
            "propertySet",
            "name",
            "valueType",
            "source"
        ):
            self.assertIsInstance(
                actual[field],
                str
            )

            self.assertTrue(
                actual[field]
            )

        for field in PROPERTY_FIELDS - {"value"}:
            self.assertEqual(
                actual[field],
                expected[field],
                f"Invalid property field: {field}"
            )

        expected_value = expected["value"]
        actual_value = actual["value"]

        if expected_value is None:
            self.assertIsNone(
                actual_value
            )

        elif type(expected_value) is bool:
            self.assertIs(
                type(actual_value),
                bool
            )

            self.assertIs(
                actual_value,
                expected_value
            )

        elif type(expected_value) is int:
            self.assertIs(
                type(actual_value),
                int
            )

            self.assertEqual(
                actual_value,
                expected_value
            )

        elif type(expected_value) is float:
            self.assertIn(
                type(actual_value),
                (int, float)
            )

            self.assertTrue(
                math.isfinite(actual_value)
            )

            self.assertTrue(
                math.isclose(
                    actual_value,
                    expected_value,
                    rel_tol=NUMERIC_TOLERANCE,
                    abs_tol=NUMERIC_TOLERANCE
                ),
                f"{actual_value} != {expected_value}"
            )

        elif isinstance(expected_value, str):
            self.assertIsInstance(
                actual_value,
                str
            )

            self.assertEqual(
                actual_value,
                expected_value
            )

        else:
            self.fail(
                f"Unsupported expected property value: {expected_value}"
            )

    def assert_properties(
        self,
        actual,
        expected
    ):
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
            "Duplicate properties returned by the CLI"
        )

        self.assertEqual(
            len(expected),
            len(expected_index),
            "Duplicate expected properties"
        )

        self.assertEqual(
            set(actual_index),
            set(expected_index),
            "Unexpected property keys"
        )

        for key, property_value in expected_index.items():
            with self.subTest(property=key):
                self.assert_property(
                    actual_index[key],
                    property_value
                )

    def assert_entity_properties(
        self,
        category,
        entity,
        properties
    ):
        expected = with_source(
            properties,
            ENTITY_SOURCES[category]
        )

        self.assert_properties(
            entity["properties"],
            expected
        )

        retrieved = self.get_entity(
            category,
            entity["id"]
        )

        self.assert_identity(
            entity,
            retrieved
        )

        self.assert_properties(
            retrieved["properties"],
            expected
        )

    def assert_properties_roundtrip(
        self,
        properties,
        name_prefix
    ):
        created = {}

        for category in ENTITY_SOURCES:
            with self.subTest(category=category):
                entity = self.create_entity(
                    category,
                    name=f"{name_prefix} {category}",
                    properties=properties
                )

                self.assert_entity_properties(
                    category,
                    entity,
                    properties
                )

                created[category] = entity

        self.save_and_reload()

        for category, entity in created.items():
            with self.subTest(
                category=category,
                phase="reload"
            ):
                reloaded = self.refind_entity(
                    category,
                    entity
                )

                self.assert_entity_properties(
                    category,
                    reloaded,
                    properties
                )

                if entity["globalId"] is not None:
                    self.assertEqual(
                        reloaded["globalId"],
                        entity["globalId"]
                    )

        return created

    def find_step_entity_by_global_id(
        self,
        entities,
        global_id
    ):
        matches = [
            identifier
            for identifier, entity in entities.items()
            if re.match(
                rf"\s*'{re.escape(global_id)}'\s*,",
                entity["arguments"]
            )
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one STEP entity with GlobalId {global_id}"
        )

        return matches[0]

    def find_step_property(
        self,
        entities,
        class_name,
        name
    ):
        matches = [
            identifier
            for identifier, entity in entities.items()
            if (
                entity["class"] == class_name.upper()
                and re.match(
                    rf"\s*'{re.escape(name)}'\s*,",
                    entity["arguments"]
                )
            )
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one {class_name} named {name}"
        )

        return matches[0]

    def assert_step_property_set_link(
        self,
        entities,
        product_id,
        property_set_id
    ):
        matching_relations = []

        for identifier, entity in entities.items():
            if entity["class"] != "IFCRELDEFINESBYPROPERTIES":
                continue

            references = step_references(
                entity["arguments"]
            )

            if (
                product_id in references
                and property_set_id in references
            ):
                matching_relations.append(
                    identifier
                )

        self.assertEqual(
            len(matching_relations),
            1,
            "Invalid product-to-property-set relationship"
        )

    def readonly_properties(self):
        entity = self.get_transform(
            self.readonly_product_id
        )

        return [
            property_value
            for property_value in entity["properties"]
            if property_value["source"] != "occurrence"
        ]

    def assert_readonly_step_entities(
        self,
        content
    ):
        entities = parse_step_entities(content)

        product_id = self.find_step_entity_by_global_id(
            entities,
            READONLY_PRODUCT_GLOBAL_ID
        )

        property_set_id = self.find_step_entity_by_global_id(
            entities,
            READONLY_PROPERTY_SET_GLOBAL_ID
        )

        self.assertEqual(
            entities[property_set_id]["class"],
            "IFCPROPERTYSET"
        )

        readonly_list_id = self.find_step_property(
            entities,
            "IFCPROPERTYLISTVALUE",
            "ReadonlyList"
        )

        readonly_bounds_id = self.find_step_property(
            entities,
            "IFCPROPERTYBOUNDEDVALUE",
            "ReadonlyBounds"
        )

        readonly_complex_id = self.find_step_property(
            entities,
            "IFCCOMPLEXPROPERTY",
            "ReadonlyComplex"
        )

        nested_value_id = self.find_step_property(
            entities,
            "IFCPROPERTYSINGLEVALUE",
            "NestedValue"
        )

        property_set_references = step_references(
            entities[property_set_id]["arguments"]
        )

        self.assertTrue(
            {
                readonly_list_id,
                readonly_bounds_id,
                readonly_complex_id
            }.issubset(
                property_set_references
            )
        )

        list_arguments = entities[
            readonly_list_id
        ]["arguments"]

        self.assertIn(
            "IFCLABEL('Alpha')",
            list_arguments
        )

        self.assertIn(
            "IFCLABEL('Beta')",
            list_arguments
        )

        bounded_arguments = entities[
            readonly_bounds_id
        ]["arguments"]

        bounded_values = [
            float(value)
            for value in re.findall(
                r"IFCREAL\(([^)]+)\)",
                bounded_arguments,
                flags=re.IGNORECASE
            )
        ]

        self.assertEqual(
            bounded_values,
            [10.0, 0.0, 5.0]
        )

        complex_arguments = entities[
            readonly_complex_id
        ]["arguments"]

        self.assertIn(
            "'Complex usage'",
            complex_arguments
        )

        self.assertIn(
            nested_value_id,
            step_references(complex_arguments)
        )

        self.assertRegex(
            entities[nested_value_id]["arguments"],
            r"IFCINTEGER\(\s*123\s*\)"
        )

        self.assert_step_property_set_link(
            entities,
            product_id,
            property_set_id
        )

        type_id = self.find_step_entity_by_global_id(
            entities,
            "0000000000000000000011"
        )

        type_set_id = self.find_step_entity_by_global_id(
            entities,
            "0000000000000000000010"
        )

        quantity_set_id = self.find_step_entity_by_global_id(
            entities,
            "0000000000000000000013"
        )

        self.assertIn(
            type_set_id,
            step_references(
                entities[type_id]["arguments"]
            )
        )

        type_relations = [
            entity
            for entity in entities.values()
            if (
                entity["class"] == "IFCRELDEFINESBYTYPE"
                and {
                    product_id,
                    type_id
                }.issubset(
                    step_references(
                        entity["arguments"]
                    )
                )
            )
        ]

        self.assertEqual(
            len(type_relations),
            1
        )

        self.assert_step_property_set_link(
            entities,
            product_id,
            quantity_set_id
        )

    def test_property_creation(self):
        """Verify creation of a property within an IFC property set."""

        properties = [
            property_input(
                name="Reference",
                value="ABC-123",
                property_set="Pset_Custom"
            )
        ]

        for category in ENTITY_SOURCES:
            with self.subTest(category=category):
                created = self.create_entity(
                    category,
                    name=f"Property creation {category}"
                )

                self.assertEqual(
                    created["properties"],
                    []
                )

                updated = self.update_properties(
                    category,
                    created["id"],
                    properties
                )

                self.assert_identity(
                    created,
                    updated
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    properties
                )

                self.assertEqual(
                    updated["name"],
                    created["name"]
                )

        content = self.save_model()

        self.assertIn(
            "Pset_Custom",
            content
        )

        self.assertIn(
            "ABC-123",
            content
        )

        self.assertRegex(
            content,
            r"IFCPROPERTYSINGLEVALUE\(\s*'Reference'"
        )

        self.assertRegex(
            content,
            r"IFCPROPERTYSET\("
        )

        self.assertRegex(
            content,
            r"IFCMATERIALPROPERTIES\("
        )

        self.load_model(
            self.output_path
        )

        for category in ENTITY_SOURCES:
            name = f"Property creation {category}"

            if category == "transform":
                summaries = self.list_transforms()

                matches = [
                    self.get_transform(item["id"])
                    for item in summaries
                ]

                entities = [
                    item
                    for item in matches
                    if item["name"] == name
                ]

                self.assertEqual(
                    len(entities),
                    1
                )

                entity = entities[0]

            else:
                entity = self.find_material(
                    name
                )

            self.assert_entity_properties(
                category,
                entity,
                properties
            )

    def test_string_property(self):
        """Verify IfcLabel, IfcText and IfcIdentifier properties."""

        properties = [
            property_input(
                "Label",
                "Structural element",
                value_type="IfcLabel"
            ),
            property_input(
                "EmptyLabel",
                "",
                value_type="IfcLabel"
            ),
            property_input(
                "Identifier",
                "ELEMENT-00042",
                value_type="IfcIdentifier"
            ),
            property_input(
                "NumericIdentifier",
                "000123",
                value_type="IfcIdentifier"
            ),
            property_input(
                "Description",
                "First line\nSecond line\tIndented",
                value_type="IfcText"
            ),
            property_input(
                "Quotes",
                "Element's \"quoted\" description",
                value_type="IfcText"
            ),
            property_input(
                "Unicode",
                "Bâtiment — Gebäude — Δοκιμή — 建筑",
                value_type="IfcText"
            ),
            property_input(
                "LongText",
                "Long IFC property content. " * 256,
                value_type="IfcText"
            )
        ]

        self.assert_properties_roundtrip(
            properties,
            "String properties"
        )

    def test_integer_property(self):
        """Verify creation and retrieval of integer properties."""

        values = [
            0,
            1,
            -1,
            42,
            -1000,
            2147483647,
            -2147483648
        ]

        properties = [
            property_input(
                name=f"Integer{index}",
                value=value,
                value_type="IfcInteger"
            )
            for index, value in enumerate(values)
        ]

        created = self.assert_properties_roundtrip(
            properties,
            "Integer properties"
        )

        replacement = [
            property_input(
                "UpdatedInteger",
                -123456,
                value_type="IfcInteger"
            )
        ]

        for category, entity in created.items():
            with self.subTest(category=category):
                reloaded = self.refind_entity(
                    category,
                    entity
                )

                updated = self.update_properties(
                    category,
                    reloaded["id"],
                    replacement
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    replacement
                )

    def test_real_property(self):
        """Verify creation and retrieval of real-valued properties."""

        values = [
            0.0,
            1.0,
            -1.0,
            3.141592653589793,
            -123.456789,
            0.000000123456,
            1.23456789e-10,
            1.23456789e10
        ]

        properties = [
            property_input(
                name=f"Real{index}",
                value=value,
                value_type="IfcReal"
            )
            for index, value in enumerate(values)
        ]

        created = self.assert_properties_roundtrip(
            properties,
            "Real properties"
        )

        for category, entity in created.items():
            with self.subTest(category=category):
                reloaded = self.refind_entity(
                    category,
                    entity
                )

                for property_value in reloaded["properties"]:
                    self.assertIs(
                        type(property_value["value"]),
                        float
                    )

    def test_boolean_property(self):
        """Verify preservation of true and false IFC boolean values."""

        properties = [
            property_input(
                "Enabled",
                True,
                value_type="IfcBoolean"
            ),
            property_input(
                "Disabled",
                False,
                value_type="IfcBoolean"
            )
        ]

        created = self.assert_properties_roundtrip(
            properties,
            "Boolean properties"
        )

        replacement = [
            property_input(
                "Enabled",
                False,
                value_type="IfcBoolean"
            ),
            property_input(
                "Disabled",
                True,
                value_type="IfcBoolean"
            )
        ]

        for category, entity in created.items():
            with self.subTest(category=category):
                reloaded = self.refind_entity(
                    category,
                    entity
                )

                updated = self.update_properties(
                    category,
                    reloaded["id"],
                    replacement
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    replacement
                )

        self.save_and_reload()

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            self.assert_entity_properties(
                category,
                reloaded,
                replacement
            )

    def test_null_property(self):
        """Verify null property values and preservation of their IFC types."""

        value_types = [
            "IfcLabel",
            "IfcText",
            "IfcIdentifier",
            "IfcInteger",
            "IfcReal",
            "IfcBoolean",
            "IfcLengthMeasure"
        ]

        properties = [
            property_input(
                name=f"Null{value_type}",
                value=None,
                value_type=value_type
            )
            for value_type in value_types
        ]

        properties.append(
            property_input(
                "NullLengthWithUnit",
                None,
                value_type="IfcLengthMeasure",
                unit="METRE"
            )
        )

        created = self.assert_properties_roundtrip(
            properties,
            "Null properties"
        )

        replacement = [
            property_input(
                "OptionalReference",
                "Defined value",
                value_type="IfcLabel"
            ),
            property_input(
                "OptionalCount",
                42,
                value_type="IfcInteger"
            )
        ]

        null_replacement = [
            property_input(
                "OptionalReference",
                None,
                value_type="IfcLabel"
            ),
            property_input(
                "OptionalCount",
                None,
                value_type="IfcInteger"
            )
        ]

        for category, entity in created.items():
            with self.subTest(category=category):
                reloaded = self.refind_entity(
                    category,
                    entity
                )

                defined = self.update_properties(
                    category,
                    reloaded["id"],
                    replacement
                )

                self.assert_entity_properties(
                    category,
                    defined,
                    replacement
                )

                cleared_values = self.update_properties(
                    category,
                    reloaded["id"],
                    null_replacement
                )

                self.assert_entity_properties(
                    category,
                    cleared_values,
                    null_replacement
                )

                self.assertEqual(
                    len(cleared_values["properties"]),
                    2,
                    "Null values must not remove their properties"
                )

        self.save_and_reload()

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            self.assert_entity_properties(
                category,
                reloaded,
                null_replacement
            )

    def test_property_units(self):
        """Verify association of properties with existing IFC units."""

        properties = [
            property_input(
                "Length",
                2.5,
                value_type="IfcLengthMeasure",
                unit="METRE",
                property_set="Pset_Dimensions"
            ),
            property_input(
                "Area",
                12.75,
                value_type="IfcAreaMeasure",
                unit="SQUARE_METRE",
                property_set="Pset_Dimensions"
            ),
            property_input(
                "Volume",
                25.5,
                value_type="IfcVolumeMeasure",
                unit="CUBIC_METRE",
                property_set="Pset_Dimensions"
            ),
            property_input(
                "Mass",
                1500.0,
                value_type="IfcMassMeasure",
                unit="GRAM",
                property_set="Pset_Physical"
            ),
            property_input(
                "Duration",
                60.0,
                value_type="IfcTimeMeasure",
                unit="SECOND",
                property_set="Pset_Physical"
            ),
            property_input(
                "Temperature",
                293.15,
                value_type="IfcThermodynamicTemperatureMeasure",
                unit="KELVIN",
                property_set="Pset_Physical"
            ),
            property_input(
                "Dimensionless",
                0.75,
                value_type="IfcReal"
            )
        ]

        # The current CLI identifies SI units by their IFC Name
        # rather than by their full prefix-qualified symbol.
        # The fixture therefore contains one unit per SI name.

        created = self.assert_properties_roundtrip(
            properties,
            "Properties with units"
        )

        content = self.save_model()

        entities = parse_step_entities(
            content
        )

        unit_entities = {
            identifier: entity
            for identifier, entity in entities.items()
            if entity["class"] == "IFCSIUNIT"
        }

        self.assertEqual(
            len(unit_entities),
            6,
            "Existing model units must be reused"
        )

        kilogram_units = [
            entity
            for entity in unit_entities.values()
            if (
                ".KILO." in entity["arguments"]
                and ".GRAM." in entity["arguments"]
            )
        ]

        self.assertEqual(
            len(kilogram_units),
            1
        )

        for property_value in properties:
            unit_name = property_value["unit"]

            if unit_name is None:
                continue

            matching_units = {
                identifier
                for identifier, entity in unit_entities.items()
                if f".{unit_name}." in entity["arguments"]
            }

            self.assertEqual(
                len(matching_units),
                1
            )

            matching_properties = [
                entity
                for entity in entities.values()
                if (
                    entity["class"] == "IFCPROPERTYSINGLEVALUE"
                    and re.match(
                        rf"\s*'{re.escape(property_value['name'])}'\s*,",
                        entity["arguments"]
                    )
                )
            ]

            self.assertEqual(
                len(matching_properties),
                2
            )

            for entity in matching_properties:
                self.assertTrue(
                    matching_units.issubset(
                        step_references(
                            entity["arguments"]
                        )
                    )
                )

        replacement = [
            property_input(
                "Length",
                2.5,
                value_type="IfcLengthMeasure",
                unit=None,
                property_set="Pset_Dimensions"
            )
        ]

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            updated = self.update_properties(
                category,
                reloaded["id"],
                replacement
            )

            self.assert_entity_properties(
                category,
                updated,
                replacement
            )

    def test_invalid_unit(self):
        """Verify rejection of units absent from the IFC model."""

        invalid_units = [
            "UNKNOWN_UNIT",
            "MILLIMETRE",
            "CENTIMETRE",
            "INCH",
            "FOOT",
            "METRE_UNKNOWN",
            "metre",
            "",
            42,
            True,
            [],
            {}
        ]

        for category, source in ENTITY_SOURCES.items():
            for index, unit in enumerate(invalid_units):
                with self.subTest(
                    category=category,
                    unit=unit
                ):
                    properties = [
                        property_input(
                            "InvalidLength",
                            2.5,
                            value_type="IfcLengthMeasure",
                            unit=unit,
                            source=source
                        )
                    ]

                    self.client.expect_error(
                        f"{category}Create",
                        self.create_entity_params(
                            category,
                            f"Invalid unit {index}",
                            properties
                        )
                    )

                    self.client.request(
                        "getCapabilities"
                    )

        for category, source in ENTITY_SOURCES.items():
            with self.subTest(category=category):
                entity = self.create_entity(
                    category,
                    name=f"Valid unit recovery {category}"
                )

                self.client.expect_error(
                    f"{category}Update",
                    {
                        "id": entity["id"],
                        "properties": [
                            property_input(
                                "InvalidLength",
                                5.0,
                                value_type="IfcLengthMeasure",
                                unit="NONEXISTENT_UNIT",
                                source=source
                            )
                        ]
                    }
                )

                self.assertEqual(
                    self.get_properties(
                        category,
                        entity["id"]
                    ),
                    []
                )

                valid = [
                    property_input(
                        "ValidLength",
                        5.0,
                        value_type="IfcLengthMeasure",
                        unit="METRE"
                    )
                ]

                updated = self.update_properties(
                    category,
                    entity["id"],
                    valid
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    valid
                )

    def test_invalid_value_type(self):
        """Verify rejection of unknown or incompatible IFC value types."""

        invalid_cases = [
            ("UnknownIfcType", "value"),
            ("IfcNonexistentMeasure", 1.0),
            ("IfcWall", "Not a simple value"),
            ("IfcPropertySet", "Not a simple value"),
            ("", "value"),
            (None, "value"),
            (42, "value"),
            ("IfcInteger", "not an integer"),
            ("IfcInteger", 3.5),
            ("IfcInteger", True),
            ("IfcReal", "not a real number"),
            ("IfcReal", False),
            ("IfcBoolean", "true"),
            ("IfcBoolean", 1),
            ("IfcBoolean", 0.5),
            ("IfcLabel", 42),
            ("IfcIdentifier", True),
            ("IfcText", 3.14),
            ("IfcLabel", ["invalid", "list"]),
            ("IfcReal", {"value": 1.0})
        ]

        # Invalid semantic types must return INVALID_PARAMETERS.
        # Acceptance or INTERNAL_ERROR indicates a validation defect.

        for category, source in ENTITY_SOURCES.items():
            for index, (value_type, value) in enumerate(
                invalid_cases
            ):
                with self.subTest(
                    category=category,
                    value_type=value_type,
                    value=value
                ):
                    properties = [
                        property_input(
                            "InvalidValue",
                            value,
                            value_type=value_type,
                            source=source
                        )
                    ]

                    self.client.expect_error(
                        f"{category}Create",
                        self.create_entity_params(
                            category,
                            f"Invalid type {index}",
                            properties
                        )
                    )

                    self.client.request(
                        "getCapabilities"
                    )

        for category, source in ENTITY_SOURCES.items():
            with self.subTest(category=category):
                entity = self.create_entity(
                    category,
                    name=f"Invalid type recovery {category}"
                )

                self.client.expect_error(
                    f"{category}Update",
                    {
                        "id": entity["id"],
                        "properties": [
                            property_input(
                                "InvalidValue",
                                1.0,
                                value_type="IfcUnknownValueType",
                                source=source
                            )
                        ]
                    }
                )

                self.assertEqual(
                    self.get_properties(
                        category,
                        entity["id"]
                    ),
                    []
                )

                valid = [
                    property_input(
                        "ValidInteger",
                        42,
                        value_type="IfcInteger"
                    )
                ]

                updated = self.update_properties(
                    category,
                    entity["id"],
                    valid
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    valid
                )

    def test_multiple_property_sets(self):
        """Verify independent property sets on the same entity."""

        properties = [
            property_input(
                "Reference",
                "IDENTITY-001",
                property_set="Pset_Identity"
            ),
            property_input(
                "Reference",
                "SIMULATION-001",
                property_set="Pset_Simulation"
            ),
            property_input(
                "Enabled",
                True,
                value_type="IfcBoolean",
                property_set="Pset_Simulation"
            ),
            property_input(
                "Length",
                12.5,
                value_type="IfcLengthMeasure",
                unit="METRE",
                property_set="Pset_Dimensions"
            ),
            property_input(
                "Width",
                3.0,
                value_type="IfcLengthMeasure",
                unit="METRE",
                property_set="Pset_Dimensions"
            ),
            property_input(
                "Reference",
                "CUSTOM-001",
                property_set="Pset_Custom"
            )
        ]

        created = self.assert_properties_roundtrip(
            properties,
            "Multiple property sets"
        )

        replacement = [
            property_input(
                "Reference",
                "IDENTITY-002",
                property_set="Pset_Identity"
            ),
            property_input(
                "Enabled",
                False,
                value_type="IfcBoolean",
                property_set="Pset_Simulation"
            ),
            property_input(
                "Height",
                5.0,
                value_type="IfcLengthMeasure",
                unit="METRE",
                property_set="Pset_Dimensions"
            )
        ]

        for category, entity in created.items():
            with self.subTest(category=category):
                reloaded = self.refind_entity(
                    category,
                    entity
                )

                property_sets = {
                    item["propertySet"]
                    for item in reloaded["properties"]
                }

                self.assertEqual(
                    property_sets,
                    {
                        "Pset_Identity",
                        "Pset_Simulation",
                        "Pset_Dimensions",
                        "Pset_Custom"
                    }
                )

                updated = self.update_properties(
                    category,
                    reloaded["id"],
                    replacement
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    replacement
                )

                updated_sets = {
                    item["propertySet"]
                    for item in updated["properties"]
                }

                self.assertNotIn(
                    "Pset_Custom",
                    updated_sets
                )

                self.assertEqual(
                    len(updated_sets),
                    3
                )

        self.save_and_reload()

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            self.assert_entity_properties(
                category,
                reloaded,
                replacement
            )

    def test_duplicate_property(self):
        """Verify rejection of duplicate property-set and name pairs."""

        for category, source in ENTITY_SOURCES.items():
            with self.subTest(category=category):
                original = [
                    property_input(
                        "Reference",
                        "Original reference",
                        property_set="Pset_DuplicateTest"
                    )
                ]

                entity = self.create_entity(
                    category,
                    name=f"Duplicate property {category}",
                    properties=original
                )

                before = self.get_entity(
                    category,
                    entity["id"]
                )

                first = property_input(
                    "Duplicate",
                    "First value",
                    property_set="Pset_DuplicateTest",
                    source=source
                )

                duplicate_cases = [
                    [
                        first,
                        first.copy()
                    ],
                    [
                        first,
                        {
                            **first,
                            "value": "Second value"
                        }
                    ],
                    [
                        first,
                        {
                            **first,
                            "valueType": "IfcInteger",
                            "value": 42
                        }
                    ],
                    [
                        first,
                        {
                            **first,
                            "value": None
                        }
                    ]
                ]

                for properties in duplicate_cases:
                    with self.subTest(
                        properties=properties
                    ):
                        index_before = self.list_entities(
                            category
                        )

                        self.client.expect_error(
                            f"{category}Create",
                            self.create_entity_params(
                                category,
                                "Invalid duplicate property",
                                properties
                            )
                        )

                        self.assertEqual(
                            self.list_entities(category),
                            index_before
                        )

                        self.client.expect_error(
                            f"{category}Update",
                            {
                                "id": entity["id"],
                                "properties": properties
                            }
                        )

                        self.assertEqual(
                            self.get_entity(
                                category,
                                entity["id"]
                            ),
                            before
                        )

                valid = [
                    property_input(
                        "Reference",
                        "First set",
                        property_set="Pset_First"
                    ),
                    property_input(
                        "Reference",
                        "Second set",
                        property_set="Pset_Second"
                    ),
                    property_input(
                        "reference",
                        "Case-sensitive name",
                        property_set="Pset_First"
                    )
                ]

                updated = self.update_properties(
                    category,
                    entity["id"],
                    valid
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    valid
                )

    def test_property_replacement(self):
        """Verify complete replacement of editable properties."""

        original = [
            property_input(
                "Reference",
                "Original reference",
                property_set="Pset_Identity"
            ),
            property_input(
                "Count",
                42,
                value_type="IfcInteger",
                property_set="Pset_Identity"
            ),
            property_input(
                "Enabled",
                True,
                value_type="IfcBoolean",
                property_set="Pset_Configuration"
            ),
            property_input(
                "Length",
                5.0,
                value_type="IfcLengthMeasure",
                unit="METRE",
                property_set="Pset_Dimensions"
            )
        ]

        replacement = [
            property_input(
                "Reference",
                "Updated reference",
                property_set="Pset_Identity"
            ),
            property_input(
                "Count",
                "Forty-two",
                value_type="IfcText",
                property_set="Pset_Identity"
            ),
            property_input(
                "Enabled",
                False,
                value_type="IfcBoolean",
                property_set="Pset_Configuration"
            ),
            property_input(
                "NewValue",
                3.125,
                value_type="IfcReal",
                property_set="Pset_New"
            )
        ]

        created = {}

        for category in ENTITY_SOURCES:
            with self.subTest(category=category):
                entity = self.create_entity(
                    category,
                    name=f"Property replacement {category}",
                    properties=original
                )

                created[category] = entity

                updated = self.update_properties(
                    category,
                    entity["id"],
                    replacement
                )

                self.assert_identity(
                    entity,
                    updated
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    replacement
                )

                for field in (
                    "name",
                    "globalId"
                ):
                    self.assertEqual(
                        updated[field],
                        entity[field]
                    )

                if category == "transform":
                    for field in (
                        "parents",
                        "transforms",
                        "geometries",
                        "materials"
                    ):
                        self.assertEqual(
                            updated[field],
                            entity[field]
                        )

                else:
                    for field in (
                        "category",
                        "visual"
                    ):
                        self.assertEqual(
                            updated[field],
                            entity[field]
                        )

                repeated = self.update_properties(
                    category,
                    entity["id"],
                    replacement
                )

                self.assert_entity_properties(
                    category,
                    repeated,
                    replacement
                )

        self.save_and_reload()

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            self.assert_entity_properties(
                category,
                reloaded,
                replacement
            )

    def test_property_removal(self):
        """Verify removal of properties omitted from an update array."""

        original = [
            property_input(
                "Preserved",
                "Preserved value",
                property_set="Pset_Removal"
            ),
            property_input(
                "RemovedSingleValue",
                "VALUE_TO_REMOVE",
                property_set="Pset_Removal"
            ),
            property_input(
                "RemovedInteger",
                42,
                value_type="IfcInteger",
                property_set="Pset_Removal"
            ),
            property_input(
                "RemovedSetProperty",
                "SET_TO_REMOVE",
                property_set="Pset_RemoveEntirely"
            )
        ]

        remaining = [
            original[0]
        ]

        created = {}

        for category in ENTITY_SOURCES:
            with self.subTest(category=category):
                entity = self.create_entity(
                    category,
                    name=f"Property removal {category}",
                    properties=original
                )

                created[category] = entity

                updated = self.update_properties(
                    category,
                    entity["id"],
                    remaining
                )

                self.assert_entity_properties(
                    category,
                    updated,
                    remaining
                )

                names = {
                    item["name"]
                    for item in updated["properties"]
                }

                self.assertEqual(
                    names,
                    {"Preserved"}
                )

                renamed = self.client.request(
                    f"{category}Update",
                    {
                        "id": entity["id"],
                        "name": f"Renamed removal {category}"
                    }
                )

                created[category] = renamed

                self.assert_entity_properties(
                    category,
                    renamed,
                    remaining
                )

        content = self.save_model()

        for removed_text in (
            "RemovedSingleValue",
            "RemovedInteger",
            "RemovedSetProperty",
            "VALUE_TO_REMOVE",
            "SET_TO_REMOVE",
            "Pset_RemoveEntirely"
        ):
            with self.subTest(removed=removed_text):
                self.assertNotIn(
                    removed_text,
                    content
                )

        self.assertIn(
            "Preserved value",
            content
        )

        self.load_model(
            self.output_path
        )

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            self.assert_entity_properties(
                category,
                reloaded,
                remaining
            )

    def test_property_clear(self):
        """Verify clearing all editable properties with an empty array."""

        properties = [
            property_input(
                "ClearReference",
                "CLEAR_REFERENCE_VALUE",
                property_set="Pset_ClearIdentity"
            ),
            property_input(
                "ClearCount",
                42,
                value_type="IfcInteger",
                property_set="Pset_ClearIdentity"
            ),
            property_input(
                "ClearEnabled",
                True,
                value_type="IfcBoolean",
                property_set="Pset_ClearConfiguration"
            )
        ]

        created = {}

        for category in ENTITY_SOURCES:
            with self.subTest(category=category):
                entity = self.create_entity(
                    category,
                    name=f"Property clearing {category}",
                    properties=properties
                )

                created[category] = entity

                self.assert_entity_properties(
                    category,
                    entity,
                    properties
                )

                cleared = self.update_properties(
                    category,
                    entity["id"],
                    []
                )

                self.assert_identity(
                    entity,
                    cleared
                )

                self.assertEqual(
                    cleared["properties"],
                    []
                )

                self.assert_entity_properties(
                    category,
                    cleared,
                    []
                )

                repeated = self.update_properties(
                    category,
                    entity["id"],
                    []
                )

                self.assertEqual(
                    repeated["properties"],
                    []
                )

                if category == "material":
                    self.assertEqual(
                        repeated["visual"],
                        entity["visual"]
                    )

                    self.assertEqual(
                        repeated["category"],
                        entity["category"]
                    )

        content = self.save_model()

        for removed_text in (
            "Pset_ClearIdentity",
            "Pset_ClearConfiguration",
            "ClearReference",
            "ClearCount",
            "ClearEnabled",
            "CLEAR_REFERENCE_VALUE"
        ):
            self.assertNotIn(
                removed_text,
                content
            )

        self.load_model(
            self.output_path
        )

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            self.assert_entity_properties(
                category,
                reloaded,
                []
            )

            if category == "material":
                self.assertEqual(
                    reloaded["visual"],
                    entity["visual"]
                )

                self.assertEqual(
                    reloaded["category"],
                    entity["category"]
                )

            restored = self.update_properties(
                category,
                reloaded["id"],
                properties
            )

            self.assert_entity_properties(
                category,
                restored,
                properties
            )

    def test_property_source(self):
        """Verify distinction between occurrence and material properties."""

        transform_properties = [
            property_input(
                "Reference",
                "Transform reference",
                property_set="Pset_Identity",
                source="occurrence"
            )
        ]

        material_properties = [
            property_input(
                "Reference",
                "Material reference",
                property_set="Pset_Identity",
                source="material"
            )
        ]

        transform = self.create_entity(
            "transform",
            name="Source transform",
            properties=transform_properties
        )

        material = self.create_entity(
            "material",
            name="Source material",
            properties=material_properties
        )

        self.assert_properties(
            transform["properties"],
            transform_properties
        )

        self.assert_properties(
            material["properties"],
            material_properties
        )

        self.assertEqual(
            {
                item["source"]
                for item in transform["properties"]
            },
            {"occurrence"}
        )

        self.assertEqual(
            {
                item["source"]
                for item in material["properties"]
            },
            {"material"}
        )

        for category, source in ENTITY_SOURCES.items():
            entity = (
                transform
                if category == "transform"
                else material
            )

            before = self.get_entity(
                category,
                entity["id"]
            )

            invalid_sources = [
                candidate
                for candidate in (
                    "occurrence",
                    "material",
                    "type",
                    "quantity",
                    "unknown",
                    "Occurrence",
                    "Material",
                    "",
                    None,
                    42,
                    True
                )
                if candidate != source
            ]

            for invalid_source in invalid_sources:
                with self.subTest(
                    category=category,
                    source=invalid_source
                ):
                    properties = [
                        property_input(
                            "InvalidSource",
                            "Invalid source value",
                            source=invalid_source
                        )
                    ]

                    self.client.expect_error(
                        f"{category}Create",
                        self.create_entity_params(
                            category,
                            "Invalid source entity",
                            properties
                        )
                    )

                    self.client.expect_error(
                        f"{category}Update",
                        {
                            "id": entity["id"],
                            "properties": properties
                        }
                    )

                    self.assertEqual(
                        self.get_entity(
                            category,
                            entity["id"]
                        ),
                        before
                    )

        readonly = self.get_transform(
            self.readonly_product_id
        )

        sources = {
            item["source"]
            for item in readonly["properties"]
        }

        self.assertEqual(
            sources,
            {
                "occurrence",
                "type",
                "quantity"
            }
        )

        self.save_and_reload()

        reloaded_transform = self.find_transform(
            transform["globalId"]
        )

        reloaded_material = self.find_material(
            material["name"]
        )

        self.assert_properties(
            reloaded_transform["properties"],
            transform_properties
        )

        self.assert_properties(
            reloaded_material["properties"],
            material_properties
        )

    def test_shared_property_set(self):
        """Verify isolation when modifying a shared IFC property set."""

        # Two independent IfcRelDefinesByProperties relationships
        # reference the same property set. Editing one occurrence
        # must not mutate the other occurrence's properties.

        original = [
            property_input(
                "SharedReference",
                "Original shared value",
                property_set="Pset_Shared"
            ),
            property_input(
                "SharedCount",
                42,
                value_type="IfcInteger",
                property_set="Pset_Shared"
            )
        ]

        first_before = self.get_transform(
            self.first_product_id
        )

        second_before = self.get_transform(
            self.second_product_id
        )

        self.assert_properties(
            first_before["properties"],
            original
        )

        self.assert_properties(
            second_before["properties"],
            original
        )

        replacement = [
            property_input(
                "SharedReference",
                "First product independent value",
                property_set="Pset_Shared"
            ),
            property_input(
                "NewReference",
                "New first product property",
                property_set="Pset_Shared"
            )
        ]

        updated_first = self.update_properties(
            "transform",
            self.first_product_id,
            replacement
        )

        self.assert_identity(
            first_before,
            updated_first
        )

        self.assert_properties(
            updated_first["properties"],
            replacement
        )

        second_after = self.get_transform(
            self.second_product_id
        )

        self.assert_properties(
            second_after["properties"],
            original
        )

        self.assertEqual(
            second_after,
            second_before,
            "Updating a shared property set modified another product"
        )

        self.save_and_reload()

        self.assert_properties(
            self.get_transform(
                self.first_product_id
            )["properties"],
            replacement
        )

        self.assert_properties(
            self.get_transform(
                self.second_product_id
            )["properties"],
            original
        )

        second_replacement = [
            property_input(
                "SharedReference",
                "Second product independent value",
                property_set="Pset_Shared"
            )
        ]

        self.update_properties(
            "transform",
            self.second_product_id,
            second_replacement
        )

        self.assert_properties(
            self.get_transform(
                self.first_product_id
            )["properties"],
            replacement
        )

        self.assert_properties(
            self.get_transform(
                self.second_product_id
            )["properties"],
            second_replacement
        )

        self.update_properties(
            "transform",
            self.first_product_id,
            []
        )

        self.assertEqual(
            self.get_properties(
                "transform",
                self.first_product_id
            ),
            []
        )

        self.assert_properties(
            self.get_properties(
                "transform",
                self.second_product_id
            ),
            second_replacement
        )

        self.save_and_reload()

        self.assertEqual(
            self.get_properties(
                "transform",
                self.first_product_id
            ),
            []
        )

        self.assert_properties(
            self.get_properties(
                "transform",
                self.second_product_id
            ),
            second_replacement
        )

    def test_readonly_properties(self):
        """Verify preservation of unsupported and inherited IFC properties."""

        expected_editable = [
            property_input(
                "EditableReference",
                "Original editable value",
                property_set="Pset_Mixed"
            )
        ]

        expected_readonly = [
            property_input(
                "TypeReference",
                "Inherited type value",
                property_set="Pset_Type",
                source="type"
            ),
            property_input(
                "NativeLength",
                12.5,
                value_type="IfcQuantityLength",
                property_set="Qto_Native",
                source="quantity"
            )
        ]

        original = self.get_transform(
            self.readonly_product_id
        )

        self.assert_properties(
            original["properties"],
            expected_editable + expected_readonly
        )

        self.assert_readonly_step_entities(
            IFC_FIXTURE
        )

        self.assert_readonly_step_entities(
            self.save_model()
        )

        replacement = [
            property_input(
                "EditableReference",
                "Updated editable value",
                property_set="Pset_Mixed"
            ),
            property_input(
                "AdditionalValue",
                123.45,
                value_type="IfcReal",
                property_set="Pset_Mixed"
            )
        ]

        updated = self.update_properties(
            "transform",
            self.readonly_product_id,
            replacement
        )

        self.assert_identity(
            original,
            updated
        )

        self.assert_properties(
            updated["properties"],
            replacement + expected_readonly
        )

        self.assert_readonly_step_entities(
            self.save_model()
        )

        self.load_model(
            self.output_path
        )

        self.assert_properties(
            self.get_transform(
                self.readonly_product_id
            )["properties"],
            replacement + expected_readonly
        )

        for readonly_property in expected_readonly:
            with self.subTest(
                source=readonly_property["source"]
            ):
                before = self.get_transform(
                    self.readonly_product_id
                )

                self.client.expect_error(
                    "transformUpdate",
                    {
                        "id": self.readonly_product_id,
                        "properties": [
                            {
                                **readonly_property,
                                "value": "Invalid readonly update"
                            }
                        ]
                    }
                )

                self.assertEqual(
                    self.get_transform(
                        self.readonly_product_id
                    ),
                    before
                )

        cleared = self.update_properties(
            "transform",
            self.readonly_product_id,
            []
        )

        self.assert_properties(
            cleared["properties"],
            expected_readonly
        )

        content = self.save_model()

        self.assert_readonly_step_entities(
            content
        )

        self.assertNotIn(
            "Updated editable value",
            content
        )

        self.assertNotIn(
            "AdditionalValue",
            content
        )

        self.load_model(
            self.output_path
        )

        self.assert_properties(
            self.get_transform(
                self.readonly_product_id
            )["properties"],
            expected_readonly
        )

        self.assert_properties(
            self.readonly_properties(),
            expected_readonly
        )

        self.assert_readonly_step_entities(
            self.save_model()
        )

    def test_property_persistence(self):
        """Verify preservation of property types, values, units and sources."""

        properties = [
            property_input(
                "Reference",
                "PERSISTENT-001",
                value_type="IfcIdentifier",
                property_set="Pset_Identity"
            ),
            property_input(
                "Label",
                "Persistent label",
                value_type="IfcLabel",
                property_set="Pset_Identity"
            ),
            property_input(
                "Description",
                "Propriétés persistantes — Gebäude — 建筑",
                value_type="IfcText",
                property_set="Pset_Identity"
            ),
            property_input(
                "Count",
                123456,
                value_type="IfcInteger",
                property_set="Pset_Physical"
            ),
            property_input(
                "Coefficient",
                0.123456789,
                value_type="IfcReal",
                property_set="Pset_Physical"
            ),
            property_input(
                "Length",
                12.3456789,
                value_type="IfcLengthMeasure",
                unit="METRE",
                property_set="Pset_Physical"
            ),
            property_input(
                "Area",
                123.456789,
                value_type="IfcAreaMeasure",
                unit="SQUARE_METRE",
                property_set="Pset_Physical"
            ),
            property_input(
                "Volume",
                456.789123,
                value_type="IfcVolumeMeasure",
                unit="CUBIC_METRE",
                property_set="Pset_Physical"
            ),
            property_input(
                "Mass",
                1500.25,
                value_type="IfcMassMeasure",
                unit="GRAM",
                property_set="Pset_Physical"
            ),
            property_input(
                "Temperature",
                293.15,
                value_type="IfcThermodynamicTemperatureMeasure",
                unit="KELVIN",
                property_set="Pset_Physical"
            ),
            property_input(
                "Enabled",
                True,
                value_type="IfcBoolean",
                property_set="Pset_Configuration"
            ),
            property_input(
                "Disabled",
                False,
                value_type="IfcBoolean",
                property_set="Pset_Configuration"
            ),
            property_input(
                "OptionalInteger",
                None,
                value_type="IfcInteger",
                property_set="Pset_Optional"
            ),
            property_input(
                "OptionalLength",
                None,
                value_type="IfcLengthMeasure",
                unit="METRE",
                property_set="Pset_Optional"
            ),
            property_input(
                "OptionalLabel",
                None,
                value_type="IfcLabel",
                property_set="Pset_Optional"
            )
        ]

        created = {
            category: self.create_entity(
                category,
                name=f"Persistent properties {category}",
                properties=properties
            )
            for category in ENTITY_SOURCES
        }

        for category, entity in created.items():
            self.assert_entity_properties(
                category,
                entity,
                properties
            )

        for iteration in range(10):
            with self.subTest(
                roundtrip=iteration
            ):
                self.save_and_reload()

                for category, entity in created.items():
                    reloaded = self.refind_entity(
                        category,
                        entity
                    )

                    self.assert_entity_properties(
                        category,
                        reloaded,
                        properties
                    )

                    self.assertEqual(
                        len(reloaded["properties"]),
                        len(properties)
                    )

                    if category == "transform":
                        self.assertEqual(
                            reloaded["globalId"],
                            entity["globalId"]
                        )

                    else:
                        self.assertEqual(
                            reloaded["category"],
                            entity["category"]
                        )

                        self.assertEqual(
                            reloaded["visual"],
                            entity["visual"]
                        )

        replacement = [
            {
                **property_value,
                "value": "PERSISTENT-002"
            }
            if property_value["name"] == "Reference"
            else {
                **property_value,
                "value": 654321
            }
            if property_value["name"] == "Count"
            else {
                **property_value,
                "value": 20.5
            }
            if property_value["name"] == "OptionalLength"
            else {
                **property_value,
                "value": False
            }
            if property_value["name"] == "Enabled"
            else property_value.copy()
            for property_value in properties
        ]

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            updated = self.update_properties(
                category,
                reloaded["id"],
                replacement
            )

            self.assert_entity_properties(
                category,
                updated,
                replacement
            )

        self.save_and_reload()

        for category, entity in created.items():
            reloaded = self.refind_entity(
                category,
                entity
            )

            self.assert_entity_properties(
                category,
                reloaded,
                replacement
            )

        final_content = self.save_model()

        self.assertIn(
            "PERSISTENT-002",
            final_content
        )

        self.assertNotIn(
            "PERSISTENT-001",
            final_content
        )

        for value_type in (
            "IFCIDENTIFIER",
            "IFCLABEL",
            "IFCTEXT",
            "IFCINTEGER",
            "IFCREAL",
            "IFCBOOLEAN",
            "IFCLENGTHMEASURE",
            "IFCAREAMEASURE",
            "IFCVOLUMEMEASURE",
            "IFCMASSMEASURE",
            "IFCTHERMODYNAMICTEMPERATUREMEASURE"
        ):
            with self.subTest(value_type=value_type):
                self.assertIn(
                    f"{value_type}(",
                    final_content.upper()
                )

        self.assert_readonly_step_entities(
            final_content
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