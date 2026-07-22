cmake_minimum_required(VERSION 3.21)

if(POLICY CMP0144)
    cmake_policy(SET CMP0144 NEW)
endif()

if(POLICY CMP0207)
    cmake_policy(SET CMP0207 NEW)
endif()

project(ifc-cli LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

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

if(WIN32)
    if(NOT MSVC)
        message(FATAL_ERROR "The Windows build requires MSVC.")
    endif()

    if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
        message(FATAL_ERROR "The Windows build requires a 64-bit generator.")
    endif()

    set(
        IFCOPENSHELL_ROOT
        "${CMAKE_CURRENT_SOURCE_DIR}/IfcOpenShell/_installed-vs2022-x64"
        CACHE PATH
        "IfcOpenShell installation directory"
    )

    set(
        IFCOPENSHELL_DEPS_ROOT
        "${CMAKE_CURRENT_SOURCE_DIR}/IfcOpenShell/_deps-vs2022-x64-installed"
        CACHE PATH
        "IfcOpenShell dependency installation directory"
    )

    set(
        IFCOPENSHELL_BOOST_ROOT
        "${CMAKE_CURRENT_SOURCE_DIR}/IfcOpenShell/_deps/boost_1_86_0/stage/vs2022-x64"
        CACHE PATH
        "IfcOpenShell Boost installation directory"
    )

    set(
        IfcOpenShell_DIR
        "${IFCOPENSHELL_ROOT}/lib/cmake/IfcOpenShell"
        CACHE PATH
        "Directory containing IfcOpenShellConfig.cmake"
    )

    set(
        Boost_DIR
        "${IFCOPENSHELL_BOOST_ROOT}/lib/cmake/Boost-1.86.0"
        CACHE PATH
        "Directory containing BoostConfig.cmake"
    )

    set(
        Boost_ROOT
        "${IFCOPENSHELL_BOOST_ROOT}"
        CACHE PATH
        "Boost installation directory"
    )

    set(BOOST_ROOT "${IFCOPENSHELL_BOOST_ROOT}")

    set(
        OpenCASCADE_DIR
        "${IFCOPENSHELL_DEPS_ROOT}/opencascade-7.8.1-new-layout/cmake"
        CACHE PATH
        "Directory containing OpenCASCADEConfig.cmake"
    )

    set(
        HDF5_DIR
        "${IFCOPENSHELL_DEPS_ROOT}/HDF5-1_13_1-win64/cmake"
        CACHE PATH
        "Directory containing HDF5Config.cmake"
    )

    set(
        PROJ_DIR
        "${IFCOPENSHELL_DEPS_ROOT}/proj-9.4.1/lib/cmake/proj"
        CACHE PATH
        "Directory containing PROJConfig.cmake"
    )

    set(
        RocksDB_DIR
        "${IFCOPENSHELL_DEPS_ROOT}/rocksdb/lib/cmake/rocksdb"
        CACHE PATH
        "Directory containing RocksDBConfig.cmake"
    )

    set(
        zstd_DIR
        "${IFCOPENSHELL_DEPS_ROOT}/zstd/lib/cmake/zstd"
        CACHE PATH
        "Directory containing zstdConfig.cmake"
    )

    set(
        OpenCOLLADA_DIR
        "${IFCOPENSHELL_DEPS_ROOT}/OpenCOLLADA/lib/opencollada/cmake"
        CACHE PATH
        "OpenCOLLADA CMake directory"
    )

    foreach(required_path IN ITEMS
        "${IfcOpenShell_DIR}/IfcOpenShellConfig.cmake"
        "${Boost_DIR}/BoostConfig.cmake"
        "${OpenCASCADE_DIR}"
    )
        if(NOT EXISTS "${required_path}")
            message(
                FATAL_ERROR
                "Required Windows dependency path does not exist: ${required_path}"
            )
        endif()
    endforeach()

    list(
        PREPEND CMAKE_PREFIX_PATH
        "${IFCOPENSHELL_ROOT}"
        "${IFCOPENSHELL_BOOST_ROOT}"
        "${IFCOPENSHELL_DEPS_ROOT}/HDF5-1_13_1-win64"
        "${IFCOPENSHELL_DEPS_ROOT}/OpenCOLLADA"
        "${IFCOPENSHELL_DEPS_ROOT}/opencascade-7.8.1-new-layout"
        "${IFCOPENSHELL_DEPS_ROOT}/proj-9.4.1"
        "${IFCOPENSHELL_DEPS_ROOT}/rocksdb"
        "${IFCOPENSHELL_DEPS_ROOT}/zstd"
    )

    list(PREPEND CMAKE_MODULE_PATH "${IfcOpenShell_DIR}")

    set(
        _generated_packages
        "${CMAKE_CURRENT_BINARY_DIR}/generated-packages"
    )

    set(
        _eigen_config_dir
        "${_generated_packages}/Eigen3"
    )

    file(MAKE_DIRECTORY "${_eigen_config_dir}")

    file(
        WRITE
        "${_eigen_config_dir}/Eigen3Config.cmake"
        "if(NOT TARGET Eigen3::Eigen)\n"
        "    add_library(Eigen3::Eigen INTERFACE IMPORTED GLOBAL)\n"
        "    set_target_properties(Eigen3::Eigen PROPERTIES INTERFACE_INCLUDE_DIRECTORIES \"${IFCOPENSHELL_DEPS_ROOT}/Eigen\")\n"
        "endif()\n"
        "set(Eigen3_FOUND TRUE)\n"
        "set(EIGEN3_FOUND TRUE)\n"
        "set(EIGEN3_INCLUDE_DIR \"${IFCOPENSHELL_DEPS_ROOT}/Eigen\")\n"
        "set(EIGEN3_INCLUDE_DIRS \"${IFCOPENSHELL_DEPS_ROOT}/Eigen\")\n"
    )

    set(
        Eigen3_DIR
        "${_eigen_config_dir}"
        CACHE PATH
        "Generated Eigen3 package directory"
        FORCE
    )

    set(
        _json_config_dir
        "${_generated_packages}/nlohmann_json"
    )

    file(MAKE_DIRECTORY "${_json_config_dir}")

    file(
        WRITE
        "${_json_config_dir}/nlohmann_jsonConfig.cmake"
        "if(NOT TARGET nlohmann_json::nlohmann_json)\n"
        "    add_library(nlohmann_json::nlohmann_json INTERFACE IMPORTED GLOBAL)\n"
        "    set_target_properties(nlohmann_json::nlohmann_json PROPERTIES INTERFACE_INCLUDE_DIRECTORIES \"${IFCOPENSHELL_DEPS_ROOT}/json\")\n"
        "endif()\n"
        "set(nlohmann_json_FOUND TRUE)\n"
    )

    set(
        nlohmann_json_DIR
        "${_json_config_dir}"
        CACHE PATH
        "Generated nlohmann_json package directory"
        FORCE
    )

    if(EXISTS "${IFCOPENSHELL_DEPS_ROOT}/cgal/lib/cmake/CGAL")
        set(
            CGAL_DIR
            "${IFCOPENSHELL_DEPS_ROOT}/cgal/lib/cmake/CGAL"
            CACHE PATH
            "Directory containing CGALConfig.cmake"
        )
    endif()

    set(BOOST_ALL_NO_LIB ON)
    set(Boost_NO_WARN_NEW_VERSIONS ON)

    find_package(Eigen3 CONFIG REQUIRED)
    find_package(nlohmann_json CONFIG REQUIRED)
    find_package(IfcOpenShell CONFIG REQUIRED)

    foreach(required_target IN ITEMS
        IfcOpenShell::IfcParse
        IfcOpenShell::IfcGeom
        IfcOpenShell::geometry_kernel_opencascade
    )
        if(NOT TARGET "${required_target}")
            message(
                FATAL_ERROR
                "IfcOpenShell package did not export target ${required_target}."
            )
        endif()
    endforeach()

    target_include_directories(
        ifc-cli
        PRIVATE
            "${IFCOPENSHELL_ROOT}/include"
    )

    target_link_libraries(
        ifc-cli
        PRIVATE
            IfcOpenShell::geometry_kernel_opencascade
            IfcOpenShell::IfcGeom
            IfcOpenShell::IfcParse
            nlohmann_json::nlohmann_json
            Eigen3::Eigen
    )

    target_compile_definitions(
        ifc-cli
        PRIVATE
            BOOST_ALL_NO_LIB
            NOMINMAX
            WIN32_LEAN_AND_MEAN
            _CRT_SECURE_NO_WARNINGS
    )

    target_compile_options(
        ifc-cli
        PRIVATE
            /W4
            /permissive-
            /bigobj
            /utf-8
    )

    set(
        CMAKE_INSTALL_SYSTEM_RUNTIME_DESTINATION
        "."
    )

    include(InstallRequiredSystemLibraries)

    set(
        _runtime_dependency_roots
        "${IFCOPENSHELL_ROOT}"
        "${IFCOPENSHELL_DEPS_ROOT}"
        "${IFCOPENSHELL_BOOST_ROOT}"
    )

    set(_runtime_search_directories)

    foreach(_runtime_root IN LISTS _runtime_dependency_roots)
        if(NOT EXISTS "${_runtime_root}")
            continue()
        endif()

        file(
            GLOB_RECURSE _runtime_dlls
            LIST_DIRECTORIES FALSE
            "${_runtime_root}/*.dll"
        )

        foreach(_runtime_dll IN LISTS _runtime_dlls)
            get_filename_component(
                _runtime_directory
                "${_runtime_dll}"
                DIRECTORY
            )

            list(
                APPEND
                _runtime_search_directories
                "${_runtime_directory}"
            )
        endforeach()
    endforeach()

    list(
        REMOVE_DUPLICATES
        _runtime_search_directories
    )

    if(NOT _runtime_search_directories)
        message(
            WARNING
            "No Windows runtime dependency directories were detected."
        )
    endif()

    install(
        TARGETS ifc-cli

        RUNTIME_DEPENDENCIES
            PRE_EXCLUDE_REGEXES
                "[Aa][Pp][Ii]-[Mm][Ss]-[Ww][Ii][Nn]-.*"
                "[Ee][Xx][Tt]-[Mm][Ss]-[Ww][Ii][Nn]-.*"

            POST_EXCLUDE_REGEXES
                ".*[\\\\/][Ww][Ii][Nn][Dd][Oo][Ww][Ss][\\\\/][Ss][Yy][Ss][Tt][Ee][Mm]32[\\\\/].*"
                ".*[\\\\/][Ww][Ii][Nn][Dd][Oo][Ww][Ss][\\\\/][Ss][Yy][Ss][Ww][Oo][Ww]64[\\\\/].*"

            DIRECTORIES
                ${_runtime_search_directories}

        RUNTIME
            DESTINATION "."
    )

    install(
        FILES
            "${CMAKE_CURRENT_SOURCE_DIR}/README.md"
        DESTINATION "."
    )

    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/LICENSE")
        install(
            FILES
                "${CMAKE_CURRENT_SOURCE_DIR}/LICENSE"
            DESTINATION "."
        )
    endif()

elseif(APPLE)
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

    target_include_directories(
        ifc-cli
        PRIVATE
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
        target_link_libraries(
            ifc-cli
            PRIVATE
                Boost::headers
        )
    elseif(TARGET Boost::boost)
        target_link_libraries(
            ifc-cli
            PRIVATE
                Boost::boost
        )
    endif()

    target_compile_options(
        ifc-cli
        PRIVATE
            -Wall
            -Wextra
            -Wpedantic
    )

    set_target_properties(
        ifc-cli
        PROPERTIES
            BUILD_RPATH "${IFCOPENSHELL_ROOT}/lib"
            INSTALL_RPATH "${IFCOPENSHELL_ROOT}/lib"
            MACOSX_RPATH ON
    )

else()
    message(
        FATAL_ERROR
        "Only Windows x64 and macOS are currently supported."
    )
endif()