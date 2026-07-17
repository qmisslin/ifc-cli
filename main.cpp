#include <ifcgeom/Iterator.h>
#include <ifcgeom/hybrid_kernel.h>
#include <ifcparse/IfcFile.h>
#include <ifcparse/IfcGlobalId.h>
#include <ifcparse/IfcLogger.h>
#include <ifcparse/IfcParse.h>
#include <ifcparse/utils.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#endif

using json = nlohmann::json;
using IfcObject = IfcUtil::IfcBaseClass;
using IfcEntity = IfcUtil::IfcBaseEntity;
using Matrix = std::array<double, 16>;

class IfcEditor {
public:
    json execute(const json& request) {
        const std::string action =
            request.at("action").get<std::string>();

        if (action == "load") {
            return load(request.at("path").get<std::string>());
        }

        if (action == "tree") {
            return tree();
        }

        if (action == "get") {
            return get(
                request.at("id").get<int>(),
                request.value("mesh", false)
            );
        }

        if (action == "primitive") {
            return primitive(request);
        }

        if (action == "delete") {
            return deleteElement(
                request.at("id").get<int>()
            );
        }

        if (action == "update") {
            return updateElement(request);
        }

        if (action == "save") {
            return save(
                request.value("path", sourcePath_)
            );
        }

        if (action == "help") {
            return help();
        }

        if (action == "exit") {
            return {{"status", "bye"}};
        }

        throw std::runtime_error(
            "Unknown action: " + action
        );
    }

private:
    struct ReferencePatch {
        IfcEntity* owner{};
        size_t index{};
        IfcUtil::ArgumentType type{
            IfcUtil::Argument_UNKNOWN
        };
        std::vector<IfcObject*> values;
        std::vector<std::vector<IfcObject*>>
            nestedValues;
    };

    std::unique_ptr<IfcParse::IfcFile> file_;
    std::string sourcePath_;

    static Matrix identity() {
        return {
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            0, 0, 0, 1
        };
    }

    static Matrix multiply(
        const Matrix& a,
        const Matrix& b
    ) {
        Matrix result{};

        for (int column = 0; column < 4; ++column) {
            for (int row = 0; row < 4; ++row) {
                for (int k = 0; k < 4; ++k) {
                    result[column * 4 + row] +=
                        a[k * 4 + row] *
                        b[column * 4 + k];
                }
            }
        }

        return result;
    }

    static std::array<double, 3> normalize(
        std::array<double, 3> value
    ) {
        const double length = std::sqrt(
            value[0] * value[0] +
            value[1] * value[1] +
            value[2] * value[2]
        );

        if (length < 1e-12) {
            throw std::runtime_error(
                "Transform contains a zero-length axis"
            );
        }

        for (double& component : value) {
            component /= length;
        }

        return value;
    }

