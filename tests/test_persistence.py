import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from collections import Counter
from itertools import count
from pathlib import Path

import ifcopenshell
import networkx as nx


# Integration test dependencies:
# python3 -m pip install ifcopenshell "networkx>=3.2"
#
# Run:
# python3 tests/test_persistence.py ./build/ifc-cli
#
# Not executed against the compiled CLI.
#
# IFC files are compared as directed semantic graphs.
# STEP identifiers, entity serialization order and formatting are ignored.
# SET and BAG aggregates are unordered, while LIST and ARRAY aggregates
# preserve their original ordering.
#
# Shared IFC references, entity multiplicity, GlobalIds and native
# attributes are preserved during graph comparison.
#
# The Python IfcOpenShell package provides a parser independent of the
# CLI executable, but uses the same underlying IFC parsing technology.
# This suite verifies readability and semantic integrity, not complete
# compliance with every IFC EXPRESS WHERE rule.

DEFAULT_EXECUTABLE = "./build/ifc-cli"
REQUEST_TIMEOUT = 60
NUMERIC_TOLERANCE = 1e-8
MESH_TOLERANCE = 1e-6
ROUNDTRIP_COUNT = 10

EXECUTABLE = Path(DEFAULT_EXECUTABLE).resolve()

ROOT_GLOBAL_ID = "0000000000000000000001"
SURVIVOR_GLOBAL_ID = "0000000000000000000002"
STOREY_GLOBAL_ID = "0000000000000000000003"
BUILDING_GLOBAL_ID = "0000000000000000000004"
PROJECT_GLOBAL_ID = "0000000000000000000005"
WALL_TYPE_GLOBAL_ID = "0000000000000000000006"
GROUP_GLOBAL_ID = "0000000000000000000007"

DEFAULT_PROFILE = [
    [0, 0],
    [2, 0],
    [2, 3],
    [0, 3]
]

DEFAULT_VISUAL = {
    "color": [0.25, 0.5, 0.75],
    "opacity": 0.85,
    "metallic": 0.2,
    "roughness": 0.6
}

