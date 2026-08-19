"""Yuanta broker adapter. Only this package may import vendor (`pythonnet`/
`YuantaOneAPI`) types.

Feature 01 shipped mock gateways only (`MockTradeGateway`, `MockQuoteGateway`) so the
rest of the codebase can build and test without the vendor API installed. Feature 02's
real session (`spark_client.py`/`spark_api_adapter.py`, `pythonnet`-backed, driving
`YuantaSparkAPITrader`) — see this package's README.md.
"""
