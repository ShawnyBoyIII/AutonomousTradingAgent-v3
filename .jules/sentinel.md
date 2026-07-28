## 2026-07-28 - Prevent XXE Vulnerability in RSS Feeds
**Vulnerability:** Used `xml.etree.ElementTree.fromstring` to parse external XML/RSS payloads in `trading_bot/sentiment/context.py`, which is vulnerable to XML External Entity (XXE) and Billion Laughs attacks.
**Learning:** External feeds are untrusted data. The standard Python library `xml.etree` does not protect against malicious XML documents.
**Prevention:** Always use `defusedxml` when parsing XML from external or untrusted sources.
