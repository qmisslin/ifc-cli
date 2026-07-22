#pragma once

#include <ifcparse/IfcBaseClass.h>
#include <ifcparse/IfcFile.h>
#include <ifcparse/IfcGlobalId.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <boost/logic/tribool.hpp>
#include <boost/shared_ptr.hpp>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace ifc_editor {

using json = nlohmann::json;
using Matrix4 = std::array<double, 16>;
using Entity = IfcUtil::IfcBaseEntity;
using Base = IfcUtil::IfcBaseClass;
using EntityResolver = std::function<json(const Base*)>;

class SceneError final : public std::runtime_error {
public:
    SceneError(std::string code, std::string message, json details = json::object())
        : std::runtime_error(message), code_(std::move(code)), details_(std::move(details)) {}

    const std::string& code() const noexcept {
        return code_;
    }

    const json& details() const noexcept {
        return details_;
    }

private:
    std::string code_;
    json details_;
};

inline Matrix4 identityMatrix() {
    return Matrix4{
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0};
}

inline json matrixToJson(const Matrix4& matrix) {
    return json::array({
        matrix[0], matrix[1], matrix[2], matrix[3],
        matrix[4], matrix[5], matrix[6], matrix[7],
        matrix[8], matrix[9], matrix[10], matrix[11],
        matrix[12], matrix[13], matrix[14], matrix[15]});
}

inline Matrix4 matrixFromJson(const json& value) {
    Matrix4 matrix{};
    for (std::size_t i = 0; i < matrix.size(); ++i) {
        matrix[i] = value.at(i).get<double>();
    }
    return matrix;
}

inline Matrix4 multiplyMatrix(const Matrix4& lhs, const Matrix4& rhs) {
    Matrix4 result{};
    for (std::size_t row = 0; row < 4; ++row) {
        for (std::size_t col = 0; col < 4; ++col) {
            double value = 0.0;
            for (std::size_t k = 0; k < 4; ++k) {
                value += lhs[row * 4 + k] * rhs[k * 4 + col];
            }
            result[row * 4 + col] = value;
        }
    }
    return result;
}

inline double dot3(const std::array<double, 3>& lhs, const std::array<double, 3>& rhs) {
    return lhs[0] * rhs[0] + lhs[1] * rhs[1] + lhs[2] * rhs[2];
}

inline std::array<double, 3> cross3(const std::array<double, 3>& lhs, const std::array<double, 3>& rhs) {
    return std::array<double, 3>{
        lhs[1] * rhs[2] - lhs[2] * rhs[1],
        lhs[2] * rhs[0] - lhs[0] * rhs[2],
        lhs[0] * rhs[1] - lhs[1] * rhs[0]};
}

inline double norm3(const std::array<double, 3>& value) {
    return std::sqrt(dot3(value, value));
}

inline std::array<double, 3> normalize3(const std::array<double, 3>& value) {
    const double length = norm3(value);
    if (!std::isfinite(length) || length <= 1.0e-12) {
        throw SceneError("INVALID_PLACEMENT", "Placement contains a null direction");
    }
    return std::array<double, 3>{value[0] / length, value[1] / length, value[2] / length};
}

inline void validateRigidMatrix(const Matrix4& matrix) {
    for (double value : matrix) {
        if (!std::isfinite(value)) {
            throw SceneError("INVALID_PLACEMENT", "Placement matrix contains a non-finite number");
        }
    }

    constexpr double epsilon = 1.0e-8;
    if (std::abs(matrix[12]) > epsilon || std::abs(matrix[13]) > epsilon ||
        std::abs(matrix[14]) > epsilon || std::abs(matrix[15] - 1.0) > epsilon) {
        throw SceneError("INVALID_PLACEMENT", "Placement matrix must be affine with last row [0,0,0,1]");
    }

    const std::array<double, 3> x{matrix[0], matrix[4], matrix[8]};
    const std::array<double, 3> y{matrix[1], matrix[5], matrix[9]};
    const std::array<double, 3> z{matrix[2], matrix[6], matrix[10]};
    if (std::abs(norm3(x) - 1.0) > epsilon || std::abs(norm3(y) - 1.0) > epsilon ||
        std::abs(norm3(z) - 1.0) > epsilon || std::abs(dot3(x, y)) > epsilon ||
        std::abs(dot3(x, z)) > epsilon || std::abs(dot3(y, z)) > epsilon) {
        throw SceneError("INVALID_PLACEMENT", "Placement matrix must contain an orthonormal basis without scale or shear");
    }

    const auto expectedZ = cross3(x, y);
    if (norm3(std::array<double, 3>{expectedZ[0] - z[0], expectedZ[1] - z[1], expectedZ[2] - z[2]}) > epsilon) {
        throw SceneError("INVALID_PLACEMENT", "Placement matrix basis must be right-handed");
    }
}

inline Matrix4 inverseRigidMatrix(const Matrix4& matrix) {
    validateRigidMatrix(matrix);
    Matrix4 inverse = identityMatrix();
    inverse[0] = matrix[0];
    inverse[1] = matrix[4];
    inverse[2] = matrix[8];
    inverse[4] = matrix[1];
    inverse[5] = matrix[5];
    inverse[6] = matrix[9];
    inverse[8] = matrix[2];
    inverse[9] = matrix[6];
    inverse[10] = matrix[10];

    const std::array<double, 3> translation{matrix[3], matrix[7], matrix[11]};
    inverse[3] = -(inverse[0] * translation[0] + inverse[1] * translation[1] + inverse[2] * translation[2]);
    inverse[7] = -(inverse[4] * translation[0] + inverse[5] * translation[1] + inverse[6] * translation[2]);
    inverse[11] = -(inverse[8] * translation[0] + inverse[9] * translation[1] + inverse[10] * translation[2]);
    return inverse;
}

