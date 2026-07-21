#pragma once

#include <nlohmann/json.hpp>

#include <exception>
#include <stdexcept>
#include <string>
#include <utility>

namespace ifc_editor {

using json = nlohmann::json;

struct ProtocolRequest {
    std::string id;
    std::string command;
    json params;
};

struct ProtocolError {
    std::string code;
    std::string message;
    json details = json::object();
};

class ProtocolException final : public std::runtime_error {
public:
    ProtocolException(std::string code, std::string message, json details = json::object(), json responseId = nullptr)
        : std::runtime_error(message),
          code_(std::move(code)),
          details_(std::move(details)),
          responseId_(std::move(responseId)) {}

    const std::string& code() const noexcept {
        return code_;
    }

    const json& details() const noexcept {
        return details_;
    }

    const json& responseId() const noexcept {
        return responseId_;
    }

private:
    std::string code_;
    json details_;
    json responseId_;
};

class JsonlProtocol {
public:
    static ProtocolRequest parseRequest(const std::string& line) {
        json document;
        try {
            document = json::parse(line);
        } catch (const json::exception&) {
            throw ProtocolException("INVALID_JSON", "Invalid JSONL request", json::object(), nullptr);
        }

        if (!document.is_object()) {
            throw ProtocolException("INVALID_JSON", "Invalid JSONL request", json::object(), nullptr);
        }

        json responseId = nullptr;
        if (document.contains("id") && document.at("id").is_string() && !document.at("id").get_ref<const std::string&>().empty()) {
            responseId = document.at("id");
        }

        if (document.size() != 3 || !document.contains("id") || !document.contains("command") || !document.contains("params")) {
            throw ProtocolException(
                "INVALID_PARAMETERS",
                "Request must contain exactly id, command and params",
                json::object(),
                responseId);
        }

        if (!document.at("id").is_string() || document.at("id").get_ref<const std::string&>().empty()) {
            throw ProtocolException("INVALID_PARAMETERS", "Request id must be a non-empty string", json::object(), nullptr);
        }

        if (!document.at("command").is_string() || document.at("command").get_ref<const std::string&>().empty()) {
            throw ProtocolException(
                "INVALID_PARAMETERS",
                "Request command must be a non-empty string",
                json::object(),
                document.at("id"));
        }

        if (!document.at("params").is_object()) {
            throw ProtocolException(
                "INVALID_PARAMETERS",
                "Request params must be an object",
                json::object(),
                document.at("id"));
        }

        return ProtocolRequest{
            document.at("id").get<std::string>(),
            document.at("command").get<std::string>(),
            document.at("params")};
    }

    static json createSuccessResponse(const json& id, json result) {
        return json{{"id", id}, {"ok", true}, {"result", std::move(result)}};
    }

    static json createErrorResponse(const json& id, const ProtocolError& error) {
        return json{
            {"id", id},
            {"ok", false},
            {"error", {
                {"code", error.code},
                {"message", error.message},
                {"details", error.details.is_object() ? error.details : json::object()}
            }}};
    }

    static std::string serializeResponse(const json& response) {
        return response.dump();
    }
};

} // namespace ifc_editor