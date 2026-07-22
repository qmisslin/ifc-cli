#include "protocol.hpp"
#include "scene.hpp"

#include <algorithm>
#include <cmath>
#include <exception>
#include <iostream>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

using ifc_editor::IfcScene;
using ifc_editor::JsonlProtocol;
using ifc_editor::ProtocolError;
using ifc_editor::ProtocolException;
using ifc_editor::ProtocolRequest;
using ifc_editor::SceneError;
using ifc_editor::json;

[[noreturn]] void invalidParameters(const std::string& message, json details = json::object()) {
    throw SceneError("INVALID_PARAMETERS", message, std::move(details));
}

void requireExactFields(const json& object, const std::set<std::string>& required, const std::set<std::string>& optional = {}) {
    if (!object.is_object()) {
        invalidParameters("Parameters must be a JSON object");
    }
    for (const std::string& field : required) {
        if (!object.contains(field)) {
            invalidParameters("Required parameter is missing", json{{"field", field}});
        }
    }
    for (auto iterator = object.begin(); iterator != object.end(); ++iterator) {
        if (required.count(iterator.key()) == 0 && optional.count(iterator.key()) == 0) {
            invalidParameters("Unknown parameter", json{{"field", iterator.key()}});
        }
    }
}

void requireEmptyObject(const json& params) {
    requireExactFields(params, {});
}

std::string requireString(const json& object, const std::string& field, bool nonEmpty = false) {
    if (!object.at(field).is_string()) {
        invalidParameters("Parameter must be a string", json{{"field", field}});
    }
    const std::string value = object.at(field).get<std::string>();
    if (nonEmpty && value.empty()) {
        invalidParameters("Parameter must be a non-empty string", json{{"field", field}});
    }
    return value;
}

bool requireBoolean(const json& object, const std::string& field) {
    if (!object.at(field).is_boolean()) {
        invalidParameters("Parameter must be a boolean", json{{"field", field}});
    }
    return object.at(field).get<bool>();
}

double requireFiniteNumber(const json& object, const std::string& field, bool strictlyPositive = false) {
    if (!object.at(field).is_number()) {
        invalidParameters("Parameter must be a number", json{{"field", field}});
    }
    const double value = object.at(field).get<double>();
    if (!std::isfinite(value)) {
        invalidParameters("Parameter must be finite", json{{"field", field}});
    }
    if (strictlyPositive && value <= 0.0) {
        invalidParameters("Parameter must be strictly positive", json{{"field", field}});
    }
    return value;
}

void validateSessionId(const json& value, const std::string& field, const std::set<std::string>& prefixes) {
    if (!value.is_string() || value.get_ref<const std::string&>().empty()) {
        invalidParameters("Session identifier must be a non-empty string", json{{"field", field}});
    }
    const std::string id = value.get<std::string>();
    bool valid = false;
    for (const std::string& prefix : prefixes) {
        if (id.rfind(prefix + ":", 0) == 0 && id.size() > prefix.size() + 1) {
            valid = true;
            break;
        }
    }
    if (!valid) {
        const bool knownType = id.rfind("transform:", 0) == 0 || id.rfind("geometry:", 0) == 0 || id.rfind("material:", 0) == 0;
        if (knownType) {
            throw SceneError("INVALID_ENTITY_TYPE", "Session identifier has an invalid entity type", json{{"field", field}, {"id", id}});
        }
        invalidParameters("Session identifier has an invalid prefix", json{{"field", field}, {"id", id}});
    }
}

void validateMatrix(const json& value) {
    if (!value.is_array() || value.size() != 16) {
        invalidParameters("Matrix must contain exactly 16 numbers");
    }
    for (const auto& component : value) {
        if (!component.is_number()) {
            invalidParameters("Matrix components must be numbers");
        }
        const double number = component.get<double>();
        if (!std::isfinite(number)) {
            invalidParameters("Matrix components must be finite");
        }
    }
}

