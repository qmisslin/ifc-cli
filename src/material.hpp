#pragma once

#include "transform.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace ifc_editor {

struct Visual {
    std::array<double, 3> color{0.6, 0.6, 0.6};
    double opacity = 1.0;
    double metallic = 0.0;
    double roughness = 0.8;
};

inline Visual visualFromJson(const json& value) {
    Visual visual;
    visual.color = std::array<double, 3>{
        value.at("color").at(0).get<double>(),
        value.at("color").at(1).get<double>(),
        value.at("color").at(2).get<double>()};
    visual.opacity = value.at("opacity").get<double>();
    visual.metallic = value.at("metallic").get<double>();
    visual.roughness = value.at("roughness").get<double>();
    return visual;
}

inline json visualToJson(const Visual& visual) {
    return json{
        {"color", json::array({visual.color[0], visual.color[1], visual.color[2]})},
        {"opacity", visual.opacity},
        {"metallic", visual.metallic},
        {"roughness", visual.roughness}};
}

inline void validateVisual(const Visual& visual) {
    const auto validateComponent = [](double value, const char* name) {
        if (!std::isfinite(value) || value < 0.0 || value > 1.0) {
            throw SceneError(
                "INVALID_PARAMETERS",
                std::string("Visual component ") + name + " must be between 0.0 and 1.0");
        }
    };
    validateComponent(visual.color[0], "color[0]");
    validateComponent(visual.color[1], "color[1]");
    validateComponent(visual.color[2], "color[2]");
    validateComponent(visual.opacity, "opacity");
    validateComponent(visual.metallic, "metallic");
    validateComponent(visual.roughness, "roughness");
}

inline bool schemaSupports(IfcParse::IfcFile& file, const std::string& className) {
    try {
        return file.schema()->declaration_by_name(className) != nullptr;
    } catch (const std::exception&) {
        return false;
    }
}

inline std::string materialPropertiesAggregateAttribute(const Base* container) {
    if (hasAttribute(container, "Properties")) {
        return "Properties";
    }
    if (hasAttribute(container, "ExtendedProperties")) {
        return "ExtendedProperties";
    }
    throw SceneError(
        "INVALID_ENTITY_TYPE",
        "Unsupported IFC material property container",
        json{{"ifcClass", container == nullptr ? json(nullptr) : json(container->declaration().name())}});
}

inline std::vector<Base*> materialPropertyContainers(IfcParse::IfcFile& file, Base* material) {
    std::vector<Base*> result;
    for (const std::string& className : {"IfcMaterialProperties", "IfcExtendedMaterialProperties"}) {
        for (Base* container : instancesByType(file, className)) {
            if (getEntityAttribute(container, "Material") == material) {
                result.push_back(container);
            }
        }
    }
    return result;
}

inline Base* findMaterialPropertyContainer(IfcParse::IfcFile& file, Base* material, const std::string& name) {
    for (Base* container : materialPropertyContainers(file, material)) {
        if (getString(container, "Name") == name) {
            return container;
        }
    }
    return nullptr;
}

inline Base* createMaterialPropertyContainer(
    IfcParse::IfcFile& file,
    Base* material,
    const std::string& name,
    const std::vector<Base*>& properties) {
    const std::string className = schemaSupports(file, "IfcMaterialProperties") ?
        "IfcMaterialProperties" : "IfcExtendedMaterialProperties";
    Base* container = createInstance(file, className);
    setEntityAttribute(container, "Material", material);
    if (hasAttribute(container, "Name")) {
        setAttribute(container, "Name", name);
    }
    if (hasAttribute(container, "Description")) {
        unsetAttribute(container, "Description");
    }
    setAttribute(container, materialPropertiesAggregateAttribute(container), makeAggregate(properties));
    return container;
}

