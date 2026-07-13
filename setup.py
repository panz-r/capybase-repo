"""Build shim for the _cdiff C extension.

pyproject.toml drives the pure-Python packaging; this shim adds the single
compiled extension (capybase._cdiff). setuptools merges both, so the
declarative config in pyproject.toml plus the ext_modules here produce a
wheel that includes the .so.

Rebuild after editing _cdiff.c:

    pip install -e . --force-reinstall --no-build-isolation

The extension is optional: capybase.diff falls back to a pure-Python
implementation when the import fails (no compiler / failed build).
"""

from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "capybase._cdiff",
            sources=["src/capybase/_cdiff.c"],
            extra_compile_args=["-O2"],
        ),
    ],
)