void validateTransformInput(const json& value) {
    requireExactFields(value, {"space", "matrix"});
    const std::string space = requireString(value, "space", true);
    if (space != "parent" && space != "world") {
        invalidParameters("Transform space must be parent or world", json{{"field", "space"}});
    }
    validateMatrix(value.at("matrix"));
}

void validateParentsInput(const json& value) {
    requireExactFields(value, {"spatial", "placement", "decomposition"});
    for (const std::string& field : {"spatial", "placement", "decomposition"}) {
        const json& parent = value.at(field);
        if (!parent.is_null()) {
            validateSessionId(parent, field, {"transform"});
        }
    }
}

void validatePropertyValue(const json& value) {
    if (!value.is_null() && !value.is_string() && !value.is_number() && !value.is_boolean()) {
        invalidParameters("Property value must be a string, number, boolean or null");
    }
    if (value.is_number() && !std::isfinite(value.get<double>())) {
        invalidParameters("Property numeric value must be finite");
    }
}

void validateProperties(const json& value, const std::string& expectedSource) {
    if (!value.is_array()) {
        invalidParameters("Properties must be an array", json{{"field", "properties"}});
    }
    std::set<std::pair<std::string, std::string>> keys;
    for (const auto& property : value) {
        requireExactFields(property, {"propertySet", "name", "valueType", "value", "unit", "source"});
        const std::string propertySet = requireString(property, "propertySet", true);
        const std::string name = requireString(property, "name", true);
        requireString(property, "valueType", true);
        validatePropertyValue(property.at("value"));
        if (!property.at("unit").is_null() && !property.at("unit").is_string()) {
            invalidParameters("Property unit must be a string or null");
        }
        if (property.at("unit").is_string() && property.at("unit").get_ref<const std::string&>().empty()) {
            invalidParameters("Property unit must be null or a non-empty string");
        }
        const std::string source = requireString(property, "source", true);
        if (source != expectedSource) {
            invalidParameters("Property source is invalid", json{{"expected", expectedSource}, {"actual", source}});
        }
        if (!keys.emplace(propertySet, name).second) {
            invalidParameters("Property set and property name must be unique", json{{"propertySet", propertySet}, {"name", name}});
        }
    }
}

void validateProfile(const json& value) {
    if (!value.is_array() || value.size() < 3) {
        invalidParameters("Profile must contain at least three points");
    }
    for (const auto& point : value) {
        if (!point.is_array() || point.size() != 2) {
            invalidParameters("Each profile point must contain exactly two numbers");
        }
        for (const auto& component : point) {
            if (!component.is_number() || !std::isfinite(component.get<double>())) {
                invalidParameters("Profile coordinates must be finite numbers");
            }
        }
    }
}

void validateVisual(const json& value) {
    requireExactFields(value, {"color", "opacity", "metallic", "roughness"});
    if (!value.at("color").is_array() || value.at("color").size() != 3) {
        invalidParameters("Visual color must contain exactly three numbers");
    }
    for (const auto& component : value.at("color")) {
        if (!component.is_number()) {
            invalidParameters("Visual color components must be numbers");
        }
        const double number = component.get<double>();
        if (!std::isfinite(number) || number < 0.0 || number > 1.0) {
            invalidParameters("Visual color components must be finite and between 0 and 1");
        }
    }
    for (const std::string& field : {"opacity", "metallic", "roughness"}) {
        const double number = requireFiniteNumber(value, field);
        if (number < 0.0 || number > 1.0) {
            invalidParameters("Visual component must be between 0 and 1", json{{"field", field}});
        }
    }
}

void validateOperation(const std::string& operation) {
    if (operation != "ADD" && operation != "SUBTRACT") {
        invalidParameters("Geometry operation must be ADD or SUBTRACT", json{{"field", "operation"}});
    }
}

