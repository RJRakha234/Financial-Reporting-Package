"""Data layer: fetching, storage, integrity, and the source abstraction.

Intentionally free of eager re-exports. ``cse.config`` imports
``cse.data.schema`` for interval validation, so pulling submodules in here
would create an import cycle (config -> data -> store -> config). Import the
submodule you need directly.
"""
