cmake_minimum_required(VERSION 3.21)

project(ifc-cli LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(
    IFCOPENSHELL_ROOT
    "$ENV{HOME}/.local/ifcopenshell"
    CACHE PATH
    "IfcOpenShell installation directory"
)

find_package(Boost CONFIG REQUIRED)
find_package(Eigen3 CONFIG REQUIRED)
find_package(OpenCASCADE CONFIG REQUIRED)
find_package(nlohmann_json CONFIG REQUIRED)

find_library(
    IFC_PARSE_LIBRARY
    NAMES IfcParse
    PATHS "${IFCOPENSHELL_ROOT}/lib"
    NO_DEFAULT_PATH
    REQUIRED
)

find_library(
    IFC_GEOM_LIBRARY
    NAMES IfcGeom
    PATHS "${IFCOPENSHELL_ROOT}/lib"
    NO_DEFAULT_PATH
    REQUIRED
)

find_library(
    IFC_GEOM_KERNEL_LIBRARY
    NAMES geometry_kernel_opencascade
    PATHS "${IFCOPENSHELL_ROOT}/lib"
    NO_DEFAULT_PATH
    REQUIRED
)

add_executable(
    ifc-cli
    src/main.cpp
    src/protocol.hpp
    src/scene.hpp
    src/transform.hpp
    src/geometry.hpp
    src/material.hpp
)

target_include_directories(
    ifc-cli
    PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
        "${IFCOPENSHELL_ROOT}/include"
)

target_link_libraries(
    ifc-cli
    PRIVATE
        "${IFC_GEOM_KERNEL_LIBRARY}"
        "${IFC_GEOM_LIBRARY}"
        "${IFC_PARSE_LIBRARY}"
        nlohmann_json::nlohmann_json
        Eigen3::Eigen
        ${OpenCASCADE_LIBRARIES}
)

if(TARGET Boost::headers)
    target_link_libraries(ifc-cli PRIVATE Boost::headers)
elseif(TARGET Boost::boost)
    target_link_libraries(ifc-cli PRIVATE Boost::boost)
endif()

target_compile_options(
    ifc-cli
    PRIVATE
        -Wall
        -Wextra
        -Wpedantic
)

if(APPLE)
    set_target_properties(
        ifc-cli
        PROPERTIES
            BUILD_RPATH "${IFCOPENSHELL_ROOT}/lib"
            INSTALL_RPATH "${IFCOPENSHELL_ROOT}/lib"
            MACOSX_RPATH ON
    )
endif()