void validateTessellationOptions(const json& value) {
    requireExactFields(value, {"space", "includeNormals", "includeMaterials", "includeChildren"});
    const std::string space = requireString(value, "space", true);
    if (space != "local" && space != "world") {
        invalidParameters("Tessellation space must be local or world", json{{"field", "options.space"}});
    }
    requireBoolean(value, "includeNormals");
    requireBoolean(value, "includeMaterials");
    requireBoolean(value, "includeChildren");
}

void validateCommand(const ProtocolRequest& request) {
    const json& params = request.params;
    const std::string& command = request.command;

    if (command == "help" || command == "getCapabilities" || command == "exit" ||
        command == "transformGetAll" || command == "materialGetAll") {
        requireEmptyObject(params);
        return;
    }
    if (command == "load" || command == "save") {
        requireExactFields(params, {"path"});
        requireString(params, "path", true);
        return;
    }
    if (command == "transformGet" || command == "transformDelete") {
        requireExactFields(params, {"id"});
        validateSessionId(params.at("id"), "id", {"transform"});
        return;
    }
    if (command == "transformDuplicate") {
        requireExactFields(params, {"id", "includeChildren"});
        validateSessionId(params.at("id"), "id", {"transform"});
        requireBoolean(params, "includeChildren");
        return;
    }
    if (command == "transformCreate") {
        requireExactFields(params, {"name", "parents", "transform", "properties"});
        requireString(params, "name");
        validateParentsInput(params.at("parents"));
        validateTransformInput(params.at("transform"));
        validateProperties(params.at("properties"), "occurrence");
        return;
    }
    if (command == "transformUpdate") {
        requireExactFields(params, {"id"}, {"name", "parents", "transform", "properties"});
        validateSessionId(params.at("id"), "id", {"transform"});
        if (params.size() == 1) {
            invalidParameters("Transform update must contain at least one modifiable field");
        }
        if (params.contains("name")) {
            requireString(params, "name");
        }
        if (params.contains("parents")) {
            validateParentsInput(params.at("parents"));
        }
        if (params.contains("transform")) {
            validateTransformInput(params.at("transform"));
        }
        if (params.contains("properties")) {
            validateProperties(params.at("properties"), "occurrence");
        }
        return;
    }
    if (command == "transformTessellate") {
        requireExactFields(params, {"id", "options"});
        validateSessionId(params.at("id"), "id", {"transform"});
        validateTessellationOptions(params.at("options"));
        return;
    }
    if (command == "geometryGetAll") {
        requireExactFields(params, {"transformId"});
        if (!params.at("transformId").is_null()) {
            validateSessionId(params.at("transformId"), "transformId", {"transform"});
        }
        return;
    }
    if (command == "geometryGet" || command == "geometryDelete") {
        requireExactFields(params, {"id"});
        validateSessionId(params.at("id"), "id", {"geometry"});
        return;
    }
    if (command == "geometryCreate") {
        requireExactFields(params, {"parentId", "name", "transform", "profile", "depth", "operation"});
        validateSessionId(params.at("parentId"), "parentId", {"transform"});
        requireString(params, "name");
        validateTransformInput(params.at("transform"));
        validateProfile(params.at("profile"));
        requireFiniteNumber(params, "depth", true);
        validateOperation(requireString(params, "operation", true));
        return;
    }
    if (command == "geometryUpdate") {
        requireExactFields(params, {"id"}, {"name", "transform", "profile", "depth", "operation"});
        validateSessionId(params.at("id"), "id", {"geometry"});
        if (params.size() == 1) {
            invalidParameters("Geometry update must contain at least one modifiable field");
        }
        if (params.contains("name")) {
            requireString(params, "name");
        }
        if (params.contains("transform")) {
            validateTransformInput(params.at("transform"));
        }
        if (params.contains("profile")) {
            validateProfile(params.at("profile"));
        }
        if (params.contains("depth")) {
            requireFiniteNumber(params, "depth", true);
        }
        if (params.contains("operation")) {
            validateOperation(requireString(params, "operation", true));
        }
        return;
    }
    if (command == "materialGet" || command == "materialDelete") {
        requireExactFields(params, {"id"});
        validateSessionId(params.at("id"), "id", {"material"});
        return;
    }
    if (command == "materialCreate") {
        requireExactFields(params, {"name", "category", "visual", "properties"});
        requireString(params, "name");
        requireString(params, "category");
        validateVisual(params.at("visual"));
        validateProperties(params.at("properties"), "material");
        return;
    }
    if (command == "materialUpdate") {
        requireExactFields(params, {"id"}, {"name", "category", "visual", "properties"});
        validateSessionId(params.at("id"), "id", {"material"});
        if (params.size() == 1) {
            invalidParameters("Material update must contain at least one modifiable field");
        }
        if (params.contains("name")) {
            requireString(params, "name");
        }
        if (params.contains("category")) {
            requireString(params, "category");
        }
        if (params.contains("visual")) {
            validateVisual(params.at("visual"));
        }
        if (params.contains("properties")) {
            validateProperties(params.at("properties"), "material");
        }
        return;
    }
    if (command == "materialAssign" || command == "materialUnassign") {
        requireExactFields(params, {"materialId", "targetId"});
        validateSessionId(params.at("materialId"), "materialId", {"material"});
        validateSessionId(params.at("targetId"), "targetId", {"transform", "geometry"});
        return;
    }

    throw SceneError("UNKNOWN_COMMAND", "Unknown command", json{{"command", command}});
}

