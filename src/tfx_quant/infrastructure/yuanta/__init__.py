"""Yuanta broker adapter. Only this package may import vendor (`comtypes`/
`YuantaOneAPI`) types.

Feature 01 shipped a mock trade gateway only (`MockTradeGateway`) so the rest of the
codebase can build and test without the vendor API installed — there is no quote gateway
here at all, mock or real, since market data comes entirely from `yfinance`. Feature 02's
real session (`spark_client.py`/`spark_api_adapter.py`, `pythonnet`-backed, driving
`YuantaSparkAPITrader`) — see this package's README.md.
"""