inline std::array<double, 3> transformPoint(const Matrix4& matrix, const std::array<double, 3>& point) {
    return std::array<double, 3>{
        matrix[0] * point[0] + matrix[1] * point[1] + matrix[2] * point[2] + matrix[3],
        matrix[4] * point[0] + matrix[5] * point[1] + matrix[6] * point[2] + matrix[7],
        matrix[8] * point[0] + matrix[9] * point[1] + matrix[10] * point[2] + matrix[11]};
}

inline std::array<double, 3> transformDirection(const Matrix4& matrix, const std::array<double, 3>& direction) {
    return normalize3(std::array<double, 3>{
        matrix[0] * direction[0] + matrix[1] * direction[1] + matrix[2] * direction[2],
        matrix[4] * direction[0] + matrix[5] * direction[1] + matrix[6] * direction[2],
        matrix[8] * direction[0] + matrix[9] * direction[1] + matrix[10] * direction[2]});
}

inline Entity* asEntity(Base* value) {
    return value == nullptr ? nullptr : value->as<Entity>();
}

inline const Entity* asEntity(const Base* value) {
    return value == nullptr ? nullptr : value->as<Entity>();
}

inline bool isA(const Base* value, const std::string& className) {
    return value != nullptr && value->declaration().is(className);
}

inline bool hasAttribute(const Base* value, const std::string& name) {
    if (value == nullptr) {
        return false;
    }
    const auto* declaration = value->declaration().as_entity();
    return declaration != nullptr && declaration->attribute_index(name) >= 0;
}

inline std::size_t attributeIndex(const Base* value, const std::string& name) {
    const auto* declaration = value == nullptr ? nullptr : value->declaration().as_entity();
    if (declaration == nullptr) {
        throw SceneError("INVALID_ENTITY_TYPE", "IFC instance is not an entity");
    }
    const ptrdiff_t index = declaration->attribute_index(name);
    if (index < 0) {
        throw SceneError(
            "INVALID_ENTITY_TYPE",
            "IFC entity does not expose attribute " + name,
            json{{"ifcClass", value->declaration().name()}, {"attribute", name}});
    }
    return static_cast<std::size_t>(index);
}

inline AttributeValue getAttribute(const Base* value, const std::string& name) {
    return value->get_attribute_value(attributeIndex(value, name));
}

inline Base* getEntityAttribute(const Base* value, const std::string& name) {
    if (!hasAttribute(value, name)) {
        return nullptr;
    }
    const AttributeValue attribute = getAttribute(value, name);
    return attribute.isNull() ? nullptr : static_cast<Base*>(attribute);
}

inline boost::shared_ptr<aggregate_of_instance> getEntityAggregate(const Base* value, const std::string& name) {
    if (!hasAttribute(value, name)) {
        return boost::shared_ptr<aggregate_of_instance>(new aggregate_of_instance);
    }
    const AttributeValue attribute = getAttribute(value, name);
    if (attribute.isNull()) {
        return boost::shared_ptr<aggregate_of_instance>(new aggregate_of_instance);
    }
    return static_cast<boost::shared_ptr<aggregate_of_instance>>(attribute);
}

inline std::optional<std::string> getOptionalString(const Base* value, const std::string& name) {
    if (!hasAttribute(value, name)) {
        return std::nullopt;
    }
    const AttributeValue attribute = getAttribute(value, name);
    if (attribute.isNull()) {
        return std::nullopt;
    }
    return static_cast<std::string>(attribute);
}

inline std::string getString(const Base* value, const std::string& name, const std::string& fallback = std::string()) {
    const auto result = getOptionalString(value, name);
    return result.has_value() ? *result : fallback;
}

inline std::optional<double> getOptionalDouble(const Base* value, const std::string& name) {
    if (!hasAttribute(value, name)) {
        return std::nullopt;
    }
    const AttributeValue attribute = getAttribute(value, name);
    if (attribute.isNull()) {
        return std::nullopt;
    }
    return static_cast<double>(attribute);
}

template <typename T>
inline void setAttribute(Base* value, const std::string& name, const T& data) {
    value->set_attribute_value(attributeIndex(value, name), data);
}

inline void setEntityAttribute(Base* value, const std::string& name, Base* data) {
    value->set_attribute_value(attributeIndex(value, name), data);
}

inline void unsetAttribute(Base* value, const std::string& name) {
    if (hasAttribute(value, name)) {
        value->unset_attribute_value(attributeIndex(value, name));
    }
}

inline const IfcParse::enumeration_type* enumerationForAttribute(const Base* value, const std::string& name) {
    const auto* declaration = value->declaration().as_entity();
    const std::size_t index = attributeIndex(value, name);
    const auto* attribute = declaration->attribute_by_index(index);
    const IfcParse::parameter_type* parameter = attribute->type_of_attribute();
    const auto* named = parameter == nullptr ? nullptr : parameter->as_named_type();
    return named == nullptr || named->declared_type() == nullptr ? nullptr : named->declared_type()->as_enumeration_type();
}

inline void setEnumeration(Base* value, const std::string& name, const std::string& enumerationValue) {
    const auto* enumeration = enumerationForAttribute(value, name);
    if (enumeration == nullptr) {
        throw SceneError(
            "INVALID_ENTITY_TYPE",
            "IFC attribute is not an enumeration",
            json{{"ifcClass", value->declaration().name()}, {"attribute", name}});
    }
    const std::size_t offset = enumeration->lookup_enum_offset(enumerationValue);
    setAttribute(value, name, EnumerationReference(enumeration, offset));
}

inline Base* createInstance(IfcParse::IfcFile& file, const std::string& className) {
    try {
        const auto* declaration = file.schema()->declaration_by_name(className);
        if (declaration == nullptr) {
            throw std::runtime_error("Schema declaration not found");
        }
        Base* instance = file.create(declaration);
        if (instance == nullptr) {
            throw SceneError("INTERNAL_ERROR", "IfcOpenShell failed to instantiate " + className);
        }
        return instance;
    } catch (const SceneError&) {
        throw;
    } catch (const std::exception& exception) {
        throw SceneError(
            "INVALID_RELATION",
            "IFC schema does not support " + className,
            json{{"ifcClass", className}, {"reason", exception.what()}});
    }
}