inline json internalVisualProperties(const Visual& visual) {
    return json::array({
        json{{"propertySet", "Pset_IfcEditorVisual"}, {"name", "ColorRed"}, {"valueType", "IfcNormalisedRatioMeasure"}, {"value", visual.color[0]}, {"unit", nullptr}, {"source", "material"}},
        json{{"propertySet", "Pset_IfcEditorVisual"}, {"name", "ColorGreen"}, {"valueType", "IfcNormalisedRatioMeasure"}, {"value", visual.color[1]}, {"unit", nullptr}, {"source", "material"}},
        json{{"propertySet", "Pset_IfcEditorVisual"}, {"name", "ColorBlue"}, {"valueType", "IfcNormalisedRatioMeasure"}, {"value", visual.color[2]}, {"unit", nullptr}, {"source", "material"}},
        json{{"propertySet", "Pset_IfcEditorVisual"}, {"name", "Opacity"}, {"valueType", "IfcNormalisedRatioMeasure"}, {"value", visual.opacity}, {"unit", nullptr}, {"source", "material"}},
        json{{"propertySet", "Pset_IfcEditorVisual"}, {"name", "Metallic"}, {"valueType", "IfcNormalisedRatioMeasure"}, {"value", visual.metallic}, {"unit", nullptr}, {"source", "material"}},
        json{{"propertySet", "Pset_IfcEditorVisual"}, {"name", "Roughness"}, {"valueType", "IfcNormalisedRatioMeasure"}, {"value", visual.roughness}, {"unit", nullptr}, {"source", "material"}}});
}

inline void replaceMaterialPropertySet(
    IfcParse::IfcFile& file,
    Base* material,
    const std::string& propertySetName,
    const json& properties) {
    Base* container = findMaterialPropertyContainer(file, material, propertySetName);
    std::map<std::string, json> desired;
    for (const auto& property : properties) {
        desired[property.at("name").get<std::string>()] = property;
    }

    if (container == nullptr) {
        if (desired.empty()) {
            return;
        }
        std::vector<Base*> propertyInstances;
        for (const auto& [name, property] : desired) {
            static_cast<void>(name);
            propertyInstances.push_back(createPropertySingleValue(file, property));
        }
        createMaterialPropertyContainer(file, material, propertySetName, propertyInstances);
        return;
    }

    const std::string aggregateAttribute = materialPropertiesAggregateAttribute(container);
    std::vector<Base*> next;
    std::vector<Base*> removed;
    std::set<std::string> updated;
    for (Base* property : aggregateToVector(getEntityAggregate(container, aggregateAttribute))) {
        if (!isA(property, "IfcPropertySingleValue")) {
            next.push_back(property);
            continue;
        }
        const std::string name = getString(property, "Name");
        const auto iterator = desired.find(name);
        if (iterator == desired.end()) {
            removed.push_back(property);
        } else {
            updatePropertySingleValue(file, property, iterator->second);
            next.push_back(property);
            updated.insert(name);
        }
    }
    for (const auto& [name, property] : desired) {
        if (updated.count(name) == 0) {
            next.push_back(createPropertySingleValue(file, property));
        }
    }

    if (next.empty()) {
        file.removeEntity(container);
    } else {
        setAttribute(container, aggregateAttribute, makeAggregate(next));
    }
    for (Base* property : removed) {
        removeIfUnreferenced(file, property);
    }
}

inline json readMaterialProperties(IfcParse::IfcFile& file, Base* material, bool includeInternal = false) {
    json result = json::array();
    for (Base* container : materialPropertyContainers(file, material)) {
        const std::string name = getString(container, "Name", container->declaration().name());
        if (!includeInternal && (name == "Pset_IfcEditorVisual" || name == "Pset_IfcEditorMaterial")) {
            continue;
        }
        for (Base* property : aggregateToVector(getEntityAggregate(container, materialPropertiesAggregateAttribute(container)))) {
            if (isA(property, "IfcPropertySingleValue")) {
                result.push_back(readPropertySingleValue(property, name, "material"));
            }
        }
    }
    return result;
}

