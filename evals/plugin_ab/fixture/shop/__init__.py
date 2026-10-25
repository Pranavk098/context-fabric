# mypy: ignore-errors

"""Toy shop domain for the plugin A/B benchmark fixture.

This ``fixture/`` tree is excluded from mypy (``# mypy: ignore-errors`` in every
file): it is a standalone toy project whose ``shop`` package is not importable
under the ``evals.plugin_ab`` namespace, and its own tests use ``pytest``. The
harness code under ``evals/plugin_ab/*.py`` stays strict.
"""
