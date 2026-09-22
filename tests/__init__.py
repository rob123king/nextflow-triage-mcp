"""Test package.

Making `tests` a package (rather than a bare directory) is what lets test
modules import shared helpers from `tests.factories`. `conftest.py` stays
for fixtures only: it is auto-discovered by pytest and importing from it
directly works by accident rather than by design.
"""
