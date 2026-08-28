"""Adapters for Yuanta's documented order and quote ActiveX controls.

Only this package imports vendor COM types. Application and domain code depend on
ports, allowing tests to substitute the mock gateways without installed OCX files.
"""