inline std::optional<json> findProperty(const json& properties, const std::string& name) {
    for (const auto& property : properties) {
        if (property.at("name").get<std::string>() == name) {
            return property;
        }
    }
    return std::nullopt;
}

inline Visual readVisualMetadata(IfcParse::IfcFile& file, Base* material) {
    Visual visual;
    Base* container = findMaterialPropertyContainer(file, material, "Pset_IfcEditorVisual");
    if (container == nullptr) {
        return visual;
    }
    const json properties = readPropertiesFromSet(container, "material");
    const auto red = findProperty(properties, "ColorRed");
    const auto green = findProperty(properties, "ColorGreen");
    const auto blue = findProperty(properties, "ColorBlue");
    const auto opacity = findProperty(properties, "Opacity");
    const auto metallic = findProperty(properties, "Metallic");
    const auto roughness = findProperty(properties, "Roughness");
    if (red.has_value() && red->at("value").is_number()) {
        visual.color[0] = red->at("value").get<double>();
    }
    if (green.has_value() && green->at("value").is_number()) {
        visual.color[1] = green->at("value").get<double>();
    }
    if (blue.has_value() && blue->at("value").is_number()) {
        visual.color[2] = blue->at("value").get<double>();
    }
    if (opacity.has_value() && opacity->at("value").is_number()) {
        visual.opacity = opacity->at("value").get<double>();
    }
    if (metallic.has_value() && metallic->at("value").is_number()) {
        visual.metallic = metallic->at("value").get<double>();
    }
    if (roughness.has_value() && roughness->at("value").is_number()) {
        visual.roughness = roughness->at("value").get<double>();
    }
    return visual;
}

inline Base* createColourRgb(IfcParse::IfcFile& file, const Visual& visual) {
    Base* colour = createInstance(file, "IfcColourRgb");
    if (hasAttribute(colour, "Name")) {
        unsetAttribute(colour, "Name");
    }
    setAttribute(colour, "Red", visual.color[0]);
    setAttribute(colour, "Green", visual.color[1]);
    setAttribute(colour, "Blue", visual.color[2]);
    return colour;
}

inline Base* createSurfaceStyleRendering(IfcParse::IfcFile& file, const Visual& visual) {
    Base* rendering = createInstance(file, "IfcSurfaceStyleRendering");
    Base* colour = createColourRgb(file, visual);
    setEntityAttribute(rendering, "SurfaceColour", colour);
    if (hasAttribute(rendering, "Transparency")) {
        setAttribute(rendering, "Transparency", 1.0 - visual.opacity);
    }
    if (hasAttribute(rendering, "DiffuseColour")) {
        setEntityAttribute(rendering, "DiffuseColour", colour);
    }
    if (hasAttribute(rendering, "TransmissionColour")) {
        unsetAttribute(rendering, "TransmissionColour");
    }
    if (hasAttribute(rendering, "DiffuseTransmissionColour")) {
        unsetAttribute(rendering, "DiffuseTransmissionColour");
    }
    if (hasAttribute(rendering, "ReflectionColour")) {
        unsetAttribute(rendering, "ReflectionColour");
    }
    if (hasAttribute(rendering, "SpecularColour")) {
        Base* metallic = createIfcValue(file, "IfcNormalisedRatioMeasure", visual.metallic);
        setEntityAttribute(rendering, "SpecularColour", metallic);
    }
    if (hasAttribute(rendering, "SpecularHighlight")) {
        const std::string roughnessType = schemaSupports(file, "IfcSpecularRoughness") ?
            "IfcSpecularRoughness" : "IfcSpecularExponent";
        const double roughnessValue = roughnessType == "IfcSpecularRoughness" ? visual.roughness :
            std::max(1.0, (1.0 - visual.roughness) * 128.0);
        setEntityAttribute(rendering, "SpecularHighlight", createIfcValue(file, roughnessType, roughnessValue));
    }
    if (hasAttribute(rendering, "ReflectanceMethod")) {
        try {
            setEnumeration(rendering, "ReflectanceMethod", "PHYSICAL");
        } catch (const std::exception&) {
            try {
                setEnumeration(rendering, "ReflectanceMethod", "NOTDEFINED");
            } catch (const std::exception&) {
                unsetAttribute(rendering, "ReflectanceMethod");
            }
        }
    }
    return rendering;
}