json helpResult() {
    static const std::string documentation =
        "IFC JSONL protocol 1.0\n"
        "Request: {\"id\":\"non-empty string\",\"command\":\"exact command\",\"params\":{...}}\n"
        "Success: {\"id\":...,\"ok\":true,\"result\":...}\n"
        "Error: {\"id\":...,\"ok\":false,\"error\":{\"code\":...,\"message\":...,\"details\":{...}}}\n"
        "Commands: help, getCapabilities, load, save, exit, transformGetAll, transformGet, "
        "transformCreate, transformDuplicate, transformUpdate, transformDelete, transformTessellate, geometryGetAll, "
        "geometryGet, geometryCreate, geometryUpdate, geometryDelete, materialGetAll, materialGet, "
        "materialCreate, materialUpdate, materialDelete, materialAssign, materialUnassign.";
    return json{{"documentation", documentation}};
}

void writeResponse(const json& response) {
    std::cout << JsonlProtocol::serializeResponse(response) << '\n';
    std::cout.flush();
}

json dispatchSingle(IfcScene& scene, const ProtocolRequest& request) {
    const json& params = request.params;
    const std::string& command = request.command;
    if (command == "help") {
        return helpResult();
    }
    if (command == "getCapabilities") {
        return scene.getCapabilities();
    }
    if (command == "load") {
        return scene.load(params.at("path").get<std::string>());
    }
    if (command == "save") {
        return scene.save(params.at("path").get<std::string>());
    }
    if (command == "exit") {
        scene.close();
        return json::object();
    }
    if (command == "transformGetAll") {
        return scene.transformGetAll();
    }
    if (command == "transformGet") {
        return scene.transformGet(params.at("id").get<std::string>());
    }
    if (command == "transformCreate") {
        return scene.transformCreate(
            params.at("name").get<std::string>(),
            params.at("parents"),
            params.at("transform"),
            params.at("properties"));
    }
    if (command == "transformDuplicate") {
        return scene.transformDuplicate(
            params.at("id").get<std::string>(),
            params.at("includeChildren").get<bool>());
    }
    if (command == "transformUpdate") {
        return scene.transformUpdate(params.at("id").get<std::string>(), params);
    }
    if (command == "transformDelete") {
        return scene.transformDelete(params.at("id").get<std::string>());
    }
    if (command == "geometryGetAll") {
        const std::optional<std::string> transformId = params.at("transformId").is_null() ?
            std::nullopt : std::optional<std::string>(params.at("transformId").get<std::string>());
        return scene.geometryGetAll(transformId);
    }
    if (command == "geometryGet") {
        return scene.geometryGet(params.at("id").get<std::string>());
    }
    if (command == "geometryCreate") {
        return scene.geometryCreate(
            params.at("parentId").get<std::string>(),
            params.at("name").get<std::string>(),
            params.at("transform"),
            params.at("profile"),
            params.at("depth").get<double>(),
            params.at("operation").get<std::string>());
    }
    if (command == "geometryUpdate") {
        return scene.geometryUpdate(params.at("id").get<std::string>(), params);
    }
    if (command == "geometryDelete") {
        return scene.geometryDelete(params.at("id").get<std::string>());
    }
    if (command == "materialGetAll") {
        return scene.materialGetAll();
    }
    if (command == "materialGet") {
        return scene.materialGet(params.at("id").get<std::string>());
    }
    if (command == "materialCreate") {
        return scene.materialCreate(
            params.at("name").get<std::string>(),
            params.at("category").get<std::string>(),
            params.at("visual"),
            params.at("properties"));
    }
    if (command == "materialUpdate") {
        return scene.materialUpdate(params.at("id").get<std::string>(), params);
    }
    if (command == "materialDelete") {
        return scene.materialDelete(params.at("id").get<std::string>());
    }
    if (command == "materialAssign") {
        return scene.materialAssign(
            params.at("materialId").get<std::string>(),
            params.at("targetId").get<std::string>());
    }
    if (command == "materialUnassign") {
        return scene.materialUnassign(
            params.at("materialId").get<std::string>(),
            params.at("targetId").get<std::string>());
    }
    throw SceneError("UNKNOWN_COMMAND", "Unknown command", json{{"command", command}});
}

} // namespace