inline boost::shared_ptr<aggregate_of_instance> makeAggregate(const std::vector<Base*>& values) {
    boost::shared_ptr<aggregate_of_instance> aggregate(new aggregate_of_instance);
    for (Base* value : values) {
        aggregate->push(value);
    }
    return aggregate;
}

inline std::vector<Base*> aggregateToVector(const boost::shared_ptr<aggregate_of_instance>& aggregate) {
    std::vector<Base*> values;
    if (!aggregate) {
        return values;
    }
    values.reserve(aggregate->size());
    for (auto iterator = aggregate->begin(); iterator != aggregate->end(); ++iterator) {
        values.push_back(*iterator);
    }
    return values;
}

inline bool aggregateContains(const boost::shared_ptr<aggregate_of_instance>& aggregate, const Base* value) {
    if (!aggregate) {
        return false;
    }
    for (auto iterator = aggregate->begin(); iterator != aggregate->end(); ++iterator) {
        if (*iterator == value) {
            return true;
        }
    }
    return false;
}

inline bool removeFromAggregate(Base* owner, const std::string& attributeName, const Base* value) {
    const auto current = getEntityAggregate(owner, attributeName);
    std::vector<Base*> next;
    bool removed = false;
    for (Base* item : aggregateToVector(current)) {
        if (item == value) {
            removed = true;
        } else {
            next.push_back(item);
        }
    }
    if (removed) {
        setAttribute(owner, attributeName, makeAggregate(next));
    }
    return removed;
}

inline void appendUniqueToAggregate(Base* owner, const std::string& attributeName, Base* value) {
    auto values = aggregateToVector(getEntityAggregate(owner, attributeName));
    if (std::find(values.begin(), values.end(), value) == values.end()) {
        values.push_back(value);
        setAttribute(owner, attributeName, makeAggregate(values));
    }
}

inline std::vector<Base*> instancesByType(IfcParse::IfcFile& file, const std::string& className) {
    std::vector<Base*> result;
    try {
        const auto instances = file.instances_by_type(className);
        if (!instances) {
            return result;
        }
        result.reserve(instances->size());
        for (auto iterator = instances->begin(); iterator != instances->end(); ++iterator) {
            result.push_back(*iterator);
        }
    } catch (const std::exception&) {
    }
    return result;
}

inline Base* firstByType(IfcParse::IfcFile& file, const std::string& className) {
    const auto values = instancesByType(file, className);
    return values.empty() ? nullptr : values.front();
}

inline Base* ownerHistoryForNewRoot(IfcParse::IfcFile& file) {
    return firstByType(file, "IfcOwnerHistory");
}

inline void initializeRoot(IfcParse::IfcFile& file, Base* root, const std::string& name) {
    if (hasAttribute(root, "GlobalId")) {
        IfcParse::IfcGlobalId globalId;
        setAttribute(root, "GlobalId", static_cast<const std::string&>(globalId));
    }
    if (hasAttribute(root, "OwnerHistory")) {
        Base* ownerHistory = ownerHistoryForNewRoot(file);
        if (ownerHistory != nullptr) {
            setEntityAttribute(root, "OwnerHistory", ownerHistory);
        } else {
            const auto* declaration = root->declaration().as_entity();
            const auto* attribute = declaration->attribute_by_index(attributeIndex(root, "OwnerHistory"));
            if (!attribute->optional()) {
                throw SceneError(
                    "INVALID_RELATION",
                    "The loaded IFC model has no IfcOwnerHistory required by its schema");
            }
            unsetAttribute(root, "OwnerHistory");
        }
    }
    if (hasAttribute(root, "Name")) {
        setAttribute(root, "Name", name);
    }
    if (hasAttribute(root, "Description")) {
        unsetAttribute(root, "Description");
    }
}

inline json defaultReference(const Base* value) {
    if (value == nullptr) {
        return nullptr;
    }
    json reference{
        {"ifcEntityId", value->id()},
        {"ifcClass", value->declaration().name()}};
    if (hasAttribute(value, "GlobalId")) {
        const auto globalId = getOptionalString(value, "GlobalId");
        reference["globalId"] = globalId.has_value() ? json(*globalId) : json(nullptr);
    }
    return reference;
}

inline json attributeValueToJson(const AttributeValue& value, const EntityResolver& resolver) {
    if (value.isNull()) {
        return nullptr;
    }

    switch (value.type()) {
        case IfcUtil::Argument_INT:
            return static_cast<int>(value);
        case IfcUtil::Argument_BOOL:
            return static_cast<bool>(value);
        case IfcUtil::Argument_LOGICAL: {
            const boost::logic::tribool logical = value;
            return boost::logic::indeterminate(logical) ? json(nullptr) : json(static_cast<bool>(logical));
        }
        case IfcUtil::Argument_DOUBLE:
            return static_cast<double>(value);
        case IfcUtil::Argument_STRING:
            return static_cast<std::string>(value);
        case IfcUtil::Argument_ENUMERATION:
            return std::string(static_cast<EnumerationReference>(value).value());
        case IfcUtil::Argument_ENTITY_INSTANCE: {
            Base* entity = static_cast<Base*>(value);
            return resolver ? resolver(entity) : defaultReference(entity);
        }
        case IfcUtil::Argument_AGGREGATE_OF_INT:
            return static_cast<std::vector<int>>(value);
        case IfcUtil::Argument_AGGREGATE_OF_DOUBLE:
            return static_cast<std::vector<double>>(value);
        case IfcUtil::Argument_AGGREGATE_OF_STRING:
            return static_cast<std::vector<std::string>>(value);
        case IfcUtil::Argument_AGGREGATE_OF_ENTITY_INSTANCE: {
            json output = json::array();
            const auto aggregate = static_cast<boost::shared_ptr<aggregate_of_instance>>(value);
            for (Base* entity : aggregateToVector(aggregate)) {
                output.push_back(resolver ? resolver(entity) : defaultReference(entity));
            }
            return output;
        }
        case IfcUtil::Argument_AGGREGATE_OF_AGGREGATE_OF_INT:
            return static_cast<std::vector<std::vector<int>>>(value);
        case IfcUtil::Argument_AGGREGATE_OF_AGGREGATE_OF_DOUBLE:
            return static_cast<std::vector<std::vector<double>>>(value);
        default:
            return nullptr;
    }
}