inline Base* createSurfaceStyle(IfcParse::IfcFile& file, const std::string& name, const Visual& visual) {
    Base* surfaceStyle = createInstance(file, "IfcSurfaceStyle");
    setAttribute(surfaceStyle, "Name", name);
    setEnumeration(surfaceStyle, "Side", "BOTH");
    setAttribute(surfaceStyle, "Styles", makeAggregate({createSurfaceStyleRendering(file, visual)}));
    return surfaceStyle;
}

inline Base* createStyledItem(IfcParse::IfcFile& file, Base* item, Base* surfaceStyle) {
    Base* styleReference = surfaceStyle;
    if (schemaSupports(file, "IfcPresentationStyleAssignment")) {
        Base* assignment = createInstance(file, "IfcPresentationStyleAssignment");
        setAttribute(assignment, "Styles", makeAggregate({surfaceStyle}));
        styleReference = assignment;
    }
    Base* styledItem = createInstance(file, "IfcStyledItem");
    if (item == nullptr) {
        unsetAttribute(styledItem, "Item");
    } else {
        setEntityAttribute(styledItem, "Item", item);
    }
    setAttribute(styledItem, "Styles", makeAggregate({styleReference}));
    if (hasAttribute(styledItem, "Name")) {
        unsetAttribute(styledItem, "Name");
    }
    return styledItem;
}

inline Base* findMaterialRepresentationContext(IfcParse::IfcFile& file) {
    for (const std::string& className : {"IfcGeometricRepresentationSubContext", "IfcGeometricRepresentationContext"}) {
        for (Base* context : instancesByType(file, className)) {
            if (hasAttribute(context, "ContextType")) {
                const std::string contextType = getString(context, "ContextType");
                if (contextType.empty() || contextType == "Model") {
                    return context;
                }
            } else {
                return context;
            }
        }
    }
    throw SceneError("INVALID_RELATION", "The model does not contain a usable representation context");
}

inline Base* createMaterialStyleRepresentation(
    IfcParse::IfcFile& file,
    Base* material,
    const std::string& name,
    const Visual& visual) {
    Base* surfaceStyle = createSurfaceStyle(file, name, visual);
    Base* styledItem = createStyledItem(file, nullptr, surfaceStyle);
    Base* styledRepresentation = createInstance(file, "IfcStyledRepresentation");
    setEntityAttribute(styledRepresentation, "ContextOfItems", findMaterialRepresentationContext(file));
    if (hasAttribute(styledRepresentation, "RepresentationIdentifier")) {
        setAttribute(styledRepresentation, "RepresentationIdentifier", std::string("Style"));
    }
    if (hasAttribute(styledRepresentation, "RepresentationType")) {
        setAttribute(styledRepresentation, "RepresentationType", std::string("Material"));
    }
    setAttribute(styledRepresentation, "Items", makeAggregate({styledItem}));

    Base* definitionRepresentation = createInstance(file, "IfcMaterialDefinitionRepresentation");
    if (hasAttribute(definitionRepresentation, "Name")) {
        unsetAttribute(definitionRepresentation, "Name");
    }
    if (hasAttribute(definitionRepresentation, "Description")) {
        unsetAttribute(definitionRepresentation, "Description");
    }
    setAttribute(definitionRepresentation, "Representations", makeAggregate({styledRepresentation}));
    setEntityAttribute(definitionRepresentation, "RepresentedMaterial", material);
    return surfaceStyle;
}