    static std::array<double, 3> cross(
        const std::array<double, 3>& a,
        const std::array<double, 3>& b
    ) {
        return {
            a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]
        };
    }

    void requireFile() const {
        if (!file_) {
            throw std::runtime_error(
                "No IFC file is loaded"
            );
        }
    }

    IfcEntity* entityById(int id) const {
        requireFile();

        IfcObject* object =
            file_->instance_by_id(id);

        IfcEntity* entity =
            object
                ? object->as<IfcEntity>()
                : nullptr;

        if (!entity) {
            throw std::runtime_error(
                "IFC entity not found: #" +
                std::to_string(id)
            );
        }

        return entity;
    }

    static ptrdiff_t attributeIndex(
        const IfcObject* object,
        const std::string& name
    ) {
        const IfcParse::entity* declaration =
            object
                ? object->declaration().as_entity()
                : nullptr;

        return declaration
            ? declaration->attribute_index(name)
            : -1;
    }

    static std::string stringAttribute(
        const IfcObject* object,
        const std::string& name
    ) {
        const ptrdiff_t index =
            attributeIndex(object, name);

        if (index < 0) {
            return {};
        }

        const AttributeValue value =
            object->get_attribute_value(
                static_cast<size_t>(index)
            );

        if (value.isNull()) {
            return {};
        }

        if (value.type() ==
                IfcUtil::Argument_STRING ||
            value.type() ==
                IfcUtil::Argument_ENUMERATION) {
            return static_cast<std::string>(value);
        }

        return {};
    }

    static IfcObject* referenceAttribute(
        const IfcObject* object,
        const std::string& name
    ) {
        const ptrdiff_t index =
            attributeIndex(object, name);

        if (index < 0) {
            return nullptr;
        }

        const AttributeValue value =
            object->get_attribute_value(
                static_cast<size_t>(index)
            );

        if (value.isNull() ||
            value.type() !=
                IfcUtil::Argument_ENTITY_INSTANCE) {
            return nullptr;
        }

        return static_cast<IfcObject*>(value);
    }

    static aggregate_of_instance::ptr
    referencesAttribute(
        const IfcObject* object,
        const std::string& name
    ) {
        const ptrdiff_t index =
            attributeIndex(object, name);

        if (index < 0) {
            return aggregate_of_instance::ptr(
                new aggregate_of_instance()
            );
        }

        const AttributeValue value =
            object->get_attribute_value(
                static_cast<size_t>(index)
            );

        if (value.isNull() ||
            value.type() !=
                IfcUtil::
                    Argument_AGGREGATE_OF_ENTITY_INSTANCE) {
            return aggregate_of_instance::ptr(
                new aggregate_of_instance()
            );
        }

        return static_cast<
            aggregate_of_instance::ptr
        >(value);
    }

    static std::vector<double> doublesAttribute(
        const IfcObject* object,
        const std::string& name
    ) {
        const ptrdiff_t index =
            attributeIndex(object, name);

        if (index < 0) {
            return {};
        }

        const AttributeValue value =
            object->get_attribute_value(
                static_cast<size_t>(index)
            );

        if (value.isNull() ||
            value.type() !=
                IfcUtil::
                    Argument_AGGREGATE_OF_DOUBLE) {
            return {};
        }

        return static_cast<
            std::vector<double>
        >(value);
    }

    template <typename T>
    static void setValue(
        IfcObject* object,
        const std::string& name,
        const T& value
    ) {
        const ptrdiff_t index =
            attributeIndex(object, name);

        if (index >= 0) {
            object->set_attribute_value(
                static_cast<size_t>(index),
                value
            );
        }
    }

    static void setReference(
        IfcObject* object,
        const std::string& name,
        IfcObject* value
    ) {
        const ptrdiff_t index =
            attributeIndex(object, name);

        if (index >= 0) {
            object->set_attribute_value(
                static_cast<size_t>(index),
                value
            );
        }
    }

    static void setEnumeration(
        IfcObject* object,
        const std::string& name,
        const std::string& value
    ) {
        const ptrdiff_t index =
            attributeIndex(object, name);

        if (index < 0) {
            return;
        }

        const auto* declaredAttribute =
            object
                ->declaration()
                .as_entity()
                ->attribute_by_index(
                    static_cast<size_t>(index)
                );

        const auto* namedType =
            declaredAttribute
                ->type_of_attribute()
                ->as_named_type();

        const auto* enumeration =
            namedType
                ? namedType
                    ->declared_type()
                    ->as_enumeration_type()
                : nullptr;

        if (!enumeration) {
            throw std::runtime_error(
                name + " is not an enumeration"
            );
        }

        std::string keyword = value;

        std::transform(
            keyword.begin(),
            keyword.end(),
            keyword.begin(),
            [](unsigned char character) {
                return static_cast<char>(
                    std::toupper(character)
                );
            }
        );

        object->set_attribute_value(
            static_cast<size_t>(index),
            EnumerationReference(
                enumeration,
                enumeration->lookup_enum_offset(
                    keyword
                )
            )
        );
    }

    static IfcUtil::ArgumentType expectedType(
        const IfcObject* object,
        size_t index
    ) {
        const auto* declaration =
            object->declaration().as_entity();

        if (!declaration) {
            return IfcUtil::Argument_UNKNOWN;
        }

        if (declaration->derived()[index]) {
            return IfcUtil::Argument_DERIVED;
        }

        return IfcUtil::from_parameter_type(
            declaration
                ->attribute_by_index(index)
                ->type_of_attribute()
        );
    }

    static void copyValue(
        IfcObject* destination,
        size_t index,
        const AttributeValue& value
    ) {
        if (value.isNull() ||
            value.type() ==
                IfcUtil::Argument_DERIVED) {
            return;
        }

        switch (value.type()) {
        case IfcUtil::Argument_INT:
            destination->set_attribute_value(
                index,
                static_cast<int>(value)
            );
            break;

        case IfcUtil::Argument_BOOL:
            destination->set_attribute_value(
                index,
                static_cast<bool>(value)
            );
            break;

        case IfcUtil::Argument_LOGICAL: {
            const boost::logic::tribool logical =
                value.operator boost::logic::tribool();

            destination->set_attribute_value(index, logical);
            break;
        }

        case IfcUtil::Argument_DOUBLE:
            destination->set_attribute_value(
                index,
                static_cast<double>(value)
            );
            break;

        case IfcUtil::Argument_STRING:
            destination->set_attribute_value(
                index,
                static_cast<std::string>(value)
            );
            break;

        case IfcUtil::Argument_BINARY:
            destination->set_attribute_value(
                index,
                static_cast<
                    boost::dynamic_bitset<>
                >(value)
            );
            break;

        case IfcUtil::Argument_ENUMERATION: {
            const std::string name =
                destination
                    ->declaration()
                    .as_entity()
                    ->attribute_by_index(index)
                    ->name();

            setEnumeration(
                destination,
                name,
                static_cast<std::string>(value)
            );
            break;
        }

        case IfcUtil::Argument_ENTITY_INSTANCE:
            destination->set_attribute_value(
                index,
                static_cast<IfcObject*>(value)
            );
            break;

        case IfcUtil::Argument_EMPTY_AGGREGATE:
            destination->set_attribute_value(
                index,
                empty_aggregate_t{}
            );
            break;

        case IfcUtil::Argument_AGGREGATE_OF_INT:
            destination->set_attribute_value(
                index,
                static_cast<
                    std::vector<int>
                >(value)
            );
            break;

        case IfcUtil::Argument_AGGREGATE_OF_DOUBLE:
            destination->set_attribute_value(
                index,
                static_cast<
                    std::vector<double>
                >(value)
            );
            break;

        case IfcUtil::Argument_AGGREGATE_OF_STRING:
            destination->set_attribute_value(
                index,
                static_cast<
                    std::vector<std::string>
                >(value)
            );
            break;

        case IfcUtil::Argument_AGGREGATE_OF_BINARY:
            destination->set_attribute_value(
                index,
                static_cast<
                    std::vector<
                        boost::dynamic_bitset<>
                    >
                >(value)
            );
            break;

        case IfcUtil::
            Argument_AGGREGATE_OF_ENTITY_INSTANCE:
            destination->set_attribute_value(
                index,
                static_cast<
                    aggregate_of_instance::ptr
                >(value)
            );
            break;

        case IfcUtil::
            Argument_AGGREGATE_OF_EMPTY_AGGREGATE:
            destination->set_attribute_value(
                index,
                empty_aggregate_of_aggregate_t{}
            );
            break;

        case IfcUtil::
            Argument_AGGREGATE_OF_AGGREGATE_OF_INT:
            destination->set_attribute_value(
                index,
                static_cast<
                    std::vector<std::vector<int>>
                >(value)
            );
            break;

        case IfcUtil::
            Argument_AGGREGATE_OF_AGGREGATE_OF_DOUBLE:
            destination->set_attribute_value(
                index,
                static_cast<
                    std::vector<
                        std::vector<double>
                    >
                >(value)
            );
            break;

        case IfcUtil::
            Argument_AGGREGATE_OF_AGGREGATE_OF_ENTITY_INSTANCE:
            destination->set_attribute_value(
                index,
                static_cast<
                    aggregate_of_aggregate_of_instance::ptr
                >(value)
            );
            break;

        default:
            break;
        }
    }

    static void setJsonAttribute(
        IfcEntity* object,
        const std::string& name,
        const json& value
    ) {
        const ptrdiff_t rawIndex =
            attributeIndex(object, name);

        if (rawIndex < 0) {
            throw std::runtime_error(
                object->declaration().name() +
                " has no attribute named " +
                name
            );
        }

        const size_t index =
            static_cast<size_t>(rawIndex);

        if (value.is_null()) {
            object->set_attribute_value(
                index,
                Blank{}
            );
            return;
        }

        switch (expectedType(object, index)) {
        case IfcUtil::Argument_INT:
            object->set_attribute_value(
                index,
                value.get<int>()
            );
            break;

        case IfcUtil::Argument_BOOL:
            object->set_attribute_value(
                index,
                value.get<bool>()
            );
            break;

        case IfcUtil::Argument_LOGICAL:
            object->set_attribute_value(
                index,
                boost::logic::tribool(
                    value.get<bool>()
                )
            );
            break;

        case IfcUtil::Argument_DOUBLE:
            object->set_attribute_value(
                index,
                value.get<double>()
            );
            break;

        case IfcUtil::Argument_STRING:
            object->set_attribute_value(
                index,
                value.get<std::string>()
            );
            break;

        case IfcUtil::Argument_ENUMERATION:
            setEnumeration(
                object,
                name,
                value.get<std::string>()
            );
            break;

        case IfcUtil::Argument_ENTITY_INSTANCE: {
            const int id =
                value.is_object()
                    ? value.at("id").get<int>()
                    : value.get<int>();

            IfcObject* reference =
                object
                    ->file_
                    ->instance_by_id(id);

            if (!reference) {
                throw std::runtime_error(
                    "Referenced IFC entity not found: #" +
                    std::to_string(id)
                );
            }

            object->set_attribute_value(
                index,
                reference
            );
            break;
        }

        default:
            throw std::runtime_error(
                "Attribute " + name +
                " is not a supported scalar attribute"
            );
        }
    }

    IfcObject* create(
        const std::string& type
    ) {
        return file_->create(
            file_
                ->schema()
                ->declaration_by_name(type)
        );
    }

    aggregate_of_instance::ptr instances(
        const std::string& type
    ) const {
        try {
            auto result =
                file_->instances_by_type(type);

            return result
                ? result
                : aggregate_of_instance::ptr(
                    new aggregate_of_instance()
                );
        } catch (...) {
            return aggregate_of_instance::ptr(
                new aggregate_of_instance()
            );
        }
    }

    std::vector<ReferencePatch> captureReferences(
        IfcObject* source
    ) const {
        std::vector<ReferencePatch> patches;

        const auto references =
            file_->instances_by_reference(
                static_cast<int>(source->id())
            );

        if (!references) {
            return patches;
        }

        for (auto iterator = references->begin();
             iterator != references->end();
             ++iterator) {
            IfcEntity* owner =
                (*iterator)->as<IfcEntity>();

            if (!owner || owner == source) {
                continue;
            }

            const size_t count =
                owner
                    ->declaration()
                    .as_entity()
                    ->attribute_count();

            for (size_t index = 0;
                 index < count;
                 ++index) {
                const AttributeValue value =
                    owner->get_attribute_value(index);

                if (value.isNull()) {
                    continue;
                }

                ReferencePatch patch{
                    owner,
                    index,
                    value.type(),
                    {},
                    {}
                };

                if (value.type() ==
                    IfcUtil::Argument_ENTITY_INSTANCE) {
                    if (static_cast<IfcObject*>(value) ==
                        source) {
                        patches.push_back(
                            std::move(patch)
                        );
                    }
                } else if (
                    value.type() ==
                    IfcUtil::
                        Argument_AGGREGATE_OF_ENTITY_INSTANCE
                ) {
                    const auto list =
                        static_cast<
                            aggregate_of_instance::ptr
                        >(value);

                    if (!list ||
                        !list->contains(source)) {
                        continue;
                    }

                    for (auto item = list->begin();
                         item != list->end();
                         ++item) {
                        patch.values.push_back(
                            *item == source
                                ? nullptr
                                : *item
                        );
                    }

                    patches.push_back(
                        std::move(patch)
                    );
                } else if (
                    value.type() ==
                    IfcUtil::
                        Argument_AGGREGATE_OF_AGGREGATE_OF_ENTITY_INSTANCE
                ) {
                    const auto lists =
                        static_cast<
                            aggregate_of_aggregate_of_instance::ptr
                        >(value);

                    if (!lists ||
                        !lists->contains(source)) {
                        continue;
                    }

                    for (auto group = lists->begin();
                         group != lists->end();
                         ++group) {
                        std::vector<IfcObject*> copied;

                        for (IfcObject* item : *group) {
                            copied.push_back(
                                item == source
                                    ? nullptr
                                    : item
                            );
                        }

                        patch.nestedValues.push_back(
                            std::move(copied)
                        );
                    }

                    patches.push_back(
                        std::move(patch)
                    );
                }
            }
        }

        return patches;
    }

    static void restoreReferences(
        const std::vector<ReferencePatch>& patches,
        IfcObject* replacement
    ) {
        for (const ReferencePatch& patch : patches) {
            if (patch.type ==
                IfcUtil::Argument_ENTITY_INSTANCE) {
                patch.owner->set_attribute_value(
                    patch.index,
                    replacement
                );
            } else if (
                patch.type ==
                IfcUtil::
                    Argument_AGGREGATE_OF_ENTITY_INSTANCE
            ) {
                aggregate_of_instance::ptr list(
                    new aggregate_of_instance()
                );

                for (IfcObject* item : patch.values) {
                    list->push(
                        item ? item : replacement
                    );
                }

                patch.owner->set_attribute_value(
                    patch.index,
                    list
                );
            } else if (
                patch.type ==
                IfcUtil::
                    Argument_AGGREGATE_OF_AGGREGATE_OF_ENTITY_INSTANCE
            ) {
                aggregate_of_aggregate_of_instance::ptr
                    lists(
                        new
                            aggregate_of_aggregate_of_instance()
                    );

                for (const auto& group :
                     patch.nestedValues) {
                    std::vector<IfcObject*> copied;

                    for (IfcObject* item : group) {
                        copied.push_back(
                            item ? item : replacement
                        );
                    }

                    lists->push(copied);
                }

                patch.owner->set_attribute_value(
                    patch.index,
                    lists
                );
            }
        }
    }

    std::pair<std::string, std::string>
    resolveClass(
        const std::string& requested
    ) const {
        std::string key = requested;

        std::transform(
            key.begin(),
            key.end(),
            key.begin(),
            [](unsigned char character) {
                return static_cast<char>(
                    std::tolower(character)
                );
            }
        );

        static const std::map<
            std::string,
            std::pair<std::string, std::string>
        > aliases = {
            {
                "proxy",
                {
                    "IfcBuildingElementProxy",
                    "NOTDEFINED"
                }
            },
            {
                "wall",
                {"IfcWall", "NOTDEFINED"}
            },
            {
                "slab",
                {"IfcSlab", "NOTDEFINED"}
            },
            {
                "floor",
                {"IfcSlab", "FLOOR"}
            },
            {
                "ceiling",
                {"IfcCovering", "CEILING"}
            },
            {
                "roof",
                {"IfcRoof", "NOTDEFINED"}
            },
            {
                "column",
                {"IfcColumn", "NOTDEFINED"}
            },
            {
                "beam",
                {"IfcBeam", "NOTDEFINED"}
            },
            {
                "door",
                {"IfcDoor", "NOTDEFINED"}
            },
            {
                "window",
                {"IfcWindow", "NOTDEFINED"}
            },
            {
                "stair",
                {"IfcStair", "NOTDEFINED"}
            },
            {
                "railing",
                {"IfcRailing", "NOTDEFINED"}
            },
            {
                "furniture",
                {"IfcFurniture", "NOTDEFINED"}
            }
        };

        const auto alias = aliases.find(key);

        return alias != aliases.end()
            ? alias->second
            : std::make_pair(
                requested,
                std::string()
            );
    }

    IfcEntity* changeClass(
        IfcEntity* source,
        const std::string& requestedClass
    ) {
        if (!source
                ->declaration()
                .is("IfcElement")) {
            throw std::runtime_error(
                "Class changes are limited to IfcElement instances"
            );
        }

        const auto resolved =
            resolveClass(requestedClass);

        const IfcParse::declaration* declaration =
            file_
                ->schema()
                ->declaration_by_name(
                    resolved.first
                );

        const IfcParse::entity* entityDeclaration =
            declaration
                ? declaration->as_entity()
                : nullptr;

        if (!entityDeclaration ||
            entityDeclaration->is_abstract() ||
            !declaration->is("IfcElement")) {
            throw std::runtime_error(
                resolved.first +
                " is not a concrete IfcElement class in this schema"
            );
        }

        if (&source->declaration() == declaration) {
            if (!resolved.second.empty() &&
                attributeIndex(
                    source,
                    "PredefinedType"
                ) >= 0) {
                setEnumeration(
                    source,
                    "PredefinedType",
                    resolved.second
                );
            }

            return source;
        }

        IfcObject* detached =
            file_
                ->schema()
                ->instantiate(
                    declaration,
                    in_memory_attribute_storage(
                        entityDeclaration
                            ->attribute_count()
                    )
                );

        const auto destinationAttributes =
            entityDeclaration->all_attributes();

        for (size_t destinationIndex = 0;
             destinationIndex <
                 destinationAttributes.size();
             ++destinationIndex) {
            const std::string attributeName =
                destinationAttributes[
                    destinationIndex
                ]->name();

            if (attributeName ==
                "PredefinedType") {
                continue;
            }

            const ptrdiff_t sourceIndex =
                attributeIndex(
                    source,
                    attributeName
                );

            if (sourceIndex < 0) {
                continue;
            }

            const AttributeValue value =
                source->get_attribute_value(
                    static_cast<size_t>(
                        sourceIndex
                    )
                );

            const IfcUtil::ArgumentType
                destinationType =
                    expectedType(
                        detached,
                        destinationIndex
                    );

            if (value.isNull() ||
                destinationType != value.type()) {
                continue;
            }

            copyValue(
                detached,
                destinationIndex,
                value
            );
        }

        if (!resolved.second.empty() &&
            attributeIndex(
                detached,
                "PredefinedType"
            ) >= 0) {
            setEnumeration(
                detached,
                "PredefinedType",
                resolved.second
            );
        } else if (
            attributeIndex(
                detached,
                "PredefinedType"
            ) >= 0
        ) {
            const size_t predefinedIndex =
                static_cast<size_t>(
                    attributeIndex(
                        detached,
                        "PredefinedType"
                    )
                );

            if (detached
                    ->get_attribute_value(
                        predefinedIndex
                    )
                    .isNull()) {
                try {
                    setEnumeration(
                        detached,
                        "PredefinedType",
                        "NOTDEFINED"
                    );
                } catch (...) {
                }
            }
        }

        const int id =
            static_cast<int>(source->id());

        const auto patches =
            captureReferences(source);

        file_->removeEntity(source);

        IfcObject* replacement =
            file_->addEntity(detached, id);

        restoreReferences(
            patches,
            replacement
        );

        return replacement->as<IfcEntity>();
    }

    static json objectReference(
        const IfcObject* object
    ) {
        if (!object) {
            return nullptr;
        }

        if (!object->declaration().as_entity()) {
            std::unordered_set<uint32_t> path;

            return {
                {
                    "type",
                    object->declaration().name()
                },
                {
                    "value",
                    valueToJson(
                        object->get_attribute_value(0),
                        0,
                        path
                    )
                }
            };
        }

        return {
            {"id", object->id()},
            {"type", object->declaration().name()}
        };
    }

    static json entityToJson(
        const IfcObject* object,
        int depth,
        std::unordered_set<uint32_t>& path
    ) {
        if (!object) {
            return nullptr;
        }

        if (!object->declaration().as_entity()) {
            return objectReference(object);
        }

        json result = objectReference(object);

        if (depth <= 0 ||
            (
                object->id() != 0 &&
                path.count(object->id())
            )) {
            return result;
        }

        if (object->id() != 0) {
            path.insert(object->id());
        }

        json attributes = json::object();

        const auto allAttributes =
            object
                ->declaration()
                .as_entity()
                ->all_attributes();

        for (size_t index = 0;
             index < allAttributes.size();
             ++index) {
            attributes[
                allAttributes[index]->name()
            ] = valueToJson(
                object->get_attribute_value(index),
                depth - 1,
                path
            );
        }

        result["attributes"] =
            std::move(attributes);

        if (object->id() != 0) {
            path.erase(object->id());
        }

        return result;
    }

    static json valueToJson(
        const AttributeValue& value,
        int depth,
        std::unordered_set<uint32_t>& path
    ) {
        if (value.isNull()) {
            return nullptr;
        }

        switch (value.type()) {
        case IfcUtil::Argument_DERIVED:
            return "*";

        case IfcUtil::Argument_INT:
            return static_cast<int>(value);

        case IfcUtil::Argument_BOOL:
            return static_cast<bool>(value);

        case IfcUtil::Argument_LOGICAL: {
            const boost::logic::tribool logical =
                value.operator boost::logic::tribool();

            if (boost::logic::indeterminate(logical)) {
                return "unknown";
            }

            return logical
                ? json(true)
                : json(false);
        }

        case IfcUtil::Argument_DOUBLE:
            return static_cast<double>(value);

        case IfcUtil::Argument_STRING:
        case IfcUtil::Argument_ENUMERATION:
            return static_cast<std::string>(value);

        case IfcUtil::Argument_BINARY: {
            std::string text;

            boost::to_string(
                static_cast<
                    boost::dynamic_bitset<>
                >(value),
                text
            );

            return text;
        }

        case IfcUtil::Argument_ENTITY_INSTANCE:
            return entityToJson(
                static_cast<IfcObject*>(value),
                depth,
                path
            );

        case IfcUtil::Argument_AGGREGATE_OF_INT:
            return static_cast<
                std::vector<int>
            >(value);

        case IfcUtil::Argument_AGGREGATE_OF_DOUBLE:
            return static_cast<
                std::vector<double>
            >(value);

        case IfcUtil::Argument_AGGREGATE_OF_STRING:
            return static_cast<
                std::vector<std::string>
            >(value);

        case IfcUtil::Argument_AGGREGATE_OF_BINARY: {
            json result = json::array();

            const auto binaries =
                static_cast<
                    std::vector<
                        boost::dynamic_bitset<>
                    >
                >(value);

            for (const auto& bits : binaries) {
                std::string text;
                boost::to_string(bits, text);
                result.push_back(text);
            }

            return result;
        }

        case IfcUtil::
            Argument_AGGREGATE_OF_ENTITY_INSTANCE: {
            json result = json::array();

            const auto values =
                static_cast<
                    aggregate_of_instance::ptr
                >(value);

            if (values) {
                for (auto iterator =
                         values->begin();
                     iterator != values->end();
                     ++iterator) {
                    result.push_back(
                        entityToJson(
                            *iterator,
                            depth,
                            path
                        )
                    );
                }
            }

            return result;
        }

        case IfcUtil::
            Argument_AGGREGATE_OF_AGGREGATE_OF_INT:
            return static_cast<
                std::vector<std::vector<int>>
            >(value);

        case IfcUtil::
            Argument_AGGREGATE_OF_AGGREGATE_OF_DOUBLE:
            return static_cast<
                std::vector<std::vector<double>>
            >(value);

        case IfcUtil::
            Argument_AGGREGATE_OF_AGGREGATE_OF_ENTITY_INSTANCE: {
            json outer = json::array();

            const auto values =
                static_cast<
                    aggregate_of_aggregate_of_instance::ptr
                >(value);

            if (values) {
                for (auto group = values->begin();
                     group != values->end();
                     ++group) {
                    json inner = json::array();

                    for (IfcObject* object : *group) {
                        inner.push_back(
                            entityToJson(
                                object,
                                depth,
                                path
                            )
                        );
                    }

                    outer.push_back(
                        std::move(inner)
                    );
                }
            }

            return outer;
        }

        case IfcUtil::Argument_EMPTY_AGGREGATE:
        case IfcUtil::
            Argument_AGGREGATE_OF_EMPTY_AGGREGATE:
            return json::array();

        default:
            return nullptr;
        }
    }

    Matrix axisPlacementMatrix(
        const IfcObject* placement
    ) const {
        if (!placement) {
            return identity();
        }

        const std::vector<double> coordinates =
            doublesAttribute(
                referenceAttribute(
                    placement,
                    "Location"
                ),
                "Coordinates"
            );

        std::array<double, 3> origin{0, 0, 0};

        for (size_t index = 0;
             index < std::min<size_t>(
                 3,
                 coordinates.size()
             );
             ++index) {
            origin[index] = coordinates[index];
        }

        std::array<double, 3> z{0, 0, 1};
        std::array<double, 3> x{1, 0, 0};

        const std::vector<double> axis =
            doublesAttribute(
                referenceAttribute(
                    placement,
                    "Axis"
                ),
                "DirectionRatios"
            );

        const std::vector<double> reference =
            doublesAttribute(
                referenceAttribute(
                    placement,
                    "RefDirection"
                ),
                "DirectionRatios"
            );

        if (axis.size() >= 3) {
            z = {axis[0], axis[1], axis[2]};
        }

        if (reference.size() >= 2) {
            x = {
                reference[0],
                reference[1],
                reference.size() >= 3
                    ? reference[2]
                    : 0.0
            };
        }

        z = normalize(z);

        std::array<double, 3> y =
            normalize(cross(z, x));

        x = normalize(cross(y, z));

        Matrix result = identity();

        for (int index = 0; index < 3; ++index) {
            result[index] = x[index];
            result[4 + index] = y[index];
            result[8 + index] = z[index];
            result[12 + index] = origin[index];
        }

        return result;
    }

    Matrix placementMatrix(
        const IfcObject* placement,
        std::unordered_set<uint32_t>& path
    ) const {
        if (!placement ||
            !placement
                ->declaration()
                .is("IfcLocalPlacement") ||
            path.count(placement->id())) {
            return identity();
        }

        path.insert(placement->id());

        const Matrix parent =
            placementMatrix(
                referenceAttribute(
                    placement,
                    "PlacementRelTo"
                ),
                path
            );

        const Matrix local =
            axisPlacementMatrix(
                referenceAttribute(
                    placement,
                    "RelativePlacement"
                )
            );

        path.erase(placement->id());

        return multiply(parent, local);
    }

    Matrix objectMatrix(
        const IfcObject* object
    ) const {
        std::unordered_set<uint32_t> path;

        return placementMatrix(
            referenceAttribute(
                object,
                "ObjectPlacement"
            ),
            path
        );
    }

    json load(const std::string& path) {
        auto candidate =
            std::make_unique<IfcParse::IfcFile>(
                path
            );

        if (!candidate->good()) {
            throw std::runtime_error(
                "IfcOpenShell could not load the IFC file: " +
                path
            );
        }

        file_ = std::move(candidate);
        sourcePath_ = path;

        return {
            {"path", path},
            {"schema", file_->schema()->name()},
            {
                "entityCount",
                static_cast<int>(
                    std::distance(
                        file_->begin(),
                        file_->end()
                    )
                )
            }
        };
    }

    json tree() const {
        requireFile();

        std::map<int, std::set<int>> parents;
        std::map<int, std::set<int>> children;

        const auto addEdge = [&](
            IfcObject* parent,
            IfcObject* child
        ) {
            if (!parent || !child) {
                return;
            }

            parents[
                static_cast<int>(child->id())
            ].insert(
                static_cast<int>(parent->id())
            );

            children[
                static_cast<int>(parent->id())
            ].insert(
                static_cast<int>(child->id())
            );
        };

        const auto addListRelation = [&](
            const std::string& type,
            const std::string& parentName,
            const std::string& childName
        ) {
            const auto relations = instances(type);

            for (auto relation = relations->begin();
                 relation != relations->end();
                 ++relation) {
                IfcObject* parent =
                    referenceAttribute(
                        *relation,
                        parentName
                    );

                const auto related =
                    referencesAttribute(
                        *relation,
                        childName
                    );

                for (auto child = related->begin();
                     child != related->end();
                     ++child) {
                    addEdge(parent, *child);
                }
            }
        };

        const auto addSingleRelation = [&](
            const std::string& type,
            const std::string& parentName,
            const std::string& childName
        ) {
            const auto relations = instances(type);

            for (auto relation = relations->begin();
                 relation != relations->end();
                 ++relation) {
                addEdge(
                    referenceAttribute(
                        *relation,
                        parentName
                    ),
                    referenceAttribute(
                        *relation,
                        childName
                    )
                );
            }
        };

        addListRelation(
            "IfcRelAggregates",
            "RelatingObject",
            "RelatedObjects"
        );

        addListRelation(
            "IfcRelNests",
            "RelatingObject",
            "RelatedObjects"
        );

        addListRelation(
            "IfcRelContainedInSpatialStructure",
            "RelatingStructure",
            "RelatedElements"
        );

        addSingleRelation(
            "IfcRelVoidsElement",
            "RelatingBuildingElement",
            "RelatedOpeningElement"
        );

        addSingleRelation(
            "IfcRelFillsElement",
            "RelatingOpeningElement",
            "RelatedBuildingElement"
        );

        json nodes = json::array();
        std::vector<int> allIds;
        std::vector<IfcObject*> objects;

        for (auto iterator = file_->begin();
             iterator != file_->end();
             ++iterator) {
            IfcObject* object = iterator->second;

            allIds.push_back(
                static_cast<int>(object->id())
            );

            if (object
                    ->declaration()
                    .is("IfcObjectDefinition")) {
                objects.push_back(object);
            }
        }

        std::sort(
            objects.begin(),
            objects.end(),
            [](
                const IfcObject* a,
                const IfcObject* b
            ) {
                return a->id() < b->id();
            }
        );

        std::sort(
            allIds.begin(),
            allIds.end()
        );

        for (IfcObject* object : objects) {
            const int id =
                static_cast<int>(object->id());

            const IfcParse::entity* supertype =
                object
                    ->declaration()
                    .as_entity()
                    ->supertype();

            nodes.push_back({
                {"id", id},
                {"type", object->declaration().name()},
                {
                    "supertype",
                    supertype
                        ? supertype->name()
                        : ""
                },
                {
                    "name",
                    stringAttribute(object, "Name")
                },
                {
                    "hasGeometry",
                    referenceAttribute(
                        object,
                        "Representation"
                    ) != nullptr
                },
                {"parentIds", parents[id]},
                {"childIds", children[id]}
            });
        }

        return {
            {"allEntityIds", allIds},
            {"nodes", std::move(nodes)}
        };
    }

    json relatedValues(
        IfcObject* target,
        const std::string& relationType,
        const std::string& relatedName,
        const std::string& valueName,
        int depth
    ) const {
        json result = json::array();
        const auto relations =
            instances(relationType);

        for (auto relation = relations->begin();
             relation != relations->end();
             ++relation) {
            const auto related =
                referencesAttribute(
                    *relation,
                    relatedName
                );

            if (!related->contains(target)) {
                continue;
            }

            std::unordered_set<uint32_t> path;

            result.push_back(
                entityToJson(
                    referenceAttribute(
                        *relation,
                        valueName
                    ),
                    depth,
                    path
                )
            );
        }

        return result;
    }

    IfcObject* ownerHistory() const {
        const auto values =
            instances("IfcOwnerHistory");

        return values->size()
            ? (*values)[0]
            : nullptr;
    }

    void removeMaterialAssociations(
        IfcObject* target
    ) {
        const auto relations =
            instances("IfcRelAssociatesMaterial");

        std::vector<IfcObject*> toDelete;

        for (auto relation = relations->begin();
             relation != relations->end();
             ++relation) {
            const auto related =
                referencesAttribute(
                    *relation,
                    "RelatedObjects"
                );

            if (!related->contains(target)) {
                continue;
            }

            if (related->size() <= 1) {
                toDelete.push_back(*relation);
                continue;
            }

            aggregate_of_instance::ptr updated(
                new aggregate_of_instance()
            );

            for (auto item = related->begin();
                 item != related->end();
                 ++item) {
                if (*item != target) {
                    updated->push(*item);
                }
            }

            setValue(
                *relation,
                "RelatedObjects",
                updated
            );
        }

        for (IfcObject* relation : toDelete) {
            file_->removeEntity(relation);
        }
    }

    IfcObject* materialFromJson(
        const json& material
    ) {
        if (material.is_number_integer()) {
            return entityById(
                material.get<int>()
            );
        }

        if (material.is_object() &&
            material.contains("id")) {
            return entityById(
                material.at("id").get<int>()
            );
        }

        const std::string name =
            material.is_string()
                ? material.get<std::string>()
                : material
                    .at("name")
                    .get<std::string>();

        if (name.empty()) {
            throw std::runtime_error(
                "Material name cannot be empty"
            );
        }

        const auto materials =
            instances("IfcMaterial");

        for (auto iterator = materials->begin();
             iterator != materials->end();
             ++iterator) {
            if (stringAttribute(
                    *iterator,
                    "Name"
                ) == name) {
                return *iterator;
            }
        }

        IfcObject* created =
            create("IfcMaterial");

        setValue(created, "Name", name);

        if (material.is_object()) {
            if (material.contains("description")) {
                setValue(
                    created,
                    "Description",
                    material
                        .at("description")
                        .get<std::string>()
                );
            }

            if (material.contains("category")) {
                setValue(
                    created,
                    "Category",
                    material
                        .at("category")
                        .get<std::string>()
                );
            }
        }

        return created;
    }

    IfcObject* assignMaterial(
        IfcObject* target,
        const json& materialRequest
    ) {
        IfcObject* material = nullptr;

        if (!materialRequest.is_null()) {
            material =
                materialFromJson(materialRequest);

            if (material
                    ->declaration()
                    .name()
                    .rfind("IfcMaterial", 0) != 0) {
                throw std::runtime_error(
                    "The material id does not reference an IFC material definition"
                );
            }
        }

        removeMaterialAssociations(target);

        if (!material) {
            return nullptr;
        }

        const auto relations =
            instances("IfcRelAssociatesMaterial");

        for (auto relation = relations->begin();
             relation != relations->end();
             ++relation) {
            if (referenceAttribute(
                    *relation,
                    "RelatingMaterial"
                ) != material) {
                continue;
            }

            const auto related =
                referencesAttribute(
                    *relation,
                    "RelatedObjects"
                );

            aggregate_of_instance::ptr updated(
                new aggregate_of_instance()
            );

            for (auto item = related->begin();
                 item != related->end();
                 ++item) {
                updated->push(*item);
            }

            updated->push(target);

            setValue(
                *relation,
                "RelatedObjects",
                updated
            );

            return material;
        }

        IfcObject* relation =
            create("IfcRelAssociatesMaterial");

        setValue(
            relation,
            "GlobalId",
            static_cast<const std::string&>(
                IfcParse::IfcGlobalId()
            )
        );

        if (IfcObject* history = ownerHistory()) {
            setReference(
                relation,
                "OwnerHistory",
                history
            );
        }

        aggregate_of_instance::ptr related(
            new aggregate_of_instance()
        );

        related->push(target);

        setValue(
            relation,
            "RelatedObjects",
            related
        );

        setReference(
            relation,
            "RelatingMaterial",
            material
        );

        return material;
    }

    IfcObject* typedPropertyValue(
        const json& value
    ) {
        IfcObject* result = nullptr;

        if (value.is_boolean()) {
            result = create("IfcBoolean");
            result->set_attribute_value(
                0,
                value.get<bool>()
            );
        } else if (
            value.is_number_integer() ||
            value.is_number_unsigned()
        ) {
            result = create("IfcInteger");
            result->set_attribute_value(
                0,
                value.get<int>()
            );
        } else if (value.is_number_float()) {
            result = create("IfcReal");
            result->set_attribute_value(
                0,
                value.get<double>()
            );
        } else if (value.is_string()) {
            result = create("IfcText");
            result->set_attribute_value(
                0,
                value.get<std::string>()
            );
        } else {
            throw std::runtime_error(
                "Property values must be strings, numbers, booleans or null"
            );
        }

        return result;
    }

    IfcObject* cloneEntity(
        IfcObject* source
    ) {
        IfcObject* clone =
            create(source->declaration().name());

        const size_t count =
            source
                ->declaration()
                .as_entity()
                ->attribute_count();

        for (size_t index = 0;
             index < count;
             ++index) {
            copyValue(
                clone,
                index,
                source->get_attribute_value(index)
            );
        }

        return clone;
    }

    IfcObject* createPropertyRelation(
        IfcObject* target,
        IfcObject* propertySet
    ) {
        IfcObject* relation =
            create("IfcRelDefinesByProperties");

        setValue(
            relation,
            "GlobalId",
            static_cast<const std::string&>(
                IfcParse::IfcGlobalId()
            )
        );

        if (IfcObject* history = ownerHistory()) {
            setReference(
                relation,
                "OwnerHistory",
                history
            );
        }

        aggregate_of_instance::ptr related(
            new aggregate_of_instance()
        );

        related->push(target);

        setValue(
            relation,
            "RelatedObjects",
            related
        );

        setReference(
            relation,
            "RelatingPropertyDefinition",
            propertySet
        );

        return relation;
    }

    IfcObject* editablePropertySet(
        IfcObject* target,
        const std::string& name
    ) {
        const auto relations =
            instances("IfcRelDefinesByProperties");

        for (auto relation = relations->begin();
             relation != relations->end();
             ++relation) {
            const auto related =
                referencesAttribute(
                    *relation,
                    "RelatedObjects"
                );

            IfcObject* propertySet =
                referenceAttribute(
                    *relation,
                    "RelatingPropertyDefinition"
                );

            if (!related->contains(target) ||
                !propertySet ||
                !propertySet
                    ->declaration()
                    .is("IfcPropertySet") ||
                stringAttribute(
                    propertySet,
                    "Name"
                ) != name) {
                continue;
            }

            if (related->size() <= 1) {
                return propertySet;
            }

            aggregate_of_instance::ptr remaining(
                new aggregate_of_instance()
            );

            for (auto item = related->begin();
                 item != related->end();
                 ++item) {
                if (*item != target) {
                    remaining->push(*item);
                }
            }

            setValue(
                *relation,
                "RelatedObjects",
                remaining
            );

            IfcObject* clone =
                create("IfcPropertySet");

            setValue(
                clone,
                "GlobalId",
                static_cast<const std::string&>(
                    IfcParse::IfcGlobalId()
                )
            );

            if (IfcObject* history =
                    ownerHistory()) {
                setReference(
                    clone,
                    "OwnerHistory",
                    history
                );
            }

            setValue(clone, "Name", name);

            const std::string description =
                stringAttribute(
                    propertySet,
                    "Description"
                );

            if (!description.empty()) {
                setValue(
                    clone,
                    "Description",
                    description
                );
            }

            aggregate_of_instance::ptr
                clonedProperties(
                    new aggregate_of_instance()
                );

            const auto properties =
                referencesAttribute(
                    propertySet,
                    "HasProperties"
                );

            for (auto property =
                     properties->begin();
                 property != properties->end();
                 ++property) {
                clonedProperties->push(
                    cloneEntity(*property)
                );
            }

            setValue(
                clone,
                "HasProperties",
                clonedProperties
            );

            createPropertyRelation(
                target,
                clone
            );

            return clone;
        }

        IfcObject* propertySet =
            create("IfcPropertySet");

        setValue(
            propertySet,
            "GlobalId",
            static_cast<const std::string&>(
                IfcParse::IfcGlobalId()
            )
        );

        if (IfcObject* history = ownerHistory()) {
            setReference(
                propertySet,
                "OwnerHistory",
                history
            );
        }

        setValue(
            propertySet,
            "Name",
            name
        );

        setValue(
            propertySet,
            "HasProperties",
            aggregate_of_instance::ptr(
                new aggregate_of_instance()
            )
        );

        createPropertyRelation(
            target,
            propertySet
        );

        return propertySet;
    }

    void updatePropertySet(
        IfcObject* target,
        const std::string& setName,
        const json& changes
    ) {
        if (!changes.is_object()) {
            throw std::runtime_error(
                "Property set " + setName +
                " must be a JSON object"
            );
        }

        IfcObject* propertySet =
            editablePropertySet(
                target,
                setName
            );

        const auto properties =
            referencesAttribute(
                propertySet,
                "HasProperties"
            );

        aggregate_of_instance::ptr updated(
            new aggregate_of_instance()
        );

        for (auto property = properties->begin();
             property != properties->end();
             ++property) {
            updated->push(*property);
        }

        for (auto change = changes.begin();
             change != changes.end();
             ++change) {
            IfcObject* property = nullptr;

            for (auto item = updated->begin();
                 item != updated->end();
                 ++item) {
                if (stringAttribute(
                        *item,
                        "Name"
                    ) == change.key()) {
                    property = *item;
                    break;
                }
            }

            if (!property) {
                property =
                    create(
                        "IfcPropertySingleValue"
                    );

                setValue(
                    property,
                    "Name",
                    change.key()
                );

                updated->push(property);
            }

            if (!property
                    ->declaration()
                    .is("IfcPropertySingleValue")) {
                throw std::runtime_error(
                    "Property " + change.key() +
                    " is not an IfcPropertySingleValue"
                );
            }

            if (change.value().is_null()) {
                const ptrdiff_t index =
                    attributeIndex(
                        property,
                        "NominalValue"
                    );

                if (index >= 0) {
                    property->set_attribute_value(
                        static_cast<size_t>(index),
                        Blank{}
                    );
                }
            } else {
                setReference(
                    property,
                    "NominalValue",
                    typedPropertyValue(
                        change.value()
                    )
                );
            }
        }

        setValue(
            propertySet,
            "HasProperties",
            updated
        );
    }

    void updatePropertySets(
        IfcObject* target,
        const json& propertySets
    ) {
        if (!propertySets.is_object()) {
            throw std::runtime_error(
                "properties must be a JSON object of property sets"
            );
        }

        for (auto iterator = propertySets.begin();
             iterator != propertySets.end();
             ++iterator) {
            updatePropertySet(
                target,
                iterator.key(),
                iterator.value()
            );
        }
    }

    static bool relationshipBecomesInvalid(
        IfcObject* relationship,
        IfcObject* target
    ) {
        if (!relationship ||
            !relationship
                ->declaration()
                .is("IfcRelationship")) {
            return false;
        }

        const size_t count =
            relationship
                ->declaration()
                .as_entity()
                ->attribute_count();

        for (size_t index = 0;
             index < count;
             ++index) {
            const AttributeValue value =
                relationship
                    ->get_attribute_value(index);

            if (value.isNull()) {
                continue;
            }

            if (value.type() ==
                    IfcUtil::
                        Argument_ENTITY_INSTANCE &&
                static_cast<IfcObject*>(value) ==
                    target) {
                return true;
            }

            if (value.type() ==
                IfcUtil::
                    Argument_AGGREGATE_OF_ENTITY_INSTANCE) {
                const auto list =
                    static_cast<
                        aggregate_of_instance::ptr
                    >(value);

                if (list &&
                    list->contains(target) &&
                    list->size() <= 1) {
                    return true;
                }
            }
        }

        return false;
    }

    static void collectTraversal(
        IfcObject* root,
        std::set<int>& ids
    ) {
        if (!root) {
            return;
        }

        const auto traversed =
            IfcParse::IfcFile::traverse(root);

        if (!traversed) {
            return;
        }

        for (auto iterator = traversed->begin();
             iterator != traversed->end();
             ++iterator) {
            if ((*iterator)->id() != 0) {
                ids.insert(
                    static_cast<int>(
                        (*iterator)->id()
                    )
                );
            }
        }
    }

    static bool protectedFromOrphanCleanup(
        const IfcObject* object
    ) {
        if (!object) {
            return true;
        }

        const std::string type =
            object->declaration().name();

        return
            object
                ->declaration()
                .is(
                    "IfcGeometricRepresentationContext"
                ) ||
            object
                ->declaration()
                .is("IfcMaterial") ||
            type.find("Unit") !=
                std::string::npos ||
            type == "IfcOwnerHistory" ||
            type == "IfcApplication" ||
            type == "IfcPerson" ||
            type == "IfcOrganization" ||
            type == "IfcProject";
    }

    json deleteElement(int id) {
        IfcEntity* target = entityById(id);

        if (!target
                ->declaration()
                .is("IfcProduct")) {
            throw std::runtime_error(
                "delete is limited to IfcProduct instances"
            );
        }

        std::set<int> cleanupCandidates;

        collectTraversal(
            referenceAttribute(
                target,
                "Representation"
            ),
            cleanupCandidates
        );

        collectTraversal(
            referenceAttribute(
                target,
                "ObjectPlacement"
            ),
            cleanupCandidates
        );

        std::vector<IfcObject*>
            relationshipsToDelete;

        const auto references =
            file_->instances_by_reference(id);

        if (references) {
            for (auto reference =
                     references->begin();
                 reference != references->end();
                 ++reference) {
                IfcObject* relationship =
                    *reference;

                if (!relationshipBecomesInvalid(
                        relationship,
                        target
                    )) {
                    continue;
                }

                if (relationship
                        ->declaration()
                        .is(
                            "IfcRelDefinesByProperties"
                        )) {
                    collectTraversal(
                        referenceAttribute(
                            relationship,
                            "RelatingPropertyDefinition"
                        ),
                        cleanupCandidates
                    );
                }

                relationshipsToDelete.push_back(
                    relationship
                );
            }
        }

        for (IfcObject* relationship :
             relationshipsToDelete) {
            file_->removeEntity(relationship);
        }

        file_->removeEntity(target);

        std::vector<int> removedOwnedEntities;
        bool changed = true;

        while (changed) {
            changed = false;

            for (int candidateId :
                 cleanupCandidates) {
                IfcObject* candidate = nullptr;

                try {
                    candidate =
                        file_->instance_by_id(
                            candidateId
                        );
                } catch (...) {
                    continue;
                }

                if (!candidate ||
                    protectedFromOrphanCleanup(
                        candidate
                    ) ||
                    file_->getTotalInverses(
                        candidateId
                    ) != 0) {
                    continue;
                }

                file_->removeEntity(candidate);

                removedOwnedEntities.push_back(
                    candidateId
                );

                changed = true;
            }
        }

        std::sort(
            removedOwnedEntities.begin(),
            removedOwnedEntities.end()
        );

        return {
            {"id", id},
            {"deleted", true},
            {
                "deletedRelationships",
                relationshipsToDelete.size()
            },
            {
                "deletedOwnedEntityIds",
                removedOwnedEntities
            }
        };
    }

    json updateElement(
        const json& request
    ) {
        const int id =
            request.at("id").get<int>();

        IfcEntity* target =
            entityById(id);

        if (request.contains("class")) {
            target = changeClass(
                target,
                request
                    .at("class")
                    .get<std::string>()
            );
        }

        json attributes =
            request.value(
                "attributes",
                json::object()
            );

        if (!attributes.is_object()) {
            throw std::runtime_error(
                "attributes must be a JSON object"
            );
        }

        const std::pair<
            const char*,
            const char*
        > shortcuts[] = {
            {"name", "Name"},
            {"description", "Description"},
            {"tag", "Tag"},
            {"objectType", "ObjectType"},
            {"predefinedType", "PredefinedType"}
        };

        for (const auto& shortcut : shortcuts) {
            if (request.contains(shortcut.first)) {
                attributes[shortcut.second] =
                    request.at(shortcut.first);
            }
        }

        json changedAttributes =
            json::array();

        for (auto attribute = attributes.begin();
             attribute != attributes.end();
             ++attribute) {
            setJsonAttribute(
                target,
                attribute.key(),
                attribute.value()
            );

            changedAttributes.push_back(
                attribute.key()
            );
        }

        json materialResult = nullptr;

        if (request.contains("material")) {
            materialResult =
                objectReference(
                    assignMaterial(
                        target,
                        request.at("material")
                    )
                );
        }

        if (request.contains("properties")) {
            updatePropertySets(
                target,
                request.at("properties")
            );
        }

        return {
            {"id", id},
            {
                "type",
                target->declaration().name()
            },
            {
                "changedAttributes",
                changedAttributes
            },
            {"material", materialResult},
            {
                "propertiesUpdated",
                request.contains("properties")
            }
        };
    }

    json mesh(int id) const {
        ifcopenshell::geometry::Settings settings;

        settings
            .get<
                ifcopenshell::geometry::settings::
                    UseWorldCoords
            >()
            .value = true;

        std::vector<IfcGeom::filter_t> filters;

        filters.emplace_back(
            [id](IfcEntity* product) {
                return product &&
                    static_cast<int>(
                        product->id()
                    ) == id;
            }
        );

        auto kernel =
            ifcopenshell::geometry::kernels::construct(
                file_.get(),
                "opencascade",
                settings
            );

        IfcGeom::Iterator iterator(
            std::move(kernel),
            settings,
            file_.get(),
            filters,
            1
        );

        json meshes = json::array();

        if (!iterator.initialize()) {
            return meshes;
        }

        do {
            auto* element =
                dynamic_cast<
                    IfcGeom::TriangulationElement*
                >(iterator.get());

            if (!element ||
                element->id() != id) {
                continue;
            }

            const auto& geometry =
                element->geometry();

            meshes.push_back({
                {"context", element->context()},
                {"vertices", geometry.verts()},
                {"triangles", geometry.faces()},
                {"normals", geometry.normals()},
                {
                    "materialIds",
                    geometry.material_ids()
                }
            });
        } while (iterator.next() != nullptr);

        return meshes;
    }

    json get(
        int id,
        bool includeMesh
    ) const {
        IfcEntity* object =
            entityById(id);

        std::unordered_set<uint32_t> path;

        json result =
            objectReference(object);

        json explicitAttributes =
            json::object();

        const auto attributes =
            object
                ->declaration()
                .as_entity()
                ->all_attributes();

        for (size_t index = 0;
             index < attributes.size();
             ++index) {
            explicitAttributes[
                attributes[index]->name()
            ] = valueToJson(
                object->get_attribute_value(index),
                0,
                path
            );
        }

        result["attributes"] =
            std::move(explicitAttributes);

        json inverseAttributes =
            json::object();

        for (const auto* inverse :
             object
                 ->declaration()
                 .as_entity()
                 ->all_inverse_attributes()) {
            json values = json::array();

            try {
                const auto references =
                    object->get_inverse(
                        inverse->name()
                    );

                if (references) {
                    for (auto reference =
                             references->begin();
                         reference != references->end();
                         ++reference) {
                        values.push_back(
                            objectReference(
                                *reference
                            )
                        );
                    }
                }
            } catch (...) {
            }

            inverseAttributes[
                inverse->name()
            ] = std::move(values);
        }

        result["inverseAttributes"] =
            std::move(inverseAttributes);

        result["transform"] =
            objectMatrix(object);

        result["propertySets"] =
            relatedValues(
                object,
                "IfcRelDefinesByProperties",
                "RelatedObjects",
                "RelatingPropertyDefinition",
                6
            );

        result["types"] =
            relatedValues(
                object,
                "IfcRelDefinesByType",
                "RelatedObjects",
                "RelatingType",
                6
            );

        result["materials"] =
            relatedValues(
                object,
                "IfcRelAssociatesMaterial",
                "RelatedObjects",
                "RelatingMaterial",
                6
            );

        IfcObject* representation =
            referenceAttribute(
                object,
                "Representation"
            );

        path.clear();

        result["geometry"] = {
            {
                "representation",
                entityToJson(
                    representation,
                    10,
                    path
                )
            }
        };

        if (includeMesh && representation) {
            result["geometry"]["meshes"] =
                mesh(id);
        }

        return result;
    }

    static Matrix requestMatrix(
        const json& request
    ) {
        if (!request.contains("transform")) {
            return identity();
        }

        const auto values =
            request
                .at("transform")
                .get<std::vector<double>>();

        if (values.size() != 16) {
            throw std::runtime_error(
                "transform must contain 16 column-major values"
            );
        }

        Matrix result{};

        std::copy(
            values.begin(),
            values.end(),
            result.begin()
        );

        for (double value : result) {
            if (!std::isfinite(value)) {
                throw std::runtime_error(
                    "transform contains a non-finite value"
                );
            }
        }

        return result;
    }

    IfcObject* point(
        const std::vector<double>& coordinates
    ) {
        IfcObject* result =
            create("IfcCartesianPoint");

        setValue(
            result,
            "Coordinates",
            coordinates
        );

        return result;
    }

    IfcObject* direction(
        const std::array<double, 3>& ratios
    ) {
        IfcObject* result =
            create("IfcDirection");

        setValue(
            result,
            "DirectionRatios",
            std::vector<double>{
                ratios[0],
                ratios[1],
                ratios[2]
            }
        );

        return result;
    }

    IfcObject* axisPlacement(
        const Matrix& matrix
    ) {
        std::array<double, 3> x =
            normalize({
                matrix[0],
                matrix[1],
                matrix[2]
            });

        std::array<double, 3> z =
            normalize({
                matrix[8],
                matrix[9],
                matrix[10]
            });

        normalize(cross(z, x));

        IfcObject* result =
            create("IfcAxis2Placement3D");

        setReference(
            result,
            "Location",
            point({
                matrix[12],
                matrix[13],
                matrix[14]
            })
        );

        setReference(
            result,
            "Axis",
            direction(z)
        );

        setReference(
            result,
            "RefDirection",
            direction(x)
        );

        return result;
    }

    IfcObject* extrudedSolid(
        const json& request,
        const Matrix& placement
    ) {
        const auto profile =
            request.at("profile");

        if (!profile.is_array() ||
            profile.size() < 3) {
            throw std::runtime_error(
                "profile must contain at least three [x,y] points"
            );
        }

        const double depth =
            request.at("depth").get<double>();

        if (!std::isfinite(depth) ||
            depth <= 0) {
            throw std::runtime_error(
                "depth must be a positive number"
            );
        }

        std::vector<std::vector<double>>
            coordinatesList;

        for (const auto& item : profile) {
            const auto coordinates =
                item.get<std::vector<double>>();

            if (coordinates.size() != 2 ||
                !std::isfinite(coordinates[0]) ||
                !std::isfinite(coordinates[1])) {
                throw std::runtime_error(
                    "Every profile point must be a finite [x,y] pair"
                );
            }

            coordinatesList.push_back(
                coordinates
            );
        }

        aggregate_of_instance::ptr points(
            new aggregate_of_instance()
        );

        for (const auto& coordinates :
             coordinatesList) {
            points->push(point(coordinates));
        }

        if (coordinatesList.front() !=
            coordinatesList.back()) {
            points->push(
                point(coordinatesList.front())
            );
        }

        IfcObject* polyline =
            create("IfcPolyline");

        setValue(
            polyline,
            "Points",
            points
        );

        IfcObject* profileDefinition =
            create(
                "IfcArbitraryClosedProfileDef"
            );

        setEnumeration(
            profileDefinition,
            "ProfileType",
            "AREA"
        );

        setReference(
            profileDefinition,
            "OuterCurve",
            polyline
        );

        IfcObject* solid =
            create("IfcExtrudedAreaSolid");

        setReference(
            solid,
            "SweptArea",
            profileDefinition
        );

        setReference(
            solid,
            "Position",
            axisPlacement(placement)
        );

        setReference(
            solid,
            "ExtrudedDirection",
            direction({0, 0, 1})
        );

        setValue(
            solid,
            "Depth",
            depth
        );

        return solid;
    }

    IfcObject* bodyRepresentation(
        IfcObject* product
    ) const {
        IfcObject* productShape =
            referenceAttribute(
                product,
                "Representation"
            );

        const auto representations =
            referencesAttribute(
                productShape,
                "Representations"
            );

        IfcObject* fallback = nullptr;

        for (auto representation =
                 representations->begin();
             representation !=
                 representations->end();
             ++representation) {
            if (!fallback) {
                fallback = *representation;
            }

            if (stringAttribute(
                    *representation,
                    "RepresentationIdentifier"
                ) == "Body") {
                return *representation;
            }
        }

        return fallback;
    }

    static bool isBooleanOperand(
        const IfcObject* object
    ) {
        return object &&
            (
                object
                    ->declaration()
                    .is("IfcSolidModel") ||
                object
                    ->declaration()
                    .is("IfcHalfSpaceSolid") ||
                object
                    ->declaration()
                    .is("IfcBooleanResult") ||
                object
                    ->declaration()
                    .is("IfcCsgPrimitive3D")
            );
    }

    IfcObject* booleanResult(
        IfcObject* first,
        IfcObject* second,
        const std::string& operation
    ) {
        IfcObject* result =
            create("IfcBooleanResult");

        setEnumeration(
            result,
            "Operator",
            operation
        );

        setReference(
            result,
            "FirstOperand",
            first
        );

        setReference(
            result,
            "SecondOperand",
            second
        );

        return result;
    }

    json modifyProduct(
        const json& request,
        int targetId,
        const Matrix& transform
    ) {
        IfcEntity* product =
            entityById(targetId);

        IfcObject* representation =
            bodyRepresentation(product);

        if (!representation) {
            throw std::runtime_error(
                "The selected object has no shape representation"
            );
        }

        const auto oldItems =
            referencesAttribute(
                representation,
                "Items"
            );

        if (oldItems->size() == 0) {
            throw std::runtime_error(
                "The selected shape representation has no items"
            );
        }

        for (auto item = oldItems->begin();
             item != oldItems->end();
             ++item) {
            if (!isBooleanOperand(*item)) {
                throw std::runtime_error(
                    "The selected representation contains a non-solid item: " +
                    (*item)->declaration().name()
                );
            }
        }

        const std::string requestedOperation =
            request.value(
                "operation",
                std::string("union")
            );

        const bool subtraction =
            requestedOperation == "subtract" ||
            requestedOperation == "difference";

        if (!subtraction &&
            requestedOperation != "union" &&
            requestedOperation != "add") {
            throw std::runtime_error(
                "operation must be union/add or subtract/difference"
            );
        }

        IfcObject* addedPrimitive =
            extrudedSolid(
                request,
                transform
            );

        aggregate_of_instance::ptr newItems(
            new aggregate_of_instance()
        );

        bool consumed = false;

        for (auto item = oldItems->begin();
             item != oldItems->end();
             ++item) {
            if (subtraction) {
                newItems->push(
                    booleanResult(
                        *item,
                        addedPrimitive,
                        "DIFFERENCE"
                    )
                );
            } else if (!consumed) {
                newItems->push(
                    booleanResult(
                        *item,
                        addedPrimitive,
                        "UNION"
                    )
                );

                consumed = true;
            } else {
                newItems->push(*item);
            }
        }

        setValue(
            representation,
            "Items",
            newItems
        );

        setValue(
            representation,
            "RepresentationType",
            std::string("CSG")
        );

        return {
            {"targetId", targetId},
            {
                "primitiveId",
                addedPrimitive->id()
            },
            {
                "operation",
                subtraction
                    ? "difference"
                    : "union"
            }
        };
    }

    IfcObject* representationContext() const {
        auto contexts =
            instances(
                "IfcGeometricRepresentationSubContext"
            );

        for (auto context = contexts->begin();
             context != contexts->end();
             ++context) {
            if (stringAttribute(
                    *context,
                    "ContextIdentifier"
                ) == "Body") {
                return *context;
            }
        }

        if (contexts->size()) {
            return (*contexts)[0];
        }

        contexts =
            instances(
                "IfcGeometricRepresentationContext"
            );

        return contexts->size()
            ? (*contexts)[0]
            : nullptr;
    }

    IfcObject* firstSpatialContainer() const {
        for (const char* type : {
                 "IfcBuildingStorey",
                 "IfcBuilding",
                 "IfcSite"
             }) {
            const auto values = instances(type);

            if (values->size()) {
                return (*values)[0];
            }
        }

        return nullptr;
    }

    void attachToContainer(
        IfcObject* product,
        IfcObject* container
    ) {
        if (!container) {
            return;
        }

        const auto relations =
            instances(
                "IfcRelContainedInSpatialStructure"
            );

        for (auto relation = relations->begin();
             relation != relations->end();
             ++relation) {
            if (referenceAttribute(
                    *relation,
                    "RelatingStructure"
                ) != container) {
                continue;
            }

            const auto related =
                referencesAttribute(
                    *relation,
                    "RelatedElements"
                );

            if (!related->contains(product)) {
                aggregate_of_instance::ptr updated(
                    new aggregate_of_instance()
                );

                for (auto child = related->begin();
                     child != related->end();
                     ++child) {
                    updated->push(*child);
                }

                updated->push(product);

                setValue(
                    *relation,
                    "RelatedElements",
                    updated
                );
            }

            return;
        }

        IfcObject* relation =
            create(
                "IfcRelContainedInSpatialStructure"
            );

        setValue(
            relation,
            "GlobalId",
            static_cast<const std::string&>(
                IfcParse::IfcGlobalId()
            )
        );

        const auto ownerHistories =
            instances("IfcOwnerHistory");

        if (ownerHistories->size()) {
            setReference(
                relation,
                "OwnerHistory",
                (*ownerHistories)[0]
            );
        }

        aggregate_of_instance::ptr related(
            new aggregate_of_instance()
        );

        related->push(product);

        setValue(
            relation,
            "RelatedElements",
            related
        );

        setReference(
            relation,
            "RelatingStructure",
            container
        );
    }

    json addProduct(
        const json& request,
        const Matrix& transform
    ) {
        IfcObject* context =
            representationContext();

        if (!context) {
            throw std::runtime_error(
                "The IFC file has no geometric representation context"
            );
        }

        IfcObject* solid =
            extrudedSolid(
                request,
                identity()
            );

        IfcObject* shapeRepresentation =
            create("IfcShapeRepresentation");

        setReference(
            shapeRepresentation,
            "ContextOfItems",
            context
        );

        setValue(
            shapeRepresentation,
            "RepresentationIdentifier",
            std::string("Body")
        );

        setValue(
            shapeRepresentation,
            "RepresentationType",
            std::string("SweptSolid")
        );

        aggregate_of_instance::ptr items(
            new aggregate_of_instance()
        );

        items->push(solid);

        setValue(
            shapeRepresentation,
            "Items",
            items
        );

        IfcObject* productShape =
            create("IfcProductDefinitionShape");

        aggregate_of_instance::ptr representations(
            new aggregate_of_instance()
        );

        representations->push(
            shapeRepresentation
        );

        setValue(
            productShape,
            "Representations",
            representations
        );

        IfcObject* localPlacement =
            create("IfcLocalPlacement");

        setReference(
            localPlacement,
            "RelativePlacement",
            axisPlacement(transform)
        );

        IfcObject* product =
            create("IfcBuildingElementProxy");

        setValue(
            product,
            "GlobalId",
            static_cast<const std::string&>(
                IfcParse::IfcGlobalId()
            )
        );

        const auto ownerHistories =
            instances("IfcOwnerHistory");

        if (ownerHistories->size()) {
            setReference(
                product,
                "OwnerHistory",
                (*ownerHistories)[0]
            );
        }

        setValue(
            product,
            "Name",
            request.value(
                "name",
                std::string("Primitive")
            )
        );

        setReference(
            product,
            "ObjectPlacement",
            localPlacement
        );

        setReference(
            product,
            "Representation",
            productShape
        );

        IfcObject* container =
            request.contains("containerId")
                ? entityById(
                    request
                        .at("containerId")
                        .get<int>()
                )
                : firstSpatialContainer();

        attachToContainer(
            product,
            container
        );

        return {
            {"id", product->id()},
            {
                "type",
                product->declaration().name()
            },
            {
                "containerId",
                container
                    ? json(container->id())
                    : json(nullptr)
            }
        };
    }

    json primitive(
        const json& request
    ) {
        requireFile();

        const Matrix transform =
            requestMatrix(request);

        return request.contains("targetId")
            ? modifyProduct(
                request,
                request
                    .at("targetId")
                    .get<int>(),
                transform
            )
            : addProduct(
                request,
                transform
            );
    }

    json save(
        const std::string& path
    ) {
        requireFile();

        if (path.empty()) {
            throw std::runtime_error(
                "A save path is required"
            );
        }

        std::ofstream output(
            IfcUtil::path::from_utf8(path).c_str(),
            std::ios::out | std::ios::trunc
        );

        if (!output) {
            throw std::runtime_error(
                "Cannot open the output file: " +
                path
            );
        }

        output << *file_;

        if (!output) {
            throw std::runtime_error(
                "Failed while writing the IFC file: " +
                path
            );
        }

        sourcePath_ = path;

        return {{"path", path}};
    }

    static json help() {
        return {
            {
                "actions",
                {
                    "load: {action,path}",
                    "tree: {action}",
                    "get: {action,id,mesh?}",
                    "primitive: {action,targetId?,operation?,profile,depth,transform?,name?,containerId?}",
                    "delete: {action,id}",
                    "update: {action,id,class?,attributes?,material?,properties?}",
                    "save: {action,path?}",
                    "exit: {action}"
                }
            },
            {
                "transform",
                "16 column-major values; targeted primitives use target-local coordinates"
            }
        };
    }
};

int main() {
#ifdef _WIN32
    constexpr unsigned int kUtf8CodePage =
        65001;

    SetConsoleCP(kUtf8CodePage);
    SetConsoleOutputCP(kUtf8CodePage);
#endif

    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);

    Logger::SetOutput(
        &std::cerr,
        &std::cerr
    );

    IfcEditor editor;

    std::cout
        << json{
            {"ok", true},
            {
                "data",
                {
                    {"status", "ready"},
                    {"protocol", "json-lines"}
                }
            }
        }.dump()
        << '\n'
        << std::flush;

    std::string line;

    while (std::getline(std::cin, line)) {
        if (line.empty()) {
            continue;
        }

        bool shouldExit = false;

        try {
            const json request =
                json::parse(line);

            shouldExit =
                request.value(
                    "action",
                    std::string()
                ) == "exit";

            std::cout
                << json{
                    {"ok", true},
                    {
                        "data",
                        editor.execute(request)
                    }
                }.dump()
                << '\n';
        } catch (const std::exception& error) {
            std::cout
                << json{
                    {"ok", false},
                    {"error", error.what()}
                }.dump()
                << '\n';
        }

        std::cout << std::flush;

        if (shouldExit) {
            break;
        }
    }

    return 0;
}