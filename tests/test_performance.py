import itertools
import json
import math
import os
import platform
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None


# Run:
# python3 tests/test_performance.py ./build/ifc-cli
#
# Optional dependency for process memory measurements:
# python3 -m pip install psutil
#
# Not executed against the compiled CLI.
#
# Performance metrics are emitted as JSON lines prefixed with PERF.
# No execution time, throughput or memory consumption threshold is imposed.
# Functional assertions remain mandatory.
#
# Index construction cannot be measured independently through the CLI.
# Its cost is estimated by comparing models with and without representations.
# This comparison also includes differences in IFC parsing and allocation.
#
# Memory measurements use the CLI process RSS, not the Python test process.
# RSS retention does not necessarily indicate a memory leak because native
# memory allocators may retain released memory for subsequent allocations.
#
# JSONL measurements include process communication and Python JSON decoding.
# Time to mesh.begin includes tessellation and preparation of the mesh.
#
# Request timeouts are operational safeguards, not performance thresholds.

EXECUTABLE = Path("./build/ifc-cli").resolve()

REQUEST_TIMEOUT = float(os.environ.get("IFC_CLI_PERF_TIMEOUT", "300"))
REPETITIONS = int(os.environ.get("IFC_CLI_PERF_REPETITIONS", "3"))

MODEL_SIZES = (100, 1000, 3000)
EDIT_SIZES = (10, 100, 300)
PROFILE_SIZES = (32, 128, 512, 2048)
DOMAIN_COUNTS = (10, 50, 150)
BOOLEAN_COUNTS = (10, 50, 100)
HIERARCHY_SIZES = (10, 100, 500)
STREAMING_SIZES = (256, 1024, 2048)

MEMORY_CYCLES = 10
MEMORY_BATCH_SIZE = 50
SESSION_MEMORY_CYCLES = 20

CHUNK_SIZE = 16384
MIB = 1024 ** 2

ROOT_GLOBAL_ID = "0000000000000000000002"

IDENTITY_MATRIX = [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1
]

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


def translation_matrix(x=0, y=0, z=0):
    matrix = IDENTITY_MATRIX.copy()
    matrix[3] = x
    matrix[7] = y
    matrix[11] = z
    return matrix


def transform_input(matrix=None):
    return {
        "space": "parent",
        "matrix": IDENTITY_MATRIX.copy() if matrix is None else matrix
    }


def parents_input(parent_id=None):
    return {
        "spatial": None,
        "placement": parent_id,
        "decomposition": parent_id
    }


def property_input(name, value, value_type="IfcInteger"):
    return {
        "propertySet": "Pset_Performance",
        "name": name,
        "valueType": value_type,
        "value": value,
        "unit": None,
        "source": "occurrence"
    }


def rectangular_profile(width, length, x=0, y=0):
    return [
        [x, y],
        [x + width, y],
        [x + width, y + length],
        [x, y + length]
    ]


def regular_polygon(vertex_count, radius=10):
    return [
        [
            radius * math.cos(2 * math.pi * index / vertex_count),
            radius * math.sin(2 * math.pi * index / vertex_count)
        ]
        for index in range(vertex_count)
    ]


def polygon_area(profile):
    return abs(math.fsum(
        a[0] * b[1] - b[0] * a[1]
        for a, b in zip(profile, profile[1:] + profile[:1])
    )) / 2


def elapsed_time(function, *args, **kwargs):
    started = time.perf_counter()
    result = function(*args, **kwargs)
    return time.perf_counter() - started, result


def rate(quantity, duration):
    return quantity / duration if duration > 0 else None


def summarize(samples):
    if not samples:
        raise ValueError("At least one sample is required")

    return {
        "samples": samples,
        "minimum": min(samples),
        "maximum": max(samples),
        "mean": statistics.fmean(samples),
        "median": statistics.median(samples),
        "standardDeviation": (
            statistics.stdev(samples)
            if len(samples) > 1
            else 0.0
        )
    }


def emit_metric(test, **metrics):
    record = {
        "test": test,
        **metrics
    }

    print(
        "PERF " + json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True
        ),
        flush=True
    )


def emit_timing(test, samples, operations=None, **metrics):
    summary = summarize(samples)

    result = {
        **metrics,
        "durationSeconds": summary
    }

    if operations is not None:
        result["operations"] = operations
        result["operationsPerSecond"] = rate(
            operations,
            summary["median"]
        )

    emit_metric(test, **result)


def step_real(value):
    return f"{float(value):.16E}"


def step_coordinates(values):
    return "(" + ",".join(
        step_real(value) for value in values
    ) + ")"


def step_ref(identifier):
    return "$" if identifier is None else f"#{identifier}"


def step_refs(identifiers):
    return "(" + ",".join(
        step_ref(identifier) for identifier in identifiers
    ) + ")"


def step_string(value):
    return "'" + value.replace("'", "''") + "'"


def global_id(index):
    return str(index).zfill(22)