IFC_FIXTURE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [DesignTransferView]'),'2;1');
FILE_NAME('persistence-test.ifc','2026-01-01T00:00:00',('Test Author'),('Test Organization'),'Test','Test','');
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
#9=IFCUNITASSIGNMENT((#6,#7,#8));
#10=IFCPROJECT('0000000000000000000005',$,'Persistence project','Project description',$,'Project long name','Design phase',(#5),#9);
#11=IFCLOCALPLACEMENT($,#4);
#12=IFCBUILDING('0000000000000000000004',$,'Native building','Building description',$,#11,$,'Building long name',.ELEMENT.,$,$,$);
#13=IFCLOCALPLACEMENT(#11,#4);
#14=IFCBUILDINGSTOREY('0000000000000000000003',$,'Native storey','Storey description',$,#13,$,'Storey long name',.ELEMENT.,0.);
#15=IFCLOCALPLACEMENT(#13,#4);
#16=IFCCARTESIANPOINT((0.,0.));
#17=IFCAXIS2PLACEMENT2D(#16,$);
#18=IFCRECTANGLEPROFILEDEF(.AREA.,'Native rectangle',#17,4.,3.);
#19=IFCEXTRUDEDAREASOLID(#18,#4,#2,5.);
#20=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#19));
#21=IFCPRODUCTDEFINITIONSHAPE('Native representation','Native representation description',(#20));
#22=IFCWALL('0000000000000000000001',$,'Native wall','Original wall description','Original wall object type',#15,#21,'WALL-001',.STANDARD.);
#23=IFCCARTESIANPOINT((20.,0.,0.));
#24=IFCAXIS2PLACEMENT3D(#23,#2,#3);
#25=IFCLOCALPLACEMENT(#13,#24);
#26=IFCEXTRUDEDAREASOLID(#18,#4,#2,5.);
#27=IFCSHAPEREPRESENTATION(#5,'Body','SweptSolid',(#26));
#28=IFCPRODUCTDEFINITIONSHAPE($,$,(#27));
#29=IFCBUILDINGELEMENTPROXY('0000000000000000000002',$,'Preserved native product','Preserved description','Preserved object type',#25,#28,'PROXY-001',.NOTDEFINED.);
#30=IFCRELAGGREGATES('0000000000000000000008',$,'Project decomposition',$,#10,(#12));
#31=IFCRELAGGREGATES('0000000000000000000009',$,'Building decomposition',$,#12,(#14));
#32=IFCRELCONTAINEDINSPATIALSTRUCTURE('0000000000000000000010',$,'Storey containment',$,(#22,#29),#14);
#33=IFCPROPERTYSINGLEVALUE('Reference','Native property description',IFCLABEL('NATIVE-WALL'),$);
#34=IFCPROPERTYSINGLEVALUE('ReferenceLength',$,IFCLENGTHMEASURE(4.),#6);
#35=IFCPROPERTYLISTVALUE('NativeList',$,(IFCLABEL('First'),IFCLABEL('Second'),IFCLABEL('Third')),$);
#36=IFCPROPERTYSINGLEVALUE('NestedValue',$,IFCLABEL('NATIVE-COMPLEX-VALUE'),$);
#37=IFCCOMPLEXPROPERTY('NativeComplexProperty','Complex property description','NativeUsage',(#36));
#38=IFCPROPERTYSET('0000000000000000000011',$,'Pset_NativeWall','Native property set description',(#33,#34,#35,#37));
#39=IFCRELDEFINESBYPROPERTIES('0000000000000000000012',$,$,$,(#22),#38);
#40=IFCPROPERTYSINGLEVALUE('SharedReference',$,IFCLABEL('SHARED-VALUE'),$);
#41=IFCPROPERTYSET('0000000000000000000013',$,'Pset_Shared',$,(#40));
#42=IFCRELDEFINESBYPROPERTIES('0000000000000000000014',$,$,$,(#22,#29),#41);
#43=IFCQUANTITYLENGTH('Length',$,#6,4.,$);
#44=IFCQUANTITYAREA('Area',$,#7,12.,$);
#45=IFCQUANTITYVOLUME('Volume',$,#8,60.,$);
#46=IFCELEMENTQUANTITY('0000000000000000000015',$,'Qto_NativeWall','Native quantities','Analytical',(#43,#44,#45));
#47=IFCRELDEFINESBYPROPERTIES('0000000000000000000016',$,$,$,(#22),#46);
#48=IFCMATERIAL('Native concrete','Native material description','concrete');
#49=IFCRELASSOCIATESMATERIAL('0000000000000000000017',$,'Native material assignment',$,(#22,#29),#48);
#50=IFCPROPERTYSINGLEVALUE('NativeDensity',$,IFCREAL(2400.),$);
#51=IFCMATERIALPROPERTIES('Pset_NativeMaterial',$,(#50),#48);
#52=IFCWALLTYPE('0000000000000000000006',$,'Native wall type','Native type description',$,$,$,'TYPE-001','Native element type',.STANDARD.);
#53=IFCRELDEFINESBYTYPE('0000000000000000000018',$,'Native type relation',$,(#22),#52);
#54=IFCGROUP('0000000000000000000007',$,'Native group','Native group description','Native group object type');
#55=IFCRELASSIGNSTOGROUP('0000000000000000000019',$,'Native group relation',$,(#22,#29),$,#54);
ENDSEC;
END-ISO-10303-21;
"""


def translation_matrix(x=0.0, y=0.0, z=0.0):
    return [
        1, 0, 0, x,
        0, 1, 0, y,
        0, 0, 1, z,
        0, 0, 0, 1
    ]


def rotation_z(angle):
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)

    return [
        cosine, -sine, 0, 0,
        sine, cosine, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1
    ]


def multiply_matrices(left, right):
    return [
        math.fsum(
            left[row * 4 + index] * right[index * 4 + column]
            for index in range(4)
        )
        for row in range(4)
        for column in range(4)
    ]


def transform_input(matrix=None, space="parent"):
    return {
        "space": space,
        "matrix": translation_matrix() if matrix is None else matrix
    }


def parents_input(spatial=None, placement=None, decomposition=None):
    return {
        "spatial": spatial,
        "placement": placement,
        "decomposition": decomposition
    }


def rectangular_profile(width, length):
    return [
        [0, 0],
        [width, 0],
        [width, length],
        [0, length]
    ]


def property_input(
    name,
    value,
    value_type="IfcLabel",
    property_set="Pset_Persistence",
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


def sorted_properties(properties):
    return sorted(
        properties,
        key=lambda item: (
            item["source"],
            item["propertySet"],
            item["name"]
        )
    )


def normalize_number(value):
    if not math.isfinite(value):
        raise AssertionError(f"Non-finite IFC number: {value}")

    return round(value, 10)


def aggregation_type(parameter_type):
    if parameter_type is None:
        return None

    direct = parameter_type.as_aggregation_type()

    if direct is not None:
        return direct

    named = parameter_type.as_named_type()

    if named is None:
        return None

    declaration = named.declared_type()
    type_declaration = declaration.as_type_declaration()

    if type_declaration is None:
        return None

    return aggregation_type(type_declaration.declared_type())


def ifc_references(value):
    if isinstance(value, ifcopenshell.entity_instance):
        if value.id() > 0:
            yield value
        else:
            for item in value:
                yield from ifc_references(item)

    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from ifc_references(item)


def entity_closure(roots):
    pending = list(roots)
    entities = {}

    while pending:
        entity = pending.pop()
        identifier = entity.id()

        if identifier in entities:
            continue

        if identifier <= 0:
            raise AssertionError(f"Invalid graph root: {entity}")

        entities[identifier] = entity

        for value in entity:
            pending.extend(ifc_references(value))

    return entities


def semantic_graph(model, roots=None):
    schema = ifcopenshell.ifcopenshell_wrapper.schema_by_name(
        model.schema_identifier
    )

    selected_roots = None if roots is None else list(roots)

    entities = (
        {entity.id(): entity for entity in model}
        if selected_roots is None
        else entity_closure(selected_roots)
    )

    root_ids = {
        entity.id()
        for entity in selected_roots or []
    }

    graph = nx.DiGraph()
    node_counter = count()

    def new_node(label):
        identifier = ("value", next(node_counter))
        graph.add_node(identifier, label=label)
        return identifier

    for identifier, entity in entities.items():
        graph.add_node(
            ("entity", identifier),
            label=(
                "entity",
                entity.is_a(),
                getattr(entity, "GlobalId", None),
                identifier in root_ids
            )
        )

    def append_value(parent, value, parameter_type=None):
        if isinstance(value, ifcopenshell.entity_instance):
            if value.id() > 0:
                target = ("entity", value.id())

                if target not in graph:
                    raise AssertionError(
                        f"Reference to missing IFC entity: {value}"
                    )

                graph.add_edge(parent, target)
                return

            # Inline IFC simple values retain their declared type.

            node = new_node(("typed", value.is_a()))
            graph.add_edge(parent, node)

            declaration = schema.declaration_by_name(value.is_a())
            type_declaration = declaration.as_type_declaration()

            inner_type = (
                type_declaration.declared_type()
                if type_declaration is not None
                else None
            )

            for index, item in enumerate(value):
                slot = new_node(("typed-attribute", index))
                graph.add_edge(node, slot)
                append_value(slot, item, inner_type)

            return

        if isinstance(value, (tuple, list)):
            aggregate = aggregation_type(parameter_type)

            aggregate_name = (
                aggregate.type_of_aggregation_string().lower()
                if aggregate is not None
                else "list"
            )

            unordered = aggregate_name in {"set", "bag"}

            aggregate_node = new_node((
                "aggregate",
                aggregate_name,
                len(value)
            ))

            graph.add_edge(parent, aggregate_node)

            element_type = (
                aggregate.type_of_element()
                if aggregate is not None
                else None
            )

            for index, item in enumerate(value):
                member = new_node((
                    "member",
                    None if unordered else index
                ))

                graph.add_edge(aggregate_node, member)
                append_value(member, item, element_type)

            return

        if value is None:
            label = ("null",)

        elif isinstance(value, bool):
            label = ("boolean", value)

        elif isinstance(value, int):
            label = ("integer", value)

        elif isinstance(value, float):
            label = ("real", normalize_number(value))

        elif isinstance(value, str):
            label = ("string", value)

        else:
            raise TypeError(
                f"Unsupported IFC attribute value: {value!r}"
            )

        node = new_node(label)
        graph.add_edge(parent, node)

    for identifier, entity in entities.items():
        declaration = schema.declaration_by_name(
            entity.is_a()
        ).as_entity()

        attributes = declaration.all_attributes()
        derived = declaration.derived()

        if len(attributes) != len(entity):
            raise AssertionError(
                f"Invalid IFC attribute count: {entity}"
            )

        parent = ("entity", identifier)

        for index, attribute in enumerate(attributes):
            slot = new_node((
                "attribute",
                attribute.name()
            ))

            graph.add_edge(parent, slot)

            if derived[index]:
                graph.add_edge(
                    slot,
                    new_node(("derived",))
                )
                continue

            append_value(
                slot,
                entity[index],
                attribute.type_of_attribute()
            )

    return graph


def semantic_graph_equal(first, second):
    if first.number_of_nodes() != second.number_of_nodes():
        return False

    if first.number_of_edges() != second.number_of_edges():
        return False

    first_labels = Counter(
        data["label"]
        for _, data in first.nodes(data=True)
    )

    second_labels = Counter(
        data["label"]
        for _, data in second.nodes(data=True)
    )

    if first_labels != second_labels:
        return False

    return nx.vf2pp_is_isomorphic(
        first,
        second,
        node_label="label"
    )


def ifc_class_counts(model):
    return Counter(
        entity.is_a()
        for entity in model
    )


def ifc_global_ids(model):
    return [
        entity.GlobalId
        for entity in model.by_type("IfcRoot")
    ]


def find_ifc_entity(model, global_id):
    entity = model.by_guid(global_id)

    if entity is None:
        raise AssertionError(
            f"Missing IFC GlobalId: {global_id}"
        )

    return entity


def material_by_name(model, name):
    matches = [
        material
        for material in model.by_type("IfcMaterial")
        if material.Name == name
    ]

    if len(matches) != 1:
        raise AssertionError(
            f"Expected one IFC material named {name}"
        )

    return matches[0]


def occurrence_property_sets(model, target):
    return [
        relation.RelatingPropertyDefinition
        for relation in model.by_type("IfcRelDefinesByProperties")
        if (
            target in relation.RelatedObjects
            and relation.RelatingPropertyDefinition.is_a("IfcPropertySet")
        )
    ]


def occurrence_property_set(model, target, name):
    matches = [
        property_set
        for property_set in occurrence_property_sets(model, target)
        if property_set.Name == name
    ]

    if len(matches) != 1:
        raise AssertionError(
            f"Expected one property set named {name}"
        )

    return matches[0]


def semantic_material_targets(model, material):
    return {
        target.GlobalId
        for relation in model.by_type("IfcRelAssociatesMaterial")
        if relation.RelatingMaterial == material
        for target in relation.RelatedObjects
    }


def managed_domain_parent(model, domain):
    relations = [
        relation
        for relation in model.by_type("IfcRelAssignsToProduct")
        if (
            relation.Name == "ifc-cli:domain-parent"
            and domain in relation.RelatedObjects
        )
    ]

    if len(relations) != 1:
        raise AssertionError(
            f"Invalid managed domain relation: {domain}"
        )

    return relations[0].RelatingProduct


def mesh_points(mesh):
    return [
        tuple(mesh["vertices"][offset:offset + 3])
        for offset in range(0, len(mesh["vertices"]), 3)
    ]


def mesh_triangles(mesh):
    points = mesh_points(mesh)
    indices = mesh["indices"]

    for offset in range(0, len(indices), 3):
        yield tuple(
            points[index]
            for index in indices[offset:offset + 3]
        )


def mesh_signature(mesh, precision=6):
    return Counter(
        tuple(sorted(
            tuple(round(value, precision) for value in point)
            for point in triangle
        ))
        for triangle in mesh_triangles(mesh)
    )


def mesh_volume(mesh):
    points = mesh_points(mesh)

    if not points:
        return 0.0

    origin = points[0]
    terms = []

    for triangle in mesh_triangles(mesh):
        a, b, c = [
            tuple(
                point[axis] - origin[axis]
                for axis in range(3)
            )
            for point in triangle
        ]

        cross = (
            b[1] * c[2] - b[2] * c[1],
            b[2] * c[0] - b[0] * c[2],
            b[0] * c[1] - b[1] * c[0]
        )

        terms.append(
            math.fsum(
                a[axis] * cross[axis]
                for axis in range(3)
            )
        )

    return abs(math.fsum(terms)) / 6.0


def mesh_surface_area(mesh):
    areas = []

    for a, b, c in mesh_triangles(mesh):
        u = tuple(b[axis] - a[axis] for axis in range(3))
        v = tuple(c[axis] - a[axis] for axis in range(3))

        cross = (
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0]
        )

        areas.append(
            math.sqrt(
                math.fsum(value * value for value in cross)
            ) / 2.0
        )

    return math.fsum(areas)


def mesh_bounds(mesh):
    points = mesh_points(mesh)

    if not points:
        return None

    return tuple(
        operation(point[axis] for point in points)
        for operation in (min, max)
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

        self.process.stdin.write(
            json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False
            ) + "\n"
        )

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

            if set(error) != {"code", "message", "details"}:
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

    def tessellate(
        self,
        transform_id,
        kind="SOLID",
        space="local",
        geometry_id=None,
        include_children=False,
        include_materials=True
    ):
        options = {
            "space": space,
            "kind": kind,
            "includeChildren": include_children,
            "includeNormals": False,
            "includeMaterials": include_materials
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
            "groups": []
        }

        event_order = {
            "mesh.begin": 0,
            "mesh.vertices": 1,
            "mesh.indices": 2,
            "mesh.materials": 3,
            "mesh.end": 4
        }

        begin = None
        last_event = -1
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
                    f"Unexpected mesh event: {event}"
                )

            current_order = event_order[event_name]

            if current_order < last_event:
                raise AssertionError(
                    f"Invalid mesh event order: {event}"
                )

            last_event = current_order

            if event_name == "mesh.begin":
                if begin is not None:
                    raise AssertionError("Duplicate mesh.begin")

                begin = event
                continue

            if begin is None:
                raise AssertionError(
                    "Mesh data received before mesh.begin"
                )

            if event_name == "mesh.end":
                break

            if event_name == "mesh.materials":
                if not include_materials or materials_received:
                    raise AssertionError(
                        "Unexpected mesh.materials event"
                    )

                mesh["groups"] = event["groups"]
                materials_received = True
                continue

            key = event_name.split(".")[1]

            if event["offset"] != len(mesh[key]):
                raise AssertionError(
                    f"Invalid mesh chunk offset: {event}"
                )

            mesh[key].extend(event["values"])

        if materials_received != include_materials:
            raise AssertionError(
                "Incorrect presence of material groups"
            )

        mesh["vertexCount"] = begin["vertexCount"]
        mesh["indexCount"] = begin["indexCount"]

        if len(mesh["vertices"]) != mesh["vertexCount"] * 3:
            raise AssertionError("Invalid vertex count")

        if len(mesh["indices"]) != mesh["indexCount"]:
            raise AssertionError("Invalid index count")

        if len(mesh["indices"]) % 3 != 0:
            raise AssertionError("Invalid triangle indices")

        for index in mesh["indices"]:
            if type(index) is not int or not 0 <= index < mesh["vertexCount"]:
                raise AssertionError(
                    f"Invalid vertex index: {index}"
                )

        if not all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in mesh["vertices"]
        ):
            raise AssertionError("Invalid mesh coordinates")

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


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="ifc-cli-persistence-"
        )

        self.addCleanup(self.directory.cleanup)

        self.root_path = Path(self.directory.name)
        self.input_path = self.root_path / "input.ifc"

        self.input_path.write_text(
            IFC_FIXTURE,
            encoding="utf-8"
        )

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.save_counter = 0

        self.original_model = ifcopenshell.open(
            str(self.input_path)
        )

        self.original_graph = semantic_graph(
            self.original_model
        )

        self.load_model(self.input_path)

    def tearDown(self):
        self.client.close()

    def load_model(self, path):
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
        self.save_counter += 1

        path = self.root_path / f"roundtrip-{self.save_counter}.ifc"

        result = self.client.request(
            "save",
            {"path": str(path)}
        )

        self.assertEqual(
            result,
            {"path": str(path)}
        )

        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)

        return path

    def save_and_parse(self):
        path = self.save_model()
        model = ifcopenshell.open(str(path))

        self.assert_ifc_integrity(model)

        return path, model

    def save_and_reload(self):
        path, model = self.save_and_parse()
        result = self.load_model(path)

        return path, model, result

    @property
    def root_id(self):
        return self.find_transform(ROOT_GLOBAL_ID)["id"]

    @property
    def survivor_id(self):
        return self.find_transform(SURVIVOR_GLOBAL_ID)["id"]

    @property
    def storey_id(self):
        return self.find_transform(STOREY_GLOBAL_ID)["id"]

    @property
    def building_id(self):
        return self.find_transform(BUILDING_GLOBAL_ID)["id"]

    @property
    def project_id(self):
        return self.find_transform(PROJECT_GLOBAL_ID)["id"]

    def list_transforms(self):
        return self.client.request("transformGetAll")

    def list_geometries(self, parent_id=None, kind="ALL"):
        return self.client.request(
            "geometryGetAll",
            {
                "transformId": parent_id,
                "kind": kind
            }
        )

    def list_materials(self):
        return self.client.request("materialGetAll")

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

    def find_transform(self, global_id):
        matches = [
            item
            for item in self.list_transforms()
            if item["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one transform with GlobalId {global_id}"
        )

        return self.get_transform(matches[0]["id"])

    def find_geometry(self, global_id):
        matches = [
            item
            for item in self.list_geometries()
            if item["globalId"] == global_id
        ]

        self.assertEqual(
            len(matches),
            1,
            f"Expected one geometry with GlobalId {global_id}"
        )

        return self.get_geometry(matches[0]["id"])

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

    def create_transform(
        self,
        name="Created transform",
        parents=None,
        matrix=None,
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
                "transform": transform_input(matrix),
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

    def create_geometry(
        self,
        parent_id=None,
        name="Created geometry",
        kind="SOLID",
        operation="ADD",
        profile=None,
        depth=2.0,
        matrix=None
    ):
        return self.client.request(
            "geometryCreate",
            {
                "parentId": (
                    self.root_id
                    if parent_id is None
                    else parent_id
                ),
                "name": name,
                "kind": kind,
                "operation": operation,
                "profile": (
                    DEFAULT_PROFILE
                    if profile is None
                    else profile
                ),
                "depth": depth,
                "transform": transform_input(matrix)
            }
        )

    def create_domain(self, **options):
        return self.create_geometry(
            kind="DOMAIN",
            operation="ADD",
            **options
        )

    def update_geometry(self, identifier, **changes):
        return self.client.request(
            "geometryUpdate",
            {
                "id": identifier,
                **changes
            }
        )

    def create_material(
        self,
        name="Created material",
        category="concrete",
        visual=None,
        properties=None
    ):
        return self.client.request(
            "materialCreate",
            {
                "name": name,
                "category": category,
                "visual": (
                    DEFAULT_VISUAL
                    if visual is None
                    else visual
                ),
                "properties": (
                    []
                    if properties is None
                    else properties
                )
            }
        )

    def assign_material(self, material_id, target_id):
        params = {
            "materialId": material_id,
            "targetId": target_id
        }

        self.assertEqual(
            self.client.request("materialAssign", params),
            params
        )

    def tessellate(self, identifier, **options):
        return self.client.tessellate(
            identifier,
            **options
        )

    def tessellate_domain(self, geometry, **options):
        return self.tessellate(
            geometry["parentId"],
            kind="DOMAIN",
            geometry_id=geometry["id"],
            **options
        )

    def assert_ifc_integrity(self, model):
        self.assertEqual(model.schema.upper(), "IFC4")

        entities = {
            entity.id(): entity
            for entity in model
        }

        self.assertTrue(entities)

        self.assertEqual(
            len(entities),
            sum(1 for _ in model)
        )

        global_ids = ifc_global_ids(model)

        self.assertEqual(
            len(global_ids),
            len(set(global_ids)),
            "Duplicate IFC GlobalIds"
        )

        for global_id in global_ids:
            self.assertRegex(
                global_id,
                r"^[0-3][0-9A-Za-z_$]{21}$"
            )

        for entity in entities.values():
            for index, value in enumerate(entity):
                for reference in ifc_references(value):
                    self.assertIn(
                        reference.id(),
                        entities,
                        f"Dangling IFC reference in "
                        f"{entity.is_a()}.{entity.attribute_name(index)}"
                    )

        projects = model.by_type("IfcProject")

        self.assertEqual(len(projects), 1)
        self.assertIsNotNone(projects[0].UnitsInContext)
        self.assertTrue(projects[0].RepresentationContexts)

    def assert_semantic_models_equal(self, first, second):
        self.assertEqual(
            first.schema_identifier,
            second.schema_identifier
        )

        self.assertEqual(
            ifc_class_counts(first),
            ifc_class_counts(second),
            "IFC entity multiplicity changed"
        )

        self.assertEqual(
            set(ifc_global_ids(first)),
            set(ifc_global_ids(second)),
            "IFC GlobalIds changed"
        )

        first_graph = semantic_graph(first)
        second_graph = semantic_graph(second)

        self.assertTrue(
            semantic_graph_equal(first_graph, second_graph),
            "IFC semantic graphs differ"
        )

    def assert_native_subgraphs_equal(
        self,
        first_model,
        first_roots,
        second_model,
        second_roots
    ):
        first_graph = semantic_graph(
            first_model,
            roots=first_roots
        )

        second_graph = semantic_graph(
            second_model,
            roots=second_roots
        )

        self.assertTrue(
            semantic_graph_equal(first_graph, second_graph),
            "Native IFC subgraph changed"
        )

    def assert_matrix(self, actual, expected):
        self.assertEqual(len(actual), 16)
        self.assertEqual(len(expected), 16)

        for index, (value, reference) in enumerate(
            zip(actual, expected)
        ):
            self.assertAlmostEqual(
                value,
                reference,
                delta=NUMERIC_TOLERANCE,
                msg=f"Matrix component {index} changed"
            )

    def assert_profile(self, actual, expected):
        self.assertEqual(len(actual), len(expected))

        for point, reference in zip(actual, expected):
            self.assertEqual(len(point), 2)

            for value, coordinate in zip(point, reference):
                self.assertAlmostEqual(
                    value,
                    coordinate,
                    delta=NUMERIC_TOLERANCE
                )

    def assert_properties(self, actual, expected):
        actual = sorted_properties(actual)
        expected = sorted_properties(expected)

        self.assertEqual(len(actual), len(expected))

        for value, reference in zip(actual, expected):
            self.assertEqual(set(value), set(reference))

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

            if isinstance(reference["value"], float):
                self.assertAlmostEqual(
                    value["value"],
                    reference["value"],
                    delta=NUMERIC_TOLERANCE
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

    def assert_visual(self, actual, expected):
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(len(actual["color"]), 3)

        for value, reference in zip(
            actual["color"],
            expected["color"]
        ):
            self.assertAlmostEqual(
                value,
                reference,
                delta=NUMERIC_TOLERANCE
            )

        for field in ("opacity", "metallic", "roughness"):
            self.assertAlmostEqual(
                actual[field],
                expected[field],
                delta=NUMERIC_TOLERANCE
            )

    def assert_mesh_equivalent(
        self,
        before,
        after,
        exact_triangles=True
    ):
        self.assertAlmostEqual(
            mesh_volume(before),
            mesh_volume(after),
            delta=MESH_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_surface_area(before),
            mesh_surface_area(after),
            delta=MESH_TOLERANCE
        )

        before_bounds = mesh_bounds(before)
        after_bounds = mesh_bounds(after)

        if before_bounds is None:
            self.assertIsNone(after_bounds)
            self.assertEqual(after["indexCount"], 0)
            return

        self.assertIsNotNone(after_bounds)

        for value, reference in zip(
            after_bounds,
            before_bounds
        ):
            self.assertAlmostEqual(
                value,
                reference,
                delta=MESH_TOLERANCE
            )

        if exact_triangles:
            self.assertEqual(
                mesh_signature(before),
                mesh_signature(after)
            )

    def persistent_transform(self, transform):
        parents = {
            field: (
                self.get_transform(parent_id)["globalId"]
                if parent_id is not None
                else None
            )
            for field, parent_id in transform["parents"].items()
        }

        materials = [
            self.get_material(identifier)["name"]
            for identifier in transform["materials"]
        ]

        geometries = [
            self.get_geometry(identifier)
            for identifier in transform["geometries"]
        ]

        return {
            "globalId": transform["globalId"],
            "name": transform["name"],
            "ifcClass": transform["definition"]["ifcClass"],
            "parents": parents,
            "transforms": transform["transforms"],
            "properties": sorted_properties(
                transform["properties"]
            ),
            "materials": sorted(materials),
            "geometryCount": len(geometries),
            "geometryKinds": Counter(
                geometry["kind"]
                for geometry in geometries
            )
        }

    def persistent_geometry(self, geometry):
        result = {
            "globalId": geometry["globalId"],
            "parentGlobalId": self.get_transform(
                geometry["parentId"]
            )["globalId"],
            "source": geometry["source"],
            "kind": geometry["kind"],
            "operation": geometry["operation"],
            "editable": geometry["editable"],
            "ifcClass": geometry["ifcClass"]
        }

        if geometry["source"] == "editor":
            result.update({
                "name": geometry["name"],
                "profile": geometry["profile"],
                "depth": geometry["depth"],
                "transforms": geometry["transforms"],
                "materials": sorted(
                    self.get_material(identifier)["name"]
                    for identifier in geometry["materials"]
                )
            })

        return result

    def persistent_material(self, material):
        return {
            "name": material["name"],
            "category": material["category"],
            "editable": material["editable"],
            "globalId": material["globalId"],
            "visual": material["visual"],
            "properties": sorted_properties(
                material["properties"]
            ),
            "ifcClass": material["definition"]["ifcClass"]
        }

    def persistent_snapshot(self):
        transforms = [
            self.persistent_transform(
                self.get_transform(item["id"])
            )
            for item in self.list_transforms()
        ]

        geometries = [
            self.persistent_geometry(
                self.get_geometry(item["id"])
            )
            for item in self.list_geometries()
        ]

        materials = [
            self.persistent_material(
                self.get_material(item["id"])
            )
            for item in self.list_materials()
        ]

        # Session identifiers and STEP identifiers are deliberately
        # excluded from persistence comparisons.

        return {
            "transforms": sorted(
                transforms,
                key=lambda item: item["globalId"] or ""
            ),
            "geometries": sorted(
                geometries,
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True
                )
            ),
            "materials": sorted(
                materials,
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True
                )
            )
        }

    def assert_persistent_equal(self, actual, expected, path="model"):
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict)
            self.assertEqual(set(actual), set(expected))

            for key in expected:
                self.assert_persistent_equal(
                    actual[key],
                    expected[key],
                    f"{path}.{key}"
                )

        elif isinstance(expected, (list, tuple)):
            self.assertIsInstance(actual, (list, tuple))
            self.assertEqual(len(actual), len(expected))

            for index, (value, reference) in enumerate(
                zip(actual, expected)
            ):
                self.assert_persistent_equal(
                    value,
                    reference,
                    f"{path}[{index}]"
                )

        elif isinstance(expected, float):
            self.assertAlmostEqual(
                actual,
                expected,
                delta=NUMERIC_TOLERANCE,
                msg=f"Persistent numeric value changed: {path}"
            )

        else:
            self.assertEqual(
                actual,
                expected,
                f"Persistent value changed: {path}"
            )

    def assert_session_roundtrip(self):
        expected = self.persistent_snapshot()

        path, saved_model, _ = self.save_and_reload()

        self.assert_persistent_equal(
            self.persistent_snapshot(),
            expected
        )

        _, second_model = self.save_and_parse()

        self.assert_semantic_models_equal(
            saved_model,
            second_model
        )

        return path, saved_model

    def test_unmodified_roundtrip(self):
        """Verify semantic preservation without model modifications."""

        initial_snapshot = self.persistent_snapshot()

        initial_root_mesh = self.tessellate(
            self.root_id,
            space="world"
        )

        initial_survivor_mesh = self.tessellate(
            self.survivor_id,
            space="world"
        )

        original_bytes = self.input_path.read_bytes()

        path, saved_model, _ = self.save_and_reload()

        self.assertNotEqual(path, self.input_path)

        self.assertEqual(
            self.input_path.read_bytes(),
            original_bytes,
            "Saving unexpectedly modified the input file"
        )

        self.assert_semantic_models_equal(
            self.original_model,
            saved_model
        )

        self.assertTrue(
            semantic_graph_equal(
                self.original_graph,
                semantic_graph(saved_model)
            )
        )

        self.assert_persistent_equal(
            self.persistent_snapshot(),
            initial_snapshot
        )

        self.assert_mesh_equivalent(
            initial_root_mesh,
            self.tessellate(
                self.root_id,
                space="world"
            )
        )

        self.assert_mesh_equivalent(
            initial_survivor_mesh,
            self.tessellate(
                self.survivor_id,
                space="world"
            )
        )

        original_header = self.original_model.header
        saved_header = saved_model.header

        self.assertEqual(
            tuple(saved_header.file_description.description),
            tuple(original_header.file_description.description)
        )

        self.assertEqual(
            tuple(saved_header.file_schema.schema_identifiers),
            tuple(original_header.file_schema.schema_identifiers)
        )

        # Validate semantic comparison against deliberate STEP renumbering.

        serialized = saved_model.to_string()

        import re

        identifiers = {
            entity.id(): entity.id() + 10000
            for entity in saved_model
        }

        token_pattern = re.compile(
            r"'(?:[^']|'')*'|#(\d+)"
        )

        def replace_reference(match):
            if match.group(1) is None:
                return match.group(0)

            identifier = int(match.group(1))

            return f"#{identifiers[identifier]}"

        renumbered = ifcopenshell.file.from_string(
            token_pattern.sub(
                replace_reference,
                serialized
            )
        )

        self.assertTrue(
            semantic_graph_equal(
                semantic_graph(saved_model),
                semantic_graph(renumbered)
            )
        )

        self.assertNotEqual(
            {entity.id() for entity in saved_model},
            {entity.id() for entity in renumbered}
        )

    def test_transform_roundtrip(self):
        """Verify persistence of created and modified transforms."""

        matrix = multiply_matrices(
            translation_matrix(10.0, 20.0, 30.0),
            rotation_z(35.0)
        )

        updated = self.update_transform(
            self.root_id,
            name="Updated native wall",
            transform=transform_input(
                matrix,
                space="world"
            )
        )

        first = self.create_transform(
            name="Persistent independent transform",
            matrix=translation_matrix(100.0, 200.0, 300.0)
        )

        child_matrix = multiply_matrices(
            translation_matrix(2.0, 3.0, 4.0),
            rotation_z(25.0)
        )

        second = self.create_transform(
            name="Persistent child transform",
            parents=parents_input(
                spatial=self.storey_id,
                placement=self.root_id,
                decomposition=self.root_id
            ),
            matrix=child_matrix
        )

        expected = {
            transform["globalId"]: self.persistent_transform(
                transform
            )
            for transform in (updated, first, second)
        }

        _, model = self.assert_session_roundtrip()

        for global_id, reference in expected.items():
            with self.subTest(global_id=global_id):
                current = self.find_transform(global_id)

                self.assert_persistent_equal(
                    self.persistent_transform(current),
                    reference
                )

                native = find_ifc_entity(
                    model,
                    global_id
                )

                self.assertEqual(
                    native.Name,
                    reference["name"]
                )

                self.assertIsNotNone(
                    native.ObjectPlacement
                )

        current_root = self.find_transform(
            updated["globalId"]
        )

        current_child = self.find_transform(
            second["globalId"]
        )

        self.assert_matrix(
            current_root["transforms"]["world"],
            matrix
        )

        self.assert_matrix(
            current_child["transforms"]["local"],
            child_matrix
        )

        self.assert_matrix(
            current_child["transforms"]["world"],
            multiply_matrices(matrix, child_matrix)
        )

        current = self.update_transform(
            current_child["id"],
            name="Modified after reloading"
        )

        self.assertEqual(
            current["name"],
            "Modified after reloading"
        )

        self.assert_session_roundtrip()

        self.assertEqual(
            self.find_transform(
                second["globalId"]
            )["name"],
            "Modified after reloading"
        )

    def test_geometry_roundtrip(self):
        """Verify persistence of physical parametric geometry."""

        addition = self.create_geometry(
            name="Persistent additive extrusion",
            operation="ADD",
            profile=rectangular_profile(3.0, 2.0),
            depth=4.0,
            matrix=translation_matrix(0.0, 0.0, 4.0)
        )

        subtraction = self.create_geometry(
            name="Persistent subtractive extrusion",
            operation="SUBTRACT",
            profile=rectangular_profile(1.0, 1.0),
            depth=7.0,
            matrix=translation_matrix(-0.5, -0.5, -1.0)
        )

        addition = self.update_geometry(
            addition["id"],
            name="Updated persistent addition",
            depth=6.0,
            profile=rectangular_profile(4.0, 3.0)
        )

        expected = {
            geometry["globalId"]: self.persistent_geometry(
                geometry
            )
            for geometry in (addition, subtraction)
        }

        initial_mesh = self.tessellate(
            self.root_id
        )

        _, model = self.assert_session_roundtrip()

        for global_id, reference in expected.items():
            with self.subTest(global_id=global_id):
                current = self.find_geometry(global_id)

                self.assert_persistent_equal(
                    self.persistent_geometry(current),
                    reference
                )

                native = find_ifc_entity(
                    model,
                    global_id
                )

                self.assertIsNotNone(native.ObjectPlacement)
                self.assertIsNotNone(native.Representation)

                representations = native.Representation.Representations

                solids = [
                    item
                    for representation in representations
                    for item in representation.Items
                    if item.is_a("IfcExtrudedAreaSolid")
                ]

                self.assertEqual(len(solids), 1)

                solid = solids[0]

                self.assertAlmostEqual(
                    solid.Depth,
                    reference["depth"],
                    delta=NUMERIC_TOLERANCE
                )

                self.assertTrue(
                    solid.SweptArea.is_a(
                        "IfcArbitraryClosedProfileDef"
                    )
                )

                points = [
                    list(point.Coordinates)
                    for point in solid.SweptArea.OuterCurve.Points
                ]

                self.assertEqual(points[0], points[-1])

                self.assert_profile(
                    points[:-1],
                    reference["profile"]
                )

                if reference["operation"] == "ADD":
                    self.assertTrue(
                        native.is_a("IfcProjectionElement")
                    )

                    relations = [
                        relation
                        for relation in model.by_type(
                            "IfcRelProjectsElement"
                        )
                        if relation.RelatedFeatureElement == native
                    ]

                    self.assertEqual(len(relations), 1)

                    self.assertEqual(
                        relations[0].RelatingElement.GlobalId,
                        ROOT_GLOBAL_ID
                    )

                else:
                    self.assertTrue(
                        native.is_a("IfcOpeningElement")
                    )

                    relations = [
                        relation
                        for relation in model.by_type(
                            "IfcRelVoidsElement"
                        )
                        if relation.RelatedOpeningElement == native
                    ]

                    self.assertEqual(len(relations), 1)

                    self.assertEqual(
                        relations[0].RelatingBuildingElement.GlobalId,
                        ROOT_GLOBAL_ID
                    )

        self.assert_mesh_equivalent(
            initial_mesh,
            self.tessellate(self.root_id),
            exact_triangles=False
        )

        current = self.find_geometry(
            addition["globalId"]
        )

        self.update_geometry(
            current["id"],
            depth=8.0
        )

        self.assert_session_roundtrip()

        self.assertAlmostEqual(
            self.find_geometry(
                addition["globalId"]
            )["depth"],
            8.0,
            delta=NUMERIC_TOLERANCE
        )

    def test_domain_roundtrip(self):
        """Verify persistence of managed domains and their IFC markers."""

        physical_before = self.tessellate(
            self.root_id
        )

        first = self.create_domain(
            name="Persistent rectangular domain",
            profile=rectangular_profile(5.0, 3.0),
            depth=2.0,
            matrix=translation_matrix(2.0, 3.0, 4.0)
        )

        second = self.create_domain(
            name="Persistent concave domain",
            profile=[
                [0, 0],
                [6, 0],
                [6, 2],
                [2, 2],
                [2, 5],
                [0, 5]
            ],
            depth=3.0,
            matrix=multiply_matrices(
                translation_matrix(20.0, 0.0, 0.0),
                rotation_z(35.0)
            )
        )

        domains = [first, second]

        expected = {
            domain["globalId"]: self.persistent_geometry(
                domain
            )
            for domain in domains
        }

        expected_meshes = {
            domain["globalId"]: self.tessellate_domain(
                domain,
                space="world"
            )
            for domain in domains
        }

        combined_before = self.tessellate(
            self.root_id,
            kind="DOMAIN"
        )

        _, model = self.assert_session_roundtrip()

        self.assertEqual(
            len(self.list_geometries(
                self.root_id,
                kind="DOMAIN"
            )),
            2
        )

        for domain, expected_volume in zip(
            domains,
            (30.0, 54.0)
        ):
            global_id = domain["globalId"]

            with self.subTest(global_id=global_id):
                current = self.find_geometry(global_id)

                self.assert_persistent_equal(
                    self.persistent_geometry(current),
                    expected[global_id]
                )

                self.assertEqual(current["kind"], "DOMAIN")
                self.assertEqual(current["operation"], "ADD")
                self.assertEqual(current["materials"], [])
                self.assertIs(current["editable"], True)

                native = find_ifc_entity(
                    model,
                    global_id
                )

                self.assertTrue(
                    native.is_a("IfcSpatialZone")
                )

                self.assertEqual(
                    native.ObjectType,
                    "ifc-cli:domain"
                )

                self.assertEqual(
                    native.PredefinedType,
                    "USERDEFINED"
                )

                parent = managed_domain_parent(
                    model,
                    native
                )

                self.assertEqual(
                    parent.GlobalId,
                    ROOT_GLOBAL_ID
                )

                self.assertEqual(
                    native.ObjectPlacement.PlacementRelTo,
                    parent.ObjectPlacement
                )

                self.assertNotIn(
                    global_id,
                    {
                        transform["globalId"]
                        for transform in self.list_transforms()
                    }
                )

                mesh = self.tessellate_domain(
                    current,
                    space="world"
                )

                self.assert_mesh_equivalent(
                    expected_meshes[global_id],
                    mesh,
                    exact_triangles=False
                )

                self.assertAlmostEqual(
                    mesh_volume(mesh),
                    expected_volume,
                    delta=MESH_TOLERANCE
                )

                self.assertEqual(mesh["groups"], [])

        self.assert_mesh_equivalent(
            combined_before,
            self.tessellate(
                self.root_id,
                kind="DOMAIN"
            ),
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            physical_before,
            self.tessellate(self.root_id)
        )

        current = self.find_geometry(
            first["globalId"]
        )

        self.update_geometry(
            current["id"],
            depth=6.0
        )

        self.assert_session_roundtrip()

        current = self.find_geometry(
            first["globalId"]
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(current)
            ),
            90.0,
            delta=MESH_TOLERANCE
        )

    def test_material_roundtrip(self):
        """Verify persistence of materials, properties and assignments."""

        concrete = self.create_material(
            name="Persistent concrete",
            category="concrete",
            visual={
                "color": [0.6, 0.6, 0.6],
                "opacity": 1.0,
                "metallic": 0.0,
                "roughness": 0.9
            },
            properties=[
                property_input(
                    "Density",
                    2500.0,
                    value_type="IfcReal",
                    property_set="Pset_Physical",
                    source="material"
                )
            ]
        )

        steel = self.create_material(
            name="Persistent steel",
            category="metal",
            visual={
                "color": [0.7, 0.8, 0.9],
                "opacity": 0.95,
                "metallic": 1.0,
                "roughness": 0.2
            }
        )

        geometry = self.create_geometry(
            name="Persistent material geometry"
        )

        native_geometry = [
            item
            for item in self.list_geometries(self.root_id)
            if item["source"] == "ifc"
        ][0]

        self.assign_material(concrete["id"], self.root_id)
        self.assign_material(concrete["id"], self.survivor_id)
        self.assign_material(steel["id"], geometry["id"])
        self.assign_material(steel["id"], native_geometry["id"])

        expected_materials = {
            material["name"]: self.persistent_material(material)
            for material in (concrete, steel)
        }

        _, model = self.assert_session_roundtrip()

        for name, expected in expected_materials.items():
            with self.subTest(material=name):
                current = self.find_material(name)

                self.assert_persistent_equal(
                    self.persistent_material(current),
                    expected
                )

                native = material_by_name(
                    model,
                    name
                )

                self.assertEqual(
                    native.Category,
                    expected["category"]
                )

                containers = [
                    container
                    for container in model.by_type(
                        "IfcMaterialProperties"
                    )
                    if container.Material == native
                ]

                container_names = {
                    container.Name
                    for container in containers
                }

                self.assertIn(
                    "Pset_IfcEditorVisual",
                    container_names
                )

                self.assertIn(
                    "Pset_IfcEditorMaterial",
                    container_names
                )

                representations = [
                    representation
                    for representation in model.by_type(
                        "IfcMaterialDefinitionRepresentation"
                    )
                    if representation.RepresentedMaterial == native
                ]

                self.assertEqual(len(representations), 1)

                styles = [
                    entity
                    for entity in entity_closure(
                        representations
                    ).values()
                    if entity.is_a("IfcSurfaceStyleRendering")
                ]

                self.assertEqual(len(styles), 1)

                rendering = styles[0]
                color = rendering.SurfaceColour

                self.assert_visual(
                    {
                        "color": [
                            color.Red,
                            color.Green,
                            color.Blue
                        ],
                        "opacity": 1.0 - rendering.Transparency,
                        "metallic": rendering.SpecularColour.wrappedValue,
                        "roughness": rendering.SpecularHighlight.wrappedValue
                    },
                    expected["visual"]
                )

        current_concrete = self.find_material(
            concrete["name"]
        )

        current_steel = self.find_material(
            steel["name"]
        )

        current_geometry = self.find_geometry(
            geometry["globalId"]
        )

        self.assertIn(
            current_concrete["id"],
            self.get_transform(self.root_id)["materials"]
        )

        self.assertIn(
            current_concrete["id"],
            self.get_transform(self.survivor_id)["materials"]
        )

        self.assertEqual(
            current_geometry["materials"],
            [current_steel["id"]]
        )

        native_concrete = material_by_name(
            model,
            concrete["name"]
        )

        native_steel = material_by_name(
            model,
            steel["name"]
        )

        self.assertEqual(
            semantic_material_targets(
                model,
                native_concrete
            ),
            {
                ROOT_GLOBAL_ID,
                SURVIVOR_GLOBAL_ID
            }
        )

        self.assertEqual(
            semantic_material_targets(
                model,
                native_steel
            ),
            {geometry["globalId"]}
        )

        native_wall = find_ifc_entity(
            model,
            ROOT_GLOBAL_ID
        )

        representation_items = {
            item
            for representation in native_wall.Representation.Representations
            for item in representation.Items
        }

        assigned_styles = [
            styled_item
            for styled_item in model.by_type("IfcStyledItem")
            if styled_item.Item in representation_items
        ]

        self.assertTrue(assigned_styles)

        steel_representations = [
            representation
            for representation in model.by_type(
                "IfcMaterialDefinitionRepresentation"
            )
            if representation.RepresentedMaterial == native_steel
        ]

        steel_styles = {
            entity.id()
            for entity in entity_closure(
                steel_representations
            ).values()
            if entity.is_a("IfcSurfaceStyle")
        }

        native_styles = {
            entity.id()
            for styled_item in assigned_styles
            for entity in entity_closure(
                [styled_item]
            ).values()
            if entity.is_a("IfcSurfaceStyle")
        }

        self.assertTrue(
            steel_styles & native_styles
        )

        updated_visual = {
            "color": [0.2, 0.4, 0.6],
            "opacity": 0.75,
            "metallic": 0.8,
            "roughness": 0.3
        }

        self.client.request(
            "materialUpdate",
            {
                "id": current_steel["id"],
                "visual": updated_visual
            }
        )

        self.assert_session_roundtrip()

        self.assert_visual(
            self.find_material(steel["name"])["visual"],
            updated_visual
        )

    def test_property_roundtrip(self):
        """Verify persistence of editable and native property sets."""

        properties = [
            property_input(
                "Reference",
                "PERSISTENCE-001"
            ),
            property_input(
                "Description",
                "Persistent property description",
                value_type="IfcText"
            ),
            property_input(
                "Identifier",
                "IDENTIFIER-001",
                value_type="IfcIdentifier"
            ),
            property_input(
                "Count",
                42,
                value_type="IfcInteger"
            ),
            property_input(
                "Density",
                7850.125,
                value_type="IfcReal",
                property_set="Pset_Physical"
            ),
            property_input(
                "Enabled",
                True,
                value_type="IfcBoolean"
            ),
            property_input(
                "OptionalValue",
                None,
                value_type="IfcReal"
            ),
            property_input(
                "Length",
                12.3456789,
                value_type="IfcLengthMeasure",
                property_set="Pset_Dimensions",
                unit="METRE"
            ),
            property_input(
                "Area",
                25.5,
                value_type="IfcAreaMeasure",
                property_set="Pset_Dimensions",
                unit="SQUARE_METRE"
            ),
            property_input(
                "Volume",
                60.75,
                value_type="IfcVolumeMeasure",
                property_set="Pset_Dimensions",
                unit="CUBIC_METRE"
            ),
            property_input(
                "UnicodeText",
                "Matériau — Werkstoff — 材料",
                value_type="IfcText"
            )
        ]

        created = self.create_transform(
            name="Persistent property owner",
            properties=properties
        )

        root = self.get_transform(self.root_id)

        native_properties = [
            item
            for item in root["properties"]
            if item["source"] == "occurrence"
        ]

        self.update_transform(
            self.root_id,
            properties=[
                *native_properties,
                property_input(
                    "AdditionalReference",
                    "NATIVE-EXTENSION",
                    property_set="Pset_Additional"
                )
            ]
        )

        _, model = self.assert_session_roundtrip()

        current = self.find_transform(
            created["globalId"]
        )

        self.assert_properties(
            current["properties"],
            properties
        )

        native = find_ifc_entity(
            model,
            created["globalId"]
        )

        property_sets = occurrence_property_sets(
            model,
            native
        )

        self.assertEqual(
            {property_set.Name for property_set in property_sets},
            {
                "Pset_Persistence",
                "Pset_Physical",
                "Pset_Dimensions"
            }
        )

        native_properties_by_key = {
            (property_set.Name, value.Name): value
            for property_set in property_sets
            for value in property_set.HasProperties
        }

        for expected in properties:
            key = (
                expected["propertySet"],
                expected["name"]
            )

            with self.subTest(property=key):
                value = native_properties_by_key[key]

                self.assertTrue(
                    value.is_a("IfcPropertySingleValue")
                )

                if expected["value"] is None:
                    self.assertIsNone(value.NominalValue)
                else:
                    self.assertEqual(
                        value.NominalValue.is_a(),
                        expected["valueType"]
                    )

                    self.assert_persistent_equal(
                        value.NominalValue.wrappedValue,
                        expected["value"]
                    )

                if expected["unit"] is None:
                    self.assertIsNone(value.Unit)
                else:
                    self.assertEqual(
                        value.Unit.Name,
                        expected["unit"]
                    )

        original_wall = find_ifc_entity(
            self.original_model,
            ROOT_GLOBAL_ID
        )

        saved_wall = find_ifc_entity(
            model,
            ROOT_GLOBAL_ID
        )

        original_set = occurrence_property_set(
            self.original_model,
            original_wall,
            "Pset_NativeWall"
        )

        saved_set = occurrence_property_set(
            model,
            saved_wall,
            "Pset_NativeWall"
        )

        original_complex = [
            value
            for value in original_set.HasProperties
            if not value.is_a("IfcPropertySingleValue")
        ]

        saved_complex = [
            value
            for value in saved_set.HasProperties
            if not value.is_a("IfcPropertySingleValue")
        ]

        self.assertEqual(
            len(original_complex),
            2
        )

        self.assert_native_subgraphs_equal(
            self.original_model,
            original_complex,
            model,
            saved_complex
        )

        original_quantities = self.original_model.by_type(
            "IfcElementQuantity"
        )

        saved_quantities = model.by_type(
            "IfcElementQuantity"
        )

        self.assert_native_subgraphs_equal(
            self.original_model,
            original_quantities,
            model,
            saved_quantities
        )

        original_shared = occurrence_property_set(
            self.original_model,
            original_wall,
            "Pset_Shared"
        )

        saved_shared = occurrence_property_set(
            model,
            saved_wall,
            "Pset_Shared"
        )

        self.assert_native_subgraphs_equal(
            self.original_model,
            [original_shared],
            model,
            [saved_shared]
        )

        replacement = [
            property_input(
                "Reference",
                "PERSISTENCE-002"
            ),
            property_input(
                "Count",
                100,
                value_type="IfcInteger"
            )
        ]

        self.update_transform(
            current["id"],
            properties=replacement
        )

        self.assert_session_roundtrip()

        self.assert_properties(
            self.find_transform(
                created["globalId"]
            )["properties"],
            replacement
        )

    def test_hierarchy_roundtrip(self):
        """Verify persistence of spatial, placement and decomposition relations."""

        anchor = self.create_transform(
            name="Independent placement parent",
            matrix=translation_matrix(10.0, 20.0, 30.0)
        )

        decomposition = self.create_transform(
            name="Independent decomposition parent",
            matrix=translation_matrix(100.0, 200.0, 300.0)
        )

        child = self.create_transform(
            name="Independent hierarchy child",
            parents=parents_input(
                spatial=self.storey_id,
                placement=anchor["id"],
                decomposition=decomposition["id"]
            ),
            matrix=translation_matrix(2.0, 3.0, 4.0)
        )

        grandchild = self.create_transform(
            name="Persistent hierarchy grandchild",
            parents=parents_input(
                spatial=self.storey_id,
                placement=child["id"],
                decomposition=child["id"]
            ),
            matrix=translation_matrix(5.0, 6.0, 7.0)
        )

        domain = self.create_domain(
            parent_id=grandchild["id"],
            name="Persistent hierarchy domain",
            depth=3.0
        )

        hierarchy = [
            anchor,
            decomposition,
            child,
            grandchild
        ]

        expected = {
            item["globalId"]: self.persistent_transform(
                self.get_transform(item["id"])
            )
            for item in hierarchy
        }

        self.assertEqual(
            expected[grandchild["globalId"]]["geometryCount"],
            1
        )

        self.assertEqual(
            expected[grandchild["globalId"]]["geometryKinds"],
            Counter({"DOMAIN": 1})
        )

        domain_before = self.tessellate_domain(
            domain,
            space="world"
        )

        _, model = self.assert_session_roundtrip()

        for global_id, reference in expected.items():
            current = self.find_transform(global_id)

            self.assert_persistent_equal(
                self.persistent_transform(current),
                reference
            )

        current_child = self.find_transform(
            child["globalId"]
        )

        current_grandchild = self.find_transform(
            grandchild["globalId"]
        )

        self.assertEqual(
            self.persistent_transform(
                current_child
            )["parents"],
            {
                "spatial": STOREY_GLOBAL_ID,
                "placement": anchor["globalId"],
                "decomposition": decomposition["globalId"]
            }
        )

        self.assert_matrix(
            current_child["transforms"]["world"],
            translation_matrix(12.0, 23.0, 34.0)
        )

        self.assert_matrix(
            current_grandchild["transforms"]["world"],
            translation_matrix(17.0, 29.0, 41.0)
        )

        native_child = find_ifc_entity(
            model,
            child["globalId"]
        )

        native_anchor = find_ifc_entity(
            model,
            anchor["globalId"]
        )

        native_decomposition = find_ifc_entity(
            model,
            decomposition["globalId"]
        )

        self.assertEqual(
            native_child.ObjectPlacement.PlacementRelTo,
            native_anchor.ObjectPlacement
        )

        containment = [
            relation
            for relation in model.by_type(
                "IfcRelContainedInSpatialStructure"
            )
            if native_child in relation.RelatedElements
        ]

        self.assertEqual(len(containment), 1)

        self.assertEqual(
            containment[0].RelatingStructure.GlobalId,
            STOREY_GLOBAL_ID
        )

        decomposition_relations = [
            relation
            for relation in model.by_type("IfcRelAggregates")
            if native_child in relation.RelatedObjects
        ]

        self.assertEqual(
            len(decomposition_relations),
            1
        )

        self.assertEqual(
            decomposition_relations[0].RelatingObject,
            native_decomposition
        )

        current_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assert_mesh_equivalent(
            domain_before,
            self.tessellate_domain(
                current_domain,
                space="world"
            )
        )

        current_anchor = self.find_transform(
            anchor["globalId"]
        )

        self.update_transform(
            current_anchor["id"],
            transform=transform_input(
                translation_matrix(20.0, 30.0, 40.0)
            )
        )

        self.assert_session_roundtrip()

        current_grandchild = self.find_transform(
            grandchild["globalId"]
        )

        self.assert_matrix(
            current_grandchild["transforms"]["world"],
            translation_matrix(27.0, 39.0, 51.0)
        )

    def test_native_attributes(self):
        """Verify preservation of native attributes outside the simplified API."""

        native_wall = find_ifc_entity(
            self.original_model,
            ROOT_GLOBAL_ID
        )

        native_survivor = find_ifc_entity(
            self.original_model,
            SURVIVOR_GLOBAL_ID
        )

        preserved_fields = (
            "Description",
            "ObjectType",
            "Tag",
            "PredefinedType"
        )

        expected_wall = {
            field: getattr(native_wall, field)
            for field in preserved_fields
        }

        expected_survivor = {
            field: getattr(native_survivor, field)
            for field in preserved_fields
        }

        self.update_transform(
            self.root_id,
            name="Renamed native wall",
            transform=transform_input(
                translation_matrix(10.0, 20.0, 30.0)
            )
        )

        self.create_domain(
            parent_id=self.root_id,
            name="Native attribute preservation domain"
        )

        _, model = self.assert_session_roundtrip()

        saved_wall = find_ifc_entity(
            model,
            ROOT_GLOBAL_ID
        )

        saved_survivor = find_ifc_entity(
            model,
            SURVIVOR_GLOBAL_ID
        )

        self.assertEqual(
            saved_wall.Name,
            "Renamed native wall"
        )

        self.assertTrue(saved_wall.is_a("IfcWall"))

        for field in preserved_fields:
            with self.subTest(attribute=field):
                self.assertEqual(
                    getattr(saved_wall, field),
                    expected_wall[field]
                )

                self.assertEqual(
                    getattr(saved_survivor, field),
                    expected_survivor[field]
                )

        for global_id in (
            PROJECT_GLOBAL_ID,
            BUILDING_GLOBAL_ID,
            STOREY_GLOBAL_ID,
            WALL_TYPE_GLOBAL_ID,
            GROUP_GLOBAL_ID
        ):
            original = find_ifc_entity(
                self.original_model,
                global_id
            )

            saved = find_ifc_entity(
                model,
                global_id
            )

            self.assertEqual(
                saved.is_a(),
                original.is_a()
            )

            for index, original_value in enumerate(original):
                if isinstance(
                    original_value,
                    ifcopenshell.entity_instance
                ):
                    continue

                if isinstance(original_value, (tuple, list)):
                    continue

                self.assertEqual(
                    saved[index],
                    original_value,
                    f"Native attribute changed: "
                    f"{original.is_a()}.{original.attribute_name(index)}"
                )

        original_material = material_by_name(
            self.original_model,
            "Native concrete"
        )

        saved_material = material_by_name(
            model,
            "Native concrete"
        )

        self.assertEqual(
            saved_material.Description,
            original_material.Description
        )

        self.assertEqual(
            saved_material.Category,
            original_material.Category
        )

    def test_native_relationships(self):
        """Verify preservation of unmodified native IFC relationships."""

        original_relationships = {
            relation.GlobalId: relation
            for relation in self.original_model.by_type(
                "IfcRelationship"
            )
        }

        self.assertTrue(original_relationships)

        # Modify an independent object so that the native
        # relationships and their complete dependency graphs
        # are not expected to change.

        created = self.create_transform(
            name="Independent relationship test object"
        )

        self.create_domain(
            parent_id=created["id"],
            name="Independent relationship test domain"
        )

        self.create_material(
            name="Independent relationship test material"
        )

        _, model = self.assert_session_roundtrip()

        saved_relationships = {
            relation.GlobalId: relation
            for relation in model.by_type(
                "IfcRelationship"
            )
        }

        self.assertTrue(
            set(original_relationships).issubset(
                saved_relationships
            )
        )

        for global_id, original in original_relationships.items():
            with self.subTest(
                relation=global_id,
                ifc_class=original.is_a()
            ):
                saved = saved_relationships[global_id]

                self.assertEqual(
                    saved.is_a(),
                    original.is_a()
                )

                self.assert_native_subgraphs_equal(
                    self.original_model,
                    [original],
                    model,
                    [saved]
                )

        original_type_relation = find_ifc_entity(
            self.original_model,
            "0000000000000000000018"
        )

        saved_type_relation = find_ifc_entity(
            model,
            original_type_relation.GlobalId
        )

        self.assertEqual(
            saved_type_relation.RelatingType.GlobalId,
            WALL_TYPE_GLOBAL_ID
        )

        self.assertEqual(
            {
                entity.GlobalId
                for entity in saved_type_relation.RelatedObjects
            },
            {ROOT_GLOBAL_ID}
        )

        group_relation = find_ifc_entity(
            model,
            "0000000000000000000019"
        )

        self.assertEqual(
            group_relation.RelatingGroup.GlobalId,
            GROUP_GLOBAL_ID
        )

        self.assertEqual(
            {
                entity.GlobalId
                for entity in group_relation.RelatedObjects
            },
            {
                ROOT_GLOBAL_ID,
                SURVIVOR_GLOBAL_ID
            }
        )

        material_relation = find_ifc_entity(
            model,
            "0000000000000000000017"
        )

        self.assertEqual(
            material_relation.RelatingMaterial.Name,
            "Native concrete"
        )

        self.assertEqual(
            {
                entity.GlobalId
                for entity in material_relation.RelatedObjects
            },
            {
                ROOT_GLOBAL_ID,
                SURVIVOR_GLOBAL_ID
            }
        )

    def test_native_representations(self):
        """Verify preservation of untouched native geometry representations."""

        original_wall = find_ifc_entity(
            self.original_model,
            ROOT_GLOBAL_ID
        )

        original_survivor = find_ifc_entity(
            self.original_model,
            SURVIVOR_GLOBAL_ID
        )

        root_before = self.tessellate(
            self.root_id,
            space="local"
        )

        survivor_before = self.tessellate(
            self.survivor_id,
            space="world"
        )

        self.assertAlmostEqual(
            mesh_volume(root_before),
            60.0,
            delta=MESH_TOLERANCE
        )

        self.update_transform(
            self.root_id,
            name="Native representation preservation wall",
            transform=transform_input(
                multiply_matrices(
                    translation_matrix(10.0, 20.0, 30.0),
                    rotation_z(45.0)
                )
            )
        )

        self.create_domain(
            parent_id=self.root_id,
            name="Independent representation domain",
            profile=rectangular_profile(10.0, 10.0),
            depth=8.0
        )

        _, model = self.assert_session_roundtrip()

        saved_wall = find_ifc_entity(
            model,
            ROOT_GLOBAL_ID
        )

        saved_survivor = find_ifc_entity(
            model,
            SURVIVOR_GLOBAL_ID
        )

        self.assert_native_subgraphs_equal(
            self.original_model,
            [
                original_wall.Representation,
                original_survivor.Representation
            ],
            model,
            [
                saved_wall.Representation,
                saved_survivor.Representation
            ]
        )

        wall_representation = (
            saved_wall.Representation.Representations[0]
        )

        survivor_representation = (
            saved_survivor.Representation.Representations[0]
        )

        wall_solid = wall_representation.Items[0]
        survivor_solid = survivor_representation.Items[0]

        self.assertTrue(
            wall_solid.is_a("IfcExtrudedAreaSolid")
        )

        self.assertTrue(
            survivor_solid.is_a("IfcExtrudedAreaSolid")
        )

        self.assertNotEqual(
            wall_solid,
            survivor_solid
        )

        # The original two native solids share the same profile.
        # Persistence must preserve this sharing relationship.

        self.assertEqual(
            wall_solid.SweptArea,
            survivor_solid.SweptArea
        )

        self.assertEqual(
            wall_solid.SweptArea.ProfileName,
            "Native rectangle"
        )

        self.assertAlmostEqual(
            wall_solid.SweptArea.XDim,
            4.0,
            delta=NUMERIC_TOLERANCE
        )

        self.assertAlmostEqual(
            wall_solid.SweptArea.YDim,
            3.0,
            delta=NUMERIC_TOLERANCE
        )

        self.assertAlmostEqual(
            wall_solid.Depth,
            5.0,
            delta=NUMERIC_TOLERANCE
        )

        self.assertEqual(
            wall_representation.RepresentationIdentifier,
            "Body"
        )

        self.assertEqual(
            wall_representation.RepresentationType,
            "SweptSolid"
        )

        self.assert_mesh_equivalent(
            root_before,
            self.tessellate(
                self.root_id,
                space="local"
            ),
            exact_triangles=False
        )

        self.assert_mesh_equivalent(
            survivor_before,
            self.tessellate(
                self.survivor_id,
                space="world"
            )
        )

    def test_repeated_roundtrip(self):
        """Verify absence of semantic degradation over repeated roundtrips."""

        parent = self.create_transform(
            name="Repeated roundtrip parent",
            parents=parents_input(
                spatial=self.storey_id,
                placement=self.root_id,
                decomposition=self.root_id
            ),
            matrix=translation_matrix(10.0, 20.0, 30.0),
            properties=[
                property_input(
                    "Reference",
                    "ROUNDTRIP-001"
                ),
                property_input(
                    "Precision",
                    0.123456789,
                    value_type="IfcReal"
                )
            ]
        )

        child = self.create_transform(
            name="Repeated roundtrip child",
            parents=parents_input(
                placement=parent["id"],
                decomposition=parent["id"]
            ),
            matrix=multiply_matrices(
                translation_matrix(5.0, 6.0, 7.0),
                rotation_z(37.0)
            )
        )

        self.create_geometry(
            parent_id=self.root_id,
            name="Repeated roundtrip subtraction",
            operation="SUBTRACT",
            profile=rectangular_profile(1.0, 1.0),
            depth=7.0,
            matrix=translation_matrix(-0.5, -0.5, -1.0)
        )

        domain = self.create_domain(
            parent_id=child["id"],
            name="Repeated roundtrip domain",
            profile=[
                [0, 0],
                [6, 0],
                [6, 2],
                [2, 2],
                [2, 5],
                [0, 5]
            ],
            depth=3.25
        )

        material = self.create_material(
            name="Repeated roundtrip material",
            category="metal",
            visual={
                "color": [
                    0.123456789,
                    0.987654321,
                    0.456789123
                ],
                "opacity": 0.765432198,
                "metallic": 0.234567891,
                "roughness": 0.345678912
            },
            properties=[
                property_input(
                    "Density",
                    7850.123456,
                    value_type="IfcReal",
                    source="material"
                )
            ]
        )

        self.assign_material(
            material["id"],
            self.root_id
        )

        self.assign_material(
            material["id"],
            child["id"]
        )

        initial_snapshot = self.persistent_snapshot()

        physical_before = self.tessellate(
            self.root_id,
            space="world",
            include_children=True
        )

        domain_before = self.tessellate_domain(
            domain,
            space="world"
        )

        path, reference_model = self.save_and_parse()

        reference_graph = semantic_graph(
            reference_model
        )

        reference_class_counts = ifc_class_counts(
            reference_model
        )

        reference_global_ids = set(
            ifc_global_ids(reference_model)
        )

        reference_entity_count = sum(
            1 for _ in reference_model
        )

        for iteration in range(ROUNDTRIP_COUNT):
            with self.subTest(roundtrip=iteration + 1):
                self.load_model(path)

                self.assert_persistent_equal(
                    self.persistent_snapshot(),
                    initial_snapshot
                )

                path, current_model = self.save_and_parse()

                self.assertEqual(
                    ifc_class_counts(current_model),
                    reference_class_counts
                )

                self.assertEqual(
                    set(ifc_global_ids(current_model)),
                    reference_global_ids
                )

                self.assertEqual(
                    sum(1 for _ in current_model),
                    reference_entity_count,
                    "Entity count changed during roundtrip"
                )

                self.assertTrue(
                    semantic_graph_equal(
                        reference_graph,
                        semantic_graph(current_model)
                    ),
                    f"Semantic degradation after roundtrip "
                    f"{iteration + 1}"
                )

                self.assert_mesh_equivalent(
                    physical_before,
                    self.tessellate(
                        self.root_id,
                        space="world",
                        include_children=True
                    ),
                    exact_triangles=False
                )

                current_domain = self.find_geometry(
                    domain["globalId"]
                )

                self.assert_mesh_equivalent(
                    domain_before,
                    self.tessellate_domain(
                        current_domain,
                        space="world"
                    ),
                    exact_triangles=False
                )

                current_material = self.find_material(
                    material["name"]
                )

                self.assert_visual(
                    current_material["visual"],
                    material["visual"]
                )

                current_child = self.find_transform(
                    child["globalId"]
                )

                self.assertIn(
                    current_material["id"],
                    current_child["materials"]
                )

        # Verify that the model remains editable after all roundtrips.

        current_domain = self.find_geometry(
            domain["globalId"]
        )

        self.update_geometry(
            current_domain["id"],
            depth=6.5
        )

        self.assert_session_roundtrip()

        current_domain = self.find_geometry(
            domain["globalId"]
        )

        self.assertAlmostEqual(
            current_domain["depth"],
            6.5,
            delta=NUMERIC_TOLERANCE
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(current_domain)
            ),
            mesh_volume(domain_before) * 2.0,
            delta=MESH_TOLERANCE
        )

    def test_independent_ifc_validation(self):
        """Verify readability and integrity using the Python IFC parser."""

        created = self.create_transform(
            name="Independent parser transform",
            parents=parents_input(
                spatial=self.storey_id,
                placement=self.root_id,
                decomposition=self.root_id
            ),
            matrix=translation_matrix(10.0, 20.0, 30.0),
            properties=[
                property_input(
                    "Reference",
                    "INDEPENDENT-PARSER"
                )
            ]
        )

        addition = self.create_geometry(
            parent_id=self.root_id,
            name="Independent parser addition",
            depth=3.0
        )

        subtraction = self.create_geometry(
            parent_id=self.root_id,
            name="Independent parser subtraction",
            operation="SUBTRACT",
            depth=7.0,
            profile=rectangular_profile(1.0, 1.0),
            matrix=translation_matrix(-0.5, -0.5, -1.0)
        )

        domain = self.create_domain(
            parent_id=created["id"],
            name="Independent parser domain",
            profile=rectangular_profile(5.0, 3.0),
            depth=2.0
        )

        material = self.create_material(
            name="Independent parser material",
            category="concrete"
        )

        self.assign_material(
            material["id"],
            created["id"]
        )

        self.assign_material(
            material["id"],
            addition["id"]
        )

        path = self.save_model()

        # Close the CLI before parsing to demonstrate that
        # the output does not depend on its in-memory state.

        self.client.close()

        parser_script = """
