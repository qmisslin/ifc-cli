#pragma once

#include "transform.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace ifc_editor {

inline Base* createGeometryInstance(IfcParse::IfcFile& file, const std::string& className) {
    try {
        return createInstance(file, className);
    } catch (const SceneError& exception) {
        throw SceneError(
            "GEOMETRY_OPERATION_NOT_SUPPORTED",
            "IFC schema does not support geometry entity " + className,
            json{{"ifcClass", className}, {"reason", exception.what()}});
    }
}

using Profile2D = std::vector<std::array<double, 2>>;

inline Profile2D profileFromJson(const json& profile) {
    Profile2D result;
    result.reserve(profile.size());
    for (const auto& point : profile) {
        result.push_back(std::array<double, 2>{point.at(0).get<double>(), point.at(1).get<double>()});
    }
    return result;
}

inline json profileToJson(const Profile2D& profile) {
    json result = json::array();
    for (const auto& point : profile) {
        result.push_back(json::array({point[0], point[1]}));
    }
    return result;
}

inline double orientation2D(
    const std::array<double, 2>& a,
    const std::array<double, 2>& b,
    const std::array<double, 2>& c) {
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
}

inline bool pointOnSegment2D(
    const std::array<double, 2>& point,
    const std::array<double, 2>& start,
    const std::array<double, 2>& end,
    double epsilon = 1.0e-12) {
    return std::abs(orientation2D(start, end, point)) <= epsilon &&
        point[0] >= std::min(start[0], end[0]) - epsilon &&
        point[0] <= std::max(start[0], end[0]) + epsilon &&
        point[1] >= std::min(start[1], end[1]) - epsilon &&
        point[1] <= std::max(start[1], end[1]) + epsilon;
}

inline bool segmentsIntersect2D(
    const std::array<double, 2>& a1,
    const std::array<double, 2>& a2,
    const std::array<double, 2>& b1,
    const std::array<double, 2>& b2) {
    constexpr double epsilon = 1.0e-12;
    const double o1 = orientation2D(a1, a2, b1);
    const double o2 = orientation2D(a1, a2, b2);
    const double o3 = orientation2D(b1, b2, a1);
    const double o4 = orientation2D(b1, b2, a2);

    if (((o1 > epsilon && o2 < -epsilon) || (o1 < -epsilon && o2 > epsilon)) &&
        ((o3 > epsilon && o4 < -epsilon) || (o3 < -epsilon && o4 > epsilon))) {
        return true;
    }
    return (std::abs(o1) <= epsilon && pointOnSegment2D(b1, a1, a2)) ||
        (std::abs(o2) <= epsilon && pointOnSegment2D(b2, a1, a2)) ||
        (std::abs(o3) <= epsilon && pointOnSegment2D(a1, b1, b2)) ||
        (std::abs(o4) <= epsilon && pointOnSegment2D(a2, b1, b2));
}

