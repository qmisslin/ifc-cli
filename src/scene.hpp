#pragma once

#include "transform.hpp"
#include "geometry.hpp"
#include "material.hpp"

#include <ifcgeom/ConversionSettings.h>
#include <ifcgeom/Iterator.h>
#include <ifcgeom/kernels/opencascade/OpenCascadeKernel.h>
#include <ifcparse/IfcFile.h>
#include <ifcparse/IfcSchema.h>

#include <algorithm>
#include <cstddef>
#include <filesystem>
#include <fstream>
#include <functional>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <system_error>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace ifc_editor {

class IfcScene {
public:
    using MeshEmitter = std::function<void(const json&)>;

    IfcScene() = default;
    IfcScene(const IfcScene&) = delete;
    IfcScene& operator=(const IfcScene&) = delete;

    json getCapabilities() const {
        std::vector<std::string> schemas = IfcParse::schema_names();
        std::sort(schemas.begin(), schemas.end());
        return json{
            {"protocolVersion", "1.0"},
            {"ifcOpenShellVersion", ifcOpenShellVersion()},
            {"supportedSchemas", schemas},
            {"supportedProfiles", json::array({"polygon"})},
            {"supportedGeometryOperations", json::array({"ADD", "SUBTRACT"})}};
    }

    json load(const std::string& path) {
        clear();
        std::error_code fileError;
        const bool exists = std::filesystem::exists(path, fileError);
        const bool regular = exists && std::filesystem::is_regular_file(path, fileError);
        if (path.empty() || fileError || !regular) {
            throw SceneError("FILE_NOT_FOUND", "IFC file does not exist or is not accessible", json{{"path", path}});
        }

        try {
            std::unique_ptr<IfcParse::IfcFile> candidate = std::make_unique<IfcParse::IfcFile>(path);
            const auto status = candidate->good().value();
            if (status == IfcParse::file_open_status::UNSUPPORTED_SCHEMA) {
                throw SceneError("UNSUPPORTED_SCHEMA", "IFC schema is not supported", json{{"path", path}});
            }
            if (status != IfcParse::file_open_status::SUCCESS) {
                throw SceneError("IFC_PARSE_ERROR", "IfcOpenShell could not load the IFC file", json{{"path", path}});
            }

            file_ = std::move(candidate);
            loadedPath_ = path;
            schema_ = file_->schema()->name();
            buildIndexes();
            return json{
                {"path", path},
                {"schema", schema_},
                {"transformCount", transforms_.size()},
                {"geometryCount", geometries_.size()},
                {"materialCount", materials_.size()}};
        } catch (const SceneError&) {
            clear();
            throw;
        } catch (const std::exception& exception) {
            clear();
            throw SceneError("IFC_PARSE_ERROR", "IfcOpenShell could not load the IFC file", json{{"path", path}, {"reason", exception.what()}});
        }
    }

    json save(const std::string& path) {
        requireLoaded();
        if (path.empty()) {
            throw SceneError("INVALID_PARAMETERS", "Save path must be a non-empty string");
        }
        try {
            std::ofstream output(path, std::ios::binary | std::ios::trunc);
            if (!output.is_open()) {
                throw SceneError("IFC_WRITE_ERROR", "IFC output file cannot be opened", json{{"path", path}});
            }
            output << *file_;
            output.flush();
            if (!output.good()) {
                throw SceneError("IFC_WRITE_ERROR", "IfcOpenShell could not write the IFC file", json{{"path", path}});
            }
            return json{{"path", path}};
        } catch (const SceneError&) {
            throw;
        } catch (const std::exception& exception) {
            throw SceneError("IFC_WRITE_ERROR", "IfcOpenShell could not write the IFC file", json{{"path", path}, {"reason", exception.what()}});
        }
    }

    void close() {
        clear();
    }

    json transformGetAll() {
        requireLoaded();
        json result = json::array();
        for (const std::string& id : sortedIds(transforms_)) {
            const Transform& transform = *transforms_.at(id);
            const auto global = transform.globalId();
            result.push_back(json{
                {"id", id},
                {"ifcEntityId", transform.entity()->id()},
                {"globalId", global.has_value() ? json(*global) : json(nullptr)}});
        }
        return result;
    }

    json transformGet(const std::string& id) {
        requireLoaded();
        Transform& transform = requireTransform(id);
        return transform.toJson(*file_, transformWorldMatrix(transform), resolver());
    }

    json transformCreate(
        const std::string& name,
        const json& parents,
        const json& transformInput,
        const json& properties) {
        requireLoaded();
        const ParentSelection selection = resolveParents(parents, nullptr);
        validateParentCompatibility(nullptr, selection);
        Matrix4 requested = matrixFromJson(transformInput.at("matrix"));
        validateRigidMatrix(requested);
        const Matrix4 local = transformInput.at("space").get<std::string>() == "world" ?
            multiplyMatrix(inverseRigidMatrix(parentWorldMatrix(selection.placement)), requested) : requested;

        Base* product = createInstance(*file_, "IfcBuildingElementProxy");
        initializeRoot(*file_, product, name);
        if (hasAttribute(product, "ObjectType")) {
            unsetAttribute(product, "ObjectType");
        }
        setEntityAttribute(product, "ObjectPlacement", createLocalPlacement(
            *file_,
            selection.placement == nullptr ? nullptr : selection.placement->objectPlacement(),
            local));
        if (hasAttribute(product, "Representation")) {
            unsetAttribute(product, "Representation");
        }
        if (hasAttribute(product, "Tag")) {
            unsetAttribute(product, "Tag");
        }
        if (hasAttribute(product, "PredefinedType")) {
            try {
                setEnumeration(product, "PredefinedType", "NOTDEFINED");
            } catch (const std::exception&) {
                unsetAttribute(product, "PredefinedType");
            }
        }

        Entity* productEntity = asEntity(product);
        if (productEntity == nullptr) {
            throw SceneError("INTERNAL_ERROR", "Created IFC product is not an entity");
        }
        const std::string id = nextId("transform", transformCounter_);
        transforms_.emplace(id, std::make_unique<Transform>(id, productEntity));
        transformByEntity_[product] = id;

        try {
            replaceSpatialParent(product, selection.spatial == nullptr ? nullptr : selection.spatial->entity());
            replaceDecompositionParent(product, selection.decomposition == nullptr ? nullptr : selection.decomposition->entity());
            replaceOccurrenceProperties(*file_, product, properties);
            refreshRelations();
            return transformGet(id);
        } catch (...) {
            transforms_.erase(id);
            transformByEntity_.erase(product);
            if (file_ != nullptr) {
                try {
                    file_->removeEntity(product);
                } catch (...) {
                }
            }
            throw;
        }
    }

    json transformDuplicate(const std::string& id, bool includeChildren) {
        requireLoaded();
        requireTransform(id);

        std::unordered_set<std::string> selectedIds{id};
        if (includeChildren) {
            collectTransformChildren(selectedIds);
        }

        std::unordered_map<std::string, std::string> transformMapping;
        std::unordered_map<std::string, std::string> geometryMapping;
        std::unordered_map<const Base*, Base*> representationMapping;
        std::vector<std::string> pending(selectedIds.begin(), selectedIds.end());
        std::sort(pending.begin(), pending.end(), [&id](const std::string& lhs, const std::string& rhs) {
            if (lhs == id || rhs == id) {
                return lhs == id && rhs != id;
            }
            const auto left = lhs.find(':');
            const auto right = rhs.find(':');
            return std::stoull(lhs.substr(left + 1)) < std::stoull(rhs.substr(right + 1));
        });

        std::optional<std::string> duplicatedRootId;
        try {
            while (!pending.empty()) {
                bool progressed = false;
                for (auto iterator = pending.begin(); iterator != pending.end();) {
                    Transform& source = requireTransform(*iterator);
                    if (!internalParentsDuplicated(source, selectedIds, transformMapping)) {
                        ++iterator;
                        continue;
                    }

                    const ParentSelection parents = duplicateParents(source, transformMapping);
                    const bool root = source.id() == id;
                    const std::string duplicateId = duplicateTransformEntity(
                        source,
                        parents,
                        root,
                        representationMapping);
                    transformMapping[source.id()] = duplicateId;
                    if (root) {
                        duplicatedRootId = duplicateId;
                    }
                    iterator = pending.erase(iterator);
                    progressed = true;
                }

                if (!progressed) {
                    throw SceneError(
                        "INVALID_RELATION",
                        "Transform graph contains a parent cycle that cannot be duplicated",
                        json{{"id", id}});
                }
            }

            cloneRepresentationStyles(representationMapping);
            copyDuplicateRelations(transformMapping);
            copyDuplicateTransformMaterials(transformMapping);
            indexGeometries();
            mapDuplicateNativeGeometries(representationMapping, geometryMapping);
            duplicateEditorGeometries(transformMapping, geometryMapping);
            refreshRelations();

            json transformResult = json::object();
            for (const auto& [sourceId, duplicateId] : transformMapping) {
                transformResult[sourceId] = duplicateId;
            }
            json geometryResult = json::object();
            for (const auto& [sourceId, duplicateId] : geometryMapping) {
                geometryResult[sourceId] = duplicateId;
            }
            return json{
                {"rootId", *duplicatedRootId},
                {"transforms", std::move(transformResult)},
                {"geometries", std::move(geometryResult)}};
        } catch (...) {
            if (duplicatedRootId.has_value() && transforms_.count(*duplicatedRootId) != 0) {
                try {
                    std::unordered_set<std::string> visiting;
                    deleteTransformRecursive(*duplicatedRootId, visiting);
                    refreshRelations();
                } catch (...) {
                }
            }
            throw;
        }
    }