inline json buildDefinition(
    const Base* entity,
    const std::set<std::string>& excludedAttributes,
    const EntityResolver& resolver = EntityResolver()) {
    json attributes = json::object();
    json references = json::object();
    if (entity == nullptr || entity->declaration().as_entity() == nullptr) {
        return json{{"ifcClass", entity == nullptr ? json(nullptr) : json(entity->declaration().name())},
                    {"attributes", attributes},
                    {"references", references}};
    }

    const auto allAttributes = entity->declaration().as_entity()->all_attributes();
    for (std::size_t index = 0; index < allAttributes.size(); ++index) {
        const std::string& name = allAttributes[index]->name();
        if (excludedAttributes.count(name) != 0) {
            continue;
        }
        const AttributeValue value = entity->get_attribute_value(index);
        const bool isReference = !value.isNull() &&
            (value.type() == IfcUtil::Argument_ENTITY_INSTANCE ||
             value.type() == IfcUtil::Argument_AGGREGATE_OF_ENTITY_INSTANCE ||
             value.type() == IfcUtil::Argument_AGGREGATE_OF_AGGREGATE_OF_ENTITY_INSTANCE);
        if (isReference) {
            references[name] = attributeValueToJson(value, resolver);
        } else {
            attributes[name] = attributeValueToJson(value, resolver);
        }
    }

    return json{{"ifcClass", entity->declaration().name()}, {"attributes", attributes}, {"references", references}};
}

inline std::vector<double> getCoordinates(const Base* pointOrDirection) {
    if (pointOrDirection == nullptr ||
        (!hasAttribute(pointOrDirection, "Coordinates") && !hasAttribute(pointOrDirection, "DirectionRatios"))) {
        return {};
    }
    const std::string attributeName = hasAttribute(pointOrDirection, "Coordinates") ? "Coordinates" : "DirectionRatios";
    const AttributeValue value = getAttribute(pointOrDirection, attributeName);
    if (value.isNull()) {
        return {};
    }
    return static_cast<std::vector<double>>(value);
}

inline Matrix4 axisPlacementToMatrix(const Base* placement) {
    if (placement == nullptr) {
        return identityMatrix();
    }

    if (isA(placement, "IfcAxis2Placement3D")) {
        std::array<double, 3> origin{0.0, 0.0, 0.0};
        const auto location = getCoordinates(getEntityAttribute(placement, "Location"));
        for (std::size_t i = 0; i < std::min<std::size_t>(3, location.size()); ++i) {
            origin[i] = location[i];
        }

        std::array<double, 3> z{0.0, 0.0, 1.0};
        const auto axis = getCoordinates(getEntityAttribute(placement, "Axis"));
        if (!axis.empty()) {
            for (std::size_t i = 0; i < std::min<std::size_t>(3, axis.size()); ++i) {
                z[i] = axis[i];
            }
            z = normalize3(z);
        }

        std::array<double, 3> x{1.0, 0.0, 0.0};
        const auto refDirection = getCoordinates(getEntityAttribute(placement, "RefDirection"));
        if (!refDirection.empty()) {
            for (std::size_t i = 0; i < std::min<std::size_t>(3, refDirection.size()); ++i) {
                x[i] = refDirection[i];
            }
        }
        x = normalize3(std::array<double, 3>{
            x[0] - dot3(x, z) * z[0],
            x[1] - dot3(x, z) * z[1],
            x[2] - dot3(x, z) * z[2]});
        const auto y = normalize3(cross3(z, x));

        Matrix4 matrix = identityMatrix();
        matrix[0] = x[0];
        matrix[4] = x[1];
        matrix[8] = x[2];
        matrix[1] = y[0];
        matrix[5] = y[1];
        matrix[9] = y[2];
        matrix[2] = z[0];
        matrix[6] = z[1];
        matrix[10] = z[2];
        matrix[3] = origin[0];
        matrix[7] = origin[1];
        matrix[11] = origin[2];
        return matrix;
    }

    if (isA(placement, "IfcAxis2Placement2D")) {
        std::array<double, 3> origin{0.0, 0.0, 0.0};
        const auto location = getCoordinates(getEntityAttribute(placement, "Location"));
        for (std::size_t i = 0; i < std::min<std::size_t>(2, location.size()); ++i) {
            origin[i] = location[i];
        }

        std::array<double, 3> x{1.0, 0.0, 0.0};
        const auto refDirection = getCoordinates(getEntityAttribute(placement, "RefDirection"));
        if (!refDirection.empty()) {
            x[0] = refDirection[0];
            x[1] = refDirection.size() > 1 ? refDirection[1] : 0.0;
            x = normalize3(x);
        }
        const std::array<double, 3> y{-x[1], x[0], 0.0};
        Matrix4 matrix = identityMatrix();
        matrix[0] = x[0];
        matrix[4] = x[1];
        matrix[1] = y[0];
        matrix[5] = y[1];
        matrix[3] = origin[0];
        matrix[7] = origin[1];
        return matrix;
    }

    throw SceneError(
        "INVALID_PLACEMENT",
        "Unsupported IFC axis placement type",
        json{{"ifcClass", placement->declaration().name()}});
}