inline void validateProfile(const Profile2D& profile) {
    if (profile.size() < 3) {
        throw SceneError("INVALID_PROFILE", "Profile must contain at least three points");
    }

    constexpr double epsilon = 1.0e-12;
    std::set<std::pair<double, double>> distinct;
    for (const auto& point : profile) {
        if (!std::isfinite(point[0]) || !std::isfinite(point[1])) {
            throw SceneError("INVALID_PROFILE", "Profile contains a non-finite coordinate");
        }
        distinct.emplace(point[0], point[1]);
    }
    if (distinct.size() < 3) {
        throw SceneError("INVALID_PROFILE", "Profile must contain at least three distinct points");
    }
    if (profile.front() == profile.back()) {
        throw SceneError("INVALID_PROFILE", "The first profile point must not be repeated at the end");
    }

    for (std::size_t index = 0; index < profile.size(); ++index) {
        const auto& start = profile[index];
        const auto& end = profile[(index + 1) % profile.size()];
        const double dx = end[0] - start[0];
        const double dy = end[1] - start[1];
        if (dx * dx + dy * dy <= epsilon * epsilon) {
            throw SceneError("INVALID_PROFILE", "Profile contains a zero-length segment");
        }
    }

    double twiceArea = 0.0;
    for (std::size_t index = 0; index < profile.size(); ++index) {
        const auto& current = profile[index];
        const auto& next = profile[(index + 1) % profile.size()];
        twiceArea += current[0] * next[1] - next[0] * current[1];
    }
    if (std::abs(twiceArea) <= epsilon) {
        throw SceneError("INVALID_PROFILE", "Profile area must be non-zero");
    }

    for (std::size_t first = 0; first < profile.size(); ++first) {
        const std::size_t firstNext = (first + 1) % profile.size();
        for (std::size_t second = first + 1; second < profile.size(); ++second) {
            const std::size_t secondNext = (second + 1) % profile.size();
            if (first == second || firstNext == second || secondNext == first) {
                continue;
            }
            if (first == 0 && secondNext == 0) {
                continue;
            }
            if (segmentsIntersect2D(profile[first], profile[firstNext], profile[second], profile[secondNext])) {
                throw SceneError("INVALID_PROFILE", "Profile contains a self-intersection");
            }
        }
    }
}

inline Base* findRepresentationContext(IfcParse::IfcFile& file) {
    for (Base* context : instancesByType(file, "IfcGeometricRepresentationSubContext")) {
        const std::string identifier = getString(context, "ContextIdentifier");
        if (identifier == "Body") {
            return context;
        }
    }
    const auto subContexts = instancesByType(file, "IfcGeometricRepresentationSubContext");
    if (!subContexts.empty()) {
        return subContexts.front();
    }
    const auto contexts = instancesByType(file, "IfcGeometricRepresentationContext");
    if (!contexts.empty()) {
        return contexts.front();
    }
    throw SceneError("INVALID_RELATION", "The IFC model has no geometric representation context");
}

inline Base* createPolylineProfile(IfcParse::IfcFile& file, const Profile2D& profile) {
    validateProfile(profile);
    std::vector<Base*> points;
    points.reserve(profile.size() + 1);
    for (const auto& point : profile) {
        points.push_back(createCartesianPoint(file, {point[0], point[1]}));
    }
    points.push_back(createCartesianPoint(file, {profile.front()[0], profile.front()[1]}));

    Base* polyline = createGeometryInstance(file, "IfcPolyline");
    setAttribute(polyline, "Points", makeAggregate(points));

    Base* profileDefinition = createGeometryInstance(file, "IfcArbitraryClosedProfileDef");
    setEnumeration(profileDefinition, "ProfileType", "AREA");
    if (hasAttribute(profileDefinition, "ProfileName")) {
        unsetAttribute(profileDefinition, "ProfileName");
    }
    setEntityAttribute(profileDefinition, "OuterCurve", polyline);
    return profileDefinition;
}

inline Base* createExtrudedAreaSolid(IfcParse::IfcFile& file, const Profile2D& profile, double depth) {
    if (!std::isfinite(depth) || depth <= 0.0) {
        throw SceneError("INVALID_PROFILE", "Extrusion depth must be a finite strictly positive number");
    }
    Base* solid = createGeometryInstance(file, "IfcExtrudedAreaSolid");
    setEntityAttribute(solid, "SweptArea", createPolylineProfile(file, profile));
    setEntityAttribute(solid, "Position", createAxis2Placement3D(file, identityMatrix()));
    setEntityAttribute(solid, "ExtrudedDirection", createDirection(file, {0.0, 0.0, 1.0}));
    setAttribute(solid, "Depth", depth);
    return solid;
}

