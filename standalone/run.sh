PYTHONMALLOC=malloc valgrind --tool=memcheck --free-fill=0x55 --undef-value-errors=no python main.py
