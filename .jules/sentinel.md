## 2026-07-28 - Prevent XXE Vulnerability in RSS Feeds
**Vulnerability:** Used `xml.etree.ElementTree.fromstring` to parse external XML/RSS payloads in `trading_bot/sentiment/context.py`, which is vulnerable to XML External Entity (XXE) and Billion Laughs attacks.
**Learning:** External feeds are untrusted data. The standard Python library `xml.etree` does not protect against malicious XML documents.
**Prevention:** Always use `defusedxml` when parsing XML from external or untrusted sources.
## 2026-07-29 - SQLite Database File Permissions
**Vulnerability:** SQLite database files were created with default permissions (e.g. 0o644), exposing sensitive trading data (orders, kill switch, PnL) to unauthorized local users.
**Learning:** Default permissions on files created programmatically inherit from the user's umask, which is often too permissive for sensitive files.
**Prevention:** Explicitly set secure file permissions using `os.chmod(db_path, 0o600)` immediately after initializing SQLite database files.