inline Base* createProductDefinitionShape(IfcParse::IfcFile& file, Base* representationItem) {
    Base* shapeRepresentation = createGeometryInstance(file, "IfcShapeRepresentation");
    setEntityAttribute(shapeRepresentation, "ContextOfItems", findRepresentationContext(file));
    if (hasAttribute(shapeRepresentation, "RepresentationIdentifier")) {
        setAttribute(shapeRepresentation, "RepresentationIdentifier", std::string("Body"));
    }
    if (hasAttribute(shapeRepresentation, "RepresentationType")) {
        setAttribute(shapeRepresentation, "RepresentationType", std::string("SweptSolid"));
    }
    setAttribute(shapeRepresentation, "Items", makeAggregate({representationItem}));

    Base* productDefinitionShape = createGeometryInstance(file, "IfcProductDefinitionShape");
    if (hasAttribute(productDefinitionShape, "Name")) {
        unsetAttribute(productDefinitionShape, "Name");
    }
    if (hasAttribute(productDefinitionShape, "Description")) {
        unsetAttribute(productDefinitionShape, "Description");
    }
    setAttribute(productDefinitionShape, "Representations", makeAggregate({shapeRepresentation}));
    return productDefinitionShape;
}

inline bool canReceiveOperation(const Base* parent, const std::string& operation) {
    if (parent == nullptr || !isA(parent, "IfcElement")) {
        return false;
    }
    if (operation == "SUBTRACT") {
        return true;
    }
    if (operation == "ADD") {
        return true;
    }
    return false;
}

inline Base* createOperationRelation(
    IfcParse::IfcFile& file,
    Base* parent,
    Base* feature,
    const std::string& operation) {
    if (!canReceiveOperation(parent, operation)) {
        throw SceneError(
            "GEOMETRY_OPERATION_NOT_SUPPORTED",
            "Target transform cannot receive the requested geometry operation",
            json{{"operation", operation}, {"ifcClass", parent == nullptr ? json(nullptr) : json(parent->declaration().name())}});
    }

    if (operation == "SUBTRACT") {
        Base* relation = createGeometryInstance(file, "IfcRelVoidsElement");
        initializeRoot(file, relation, std::string());
        setEntityAttribute(relation, "RelatingBuildingElement", parent);
        setEntityAttribute(relation, "RelatedOpeningElement", feature);
        return relation;
    }

    if (operation == "ADD") {
        Base* relation = createGeometryInstance(file, "IfcRelProjectsElement");
        initializeRoot(file, relation, std::string());
        setEntityAttribute(relation, "RelatingElement", parent);
        setEntityAttribute(relation, "RelatedFeatureElement", feature);
        return relation;
    }

    throw SceneError("INVALID_PARAMETERS", "Geometry operation must be ADD or SUBTRACT");
}

inline Base* createFeatureElement(
    IfcParse::IfcFile& file,
    Base* parent,
    const std::string& name,
    const Matrix4& localMatrix,
    const Profile2D& profile,
    double depth,
    const std::string& operation) {
    const std::string className = operation == "SUBTRACT" ? "IfcOpeningElement" :
        (operation == "ADD" ? "IfcProjectionElement" : std::string());
    if (className.empty()) {
        throw SceneError("INVALID_PARAMETERS", "Geometry operation must be ADD or SUBTRACT");
    }

    Base* feature = createGeometryInstance(file, className);
    initializeRoot(file, feature, name);
    if (hasAttribute(feature, "ObjectType")) {
        unsetAttribute(feature, "ObjectType");
    }
    Base* parentPlacement = getEntityAttribute(parent, "ObjectPlacement");
    setEntityAttribute(feature, "ObjectPlacement", createLocalPlacement(file, parentPlacement, localMatrix));
    Base* solid = createExtrudedAreaSolid(file, profile, depth);
    setEntityAttribute(feature, "Representation", createProductDefinitionShape(file, solid));
    if (hasAttribute(feature, "Tag")) {
        unsetAttribute(feature, "Tag");
    }
    if (hasAttribute(feature, "PredefinedType")) {
        try {
            setEnumeration(feature, "PredefinedType", operation == "SUBTRACT" ? "OPENING" : "USERDEFINED");
        } catch (const std::exception&) {
            try {
                setEnumeration(feature, "PredefinedType", "NOTDEFINED");
            } catch (const std::exception&) {
                unsetAttribute(feature, "PredefinedType");
            }
        }
    }
    createOperationRelation(file, parent, feature, operation);
    return feature;
}

