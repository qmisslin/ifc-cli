cmake_minimum_required(VERSION 3.21)

project(ifc-cli LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT WIN32)
    message(FATAL_ERROR "This CMake configuration currently targets Windows only.")
endif()

set(
    IFCOPENSHELL_SOURCE_ROOT
    "${CMAKE_CURRENT_SOURCE_DIR}/IfcOpenShell"
    CACHE PATH
    "IfcOpenShell source directory"
)

set(
    IFCOPENSHELL_ROOT
    "${IFCOPENSHELL_SOURCE_ROOT}/_installed-vs2022-x64"
    CACHE PATH
    "IfcOpenShell installation directory"
)

set(
    IFCOPENSHELL_DEPS_ROOT
    "${IFCOPENSHELL_SOURCE_ROOT}/_deps-vs2022-x64-installed"
    CACHE PATH
    "IfcOpenShell installed dependencies directory"
)

set(
    IFCOPENSHELL_BOOST_ROOT
    "${IFCOPENSHELL_SOURCE_ROOT}/_deps/boost_1_86_0/stage/vs2022-x64"
    CACHE PATH
    "IfcOpenShell Boost installation directory"
)

set(
    IFCOPENSHELL_CONFIG
    "${IFCOPENSHELL_ROOT}/lib/cmake/IfcOpenShell/IfcOpenShellConfig.cmake"
)

if(NOT EXISTS "${IFCOPENSHELL_CONFIG}")
    message(
        FATAL_ERROR
        "IfcOpenShellConfig.cmake was not found at:\n"
        "${IFCOPENSHELL_CONFIG}\n"
        "Build and install IfcOpenShell before configuring ifc-cli."
    )
endif()

set(CMAKE_FIND_PACKAGE_PREFER_CONFIG ON)

set(CMAKE_MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release)
set(CMAKE_MAP_IMPORTED_CONFIG_MINSIZEREL Release)

list(
    PREPEND CMAKE_MODULE_PATH
    "${IFCOPENSHELL_SOURCE_ROOT}/cmake"
)

list(
    PREPEND CMAKE_PREFIX_PATH
    "${IFCOPENSHELL_ROOT}"
    "${IFCOPENSHELL_BOOST_ROOT}"
    "${IFCOPENSHELL_DEPS_ROOT}/cgal"
    "${IFCOPENSHELL_DEPS_ROOT}/Eigen"
    "${IFCOPENSHELL_DEPS_ROOT}/HDF5-1_13_1-win64"
    "${IFCOPENSHELL_DEPS_ROOT}/opencascade-7.8.1-new-layout"
    "${IFCOPENSHELL_DEPS_ROOT}/OpenCOLLADA"
    "${IFCOPENSHELL_DEPS_ROOT}/proj-9.4.1"
    "${IFCOPENSHELL_DEPS_ROOT}/rocksdb"
    "${IFCOPENSHELL_DEPS_ROOT}/sqlite3"
    "${IFCOPENSHELL_DEPS_ROOT}/zstd"
    "${IFCOPENSHELL_DEPS_ROOT}/mpfr"
    "${IFCOPENSHELL_DEPS_ROOT}/mpir"
)

set(
    IfcOpenShell_DIR
    "${IFCOPENSHELL_ROOT}/lib/cmake/IfcOpenShell"
    CACHE PATH
    "IfcOpenShell CMake package directory"
    FORCE
)

set(
    Boost_DIR
    "${IFCOPENSHELL_BOOST_ROOT}/lib/cmake/Boost-1.86.0"
    CACHE PATH
    "Boost CMake package directory"
    FORCE
)

set(
    Boost_ROOT
    "${IFCOPENSHELL_BOOST_ROOT}"
    CACHE PATH
    "Boost installation directory"
    FORCE
)

set(
    BOOST_ROOT
    "${IFCOPENSHELL_BOOST_ROOT}"
    CACHE PATH
    "Boost installation directory"
    FORCE
)

set(Boost_USE_STATIC_LIBS ON)
set(Boost_USE_STATIC_RUNTIME OFF)
set(Boost_USE_MULTITHREADED ON)

set(
    OpenCASCADE_DIR
    "${IFCOPENSHELL_DEPS_ROOT}/opencascade-7.8.1-new-layout/cmake"
    CACHE PATH
    "OpenCascade CMake package directory"
    FORCE
)

set(
    CGAL_DIR
    "${IFCOPENSHELL_DEPS_ROOT}/cgal/lib/cmake/CGAL"
    CACHE PATH
    "CGAL CMake package directory"
    FORCE
)

set(
    HDF5_ROOT
    "${IFCOPENSHELL_DEPS_ROOT}/HDF5-1_13_1-win64"
    CACHE PATH
    "HDF5 installation directory"
    FORCE
)

set(
    PROJ_DIR
    "${IFCOPENSHELL_DEPS_ROOT}/proj-9.4.1/lib/cmake/proj"
    CACHE PATH
    "PROJ CMake package directory"
    FORCE
)

set(
    RocksDB_DIR
    "${IFCOPENSHELL_DEPS_ROOT}/rocksdb/lib/cmake/rocksdb"
    CACHE PATH
    "RocksDB CMake package directory"
    FORCE
)

set(
    zstd_DIR
    "${IFCOPENSHELL_DEPS_ROOT}/zstd/lib/cmake/zstd"
    CACHE PATH
    "zstd CMake package directory"
    FORCE
)