    json transformUpdate(const std::string& id, const json& changes) {
        requireLoaded();
        Transform& target = requireTransform(id);

        ParentSelection selection{
            target.spatialParentId().has_value() ? &requireTransform(*target.spatialParentId()) : nullptr,
            target.placementParentId().has_value() ? &requireTransform(*target.placementParentId()) : nullptr,
            target.decompositionParentId().has_value() ? &requireTransform(*target.decompositionParentId()) : nullptr};
        if (changes.contains("parents")) {
            selection = resolveParents(changes.at("parents"), &target);
            validateParentCompatibility(&target, selection);
        }

        const Matrix4 previousLocal = target.localMatrix();
        if (changes.contains("name")) {
            target.setName(changes.at("name").get<std::string>());
        }
        if (changes.contains("parents")) {
            replaceSpatialParent(target.entity(), selection.spatial == nullptr ? nullptr : selection.spatial->entity());
            replaceDecompositionParent(target.entity(), selection.decomposition == nullptr ? nullptr : selection.decomposition->entity());
        }

        if (changes.contains("transform") || changes.contains("parents")) {
            Matrix4 local = previousLocal;
            if (changes.contains("transform")) {
                const json& input = changes.at("transform");
                const Matrix4 requested = matrixFromJson(input.at("matrix"));
                validateRigidMatrix(requested);
                local = input.at("space").get<std::string>() == "world" ?
                    multiplyMatrix(inverseRigidMatrix(parentWorldMatrix(selection.placement)), requested) : requested;
            }
            Base* previousPlacement = target.replacePlacement(
                *file_,
                selection.placement == nullptr ? nullptr : selection.placement->objectPlacement(),
                local);
            removePlacementIfUnreferenced(previousPlacement);
        }
        if (changes.contains("properties")) {
            target.replaceProperties(*file_, changes.at("properties"));
        }
        refreshRelations();
        return transformGet(id);
    }

    json transformDelete(const std::string& id) {
        requireLoaded();
        requireTransform(id);
        std::unordered_set<std::string> visiting;
        deleteTransformRecursive(id, visiting);
        refreshRelations();
        return json{{"id", id}};
    }

    void transformTessellate(const std::string& id, const json& options, const MeshEmitter& emit) {
        requireLoaded();
        Transform& root = requireTransform(id);
        const bool includeChildren = options.at("includeChildren").get<bool>();
        const bool includeNormals = options.at("includeNormals").get<bool>();
        const bool includeMaterials = options.at("includeMaterials").get<bool>();
        const bool worldSpace = options.at("space").get<std::string>() == "world";

        std::unordered_set<std::string> selectedIds{id};
        if (includeChildren) {
            collectTransformChildren(selectedIds);
        }
        std::unordered_set<unsigned> productIds;
        for (const std::string& selectedId : selectedIds) {
            productIds.insert(requireTransform(selectedId).entity()->id());
        }

        try {
            ifcopenshell::geometry::Settings settings;
            settings.set("use-world-coords", true);
            settings.set("weld-vertices", false);
            settings.set("apply-default-materials", true);
            auto kernel = std::make_unique<IfcGeom::OpenCascadeKernel>(settings);
            IfcGeom::Iterator iterator(std::move(kernel), settings, file_.get());

            std::vector<double> vertices;
            std::vector<int> indices;
            std::vector<double> normals;
            std::vector<json> groups;
            const Matrix4 outputTransform = worldSpace ? identityMatrix() : inverseRigidMatrix(transformWorldMatrix(root));

            if (iterator.initialize()) {
                do {
                    const auto* element = dynamic_cast<const IfcGeom::TriangulationElement*>(iterator.get());
                    if (element == nullptr || element->product() == nullptr || productIds.count(element->product()->id()) == 0) {
                        continue;
                    }
                    const auto& triangulation = element->geometry();
                    const std::size_t vertexOffset = vertices.size() / 3;
                    const std::size_t indexOffset = indices.size();
                    const auto& elementVertices = triangulation.verts();
                    for (std::size_t offset = 0; offset + 2 < elementVertices.size(); offset += 3) {
                        const auto point = transformPoint(outputTransform, {
                            elementVertices[offset],
                            elementVertices[offset + 1],
                            elementVertices[offset + 2]});
                        vertices.insert(vertices.end(), point.begin(), point.end());
                    }
                    for (int value : triangulation.faces()) {
                        indices.push_back(static_cast<int>(vertexOffset) + value);
                    }
                    if (includeNormals) {
                        const auto& elementNormals = triangulation.normals();
                        for (std::size_t offset = 0; offset + 2 < elementNormals.size(); offset += 3) {
                            const auto direction = normalize3(transformDirection(outputTransform, {
                                elementNormals[offset],
                                elementNormals[offset + 1],
                                elementNormals[offset + 2]}));
                            normals.insert(normals.end(), direction.begin(), direction.end());
                        }
                    }
                    if (includeMaterials && indices.size() > indexOffset) {
                        const auto transformIterator = transformByEntity_.find(element->product());
                        if (transformIterator != transformByEntity_.end()) {
                            const auto& materialIds = requireTransform(transformIterator->second).materialIds();
                            if (!materialIds.empty()) {
                                groups.push_back(json{
                                    {"materialId", materialIds.front()},
                                    {"indexOffset", indexOffset},
                                    {"indexCount", indices.size() - indexOffset}});
                            }
                        }
                    }
                } while (iterator.next());
            }
            if (iterator.had_error_processing_elements()) {
                throw SceneError("TESSELLATION_FAILED", "IfcOpenShell reported geometry processing errors");
            }

            emit(json{{"event", "mesh.begin"}, {"vertexCount", vertices.size() / 3}, {"indexCount", indices.size()}});
            emitChunks("mesh.vertices", vertices, emit);
            emitChunks("mesh.indices", indices, emit);
            if (includeNormals) {
                emitChunks("mesh.normals", normals, emit);
            }
            if (includeMaterials) {
                emit(json{{"event", "mesh.materials"}, {"groups", groups}});
            }
            emit(json{{"event", "mesh.end"}});
        } catch (const SceneError&) {
            throw;
        } catch (const std::exception& exception) {
            throw SceneError("TESSELLATION_FAILED", "IfcOpenShell could not tessellate the transform", json{{"id", id}, {"reason", exception.what()}});
        }
    }

    json geometryGetAll(const std::optional<std::string>& transformId) {
        requireLoaded();
        if (transformId.has_value()) {
            requireTransform(*transformId);
        }
        json result = json::array();
        for (const std::string& id : sortedIds(geometries_)) {
            const Geometry& geometry = *geometries_.at(id);
            if (transformId.has_value() && geometry.parentId() != *transformId) {
                continue;
            }
            const auto global = geometry.globalId();
            result.push_back(json{
                {"id", id},
                {"ifcEntityId", geometry.entity()->id()},
                {"globalId", global.has_value() ? json(*global) : json(nullptr)},
                {"parentId", geometry.parentId()},
                {"source", geometry.isEditorGeometry() ? "editor" : "ifc"},
                {"editable", geometry.editable()},
                {"operation", geometry.operation()}});
        }
        return result;
    }

    json geometryGet(const std::string& id) {
        requireLoaded();
        return requireGeometry(id).toJson(resolver());
    }