inline Base* findExtrudedSolid(Base* feature) {
    Base* representation = getEntityAttribute(feature, "Representation");
    if (representation == nullptr || !hasAttribute(representation, "Representations")) {
        return nullptr;
    }
    for (Base* shapeRepresentation : aggregateToVector(getEntityAggregate(representation, "Representations"))) {
        if (!hasAttribute(shapeRepresentation, "Items")) {
            continue;
        }
        for (Base* item : aggregateToVector(getEntityAggregate(shapeRepresentation, "Items"))) {
            if (isA(item, "IfcExtrudedAreaSolid")) {
                return item;
            }
        }
    }
    return nullptr;
}

inline Base* findOuterPolyline(Base* solid) {
    Base* sweptArea = getEntityAttribute(solid, "SweptArea");
    Base* outerCurve = getEntityAttribute(sweptArea, "OuterCurve");
    return outerCurve != nullptr && isA(outerCurve, "IfcPolyline") ? outerCurve : nullptr;
}

inline Profile2D readProfile(Base* feature) {
    Base* solid = findExtrudedSolid(feature);
    Base* polyline = findOuterPolyline(solid);
    if (polyline == nullptr) {
        return {};
    }
    Profile2D profile;
    for (Base* point : aggregateToVector(getEntityAggregate(polyline, "Points"))) {
        const auto coordinates = getCoordinates(point);
        if (coordinates.size() >= 2) {
            profile.push_back(std::array<double, 2>{coordinates[0], coordinates[1]});
        }
    }
    if (profile.size() >= 2 && profile.front() == profile.back()) {
        profile.pop_back();
    }
    return profile;
}

inline double readDepth(Base* feature) {
    Base* solid = findExtrudedSolid(feature);
    return solid == nullptr ? 0.0 : getOptionalDouble(solid, "Depth").value_or(0.0);
}

inline void replaceProfile(IfcParse::IfcFile& file, Base* feature, const Profile2D& profile) {
    validateProfile(profile);
    Base* solid = findExtrudedSolid(feature);
    if (solid == nullptr) {
        throw SceneError("GEOMETRY_NOT_EDITABLE", "Geometry has no editable IfcExtrudedAreaSolid");
    }
    Base* previousProfile = getEntityAttribute(solid, "SweptArea");
    setEntityAttribute(solid, "SweptArea", createPolylineProfile(file, profile));
    removeIfUnreferenced(file, previousProfile);
}

inline void replaceDepth(Base* feature, double depth) {
    if (!std::isfinite(depth) || depth <= 0.0) {
        throw SceneError("INVALID_PROFILE", "Extrusion depth must be a finite strictly positive number");
    }
    Base* solid = findExtrudedSolid(feature);
    if (solid == nullptr) {
        throw SceneError("GEOMETRY_NOT_EDITABLE", "Geometry has no editable IfcExtrudedAreaSolid");
    }
    setAttribute(solid, "Depth", depth);
}

inline std::string operationForFeature(const Base* feature) {
    if (isA(feature, "IfcOpeningElement")) {
        return "SUBTRACT";
    }
    if (isA(feature, "IfcProjectionElement")) {
        return "ADD";
    }
    return "BASE";
}

inline Base* findOperationRelation(IfcParse::IfcFile& file, Base* feature, const std::string& operation) {
    if (operation == "SUBTRACT") {
        for (Base* relation : instancesByType(file, "IfcRelVoidsElement")) {
            if (getEntityAttribute(relation, "RelatedOpeningElement") == feature) {
                return relation;
            }
        }
    } else if (operation == "ADD") {
        for (Base* relation : instancesByType(file, "IfcRelProjectsElement")) {
            if (getEntityAttribute(relation, "RelatedFeatureElement") == feature) {
                return relation;
            }
        }
    }
    return nullptr;
}

