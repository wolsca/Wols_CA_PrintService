cmake_minimum_required(VERSION 3.20)

# The project is pure Python, so no compiler/toolchain is required.
# CMake is used only as the project model so that CLion can load the sources
# and provide ready-to-use run targets.
project(Wols_CA_PrintService LANGUAGES NONE)

set(SERVICE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Wols_CA_PrintService")
set(SERVICE_SCRIPT "${SERVICE_DIR}/Wols_CA_PrintService.py")
set(SERVICE_CONFIG "${SERVICE_DIR}/WolsCAPrintService.json")

if (WIN32)
    set(PYTHON_EXECUTABLE_NAME "python" CACHE STRING "Python interpreter used to run the service")
else ()
    set(PYTHON_EXECUTABLE_NAME "python3" CACHE STRING "Python interpreter used to run the service")
endif ()

# Make the sources visible in the CLion project tree.
add_custom_target(sources SOURCES
        "${SERVICE_SCRIPT}"
        "${SERVICE_CONFIG}"
        "${CMAKE_CURRENT_SOURCE_DIR}/requirements.txt"
        "${CMAKE_CURRENT_SOURCE_DIR}/README.md"
        "${CMAKE_CURRENT_SOURCE_DIR}/CHANGELOG.md"
        "${CMAKE_CURRENT_SOURCE_DIR}/docs/USER_GUIDE.md"
        "${CMAKE_CURRENT_SOURCE_DIR}/.github/workflows/ci.yml"
        "${CMAKE_CURRENT_SOURCE_DIR}/LICENSE"
        "${CMAKE_CURRENT_SOURCE_DIR}/deploy/debian/README.md"
        "${CMAKE_CURRENT_SOURCE_DIR}/deploy/debian/install.sh"
        "${CMAKE_CURRENT_SOURCE_DIR}/deploy/debian/uninstall.sh"
        "${CMAKE_CURRENT_SOURCE_DIR}/deploy/debian/wolsca-print-service.service"
        "${CMAKE_CURRENT_SOURCE_DIR}/deploy/debian/WolsCAPrintService.linux.json")

# Install the Python dependencies of the service.
add_custom_target(install_requirements
        COMMAND "${PYTHON_EXECUTABLE_NAME}" -m pip install -r "${CMAKE_CURRENT_SOURCE_DIR}/requirements.txt"
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        COMMENT "Installing Python dependencies"
        USES_TERMINAL VERBATIM)

# Start the print service (watcher + MQTT bridge).
add_custom_target(run_service
        COMMAND "${PYTHON_EXECUTABLE_NAME}" "${SERVICE_SCRIPT}"
        WORKING_DIRECTORY "${SERVICE_DIR}"
        COMMENT "Starting Wols CA Print Service"
        USES_TERMINAL VERBATIM)

# Run the Windows-only zero-touch virtual printer deployment.
add_custom_target(install_printer
        COMMAND "${PYTHON_EXECUTABLE_NAME}" "${SERVICE_SCRIPT}" --install-printer
        WORKING_DIRECTORY "${SERVICE_DIR}"
        COMMENT "Deploying the virtual printer (requires Administrator privileges)"
        USES_TERMINAL VERBATIM)