int main() {
    IfcScene scene;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (std::all_of(line.begin(), line.end(), [](unsigned char character) {
                return character == ' ' || character == '\t' || character == '\r' || character == '\n';
            })) {
            continue;
        }

        json responseId = nullptr;
        try {
            const ProtocolRequest request = JsonlProtocol::parseRequest(line);
            responseId = request.id;
            validateCommand(request);

            if (request.command == "transformTessellate") {
                scene.transformTessellate(
                    request.params.at("id").get<std::string>(),
                    request.params.at("options"),
                    [&](const json& event) {
                        writeResponse(JsonlProtocol::createSuccessResponse(request.id, event));
                    });
                continue;
            }

            const json result = dispatchSingle(scene, request);
            writeResponse(JsonlProtocol::createSuccessResponse(request.id, result));
            if (request.command == "exit") {
                break;
            }
        } catch (const ProtocolException& exception) {
            const ProtocolError error{exception.code(), exception.what(), exception.details()};
            writeResponse(JsonlProtocol::createErrorResponse(exception.responseId(), error));
        } catch (const SceneError& exception) {
            const ProtocolError error{exception.code(), exception.what(), exception.details()};
            writeResponse(JsonlProtocol::createErrorResponse(responseId, error));
        } catch (const json::exception& exception) {
            const ProtocolError error{"INVALID_PARAMETERS", "Invalid command parameters", json{{"reason", exception.what()}}};
            writeResponse(JsonlProtocol::createErrorResponse(responseId, error));
        } catch (const std::exception& exception) {
            std::cerr << exception.what() << '\n';
            const ProtocolError error{"INTERNAL_ERROR", "An unexpected internal error occurred", json::object()};
            writeResponse(JsonlProtocol::createErrorResponse(responseId, error));
        } catch (...) {
            std::cerr << "Unknown internal error\n";
            const ProtocolError error{"INTERNAL_ERROR", "An unexpected internal error occurred", json::object()};
            writeResponse(JsonlProtocol::createErrorResponse(responseId, error));
        }
    }
    return 0;
}