inline std::vector<Base*> collectPlacementOwnedEntities(Base* objectPlacement) {
    std::vector<Base*> result;
    if (objectPlacement == nullptr || !isA(objectPlacement, "IfcLocalPlacement")) {
        return result;
    }
    Base* relativePlacement = getEntityAttribute(objectPlacement, "RelativePlacement");
    if (relativePlacement != nullptr) {
        Base* location = getEntityAttribute(relativePlacement, "Location");
        Base* axis = getEntityAttribute(relativePlacement, "Axis");
        Base* refDirection = getEntityAttribute(relativePlacement, "RefDirection");
        if (location != nullptr) {
            result.push_back(location);
        }
        if (axis != nullptr) {
            result.push_back(axis);
        }
        if (refDirection != nullptr) {
            result.push_back(refDirection);
        }
        result.push_back(relativePlacement);
    }
    result.push_back(objectPlacement);
    return result;
}

class Geometry {
public:
    enum class Source {
        Ifc,
        Editor
    };

    Geometry(
        std::string id,
        Base* entity,
        Entity* parent,
        std::string parentId,
        Source source,
        bool editable,
        std::string operation)
        : id_(std::move(id)),
          entity_(entity),
          parent_(parent),
          parentId_(std::move(parentId)),
          source_(source),
          editable_(editable),
          operation_(std::move(operation)) {
        if (entity_ == nullptr || parent_ == nullptr) {
            throw SceneError("INTERNAL_ERROR", "Geometry cannot wrap null IFC entities");
        }
    }

    static std::unique_ptr<Geometry> createNative(
        std::string id,
        Base* representationItem,
        Entity* parent,
        std::string parentId) {
        return std::unique_ptr<Geometry>(new Geometry(
            std::move(id),
            representationItem,
            parent,
            std::move(parentId),
            Source::Ifc,
            false,
            "BASE"));
    }

    static std::unique_ptr<Geometry> createEditor(
        std::string id,
        Entity* feature,
        Entity* parent,
        std::string parentId) {
        return std::unique_ptr<Geometry>(new Geometry(
            std::move(id),
            feature,
            parent,
            std::move(parentId),
            Source::Editor,
            true,
            operationForFeature(feature)));
    }

    const std::string& id() const noexcept {
        return id_;
    }

    Base* entity() const noexcept {
        return entity_;
    }

    Entity* parent() const noexcept {
        return parent_;
    }

    const std::string& parentId() const noexcept {
        return parentId_;
    }

    bool editable() const noexcept {
        return editable_;
    }

    bool isEditorGeometry() const noexcept {
        return source_ == Source::Editor;
    }

    const std::string& operation() const noexcept {
        return operation_;
    }

    std::string name() const {
        return source_ == Source::Editor ? getString(entity_, "Name") : std::string();
    }

    std::optional<std::string> globalId() const {
        return globalIdOf(entity_);
    }

    Base* objectPlacement() const {
        return source_ == Source::Editor ? getEntityAttribute(entity_, "ObjectPlacement") : nullptr;
    }

    Matrix4 localMatrix() const {
        return source_ == Source::Editor ? localPlacementMatrix(objectPlacement()) : identityMatrix();
    }

    Matrix4 worldMatrix() const {
        return source_ == Source::Editor ? worldPlacementMatrix(objectPlacement()) : worldPlacementMatrix(getEntityAttribute(parent_, "ObjectPlacement"));
    }

    Profile2D profile() const {
        return source_ == Source::Editor ? readProfile(entity_) : Profile2D();
    }

    double depth() const {
        return source_ == Source::Editor ? readDepth(entity_) : 0.0;
    }

    void setMaterialIds(std::vector<std::string> ids) {
        materialIds_ = std::move(ids);
    }

    const std::vector<std::string>& materialIds() const noexcept {
        return materialIds_;
    }