set(
    OPENCOLLADA_INCLUDE_DIR
    "${IFCOPENSHELL_DEPS_ROOT}/OpenCOLLADA/include/opencollada"
    CACHE PATH
    "OpenCOLLADA include directory"
    FORCE
)

set(
    OPENCOLLADA_LIBRARY_DIR
    "${IFCOPENSHELL_DEPS_ROOT}/OpenCOLLADA/lib/opencollada"
    CACHE PATH
    "OpenCOLLADA library directory"
    FORCE
)

set(
    LIBXML2_INCLUDE_DIR
    "${IFCOPENSHELL_SOURCE_ROOT}/_deps/OpenCOLLADA/Externals/LibXML/include"
    CACHE PATH
    "LibXml2 include directory"
    FORCE
)

find_library(
    IFCOPENSHELL_LIBXML2_LIBRARY
    NAMES
        xml_static
        libxml2
        xml2
        xml
    PATHS
        "${IFCOPENSHELL_DEPS_ROOT}/OpenCOLLADA/lib/opencollada"
        "${IFCOPENSHELL_SOURCE_ROOT}/_deps/OpenCOLLADA/_build-vs2022-x64/Externals/LibXML/Release"
    NO_DEFAULT_PATH
)

if(IFCOPENSHELL_LIBXML2_LIBRARY)
    set(
        LIBXML2_LIBRARY
        "${IFCOPENSHELL_LIBXML2_LIBRARY}"
        CACHE FILEPATH
        "LibXml2 library"
        FORCE
    )

    set(
        LIBXML2_LIBRARIES
        "${IFCOPENSHELL_LIBXML2_LIBRARY}"
        CACHE FILEPATH
        "LibXml2 libraries"
        FORCE
    )
endif()

set(
    IFC_GENERATED_PACKAGES_DIR
    "${CMAKE_CURRENT_BINARY_DIR}/generated-packages"
)

file(MAKE_DIRECTORY "${IFC_GENERATED_PACKAGES_DIR}/Eigen3")
file(MAKE_DIRECTORY "${IFC_GENERATED_PACKAGES_DIR}/nlohmann_json")

file(
    TO_CMAKE_PATH
    "${IFCOPENSHELL_DEPS_ROOT}/Eigen"
    IFC_EIGEN_INCLUDE_DIR
)

file(
    TO_CMAKE_PATH
    "${IFCOPENSHELL_DEPS_ROOT}/json"
    IFC_NLOHMANN_JSON_INCLUDE_DIR
)

file(
    WRITE
    "${IFC_GENERATED_PACKAGES_DIR}/Eigen3/Eigen3Config.cmake"
    "if(NOT TARGET Eigen3::Eigen)\n"
    "    add_library(Eigen3::Eigen INTERFACE IMPORTED)\n"
    "    set_target_properties(\n"
    "        Eigen3::Eigen\n"
    "        PROPERTIES\n"
    "            INTERFACE_INCLUDE_DIRECTORIES \"${IFC_EIGEN_INCLUDE_DIR}\"\n"
    "    )\n"
    "endif()\n"
    "set(Eigen3_FOUND TRUE)\n"
    "set(EIGEN3_FOUND TRUE)\n"
)

file(
    WRITE
    "${IFC_GENERATED_PACKAGES_DIR}/nlohmann_json/nlohmann_jsonConfig.cmake"
    "if(NOT TARGET nlohmann_json::nlohmann_json)\n"
    "    add_library(nlohmann_json::nlohmann_json INTERFACE IMPORTED)\n"
    "    set_target_properties(\n"
    "        nlohmann_json::nlohmann_json\n"
    "        PROPERTIES\n"
    "            INTERFACE_INCLUDE_DIRECTORIES \"${IFC_NLOHMANN_JSON_INCLUDE_DIR}\"\n"
    "    )\n"
    "endif()\n"
    "set(nlohmann_json_FOUND TRUE)\n"
)

set(
    Eigen3_DIR
    "${IFC_GENERATED_PACKAGES_DIR}/Eigen3"
    CACHE PATH
    "Eigen3 CMake package directory"
    FORCE
)

set(
    nlohmann_json_DIR
    "${IFC_GENERATED_PACKAGES_DIR}/nlohmann_json"
    CACHE PATH
    "nlohmann/json CMake package directory"
    FORCE
)

find_package(IfcOpenShell CONFIG REQUIRED)
find_package(nlohmann_json CONFIG REQUIRED)

foreach(
    IFC_REQUIRED_TARGET
    IN ITEMS
        IfcOpenShell::IfcParse
        IfcOpenShell::IfcGeom
        IfcOpenShell::geometry_kernel_opencascade
)
    if(NOT TARGET "${IFC_REQUIRED_TARGET}")
        message(
            FATAL_ERROR
            "Required IfcOpenShell target was not exported: "
            "${IFC_REQUIRED_TARGET}"
        )
    endif()
endforeach()

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
)

target_compile_definitions(
    ifc-cli
    PRIVATE
        BOOST_ALL_NO_LIB
        NOMINMAX
        WIN32_LEAN_AND_MEAN
        _CRT_SECURE_NO_WARNINGS
        _SCL_SECURE_NO_WARNINGS
        _DISABLE_CONSTEXPR_MUTEX_CONSTRUCTOR
)

target_compile_options(
    ifc-cli
    PRIVATE
        /W4
        /permissive-
        /bigobj
)

target_link_libraries(
    ifc-cli
    PRIVATE
        IfcOpenShell::geometry_kernel_opencascade
        IfcOpenShell::IfcGeom
        IfcOpenShell::IfcParse
        nlohmann_json::nlohmann_json
)