    json geometryCreate(
        const std::string& parentId,
        const std::string& name,
        const json& transformInput,
        const json& profileInput,
        double depth,
        const std::string& operation) {
        requireLoaded();
        Transform& parent = requireTransform(parentId);
        Matrix4 requested = matrixFromJson(transformInput.at("matrix"));
        validateRigidMatrix(requested);
        Matrix4 local = requested;
        if (transformInput.at("space").get<std::string>() == "world") {
            local = multiplyMatrix(inverseRigidMatrix(transformWorldMatrix(parent)), requested);
        }
        const Profile2D profile = profileFromJson(profileInput);
        validateProfile(profile);
        Entity* feature = asEntity(createFeatureElement(*file_, parent.entity(), name, local, profile, depth, operation));
        if (feature == nullptr) {
            throw SceneError("INTERNAL_ERROR", "Created geometry feature is not an IFC entity");
        }
        const std::string id = nextId("geometry", geometryCounter_);
        geometries_.emplace(id, Geometry::createEditor(id, feature, parent.entity(), parentId));
        geometryByEntity_[feature] = id;
        refreshRelations();
        return geometryGet(id);
    }

    json geometryUpdate(const std::string& id, const json& changes) {
        requireLoaded();
        Geometry& geometry = requireGeometry(id);
        if (!geometry.editable()) {
            throw SceneError("GEOMETRY_NOT_EDITABLE", "Geometry is not editable", json{{"id", id}});
        }
        if (changes.contains("name")) {
            geometry.setName(changes.at("name").get<std::string>());
        }
        if (changes.contains("transform")) {
            const json& input = changes.at("transform");
            Matrix4 requested = matrixFromJson(input.at("matrix"));
            validateRigidMatrix(requested);
            Matrix4 local = requested;
            if (input.at("space").get<std::string>() == "world") {
                local = multiplyMatrix(inverseRigidMatrix(transformWorldMatrix(requireTransform(geometry.parentId()))), requested);
            }
            Base* previous = geometry.replacePlacement(*file_, local);
            removePlacementIfUnreferenced(previous);
        }
        if (changes.contains("profile")) {
            const Profile2D profile = profileFromJson(changes.at("profile"));
            validateProfile(profile);
            geometry.setProfile(*file_, profile);
        }
        if (changes.contains("depth")) {
            geometry.setDepth(changes.at("depth").get<double>());
        }
        if (changes.contains("operation")) {
            Base* previousEntity = geometry.entity();
            const std::vector<Base*> obsolete = geometry.convertOperation(*file_, changes.at("operation").get<std::string>());
            if (geometry.entity() != previousEntity) {
                replaceSemanticAssignmentTarget(previousEntity, geometry.entity());
                geometryByEntity_.erase(previousEntity);
                geometryByEntity_[geometry.entity()] = id;
            }
            for (Base* entity : obsolete) {
                if (entity != nullptr) {
                    file_->removeEntity(entity);
                }
            }
        }
        refreshRelations();
        return geometryGet(id);
    }

    json geometryDelete(const std::string& id) {
        requireLoaded();
        Geometry& geometry = requireGeometry(id);
        removeMaterialAssignments(geometry.entity());
        Base* entity = geometry.entity();
        geometry.deleteFromIfc(*file_);
        geometryByEntity_.erase(entity);
        geometries_.erase(id);
        refreshRelations();
        return json{{"id", id}};
    }

    json materialGetAll() {
        requireLoaded();
        json result = json::array();
        for (const std::string& id : sortedIds(materials_)) {
            const Material& material = *materials_.at(id);
            const auto global = material.globalId();
            result.push_back(json{
                {"id", id},
                {"ifcEntityId", material.entity()->id()},
                {"globalId", global.has_value() ? json(*global) : json(nullptr)}});
        }
        return result;
    }

    json materialGet(const std::string& id) {
        requireLoaded();
        return requireMaterial(id).toJson(*file_, resolver());
    }

    json materialCreate(
        const std::string& name,
        const std::string& category,
        const json& visualInput,
        const json& properties) {
        requireLoaded();
        const Visual visual = visualFromJson(visualInput);
        validateVisual(visual);
        const std::string id = nextId("material", materialCounter_);
        std::unique_ptr<Material> material = Material::create(*file_, id, name, category, visual, properties);
        Base* entity = material->entity();
        materials_.emplace(id, std::move(material));
        materialByEntity_[entity] = id;
        refreshRelations();
        return materialGet(id);
    }

    json materialUpdate(const std::string& id, const json& changes) {
        requireLoaded();
        Material& material = requireMaterial(id);
        if (!material.editable()) {
            throw SceneError("MATERIAL_NOT_EDITABLE", "Material is not editable", json{{"id", id}});
        }
        if (changes.contains("name")) {
            material.setName(*file_, changes.at("name").get<std::string>());
        }
        if (changes.contains("category")) {
            material.setCategory(*file_, changes.at("category").get<std::string>());
        }
        if (changes.contains("visual")) {
            material.setVisual(*file_, visualFromJson(changes.at("visual")));
        }
        if (changes.contains("properties")) {
            material.replaceProperties(*file_, changes.at("properties"));
        }
        refreshRelations();
        return materialGet(id);
    }

    json materialDelete(const std::string& id) {
        requireLoaded();
        Material& material = requireMaterial(id);
        Base* entity = material.entity();
        removeAllAssignmentsForMaterial(entity);
        material.deleteFromIfc(*file_);
        materialByEntity_.erase(entity);
        materials_.erase(id);
        refreshRelations();
        return json{{"id", id}};
    }

    json materialAssign(const std::string& materialId, const std::string& targetId) {
        requireLoaded();
        Material& material = requireMaterial(materialId);
        Target target = resolveTarget(targetId);
        if (assignmentExists(material, target)) {
            return json{{"materialId", materialId}, {"targetId", targetId}};
        }
        if (target.semanticObject != nullptr) {
            assignSemanticMaterial(material.entity(), target.semanticObject);
        } else {
            assignVisualMaterial(material, target.entity);
        }
        refreshRelations();
        return json{{"materialId", materialId}, {"targetId", targetId}};
    }

    json materialUnassign(const std::string& materialId, const std::string& targetId) {
        requireLoaded();
        Material& material = requireMaterial(materialId);
        Target target = resolveTarget(targetId);
        bool removed = false;
        if (target.semanticObject != nullptr) {
            removed = unassignSemanticMaterial(material.entity(), target.semanticObject);
        }
        if (!removed) {
            removed = unassignVisualMaterial(material, target.entity);
        }
        if (!removed) {
            throw SceneError("INVALID_RELATION", "Material assignment does not exist", json{{"materialId", materialId}, {"targetId", targetId}});
        }
        refreshRelations();
        return json{{"materialId", materialId}, {"targetId", targetId}};
    }

private:
    struct ParentSelection {
        Transform* spatial;
        Transform* placement;
        Transform* decomposition;
    };

    struct Target {
        Base* entity;
        Entity* semanticObject;
    };

    static std::string ifcOpenShellVersion() {
#if defined(IFCOPENSHELL_VERSION_STRING)
        return IFCOPENSHELL_VERSION_STRING;
#elif defined(IFCOPENSHELL_VERSION)
        std::ostringstream output;
        output << IFCOPENSHELL_VERSION;
        return output.str();
#else
        return "unknown";
#endif
    }

    template <typename Map>
    static std::vector<std::string> sortedIds(const Map& map) {
        std::vector<std::string> ids;
        ids.reserve(map.size());
        for (const auto& [id, value] : map) {
            static_cast<void>(value);
            ids.push_back(id);
        }
        std::sort(ids.begin(), ids.end(), [](const std::string& lhs, const std::string& rhs) {
            const auto left = lhs.find(':');
            const auto right = rhs.find(':');
            return std::stoull(lhs.substr(left + 1)) < std::stoull(rhs.substr(right + 1));
        });
        return ids;
    }

    static std::string nextId(const std::string& prefix, std::uint64_t& counter) {
        ++counter;
        return prefix + ":" + std::to_string(counter);
    }

    using EntityMapper = std::function<Base*(Base*)>;

