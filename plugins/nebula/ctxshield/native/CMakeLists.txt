cmake_minimum_required(VERSION 3.20)
project(ctxshield_native LANGUAGES C CXX)

# Build doc Part IX. Step 3: CPU backend. Step 8: C ABI + Metal (Apple) + CUDA (UNTESTED, L4).
if(APPLE)
  option(SHIELD_METAL "Build the Metal backend (Apple)" ON)
else()
  option(SHIELD_METAL "Build the Metal backend (Apple)" OFF)
endif()
option(SHIELD_CUDA "Build the CUDA backend (UNTESTED until an L4 run)" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(PYBIND11_FINDPYTHON ON)
find_package(pybind11 CONFIG REQUIRED)

add_library(utf8proc STATIC third_party/utf8proc/utf8proc.c)   # includes utf8proc_data.c
target_compile_definitions(utf8proc PUBLIC UTF8PROC_STATIC)
target_include_directories(utf8proc PUBLIC third_party/utf8proc)
set_target_properties(utf8proc PROPERTIES POSITION_INDEPENDENT_CODE ON)

set(SHIELD_SOURCES src/featurize.cpp src/score.cpp src/c_abi.cpp python/bind.cpp)

if(SHIELD_METAL)
  if(NOT APPLE)
    message(FATAL_ERROR "SHIELD_METAL=ON requires Apple")
  endif()
  enable_language(OBJCXX)
  # shield.metal -> shield.air -> shield.metallib -> embedded as bytes (metallib_data.h); the
  # library ships inside the extension, so the kernel binary is bound to the module's sha.
  set(METAL_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src/metal/shield.metal)
  set(METAL_AIR ${CMAKE_CURRENT_BINARY_DIR}/shield.air)
  set(METAL_LIB ${CMAKE_CURRENT_BINARY_DIR}/shield.metallib)
  set(METAL_HDR ${CMAKE_CURRENT_BINARY_DIR}/metallib_data.h)
  add_custom_command(OUTPUT ${METAL_AIR}
    COMMAND xcrun -sdk macosx metal -O3 -std=metal3.0 -Wall -c ${METAL_SRC} -o ${METAL_AIR}
    DEPENDS ${METAL_SRC} VERBATIM)
  add_custom_command(OUTPUT ${METAL_LIB}
    COMMAND xcrun -sdk macosx metallib ${METAL_AIR} -o ${METAL_LIB}
    DEPENDS ${METAL_AIR} VERBATIM)
  add_custom_command(OUTPUT ${METAL_HDR}
    COMMAND ${CMAKE_COMMAND} -DIN=${METAL_LIB} -DOUT=${METAL_HDR} -P ${CMAKE_CURRENT_SOURCE_DIR}/src/metal/embed.cmake
    DEPENDS ${METAL_LIB} ${CMAKE_CURRENT_SOURCE_DIR}/src/metal/embed.cmake VERBATIM)
  list(APPEND SHIELD_SOURCES src/metal/metal_backend.mm ${METAL_HDR})
endif()

if(SHIELD_CUDA)
  enable_language(CUDA)
  set(CMAKE_CUDA_STANDARD 17)
  if(NOT CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES 89)   # L4 (Ada)
  endif()
  find_package(CUDAToolkit REQUIRED)
  list(APPEND SHIELD_SOURCES src/cuda/shield.cu src/cuda/cuda_backend.cpp)
endif()

pybind11_add_module(_native ${SHIELD_SOURCES})
target_include_directories(_native PRIVATE include src ${CMAKE_CURRENT_BINARY_DIR})
target_link_libraries(_native PRIVATE utf8proc)

if(APPLE AND CMAKE_SYSTEM_PROCESSOR MATCHES "arm64")
  set(SHIELD_ARCH_FLAGS -O3 -mcpu=apple-m1)
else()
  set(SHIELD_ARCH_FLAGS -O3 -march=native)
endif()
target_compile_options(_native PRIVATE $<$<COMPILE_LANGUAGE:CXX,OBJCXX>:${SHIELD_ARCH_FLAGS} -Wall -Wextra>)
target_compile_options(utf8proc PRIVATE -O3)

if(SHIELD_METAL)
  target_compile_definitions(_native PRIVATE SHIELD_HAVE_METAL=1)
  set_source_files_properties(src/metal/metal_backend.mm PROPERTIES COMPILE_FLAGS "-fobjc-arc")
  target_link_libraries(_native PRIVATE "-framework Metal" "-framework Foundation")
endif()
if(SHIELD_CUDA)
  target_compile_definitions(_native PRIVATE SHIELD_HAVE_CUDA=1 SHIELD_CUDA_VERIFIED=0)
  set_target_properties(_native PROPERTIES CUDA_SEPARABLE_COMPILATION OFF)
  target_include_directories(_native PRIVATE src/cuda)
  target_compile_options(_native PRIVATE $<$<COMPILE_LANGUAGE:CUDA>:-O3 -lineinfo>)
  target_link_libraries(_native PRIVATE CUDA::cudart)
endif()

# Stage-split profiler (no Python): build-*/shield_bench <docs.bin>
add_executable(shield_bench bench/bench_kernel.cpp src/featurize.cpp src/score.cpp)
target_include_directories(shield_bench PRIVATE include src)
target_link_libraries(shield_bench PRIVATE utf8proc)
target_compile_options(shield_bench PRIVATE ${SHIELD_ARCH_FLAGS} -Wall -Wextra)
target_compile_definitions(shield_bench PRIVATE SHIELD_PROFILE=1)