inline Matrix4 localPlacementMatrix(const Base* objectPlacement) {
    if (objectPlacement == nullptr) {
        return identityMatrix();
    }
    if (!isA(objectPlacement, "IfcLocalPlacement")) {
        throw SceneError(
            "INVALID_PLACEMENT",
            "Only IfcLocalPlacement is editable",
            json{{"ifcClass", objectPlacement->declaration().name()}});
    }
    return axisPlacementToMatrix(getEntityAttribute(objectPlacement, "RelativePlacement"));
}

inline Matrix4 worldPlacementMatrix(const Base* objectPlacement, std::unordered_set<const Base*>& visiting) {
    if (objectPlacement == nullptr) {
        return identityMatrix();
    }
    if (!isA(objectPlacement, "IfcLocalPlacement")) {
        return identityMatrix();
    }
    if (!visiting.insert(objectPlacement).second) {
        throw SceneError("INVALID_RELATION", "Cyclic IfcLocalPlacement hierarchy detected");
    }
    const Matrix4 local = localPlacementMatrix(objectPlacement);
    const Matrix4 parent = worldPlacementMatrix(getEntityAttribute(objectPlacement, "PlacementRelTo"), visiting);
    visiting.erase(objectPlacement);
    return multiplyMatrix(parent, local);
}

inline Matrix4 worldPlacementMatrix(const Base* objectPlacement) {
    std::unordered_set<const Base*> visiting;
    return worldPlacementMatrix(objectPlacement, visiting);
}

inline Base* createCartesianPoint(IfcParse::IfcFile& file, const std::vector<double>& coordinates) {
    Base* point = createInstance(file, "IfcCartesianPoint");
    setAttribute(point, "Coordinates", coordinates);
    return point;
}

inline Base* createDirection(IfcParse::IfcFile& file, const std::vector<double>& ratios) {
    Base* direction = createInstance(file, "IfcDirection");
    setAttribute(direction, "DirectionRatios", ratios);
    return direction;
}

inline Base* createAxis2Placement3D(IfcParse::IfcFile& file, const Matrix4& matrix) {
    validateRigidMatrix(matrix);
    Base* placement = createInstance(file, "IfcAxis2Placement3D");
    setEntityAttribute(placement, "Location", createCartesianPoint(file, {matrix[3], matrix[7], matrix[11]}));
    setEntityAttribute(placement, "Axis", createDirection(file, {matrix[2], matrix[6], matrix[10]}));
    setEntityAttribute(placement, "RefDirection", createDirection(file, {matrix[0], matrix[4], matrix[8]}));
    return placement;
}

inline Base* createLocalPlacement(IfcParse::IfcFile& file, Base* parentPlacement, const Matrix4& localMatrix) {
    Base* placement = createInstance(file, "IfcLocalPlacement");
    if (parentPlacement == nullptr) {
        unsetAttribute(placement, "PlacementRelTo");
    } else {
        setEntityAttribute(placement, "PlacementRelTo", parentPlacement);
    }
    setEntityAttribute(placement, "RelativePlacement", createAxis2Placement3D(file, localMatrix));
    return placement;
}

inline std::optional<std::string> globalIdOf(const Base* entity) {
    return entity != nullptr && hasAttribute(entity, "GlobalId") ? getOptionalString(entity, "GlobalId") : std::nullopt;
}

inline Base* createIfcValue(IfcParse::IfcFile& file, const std::string& valueType, const json& value) {
    if (value.is_null()) {
        return nullptr;
    }

    Base* typedValue = nullptr;
    try {
        typedValue = file.create(file.schema()->declaration_by_name(valueType));
    } catch (const std::exception& exception) {
        throw SceneError(
            "INVALID_PARAMETERS",
            "Unknown IFC value type " + valueType,
            json{{"valueType", valueType}, {"reason", exception.what()}});
    }
    if (typedValue == nullptr || typedValue->declaration().as_type_declaration() == nullptr) {
        throw SceneError("INVALID_PARAMETERS", "valueType must name an IFC simple type", json{{"valueType", valueType}});
    }

    if (value.is_boolean()) {
        typedValue->set_attribute_value(0, value.get<bool>());
    } else if (value.is_number_integer()) {
        if (valueType == "IfcInteger" || valueType == "IfcCountMeasure") {
            typedValue->set_attribute_value(0, value.get<int>());
        } else {
            typedValue->set_attribute_value(0, value.get<double>());
        }
    } else if (value.is_number_float()) {
        typedValue->set_attribute_value(0, value.get<double>());
    } else if (value.is_string()) {
        typedValue->set_attribute_value(0, value.get<std::string>());
    } else {
        throw SceneError(
            "INVALID_PARAMETERS",
            "Property value must be a string, number, boolean or null",
            json{{"valueType", valueType}});
    }
    return typedValue;
}

inline json readIfcValue(const Base* value) {
    if (value == nullptr) {
        return nullptr;
    }
    const AttributeValue argument = value->get_attribute_value(0);
    if (argument.isNull()) {
        return nullptr;
    }
    switch (argument.type()) {
        case IfcUtil::Argument_INT:
            return static_cast<int>(argument);
        case IfcUtil::Argument_BOOL:
            return static_cast<bool>(argument);
        case IfcUtil::Argument_LOGICAL: {
            const boost::logic::tribool logical = argument;
            return boost::logic::indeterminate(logical) ? json(nullptr) : json(static_cast<bool>(logical));
        }
        case IfcUtil::Argument_DOUBLE:
            return static_cast<double>(argument);
        case IfcUtil::Argument_STRING:
            return static_cast<std::string>(argument);
        case IfcUtil::Argument_ENUMERATION:
            return std::string(static_cast<EnumerationReference>(argument).value());
        default:
            return nullptr;
    }
}

inline std::string nullValueTypeMarker(const std::string& valueType) {
    return "ifc-editor:null-value-type=" + valueType;
}

inline std::optional<std::string> parseNullValueTypeMarker(const Base* property) {
    const auto description = getOptionalString(property, "Description");
    const std::string prefix = "ifc-editor:null-value-type=";
    if (!description.has_value() || description->rfind(prefix, 0) != 0 || description->size() == prefix.size()) {
        return std::nullopt;
    }
    return description->substr(prefix.size());
}