    void copyAttributeValue(
        Base* target,
        std::size_t index,
        const AttributeValue& value,
        const EntityMapper& mapEntity) {
        if (value.isNull()) {
            target->unset_attribute_value(index);
            return;
        }

        switch (value.type()) {
            case IfcUtil::Argument_DERIVED:
                target->set_attribute_value(index, Derived{});
                return;
            case IfcUtil::Argument_INT:
                target->set_attribute_value(index, static_cast<int>(value));
                return;
            case IfcUtil::Argument_BOOL:
                target->set_attribute_value(index, static_cast<bool>(value));
                return;
            case IfcUtil::Argument_LOGICAL: {
                const boost::logic::tribool logical = value;
                target->set_attribute_value(index, logical);
                return;
            }
            case IfcUtil::Argument_DOUBLE:
                target->set_attribute_value(index, static_cast<double>(value));
                return;
            case IfcUtil::Argument_STRING:
                target->set_attribute_value(index, static_cast<std::string>(value));
                return;
            case IfcUtil::Argument_BINARY:
                target->set_attribute_value(index, static_cast<boost::dynamic_bitset<>>(value));
                return;
            case IfcUtil::Argument_ENUMERATION:
                target->set_attribute_value(index, static_cast<EnumerationReference>(value));
                return;
            case IfcUtil::Argument_ENTITY_INSTANCE:
                target->set_attribute_value(index, mapEntity(static_cast<Base*>(value)));
                return;
            case IfcUtil::Argument_EMPTY_AGGREGATE:
                target->set_attribute_value(index, empty_aggregate_t{});
                return;
            case IfcUtil::Argument_AGGREGATE_OF_INT:
                target->set_attribute_value(index, static_cast<std::vector<int>>(value));
                return;
            case IfcUtil::Argument_AGGREGATE_OF_DOUBLE:
                target->set_attribute_value(index, static_cast<std::vector<double>>(value));
                return;
            case IfcUtil::Argument_AGGREGATE_OF_STRING:
                target->set_attribute_value(index, static_cast<std::vector<std::string>>(value));
                return;
            case IfcUtil::Argument_AGGREGATE_OF_BINARY:
                target->set_attribute_value(index, static_cast<std::vector<boost::dynamic_bitset<>>>(value));
                return;
            case IfcUtil::Argument_AGGREGATE_OF_ENTITY_INSTANCE: {
                const auto source = static_cast<boost::shared_ptr<aggregate_of_instance>>(value);
                boost::shared_ptr<aggregate_of_instance> duplicate(new aggregate_of_instance);
                duplicate->reserve(source->size());
                for (Base* entity : aggregateToVector(source)) {
                    duplicate->push(mapEntity(entity));
                }
                target->set_attribute_value(index, duplicate);
                return;
            }
            case IfcUtil::Argument_AGGREGATE_OF_EMPTY_AGGREGATE:
                target->set_attribute_value(index, empty_aggregate_of_aggregate_t{});
                return;
            case IfcUtil::Argument_AGGREGATE_OF_AGGREGATE_OF_INT:
                target->set_attribute_value(index, static_cast<std::vector<std::vector<int>>>(value));
                return;
            case IfcUtil::Argument_AGGREGATE_OF_AGGREGATE_OF_DOUBLE:
                target->set_attribute_value(index, static_cast<std::vector<std::vector<double>>>(value));
                return;
            case IfcUtil::Argument_AGGREGATE_OF_AGGREGATE_OF_ENTITY_INSTANCE: {
                const auto source = static_cast<boost::shared_ptr<aggregate_of_aggregate_of_instance>>(value);
                boost::shared_ptr<aggregate_of_aggregate_of_instance> duplicate(new aggregate_of_aggregate_of_instance);
                for (auto outer = source->begin(); outer != source->end(); ++outer) {
                    std::vector<Base*> values;
                    values.reserve(outer->size());
                    for (Base* entity : *outer) {
                        values.push_back(mapEntity(entity));
                    }
                    duplicate->push(values);
                }
                target->set_attribute_value(index, duplicate);
                return;
            }
            default:
                throw SceneError(
                    "INTERNAL_ERROR",
                    "Unsupported IFC attribute type while duplicating a transform",
                    json{{"ifcClass", target->declaration().name()}, {"attributeIndex", index}});
        }
    }

    void copyEntityAttributes(
        const Base* source,
        Base* target,
        const std::set<std::string>& excludedAttributes,
        const EntityMapper& mapEntity) {
        const auto* declaration = source->declaration().as_entity();
        if (declaration == nullptr) {
            throw SceneError(
                "INTERNAL_ERROR",
                "Cannot duplicate a non-entity IFC instance",
                json{{"ifcClass", source->declaration().name()}});
        }
        const auto attributes = declaration->all_attributes();
        for (std::size_t index = 0; index < attributes.size(); ++index) {
            if (excludedAttributes.count(attributes[index]->name()) != 0) {
                continue;
            }
            copyAttributeValue(target, index, source->get_attribute_value(index), mapEntity);
        }
    }

    bool shareRepresentationReference(const std::string& attributeName, const Base* referenced) const {
        if (referenced == nullptr || referenced->id() == 0) {
            return true;
        }
        if (attributeName == "ContextOfItems" || attributeName == "MappingSource" || attributeName == "Styles") {
            return true;
        }
        return isA(referenced, "IfcRepresentationContext") ||
            isA(referenced, "IfcPresentationStyle") ||
            isA(referenced, "IfcOwnerHistory") ||
            isA(referenced, "IfcExternalReference");
    }

    Base* cloneRepresentationEntity(
        Base* source,
        std::unordered_map<const Base*, Base*>& mapping) {
        if (source == nullptr || source->id() == 0) {
            return source;
        }
        const auto existing = mapping.find(source);
        if (existing != mapping.end()) {
            return existing->second;
        }

        const auto* declaration = source->declaration().as_entity();
        if (declaration == nullptr) {
            return source;
        }
        Base* duplicate = createInstance(*file_, source->declaration().name());
        mapping[source] = duplicate;
        const auto attributes = declaration->all_attributes();
        for (std::size_t index = 0; index < attributes.size(); ++index) {
            const std::string attributeName = attributes[index]->name();
            copyAttributeValue(
                duplicate,
                index,
                source->get_attribute_value(index),
                [this, &mapping, attributeName](Base* referenced) -> Base* {
                    if (shareRepresentationReference(attributeName, referenced)) {
                        return referenced;
                    }
                    return cloneRepresentationEntity(referenced, mapping);
                });
        }
        return duplicate;
    }

    void cloneRepresentationStyles(const std::unordered_map<const Base*, Base*>& mapping) {
        const std::vector<Base*> styledItems = instancesByType(*file_, "IfcStyledItem");
        for (Base* styledItem : styledItems) {
            Base* item = getEntityAttribute(styledItem, "Item");
            const auto replacement = mapping.find(item);
            if (replacement == mapping.end()) {
                continue;
            }
            Base* duplicate = createInstance(*file_, styledItem->declaration().name());
            copyEntityAttributes(
                styledItem,
                duplicate,
                {},
                [&mapping](Base* referenced) -> Base* {
                    const auto iterator = mapping.find(referenced);
                    return iterator == mapping.end() ? referenced : iterator->second;
                });
        }
    }

    bool internalParentsDuplicated(
        const Transform& source,
        const std::unordered_set<std::string>& selectedIds,
        const std::unordered_map<std::string, std::string>& mapping) const {
        for (const auto* parentId : {
                 &source.spatialParentId(),
                 &source.placementParentId(),
                 &source.decompositionParentId()}) {
            if (parentId->has_value() &&
                selectedIds.count(**parentId) != 0 &&
                mapping.count(**parentId) == 0) {
                return false;
            }
        }
        return true;
    }

    Transform* duplicateParent(
        const std::optional<std::string>& sourceParentId,
        const std::unordered_map<std::string, std::string>& mapping) {
        if (!sourceParentId.has_value()) {
            return nullptr;
        }
        const auto duplicate = mapping.find(*sourceParentId);
        return duplicate == mapping.end() ?
            &requireTransform(*sourceParentId) :
            &requireTransform(duplicate->second);
    }

    ParentSelection duplicateParents(
        const Transform& source,
        const std::unordered_map<std::string, std::string>& mapping) {
        return ParentSelection{
            duplicateParent(source.spatialParentId(), mapping),
            duplicateParent(source.placementParentId(), mapping),
            duplicateParent(source.decompositionParentId(), mapping)};
    }

