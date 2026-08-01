## 2026-07-28 - Prevent XXE Vulnerability in RSS Feeds
**Vulnerability:** Used `xml.etree.ElementTree.fromstring` to parse external XML/RSS payloads in `trading_bot/sentiment/context.py`, which is vulnerable to XML External Entity (XXE) and Billion Laughs attacks.
**Learning:** External feeds are untrusted data. The standard Python library `xml.etree` does not protect against malicious XML documents.
**Prevention:** Always use `defusedxml` when parsing XML from external or untrusted sources.

## 2026-08-01 - Secure SQLite DB file permissions
**Vulnerability:** Several SQLite database files were created across the application without explicitly setting file permissions to user-only read/write (`0o600`), potentially exposing sensitive trading and configuration data if created in a shared environment.
**Learning:** In projects that instantiate multiple SQLite databases across various storage subsystems (e.g., data store, cache, research, memory, ORM), a vulnerability pattern exists where secure file permissions are only enforced on the main DB (e.g. ledger) and missed on ancillary files.
**Prevention:** Ensure explicit `os.chmod(<db_path>, 0o600)` calls are placed immediately after initial DB file creation in all modules that instantiate SQLite databases.