inline std::string unitToString(const Base* unit) {
    if (unit == nullptr) {
        return std::string();
    }
    if (hasAttribute(unit, "Name")) {
        const AttributeValue name = getAttribute(unit, "Name");
        if (!name.isNull()) {
            if (name.type() == IfcUtil::Argument_ENUMERATION) {
                return static_cast<EnumerationReference>(name).value();
            }
            if (name.type() == IfcUtil::Argument_STRING) {
                return static_cast<std::string>(name);
            }
        }
    }
    if (hasAttribute(unit, "UnitType")) {
        const AttributeValue unitType = getAttribute(unit, "UnitType");
        if (!unitType.isNull() && unitType.type() == IfcUtil::Argument_ENUMERATION) {
            return static_cast<EnumerationReference>(unitType).value();
        }
    }
    return unit->declaration().name() + "#" + std::to_string(unit->id());
}

inline Base* resolveUnit(IfcParse::IfcFile& file, const json& unit) {
    if (unit.is_null()) {
        return nullptr;
    }
    const std::string requested = unit.get<std::string>();
    for (Base* candidate : instancesByType(file, "IfcUnit")) {
        if (unitToString(candidate) == requested) {
            return candidate;
        }
    }
    for (const std::string& className : {"IfcNamedUnit", "IfcMonetaryUnit", "IfcDerivedUnit"}) {
        for (Base* candidate : instancesByType(file, className)) {
            if (unitToString(candidate) == requested) {
                return candidate;
            }
        }
    }
    throw SceneError("INVALID_PARAMETERS", "Property unit does not exist in the loaded IFC model", json{{"unit", requested}});
}

inline json readPropertySingleValue(const Base* property, const std::string& propertySet, const std::string& source) {
    Base* nominalValue = getEntityAttribute(property, "NominalValue");
    std::string valueType;
    json value = nullptr;
    if (nominalValue != nullptr) {
        valueType = nominalValue->declaration().name();
        value = readIfcValue(nominalValue);
    } else {
        valueType = parseNullValueTypeMarker(property).value_or("IfcLabel");
    }
    Base* unit = getEntityAttribute(property, "Unit");
    return json{
        {"propertySet", propertySet},
        {"name", getString(property, "Name")},
        {"valueType", valueType},
        {"value", value},
        {"unit", unit == nullptr ? json(nullptr) : json(unitToString(unit))},
        {"source", source}};
}

inline json readPropertiesFromSet(const Base* propertySet, const std::string& source) {
    json result = json::array();
    const std::string propertySetName = getString(propertySet, "Name", propertySet->declaration().name());
    const std::string propertiesAttribute = hasAttribute(propertySet, "HasProperties") ? "HasProperties" :
        (hasAttribute(propertySet, "ExtendedProperties") ? "ExtendedProperties" : "Properties");
    for (Base* property : aggregateToVector(getEntityAggregate(propertySet, propertiesAttribute))) {
        if (isA(property, "IfcPropertySingleValue")) {
            result.push_back(readPropertySingleValue(property, propertySetName, source));
        }
    }
    return result;
}

inline json readOccurrenceProperties(IfcParse::IfcFile& file, Base* object) {
    json result = json::array();
    for (Base* relation : instancesByType(file, "IfcRelDefinesByProperties")) {
        const auto relatedObjects = getEntityAggregate(relation, "RelatedObjects");
        if (!aggregateContains(relatedObjects, object) || relatedObjects->size() != 1) {
            continue;
        }
        Base* definition = getEntityAttribute(relation, "RelatingPropertyDefinition");
        if (definition != nullptr && isA(definition, "IfcPropertySet")) {
            for (const auto& property : readPropertiesFromSet(definition, "occurrence")) {
                result.push_back(property);
            }
        }
    }
    return result;
}

inline json readTypeProperties(IfcParse::IfcFile& file, Base* object) {
    json result = json::array();
    for (Base* relation : instancesByType(file, "IfcRelDefinesByType")) {
        if (!aggregateContains(getEntityAggregate(relation, "RelatedObjects"), object)) {
            continue;
        }
        Base* typeObject = getEntityAttribute(relation, "RelatingType");
        if (typeObject == nullptr || !hasAttribute(typeObject, "HasPropertySets")) {
            continue;
        }
        for (Base* propertySet : aggregateToVector(getEntityAggregate(typeObject, "HasPropertySets"))) {
            if (isA(propertySet, "IfcPropertySet")) {
                for (const auto& property : readPropertiesFromSet(propertySet, "type")) {
                    result.push_back(property);
                }
            }
        }
    }
    return result;
}

inline json readQuantityProperties(IfcParse::IfcFile& file, Base* object) {
    json result = json::array();
    for (Base* relation : instancesByType(file, "IfcRelDefinesByProperties")) {
        if (!aggregateContains(getEntityAggregate(relation, "RelatedObjects"), object)) {
            continue;
        }
        Base* quantitySet = getEntityAttribute(relation, "RelatingPropertyDefinition");
        if (quantitySet == nullptr || !isA(quantitySet, "IfcElementQuantity")) {
            continue;
        }
        const std::string setName = getString(quantitySet, "Name", "IfcElementQuantity");
        for (Base* quantity : aggregateToVector(getEntityAggregate(quantitySet, "Quantities"))) {
            if (!isA(quantity, "IfcPhysicalSimpleQuantity")) {
                continue;
            }
            const auto* declaration = quantity->declaration().as_entity();
            const auto attributes = declaration->all_attributes();
            for (std::size_t index = 0; index < attributes.size(); ++index) {
                const std::string& attributeName = attributes[index]->name();
                if (attributeName.size() < 5 || attributeName.substr(attributeName.size() - 5) != "Value") {
                    continue;
                }
                const AttributeValue attribute = quantity->get_attribute_value(index);
                if (attribute.isNull() || (attribute.type() != IfcUtil::Argument_DOUBLE && attribute.type() != IfcUtil::Argument_INT)) {
                    continue;
                }
                result.push_back(json{
                    {"propertySet", setName},
                    {"name", getString(quantity, "Name")},
                    {"valueType", quantity->declaration().name()},
                    {"value", attribute.type() == IfcUtil::Argument_INT ? json(static_cast<int>(attribute)) : json(static_cast<double>(attribute))},
                    {"unit", nullptr},
                    {"source", "quantity"}});
                break;
            }
        }
    }
    return result;
}