class Fixture:
    def __init__(self):
        self.entities = []

        self.origin = self.point(0, 0, 0)

        self.z_axis = self.add(
            "IFCDIRECTION",
            "(0.,0.,1.)"
        )

        self.x_axis = self.add(
            "IFCDIRECTION",
            "(1.,0.,0.)"
        )

        self.axis = self.add(
            "IFCAXIS2PLACEMENT3D",
            f"#{self.origin},#{self.z_axis},#{self.x_axis}"
        )

        self.context = self.add(
            "IFCGEOMETRICREPRESENTATIONCONTEXT",
            f"$,'Model',3,1.E-6,#{self.axis},$"
        )

        length_unit = self.add(
            "IFCSIUNIT",
            "*,.LENGTHUNIT.,$,.METRE."
        )

        area_unit = self.add(
            "IFCSIUNIT",
            "*,.AREAUNIT.,$,.SQUARE_METRE."
        )

        volume_unit = self.add(
            "IFCSIUNIT",
            "*,.VOLUMEUNIT.,$,.CUBIC_METRE."
        )

        units = self.add(
            "IFCUNITASSIGNMENT",
            step_refs([length_unit, area_unit, volume_unit])
        )

        self.project = self.add(
            "IFCPROJECT",
            f"'{global_id(1)}',$,'Performance project',$,$,$,$,"
            f"(#{self.context}),#{units}"
        )

    def add(self, class_name, arguments):
        identifier = len(self.entities) + 1

        self.entities.append(
            f"#{identifier}={class_name.upper()}({arguments});"
        )

        return identifier

    def point(self, *coordinates):
        return self.add(
            "IFCCARTESIANPOINT",
            step_coordinates(coordinates)
        )

    def placement(self, x=0, y=0, z=0, parent=None):
        point = self.point(x, y, z)

        axis = self.add(
            "IFCAXIS2PLACEMENT3D",
            f"#{point},#{self.z_axis},#{self.x_axis}"
        )

        return self.add(
            "IFCLOCALPLACEMENT",
            f"{step_ref(parent)},#{axis}"
        )

    def rectangle(self, width=2, length=3):
        point = self.point(0, 0)

        axis = self.add(
            "IFCAXIS2PLACEMENT2D",
            f"#{point},$"
        )

        return self.add(
            "IFCRECTANGLEPROFILEDEF",
            f".AREA.,$,#{axis},{step_real(width)},{step_real(length)}"
        )

    def polygon(self, points):
        identifiers = [
            self.point(*point)
            for point in points
        ]

        polyline = self.add(
            "IFCPOLYLINE",
            step_refs([*identifiers, identifiers[0]])
        )

        return self.add(
            "IFCARBITRARYCLOSEDPROFILEDEF",
            f".AREA.,$,#{polyline}"
        )

    def representation(self, profile, depth):
        solid = self.add(
            "IFCEXTRUDEDAREASOLID",
            f"#{profile},#{self.axis},#{self.z_axis},{step_real(depth)}"
        )

        shape = self.add(
            "IFCSHAPEREPRESENTATION",
            f"#{self.context},'Body','SweptSolid',(#{solid})"
        )

        return self.add(
            "IFCPRODUCTDEFINITIONSHAPE",
            f"$,$,(#{shape})"
        )

    def product(self, index, name, placement, representation=None):
        return self.add(
            "IFCBUILDINGELEMENTPROXY",
            f"'{global_id(index)}',$,{step_string(name)},$,$,"
            f"#{placement},{step_ref(representation)},$,.NOTDEFINED."
        )

    def aggregate(self, index, parent, children):
        return self.add(
            "IFCRELAGGREGATES",
            f"'{global_id(index)}',$,$,$,#{parent},{step_refs(children)}"
        )

    def write(self, path):
        body = "\n".join(self.entities)

        content = f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('performance.ifc','2026-01-01T00:00:00',('Test'),('Test'),'Test','Test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
{body}
ENDSEC;
END-ISO-10303-21;
"""

        path.write_text(content, encoding="utf-8")

        return {
            "path": path,
            "ifcEntityCount": len(self.entities),
            "fileBytes": path.stat().st_size
        }


def build_model(
    path,
    product_count=0,
    with_geometry=True,
    root_profile=None,
    root_depth=2,
    hierarchy_depth=1
):
    fixture = Fixture()

    root_placement = fixture.placement()

    root_representation = None

    if root_profile is not None:
        profile = fixture.polygon(root_profile)

        root_representation = fixture.representation(
            profile,
            root_depth
        )

    root = fixture.product(
        2,
        "Performance root",
        root_placement,
        root_representation
    )

    shared_profile = None

    if product_count and with_geometry:
        shared_profile = fixture.rectangle()

    products = [root]
    placements = [root_placement]
    children_by_parent = {}

    for index in range(product_count):
        # Build a bounded-depth branching hierarchy rather than
        # an unbounded chain of recursive placements.

        if hierarchy_depth <= 1:
            parent_index = 0
        else:
            parent_index = index // 4

        parent = products[parent_index]
        parent_placement = placements[parent_index]

        placement = fixture.placement(
            x=5 + index * 5,
            y=index % 10 * 5,
            z=0,
            parent=parent_placement
        )

        representation = (
            fixture.representation(shared_profile, 2)
            if with_geometry
            else None
        )

        product = fixture.product(
            index + 3,
            f"Performance product {index}",
            placement,
            representation
        )

        products.append(product)
        placements.append(placement)

        children_by_parent.setdefault(
            parent,
            []
        ).append(product)

    relation_guid = product_count + 100

    for parent, children in children_by_parent.items():
        fixture.aggregate(
            relation_guid,
            parent,
            children
        )

        relation_guid += 1

    result = fixture.write(path)

    result.update({
        "productCount": product_count,
        "transformCount": product_count + 2,
        "geometryCount": (
            (product_count if with_geometry else 0)
            + int(root_representation is not None)
        ),
        "materialCount": 0
    })

    return result


def mesh_points(mesh):
    vertices = mesh["vertices"]

    return [
        tuple(vertices[index:index + 3])
        for index in range(0, len(vertices), 3)
    ]


def subtract(first, second):
    return tuple(a - b for a, b in zip(first, second))


def dot(first, second):
    return math.fsum(a * b for a, b in zip(first, second))


def cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0]
    )


def mesh_volume(mesh):
    points = mesh_points(mesh)

    if not points:
        return 0.0

    origin = points[0]
    indices = mesh["indices"]

    terms = []

    for offset in range(0, len(indices), 3):
        a, b, c = [
            subtract(points[index], origin)
            for index in indices[offset:offset + 3]
        ]

        terms.append(dot(a, cross(b, c)))

    return abs(math.fsum(terms)) / 6


class Client:
    def __init__(self, executable):
        self.counter = 0
        self.responses = queue.Queue()

        self.process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0
        )

        self.reader = threading.Thread(
            target=self.read_stdout,
            daemon=True
        )

        self.reader.start()

    def read_stdout(self):
        try:
            while True:
                line = self.process.stdout.readline()

                if not line:
                    break

                self.responses.put((
                    time.perf_counter(),
                    line
                ))

        except Exception as error:
            self.responses.put(error)

        finally:
            self.responses.put(None)

    def send(self, command, params=None):
        if self.process.poll() is not None:
            raise RuntimeError(
                f"CLI terminated with code {self.process.returncode}"
            )

        self.counter += 1
        request_id = f"request-{self.counter}"

        request = {
            "id": request_id,
            "command": command,
            "params": {} if params is None else params
        }

        payload = (
            json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":")
            ) + "\n"
        ).encode("utf-8")

        # Handle partial writes on unbuffered subprocess pipes.

        remaining = memoryview(payload)

        while remaining:
            written = self.process.stdin.write(remaining)

            if not written:
                raise RuntimeError("Could not write JSONL request")

            remaining = remaining[written:]

        self.process.stdin.flush()

        return request_id

    def read(self, request_id):
        try:
            item = self.responses.get(timeout=REQUEST_TIMEOUT)
        except queue.Empty as error:
            raise TimeoutError(
                f"No response for request {request_id}"
            ) from error

        if item is None:
            raise RuntimeError(
                "CLI terminated before returning a response"
            )

        if isinstance(item, Exception):
            raise RuntimeError(
                "Could not read CLI stdout"
            ) from item

        received_at, line = item

        response = json.loads(line)

        if not isinstance(response, dict):
            raise AssertionError(response)

        if response.get("id") != request_id:
            raise AssertionError(
                f"Unexpected response identifier: {response}"
            )

        if type(response.get("ok")) is not bool:
            raise AssertionError(response)

        if not response["ok"]:
            raise RuntimeError(response["error"])

        if set(response) != {"id", "ok", "result"}:
            raise AssertionError(response)

        return response["result"], len(line), received_at

    def request(self, command, params=None):
        request_id = self.send(command, params)
        result, _, _ = self.read(request_id)
        return result

    def measured_request(self, command, params=None):
        started = time.perf_counter()

        request_id = self.send(command, params)
        result, response_bytes, received_at = self.read(request_id)

        finished = time.perf_counter()

        return result, {
            "durationSeconds": finished - started,
            "responseArrivalSeconds": received_at - started,
            "responseBytes": response_bytes
        }

    def tessellate(
        self,
        transform_id,
        kind="SOLID",
        include_children=False,
        include_normals=False,
        include_materials=False,
        geometry_id=None
    ):
        options = {
            "kind": kind,
            "space": "local",
            "includeChildren": include_children,
            "includeNormals": include_normals,
            "includeMaterials": include_materials
        }

        if geometry_id is not None:
            options["geometryId"] = geometry_id

        started = time.perf_counter()

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

        events = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.normals": 3,
            "mesh.materials": 4,
            "mesh.end": 5
        }

        chunk_counts = {
            "vertices": 0,
            "indices": 0,
            "normals": 0
        }

        chunk_sizes = {
            "vertices": [],
            "indices": [],
            "normals": []
        }

        total_bytes = 0
        payload_bytes = 0
        event_count = 0
        previous_order = -1

        begin = None
        begin_received_at = None
        end_received_at = None
        materials_received = False

        while True:
            event, response_bytes, received_at = self.read(request_id)

            total_bytes += response_bytes
            event_count += 1

            name = event["event"]

            if name not in events:
                raise AssertionError(
                    f"Unexpected mesh event: {event}"
                )

            order = events[name]

            if order < previous_order:
                raise AssertionError(
                    f"Invalid mesh event order: {name}"
                )

            previous_order = order

            if name == "mesh.begin":
                if begin is not None:
                    raise AssertionError("Duplicate mesh.begin")

                begin = event
                begin_received_at = received_at
                continue

            if begin is None:
                raise AssertionError(
                    "Mesh event received before mesh.begin"
                )

            if name == "mesh.end":
                end_received_at = received_at
                break

            if name == "mesh.materials":
                if materials_received:
                    raise AssertionError("Duplicate mesh.materials")

                materials_received = True
                mesh["groups"] = event["groups"]
                continue

            key = name.split(".")[1]
            values = event["values"]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(
                    f"Invalid mesh offset for {key}"
                )

            if not isinstance(values, list):
                raise AssertionError("Invalid chunk values")

            if not values or len(values) > CHUNK_SIZE:
                raise AssertionError("Invalid mesh chunk size")

            mesh[key].extend(values)

            chunk_counts[key] += 1
            chunk_sizes[key].append(len(values))
            payload_bytes += response_bytes

        finished = time.perf_counter()

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        duration = finished - started
        streaming_duration = end_received_at - begin_received_at

        metrics = {
            "durationSeconds": duration,
            "timeToFirstEventSeconds": begin_received_at - started,
            "streamingSeconds": streaming_duration,
            "responseBytes": total_bytes,
            "payloadEventBytes": payload_bytes,
            "eventCount": event_count,
            "chunkCounts": chunk_counts,
            "chunkSizes": chunk_sizes,
            "vertexCount": mesh["vertexCount"],
            "indexCount": mesh["indexCount"],
            "triangleCount": mesh["indexCount"] // 3,
            "normalCount": len(mesh["normals"]) // 3,
            "endToEndMiBPerSecond": rate(
                total_bytes / MIB,
                duration
            ),
            "streamingMiBPerSecond": rate(
                (total_bytes / MIB),
                streaming_duration
            ),
            "trianglesPerSecond": rate(
                mesh["indexCount"] // 3,
                duration
            )
        }

        # Functional validation is performed outside the measured interval.

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if mesh["indexCount"] % 3:
            raise AssertionError("Invalid triangle structure")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(f"Invalid mesh index: {index}")

        for value in itertools.chain(
            mesh["vertices"],
            mesh["normals"]
        ):
            if not math.isfinite(value):
                raise AssertionError(
                    f"Non-finite mesh coordinate: {value}"
                )

        if include_normals and mesh["vertexCount"]:
            if len(mesh["normals"]) != len(mesh["vertices"]):
                raise AssertionError("Invalid normal count")

        if not include_normals and mesh["normals"]:
            raise AssertionError("Unexpected normals")

        if materials_received != include_materials:
            raise AssertionError("Invalid material event presence")

        for key, sizes in chunk_sizes.items():
            if any(size != CHUNK_SIZE for size in sizes[:-1]):
                raise AssertionError(
                    f"Incomplete intermediate chunk for {key}"
                )

        return mesh, metrics

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

        if not self.process.stdin.closed:
            self.process.stdin.close()

        self.reader.join(timeout=10)

        if not self.process.stdout.closed:
            self.process.stdout.close()


class PerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if REPETITIONS < 1:
            raise ValueError(
                "IFC_CLI_PERF_REPETITIONS must be positive"
            )

        emit_metric(
            "environment",
            platform=platform.platform(),
            architecture=platform.machine(),
            processor=platform.processor(),
            logicalCpuCount=os.cpu_count(),
            pythonVersion=platform.python_version(),
            executable=str(EXECUTABLE),
            repetitions=REPETITIONS,
            requestTimeoutSeconds=REQUEST_TIMEOUT,
            memoryMeasurementAvailable=psutil is not None
        )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-performance-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.capabilities = self.client.request("getCapabilities")

        self.empty_fixture = self.fixture(
            "empty",
            product_count=0
        )

    def tearDown(self):
        self.client.close()

    def fixture(self, name, **options):
        return build_model(
            self.root_path / f"{name}.ifc",
            **options
        )

    def load(self, fixture):
        result = self.client.request(
            "load",
            {"path": str(fixture["path"])}
        )

        self.assert_loaded(result, fixture)

        return result

    def measured_load(self, fixture):
        result, metrics = self.client.measured_request(
            "load",
            {"path": str(fixture["path"])}
        )

        self.assert_loaded(result, fixture)

        return metrics

    def assert_loaded(self, result, fixture):
        self.assertEqual(
            result["schema"].upper(),
            "IFC4"
        )

        self.assertEqual(
            result["path"],
            str(fixture["path"])
        )

        for field in (
            "transformCount",
            "geometryCount",
            "materialCount"
        ):
            self.assertEqual(
                result[field],
                fixture[field]
            )

    def list_transforms(self):
        return self.client.request("transformGetAll")

    def list_geometries(self, kind="ALL"):
        return self.client.request(
            "geometryGetAll",
            {
                "transformId": None,
                "kind": kind
            }
        )

    def list_materials(self):
        return self.client.request("materialGetAll")

    def root_id(self):
        matches = [
            item["id"]
            for item in self.list_transforms()
            if item["globalId"] == ROOT_GLOBAL_ID
        ]

        self.assertEqual(len(matches), 1)

        return matches[0]

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

    def create_transform(
        self,
        name,
        parent_id=None,
        matrix=None,
        properties=None
    ):
        return self.client.request(
            "transformCreate",
            {
                "name": name,
                "parents": parents_input(parent_id),
                "transform": transform_input(matrix),
                "properties": [] if properties is None else properties
            }
        )

    def create_geometry(
        self,
        parent_id,
        name,
        kind="DOMAIN",
        operation="ADD",
        profile=None,
        depth=2,
        matrix=None
    ):
        return self.client.request(
            "geometryCreate",
            {
                "parentId": parent_id,
                "name": name,
                "kind": kind,
                "operation": operation,
                "profile": DEFAULT_PROFILE if profile is None else profile,
                "depth": depth,
                "transform": transform_input(matrix)
            }
        )

    def create_material(self, name):
        return self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": "concrete",
                "visual": DEFAULT_VISUAL,
                "properties": []
            }
        )

    def update_transform(self, identifier, **changes):
        return self.client.request(
            "transformUpdate",
            {"id": identifier, **changes}
        )

    def update_geometry(self, identifier, **changes):
        return self.client.request(
            "geometryUpdate",
            {"id": identifier, **changes}
        )

    def update_material(self, identifier, **changes):
        return self.client.request(
            "materialUpdate",
            {"id": identifier, **changes}
        )

    def delete_transform(self, identifier):
        self.assertEqual(
            self.client.request(
                "transformDelete",
                {"id": identifier}
            ),
            {"id": identifier}
        )

    def delete_geometry(self, identifier):
        self.assertEqual(
            self.client.request(
                "geometryDelete",
                {"id": identifier}
            ),
            {"id": identifier}
        )

    def delete_material(self, identifier):
        self.assertEqual(
            self.client.request(
                "materialDelete",
                {"id": identifier}
            ),
            {"id": identifier}
        )

    def save_model(self, path):
        self.assertEqual(
            self.client.request(
                "save",
                {"path": str(path)}
            ),
            {"path": str(path)}
        )

        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)

        return path.stat().st_size

    def assert_counts(
        self,
        transform_count,
        geometry_count,
        material_count=0
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

    def assert_index_consistency(self):
        transforms = self.list_transforms()
        geometries = self.list_geometries()
        materials = self.list_materials()

        transform_ids = {
            item["id"] for item in transforms
        }

        geometry_ids = {
            item["id"] for item in geometries
        }

        material_ids = {
            item["id"] for item in materials
        }

        self.assertEqual(len(transform_ids), len(transforms))
        self.assertEqual(len(geometry_ids), len(geometries))
        self.assertEqual(len(material_ids), len(materials))

        for geometry in geometries:
            self.assertIn(
                geometry["parentId"],
                transform_ids
            )

        global_ids = [
            item["globalId"]
            for item in [*transforms, *geometries, *materials]
            if item["globalId"] is not None
        ]

        self.assertEqual(
            len(global_ids),
            len(set(global_ids))
        )

    def assert_volume(self, mesh, expected, tolerance=1e-5):
        self.assertAlmostEqual(
            mesh_volume(mesh),
            expected,
            delta=tolerance
        )

    def require_memory_measurement(self):
        if psutil is None:
            self.skipTest(
                "Install psutil to measure CLI process memory"
            )

        process = psutil.Process(self.client.process.pid)

        try:
            process.memory_info()
        except (psutil.Error, OSError) as error:
            self.skipTest(
                f"Process memory is not accessible: {error}"
            )

        return process

    @staticmethod
    def memory_sample(process):
        information = process.memory_info()

        sample = {
            "rssBytes": information.rss,
            "rssMiB": information.rss / MIB,
            "vmsBytes": information.vms,
            "vmsMiB": information.vms / MIB
        }

        # USS is available on some operating systems and provides
        # a useful complement to resident memory measurements.

        try:
            full_information = process.memory_full_info()

            if hasattr(full_information, "uss"):
                sample["ussBytes"] = full_information.uss
                sample["ussMiB"] = full_information.uss / MIB

        except (psutil.Error, OSError, NotImplementedError):
            pass

        return sample

    @staticmethod
    def memory_slope(samples):
        if len(samples) < 2:
            return 0.0

        x_values = list(range(len(samples)))
        mean_x = statistics.fmean(x_values)
        mean_y = statistics.fmean(samples)

        numerator = math.fsum(
            (x - mean_x) * (y - mean_y)
            for x, y in zip(x_values, samples)
        )

        denominator = math.fsum(
            (x - mean_x) ** 2
            for x in x_values
        )

        return numerator / denominator

    def emit_mesh_metrics(self, test, samples, **metadata):
        durations = [
            sample["durationSeconds"]
            for sample in samples
        ]

        first_event = [
            sample["timeToFirstEventSeconds"]
            for sample in samples
        ]

        streaming = [
            sample["streamingSeconds"]
            for sample in samples
        ]

        throughputs = [
            sample["endToEndMiBPerSecond"]
            for sample in samples
            if sample["endToEndMiBPerSecond"] is not None
        ]

        streaming_throughputs = [
            sample["streamingMiBPerSecond"]
            for sample in samples
            if sample["streamingMiBPerSecond"] is not None
        ]

        emit_metric(
            test,
            **metadata,
            durationSeconds=summarize(durations),
            timeToFirstEventSeconds=summarize(first_event),
            streamingSeconds=summarize(streaming),
            endToEndMiBPerSecond=(
                summarize(throughputs)
                if throughputs
                else None
            ),
            streamingMiBPerSecond=(
                summarize(streaming_throughputs)
                if streaming_throughputs
                else None
            ),
            meshSamples=samples
        )

    def test_loading_performance(self):
        """Measure loading latency as the IFC model size increases."""

        for size in MODEL_SIZES:
            with self.subTest(product_count=size):
                fixture = self.fixture(
                    f"loading-{size}",
                    product_count=size
                )

                samples = []

                # Warm up the parser and filesystem cache.

                self.load(fixture)
                self.load(self.empty_fixture)

                for _ in range(REPETITIONS):
                    self.load(self.empty_fixture)

                    metrics = self.measured_load(fixture)

                    samples.append(
                        metrics["durationSeconds"]
                    )

                self.assert_counts(
                    fixture["transformCount"],
                    fixture["geometryCount"]
                )

                median = statistics.median(samples)

                emit_timing(
                    "loading",
                    samples,
                    productCount=size,
                    ifcEntityCount=fixture["ifcEntityCount"],
                    transformCount=fixture["transformCount"],
                    geometryCount=fixture["geometryCount"],
                    inputBytes=fixture["fileBytes"],
                    ifcEntitiesPerSecond=rate(
                        fixture["ifcEntityCount"],
                        median
                    ),
                    inputMiBPerSecond=rate(
                        fixture["fileBytes"] / MIB,
                        median
                    ),
                    filesystemCache="warm",
                    previousModel="empty"
                )

    def test_indexing_performance(self):
        """Estimate indexing-related costs using paired model measurements."""

        for size in MODEL_SIZES:
            with self.subTest(product_count=size):
                empty_products = self.fixture(
                    f"indexing-empty-{size}",
                    product_count=size,
                    with_geometry=False
                )

                represented_products = self.fixture(
                    f"indexing-represented-{size}",
                    product_count=size,
                    with_geometry=True
                )

                fixtures = {
                    "transformsOnly": empty_products,
                    "transformsAndGeometries": represented_products
                }

                samples = {
                    name: []
                    for name in fixtures
                }

                deltas = []

                for fixture in fixtures.values():
                    self.load(fixture)

                for repetition in range(REPETITIONS):
                    paired_samples = {}

                    # Alternate measurement order to limit systematic
                    # warm-up and ordering biases.

                    names = list(fixtures)

                    if repetition % 2:
                        names.reverse()

                    for name in names:
                        self.load(self.empty_fixture)

                        metrics = self.measured_load(
                            fixtures[name]
                        )

                        duration = metrics["durationSeconds"]

                        samples[name].append(duration)
                        paired_samples[name] = duration

                    deltas.append(
                        paired_samples["transformsAndGeometries"]
                        - paired_samples["transformsOnly"]
                    )

                for name, fixture in fixtures.items():
                    duration = statistics.median(
                        samples[name]
                    )

                    indexed_entities = (
                        fixture["transformCount"]
                        + fixture["geometryCount"]
                    )

                    emit_timing(
                        "indexing",
                        samples[name],
                        modelType=name,
                        productCount=size,
                        ifcEntityCount=fixture["ifcEntityCount"],
                        transformCount=fixture["transformCount"],
                        geometryCount=fixture["geometryCount"],
                        indexedEntityCount=indexed_entities,
                        effectiveIndexedEntitiesPerSecond=rate(
                            indexed_entities,
                            duration
                        ),
                        measurement="load_including_index_construction"
                    )

                emit_metric(
                    "indexing_comparison",
                    productCount=size,
                    additionalGeometryCount=size,
                    additionalIfcEntityCount=(
                        represented_products["ifcEntityCount"]
                        - empty_products["ifcEntityCount"]
                    ),
                    additionalLoadSeconds=summarize(deltas),
                    estimatedAdditionalSecondsPerGeometry=(
                        statistics.median(deltas) / size
                    ),
                    isolatedIndexingMeasurement=False
                )

                self.assert_index_consistency()

    def test_listing_performance(self):
        """Measure large transform, geometry and material listings."""

        for size in MODEL_SIZES:
            with self.subTest(product_count=size):
                fixture = self.fixture(
                    f"listing-{size}",
                    product_count=size
                )

                self.load(fixture)

                scenarios = [
                    (
                        "transformGetAll",
                        {},
                        size + 2
                    ),
                    (
                        "geometryGetAll",
                        {"transformId": None, "kind": "ALL"},
                        size
                    ),
                    (
                        "geometryGetAll",
                        {"transformId": None, "kind": "SOLID"},
                        size
                    )
                ]

                for command, params, expected_count in scenarios:
                    with self.subTest(command=command, params=params):
                        self.client.request(command, params)

                        durations = []
                        response_sizes = []

                        for _ in range(REPETITIONS):
                            result, metrics = (
                                self.client.measured_request(
                                    command,
                                    params
                                )
                            )

                            self.assertEqual(
                                len(result),
                                expected_count
                            )

                            self.assertEqual(
                                len({item["id"] for item in result}),
                                expected_count
                            )

                            durations.append(
                                metrics["durationSeconds"]
                            )

                            response_sizes.append(
                                metrics["responseBytes"]
                            )

                        emit_timing(
                            "listing",
                            durations,
                            operations=expected_count,
                            command=command,
                            parameters=params,
                            returnedEntityCount=expected_count,
                            responseBytes=summarize(response_sizes),
                            responseMiBPerSecond=rate(
                                statistics.median(response_sizes) / MIB,
                                statistics.median(durations)
                            )
                        )

        # Material listings require a separate populated fixture.
        # Material creation is deliberately excluded from measurements.

        self.load(self.empty_fixture)

        for size in EDIT_SIZES:
            existing_count = len(self.list_materials())

            for index in range(existing_count, size):
                self.create_material(
                    f"Listing material {index}"
                )

            durations = []
            response_sizes = []

            self.list_materials()

            for _ in range(REPETITIONS):
                result, metrics = self.client.measured_request(
                    "materialGetAll"
                )

                self.assertEqual(len(result), size)

                durations.append(metrics["durationSeconds"])
                response_sizes.append(metrics["responseBytes"])

            emit_timing(
                "listing",
                durations,
                operations=size,
                command="materialGetAll",
                returnedEntityCount=size,
                responseBytes=summarize(response_sizes)
            )

    def test_creation_performance(self):
        """Measure transform, geometry and material creation throughput."""

        for size in EDIT_SIZES:
            with self.subTest(entity_count=size):
                transform_samples = []
                domain_samples = []
                solid_samples = []
                material_samples = []

                for repetition in range(REPETITIONS):
                    self.load(self.empty_fixture)

                    root_id = self.root_id()

                    transforms = []

                    started = time.perf_counter()

                    for index in range(size):
                        transforms.append(
                            self.create_transform(
                                f"Created transform {index}",
                                parent_id=root_id,
                                matrix=translation_matrix(
                                    index * 5,
                                    0,
                                    0
                                )
                            )
                        )

                    transform_samples.append(
                        time.perf_counter() - started
                    )

                    self.assert_counts(size + 2, 0)

                    started = time.perf_counter()

                    for index, transform in enumerate(transforms):
                        self.create_geometry(
                            transform["id"],
                            f"Created domain {index}",
                            kind="DOMAIN"
                        )

                    domain_samples.append(
                        time.perf_counter() - started
                    )

                    self.assert_counts(size + 2, size)

                    started = time.perf_counter()

                    for index, transform in enumerate(transforms):
                        self.create_geometry(
                            transform["id"],
                            f"Created physical geometry {index}",
                            kind="SOLID",
                            operation="ADD"
                        )

                    solid_samples.append(
                        time.perf_counter() - started
                    )

                    self.assert_counts(size + 2, size * 2)

                    started = time.perf_counter()

                    for index in range(size):
                        self.create_material(
                            f"Created material {index}"
                        )

                    material_samples.append(
                        time.perf_counter() - started
                    )

                    self.assert_counts(
                        size + 2,
                        size * 2,
                        size
                    )

                    self.assert_index_consistency()

                for entity_type, samples in (
                    ("transform", transform_samples),
                    ("domain", domain_samples),
                    ("solid", solid_samples),
                    ("material", material_samples)
                ):
                    emit_timing(
                        "creation",
                        samples,
                        operations=size,
                        entityType=entity_type,
                        entityCount=size
                    )

    def test_update_performance(self):
        """Measure update throughput for existing entities."""

        for size in EDIT_SIZES:
            with self.subTest(entity_count=size):
                self.load(self.empty_fixture)

                root_id = self.root_id()

                transforms = [
                    self.create_transform(
                        f"Update transform {index}",
                        parent_id=root_id
                    )
                    for index in range(size)
                ]

                domains = [
                    self.create_geometry(
                        transform["id"],
                        f"Update domain {index}"
                    )
                    for index, transform in enumerate(transforms)
                ]

                materials = [
                    self.create_material(
                        f"Update material {index}"
                    )
                    for index in range(size)
                ]

                transform_samples = []
                domain_samples = []
                material_samples = []

                for repetition in range(REPETITIONS):
                    started = time.perf_counter()

                    for index, transform in enumerate(transforms):
                        self.update_transform(
                            transform["id"],
                            name=f"Updated transform {index}-{repetition}",
                            transform=transform_input(
                                translation_matrix(
                                    index,
                                    repetition + 1,
                                    10
                                )
                            ),
                            properties=[
                                property_input(
                                    "Iteration",
                                    repetition
                                )
                            ]
                        )

                    transform_samples.append(
                        time.perf_counter() - started
                    )

                    started = time.perf_counter()

                    for index, domain in enumerate(domains):
                        self.update_geometry(
                            domain["id"],
                            name=f"Updated domain {index}-{repetition}",
                            depth=repetition + 3,
                            transform=transform_input(
                                translation_matrix(
                                    index,
                                    0,
                                    repetition
                                )
                            )
                        )

                    domain_samples.append(
                        time.perf_counter() - started
                    )

                    started = time.perf_counter()

                    for index, material in enumerate(materials):
                        self.update_material(
                            material["id"],
                            name=f"Updated material {index}-{repetition}",
                            visual={
                                "color": [0.2, 0.4, 0.6],
                                "opacity": 0.8,
                                "metallic": (
                                    (repetition % 10) / 10
                                ),
                                "roughness": 0.3
                            }
                        )

                    material_samples.append(
                        time.perf_counter() - started
                    )

                last_repetition = REPETITIONS - 1

                for index, transform in enumerate(transforms):
                    current = self.get_transform(
                        transform["id"]
                    )

                    self.assertEqual(
                        current["name"],
                        f"Updated transform {index}-{last_repetition}"
                    )

                    self.assertEqual(
                        current["transforms"]["local"],
                        translation_matrix(
                            index,
                            REPETITIONS,
                            10
                        )
                    )

                for domain in domains:
                    current = self.get_geometry(
                        domain["id"]
                    )

                    self.assertAlmostEqual(
                        current["depth"],
                        last_repetition + 3
                    )

                for material in materials:
                    current = self.get_material(
                        material["id"]
                    )

                    self.assertAlmostEqual(
                        current["visual"]["metallic"],
                        (last_repetition % 10) / 10
                    )

                self.assert_counts(
                    size + 2,
                    size,
                    size
                )

                self.assert_index_consistency()

                for entity_type, samples in (
                    ("transform", transform_samples),
                    ("domain", domain_samples),
                    ("material", material_samples)
                ):
                    emit_timing(
                        "update",
                        samples,
                        operations=size,
                        entityType=entity_type,
                        entityCount=size
                    )

    def test_tessellation_performance(self):
        """Measure tessellation latency as polygon complexity increases."""

        for vertex_count in PROFILE_SIZES:
            with self.subTest(profile_vertices=vertex_count):
                profile = regular_polygon(
                    vertex_count,
                    radius=10
                )

                depth = 5

                fixture = self.fixture(
                    f"tessellation-{vertex_count}",
                    root_profile=profile,
                    root_depth=depth
                )

                expected_volume = (
                    polygon_area(profile) * depth
                )

                self.load(fixture)

                root_id = self.root_id()

                warmup_mesh, _ = self.client.tessellate(
                    root_id
                )

                self.assertGreater(
                    warmup_mesh["indexCount"],
                    0
                )

                samples = []

                for _ in range(REPETITIONS):
                    mesh, metrics = self.client.tessellate(
                        root_id
                    )

                    self.assertGreater(
                        mesh["indexCount"],
                        0
                    )

                    self.assert_volume(
                        mesh,
                        expected_volume,
                        tolerance=1e-4
                    )

                    self.assertEqual(
                        mesh["indexCount"],
                        warmup_mesh["indexCount"]
                    )

                    samples.append(metrics)

                self.emit_mesh_metrics(
                    "tessellation",
                    samples,
                    kind="SOLID",
                    profileVertexCount=vertex_count,
                    analyticalVolume=expected_volume,
                    ifcEntityCount=fixture["ifcEntityCount"]
                )

    def test_domain_performance(self):
        """Measure combined tessellation of many independent domains."""

        for domain_count in DOMAIN_COUNTS:
            with self.subTest(domain_count=domain_count):
                self.load(self.empty_fixture)

                root_id = self.root_id()

                domains = [
                    self.create_geometry(
                        root_id,
                        f"Performance domain {index}",
                        depth=2,
                        matrix=translation_matrix(
                            index * 5,
                            0,
                            0
                        )
                    )
                    for index in range(domain_count)
                ]

                individual, _ = self.client.tessellate(
                    root_id,
                    kind="DOMAIN",
                    geometry_id=domains[0]["id"]
                )

                self.assert_volume(individual, 12.0)

                self.client.tessellate(
                    root_id,
                    kind="DOMAIN"
                )

                samples = []

                for _ in range(REPETITIONS):
                    mesh, metrics = self.client.tessellate(
                        root_id,
                        kind="DOMAIN"
                    )

                    self.assertEqual(
                        mesh["vertexCount"],
                        individual["vertexCount"] * domain_count
                    )

                    self.assertEqual(
                        mesh["indexCount"],
                        individual["indexCount"] * domain_count
                    )

                    self.assert_volume(
                        mesh,
                        domain_count * 12.0
                    )

                    samples.append(metrics)

                physical, _ = self.client.tessellate(
                    root_id,
                    kind="SOLID"
                )

                self.assertEqual(
                    physical["indexCount"],
                    0
                )

                self.assert_counts(
                    2,
                    domain_count
                )

                self.emit_mesh_metrics(
                    "domain_tessellation",
                    samples,
                    domainCount=domain_count,
                    totalAnalyticalVolume=domain_count * 12.0,
                    domainsPerSecond=rate(
                        domain_count,
                        statistics.median([
                            sample["durationSeconds"]
                            for sample in samples
                        ])
                    )
                )

    def test_boolean_performance(self):
        """Measure tessellation cost with increasing boolean operations."""

        # Use the same baseline physical volume for every scenario.
        # Each non-overlapping opening removes exactly 2 cubic metres.

        width = 2 * (max(BOOLEAN_COUNTS) + 1)
        length = 4
        depth = 2

        base_volume = width * length * depth

        fixture = self.fixture(
            "boolean-baseline",
            root_profile=rectangular_profile(width, length),
            root_depth=depth
        )

        self.load(fixture)

        root_id = self.root_id()

        baseline_mesh, baseline_metrics = self.client.tessellate(
            root_id
        )

        self.assert_volume(
            baseline_mesh,
            base_volume
        )

        emit_metric(
            "boolean_baseline",
            operationCount=0,
            analyticalVolume=base_volume,
            meshMetrics=baseline_metrics
        )

        for operation_count in BOOLEAN_COUNTS:
            with self.subTest(operation_count=operation_count):
                self.load(fixture)

                root_id = self.root_id()

                creation_started = time.perf_counter()

                for index in range(operation_count):
                    self.create_geometry(
                        root_id,
                        f"Boolean opening {index}",
                        kind="SOLID",
                        operation="SUBTRACT",
                        profile=rectangular_profile(
                            1,
                            1,
                            x=index * 2 + 1,
                            y=1
                        ),
                        depth=4,
                        matrix=translation_matrix(z=-1)
                    )

                creation_duration = (
                    time.perf_counter() - creation_started
                )

                expected_volume = (
                    base_volume - operation_count * 2
                )

                self.assert_counts(
                    2,
                    operation_count + 1
                )

                self.client.tessellate(root_id)

                samples = []

                for _ in range(REPETITIONS):
                    mesh, metrics = self.client.tessellate(
                        root_id
                    )

                    self.assert_volume(
                        mesh,
                        expected_volume,
                        tolerance=1e-4
                    )

                    self.assertGreater(
                        mesh["indexCount"],
                        baseline_mesh["indexCount"]
                    )

                    samples.append(metrics)

                self.emit_mesh_metrics(
                    "boolean_tessellation",
                    samples,
                    operation="SUBTRACT",
                    operationCount=operation_count,
                    creationSeconds=creation_duration,
                    creationOperationsPerSecond=rate(
                        operation_count,
                        creation_duration
                    ),
                    analyticalVolume=expected_volume,
                    baselineVolume=base_volume
                )

    def test_duplication_performance(self):
        """Measure recursive duplication of populated hierarchies."""

        for size in HIERARCHY_SIZES:
            with self.subTest(descendant_count=size):
                fixture = self.fixture(
                    f"duplication-{size}",
                    product_count=size,
                    hierarchy_depth=4
                )

                durations = []
                mapping_sizes = []

                for _ in range(REPETITIONS):
                    self.load(fixture)

                    root_id = self.root_id()

                    source_transforms = [
                        item
                        for item in self.list_transforms()
                        if item["id"] != self.find_project_id()
                    ]

                    source_geometry_ids = {
                        item["id"]
                        for item in self.list_geometries()
                    }

                    source_transform_ids = {
                        item["id"]
                        for item in source_transforms
                    }

                    source_global_ids = {
                        item["globalId"]
                        for item in source_transforms
                    }

                    result, metrics = self.client.measured_request(
                        "transformDuplicate",
                        {
                            "id": root_id,
                            "includeChildren": True
                        }
                    )

                    durations.append(
                        metrics["durationSeconds"]
                    )

                    mapping_sizes.append(
                        metrics["responseBytes"]
                    )

                    self.assertEqual(
                        set(result["transforms"]),
                        source_transform_ids
                    )

                    self.assertEqual(
                        set(result["geometries"]),
                        source_geometry_ids
                    )

                    self.assertEqual(
                        result["rootId"],
                        result["transforms"][root_id]
                    )

                    self.assertFalse(
                        source_transform_ids
                        & set(result["transforms"].values())
                    )

                    self.assertFalse(
                        source_geometry_ids
                        & set(result["geometries"].values())
                    )

                    current_transforms = {
                        item["id"]: item
                        for item in self.list_transforms()
                    }

                    for duplicate_id in result["transforms"].values():
                        self.assertNotIn(
                            current_transforms[duplicate_id]["globalId"],
                            source_global_ids
                        )

                    self.assert_counts(
                        2 * size + 3,
                        2 * size
                    )

                    self.assert_index_consistency()

                emit_timing(
                    "duplication",
                    durations,
                    operations=size + 1,
                    descendantCount=size,
                    duplicatedTransformCount=size + 1,
                    duplicatedGeometryCount=size,
                    responseBytes=summarize(mapping_sizes),
                    recursive=True
                )

    def find_project_id(self):
        matches = [
            item["id"]
            for item in self.list_transforms()
            if item["globalId"] == global_id(1)
        ]

        self.assertEqual(len(matches), 1)

        return matches[0]

    def test_deletion_performance(self):
        """Measure recursive deletion of large hierarchies."""

        for size in HIERARCHY_SIZES:
            with self.subTest(descendant_count=size):
                fixture = self.fixture(
                    f"deletion-{size}",
                    product_count=size,
                    hierarchy_depth=4
                )

                samples = []

                for _ in range(REPETITIONS):
                    self.load(fixture)

                    root_id = self.root_id()

                    result, metrics = self.client.measured_request(
                        "transformDelete",
                        {"id": root_id}
                    )

                    samples.append(
                        metrics["durationSeconds"]
                    )

                    self.assertEqual(
                        result,
                        {"id": root_id}
                    )

                    self.assert_counts(
                        transform_count=1,
                        geometry_count=0
                    )

                    remaining = self.list_transforms()

                    self.assertEqual(
                        remaining[0]["globalId"],
                        global_id(1)
                    )

                emit_timing(
                    "deletion",
                    samples,
                    operations=size + 1,
                    descendantCount=size,
                    deletedTransformCount=size + 1,
                    deletedGeometryCount=size,
                    deletedIndexedEntityCount=2 * size + 1
                )

    def test_save_performance(self):
        """Measure serialization and file writing latency."""

        for size in MODEL_SIZES:
            with self.subTest(product_count=size):
                fixture = self.fixture(
                    f"saving-{size}",
                    product_count=size
                )

                self.load(fixture)

                output_path = (
                    self.root_path / f"saved-{size}.ifc"
                )

                self.save_model(output_path)

                samples = []
                output_sizes = []

                for _ in range(REPETITIONS):
                    output_path.unlink(missing_ok=True)

                    result, metrics = self.client.measured_request(
                        "save",
                        {"path": str(output_path)}
                    )

                    self.assertEqual(
                        result,
                        {"path": str(output_path)}
                    )

                    self.assertTrue(output_path.is_file())

                    output_bytes = output_path.stat().st_size

                    self.assertGreater(output_bytes, 0)

                    samples.append(metrics["durationSeconds"])
                    output_sizes.append(output_bytes)

                median = statistics.median(samples)

                emit_timing(
                    "save",
                    samples,
                    productCount=size,
                    ifcEntityCount=fixture["ifcEntityCount"],
                    transformCount=fixture["transformCount"],
                    geometryCount=fixture["geometryCount"],
                    outputBytes=summarize(output_sizes),
                    outputMiBPerSecond=rate(
                        statistics.median(output_sizes) / MIB,
                        median
                    ),
                    fsyncIncluded=False
                )

                saved_fixture = {
                    **fixture,
                    "path": output_path
                }

                self.load(saved_fixture)

                self.assert_counts(
                    fixture["transformCount"],
                    fixture["geometryCount"]
                )

    def test_memory_growth(self):
        """Measure memory evolution after repeated creation and deletion."""

        process = self.require_memory_measurement()

        self.load(self.empty_fixture)

        root_id = self.root_id()

        baseline = self.memory_sample(process)

        samples = []

        for cycle in range(MEMORY_CYCLES):
            with self.subTest(cycle=cycle):
                before = self.memory_sample(process)

                started = time.perf_counter()

                batch = self.create_transform(
                    f"Memory batch {cycle}",
                    parent_id=root_id
                )

                materials = []

                for index in range(MEMORY_BATCH_SIZE):
                    transform = self.create_transform(
                        f"Memory transform {cycle}-{index}",
                        parent_id=batch["id"],
                        matrix=translation_matrix(
                            index * 5,
                            0,
                            0
                        )
                    )

                    self.create_geometry(
                        transform["id"],
                        f"Memory domain {cycle}-{index}"
                    )

                    if index % 10 == 0:
                        materials.append(
                            self.create_material(
                                f"Memory material {cycle}-{index}"
                            )
                        )

                creation_seconds = (
                    time.perf_counter() - started
                )

                after_creation = self.memory_sample(process)

                self.assert_counts(
                    MEMORY_BATCH_SIZE + 3,
                    MEMORY_BATCH_SIZE,
                    len(materials)
                )

                mesh, mesh_metrics = self.client.tessellate(
                    batch["id"],
                    kind="DOMAIN",
                    include_children=True
                )

                self.assert_volume(
                    mesh,
                    MEMORY_BATCH_SIZE * 12.0
                )

                after_tessellation = self.memory_sample(process)

                started = time.perf_counter()

                self.delete_transform(batch["id"])

                for material in materials:
                    self.delete_material(material["id"])

                deletion_seconds = (
                    time.perf_counter() - started
                )

                self.assert_counts(2, 0, 0)

                after_deletion = self.memory_sample(process)

                output_path = (
                    self.root_path / "memory-growth.ifc"
                )

                saved_bytes = self.save_model(output_path)

                sample = {
                    "cycle": cycle,
                    "before": before,
                    "afterCreation": after_creation,
                    "afterTessellation": after_tessellation,
                    "afterDeletion": after_deletion,
                    "creationSeconds": creation_seconds,
                    "deletionSeconds": deletion_seconds,
                    "tessellationSeconds": (
                        mesh_metrics["durationSeconds"]
                    ),
                    "savedModelBytes": saved_bytes,
                    "retainedRssBytes": (
                        after_deletion["rssBytes"]
                        - baseline["rssBytes"]
                    ),
                    "cycleRssDeltaBytes": (
                        after_deletion["rssBytes"]
                        - before["rssBytes"]
                    )
                }

                samples.append(sample)

                emit_metric(
                    "memory_growth_cycle",
                    batchSize=MEMORY_BATCH_SIZE,
                    **sample
                )

        retained_samples = [
            sample["afterDeletion"]["rssBytes"]
            for sample in samples
        ]

        final = self.memory_sample(process)

        emit_metric(
            "memory_growth_summary",
            cycles=MEMORY_CYCLES,
            batchSize=MEMORY_BATCH_SIZE,
            baseline=baseline,
            final=final,
            postDeletionRssBytes=summarize(
                retained_samples
            ),
            finalRssDeltaBytes=(
                final["rssBytes"]
                - baseline["rssBytes"]
            ),
            postDeletionRssSlopeBytesPerCycle=(
                self.memory_slope(retained_samples)
            ),
            observedPeakRssBytes=max(
                sample[stage]["rssBytes"]
                for sample in samples
                for stage in (
                    "before",
                    "afterCreation",
                    "afterTessellation",
                    "afterDeletion"
                )
            ),
            savedModelBytes=summarize([
                sample["savedModelBytes"]
                for sample in samples
            ]),
            leakThresholdApplied=False
        )

        self.assert_counts(2, 0, 0)

        self.assertIsNone(
            self.client.process.poll()
        )

    def test_session_memory(self):
        """Measure memory retention across successive model loads."""

        process = self.require_memory_measurement()

        large_fixture = self.fixture(
            "session-memory-large",
            product_count=max(MODEL_SIZES)
        )

        small_fixture = self.fixture(
            "session-memory-small",
            product_count=10
        )

        self.load(small_fixture)

        baseline = self.memory_sample(process)

        samples = []

        for cycle in range(SESSION_MEMORY_CYCLES):
            with self.subTest(cycle=cycle):
                large_metrics = self.measured_load(
                    large_fixture
                )

                large_memory = self.memory_sample(
                    process
                )

                self.assert_counts(
                    large_fixture["transformCount"],
                    large_fixture["geometryCount"]
                )

                small_metrics = self.measured_load(
                    small_fixture
                )

                self.assert_counts(
                    small_fixture["transformCount"],
                    small_fixture["geometryCount"]
                )

                small_memory = self.memory_sample(
                    process
                )

                sample = {
                    "cycle": cycle,
                    "largeModel": large_memory,
                    "smallModel": small_memory,
                    "largeLoadSeconds": (
                        large_metrics["durationSeconds"]
                    ),
                    "smallLoadSeconds": (
                        small_metrics["durationSeconds"]
                    ),
                    "releasedRssBytes": (
                        large_memory["rssBytes"]
                        - small_memory["rssBytes"]
                    ),
                    "retainedRssBytes": (
                        small_memory["rssBytes"]
                        - baseline["rssBytes"]
                    )
                }

                samples.append(sample)

                emit_metric(
                    "session_memory_cycle",
                    **sample
                )

        large_rss = [
            sample["largeModel"]["rssBytes"]
            for sample in samples
        ]

        small_rss = [
            sample["smallModel"]["rssBytes"]
            for sample in samples
        ]

        self.load(self.empty_fixture)

        final = self.memory_sample(process)

        emit_metric(
            "session_memory_summary",
            cycles=SESSION_MEMORY_CYCLES,
            largeIfcEntityCount=large_fixture["ifcEntityCount"],
            smallIfcEntityCount=small_fixture["ifcEntityCount"],
            baseline=baseline,
            finalEmptyModel=final,
            largeModelRssBytes=summarize(large_rss),
            smallModelRssBytes=summarize(small_rss),
            releasedRssBytes=summarize([
                sample["releasedRssBytes"]
                for sample in samples
            ]),
            largeModelRssSlopeBytesPerCycle=(
                self.memory_slope(large_rss)
            ),
            smallModelRssSlopeBytesPerCycle=(
                self.memory_slope(small_rss)
            ),
            finalRssDeltaBytes=(
                final["rssBytes"]
                - baseline["rssBytes"]
            ),
            memoryReleaseProven=False,
            leakThresholdApplied=False
        )

        self.assert_counts(2, 0, 0)

        self.assertIsNone(
            self.client.process.poll()
        )

    def test_mesh_streaming_performance(self):
        """Measure JSONL throughput for large multi-chunk meshes."""

        for size in STREAMING_SIZES:
            with self.subTest(product_count=size):
                fixture = self.fixture(
                    f"mesh-streaming-{size}",
                    product_count=size
                )

                self.load(fixture)

                root_id = self.root_id()

                for include_normals in (False, True):
                    with self.subTest(
                        include_normals=include_normals
                    ):
                        self.client.tessellate(
                            root_id,
                            include_children=True,
                            include_normals=include_normals
                        )

                        samples = []

                        for _ in range(REPETITIONS):
                            mesh, metrics = self.client.tessellate(
                                root_id,
                                include_children=True,
                                include_normals=include_normals
                            )

                            self.assertGreater(
                                mesh["vertexCount"],
                                0
                            )

                            self.assertGreater(
                                mesh["indexCount"],
                                0
                            )

                            self.assertGreater(
                                metrics["chunkCounts"]["vertices"],
                                1
                            )

                            self.assertGreater(
                                metrics["responseBytes"],
                                0
                            )

                            self.assertEqual(
                                sum(
                                    metrics["chunkSizes"]["vertices"]
                                ),
                                len(mesh["vertices"])
                            )

                            self.assertEqual(
                                sum(
                                    metrics["chunkSizes"]["indices"]
                                ),
                                len(mesh["indices"])
                            )

                            if include_normals:
                                self.assertEqual(
                                    sum(
                                        metrics["chunkSizes"]["normals"]
                                    ),
                                    len(mesh["normals"])
                                )

                            self.assert_volume(
                                mesh,
                                size * 12.0,
                                tolerance=1e-3
                            )

                            samples.append(metrics)

                        self.emit_mesh_metrics(
                            "mesh_streaming",
                            samples,
                            productCount=size,
                            includeNormals=include_normals,
                            includeMaterials=False,
                            chunkScalarLimit=CHUNK_SIZE,
                            ifcEntityCount=fixture["ifcEntityCount"]
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