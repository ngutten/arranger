cd audio_server && mkdir build && cd build
cmake -DENABLE_PYTHON_BINDINGS=ON ..
make arranger_engine    # builds the .so into standalone/
make audio_server       # still works independently

#cmake -DCMAKE_BUILD_TYPE=Release -B build
#cmake --build build -j$(nproc)
