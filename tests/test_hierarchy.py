import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from pathlib import Path


# Standalone integration suite; execution against the compiled CLI
# is required to validate the expected behavior.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

PROJECT_GLOBAL_ID = "0000000000000000000001"
FIRST_STOREY_GLOBAL_ID = "0000000000000000000002"
SECOND_STOREY_GLOBAL_ID = "0000000000000000000003"
PHYSICAL_GLOBAL_ID = "0000000000000000000004"
VIRTUAL_GLOBAL_ID = "0000000000000000000005"

PARENT_TYPES = (
    "spatial",
    "placement",
    "decomposition"
)

DEFAULT_PROFILE = [
    [0, 0],
    [5, 0],
    [5, 3],
    [0, 3]
]

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('hierarchy-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#8=IFCPROJECT('0000000000000000000001',$,'Hierarchy test project',$,$,$,$,(#5),#7);
#9=IFCCARTESIANPOINT((10.,0.,0.));
#10=IFCAXIS2PLACEMENT3D(#9,#2,#3);
#11=IFCLOCALPLACEMENT($,#10);
#12=IFCBUILDINGSTOREY('0000000000000000000002',$,'First storey',$,$,#11,$,$,.ELEMENT.,0.);
#13=IFCCARTESIANPOINT((100.,0.,0.));
#14=IFCAXIS2PLACEMENT3D(#13,#2,#3);
#15=IFCLOCALPLACEMENT($,#14);
#16=IFCBUILDINGSTOREY('0000000000000000000003',$,'Second storey',$,$,#15,$,$,.ELEMENT.,0.);
#17=IFCLOCALPLACEMENT($,#4);
#18=IFCCARTESIANPOINT((0.,0.));
#19=IFCAXIS2PLACEMENT2D(#18,$);
#20=IFCRECTANGLEPROFILEDEF(.AREA.,$,#19,2.,1.);
#21=IFCEXTRUDEDAREASOLID(#20,#4,#2,1.);
#22=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#21));
#23=IFCPRODUCTDEFINITIONSHAPE($,$,(#22));
#24=IFCBUILDINGELEMENTPROXY('0000000000000000000004',$,'Physical solid',$,$,#17,#23,$,.NOTDEFINED.);
#25=IFCBUILDINGELEMENTPROXY('0000000000000000000005',$,'Virtual product',$,$,$,$,$,.NOTDEFINED.);
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


def mesh_bounds(mesh):
    vertices = mesh["vertices"]

    if not vertices:
        raise AssertionError("Mesh has no vertices")

    return tuple(
        function(vertices[axis::3])
        for function in (min, max)
        for axis in range(3)
    )


def mesh_signature(mesh, precision=7):
    vertices = mesh["vertices"]
    indices = mesh["indices"]

    points = [
        tuple(
            round(value, precision)
            for value in vertices[offset:offset + 3]
        )
        for offset in range(0, len(vertices), 3)
    ]

    return sorted(
        tuple(sorted(
            points[index]
            for index in indices[offset:offset + 3]
        ))
        for offset in range(0, len(indices), 3)
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

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.normals": 3,
            "mesh.end": 4
        }

        begin = None
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
            raise AssertionError(
                "Missing mesh.begin event"
            )

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        for field in ("vertexCount", "indexCount"):
            if type(mesh[field]) is not int or mesh[field] < 0:
                raise AssertionError(
                    f"Invalid mesh count: {begin}"
                )

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError(
                "Invalid vertex count"
            )

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError(
                "Invalid index count"
            )

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError(
                "Invalid triangle indices"
            )

        if len(mesh["normals"]) % 3 != 0:
            raise AssertionError(
                "Invalid normal array length"
            )

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


class HierarchyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-hierarchy-"
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

        self.assertEqual(
            result["transformCount"],
            len(self.list_transforms())
        )

        self.project_id = self.find_transform(
            PROJECT_GLOBAL_ID
        )["id"]

        self.first_storey_id = self.find_transform(
            FIRST_STOREY_GLOBAL_ID
        )["id"]

        self.second_storey_id = self.find_transform(
            SECOND_STOREY_GLOBAL_ID
        )["id"]

        self.physical_id = self.find_transform(
            PHYSICAL_GLOBAL_ID
        )["id"]

        self.virtual_id = self.find_transform(
            VIRTUAL_GLOBAL_ID
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

        self.assertTrue(
            self.output_path.is_file()
        )

        self.assertGreater(
            self.output_path.stat().st_size,
            0
        )

        return self.load_model(self.output_path)

    def list_transforms(self):
        return self.client.request(
            "transformGetAll"
        )

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

        return self.get_transform(
            matches[0]["id"]
        )

    def create_transform_params(
        self,
        name="Created transform",
        parents=None,
        transform=None,
        properties=None
    ):
        return {
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

    def create_transform(
        self,
        name="Created transform",
        parents=None,
        transform=None,
        properties=None
    ):
        return self.client.request(
            "transformCreate",
            self.create_transform_params(
                name=name,
                parents=parents,
                transform=transform,
                properties=properties
            )
        )

    def update_transform(self, identifier, **changes):
        return self.client.request(
            "transformUpdate",
            {
                "id": identifier,
                **changes
            }
        )

    def set_parents(self, identifier, **changes):
        transform = self.get_transform(identifier)

        parents = {
            **transform["parents"],
            **changes
        }

        return self.update_transform(
            identifier,
            parents=parents
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

    def duplicate_transform(
        self,
        identifier,
        include_children=True
    ):
        return self.client.request(
            "transformDuplicate",
            {
                "id": identifier,
                "includeChildren": include_children
            }
        )

    def list_geometries(
        self,
        parent_id=None,
        kind=None
    ):
        params = {
            "transformId": parent_id
        }

        if kind is not None:
            params["kind"] = kind

        return self.client.request(
            "geometryGetAll",
            params
        )

    def create_domain(
        self,
        parent_id,
        name="Hierarchy domain",
        x=0,
        y=0,
        z=0
    ):
        return self.client.request(
            "geometryCreate",
            {
                "parentId": parent_id,
                "name": name,
                "kind": "DOMAIN",
                "transform": transform_input(x, y, z),
                "profile": DEFAULT_PROFILE,
                "depth": 2.0,
                "operation": "ADD"
            }
        )

    def get_geometry(self, identifier):
        return self.client.request(
            "geometryGet",
            {"id": identifier}
        )

    def snapshot_hierarchy(self):
        return {
            summary["id"]: self.get_transform(
                summary["id"]
            )
            for summary in self.list_transforms()
        }

    def assert_matrix(self, actual, expected):
        self.assertIsInstance(actual, list)
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertIn(
                type(value),
                (int, float)
            )

            self.assertTrue(
                math.isfinite(value)
            )

            self.assertAlmostEqual(
                value,
                reference,
                places=7,
                msg=f"Invalid matrix component {index}"
            )

    def assert_bounds(self, mesh, expected):
        actual = mesh_bounds(mesh)

        self.assertEqual(
            len(actual),
            len(expected)
        )

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertAlmostEqual(
                value,
                reference,
                places=6,
                msg=f"Invalid bounding box component {index}"
            )

    def assert_empty_mesh(self, mesh):
        self.assertEqual(
            mesh["vertexCount"],
            0
        )

        self.assertEqual(
            mesh["indexCount"],
            0
        )

        self.assertEqual(
            mesh["vertices"],
            []
        )

        self.assertEqual(
            mesh["indices"],
            []
        )

        self.assertEqual(
            mesh["normals"],
            []
        )

    def assert_equivalent_meshes(self, first, second):
        self.assertEqual(
            first["indexCount"],
            second["indexCount"]
        )

        self.assertEqual(
            mesh_signature(first),
            mesh_signature(second)
        )

    def assert_parents(
        self,
        identifier,
        spatial=None,
        placement=None,
        decomposition=None
    ):
        transform = self.get_transform(identifier)

        self.assertEqual(
            transform["parents"],
            parents_input(
                spatial=spatial,
                placement=placement,
                decomposition=decomposition
            )
        )

        return transform

    def assert_identity(self, before, after):
        for field in (
            "id",
            "ifcEntityId",
            "globalId"
        ):
            self.assertEqual(
                before[field],
                after[field],
                f"Transform identity changed: {field}"
            )

    def assert_parent_graph(self):
        hierarchy = self.snapshot_hierarchy()

        for identifier, transform in hierarchy.items():
            self.assertEqual(
                set(transform["parents"]),
                set(PARENT_TYPES)
            )

            for parent_type, parent_id in transform["parents"].items():
                if parent_id is None:
                    continue

                self.assertIn(
                    parent_id,
                    hierarchy,
                    f"Unknown {parent_type} parent"
                )

                self.assertNotEqual(
                    parent_id,
                    identifier,
                    f"Self-referencing {parent_type} parent"
                )

        for parent_type in PARENT_TYPES:
            for identifier in hierarchy:
                visited = set()
                current = identifier

                while current is not None:
                    self.assertNotIn(
                        current,
                        visited,
                        f"Cycle detected in {parent_type} hierarchy"
                    )

                    visited.add(current)

                    current = hierarchy[current]["parents"][
                        parent_type
                    ]

        return hierarchy

    def collect_descendants(self, root_id):
        hierarchy = self.assert_parent_graph()

        self.assertIn(
            root_id,
            hierarchy
        )

        descendants = {root_id}

        changed = True

        while changed:
            changed = False

            for identifier, transform in hierarchy.items():
                if identifier in descendants:
                    continue

                if any(
                    parent_id in descendants
                    for parent_id in transform["parents"].values()
                    if parent_id is not None
                ):
                    descendants.add(identifier)
                    changed = True

        return descendants - {root_id}

    def assert_rejected_parent_update(
        self,
        identifier,
        parent_type,
        parent_id
    ):
        hierarchy_before = self.snapshot_hierarchy()

        transform = hierarchy_before[identifier]

        params = {
            "id": identifier,
            "parents": {
                **transform["parents"],
                parent_type: parent_id
            }
        }

        self.client.expect_error(
            "transformUpdate",
            params,
            "INVALID_RELATION"
        )

        self.assertEqual(
            self.snapshot_hierarchy(),
            hierarchy_before,
            "Rejected parent update modified the hierarchy"
        )

        self.assert_parent_graph()

    def test_spatial_parent(self):
        """Verify association with a valid IFC spatial parent."""

        child = self.create_transform(
            name="Spatial child",
            parents=parents_input(
                spatial=self.first_storey_id
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        retrieved = self.assert_parents(
            child["id"],
            spatial=self.first_storey_id
        )

        self.assert_identity(
            child,
            retrieved
        )

        self.assert_matrix(
            retrieved["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            retrieved["transforms"]["world"],
            matrix(2, 3, 4)
        )

        self.assertIn(
            child["id"],
            self.collect_descendants(
                self.first_storey_id
            )
        )

        self.assertNotIn(
            child["id"],
            self.collect_descendants(
                self.second_storey_id
            )
        )

        existing = self.get_transform(
            self.physical_id
        )

        updated = self.set_parents(
            self.physical_id,
            spatial=self.first_storey_id
        )

        self.assert_identity(
            existing,
            updated
        )

        self.assertEqual(
            updated["parents"],
            parents_input(
                spatial=self.first_storey_id
            )
        )

        self.assertEqual(
            updated["transforms"],
            existing["transforms"]
        )

        self.assert_parent_graph()

    def test_placement_parent(self):
        """Verify association with a parent object placement."""

        child = self.create_transform(
            name="Placement child",
            parents=parents_input(
                placement=self.first_storey_id
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        retrieved = self.assert_parents(
            child["id"],
            placement=self.first_storey_id
        )

        self.assert_matrix(
            retrieved["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            retrieved["transforms"]["world"],
            matrix(12, 3, 4)
        )

        grandchild = self.create_transform(
            name="Placement grandchild",
            parents=parents_input(
                placement=child["id"]
            ),
            transform=transform_input(
                x=5,
                y=6,
                z=7
            )
        )

        self.assert_matrix(
            grandchild["transforms"]["world"],
            matrix(17, 9, 11)
        )

        self.update_transform(
            self.first_storey_id,
            transform=transform_input(
                x=50,
                y=10,
                z=20,
                space="world"
            )
        )

        updated_child = self.get_transform(
            child["id"]
        )

        updated_grandchild = self.get_transform(
            grandchild["id"]
        )

        self.assert_identity(
            child,
            updated_child
        )

        self.assert_identity(
            grandchild,
            updated_grandchild
        )

        self.assert_matrix(
            updated_child["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            updated_child["transforms"]["world"],
            matrix(52, 13, 24)
        )

        self.assert_matrix(
            updated_grandchild["transforms"]["local"],
            matrix(5, 6, 7)
        )

        self.assert_matrix(
            updated_grandchild["transforms"]["world"],
            matrix(57, 19, 31)
        )

        self.assert_parent_graph()

    def test_decomposition_parent(self):
        """Verify association with an IFC decomposition parent."""

        parent = self.create_transform(
            name="Assembly parent",
            transform=transform_input(
                x=100,
                y=200,
                z=300
            )
        )

        child = self.create_transform(
            name="Assembly child",
            parents=parents_input(
                decomposition=parent["id"]
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        retrieved = self.assert_parents(
            child["id"],
            decomposition=parent["id"]
        )

        self.assert_matrix(
            retrieved["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            retrieved["transforms"]["world"],
            matrix(2, 3, 4)
        )

        self.assertEqual(
            self.collect_descendants(parent["id"]),
            {child["id"]}
        )

        self.update_transform(
            parent["id"],
            transform=transform_input(
                x=500,
                y=600,
                z=700,
                space="world"
            )
        )

        updated_child = self.get_transform(
            child["id"]
        )

        self.assertEqual(
            updated_child,
            retrieved,
            "Decomposition alone must not affect object placement"
        )

        self.assert_parent_graph()

    def test_independent_parents(self):
        """Verify independent spatial, placement and decomposition parents."""

        decomposition_parent = self.create_transform(
            name="Independent assembly",
            transform=transform_input(
                x=500,
                y=600,
                z=700
            )
        )

        child = self.create_transform(
            name="Independent child",
            parents=parents_input(
                spatial=self.first_storey_id,
                placement=self.second_storey_id,
                decomposition=decomposition_parent["id"]
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        retrieved = self.assert_parents(
            child["id"],
            spatial=self.first_storey_id,
            placement=self.second_storey_id,
            decomposition=decomposition_parent["id"]
        )

        self.assertEqual(
            len(set(retrieved["parents"].values())),
            3
        )

        self.assert_matrix(
            retrieved["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            retrieved["transforms"]["world"],
            matrix(102, 3, 4)
        )

        self.update_transform(
            self.first_storey_id,
            transform=transform_input(
                x=1000,
                space="world"
            )
        )

        self.update_transform(
            decomposition_parent["id"],
            transform=transform_input(
                x=2000,
                space="world"
            )
        )

        unchanged = self.get_transform(
            child["id"]
        )

        self.assertEqual(
            unchanged,
            retrieved
        )

        self.update_transform(
            self.second_storey_id,
            transform=transform_input(
                x=200,
                y=10,
                z=20,
                space="world"
            )
        )

        moved = self.get_transform(
            child["id"]
        )

        self.assert_identity(
            retrieved,
            moved
        )

        self.assertEqual(
            moved["parents"],
            retrieved["parents"]
        )

        self.assert_matrix(
            moved["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            moved["transforms"]["world"],
            matrix(202, 13, 24)
        )

        self.assert_parent_graph()

    def test_parent_replacement(self):
        """Verify replacement of each independent parent relation."""

        first_assembly = self.create_transform(
            name="First assembly"
        )

        second_assembly = self.create_transform(
            name="Second assembly"
        )

        child = self.create_transform(
            name="Reparented child",
            parents=parents_input(
                spatial=self.first_storey_id,
                placement=self.first_storey_id,
                decomposition=first_assembly["id"]
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        sibling = self.create_transform(
            name="Preserved sibling",
            parents=parents_input(
                spatial=self.first_storey_id,
                placement=self.first_storey_id,
                decomposition=first_assembly["id"]
            )
        )

        sibling_before = self.get_transform(
            sibling["id"]
        )

        spatial_updated = self.set_parents(
            child["id"],
            spatial=self.second_storey_id
        )

        self.assert_identity(
            child,
            spatial_updated
        )

        self.assert_parents(
            child["id"],
            spatial=self.second_storey_id,
            placement=self.first_storey_id,
            decomposition=first_assembly["id"]
        )

        self.assert_matrix(
            spatial_updated["transforms"]["world"],
            matrix(12, 3, 4)
        )

        placement_updated = self.set_parents(
            child["id"],
            placement=self.second_storey_id
        )

        self.assert_identity(
            child,
            placement_updated
        )

        self.assert_matrix(
            placement_updated["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            placement_updated["transforms"]["world"],
            matrix(102, 3, 4)
        )

        decomposition_updated = self.set_parents(
            child["id"],
            decomposition=second_assembly["id"]
        )

        self.assert_identity(
            child,
            decomposition_updated
        )

        self.assert_parents(
            child["id"],
            spatial=self.second_storey_id,
            placement=self.second_storey_id,
            decomposition=second_assembly["id"]
        )

        self.assert_matrix(
            decomposition_updated["transforms"]["world"],
            matrix(102, 3, 4)
        )

        self.assertEqual(
            self.get_transform(sibling["id"]),
            sibling_before
        )

        self.assertNotIn(
            child["id"],
            self.collect_descendants(
                self.first_storey_id
            )
        )

        self.assertNotIn(
            child["id"],
            self.collect_descendants(
                first_assembly["id"]
            )
        )

        self.assertIn(
            child["id"],
            self.collect_descendants(
                self.second_storey_id
            )
        )

        self.assertIn(
            child["id"],
            self.collect_descendants(
                second_assembly["id"]
            )
        )

        # Explicit world-space input must preserve the world
        # placement when changing the placement parent.

        preserved_world = self.update_transform(
            child["id"],
            parents=parents_input(
                spatial=self.second_storey_id,
                placement=self.first_storey_id,
                decomposition=second_assembly["id"]
            ),
            transform=transform_input(
                x=102,
                y=3,
                z=4,
                space="world"
            )
        )

        self.assert_matrix(
            preserved_world["transforms"]["local"],
            matrix(92, 3, 4)
        )

        self.assert_matrix(
            preserved_world["transforms"]["world"],
            matrix(102, 3, 4)
        )

        self.assert_parent_graph()

    def test_parent_detachment(self):
        """Verify independent parent detachment using null."""

        assembly = self.create_transform(
            name="Detachment assembly"
        )

        child = self.create_transform(
            name="Detached child",
            parents=parents_input(
                spatial=self.first_storey_id,
                placement=self.second_storey_id,
                decomposition=assembly["id"]
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        sibling = self.create_transform(
            name="Attached sibling",
            parents=parents_input(
                spatial=self.first_storey_id,
                placement=self.second_storey_id,
                decomposition=assembly["id"]
            )
        )

        sibling_before = self.get_transform(
            sibling["id"]
        )

        spatial_detached = self.set_parents(
            child["id"],
            spatial=None
        )

        self.assert_parents(
            child["id"],
            placement=self.second_storey_id,
            decomposition=assembly["id"]
        )

        self.assert_matrix(
            spatial_detached["transforms"]["world"],
            matrix(102, 3, 4)
        )

        decomposition_detached = self.set_parents(
            child["id"],
            decomposition=None
        )

        self.assert_parents(
            child["id"],
            placement=self.second_storey_id
        )

        self.assert_matrix(
            decomposition_detached["transforms"]["world"],
            matrix(102, 3, 4)
        )

        placement_detached = self.set_parents(
            child["id"],
            placement=None
        )

        self.assert_identity(
            child,
            placement_detached
        )

        self.assert_parents(
            child["id"]
        )

        self.assert_matrix(
            placement_detached["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            placement_detached["transforms"]["world"],
            matrix(2, 3, 4)
        )

        self.assertEqual(
            self.get_transform(sibling["id"]),
            sibling_before
        )

        self.assertNotIn(
            child["id"],
            self.collect_descendants(
                self.first_storey_id
            )
        )

        self.assertNotIn(
            child["id"],
            self.collect_descendants(
                self.second_storey_id
            )
        )

        self.assertNotIn(
            child["id"],
            self.collect_descendants(
                assembly["id"]
            )
        )

        self.save_and_reload()

        reloaded = self.find_transform(
            child["globalId"]
        )

        self.assertEqual(
            reloaded["parents"],
            parents_input()
        )

        self.assert_matrix(
            reloaded["transforms"]["world"],
            matrix(2, 3, 4)
        )

        reloaded_sibling = self.find_transform(
            sibling["globalId"]
        )

        reloaded_assembly = self.find_transform(
            assembly["globalId"]
        )

        self.assert_parents(
            reloaded_sibling["id"],
            spatial=self.first_storey_id,
            placement=self.second_storey_id,
            decomposition=reloaded_assembly["id"]
        )

        self.assert_parent_graph()

    def test_invalid_spatial_parent(self):
        """Verify rejection of incompatible spatial parent types."""

        invalid_parent_ids = [
            self.physical_id,
            self.virtual_id,
            self.project_id
        ]

        child = self.create_transform(
            name="Spatial validation child",
            parents=parents_input(
                spatial=self.first_storey_id
            )
        )

        initial = self.snapshot_hierarchy()

        for invalid_parent_id in invalid_parent_ids:
            with self.subTest(parent_id=invalid_parent_id):
                self.client.expect_error(
                    "transformCreate",
                    self.create_transform_params(
                        name="Invalid spatial child",
                        parents=parents_input(
                            spatial=invalid_parent_id
                        )
                    ),
                    "INVALID_RELATION"
                )

                self.assertEqual(
                    self.snapshot_hierarchy(),
                    initial
                )

                self.assert_rejected_parent_update(
                    child["id"],
                    "spatial",
                    invalid_parent_id
                )

        self.assert_parents(
            child["id"],
            spatial=self.first_storey_id
        )

        valid = self.set_parents(
            child["id"],
            spatial=self.second_storey_id
        )

        self.assertEqual(
            valid["parents"]["spatial"],
            self.second_storey_id
        )

        self.assert_parent_graph()

    def test_invalid_placement_parent(self):
        """Verify rejection of parents without materialized placements."""

        child = self.create_transform(
            name="Placement validation child",
            parents=parents_input(
                placement=self.first_storey_id
            )
        )

        initial = self.snapshot_hierarchy()

        for invalid_parent_id in (
            self.virtual_id,
            self.project_id
        ):
            with self.subTest(parent_id=invalid_parent_id):
                parent = self.get_transform(
                    invalid_parent_id
                )

                self.assertEqual(
                    parent["transforms"]["sourceType"],
                    "virtual"
                )

                self.client.expect_error(
                    "transformCreate",
                    self.create_transform_params(
                        name="Invalid placement child",
                        parents=parents_input(
                            placement=invalid_parent_id
                        )
                    ),
                    "INVALID_RELATION"
                )

                self.assertEqual(
                    self.snapshot_hierarchy(),
                    initial
                )

                self.assert_rejected_parent_update(
                    child["id"],
                    "placement",
                    invalid_parent_id
                )

        self.update_transform(
            self.virtual_id,
            transform=transform_input(
                x=50,
                y=10,
                z=20
            )
        )

        materialized = self.get_transform(
            self.virtual_id
        )

        self.assertEqual(
            materialized["transforms"]["sourceType"],
            "IfcLocalPlacement"
        )

        valid = self.create_transform(
            name="Child of materialized parent",
            parents=parents_input(
                placement=self.virtual_id
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        self.assert_parents(
            valid["id"],
            placement=self.virtual_id
        )

        self.assert_matrix(
            valid["transforms"]["world"],
            matrix(52, 13, 24)
        )

        self.assert_parent_graph()

    def test_self_parent(self):
        """Verify rejection of self-referencing parent relations."""

        transform = self.create_transform(
            name="Self-parent validation"
        )

        initial = self.snapshot_hierarchy()

        for parent_type in PARENT_TYPES:
            with self.subTest(parent_type=parent_type):
                self.assert_rejected_parent_update(
                    transform["id"],
                    parent_type,
                    transform["id"]
                )

        self.assert_rejected_parent_update(
            self.first_storey_id,
            "spatial",
            self.first_storey_id
        )

        self.client.expect_error(
            "transformUpdate",
            {
                "id": transform["id"],
                "parents": parents_input(
                    spatial=transform["id"],
                    placement=transform["id"],
                    decomposition=transform["id"]
                )
            },
            "INVALID_RELATION"
        )

        self.assertEqual(
            self.snapshot_hierarchy(),
            initial
        )

        self.assert_parent_graph()

    def test_direct_cycle(self):
        """Verify rejection of cycles between two transforms."""

        # Spatial containment cycles would require spatial elements
        # to contain other spatial elements through an inappropriate
        # IFC relationship, so valid cycle fixtures use placement
        # and decomposition relationships.

        for parent_type in (
            "placement",
            "decomposition"
        ):
            with self.subTest(parent_type=parent_type):
                first = self.create_transform(
                    name=f"{parent_type} cycle first"
                )

                second = self.create_transform(
                    name=f"{parent_type} cycle second",
                    parents=parents_input(
                        **{parent_type: first["id"]}
                    )
                )

                self.assertEqual(
                    self.get_transform(
                        second["id"]
                    )["parents"][parent_type],
                    first["id"]
                )

                self.assert_rejected_parent_update(
                    first["id"],
                    parent_type,
                    second["id"]
                )

                self.assertIsNone(
                    self.get_transform(
                        first["id"]
                    )["parents"][parent_type]
                )

                self.assertEqual(
                    self.get_transform(
                        second["id"]
                    )["parents"][parent_type],
                    first["id"]
                )

        self.assert_parent_graph()

    def test_indirect_cycle(self):
        """Verify rejection of cycles involving multiple transforms."""

        for parent_type in (
            "placement",
            "decomposition"
        ):
            with self.subTest(parent_type=parent_type):
                first = self.create_transform(
                    name=f"{parent_type} root"
                )

                second = self.create_transform(
                    name=f"{parent_type} level 1",
                    parents=parents_input(
                        **{parent_type: first["id"]}
                    )
                )

                third = self.create_transform(
                    name=f"{parent_type} level 2",
                    parents=parents_input(
                        **{parent_type: second["id"]}
                    )
                )

                fourth = self.create_transform(
                    name=f"{parent_type} level 3",
                    parents=parents_input(
                        **{parent_type: third["id"]}
                    )
                )

                self.assert_rejected_parent_update(
                    first["id"],
                    parent_type,
                    fourth["id"]
                )

                self.assert_rejected_parent_update(
                    second["id"],
                    parent_type,
                    fourth["id"]
                )

                self.assert_rejected_parent_update(
                    first["id"],
                    parent_type,
                    third["id"]
                )

                self.assertEqual(
                    self.collect_descendants(
                        first["id"]
                    ),
                    {
                        second["id"],
                        third["id"],
                        fourth["id"]
                    }
                )

                self.assertEqual(
                    self.collect_descendants(
                        second["id"]
                    ),
                    {
                        third["id"],
                        fourth["id"]
                    }
                )

        self.assert_parent_graph()

    def test_deep_hierarchy(self):
        """Verify placement propagation through a deep hierarchy."""

        depth = 32

        root = self.create_transform(
            name="Deep hierarchy root"
        )

        chain = [root]
        parent_id = root["id"]

        for level in range(1, depth + 1):
            child = self.create_transform(
                name=f"Hierarchy level {level}",
                parents=parents_input(
                    placement=parent_id,
                    decomposition=parent_id
                ),
                transform=transform_input(
                    x=1,
                    y=2,
                    z=3
                )
            )

            self.assert_parents(
                child["id"],
                placement=parent_id,
                decomposition=parent_id
            )

            self.assert_matrix(
                child["transforms"]["local"],
                matrix(1, 2, 3)
            )

            self.assert_matrix(
                child["transforms"]["world"],
                matrix(
                    level,
                    level * 2,
                    level * 3
                )
            )

            chain.append(child)
            parent_id = child["id"]

        leaf = chain[-1]

        domain = self.create_domain(
            leaf["id"],
            name="Deep hierarchy domain"
        )

        self.assertEqual(
            self.collect_descendants(root["id"]),
            {
                transform["id"]
                for transform in chain[1:]
            }
        )

        self.assert_bounds(
            self.client.tessellate(
                leaf["id"],
                kind="DOMAIN"
            ),
            (
                depth,
                depth * 2,
                depth * 3,
                depth + 5,
                depth * 2 + 3,
                depth * 3 + 2
            )
        )

        self.assert_empty_mesh(
            self.client.tessellate(
                root["id"],
                kind="DOMAIN",
                include_children=False
            )
        )

        recursive_mesh = self.client.tessellate(
            root["id"],
            kind="DOMAIN",
            include_children=True
        )

        individual_mesh = self.client.tessellate(
            leaf["id"],
            kind="DOMAIN",
            geometry_id=domain["id"]
        )

        self.assert_equivalent_meshes(
            recursive_mesh,
            individual_mesh
        )

        self.update_transform(
            root["id"],
            transform=transform_input(
                x=100,
                y=200,
                z=300,
                space="world"
            )
        )

        for level, transform in enumerate(chain):
            with self.subTest(level=level):
                retrieved = self.get_transform(
                    transform["id"]
                )

                self.assert_identity(
                    transform,
                    retrieved
                )

                self.assert_matrix(
                    retrieved["transforms"]["world"],
                    matrix(
                        100 + level,
                        200 + level * 2,
                        300 + level * 3
                    )
                )

        local_mesh = self.client.tessellate(
            root["id"],
            kind="DOMAIN",
            space="local",
            include_children=True
        )

        world_mesh = self.client.tessellate(
            root["id"],
            kind="DOMAIN",
            space="world",
            include_children=True
        )

        self.assert_bounds(
            local_mesh,
            (
                depth,
                depth * 2,
                depth * 3,
                depth + 5,
                depth * 2 + 3,
                depth * 3 + 2
            )
        )

        self.assert_bounds(
            world_mesh,
            (
                100 + depth,
                200 + depth * 2,
                300 + depth * 3,
                100 + depth + 5,
                200 + depth * 2 + 3,
                300 + depth * 3 + 2
            )
        )

        self.assert_parent_graph()

        self.save_and_reload()

        reloaded_root = self.find_transform(
            root["globalId"]
        )

        reloaded_leaf = self.find_transform(
            leaf["globalId"]
        )

        self.assertEqual(
            len(
                self.collect_descendants(
                    reloaded_root["id"]
                )
            ),
            depth
        )

        self.assert_matrix(
            reloaded_leaf["transforms"]["world"],
            matrix(
                100 + depth,
                200 + depth * 2,
                300 + depth * 3
            )
        )

        self.assert_equivalent_meshes(
            world_mesh,
            self.client.tessellate(
                reloaded_root["id"],
                kind="DOMAIN",
                include_children=True
            )
        )

        self.delete_transform(
            reloaded_root["id"]
        )

        remaining_global_ids = {
            transform["globalId"]
            for transform in self.list_transforms()
        }

        for transform in chain:
            self.assertNotIn(
                transform["globalId"],
                remaining_global_ids
            )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assertEqual(
            len(self.list_transforms()),
            5
        )

        self.assert_parent_graph()

    def test_multiple_parent_paths(self):
        """Verify that descendants reached through multiple paths are processed once."""

        initial = self.snapshot_hierarchy()

        root = self.create_transform(
            name="Diamond root",
            transform=transform_input(
                x=10
            )
        )

        left = self.create_transform(
            name="Diamond left",
            parents=parents_input(
                placement=root["id"],
                decomposition=root["id"]
            ),
            transform=transform_input(
                x=2
            )
        )

        right = self.create_transform(
            name="Diamond right",
            parents=parents_input(
                placement=root["id"],
                decomposition=root["id"]
            ),
            transform=transform_input(
                x=3
            )
        )

        leaf = self.create_transform(
            name="Diamond leaf",
            parents=parents_input(
                placement=left["id"],
                decomposition=right["id"]
            ),
            transform=transform_input(
                x=4
            )
        )

        domain = self.create_domain(
            leaf["id"],
            name="Diamond domain"
        )

        expected_descendants = {
            left["id"],
            right["id"],
            leaf["id"]
        }

        self.assertEqual(
            self.collect_descendants(root["id"]),
            expected_descendants
        )

        self.assertIn(
            leaf["id"],
            self.collect_descendants(left["id"])
        )

        self.assertIn(
            leaf["id"],
            self.collect_descendants(right["id"])
        )

        self.assert_matrix(
            leaf["transforms"]["world"],
            matrix(16, 0, 0)
        )

        individual_mesh = self.client.tessellate(
            leaf["id"],
            kind="DOMAIN"
        )

        recursive_mesh = self.client.tessellate(
            root["id"],
            kind="DOMAIN",
            include_children=True
        )

        self.assert_bounds(
            individual_mesh,
            (16, 0, 0, 21, 3, 2)
        )

        self.assertEqual(
            recursive_mesh["vertexCount"],
            individual_mesh["vertexCount"]
        )

        self.assertEqual(
            recursive_mesh["indexCount"],
            individual_mesh["indexCount"]
        )

        self.assert_equivalent_meshes(
            recursive_mesh,
            individual_mesh
        )

        self.assert_empty_mesh(
            self.client.tessellate(
                root["id"],
                kind="DOMAIN",
                include_children=False
            )
        )

        duplicate = self.duplicate_transform(
            root["id"],
            include_children=True
        )

        expected_sources = {
            root["id"],
            left["id"],
            right["id"],
            leaf["id"]
        }

        transform_mapping = duplicate["transforms"]
        geometry_mapping = duplicate["geometries"]

        self.assertEqual(
            set(transform_mapping),
            expected_sources
        )

        self.assertEqual(
            len(transform_mapping),
            4
        )

        self.assertEqual(
            len(set(transform_mapping.values())),
            4
        )

        self.assertEqual(
            set(geometry_mapping),
            {domain["id"]}
        )

        self.assertFalse(
            set(transform_mapping)
            & set(transform_mapping.values())
        )

        duplicate_root_id = duplicate["rootId"]

        duplicate_left_id = transform_mapping[
            left["id"]
        ]

        duplicate_right_id = transform_mapping[
            right["id"]
        ]

        duplicate_leaf_id = transform_mapping[
            leaf["id"]
        ]

        duplicate_domain_id = geometry_mapping[
            domain["id"]
        ]

        self.assertEqual(
            duplicate_root_id,
            transform_mapping[root["id"]]
        )

        self.assert_parents(
            duplicate_left_id,
            placement=duplicate_root_id,
            decomposition=duplicate_root_id
        )

        self.assert_parents(
            duplicate_right_id,
            placement=duplicate_root_id,
            decomposition=duplicate_root_id
        )

        self.assert_parents(
            duplicate_leaf_id,
            placement=duplicate_left_id,
            decomposition=duplicate_right_id
        )

        copied_domain = self.get_geometry(
            duplicate_domain_id
        )

        self.assertEqual(
            copied_domain["parentId"],
            duplicate_leaf_id
        )

        self.assertNotEqual(
            copied_domain["globalId"],
            domain["globalId"]
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            2
        )

        original_local_mesh = self.client.tessellate(
            root["id"],
            kind="DOMAIN",
            space="local",
            include_children=True
        )

        duplicate_local_mesh = self.client.tessellate(
            duplicate_root_id,
            kind="DOMAIN",
            space="local",
            include_children=True
        )

        self.assert_equivalent_meshes(
            original_local_mesh,
            duplicate_local_mesh
        )

        self.delete_transform(
            duplicate_root_id
        )

        for identifier in transform_mapping.values():
            self.client.expect_error(
                "transformGet",
                {"id": identifier},
                "ENTITY_NOT_FOUND"
            )

        self.client.expect_error(
            "geometryGet",
            {"id": duplicate_domain_id},
            "ENTITY_NOT_FOUND"
        )

        for identifier in expected_sources:
            self.assertEqual(
                self.get_transform(identifier)["id"],
                identifier
            )

        self.assertEqual(
            self.get_geometry(domain["id"]),
            domain
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            1
        )

        self.delete_transform(
            root["id"]
        )

        for identifier in expected_sources:
            self.client.expect_error(
                "transformGet",
                {"id": identifier},
                "ENTITY_NOT_FOUND"
            )

        self.client.expect_error(
            "geometryGet",
            {"id": domain["id"]},
            "ENTITY_NOT_FOUND"
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assertEqual(
            self.snapshot_hierarchy(),
            initial
        )

        self.assert_parent_graph()

    def test_hierarchy_persistence(self):
        """Verify preservation of all three parent relations after reload."""

        assembly = self.create_transform(
            name="Persistent assembly",
            transform=transform_input(
                x=500,
                y=600,
                z=700
            )
        )

        placement_parent = self.create_transform(
            name="Persistent placement parent",
            parents=parents_input(
                placement=self.second_storey_id
            ),
            transform=transform_input(
                x=10,
                y=20,
                z=30
            )
        )

        child = self.create_transform(
            name="Persistent child",
            parents=parents_input(
                spatial=self.first_storey_id,
                placement=placement_parent["id"],
                decomposition=assembly["id"]
            ),
            transform=transform_input(
                x=2,
                y=3,
                z=4
            )
        )

        grandchild = self.create_transform(
            name="Persistent grandchild",
            parents=parents_input(
                spatial=self.second_storey_id,
                placement=child["id"],
                decomposition=child["id"]
            ),
            transform=transform_input(
                x=5,
                y=6,
                z=7
            )
        )

        domain = self.create_domain(
            grandchild["id"],
            name="Persistent hierarchy domain"
        )

        self.assert_matrix(
            child["transforms"]["world"],
            matrix(112, 23, 34)
        )

        self.assert_matrix(
            grandchild["transforms"]["world"],
            matrix(117, 29, 41)
        )

        mesh_before = self.client.tessellate(
            placement_parent["id"],
            kind="DOMAIN",
            include_children=True
        )

        self.assert_bounds(
            mesh_before,
            (117, 29, 41, 122, 32, 43)
        )

        expected_global_ids = {
            transform["globalId"]
            for transform in self.list_transforms()
        }

        result = self.save_and_reload()

        self.assertEqual(
            result["transformCount"],
            9
        )

        self.assertEqual(
            result["geometryCount"],
            2
        )

        self.assertEqual(
            {
                transform["globalId"]
                for transform in self.list_transforms()
            },
            expected_global_ids
        )

        reloaded_assembly = self.find_transform(
            assembly["globalId"]
        )

        reloaded_placement_parent = self.find_transform(
            placement_parent["globalId"]
        )

        reloaded_child = self.find_transform(
            child["globalId"]
        )

        reloaded_grandchild = self.find_transform(
            grandchild["globalId"]
        )

        self.assert_parents(
            reloaded_assembly["id"]
        )

        self.assert_parents(
            reloaded_placement_parent["id"],
            placement=self.second_storey_id
        )

        self.assert_parents(
            reloaded_child["id"],
            spatial=self.first_storey_id,
            placement=reloaded_placement_parent["id"],
            decomposition=reloaded_assembly["id"]
        )

        self.assert_parents(
            reloaded_grandchild["id"],
            spatial=self.second_storey_id,
            placement=reloaded_child["id"],
            decomposition=reloaded_child["id"]
        )

        self.assert_matrix(
            reloaded_child["transforms"]["local"],
            matrix(2, 3, 4)
        )

        self.assert_matrix(
            reloaded_child["transforms"]["world"],
            matrix(112, 23, 34)
        )

        self.assert_matrix(
            reloaded_grandchild["transforms"]["local"],
            matrix(5, 6, 7)
        )

        self.assert_matrix(
            reloaded_grandchild["transforms"]["world"],
            matrix(117, 29, 41)
        )

        domains = self.list_geometries(
            parent_id=reloaded_grandchild["id"],
            kind="DOMAIN"
        )

        self.assertEqual(
            len(domains),
            1
        )

        self.assertEqual(
            domains[0]["globalId"],
            domain["globalId"]
        )

        mesh_after = self.client.tessellate(
            reloaded_placement_parent["id"],
            kind="DOMAIN",
            include_children=True
        )

        self.assert_equivalent_meshes(
            mesh_before,
            mesh_after
        )

        self.update_transform(
            reloaded_placement_parent["id"],
            transform=transform_input(
                x=20,
                y=30,
                z=40
            )
        )

        moved_child = self.get_transform(
            reloaded_child["id"]
        )

        moved_grandchild = self.get_transform(
            reloaded_grandchild["id"]
        )

        self.assert_matrix(
            moved_child["transforms"]["world"],
            matrix(122, 33, 44)
        )

        self.assert_matrix(
            moved_grandchild["transforms"]["world"],
            matrix(127, 39, 51)
        )

        moved_mesh = self.client.tessellate(
            reloaded_placement_parent["id"],
            kind="DOMAIN",
            include_children=True
        )

        self.assert_bounds(
            moved_mesh,
            (127, 39, 51, 132, 42, 53)
        )

        self.save_and_reload()

        final_assembly = self.find_transform(
            assembly["globalId"]
        )

        final_placement_parent = self.find_transform(
            placement_parent["globalId"]
        )

        final_child = self.find_transform(
            child["globalId"]
        )

        final_grandchild = self.find_transform(
            grandchild["globalId"]
        )

        self.assert_parents(
            final_child["id"],
            spatial=self.first_storey_id,
            placement=final_placement_parent["id"],
            decomposition=final_assembly["id"]
        )

        self.assert_parents(
            final_grandchild["id"],
            spatial=self.second_storey_id,
            placement=final_child["id"],
            decomposition=final_child["id"]
        )

        self.assert_matrix(
            final_grandchild["transforms"]["world"],
            matrix(127, 39, 51)
        )

        final_mesh = self.client.tessellate(
            final_placement_parent["id"],
            kind="DOMAIN",
            include_children=True
        )

        self.assert_equivalent_meshes(
            moved_mesh,
            final_mesh
        )

        self.assert_parent_graph()


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