    void setName(const std::string& name) {
        requireEditable();
        setAttribute(entity_, "Name", name);
    }

    Base* replacePlacement(IfcParse::IfcFile& file, const Matrix4& localMatrix) {
        requireEditable();
        Base* previous = objectPlacement();
        Base* parentPlacement = getEntityAttribute(parent_, "ObjectPlacement");
        setEntityAttribute(entity_, "ObjectPlacement", createLocalPlacement(file, parentPlacement, localMatrix));
        return previous;
    }

    void setProfile(IfcParse::IfcFile& file, const Profile2D& profileValue) {
        requireEditable();
        replaceProfile(file, entity_, profileValue);
    }

    void setDepth(double depthValue) {
        requireEditable();
        replaceDepth(entity_, depthValue);
    }

    std::vector<Base*> convertOperation(IfcParse::IfcFile& file, const std::string& nextOperation) {
        requireEditable();
        if (nextOperation != "ADD" && nextOperation != "SUBTRACT") {
            throw SceneError("INVALID_PARAMETERS", "Geometry operation must be ADD or SUBTRACT");
        }
        if (nextOperation == operation_) {
            return {};
        }

        Base* oldFeature = entity_;
        Base* oldRelation = findOperationRelation(file, oldFeature, operation_);
        Base* oldPlacement = getEntityAttribute(oldFeature, "ObjectPlacement");
        Base* representation = getEntityAttribute(oldFeature, "Representation");
        const std::string featureName = getString(oldFeature, "Name");

        const std::string className = nextOperation == "SUBTRACT" ? "IfcOpeningElement" : "IfcProjectionElement";
        Base* replacement = createGeometryInstance(file, className);
        initializeRoot(file, replacement, featureName);
        if (hasAttribute(replacement, "ObjectType")) {
            unsetAttribute(replacement, "ObjectType");
        }
        setEntityAttribute(replacement, "ObjectPlacement", oldPlacement);
        setEntityAttribute(replacement, "Representation", representation);
        if (hasAttribute(replacement, "Tag")) {
            unsetAttribute(replacement, "Tag");
        }
        if (hasAttribute(replacement, "PredefinedType")) {
            try {
                setEnumeration(replacement, "PredefinedType", nextOperation == "SUBTRACT" ? "OPENING" : "USERDEFINED");
            } catch (const std::exception&) {
                try {
                    setEnumeration(replacement, "PredefinedType", "NOTDEFINED");
                } catch (const std::exception&) {
                    unsetAttribute(replacement, "PredefinedType");
                }
            }
        }
        createOperationRelation(file, parent_, replacement, nextOperation);

        entity_ = replacement;
        operation_ = nextOperation;
        return std::vector<Base*>{oldRelation, oldFeature};
    }

    json toJson(const EntityResolver& resolver = EntityResolver()) const {
        const auto global = globalId();
        json output{
            {"id", id_},
            {"ifcEntityId", entity_->id()},
            {"globalId", global.has_value() ? json(*global) : json(nullptr)},
            {"source", source_ == Source::Editor ? "editor" : "ifc"},
            {"editable", editable_},
            {"ifcClass", entity_->declaration().name()},
            {"operation", operation_},
            {"parentId", parentId_}};

        if (source_ == Source::Editor) {
            output["name"] = name();
            output["transforms"] = json{
                {"local", matrixToJson(localMatrix())},
                {"world", matrixToJson(worldMatrix())},
                {"editable", true},
                {"sourceType", "IfcLocalPlacement"}};
            output["profile"] = profileToJson(profile());
            output["depth"] = depth();
            output["materials"] = materialIds_;
            output["definition"] = buildDefinition(
                entity_,
                {"GlobalId", "OwnerHistory", "Name", "Description", "ObjectType", "ObjectPlacement", "Representation", "Tag", "PredefinedType"},
                resolver);
        } else {
            output["definition"] = buildDefinition(entity_, std::set<std::string>(), resolver);
        }
        return output;
    }