inline Base* findMaterialSurfaceStyle(IfcParse::IfcFile& file, Base* material) {
    for (Base* definition : instancesByType(file, "IfcMaterialDefinitionRepresentation")) {
        if (getEntityAttribute(definition, "RepresentedMaterial") != material) {
            continue;
        }
        for (Base* representation : aggregateToVector(getEntityAggregate(definition, "Representations"))) {
            for (Base* item : aggregateToVector(getEntityAggregate(representation, "Items"))) {
                if (!isA(item, "IfcStyledItem")) {
                    continue;
                }
                for (Base* style : aggregateToVector(getEntityAggregate(item, "Styles"))) {
                    if (isA(style, "IfcSurfaceStyle")) {
                        return style;
                    }
                    if (isA(style, "IfcPresentationStyleAssignment")) {
                        for (Base* nested : aggregateToVector(getEntityAggregate(style, "Styles"))) {
                            if (isA(nested, "IfcSurfaceStyle")) {
                                return nested;
                            }
                        }
                    }
                }
            }
        }
    }
    return nullptr;
}

inline Base* findSurfaceRendering(Base* surfaceStyle) {
    if (surfaceStyle == nullptr) {
        return nullptr;
    }
    for (Base* style : aggregateToVector(getEntityAggregate(surfaceStyle, "Styles"))) {
        if (isA(style, "IfcSurfaceStyleRendering")) {
            return style;
        }
    }
    return nullptr;
}

inline void updateSurfaceStyleRendering(IfcParse::IfcFile& file, Base* material, const std::string& name, const Visual& visual) {
    Base* surfaceStyle = findMaterialSurfaceStyle(file, material);
    if (surfaceStyle == nullptr) {
        createMaterialStyleRepresentation(file, material, name, visual);
        return;
    }
    if (hasAttribute(surfaceStyle, "Name")) {
        setAttribute(surfaceStyle, "Name", name);
    }
    Base* previousRendering = findSurfaceRendering(surfaceStyle);
    Base* replacement = createSurfaceStyleRendering(file, visual);
    std::vector<Base*> styles;
    for (Base* style : aggregateToVector(getEntityAggregate(surfaceStyle, "Styles"))) {
        if (!isA(style, "IfcSurfaceStyleRendering")) {
            styles.push_back(style);
        }
    }
    styles.push_back(replacement);
    setAttribute(surfaceStyle, "Styles", makeAggregate(styles));
    removeIfUnreferenced(file, previousRendering);
}

inline std::string readMaterialCategory(IfcParse::IfcFile& file, Base* material) {
    if (hasAttribute(material, "Category")) {
        const auto category = getOptionalString(material, "Category");
        if (category.has_value()) {
            return *category;
        }
    }
    Base* metadata = findMaterialPropertyContainer(file, material, "Pset_IfcEditorMaterial");
    if (metadata != nullptr) {
        const json properties = readPropertiesFromSet(metadata, "material");
        const auto category = findProperty(properties, "Category");
        if (category.has_value() && category->at("value").is_string()) {
            return category->at("value").get<std::string>();
        }
    }
    return std::string();
}

inline void writeMaterialCategory(IfcParse::IfcFile& file, Base* material, const std::string& category) {
    if (hasAttribute(material, "Category")) {
        setAttribute(material, "Category", category);
    }
    const json properties = json::array({
        json{{"propertySet", "Pset_IfcEditorMaterial"}, {"name", "Category"}, {"valueType", "IfcLabel"}, {"value", category}, {"unit", nullptr}, {"source", "material"}},
        json{{"propertySet", "Pset_IfcEditorMaterial"}, {"name", "ManagedBy"}, {"valueType", "IfcLabel"}, {"value", "ifc-editor-jsonl"}, {"unit", nullptr}, {"source", "material"}}});
    replaceMaterialPropertySet(file, material, "Pset_IfcEditorMaterial", properties);
}

