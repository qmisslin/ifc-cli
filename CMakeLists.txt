cmake_minimum_required(VERSION 3.21)
project(ifc_cli VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(IFCOPENSHELL_ROOT "" CACHE PATH "IfcOpenShell source root or installation prefix")

if(WIN32 AND IFCOPENSHELL_ROOT)
    set(_ifc_install "${IFCOPENSHELL_ROOT}/_installed-vs2022-x64")
    set(_ifc_deps "${IFCOPENSHELL_ROOT}/_deps-vs2022-x64-installed")
    set(IfcOpenShell_DIR "${_ifc_install}/lib/cmake/IfcOpenShell")

    file(GLOB _boost_roots LIST_DIRECTORIES true
        "${IFCOPENSHELL_ROOT}/_deps/boost_*/stage/vs2022-x64"
    )
    file(GLOB _occ_roots LIST_DIRECTORIES true
        "${_ifc_deps}/opencascade-*-new-layout"
    )

    if(NOT _boost_roots
       OR NOT _occ_roots
       OR NOT EXISTS "${IfcOpenShell_DIR}/IfcOpenShellConfig.cmake")
        message(FATAL_ERROR
            "IFCOPENSHELL_ROOT does not contain the expected vs2022-x64 installation"
        )
    endif()

    list(GET _boost_roots 0 _boost_root)
    list(GET _occ_roots 0 _occ_root)

    list(APPEND CMAKE_PREFIX_PATH
        "${_boost_root}"
        "${_occ_root}"
        "${_ifc_install}"
    )

    set(OpenCASCADE_DIR "${_occ_root}/cmake")

    # The Windows dependency script provides Eigen headers without Eigen3Config.cmake.
    set(_eigen_include "${_ifc_deps}/Eigen")
    set(Eigen3_DIR "${CMAKE_BINARY_DIR}/eigen3-config")

    file(MAKE_DIRECTORY "${Eigen3_DIR}")
    file(WRITE "${Eigen3_DIR}/Eigen3Config.cmake"
        "if(NOT TARGET Eigen3::Eigen)\n"
        "  add_library(Eigen3::Eigen INTERFACE IMPORTED)\n"
        "  set_target_properties(Eigen3::Eigen PROPERTIES "
        "INTERFACE_INCLUDE_DIRECTORIES \"${_eigen_include}\")\n"
        "endif()\n"
        "set(Eigen3_FOUND TRUE)\n"
    )
endif()

if(IFCOPENSHELL_ROOT AND NOT WIN32)
    set(_ifc_install "${IFCOPENSHELL_ROOT}/ifcopenshell")

    file(GLOB _ifc_dependency_prefixes
        LIST_DIRECTORIES true
        "${IFCOPENSHELL_ROOT}/*"
    )

    list(APPEND CMAKE_PREFIX_PATH
        "${_ifc_install}"
        ${_ifc_dependency_prefixes}
    )

    set(IfcOpenShell_DIR
        "${_ifc_install}/lib/cmake/IfcOpenShell"
    )
endif()

find_package(IfcOpenShell CONFIG REQUIRED)

find_package(nlohmann_json CONFIG QUIET)

if(NOT TARGET nlohmann_json::nlohmann_json)
    find_path(
        NLOHMANN_JSON_INCLUDE_DIR
        nlohmann/json.hpp
        HINTS "${_ifc_deps}/json"
    )

    if(NLOHMANN_JSON_INCLUDE_DIR)
        add_library(nlohmann_json_headers INTERFACE)
        target_include_directories(
            nlohmann_json_headers
            INTERFACE "${NLOHMANN_JSON_INCLUDE_DIR}"
        )
        add_library(
            nlohmann_json::nlohmann_json
            ALIAS nlohmann_json_headers
        )
    else()
        include(FetchContent)
        FetchContent_Declare(
            nlohmann_json
            URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
        )
        FetchContent_MakeAvailable(nlohmann_json)
    endif()
endif()

add_executable(ifc_cli main.cpp)

target_compile_definitions(
    ifc_cli
    PRIVATE IFOPSH_WITH_OPENCASCADE
)

target_link_libraries(
    ifc_cli
    PRIVATE
        IfcOpenShell::IfcGeom
        IfcOpenShell::geometry_kernel_opencascade
        nlohmann_json::nlohmann_json
)

if(MSVC)
    target_compile_definitions(
        ifc_cli
        PRIVATE UNICODE _UNICODE NOMINMAX
    )
    target_compile_options(
        ifc_cli
        PRIVATE /W4 /permissive- /utf-8
    )
else()
    target_compile_options(
        ifc_cli
        PRIVATE -Wall -Wextra -Wpedantic
    )
endif()