    void deleteFromIfc(IfcParse::IfcFile& file) {
        if (source_ == Source::Ifc) {
            deleteNativeFromIfc(file);
            return;
        }
        deleteEditorFromIfc(file);
    }

private:
    void requireEditable() const {
        if (!editable_ || source_ != Source::Editor) {
            throw SceneError("GEOMETRY_NOT_EDITABLE", "Geometry is not editable", json{{"id", id_}});
        }
    }

    void deleteNativeFromIfc(IfcParse::IfcFile& file) {
        std::vector<Base*> emptyShapeRepresentations;
        for (Base* shapeRepresentation : instancesByType(file, "IfcShapeRepresentation")) {
            if (!hasAttribute(shapeRepresentation, "Items")) {
                continue;
            }
            const auto currentItems = aggregateToVector(getEntityAggregate(shapeRepresentation, "Items"));
            if (std::find(currentItems.begin(), currentItems.end(), entity_) == currentItems.end()) {
                continue;
            }
            std::vector<Base*> nextItems;
            for (Base* item : currentItems) {
                if (item != entity_) {
                    nextItems.push_back(item);
                }
            }
            if (nextItems.empty()) {
                emptyShapeRepresentations.push_back(shapeRepresentation);
            } else {
                setAttribute(shapeRepresentation, "Items", makeAggregate(nextItems));
            }
        }

        file.removeEntity(entity_);
        for (Base* shapeRepresentation : emptyShapeRepresentations) {
            for (Base* productShape : instancesByType(file, "IfcProductDefinitionShape")) {
                if (!hasAttribute(productShape, "Representations")) {
                    continue;
                }
                removeFromAggregate(productShape, "Representations", shapeRepresentation);
                const auto remainingRepresentations = getEntityAggregate(productShape, "Representations");
                if (remainingRepresentations == nullptr || remainingRepresentations->size() == 0) {
                    file.removeEntity(productShape);
                }
            }
            file.removeEntity(shapeRepresentation);
        }
    }

    void deleteEditorFromIfc(IfcParse::IfcFile& file) {
        Base* relation = findOperationRelation(file, entity_, operation_);
        Base* objectPlacement = getEntityAttribute(entity_, "ObjectPlacement");
        Base* productShape = getEntityAttribute(entity_, "Representation");
        Base* solid = findExtrudedSolid(entity_);
        Base* sweptArea = getEntityAttribute(solid, "SweptArea");
        Base* outerCurve = getEntityAttribute(sweptArea, "OuterCurve");
        std::vector<Base*> curvePoints = aggregateToVector(getEntityAggregate(outerCurve, "Points"));
        Base* solidPosition = getEntityAttribute(solid, "Position");
        Base* solidDirection = getEntityAttribute(solid, "ExtrudedDirection");
        std::vector<Base*> representations = aggregateToVector(getEntityAggregate(productShape, "Representations"));
        std::vector<Base*> placementEntities = collectPlacementOwnedEntities(objectPlacement);

        if (relation != nullptr) {
            file.removeEntity(relation);
        }
        file.removeEntity(entity_);
        for (Base* representation : representations) {
            removeIfUnreferenced(file, representation);
        }
        removeIfUnreferenced(file, productShape);
        removeIfUnreferenced(file, solid);
        removeIfUnreferenced(file, sweptArea);
        removeIfUnreferenced(file, outerCurve);
        for (Base* point : curvePoints) {
            removeIfUnreferenced(file, point);
        }
        removeIfUnreferenced(file, solidPosition);
        removeIfUnreferenced(file, solidDirection);
        for (Base* placementEntity : placementEntities) {
            removeIfUnreferenced(file, placementEntity);
        }
    }

    std::string id_;
    Base* entity_;
    Entity* parent_;
    std::string parentId_;
    Source source_;
    bool editable_;
    std::string operation_;
    std::vector<std::string> materialIds_;
};

} // namespace ifc_editor