    std::string duplicateTransformEntity(
        Transform& source,
        const ParentSelection& parents,
        bool renameRoot,
        std::unordered_map<const Base*, Base*>& representationMapping) {
        Base* sourceEntity = source.entity();
        if (isA(sourceEntity, "IfcProject")) {
            throw SceneError(
                "TRANSFORM_NOT_DUPLICABLE",
                "IfcProject transforms cannot be duplicated",
                json{{"id", source.id()}});
        }
        Base* sourcePlacement = source.objectPlacement();
        if (sourcePlacement != nullptr && !isA(sourcePlacement, "IfcLocalPlacement")) {
            throw SceneError(
                "TRANSFORM_NOT_DUPLICABLE",
                "Only transforms using IfcLocalPlacement can be duplicated",
                json{{"id", source.id()}, {"sourceType", source.placementSourceType()}});
        }

        Base* duplicate = createInstance(*file_, sourceEntity->declaration().name());
        copyEntityAttributes(
            sourceEntity,
            duplicate,
            {"GlobalId", "ObjectPlacement", "Representation"},
            [](Base* referenced) -> Base* { return referenced; });

        if (hasAttribute(duplicate, "GlobalId")) {
            IfcParse::IfcGlobalId globalId;
            setAttribute(duplicate, "GlobalId", static_cast<const std::string&>(globalId));
        }
        if (renameRoot && hasAttribute(duplicate, "Name")) {
            const std::string sourceName = source.name();
            setAttribute(duplicate, "Name", sourceName.empty() ? "Copy" : sourceName + " Copy");
        }
        if (hasAttribute(duplicate, "ObjectPlacement")) {
            if (sourcePlacement == nullptr) {
                unsetAttribute(duplicate, "ObjectPlacement");
            } else {
                setEntityAttribute(
                    duplicate,
                    "ObjectPlacement",
                    createLocalPlacement(
                        *file_,
                        parents.placement == nullptr ? nullptr : parents.placement->objectPlacement(),
                        source.localMatrix()));
            }
        }
        if (hasAttribute(duplicate, "Representation")) {
            Base* representation = getEntityAttribute(sourceEntity, "Representation");
            if (representation == nullptr) {
                unsetAttribute(duplicate, "Representation");
            } else {
                setEntityAttribute(
                    duplicate,
                    "Representation",
                    cloneRepresentationEntity(representation, representationMapping));
            }
        }

        Entity* duplicateEntity = asEntity(duplicate);
        if (duplicateEntity == nullptr) {
            file_->removeEntity(duplicate);
            throw SceneError(
                "INTERNAL_ERROR",
                "Duplicated IFC transform is not an entity",
                json{{"ifcClass", sourceEntity->declaration().name()}});
        }

        const std::string duplicateId = nextId("transform", transformCounter_);
        transforms_.emplace(duplicateId, std::make_unique<Transform>(duplicateId, duplicateEntity));
        transformByEntity_[duplicate] = duplicateId;
        replaceSpatialParent(duplicate, parents.spatial == nullptr ? nullptr : parents.spatial->entity());
        replaceDecompositionParent(duplicate, parents.decomposition == nullptr ? nullptr : parents.decomposition->entity());
        replaceOccurrenceProperties(*file_, duplicate, readOccurrenceProperties(*file_, sourceEntity));
        return duplicateId;
    }

    void copyDuplicateRelations(const std::unordered_map<std::string, std::string>& mapping) {
        for (const auto& [sourceId, duplicateId] : mapping) {
            Base* source = requireTransform(sourceId).entity();
            Base* duplicate = requireTransform(duplicateId).entity();
            const std::vector<Base*> references = aggregateToVector(
                file_->instances_by_reference(static_cast<int>(source->id())));
            for (Base* relation : references) {
                if (relation == nullptr ||
                    !isA(relation, "IfcRelationship") ||
                    !hasAttribute(relation, "RelatedObjects") ||
                    !aggregateContains(getEntityAggregate(relation, "RelatedObjects"), source)) {
                    continue;
                }
                if (isA(relation, "IfcRelContainedInSpatialStructure") ||
                    isA(relation, "IfcRelAggregates") ||
                    isA(relation, "IfcRelNests") ||
                    isA(relation, "IfcRelAssociatesMaterial")) {
                    continue;
                }
                if (isA(relation, "IfcRelDefinesByProperties")) {
                    Base* definition = getEntityAttribute(relation, "RelatingPropertyDefinition");
                    const auto relatedObjects = getEntityAggregate(relation, "RelatedObjects");
                    if (definition != nullptr &&
                        isA(definition, "IfcPropertySet") &&
                        relatedObjects != nullptr &&
                        relatedObjects->size() == 1) {
                        continue;
                    }
                }
                appendUniqueToAggregate(relation, "RelatedObjects", duplicate);
            }
        }
    }

    void copyDuplicateTransformMaterials(const std::unordered_map<std::string, std::string>& mapping) {
        for (const auto& [sourceId, duplicateId] : mapping) {
            Transform& source = requireTransform(sourceId);
            Transform& duplicate = requireTransform(duplicateId);
            for (const std::string& materialId : source.materialIds()) {
                assignSemanticMaterial(requireMaterial(materialId).entity(), duplicate.entity());
            }
        }
    }

    void mapDuplicateNativeGeometries(
        const std::unordered_map<const Base*, Base*>& representationMapping,
        std::unordered_map<std::string, std::string>& geometryMapping) const {
        for (const auto& [sourceEntity, duplicateEntity] : representationMapping) {
            const auto sourceGeometry = geometryByEntity_.find(sourceEntity);
            const auto duplicateGeometry = geometryByEntity_.find(duplicateEntity);
            if (sourceGeometry == geometryByEntity_.end() ||
                duplicateGeometry == geometryByEntity_.end()) {
                continue;
            }
            geometryMapping[sourceGeometry->second] = duplicateGeometry->second;
        }
    }

    void duplicateEditorGeometries(
        const std::unordered_map<std::string, std::string>& transformMapping,
        std::unordered_map<std::string, std::string>& geometryMapping) {
        for (const auto& [sourceTransformId, duplicateTransformId] : transformMapping) {
            const std::vector<std::string> sourceGeometryIds = requireTransform(sourceTransformId).geometryIds();
            for (const std::string& sourceGeometryId : sourceGeometryIds) {
                Geometry& source = requireGeometry(sourceGeometryId);
                if (!source.isEditorGeometry()) {
                    continue;
                }
                const std::vector<std::string> materialIds = source.materialIds();
                const json duplicate = geometryCreate(
                    duplicateTransformId,
                    source.name(),
                    json{{"space", "parent"}, {"matrix", matrixToJson(source.localMatrix())}},
                    profileToJson(source.profile()),
                    source.depth(),
                    source.operation());
                const std::string duplicateGeometryId = duplicate.at("id").get<std::string>();
                geometryMapping[sourceGeometryId] = duplicateGeometryId;
                for (const std::string& materialId : materialIds) {
                    materialAssign(materialId, duplicateGeometryId);
                }
            }
        }
    }

    void clear() {
        geometries_.clear();
        materials_.clear();
        transforms_.clear();
        geometryByEntity_.clear();
        materialByEntity_.clear();
        transformByEntity_.clear();
        file_.reset();
        loadedPath_.clear();
        schema_.clear();
        transformCounter_ = 0;
        geometryCounter_ = 0;
        materialCounter_ = 0;
    }

    void requireLoaded() const {
        if (file_ == nullptr) {
            throw SceneError("NO_MODEL_LOADED", "No IFC model is loaded");
        }
    }

    static void validateIdPrefix(const std::string& id, const std::string& prefix) {
        if (id.rfind(prefix + ":", 0) != 0) {
            throw SceneError("INVALID_ENTITY_TYPE", "Session identifier has an invalid entity type", json{{"id", id}, {"expectedType", prefix}});
        }
    }

    Transform& requireTransform(const std::string& id) {
        validateIdPrefix(id, "transform");
        const auto iterator = transforms_.find(id);
        if (iterator == transforms_.end()) {
            throw SceneError("ENTITY_NOT_FOUND", "Transform does not exist", json{{"id", id}});
        }
        return *iterator->second;
    }

    Geometry& requireGeometry(const std::string& id) {
        validateIdPrefix(id, "geometry");
        const auto iterator = geometries_.find(id);
        if (iterator == geometries_.end()) {
            throw SceneError("ENTITY_NOT_FOUND", "Geometry does not exist", json{{"id", id}});
        }
        return *iterator->second;
    }

    Material& requireMaterial(const std::string& id) {
        validateIdPrefix(id, "material");
        const auto iterator = materials_.find(id);
        if (iterator == materials_.end()) {
            throw SceneError("ENTITY_NOT_FOUND", "Material does not exist", json{{"id", id}});
        }
        return *iterator->second;
    }

    EntityResolver resolver() const {
        return [this](const Base* entity) -> json {
            if (entity == nullptr) {
                return nullptr;
            }
            const auto transform = transformByEntity_.find(entity);
            if (transform != transformByEntity_.end()) {
                return transform->second;
            }
            const auto geometry = geometryByEntity_.find(entity);
            if (geometry != geometryByEntity_.end()) {
                return geometry->second;
            }
            const auto material = materialByEntity_.find(entity);
            if (material != materialByEntity_.end()) {
                return material->second;
            }
            return defaultReference(entity);
        };
    }

    void buildIndexes() {
        indexTransforms();
        indexGeometries();
        indexMaterials();
        refreshRelations();
    }