inline bool isEditorMaterial(IfcParse::IfcFile& file, Base* material) {
    Base* metadata = findMaterialPropertyContainer(file, material, "Pset_IfcEditorMaterial");
    if (metadata == nullptr) {
        return false;
    }
    const json properties = readPropertiesFromSet(metadata, "material");
    const auto managedBy = findProperty(properties, "ManagedBy");
    return managedBy.has_value() && managedBy->at("value") == "ifc-editor-jsonl";
}

class Material {
public:
    Material(std::string id, Base* entity, bool editable)
        : id_(std::move(id)), entity_(entity), editable_(editable) {
        if (entity_ == nullptr) {
            throw SceneError("INTERNAL_ERROR", "Material cannot wrap a null IFC entity");
        }
    }

    static std::unique_ptr<Material> create(
        IfcParse::IfcFile& file,
        std::string id,
        const std::string& name,
        const std::string& category,
        const Visual& visual,
        const json& properties) {
        validateVisual(visual);
        Base* entity = createInstance(file, "IfcMaterial");
        setAttribute(entity, "Name", name);
        if (hasAttribute(entity, "Description")) {
            unsetAttribute(entity, "Description");
        }
        writeMaterialCategory(file, entity, category);
        replaceMaterialPropertySet(file, entity, "Pset_IfcEditorVisual", internalVisualProperties(visual));
        createMaterialStyleRepresentation(file, entity, name, visual);

        std::map<std::string, std::vector<json>> sets;
        for (const auto& property : properties) {
            sets[property.at("propertySet").get<std::string>()].push_back(property);
        }
        for (const auto& [setName, values] : sets) {
            replaceMaterialPropertySet(file, entity, setName, json(values));
        }
        return std::unique_ptr<Material>(new Material(std::move(id), entity, true));
    }

    const std::string& id() const noexcept {
        return id_;
    }

    Base* entity() const noexcept {
        return entity_;
    }

    bool editable() const noexcept {
        return editable_;
    }

    std::optional<std::string> globalId() const {
        return globalIdOf(entity_);
    }

    std::string name() const {
        return getString(entity_, "Name", entity_->declaration().name());
    }

    std::string category(IfcParse::IfcFile& file) const {
        return isA(entity_, "IfcMaterial") ? readMaterialCategory(file, entity_) : entity_->declaration().name();
    }

    Visual visual(IfcParse::IfcFile& file) const {
        return isA(entity_, "IfcMaterial") ? readVisualMetadata(file, entity_) : Visual();
    }

    Base* surfaceStyle(IfcParse::IfcFile& file) const {
        return isA(entity_, "IfcMaterial") ? findMaterialSurfaceStyle(file, entity_) : nullptr;
    }

    void setName(IfcParse::IfcFile& file, const std::string& nameValue) {
        requireEditable();
        setAttribute(entity_, "Name", nameValue);
        Base* style = surfaceStyle(file);
        if (style != nullptr && hasAttribute(style, "Name")) {
            setAttribute(style, "Name", nameValue);
        }
    }

    void setCategory(IfcParse::IfcFile& file, const std::string& categoryValue) {
        requireEditable();
        writeMaterialCategory(file, entity_, categoryValue);
    }

    void setVisual(IfcParse::IfcFile& file, const Visual& visualValue) {
        requireEditable();
        validateVisual(visualValue);
        replaceMaterialPropertySet(file, entity_, "Pset_IfcEditorVisual", internalVisualProperties(visualValue));
        updateSurfaceStyleRendering(file, entity_, name(), visualValue);
    }

    void replaceProperties(IfcParse::IfcFile& file, const json& properties) {
        requireEditable();
        std::map<std::string, json> desiredSets;
        std::map<std::string, std::vector<json>> grouped;
        for (const auto& property : properties) {
            grouped[property.at("propertySet").get<std::string>()].push_back(property);
        }
        for (const auto& [setName, values] : grouped) {
            desiredSets[setName] = json(values);
        }

        std::vector<std::string> existingSets;
        for (Base* container : materialPropertyContainers(file, entity_)) {
            const std::string setName = getString(container, "Name");
            if (setName != "Pset_IfcEditorVisual" && setName != "Pset_IfcEditorMaterial") {
                existingSets.push_back(setName);
            }
        }
        for (const std::string& setName : existingSets) {
            if (desiredSets.count(setName) == 0) {
                replaceMaterialPropertySet(file, entity_, setName, json::array());
            }
        }
        for (const auto& [setName, values] : desiredSets) {
            replaceMaterialPropertySet(file, entity_, setName, values);
        }
    }