inline json readAllTransformProperties(IfcParse::IfcFile& file, Base* object) {
    json result = readOccurrenceProperties(file, object);
    for (const auto& property : readTypeProperties(file, object)) {
        result.push_back(property);
    }
    for (const auto& property : readQuantityProperties(file, object)) {
        result.push_back(property);
    }
    return result;
}

inline Base* createPropertySingleValue(IfcParse::IfcFile& file, const json& property) {
    Base* instance = createInstance(file, "IfcPropertySingleValue");
    setAttribute(instance, "Name", property.at("name").get<std::string>());
    if (property.at("value").is_null()) {
        unsetAttribute(instance, "NominalValue");
        if (hasAttribute(instance, "Description")) {
            setAttribute(instance, "Description", nullValueTypeMarker(property.at("valueType").get<std::string>()));
        }
    } else {
        setEntityAttribute(
            instance,
            "NominalValue",
            createIfcValue(file, property.at("valueType").get<std::string>(), property.at("value")));
        if (hasAttribute(instance, "Description")) {
            unsetAttribute(instance, "Description");
        }
    }
    Base* unit = resolveUnit(file, property.at("unit"));
    if (unit == nullptr) {
        unsetAttribute(instance, "Unit");
    } else {
        setEntityAttribute(instance, "Unit", unit);
    }
    return instance;
}

inline void updatePropertySingleValue(IfcParse::IfcFile& file, Base* instance, const json& property) {
    setAttribute(instance, "Name", property.at("name").get<std::string>());
    if (property.at("value").is_null()) {
        unsetAttribute(instance, "NominalValue");
        if (hasAttribute(instance, "Description")) {
            setAttribute(instance, "Description", nullValueTypeMarker(property.at("valueType").get<std::string>()));
        }
    } else {
        setEntityAttribute(
            instance,
            "NominalValue",
            createIfcValue(file, property.at("valueType").get<std::string>(), property.at("value")));
        if (hasAttribute(instance, "Description")) {
            unsetAttribute(instance, "Description");
        }
    }
    Base* unit = resolveUnit(file, property.at("unit"));
    if (unit == nullptr) {
        unsetAttribute(instance, "Unit");
    } else {
        setEntityAttribute(instance, "Unit", unit);
    }
}

inline void removeIfUnreferenced(IfcParse::IfcFile& file, Base* entity) {
    if (entity != nullptr && file.getTotalInverses(entity->id()) == 0) {
        file.removeEntity(entity);
    }
}

inline void removeUniqueIfUnreferenced(IfcParse::IfcFile& file, const std::vector<Base*>& entities) {
    std::unordered_set<Base*> visited;
    for (Base* entity : entities) {
        if (entity != nullptr && visited.insert(entity).second) {
            removeIfUnreferenced(file, entity);
        }
    }
}

inline void replaceOccurrenceProperties(IfcParse::IfcFile& file, Base* object, const json& properties) {
    std::map<std::string, std::map<std::string, json>> desired;
    for (const auto& property : properties) {
        desired[property.at("propertySet").get<std::string>()][property.at("name").get<std::string>()] = property;
    }

    std::set<std::string> handledSets;
    std::vector<Base*> entitiesToRemove;
    for (Base* relation : instancesByType(file, "IfcRelDefinesByProperties")) {
        const auto relatedObjects = getEntityAggregate(relation, "RelatedObjects");
        if (!aggregateContains(relatedObjects, object) || relatedObjects->size() != 1) {
            continue;
        }
        Base* propertySet = getEntityAttribute(relation, "RelatingPropertyDefinition");
        if (propertySet == nullptr || !isA(propertySet, "IfcPropertySet")) {
            continue;
        }
        const std::string setName = getString(propertySet, "Name");
        handledSets.insert(setName);
        auto currentProperties = aggregateToVector(getEntityAggregate(propertySet, "HasProperties"));
        std::vector<Base*> nextProperties;
        std::set<std::string> updated;

        for (Base* current : currentProperties) {
            if (!isA(current, "IfcPropertySingleValue")) {
                nextProperties.push_back(current);
                continue;
            }
            const std::string propertyName = getString(current, "Name");
            const auto setIterator = desired.find(setName);
            const auto propertyIterator = setIterator == desired.end() ? std::map<std::string, json>::const_iterator() : setIterator->second.find(propertyName);
            if (setIterator != desired.end() && propertyIterator != setIterator->second.end()) {
                updatePropertySingleValue(file, current, propertyIterator->second);
                nextProperties.push_back(current);
                updated.insert(propertyName);
            } else {
                entitiesToRemove.push_back(current);
            }
        }

        const auto setIterator = desired.find(setName);
        if (setIterator != desired.end()) {
            for (const auto& [propertyName, property] : setIterator->second) {
                if (updated.count(propertyName) == 0) {
                    nextProperties.push_back(createPropertySingleValue(file, property));
                }
            }
        }

        if (nextProperties.empty()) {
            entitiesToRemove.push_back(relation);
            entitiesToRemove.push_back(propertySet);
        } else {
            setAttribute(propertySet, "HasProperties", makeAggregate(nextProperties));
        }
    }

    for (const auto& [setName, setProperties] : desired) {
        if (handledSets.count(setName) != 0) {
            continue;
        }
        std::vector<Base*> propertyInstances;
        for (const auto& [propertyName, property] : setProperties) {
            static_cast<void>(propertyName);
            propertyInstances.push_back(createPropertySingleValue(file, property));
        }
        Base* propertySet = createInstance(file, "IfcPropertySet");
        initializeRoot(file, propertySet, setName);
        setAttribute(propertySet, "HasProperties", makeAggregate(propertyInstances));

        Base* relation = createInstance(file, "IfcRelDefinesByProperties");
        initializeRoot(file, relation, std::string());
        setAttribute(relation, "RelatedObjects", makeAggregate({object}));
        setEntityAttribute(relation, "RelatingPropertyDefinition", propertySet);
    }

    std::unordered_set<Base*> uniqueRemoval(entitiesToRemove.begin(), entitiesToRemove.end());
    for (Base* entity : uniqueRemoval) {
        if (entity != nullptr && file.instance_by_id(entity->id()) == entity) {
            file.removeEntity(entity);
        }
    }
}