    void indexTransforms() {
        std::unordered_set<Base*> candidates;
        for (Base* entity : instancesByType(*file_, "IfcProduct")) {
            if (!isA(entity, "IfcOpeningElement") && !isA(entity, "IfcProjectionElement")) {
                candidates.insert(entity);
            }
        }
        for (Base* entity : instancesByType(*file_, "IfcProject")) {
            candidates.insert(entity);
        }
        for (const std::string& relationClass : {"IfcRelAggregates", "IfcRelNests"}) {
            for (Base* relation : instancesByType(*file_, relationClass)) {
                Base* relating = getEntityAttribute(relation, "RelatingObject");
                if (relating != nullptr) {
                    candidates.insert(relating);
                }
                for (Base* related : aggregateToVector(getEntityAggregate(relation, "RelatedObjects"))) {
                    candidates.insert(related);
                }
            }
        }
        std::vector<Base*> ordered(candidates.begin(), candidates.end());
        std::sort(ordered.begin(), ordered.end(), [](const Base* lhs, const Base* rhs) { return lhs->id() < rhs->id(); });
        for (Base* candidate : ordered) {
            Entity* entity = asEntity(candidate);
            if (entity == nullptr) {
                continue;
            }
            const std::string id = nextId("transform", transformCounter_);
            transforms_.emplace(id, std::make_unique<Transform>(id, entity));
            transformByEntity_[entity] = id;
        }
    }

    void indexGeometries() {
        std::unordered_set<Base*> editorFeatures;
        for (Base* relation : instancesByType(*file_, "IfcRelVoidsElement")) {
            Entity* parent = asEntity(getEntityAttribute(relation, "RelatingBuildingElement"));
            Entity* feature = asEntity(getEntityAttribute(relation, "RelatedOpeningElement"));
            addEditorGeometry(parent, feature, editorFeatures);
        }
        for (Base* relation : instancesByType(*file_, "IfcRelProjectsElement")) {
            Entity* parent = asEntity(getEntityAttribute(relation, "RelatingElement"));
            Entity* feature = asEntity(getEntityAttribute(relation, "RelatedFeatureElement"));
            addEditorGeometry(parent, feature, editorFeatures);
        }

        for (const auto& [transformId, transform] : transforms_) {
            Base* representation = getEntityAttribute(transform->entity(), "Representation");
            if (representation == nullptr) {
                continue;
            }
            for (Base* shapeRepresentation : aggregateToVector(getEntityAggregate(representation, "Representations"))) {
                for (Base* item : aggregateToVector(getEntityAggregate(shapeRepresentation, "Items"))) {
                    if (item == nullptr || editorFeatures.count(item) != 0 || geometryByEntity_.count(item) != 0) {
                        continue;
                    }
                    const std::string geometryId = nextId("geometry", geometryCounter_);
                    geometries_.emplace(geometryId, Geometry::createNative(
                        geometryId,
                        item,
                        transform->entity(),
                        transformId));
                    geometryByEntity_[item] = geometryId;
                }
            }
        }
    }

    void addEditorGeometry(Entity* parent, Entity* feature, std::unordered_set<Base*>& editorFeatures) {
        if (parent == nullptr || feature == nullptr) {
            return;
        }
        const auto parentIterator = transformByEntity_.find(parent);
        if (parentIterator == transformByEntity_.end() || geometryByEntity_.count(feature) != 0) {
            return;
        }
        const std::string id = nextId("geometry", geometryCounter_);
        geometries_.emplace(id, Geometry::createEditor(id, feature, parent, parentIterator->second));
        geometryByEntity_[feature] = id;
        editorFeatures.insert(feature);
    }

    void indexMaterials() {
        std::unordered_set<Base*> candidates;
        for (const std::string& className : {
                 "IfcMaterial",
                 "IfcMaterialList",
                 "IfcMaterialLayer",
                 "IfcMaterialLayerSet",
                 "IfcMaterialLayerSetUsage",
                 "IfcMaterialProfile",
                 "IfcMaterialProfileSet",
                 "IfcMaterialProfileSetUsage",
                 "IfcMaterialConstituent",
                 "IfcMaterialConstituentSet"}) {
            try {
                for (Base* material : instancesByType(*file_, className)) {
                    candidates.insert(material);
                }
            } catch (const std::exception&) {
            }
        }
        std::vector<Base*> ordered(candidates.begin(), candidates.end());
        std::sort(ordered.begin(), ordered.end(), [](const Base* lhs, const Base* rhs) { return lhs->id() < rhs->id(); });
        for (Base* candidate : ordered) {
            const std::string id = nextId("material", materialCounter_);
            const bool editable = isA(candidate, "IfcMaterial") && isEditorMaterial(*file_, candidate);
            materials_.emplace(id, std::make_unique<Material>(id, candidate, editable));
            materialByEntity_[candidate] = id;
        }
    }

    void refreshRelations() {
        if (file_ == nullptr) {
            return;
        }
        refreshParentRelations();
        refreshGeometryLinks();
        refreshMaterialLinks();
    }

    void refreshParentRelations() {
        for (auto& [id, transform] : transforms_) {
            static_cast<void>(id);
            transform->setParents(std::nullopt, std::nullopt, std::nullopt);
        }

        for (Base* relation : instancesByType(*file_, "IfcRelContainedInSpatialStructure")) {
            const auto parent = transformByEntity_.find(getEntityAttribute(relation, "RelatingStructure"));
            if (parent == transformByEntity_.end()) {
                continue;
            }
            for (Base* child : aggregateToVector(getEntityAggregate(relation, "RelatedElements"))) {
                const auto childIterator = transformByEntity_.find(child);
                if (childIterator == transformByEntity_.end()) {
                    continue;
                }
                Transform& transform = *transforms_.at(childIterator->second);
                transform.setParents(parent->second, transform.placementParentId(), transform.decompositionParentId());
            }
        }

        for (const std::string& relationClass : {"IfcRelAggregates", "IfcRelNests"}) {
            for (Base* relation : instancesByType(*file_, relationClass)) {
                const auto parent = transformByEntity_.find(getEntityAttribute(relation, "RelatingObject"));
                if (parent == transformByEntity_.end()) {
                    continue;
                }
                for (Base* child : aggregateToVector(getEntityAggregate(relation, "RelatedObjects"))) {
                    const auto childIterator = transformByEntity_.find(child);
                    if (childIterator == transformByEntity_.end()) {
                        continue;
                    }
                    Transform& transform = *transforms_.at(childIterator->second);
                    transform.setParents(transform.spatialParentId(), transform.placementParentId(), parent->second);
                }
            }
        }

        std::unordered_map<Base*, std::string> placementOwners;
        for (const auto& [id, transform] : transforms_) {
            Base* placement = transform->objectPlacement();
            if (placement != nullptr) {
                placementOwners[placement] = id;
            }
        }
        for (auto& [id, transform] : transforms_) {
            static_cast<void>(id);
            Base* placement = transform->objectPlacement();
            Base* parentPlacement = isA(placement, "IfcLocalPlacement") ? getEntityAttribute(placement, "PlacementRelTo") : nullptr;
            const auto parent = placementOwners.find(parentPlacement);
            transform->setParents(
                transform->spatialParentId(),
                parent == placementOwners.end() ? std::nullopt : std::optional<std::string>(parent->second),
                transform->decompositionParentId());
        }
    }

    void refreshGeometryLinks() {
        std::unordered_map<std::string, std::vector<std::string>> byTransform;
        for (const auto& [id, geometry] : geometries_) {
            byTransform[geometry->parentId()].push_back(id);
        }
        for (auto& [id, transform] : transforms_) {
            auto& ids = byTransform[id];
            std::sort(ids.begin(), ids.end());
            transform->setGeometryIds(ids);
        }
    }

    void refreshMaterialLinks() {
        std::unordered_map<Base*, std::unordered_set<std::string>> assigned;
        for (Base* relation : instancesByType(*file_, "IfcRelAssociatesMaterial")) {
            Base* material = getEntityAttribute(relation, "RelatingMaterial");
            const auto materialIterator = materialByEntity_.find(material);
            if (materialIterator == materialByEntity_.end()) {
                continue;
            }
            for (Base* target : aggregateToVector(getEntityAggregate(relation, "RelatedObjects"))) {
                assigned[target].insert(materialIterator->second);
            }
        }
        for (Base* styledItem : instancesByType(*file_, "IfcStyledItem")) {
            Base* item = getEntityAttribute(styledItem, "Item");
            if (item == nullptr) {
                continue;
            }
            for (const auto& [materialId, material] : materials_) {
                Base* surfaceStyle = material->surfaceStyle(*file_);
                if (surfaceStyle != nullptr && styledItemContainsStyle(styledItem, surfaceStyle)) {
                    assigned[item].insert(materialId);
                }
            }
        }

        for (auto& [id, transform] : transforms_) {
            std::vector<std::string> ids(assigned[transform->entity()].begin(), assigned[transform->entity()].end());
            std::sort(ids.begin(), ids.end());
            transform->setMaterialIds(std::move(ids));
        }
        for (auto& [id, geometry] : geometries_) {
            std::unordered_set<std::string> ids = assigned[geometry->entity()];
            if (geometry->isEditorGeometry()) {
                const auto semantic = assigned.find(geometry->entity());
                if (semantic != assigned.end()) {
                    ids.insert(semantic->second.begin(), semantic->second.end());
                }
            }
            std::vector<std::string> output(ids.begin(), ids.end());
            std::sort(output.begin(), output.end());
            geometry->setMaterialIds(std::move(output));
        }
    }