    json toJson(IfcParse::IfcFile& file, const EntityResolver& resolver = EntityResolver()) const {
        const auto global = globalId();
        return json{
            {"id", id_},
            {"ifcEntityId", entity_->id()},
            {"globalId", global.has_value() ? json(*global) : json(nullptr)},
            {"name", name()},
            {"category", category(file)},
            {"editable", editable_},
            {"visual", visualToJson(visual(file))},
            {"properties", isA(entity_, "IfcMaterial") ? readMaterialProperties(file, entity_) : json::array()},
            {"definition", buildDefinition(
                entity_,
                {"Name", "Description", "Category"},
                resolver)}};
    }

    void deleteFromIfc(IfcParse::IfcFile& file) {
        std::vector<Base*> propertyContainers = materialPropertyContainers(file, entity_);
        std::vector<Base*> propertyEntities;
        for (Base* container : propertyContainers) {
            const auto properties = aggregateToVector(getEntityAggregate(container, materialPropertiesAggregateAttribute(container)));
            propertyEntities.insert(propertyEntities.end(), properties.begin(), properties.end());
        }

        std::vector<Base*> definitionRepresentations;
        std::vector<Base*> representations;
        std::vector<Base*> styledItems;
        std::vector<Base*> surfaceStyles;
        std::vector<Base*> renderingStyles;
        std::vector<Base*> renderingChildren;
        for (Base* definition : instancesByType(file, "IfcMaterialDefinitionRepresentation")) {
            if (getEntityAttribute(definition, "RepresentedMaterial") != entity_) {
                continue;
            }
            definitionRepresentations.push_back(definition);
            for (Base* representation : aggregateToVector(getEntityAggregate(definition, "Representations"))) {
                representations.push_back(representation);
                for (Base* item : aggregateToVector(getEntityAggregate(representation, "Items"))) {
                    styledItems.push_back(item);
                    for (Base* style : aggregateToVector(getEntityAggregate(item, "Styles"))) {
                        if (isA(style, "IfcSurfaceStyle")) {
                            surfaceStyles.push_back(style);
                            for (Base* rendering : aggregateToVector(getEntityAggregate(style, "Styles"))) {
                                renderingStyles.push_back(rendering);
                                for (const std::string& attribute : {
                                         "SurfaceColour",
                                         "DiffuseColour",
                                         "TransmissionColour",
                                         "DiffuseTransmissionColour",
                                         "ReflectionColour",
                                         "SpecularColour",
                                         "SpecularHighlight"}) {
                                    Base* child = getEntityAttribute(rendering, attribute);
                                    if (child != nullptr) {
                                        renderingChildren.push_back(child);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        file.removeEntity(entity_);
        removeUniqueIfUnreferenced(file, propertyContainers);
        removeUniqueIfUnreferenced(file, propertyEntities);
        removeUniqueIfUnreferenced(file, definitionRepresentations);
        removeUniqueIfUnreferenced(file, representations);
        removeUniqueIfUnreferenced(file, styledItems);
        removeUniqueIfUnreferenced(file, surfaceStyles);
        removeUniqueIfUnreferenced(file, renderingStyles);
        removeUniqueIfUnreferenced(file, renderingChildren);
    }

private:
    void requireEditable() const {
        if (!editable_) {
            throw SceneError("MATERIAL_NOT_EDITABLE", "Material is not editable", json{{"id", id_}});
        }
    }

    std::string id_;
    Base* entity_;
    bool editable_;
};

} // namespace ifc_editor