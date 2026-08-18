"""Yuanta broker adapter. Only this package may import vendor COM/OCX types.

Feature 01 ships mock gateways only (`MockTradeGateway`, `MockQuoteGateway`) so the
rest of the codebase can build and test without the vendor API installed. Real
COM-backed gateways (`comtypes` + `AtlAxCreateControlEx`, bitness-matched per
`元大API交易PYTHON注意事項.docx`) land in Feature 02 — see this package's README.md.
"""