    Matrix4 transformWorldMatrix(const Transform& transform) const {
        Base* placement = transform.objectPlacement();
        if (placement == nullptr) {
            return identityMatrix();
        }
        try {
            return worldPlacementMatrix(placement);
        } catch (const std::exception& exception) {
            throw SceneError("INVALID_PLACEMENT", "Transform placement cannot be resolved", json{{"id", transform.id()}, {"reason", exception.what()}});
        }
    }

    Matrix4 parentWorldMatrix(const Transform* parent) const {
        return parent == nullptr ? identityMatrix() : transformWorldMatrix(*parent);
    }

    ParentSelection resolveParents(const json& parents, Transform* target) {
        ParentSelection selection{
            parents.at("spatial").is_null() ? nullptr : &requireTransform(parents.at("spatial").get<std::string>()),
            parents.at("placement").is_null() ? nullptr : &requireTransform(parents.at("placement").get<std::string>()),
            parents.at("decomposition").is_null() ? nullptr : &requireTransform(parents.at("decomposition").get<std::string>())};
        for (Transform* parent : {selection.spatial, selection.placement, selection.decomposition}) {
            if (target != nullptr && parent == target) {
                throw SceneError("INVALID_RELATION", "A transform cannot be its own parent", json{{"id", target->id()}});
            }
        }
        if (target != nullptr) {
            validateNoParentCycle(*target, selection.spatial, &Transform::spatialParentId);
            validateNoParentCycle(*target, selection.placement, &Transform::placementParentId);
            validateNoParentCycle(*target, selection.decomposition, &Transform::decompositionParentId);
        }
        return selection;
    }

    using ParentAccessor = const std::optional<std::string>& (Transform::*)() const noexcept;

    void validateNoParentCycle(Transform& target, Transform* parent, ParentAccessor accessor) {
        std::unordered_set<std::string> visited;
        while (parent != nullptr) {
            if (parent->id() == target.id()) {
                throw SceneError("INVALID_RELATION", "Parent relation would create a cycle", json{{"id", target.id()}});
            }
            if (!visited.insert(parent->id()).second) {
                throw SceneError("INVALID_RELATION", "Existing parent relation contains a cycle", json{{"id", parent->id()}});
            }
            const auto& next = (parent->*accessor)();
            parent = next.has_value() ? &requireTransform(*next) : nullptr;
        }
    }

    void validateParentCompatibility(Transform* target, const ParentSelection& selection) {
        static_cast<void>(target);
        if (selection.spatial != nullptr &&
            !isA(selection.spatial->entity(), "IfcSpatialStructureElement") &&
            !isA(selection.spatial->entity(), "IfcSpatialElement")) {
            throw SceneError("INVALID_RELATION", "Spatial parent is not an IFC spatial element", json{{"id", selection.spatial->id()}});
        }
        if (selection.decomposition != nullptr && !isA(selection.decomposition->entity(), "IfcObjectDefinition")) {
            throw SceneError("INVALID_RELATION", "Decomposition parent is not an IfcObjectDefinition", json{{"id", selection.decomposition->id()}});
        }
        if (selection.placement != nullptr && selection.placement->objectPlacement() == nullptr) {
            throw SceneError("INVALID_RELATION", "Placement parent has no materialized IFC placement", json{{"id", selection.placement->id()}});
        }
    }

    void replaceSpatialParent(Base* child, Base* parent) {
        detachFromAggregateRelations(child, "IfcRelContainedInSpatialStructure", "RelatedElements");
        if (parent == nullptr) {
            return;
        }
        for (Base* relation : instancesByType(*file_, "IfcRelContainedInSpatialStructure")) {
            if (getEntityAttribute(relation, "RelatingStructure") == parent) {
                appendUniqueToAggregate(relation, "RelatedElements", child);
                return;
            }
        }
        Base* relation = createInstance(*file_, "IfcRelContainedInSpatialStructure");
        initializeRoot(*file_, relation, std::string());
        setAttribute(relation, "RelatedElements", makeAggregate({child}));
        setEntityAttribute(relation, "RelatingStructure", parent);
    }

    void replaceDecompositionParent(Base* child, Base* parent) {
        detachFromAggregateRelations(child, "IfcRelAggregates", "RelatedObjects");
        detachFromAggregateRelations(child, "IfcRelNests", "RelatedObjects");
        if (parent == nullptr) {
            return;
        }
        for (Base* relation : instancesByType(*file_, "IfcRelAggregates")) {
            if (getEntityAttribute(relation, "RelatingObject") == parent) {
                appendUniqueToAggregate(relation, "RelatedObjects", child);
                return;
            }
        }
        Base* relation = createInstance(*file_, "IfcRelAggregates");
        initializeRoot(*file_, relation, std::string());
        setEntityAttribute(relation, "RelatingObject", parent);
        setAttribute(relation, "RelatedObjects", makeAggregate({child}));
    }

    void detachFromAggregateRelations(Base* child, const std::string& className, const std::string& aggregateAttribute) {
        std::vector<Base*> emptyRelations;
        for (Base* relation : instancesByType(*file_, className)) {
            if (!aggregateContains(getEntityAggregate(relation, aggregateAttribute), child)) {
                continue;
            }
            removeFromAggregate(relation, aggregateAttribute, child);
            const auto remaining = getEntityAggregate(relation, aggregateAttribute);
            if (remaining == nullptr || remaining->size() == 0) {
                emptyRelations.push_back(relation);
            }
        }
        for (Base* relation : emptyRelations) {
            file_->removeEntity(relation);
        }
    }

    void removePlacementIfUnreferenced(Base* placement) {
        for (Base* entity : collectPlacementOwnedEntities(placement)) {
            removeIfUnreferenced(*file_, entity);
        }
    }

    void collectTransformChildren(std::unordered_set<std::string>& output) {
        bool changed = true;
        while (changed) {
            changed = false;
            for (const auto& [candidateId, candidate] : transforms_) {
                if (output.count(candidateId) != 0) {
                    continue;
                }
                const bool child =
                    (candidate->spatialParentId().has_value() && output.count(*candidate->spatialParentId()) != 0) ||
                    (candidate->placementParentId().has_value() && output.count(*candidate->placementParentId()) != 0) ||
                    (candidate->decompositionParentId().has_value() && output.count(*candidate->decompositionParentId()) != 0);
                if (child) {
                    output.insert(candidateId);
                    changed = true;
                }
            }
        }
    }

    void deleteTransformRecursive(const std::string& id, std::unordered_set<std::string>& visiting) {
        if (!visiting.insert(id).second) {
            return;
        }
        std::vector<std::string> children;
        for (const auto& [candidateId, candidate] : transforms_) {
            if (candidateId == id) {
                continue;
            }
            if ((candidate->spatialParentId().has_value() && *candidate->spatialParentId() == id) ||
                (candidate->placementParentId().has_value() && *candidate->placementParentId() == id) ||
                (candidate->decompositionParentId().has_value() && *candidate->decompositionParentId() == id)) {
                children.push_back(candidateId);
            }
        }
        for (const std::string& child : children) {
            if (transforms_.count(child) != 0) {
                deleteTransformRecursive(child, visiting);
            }
        }

        Transform& transform = requireTransform(id);
        std::vector<std::string> geometryIds = transform.geometryIds();
        for (const std::string& geometryId : geometryIds) {
            if (geometries_.count(geometryId) != 0) {
                geometryDelete(geometryId);
            }
        }

        Base* entity = transform.entity();
        Base* placement = transform.objectPlacement();
        Base* representation = getEntityAttribute(entity, "Representation");
        removeMaterialAssignments(entity);
        removeRelationshipsReferencing(entity);
        file_->removeEntity(entity);
        transformByEntity_.erase(entity);
        transforms_.erase(id);
        removePlacementIfUnreferenced(placement);
        removeIfUnreferenced(*file_, representation);
    }

    void removeRelationshipsReferencing(Base* entity) {
        std::vector<Base*> relations;
        for (Base* candidate : aggregateToVector(file_->instances_by_reference(static_cast<int>(entity->id())))) {
            if (candidate != nullptr && isA(candidate, "IfcRelationship")) {
                relations.push_back(candidate);
            }
        }
        std::sort(relations.begin(), relations.end());
        relations.erase(std::unique(relations.begin(), relations.end()), relations.end());
        std::vector<Base*> cleanupCandidates;
        for (Base* relation : relations) {
            bool retained = false;
            for (const std::string& attribute : {"RelatedElements", "RelatedObjects"}) {
                if (hasAttribute(relation, attribute) &&
                    aggregateContains(getEntityAggregate(relation, attribute), entity)) {
                    removeFromAggregate(relation, attribute, entity);
                    const auto remaining = getEntityAggregate(relation, attribute);
                    if (remaining != nullptr && remaining->size() != 0) {
                        retained = true;
                    }
                    break;
                }
            }
            if (retained) {
                continue;
            }
            if (isA(relation, "IfcRelDefinesByProperties")) {
                Base* propertyDefinition = getEntityAttribute(relation, "RelatingPropertyDefinition");
                if (propertyDefinition != nullptr) {
                    cleanupCandidates.push_back(propertyDefinition);
                }
            }
            file_->removeEntity(relation);
        }
        removeUniqueIfUnreferenced(*file_, cleanupCandidates);
    }

