# cmake/mingw-w64-x86_64.cmake
# Cross-compilation toolchain for building Windows x64 binaries from Linux.
#
# Usage:
#   cmake -B build-win -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-w64-x86_64.cmake ..
#
# Prerequisites (Ubuntu/Debian):
#   sudo apt install mingw-w64
#   # Plus cross-compiled versions of portaudio, fluidsynth, lilv — see BUILDING.md

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

# Toolchain binaries
set(MINGW_PREFIX x86_64-w64-mingw32)
set(CMAKE_C_COMPILER   ${MINGW_PREFIX}-gcc)
set(CMAKE_CXX_COMPILER ${MINGW_PREFIX}-g++)
set(CMAKE_RC_COMPILER  ${MINGW_PREFIX}-windres)
set(CMAKE_AR           ${MINGW_PREFIX}-ar)
set(CMAKE_RANLIB       ${MINGW_PREFIX}-ranlib)

# Where to find cross-compiled libraries.
# Override at cmake invocation time with -DMINGW_SYSROOT=/your/path
# Default: /usr/local/mingw-sysroot (populated by deps/build_mingw_deps.sh)
if(NOT DEFINED MINGW_SYSROOT)
    set(MINGW_SYSROOT "/usr/local/mingw-sysroot")
endif()

set(CMAKE_FIND_ROOT_PATH ${MINGW_SYSROOT})

# Search for programs in the build host directories
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
# Search for libraries and headers in the target sysroot
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# pkg-config: point at cross-compiled .pc files
set(ENV{PKG_CONFIG_PATH} "${MINGW_SYSROOT}/lib/pkgconfig:${MINGW_SYSROOT}/share/pkgconfig")
set(ENV{PKG_CONFIG_LIBDIR} "${MINGW_SYSROOT}/lib/pkgconfig")
set(ENV{PKG_CONFIG_SYSROOT_DIR} "${MINGW_SYSROOT}")

# Static linking preferred for Windows distribution
# (avoids needing to ship MinGW runtime DLLs)
set(BUILD_SHARED_LIBS OFF)
set(CMAKE_EXE_LINKER_FLAGS "-static-libgcc -static-libstdc++ -static")
