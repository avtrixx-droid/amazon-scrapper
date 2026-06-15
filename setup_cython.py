"""
setup_cython.py — Compile license.py and scraper.py to native C extensions.

Usage (build step, before PyInstaller):
    python setup_cython.py build_ext --inplace

Produces .so (macOS/Linux) or .pyd (Windows) files that PyInstaller bundles
instead of the .py source. These cannot be decompiled back to Python.
"""

from setuptools import setup
from Cython.Build import cythonize

setup(
    ext_modules=cythonize(
        ["license.py", "scraper.py"],
        compiler_directives={
            "language_level": "3",
        },
    ),
)