class Transform {
public:
    Transform(std::string id, Entity* entity)
        : id_(std::move(id)), entity_(entity) {
        if (entity_ == nullptr) {
            throw SceneError("INTERNAL_ERROR", "Transform cannot wrap a null IFC entity");
        }
    }

    const std::string& id() const noexcept {
        return id_;
    }

    Entity* entity() const noexcept {
        return entity_;
    }

    std::optional<unsigned> ifcEntityId() const {
        return entity_ == nullptr ? std::nullopt : std::optional<unsigned>(entity_->id());
    }

    std::optional<std::string> globalId() const {
        return globalIdOf(entity_);
    }

    std::string name() const {
        return getString(entity_, "Name");
    }

    Base* objectPlacement() const {
        return getEntityAttribute(entity_, "ObjectPlacement");
    }

    bool placementEditable() const {
        if (!hasAttribute(entity_, "ObjectPlacement")) {
            return false;
        }
        Base* placement = objectPlacement();
        return placement == nullptr || isA(placement, "IfcLocalPlacement");
    }

    std::string placementSourceType() const {
        Base* placement = objectPlacement();
        return placement == nullptr ? "virtual" : placement->declaration().name();
    }

    Matrix4 localMatrix() const {
        Base* placement = objectPlacement();
        if (placement == nullptr) {
            return identityMatrix();
        }
        if (!isA(placement, "IfcLocalPlacement")) {
            return identityMatrix();
        }
        return localPlacementMatrix(placement);
    }

    void setName(const std::string& name) {
        if (!hasAttribute(entity_, "Name")) {
            throw SceneError("TRANSFORM_NOT_EDITABLE", "Transform name is not editable", json{{"id", id_}});
        }
        setAttribute(entity_, "Name", name);
    }

    Base* replacePlacement(IfcParse::IfcFile& file, Base* parentPlacement, const Matrix4& localMatrix) {
        if (!placementEditable()) {
            throw SceneError("TRANSFORM_NOT_EDITABLE", "Transform placement is not editable", json{{"id", id_}});
        }
        Base* previous = objectPlacement();
        Base* replacement = createLocalPlacement(file, parentPlacement, localMatrix);
        setEntityAttribute(entity_, "ObjectPlacement", replacement);
        return previous;
    }

    void replaceProperties(IfcParse::IfcFile& file, const json& properties) {
        replaceOccurrenceProperties(file, entity_, properties);
    }

    void setParents(std::optional<std::string> spatial, std::optional<std::string> placement, std::optional<std::string> decomposition) {
        spatialParentId_ = std::move(spatial);
        placementParentId_ = std::move(placement);
        decompositionParentId_ = std::move(decomposition);
    }

    const std::optional<std::string>& spatialParentId() const noexcept {
        return spatialParentId_;
    }

    const std::optional<std::string>& placementParentId() const noexcept {
        return placementParentId_;
    }

    const std::optional<std::string>& decompositionParentId() const noexcept {
        return decompositionParentId_;
    }

    void setGeometryIds(std::vector<std::string> ids) {
        geometryIds_ = std::move(ids);
    }

    void setMaterialIds(std::vector<std::string> ids) {
        materialIds_ = std::move(ids);
    }

    const std::vector<std::string>& geometryIds() const noexcept {
        return geometryIds_;
    }

    const std::vector<std::string>& materialIds() const noexcept {
        return materialIds_;
    }

    json toJson(
        IfcParse::IfcFile& file,
        const Matrix4& worldMatrix,
        const EntityResolver& resolver = EntityResolver()) const {
        const auto global = globalId();
        const json parents{
            {"spatial", spatialParentId_.has_value() ? json(*spatialParentId_) : json(nullptr)},
            {"placement", placementParentId_.has_value() ? json(*placementParentId_) : json(nullptr)},
            {"decomposition", decompositionParentId_.has_value() ? json(*decompositionParentId_) : json(nullptr)}};
        const json transforms{
            {"local", matrixToJson(localMatrix())},
            {"world", matrixToJson(worldMatrix)},
            {"editable", placementEditable()},
            {"sourceType", placementSourceType()}};

        return json{
            {"id", id_},
            {"ifcEntityId", entity_->id()},
            {"globalId", global.has_value() ? json(*global) : json(nullptr)},
            {"name", name()},
            {"parents", parents},
            {"transforms", transforms},
            {"properties", readAllTransformProperties(file, entity_)},
            {"geometries", geometryIds_},
            {"materials", materialIds_},
            {"definition", buildDefinition(
                entity_,
                {"GlobalId", "OwnerHistory", "Name", "Description", "ObjectType", "ObjectPlacement", "Representation", "Tag"},
                resolver)}};
    }

private:
    std::string id_;
    Entity* entity_;
    std::optional<std::string> spatialParentId_;
    std::optional<std::string> placementParentId_;
    std::optional<std::string> decompositionParentId_;
    std::vector<std::string> geometryIds_;
    std::vector<std::string> materialIds_;
};

} // namespace ifc_editor