import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from collections import Counter
from pathlib import Path


DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 30
PARENT_GLOBAL_ID = "0000000000000000000001"
PROJECT_GLOBAL_ID = "0000000000000000000002"

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

DEFAULT_PROFILE = [
    [0, 0],
    [5, 0],
    [5, 3],
    [0, 3]
]

BASELINE_BOUNDS = (-1, -0.5, 0, 1, 0.5, 1)
DOMAIN_BOUNDS = (0, 0, 0, 5, 3, 2)

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('domain-test.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
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
#16=IFCPROJECT('0000000000000000000002',$,'Domain test project',$,$,$,$,(#6),#8);
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


def mesh_bounds(mesh):
    vertices = mesh["vertices"]

    if not vertices:
        raise AssertionError("Mesh has no vertices")

    return tuple(
        function(vertices[axis::3])
        for function in (min, max)
        for axis in range(3)
    )


def mesh_points(mesh):
    vertices = mesh["vertices"]

    return [
        tuple(vertices[index:index + 3])
        for index in range(0, len(vertices), 3)
    ]


def mesh_signature(mesh, precision=7):
    points = mesh_points(mesh)
    indices = mesh["indices"]

    triangles = []

    for offset in range(0, len(indices), 3):
        triangle = []

        for index in indices[offset:offset + 3]:
            point = tuple(
                round(value, precision)
                for value in points[index]
            )

            triangle.append(point)

        triangles.append(tuple(sorted(triangle)))

    return Counter(triangles)


def mesh_volume(mesh):
    points = mesh_points(mesh)
    indices = mesh["indices"]

    volume = 0.0

    for offset in range(0, len(indices), 3):
        a, b, c = (
            points[index]
            for index in indices[offset:offset + 3]
        )

        cross = (
            b[1] * c[2] - b[2] * c[1],
            b[2] * c[0] - b[0] * c[2],
            b[0] * c[1] - b[1] * c[0]
        )

        volume += sum(
            a[axis] * cross[axis]
            for axis in range(3)
        )

    return abs(volume) / 6.0


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
        finally:
            self.responses.put(None)

    def send(self, command, params=None):
        if self.process.poll() is not None:
            raise RuntimeError(
                f"CLI is not running: exit code {self.process.returncode}"
            )

        self.counter += 1
        request_id = str(self.counter)

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
            line = self.responses.get(
                timeout=self.timeout
            )
        except queue.Empty as error:
            raise TimeoutError(
                f"No CLI response for request {request_id}"
            ) from error

        if line is None:
            raise RuntimeError(
                "CLI terminated before returning a response"
            )

        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Invalid JSONL response: {line!r}"
            ) from error

        if not isinstance(response, dict):
            raise AssertionError(
                f"Response must be a JSON object: {response}"
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
        request_id = self.send(command, params)
        response = self.read(request_id)

        if not response["ok"]:
            raise RuntimeError(
                f"{command}: {response['error']}"
            )

        return response["result"]

    def expect_error(self, command, params, code):
        request_id = self.send(command, params)
        response = self.read(request_id)

        if response["ok"]:
            raise AssertionError(
                f"{command} unexpectedly succeeded: {response}"
            )

        if response["error"]["code"] != code:
            raise AssertionError(
                f"Expected {code}, received {response['error']}"
            )

        return response["error"]

    def tessellate(
        self,
        transform_id,
        kind="SOLID",
        geometry_id=None,
        space="world",
        include_children=False,
        include_normals=True,
        include_materials=False
    ):
        options = {
            "space": space,
            "includeNormals": include_normals,
            "includeMaterials": include_materials,
            "includeChildren": include_children
        }

        if kind is not None:
            options["kind"] = kind

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
            "materials": [],
            "vertexCount": 0,
            "indexCount": 0
        }

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.normals": 3,
            "mesh.materials": 4,
            "mesh.end": 5
        }

        last_event = -1
        begin_received = False
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
                    f"Unknown mesh event: {event}"
                )

            current_event = event_order[event_name]

            if current_event < last_event:
                raise AssertionError(
                    f"Invalid mesh event order: {event}"
                )

            last_event = current_event

            if event_name == "mesh.begin":
                if begin_received:
                    raise AssertionError(
                        "Duplicate mesh.begin event"
                    )

                begin_received = True

                mesh["vertexCount"] = event["vertexCount"]
                mesh["indexCount"] = event["indexCount"]

                for count in (
                    mesh["vertexCount"],
                    mesh["indexCount"]
                ):
                    if type(count) is not int or count < 0:
                        raise AssertionError(
                            f"Invalid mesh count: {event}"
                        )

                continue

            if not begin_received:
                raise AssertionError(
                    "Mesh data received before mesh.begin"
                )

            if event_name in (
                "mesh.vertices",
                "mesh.indices",
                "mesh.normals"
            ):
                key = event_name.split(".")[1]

                if key == "normals" and not include_normals:
                    raise AssertionError(
                        "Unexpected mesh.normals event"
                    )

                if event["offset"] != len(mesh[key]):
                    raise AssertionError(
                        f"Invalid {key} chunk offset: {event}"
                    )

                values = event["values"]

                if not isinstance(values, list):
                    raise AssertionError(
                        f"Invalid mesh chunk: {event}"
                    )

                mesh[key].extend(values)

            elif event_name == "mesh.materials":
                if not include_materials:
                    raise AssertionError(
                        "Unexpected mesh.materials event"
                    )

                if materials_received:
                    raise AssertionError(
                        "Duplicate mesh.materials event"
                    )

                materials_received = True
                mesh["materials"] = event["groups"]

                if not isinstance(mesh["materials"], list):
                    raise AssertionError(
                        f"Invalid material groups: {event}"
                    )

            elif event_name == "mesh.end":
                break

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError(
                "Vertex count does not match mesh data"
            )

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError(
                "Index count does not match mesh data"
            )

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError(
                "Mesh indices are not organized into triangles"
            )

        for value in mesh["vertices"]:
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
            ):
                raise AssertionError(
                    f"Invalid vertex coordinate: {value}"
                )

        for index in mesh["indices"]:
            if (
                type(index) is not int
                or not 0 <= index < mesh["vertexCount"]
            ):
                raise AssertionError(
                    f"Invalid vertex index: {index}"
                )

        if len(mesh["normals"]) % 3 != 0:
            raise AssertionError(
                "Invalid normal array length"
            )

        for value in mesh["normals"]:
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
            ):
                raise AssertionError(
                    f"Invalid normal component: {value}"
                )

        if include_materials and not materials_received:
            raise AssertionError(
                "Missing mesh.materials event"
            )

        return mesh

    def close(self):
        try:
            if self.process.poll() is None:
                self.request("exit")
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

        if self.process.stdin is not None:
            self.process.stdin.close()

        self.reader.join(timeout=5)

        if self.process.stdout is not None:
            self.process.stdout.close()


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-domains-"
        )

        self.addCleanup(self.directory.cleanup)

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

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        capabilities = self.client.request(
            "getCapabilities"
        )

        self.assertIn(
            "DOMAIN",
            capabilities["supportedGeometryKinds"]
        )

        self.assertIn(
            "SOLID",
            capabilities["supportedGeometryKinds"]
        )

        self.load_model(self.input_path)

    def tearDown(self):
        self.client.close()

    def load_model(self, path):
        result = self.client.request(
            "load",
            {"path": str(path)}
        )

        self.assertEqual(
            result["schema"].upper(),
            "IFC4"
        )

        self.parent_id = self.find_transform(
            PARENT_GLOBAL_ID
        )

        return result

    def save_model(self):
        self.client.request(
            "save",
            {"path": str(self.output_path)}
        )

        self.assertTrue(
            self.output_path.is_file()
        )

        self.assertGreater(
            self.output_path.stat().st_size,
            0
        )

    def save_and_reload(self):
        self.save_model()

        return self.load_model(
            self.output_path
        )

    def find_transform(self, global_id):
        transforms = self.client.request(
            "transformGetAll"
        )

        matches = [
            transform
            for transform in transforms
            if transform["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one transform with GlobalId {global_id}"
        )

        return matches[0]["id"]

    def find_geometry(self, global_id):
        geometries = self.list_geometries(
            kind="ALL"
        )

        matches = [
            geometry
            for geometry in geometries
            if geometry["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one geometry with GlobalId {global_id}"
        )

        return matches[0]["id"]

    def list_geometries(
        self,
        kind=None,
        parent_id=None
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

    def domain_params(
        self,
        name="Simulation domain",
        parent_id=None,
        profile=None,
        depth=2,
        transform=None,
        space="parent",
        operation="ADD"
    ):
        return {
            "parentId": (
                self.parent_id
                if parent_id is None
                else parent_id
            ),
            "name": name,
            "kind": "DOMAIN",
            "transform": {
                "space": space,
                "matrix": (
                    matrix()
                    if transform is None
                    else transform
                )
            },
            "profile": (
                DEFAULT_PROFILE
                if profile is None
                else profile
            ),
            "depth": depth,
            "operation": operation
        }

    def create_domain(self, **kwargs):
        return self.client.request(
            "geometryCreate",
            self.domain_params(**kwargs)
        )

    def get_geometry(self, geometry_id):
        return self.client.request(
            "geometryGet",
            {"id": geometry_id}
        )

    def get_transform(self, transform_id=None):
        return self.client.request(
            "transformGet",
            {
                "id": (
                    self.parent_id
                    if transform_id is None
                    else transform_id
                )
            }
        )

    def tessellate(
        self,
        kind="SOLID",
        geometry_id=None,
        transform_id=None,
        **kwargs
    ):
        return self.client.tessellate(
            transform_id=(
                self.parent_id
                if transform_id is None
                else transform_id
            ),
            kind=kind,
            geometry_id=geometry_id,
            **kwargs
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
                msg=(
                    f"Invalid bounding box component {index}: "
                    f"{actual} != {expected}"
                )
            )

    def assert_matrix(self, actual, expected):
        self.assertEqual(
            len(actual),
            16
        )

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertAlmostEqual(
                value,
                reference,
                places=7,
                msg=f"Invalid matrix component {index}"
            )

    def assert_closed(self, mesh):
        points = [
            tuple(round(value, 7) for value in point)
            for point in mesh_points(mesh)
        ]

        indices = mesh["indices"]
        edges = Counter()

        self.assertTrue(
            points,
            "Mesh has no vertices"
        )

        self.assertTrue(
            indices,
            "Mesh has no triangles"
        )

        for offset in range(0, len(indices), 3):
            a, b, c = (
                points[index]
                for index in indices[offset:offset + 3]
            )

            self.assertEqual(
                len({a, b, c}),
                3,
                "Degenerate triangle detected"
            )

            for edge in (
                (a, b),
                (b, c),
                (c, a)
            ):
                edges[tuple(sorted(edge))] += 1

        invalid_edges = {
            edge: count
            for edge, count in edges.items()
            if count != 2
        }

        self.assertFalse(
            invalid_edges,
            f"Mesh is not watertight: {invalid_edges}"
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

        self.assertEqual(
            mesh["materials"],
            []
        )

    def assert_equivalent_meshes(self, first, second):
        self.assertEqual(
            first["indexCount"],
            second["indexCount"]
        )

        self.assertEqual(
            mesh_signature(first),
            mesh_signature(second),
            "Meshes are not geometrically equivalent"
        )

    def assert_domain(
        self,
        geometry,
        name,
        profile=None,
        depth=2,
        parent_id=None,
        local_matrix=None
    ):
        self.assertTrue(
            geometry["id"].startswith("geometry:")
        )

        self.assertIsInstance(
            geometry["ifcEntityId"],
            int
        )

        self.assertGreater(
            geometry["ifcEntityId"],
            0
        )

        self.assertIsInstance(
            geometry["globalId"],
            str
        )

        self.assertEqual(
            len(geometry["globalId"]),
            22
        )

        self.assertEqual(
            geometry["source"],
            "editor"
        )

        self.assertIs(
            geometry["editable"],
            True
        )

        self.assertEqual(
            geometry["ifcClass"],
            "IfcSpatialZone"
        )

        self.assertEqual(
            geometry["kind"],
            "DOMAIN"
        )

        self.assertEqual(
            geometry["operation"],
            "ADD"
        )

        self.assertEqual(
            geometry["parentId"],
            self.parent_id if parent_id is None else parent_id
        )

        self.assertEqual(
            geometry["name"],
            name
        )

        self.assertEqual(
            geometry["profile"],
            DEFAULT_PROFILE if profile is None else profile
        )

        self.assertAlmostEqual(
            geometry["depth"],
            depth,
            places=7
        )

        self.assertEqual(
            geometry["materials"],
            []
        )

        self.assertIs(
            geometry["transforms"]["editable"],
            True
        )

        self.assertEqual(
            geometry["transforms"]["sourceType"],
            "IfcLocalPlacement"
        )

        self.assert_matrix(
            geometry["transforms"]["local"],
            matrix() if local_matrix is None else local_matrix
        )

    def test_baseline_mesh(self):
        """Verify the initial physical solid tessellation."""

        mesh = self.tessellate(
            kind="SOLID"
        )

        self.assert_bounds(
            mesh,
            BASELINE_BOUNDS
        )

        self.assert_closed(mesh)

        self.assertAlmostEqual(
            mesh_volume(mesh),
            2.0,
            places=6
        )

        self.assertGreater(
            mesh["vertexCount"],
            0
        )

        self.assertGreater(
            mesh["indexCount"],
            0
        )

        default_mesh = self.tessellate(
            kind=None
        )

        self.assert_equivalent_meshes(
            mesh,
            default_mesh
        )

        self.assert_empty_mesh(
            self.tessellate(kind="DOMAIN")
        )

    def test_domain_creation(self):
        """Verify the creation of an editable spatial domain."""

        transforms_before = self.client.request(
            "transformGetAll"
        )

        domain = self.create_domain(
            name="Domain A"
        )

        self.assert_domain(
            domain,
            name="Domain A"
        )

        retrieved = self.get_geometry(
            domain["id"]
        )

        self.assertEqual(
            retrieved,
            domain
        )

        transform = self.get_transform()

        self.assertIn(
            domain["id"],
            transform["geometries"]
        )

        transforms_after = self.client.request(
            "transformGetAll"
        )

        self.assertEqual(
            transforms_before,
            transforms_after,
            "A managed domain must not create an indexed transform"
        )

    def test_geometry_filtering(self):
        """Verify independent SOLID and DOMAIN geometry filters."""

        domain = self.create_domain(
            name="Domain A"
        )

        solids = self.list_geometries(
            kind="SOLID",
            parent_id=self.parent_id
        )

        domains = self.list_geometries(
            kind="DOMAIN",
            parent_id=self.parent_id
        )

        all_geometries = self.list_geometries(
            kind="ALL",
            parent_id=self.parent_id
        )

        default_geometries = self.list_geometries(
            parent_id=self.parent_id
        )

        self.assertEqual(
            len(solids),
            1
        )

        self.assertEqual(
            len(domains),
            1
        )

        self.assertEqual(
            len(all_geometries),
            2
        )

        self.assertEqual(
            domains[0]["id"],
            domain["id"]
        )

        self.assertTrue(
            all(item["kind"] == "SOLID" for item in solids)
        )

        self.assertTrue(
            all(item["kind"] == "DOMAIN" for item in domains)
        )

        solid_ids = {
            item["id"]
            for item in solids
        }

        domain_ids = {
            item["id"]
            for item in domains
        }

        all_ids = {
            item["id"]
            for item in all_geometries
        }

        default_ids = {
            item["id"]
            for item in default_geometries
        }

        self.assertFalse(
            solid_ids & domain_ids
        )

        self.assertEqual(
            all_ids,
            solid_ids | domain_ids
        )

        self.assertEqual(
            default_ids,
            all_ids
        )

        global_domains = self.list_geometries(
            kind="DOMAIN"
        )

        self.assertEqual(
            len(global_domains),
            1
        )

        self.assertEqual(
            global_domains[0]["id"],
            domain["id"]
        )

        project_id = self.find_transform(
            PROJECT_GLOBAL_ID
        )

        self.assertEqual(
            self.list_geometries(
                kind="DOMAIN",
                parent_id=project_id
            ),
            []
        )

    def test_domain_mesh(self):
        """Verify an independent and watertight domain mesh."""

        baseline = self.tessellate(
            kind="SOLID"
        )

        domain = self.create_domain(
            name="Domain A"
        )

        solid_mesh = self.tessellate(
            kind="SOLID"
        )

        self.assert_equivalent_meshes(
            baseline,
            solid_mesh
        )

        domain_mesh = self.tessellate(
            kind="DOMAIN",
            geometry_id=domain["id"],
            include_materials=True
        )

        self.assert_bounds(
            domain_mesh,
            DOMAIN_BOUNDS
        )

        self.assert_closed(
            domain_mesh
        )

        self.assertAlmostEqual(
            mesh_volume(domain_mesh),
            30.0,
            places=6
        )

        self.assertEqual(
            domain_mesh["materials"],
            []
        )

        self.assertGreater(
            len(domain_mesh["normals"]),
            0
        )

        for offset in range(
            0,
            len(domain_mesh["normals"]),
            3
        ):
            normal = domain_mesh["normals"][offset:offset + 3]

            length = math.sqrt(
                sum(value * value for value in normal)
            )

            self.assertAlmostEqual(
                length,
                1.0,
                places=6
            )

    def test_domain_selection(self):
        """Verify individual tessellation of multiple domains."""

        first = self.create_domain(
            name="Domain A"
        )

        second = self.create_domain(
            name="Domain B",
            transform=matrix(20, 0, 0)
        )

        first_mesh = self.tessellate(
            kind="DOMAIN",
            geometry_id=first["id"]
        )

        second_mesh = self.tessellate(
            kind="DOMAIN",
            geometry_id=second["id"]
        )

        combined_mesh = self.tessellate(
            kind="DOMAIN"
        )

        self.assert_bounds(
            first_mesh,
            DOMAIN_BOUNDS
        )

        self.assert_bounds(
            second_mesh,
            (20, 0, 0, 25, 3, 2)
        )

        self.assert_bounds(
            combined_mesh,
            (0, 0, 0, 25, 3, 2)
        )

        self.assert_closed(
            first_mesh
        )

        self.assert_closed(
            second_mesh
        )

        self.assert_closed(
            combined_mesh
        )

        self.assertEqual(
            combined_mesh["indexCount"],
            first_mesh["indexCount"] + second_mesh["indexCount"]
        )

        self.assertEqual(
            mesh_signature(combined_mesh),
            mesh_signature(first_mesh) + mesh_signature(second_mesh)
        )

        self.assertAlmostEqual(
            mesh_volume(combined_mesh),
            60.0,
            places=6
        )

    def test_domain_update(self):
        """Verify domain name, profile, depth and placement updates."""

        domain = self.create_domain(
            name="Domain A"
        )

        domain_id = domain["id"]

        original_global_id = domain["globalId"]

        baseline = self.tessellate(
            kind="SOLID"
        )

        renamed = self.client.request(
            "geometryUpdate",
            {
                "id": domain_id,
                "name": "Updated domain"
            }
        )

        self.assert_domain(
            renamed,
            name="Updated domain"
        )

        profile = [
            [0, 0],
            [8, 0],
            [8, 6],
            [0, 6]
        ]

        updated = self.client.request(
            "geometryUpdate",
            {
                "id": domain_id,
                "depth": 4,
                "profile": profile,
                "transform": {
                    "space": "parent",
                    "matrix": matrix(2, 3, 1)
                }
            }
        )

        self.assert_domain(
            updated,
            name="Updated domain",
            profile=profile,
            depth=4,
            local_matrix=matrix(2, 3, 1)
        )

        self.assertEqual(
            updated["id"],
            domain_id
        )

        self.assertEqual(
            updated["globalId"],
            original_global_id
        )

        self.assertEqual(
            self.get_geometry(domain_id),
            updated
        )

        mesh = self.tessellate(
            kind="DOMAIN",
            geometry_id=domain_id
        )

        self.assert_bounds(
            mesh,
            (2, 3, 1, 10, 9, 5)
        )

        self.assert_closed(mesh)

        self.assertAlmostEqual(
            mesh_volume(mesh),
            192.0,
            places=6
        )

        self.assert_equivalent_meshes(
            baseline,
            self.tessellate(kind="SOLID")
        )

    def test_invalid_operation(self):
        """Verify that subtractive domain operations are rejected."""

        initial_geometries = self.list_geometries(
            kind="ALL"
        )

        self.client.expect_error(
            "geometryCreate",
            self.domain_params(
                name="Invalid domain",
                operation="SUBTRACT"
            ),
            "INVALID_PARAMETERS"
        )

        self.assertEqual(
            self.list_geometries(kind="ALL"),
            initial_geometries
        )

        domain = self.create_domain(
            name="Domain A"
        )

        domain_id = domain["id"]

        mesh_before = self.tessellate(
            kind="DOMAIN",
            geometry_id=domain_id
        )

        self.client.expect_error(
            "geometryUpdate",
            {
                "id": domain_id,
                "operation": "SUBTRACT"
            },
            "INVALID_PARAMETERS"
        )

        self.client.expect_error(
            "geometryUpdate",
            {
                "id": domain_id,
                "name": "Invalid name update",
                "operation": "SUBTRACT"
            },
            "INVALID_PARAMETERS"
        )

        self.assertEqual(
            self.get_geometry(domain_id),
            domain
        )

        mesh_after = self.tessellate(
            kind="DOMAIN",
            geometry_id=domain_id
        )

        self.assert_equivalent_meshes(
            mesh_before,
            mesh_after
        )

        unchanged = self.client.request(
            "geometryUpdate",
            {
                "id": domain_id,
                "operation": "ADD"
            }
        )

        self.assertEqual(
            unchanged,
            domain
        )

    def test_parent_movement(self):
        """Verify domain placement after moving its parent transform."""

        domain = self.create_domain(
            name="Domain A",
            transform=matrix(2, 3, 1)
        )

        domain_id = domain["id"]

        self.client.request(
            "transformUpdate",
            {
                "id": self.parent_id,
                "transform": {
                    "space": "world",
                    "matrix": matrix(100, 0, 0)
                }
            }
        )

        updated = self.get_geometry(
            domain_id
        )

        self.assert_matrix(
            updated["transforms"]["local"],
            matrix(2, 3, 1)
        )

        self.assert_matrix(
            updated["transforms"]["world"],
            matrix(102, 3, 1)
        )

        world_mesh = self.tessellate(
            kind="DOMAIN",
            geometry_id=domain_id,
            space="world"
        )

        local_mesh = self.tessellate(
            kind="DOMAIN",
            geometry_id=domain_id,
            space="local"
        )

        self.assert_bounds(
            world_mesh,
            (102, 3, 1, 107, 6, 3)
        )

        self.assert_bounds(
            local_mesh,
            (2, 3, 1, 7, 6, 3)
        )

        self.assert_closed(
            world_mesh
        )

        self.assert_closed(
            local_mesh
        )

        self.assertAlmostEqual(
            mesh_volume(world_mesh),
            mesh_volume(local_mesh),
            places=6
        )

        solid_mesh = self.tessellate(
            kind="SOLID",
            space="world"
        )

        self.assert_bounds(
            solid_mesh,
            (99, -0.5, 0, 101, 0.5, 1)
        )

        self.assertEqual(
            updated["parentId"],
            self.parent_id
        )

    def test_domain_duplication(self):
        """Verify duplication of domains attached to a transform."""

        first = self.create_domain(
            name="Domain A"
        )

        second = self.create_domain(
            name="Domain B",
            transform=matrix(20, 0, 0)
        )

        duplicate = self.client.request(
            "transformDuplicate",
            {
                "id": self.parent_id,
                "includeChildren": False
            }
        )

        duplicate_id = duplicate["rootId"]

        self.assertNotEqual(
            duplicate_id,
            self.parent_id
        )

        self.assertEqual(
            duplicate["transforms"][self.parent_id],
            duplicate_id
        )

        copied_domains = self.list_geometries(
            kind="DOMAIN",
            parent_id=duplicate_id
        )

        self.assertEqual(
            len(copied_domains),
            2
        )

        self.assertEqual(
            len(
                self.list_geometries(
                    kind="DOMAIN",
                    parent_id=self.parent_id
                )
            ),
            2
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            4
        )

        for source in (first, second):
            source_id = source["id"]

            self.assertIn(
                source_id,
                duplicate["geometries"]
            )

            copied_id = duplicate["geometries"][source_id]

            copied = self.get_geometry(
                copied_id
            )

            self.assertNotEqual(
                copied["id"],
                source_id
            )

            self.assertNotEqual(
                copied["globalId"],
                source["globalId"]
            )

            self.assertNotEqual(
                copied["ifcEntityId"],
                source["ifcEntityId"]
            )

            self.assert_domain(
                copied,
                name=source["name"],
                profile=source["profile"],
                depth=source["depth"],
                parent_id=duplicate_id,
                local_matrix=source["transforms"]["local"]
            )

            source_mesh = self.tessellate(
                kind="DOMAIN",
                geometry_id=source_id,
                space="local"
            )

            copied_mesh = self.tessellate(
                kind="DOMAIN",
                geometry_id=copied_id,
                transform_id=duplicate_id,
                space="local"
            )

            self.assert_equivalent_meshes(
                source_mesh,
                copied_mesh
            )

        first_copy_id = duplicate["geometries"][first["id"]]

        self.client.request(
            "geometryUpdate",
            {
                "id": first_copy_id,
                "name": "Modified copy",
                "depth": 10
            }
        )

        original = self.get_geometry(
            first["id"]
        )

        self.assertEqual(
            original["name"],
            "Domain A"
        )

        self.assertEqual(
            original["depth"],
            2
        )

        self.client.request(
            "transformDelete",
            {"id": duplicate_id}
        )

        self.assertEqual(
            len(self.list_geometries(kind="DOMAIN")),
            2
        )

        self.assertEqual(
            self.get_geometry(first["id"]),
            first
        )

        self.assertEqual(
            self.get_geometry(second["id"]),
            second
        )

    def test_domain_persistence(self):
        """Verify domain preservation after IFC save and reload."""

        profile = [
            [0, 0],
            [8, 0],
            [8, 6],
            [0, 6]
        ]

        domain = self.create_domain(
            name="Persistent domain",
            profile=profile,
            depth=4,
            transform=matrix(2, 3, 1)
        )

        global_id = domain["globalId"]

        self.client.request(
            "transformUpdate",
            {
                "id": self.parent_id,
                "transform": {
                    "space": "world",
                    "matrix": matrix(100, 0, 0)
                }
            }
        )

        mesh_before = self.tessellate(
            kind="DOMAIN",
            geometry_id=domain["id"]
        )

        solid_before = self.tessellate(
            kind="SOLID"
        )

        load_result = self.save_and_reload()

        self.assertEqual(
            load_result["transformCount"],
            2
        )

        self.assertEqual(
            load_result["geometryCount"],
            2
        )

        domain_id = self.find_geometry(
            global_id
        )

        reloaded = self.get_geometry(
            domain_id
        )

        self.assert_domain(
            reloaded,
            name="Persistent domain",
            profile=profile,
            depth=4,
            local_matrix=matrix(2, 3, 1)
        )

        self.assertEqual(
            reloaded["globalId"],
            global_id
        )

        self.assert_matrix(
            reloaded["transforms"]["world"],
            matrix(102, 3, 1)
        )

        domains = self.list_geometries(
            kind="DOMAIN",
            parent_id=self.parent_id
        )

        self.assertEqual(
            len(domains),
            1
        )

        self.assertEqual(
            domains[0]["id"],
            domain_id
        )

        mesh_after = self.tessellate(
            kind="DOMAIN",
            geometry_id=domain_id
        )

        self.assert_bounds(
            mesh_after,
            (102, 3, 1, 110, 9, 5)
        )

        self.assert_closed(
            mesh_after
        )

        self.assert_equivalent_meshes(
            mesh_before,
            mesh_after
        )

        self.assert_equivalent_meshes(
            solid_before,
            self.tessellate(kind="SOLID")
        )

    def test_domain_deletion(self):
        """Verify domain deletion without removing its parent."""

        baseline = self.tessellate(
            kind="SOLID"
        )

        first = self.create_domain(
            name="Domain A"
        )

        second = self.create_domain(
            name="Domain B",
            transform=matrix(20, 0, 0)
        )

        first_id = first["id"]
        second_id = second["id"]

        deleted = self.client.request(
            "geometryDelete",
            {"id": first_id}
        )

        self.assertEqual(
            deleted,
            {"id": first_id}
        )

        self.client.expect_error(
            "geometryGet",
            {"id": first_id},
            "ENTITY_NOT_FOUND"
        )

        self.assertEqual(
            self.get_geometry(second_id),
            second
        )

        domains = self.list_geometries(
            kind="DOMAIN",
            parent_id=self.parent_id
        )

        self.assertEqual(
            len(domains),
            1
        )

        self.assertEqual(
            domains[0]["id"],
            second_id
        )

        parent = self.get_transform()

        self.assertNotIn(
            first_id,
            parent["geometries"]
        )

        self.assertIn(
            second_id,
            parent["geometries"]
        )

        self.client.request(
            "geometryDelete",
            {"id": second_id}
        )

        self.assertEqual(
            self.list_geometries(
                kind="DOMAIN",
                parent_id=self.parent_id
            ),
            []
        )

        self.assert_empty_mesh(
            self.tessellate(kind="DOMAIN")
        )

        self.assert_equivalent_meshes(
            baseline,
            self.tessellate(kind="SOLID")
        )

        parent = self.get_transform()

        self.assertEqual(
            parent["globalId"],
            PARENT_GLOBAL_ID
        )

        self.assertEqual(
            len(parent["geometries"]),
            1
        )

    def test_persistent_deletion(self):
        """Verify that deleted domains do not reappear after reload."""

        first = self.create_domain(
            name="Deleted domain"
        )

        second = self.create_domain(
            name="Preserved domain",
            transform=matrix(20, 0, 0)
        )

        deleted_global_id = first["globalId"]
        preserved_global_id = second["globalId"]

        self.client.request(
            "geometryDelete",
            {"id": first["id"]}
        )

        self.save_and_reload()

        domains = self.list_geometries(
            kind="DOMAIN"
        )

        domain_global_ids = {
            domain["globalId"]
            for domain in domains
        }

        self.assertNotIn(
            deleted_global_id,
            domain_global_ids
        )

        self.assertIn(
            preserved_global_id,
            domain_global_ids
        )

        self.assertEqual(
            len(domains),
            1
        )

        preserved_id = self.find_geometry(
            preserved_global_id
        )

        preserved = self.get_geometry(
            preserved_id
        )

        self.assert_domain(
            preserved,
            name="Preserved domain",
            local_matrix=matrix(20, 0, 0)
        )

        self.client.request(
            "geometryDelete",
            {"id": preserved_id}
        )

        load_result = self.save_and_reload()

        self.assertEqual(
            load_result["transformCount"],
            2
        )

        self.assertEqual(
            load_result["geometryCount"],
            1
        )

        self.assertEqual(
            self.list_geometries(kind="DOMAIN"),
            []
        )

        self.assert_empty_mesh(
            self.tessellate(kind="DOMAIN")
        )

        self.assert_bounds(
            self.tessellate(kind="SOLID"),
            BASELINE_BOUNDS
        )

        saved_content = self.output_path.read_text(
            encoding="utf-8"
        )

        self.assertNotIn(
            deleted_global_id,
            saved_content
        )

        self.assertNotIn(
            preserved_global_id,
            saved_content
        )

        self.assertNotIn(
            "ifc-cli:domain-parent",
            saved_content
        )

        self.assertNotIn(
            "IFCSPATIALZONE(",
            saved_content.upper()
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