    Target resolveTarget(const std::string& targetId) {
        if (targetId.rfind("transform:", 0) == 0) {
            Transform& transform = requireTransform(targetId);
            return Target{transform.entity(), transform.entity()};
        }
        if (targetId.rfind("geometry:", 0) == 0) {
            Geometry& geometry = requireGeometry(targetId);
            return Target{geometry.entity(), geometry.isEditorGeometry() ? asEntity(geometry.entity()) : nullptr};
        }
        throw SceneError("INVALID_ENTITY_TYPE", "Material target must be a transform or geometry", json{{"id", targetId}});
    }

    bool assignmentExists(Material& material, const Target& target) {
        if (target.semanticObject != nullptr) {
            for (Base* relation : instancesByType(*file_, "IfcRelAssociatesMaterial")) {
                if (getEntityAttribute(relation, "RelatingMaterial") == material.entity() &&
                    aggregateContains(getEntityAggregate(relation, "RelatedObjects"), target.semanticObject)) {
                    return true;
                }
            }
        }
        Base* style = material.surfaceStyle(*file_);
        if (style != nullptr) {
            for (Base* styledItem : instancesByType(*file_, "IfcStyledItem")) {
                if (getEntityAttribute(styledItem, "Item") == target.entity && styledItemContainsStyle(styledItem, style)) {
                    return true;
                }
            }
        }
        return false;
    }

    void replaceSemanticAssignmentTarget(Base* previousTarget, Base* replacementTarget) {
        for (Base* relation : instancesByType(*file_, "IfcRelAssociatesMaterial")) {
            if (!aggregateContains(getEntityAggregate(relation, "RelatedObjects"), previousTarget)) {
                continue;
            }
            removeFromAggregate(relation, "RelatedObjects", previousTarget);
            appendUniqueToAggregate(relation, "RelatedObjects", replacementTarget);
        }
    }

    void assignSemanticMaterial(Base* material, Entity* target) {
        for (Base* relation : instancesByType(*file_, "IfcRelAssociatesMaterial")) {
            if (getEntityAttribute(relation, "RelatingMaterial") == material) {
                appendUniqueToAggregate(relation, "RelatedObjects", target);
                return;
            }
        }
        Base* relation = createInstance(*file_, "IfcRelAssociatesMaterial");
        initializeRoot(*file_, relation, std::string());
        setAttribute(relation, "RelatedObjects", makeAggregate({target}));
        setEntityAttribute(relation, "RelatingMaterial", material);
    }

    bool unassignSemanticMaterial(Base* material, Entity* target) {
        bool removed = false;
        std::vector<Base*> emptyRelations;
        for (Base* relation : instancesByType(*file_, "IfcRelAssociatesMaterial")) {
            if (getEntityAttribute(relation, "RelatingMaterial") != material ||
                !aggregateContains(getEntityAggregate(relation, "RelatedObjects"), target)) {
                continue;
            }
            removed = true;
            removeFromAggregate(relation, "RelatedObjects", target);
            const auto remaining = getEntityAggregate(relation, "RelatedObjects");
            if (remaining == nullptr || remaining->size() == 0) {
                emptyRelations.push_back(relation);
            }
        }
        for (Base* relation : emptyRelations) {
            file_->removeEntity(relation);
        }
        return removed;
    }

    void assignVisualMaterial(Material& material, Base* item) {
        Base* style = material.surfaceStyle(*file_);
        if (style == nullptr) {
            throw SceneError("INVALID_RELATION", "Material has no visual style compatible with a representation item", json{{"id", material.id()}});
        }
        createStyledItem(*file_, item, style);
    }

    bool unassignVisualMaterial(Material& material, Base* item) {
        Base* style = material.surfaceStyle(*file_);
        if (style == nullptr) {
            return false;
        }
        bool removed = false;
        std::vector<Base*> styledItems;
        for (Base* styledItem : instancesByType(*file_, "IfcStyledItem")) {
            if (getEntityAttribute(styledItem, "Item") == item && styledItemContainsStyle(styledItem, style)) {
                styledItems.push_back(styledItem);
            }
        }
        for (Base* styledItem : styledItems) {
            file_->removeEntity(styledItem);
            removed = true;
        }
        return removed;
    }

    bool styledItemContainsStyle(Base* styledItem, Base* surfaceStyle) const {
        for (Base* style : aggregateToVector(getEntityAggregate(styledItem, "Styles"))) {
            if (style == surfaceStyle) {
                return true;
            }
            if (isA(style, "IfcPresentationStyleAssignment") &&
                aggregateContains(getEntityAggregate(style, "Styles"), surfaceStyle)) {
                return true;
            }
        }
        return false;
    }

    void removeMaterialAssignments(Base* target) {
        std::vector<Base*> emptyRelations;
        for (Base* relation : instancesByType(*file_, "IfcRelAssociatesMaterial")) {
            if (!aggregateContains(getEntityAggregate(relation, "RelatedObjects"), target)) {
                continue;
            }
            removeFromAggregate(relation, "RelatedObjects", target);
            const auto remaining = getEntityAggregate(relation, "RelatedObjects");
            if (remaining == nullptr || remaining->size() == 0) {
                emptyRelations.push_back(relation);
            }
        }
        for (Base* relation : emptyRelations) {
            file_->removeEntity(relation);
        }
        std::vector<Base*> styledItems;
        for (Base* styledItem : instancesByType(*file_, "IfcStyledItem")) {
            if (getEntityAttribute(styledItem, "Item") == target) {
                styledItems.push_back(styledItem);
            }
        }
        for (Base* styledItem : styledItems) {
            file_->removeEntity(styledItem);
        }
    }

    void removeAllAssignmentsForMaterial(Base* material) {
        std::vector<Base*> semanticRelations;
        for (Base* relation : instancesByType(*file_, "IfcRelAssociatesMaterial")) {
            if (getEntityAttribute(relation, "RelatingMaterial") == material) {
                semanticRelations.push_back(relation);
            }
        }
        for (Base* relation : semanticRelations) {
            file_->removeEntity(relation);
        }

        Base* surfaceStyle = nullptr;
        const auto iterator = materialByEntity_.find(material);
        if (iterator != materialByEntity_.end()) {
            surfaceStyle = materials_.at(iterator->second)->surfaceStyle(*file_);
        }
        if (surfaceStyle == nullptr) {
            return;
        }
        std::vector<Base*> styledItems;
        for (Base* styledItem : instancesByType(*file_, "IfcStyledItem")) {
            if (getEntityAttribute(styledItem, "Item") != nullptr && styledItemContainsStyle(styledItem, surfaceStyle)) {
                styledItems.push_back(styledItem);
            }
        }
        for (Base* styledItem : styledItems) {
            file_->removeEntity(styledItem);
        }
    }

    template <typename T>
    static void emitChunks(const std::string& event, const std::vector<T>& values, const MeshEmitter& emit) {
        constexpr std::size_t chunkSize = 16384;
        for (std::size_t offset = 0; offset < values.size(); offset += chunkSize) {
            const std::size_t end = std::min(values.size(), offset + chunkSize);
            json chunk = json::array();
            for (std::size_t index = offset; index < end; ++index) {
                chunk.push_back(values[index]);
            }
            emit(json{{"event", event}, {"offset", offset}, {"values", std::move(chunk)}});
        }
    }

    std::unique_ptr<IfcParse::IfcFile> file_;
    std::string loadedPath_;
    std::string schema_;
    std::uint64_t transformCounter_ = 0;
    std::uint64_t geometryCounter_ = 0;
    std::uint64_t materialCounter_ = 0;
    std::unordered_map<std::string, std::unique_ptr<Transform>> transforms_;
    std::unordered_map<std::string, std::unique_ptr<Geometry>> geometries_;
    std::unordered_map<std::string, std::unique_ptr<Material>> materials_;
    std::unordered_map<const Base*, std::string> transformByEntity_;
    std::unordered_map<const Base*, std::string> geometryByEntity_;
    std::unordered_map<const Base*, std::string> materialByEntity_;
};

} // namespace ifc_editor