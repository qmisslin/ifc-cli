import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


EXECUTABLE = Path(
    sys.argv[1] if len(sys.argv) > 1 else "./build/ifc-cli"
).resolve()


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


class Client:
    def __init__(self):
        self.process = subprocess.Popen(
            [str(EXECUTABLE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        self.counter = 0

    def send(self, command, params):
        self.counter += 1
        request_id = str(self.counter)

        request = {
            "id": request_id,
            "command": command,
            "params": params
        }

        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()

        return request_id

    def read(self, request_id):
        line = self.process.stdout.readline()

        if not line:
            raise RuntimeError("CLI terminated unexpectedly")

        response = json.loads(line)

        assert response["id"] == request_id, response

        return response

    def request(self, command, params=None):
        request_id = self.send(command, params or {})
        response = self.read(request_id)

        if not response["ok"]:
            raise RuntimeError(
                f"{command}: {response['error']}"
            )

        return response["result"]

    def expect_error(self, command, params, code):
        request_id = self.send(command, params)
        response = self.read(request_id)

        assert not response["ok"], response
        assert response["error"]["code"] == code, response

    def tessellate(
        self,
        transform_id,
        kind="SOLID",
        geometry_id=None,
        space="world",
        include_children=False
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

        result = {
            "vertices": [],
            "indices": [],
            "normals": []
        }

        begin = None

        while True:
            response = self.read(request_id)

            if not response["ok"]:
                raise RuntimeError(response["error"])

            event = response["result"]
            event_name = event["event"]

            if event_name == "mesh.begin":
                begin = event

            elif event_name in (
                "mesh.vertices",
                "mesh.indices",
                "mesh.normals"
            ):
                key = event_name.split(".")[1]

                assert event["offset"] == len(result[key])

                result[key].extend(event["values"])

            elif event_name == "mesh.end":
                break

        assert begin is not None
        assert len(result["vertices"]) == begin["vertexCount"] * 3
        assert len(result["indices"]) == begin["indexCount"]
        assert len(result["indices"]) % 3 == 0

        return result

    def close(self):
        if self.process.poll() is None:
            self.request("exit")
            self.process.wait(timeout=10)

    def terminate(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


def bounds(mesh):
    vertices = mesh["vertices"]

    assert vertices, "Mesh has no vertices"

    return tuple(
        round(function(vertices[axis::3]), 6)
        for function in (min, max)
        for axis in range(3)
    )


def assert_closed(mesh):
    vertices = mesh["vertices"]
    indices = mesh["indices"]

    points = [
        tuple(round(value, 7) for value in vertices[i:i + 3])
        for i in range(0, len(vertices), 3)
    ]

    edges = Counter()

    for i in range(0, len(indices), 3):
        a, b, c = (
            points[index]
            for index in indices[i:i + 3]
        )

        for edge in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted(edge))] += 1

    assert edges
    assert all(count == 2 for count in edges.values()), edges


def create_domain(client, parent_id, name, x=0):
    return client.request(
        "geometryCreate",
        {
            "parentId": parent_id,
            "name": name,
            "kind": "DOMAIN",
            "transform": {
                "space": "parent",
                "matrix": matrix(x)
            },
            "profile": [
                [0, 0],
                [5, 0],
                [5, 3],
                [0, 3]
            ],
            "depth": 2,
            "operation": "ADD"
        }
    )


def find_transform(client, global_id):
    return next(
        transform["id"]
        for transform in client.request("transformGetAll")
        if transform["globalId"] == global_id
    )


def list_geometries(client, parent_id, kind):
    return client.request(
        "geometryGetAll",
        {
            "transformId": parent_id,
            "kind": kind
        }
    )


def run_tests(client, input_path, output_path):
    capabilities = client.request("getCapabilities")

    assert "DOMAIN" in capabilities["supportedGeometryKinds"]

    client.request("load", {"path": str(input_path)})

    parent_global_id = "0000000000000000000001"
    parent_id = find_transform(client, parent_global_id)

    baseline = client.tessellate(parent_id)

    assert bounds(baseline) == (-1, -0.5, 0, 1, 0.5, 1)

    print("PASS: baseline physical mesh")

    domain = create_domain(client, parent_id, "Domain A")
    domain_id = domain["id"]
    domain_global_id = domain["globalId"]

    assert domain["kind"] == "DOMAIN"
    assert domain["editable"] is True
    assert domain["ifcClass"] == "IfcSpatialZone"

    print("PASS: domain creation")

    solids = list_geometries(client, parent_id, "SOLID")
    domains = list_geometries(client, parent_id, "DOMAIN")
    all_geometries = list_geometries(client, parent_id, "ALL")

    assert len(solids) == 1
    assert len(domains) == 1
    assert len(all_geometries) == 2

    assert domains[0]["id"] == domain_id
    assert all(item["kind"] == "SOLID" for item in solids)
    assert all(item["kind"] == "DOMAIN" for item in domains)

    print("PASS: geometry filtering")

    solid_mesh = client.tessellate(parent_id, "SOLID")

    assert bounds(solid_mesh) == bounds(baseline)
    assert len(solid_mesh["indices"]) == len(baseline["indices"])

    domain_mesh = client.tessellate(
        parent_id,
        "DOMAIN",
        domain_id
    )

    assert bounds(domain_mesh) == (0, 0, 0, 5, 3, 2)
    assert_closed(domain_mesh)

    print("PASS: independent watertight domain mesh")

    second_domain = create_domain(
        client,
        parent_id,
        "Domain B",
        x=20
    )

    all_domains_mesh = client.tessellate(parent_id, "DOMAIN")

    assert bounds(all_domains_mesh) == (0, 0, 0, 25, 3, 2)

    domain_mesh = client.tessellate(
        parent_id,
        "DOMAIN",
        domain_id
    )

    assert bounds(domain_mesh) == (0, 0, 0, 5, 3, 2)

    print("PASS: individual domain selection")

    updated = client.request(
        "geometryUpdate",
        {
            "id": domain_id,
            "name": "Updated domain",
            "depth": 4,
            "profile": [
                [0, 0],
                [8, 0],
                [8, 6],
                [0, 6]
            ],
            "transform": {
                "space": "parent",
                "matrix": matrix(2, 3, 1)
            }
        }
    )

    assert updated["kind"] == "DOMAIN"
    assert updated["depth"] == 4
    assert updated["name"] == "Updated domain"

    domain_mesh = client.tessellate(
        parent_id,
        "DOMAIN",
        domain_id
    )

    assert bounds(domain_mesh) == (2, 3, 1, 10, 9, 5)
    assert_closed(domain_mesh)

    print("PASS: domain update")

    client.expect_error(
        "geometryUpdate",
        {
            "id": domain_id,
            "operation": "SUBTRACT"
        },
        "INVALID_PARAMETERS"
    )

    print("PASS: invalid domain operation rejected")

    client.request(
        "transformUpdate",
        {
            "id": parent_id,
            "transform": {
                "space": "world",
                "matrix": matrix(100, 0, 0)
            }
        }
    )

    world_mesh = client.tessellate(
        parent_id,
        "DOMAIN",
        domain_id
    )

    local_mesh = client.tessellate(
        parent_id,
        "DOMAIN",
        domain_id,
        space="local"
    )

    assert bounds(world_mesh) == (102, 3, 1, 110, 9, 5)
    assert bounds(local_mesh) == (2, 3, 1, 10, 9, 5)

    print("PASS: parent movement and coordinate spaces")

    duplicate = client.request(
        "transformDuplicate",
        {
            "id": parent_id,
            "includeChildren": False
        }
    )

    duplicate_id = duplicate["rootId"]

    duplicated_domains = list_geometries(
        client,
        duplicate_id,
        "DOMAIN"
    )

    assert len(duplicated_domains) == 2
    assert domain_id in duplicate["geometries"]

    print("PASS: domain duplication")

    client.request(
        "transformDelete",
        {"id": duplicate_id}
    )

    client.request(
        "geometryDelete",
        {"id": second_domain["id"]}
    )

    client.request("save", {"path": str(output_path)})
    client.request("load", {"path": str(output_path)})

    parent_id = find_transform(client, parent_global_id)

    reloaded_domains = list_geometries(
        client,
        parent_id,
        "DOMAIN"
    )

    assert len(reloaded_domains) == 1

    reloaded_domain = reloaded_domains[0]

    assert reloaded_domain["globalId"] == domain_global_id
    assert reloaded_domain["editable"] is True

    domain_id = reloaded_domain["id"]

    domain_mesh = client.tessellate(
        parent_id,
        "DOMAIN",
        domain_id
    )

    assert bounds(domain_mesh) == (102, 3, 1, 110, 9, 5)
    assert_closed(domain_mesh)

    print("PASS: IFC save and reload")

    client.request(
        "geometryDelete",
        {"id": domain_id}
    )

    assert not list_geometries(
        client,
        parent_id,
        "DOMAIN"
    )

    empty_mesh = client.tessellate(parent_id, "DOMAIN")

    assert not empty_mesh["vertices"]
    assert not empty_mesh["indices"]

    print("PASS: domain deletion")

    client.request("save", {"path": str(output_path)})
    client.request("load", {"path": str(output_path)})

    assert not list_geometries(client, None, "DOMAIN")

    print("PASS: persistent domain deletion")


def main():
    if not EXECUTABLE.is_file():
        raise FileNotFoundError(EXECUTABLE)

    with tempfile.TemporaryDirectory() as directory:
        input_path = Path(directory) / "input.ifc"
        output_path = Path(directory) / "output.ifc"

        input_path.write_text(IFC_FIXTURE, encoding="utf-8")

        client = Client()

        try:
            run_tests(client, input_path, output_path)
            client.close()

            print("All domain integration tests passed.")
        finally:
            client.terminate()


if __name__ == "__main__":
    main()