import json
import sys
from pathlib import Path

import ifcopenshell

path = Path(sys.argv[1])
model = ifcopenshell.open(str(path))

entities = list(model)

result = {
    "schema": model.schema,
    "entityCount": len(entities),
    "projectCount": len(model.by_type("IfcProject")),
    "globalIds": [
        entity.GlobalId
        for entity in model.by_type("IfcRoot")
    ],
    "managedDomains": [
        entity.GlobalId
        for entity in model.by_type("IfcSpatialZone")
        if entity.ObjectType == "ifc-cli:domain"
    ],
    "materials": [
        entity.Name
        for entity in model.by_type("IfcMaterial")
    ]
}

print(json.dumps(result))
"""

        process = subprocess.run(
            [
                sys.executable,
                "-c",
                parser_script,
                str(path)
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=REQUEST_TIMEOUT,
            check=False
        )

        self.assertEqual(
            process.returncode,
            0,
            process.stderr
        )

        parsed = json.loads(process.stdout)

        self.assertEqual(
            parsed["schema"].upper(),
            "IFC4"
        )

        self.assertGreater(
            parsed["entityCount"],
            0
        )

        self.assertEqual(
            parsed["projectCount"],
            1
        )

        self.assertEqual(
            len(parsed["globalIds"]),
            len(set(parsed["globalIds"]))
        )

        for global_id in (
            ROOT_GLOBAL_ID,
            SURVIVOR_GLOBAL_ID,
            PROJECT_GLOBAL_ID,
            STOREY_GLOBAL_ID,
            created["globalId"],
            addition["globalId"],
            subtraction["globalId"],
            domain["globalId"]
        ):
            self.assertIn(
                global_id,
                parsed["globalIds"]
            )

        self.assertIn(
            domain["globalId"],
            parsed["managedDomains"]
        )

        self.assertIn(
            material["name"],
            parsed["materials"]
        )

        model = ifcopenshell.open(str(path))

        self.assert_ifc_integrity(model)

        native_created = find_ifc_entity(
            model,
            created["globalId"]
        )

        self.assertTrue(
            native_created.is_a("IfcBuildingElementProxy")
        )

        native_addition = find_ifc_entity(
            model,
            addition["globalId"]
        )

        self.assertTrue(
            native_addition.is_a("IfcProjectionElement")
        )

        native_subtraction = find_ifc_entity(
            model,
            subtraction["globalId"]
        )

        self.assertTrue(
            native_subtraction.is_a("IfcOpeningElement")
        )

        native_domain = find_ifc_entity(
            model,
            domain["globalId"]
        )

        self.assertTrue(
            native_domain.is_a("IfcSpatialZone")
        )

        self.assertEqual(
            managed_domain_parent(
                model,
                native_domain
            ),
            native_created
        )

        native_material = material_by_name(
            model,
            material["name"]
        )

        self.assertEqual(
            semantic_material_targets(
                model,
                native_material
            ),
            {
                created["globalId"],
                addition["globalId"]
            }
        )

        # Rewrite the model using the external Python API,
        # then verify that the CLI can load it again.

        external_path = (
            self.root_path / "externally-written.ifc"
        )

        model.write(str(external_path))

        external_model = ifcopenshell.open(
            str(external_path)
        )

        self.assert_semantic_models_equal(
            model,
            external_model
        )

        self.client = Client(EXECUTABLE)
        self.addCleanup(self.client.terminate)

        self.load_model(external_path)

        current_transform = self.find_transform(
            created["globalId"]
        )

        current_domain = self.find_geometry(
            domain["globalId"]
        )

        current_material = self.find_material(
            material["name"]
        )

        self.assertEqual(
            current_domain["parentId"],
            current_transform["id"]
        )

        self.assertIn(
            current_material["id"],
            current_transform["materials"]
        )

        self.assertAlmostEqual(
            mesh_volume(
                self.tessellate_domain(current_domain)
            ),
            30.0,
            delta=MESH_TOLERANCE
        )

        _, final_model = self.save_and_parse()

        self.assert_semantic_models_equal(
            external_model,
            final_model
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