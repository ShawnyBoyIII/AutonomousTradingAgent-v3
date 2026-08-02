## 2026-07-28 - Prevent XXE Vulnerability in RSS Feeds
**Vulnerability:** Used `xml.etree.ElementTree.fromstring` to parse external XML/RSS payloads in `trading_bot/sentiment/context.py`, which is vulnerable to XML External Entity (XXE) and Billion Laughs attacks.
**Learning:** External feeds are untrusted data. The standard Python library `xml.etree` does not protect against malicious XML documents.
**Prevention:** Always use `defusedxml` when parsing XML from external or untrusted sources.

## 2026-08-01 - Secure SQLite DB file permissions
**Vulnerability:** Several SQLite database files were created across the application without explicitly setting file permissions to user-only read/write (`0o600`), potentially exposing sensitive trading and configuration data if created in a shared environment.
**Learning:** In projects that instantiate multiple SQLite databases across various storage subsystems (e.g., data store, cache, research, memory, ORM), a vulnerability pattern exists where secure file permissions are only enforced on the main DB (e.g. ledger) and missed on ancillary files.
**Prevention:** Ensure explicit `os.chmod(<db_path>, 0o600)` calls are placed immediately after initial DB file creation in all modules that instantiate SQLite databases.

## 2026-08-02 - Secure Table Names in Dynamic SQL
**Vulnerability:** Used f-string string interpolation (`f"SELECT COUNT(*) FROM {t}"`) in `trading_bot/cli/app.py` for executing SQLite queries. Although the input `t` came from a hardcoded list, using string interpolation for SQL breaks strict SAST tooling rules and risks SQL injection if the list was ever made dynamic.
**Learning:** SQLite parameterized queries (`?`) cannot be used for table names, leading developers to use dangerous f-strings for dynamic tables.
**Prevention:** Avoid string formatting in `cur.execute()`. When executing SQL queries where table names cannot be parameterized, use explicit `if/elif` blocks with hardcoded, literal query strings to enforce a strict whitelist and appease static security analysis tools.
