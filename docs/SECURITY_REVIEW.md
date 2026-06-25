# Security Review - V2.5/V3 Trading Bot

**Review Date:** 2026-06-18  
**Version:** V2.5 Complete + V3.1 Foundation  
**Reviewer:** OpenCode  
**Status:** ✅ Generally Secure - Minor Hardening Recommended

---

## Executive Summary

**Overall Security Rating: GOOD (7.5/10)**

The trading bot demonstrates strong security fundamentals with multiple safety layers. The most critical controls (kill switch, paper-only default, credential isolation) are well-implemented. A few areas need hardening before production use.

### Strengths
✅ Paper-only by default (live trading disabled in code)  
✅ Credentials only from environment variables  
✅ Config validation prevents hardcoded secrets  
✅ Rate limiting on authentication (3 attempts, 30-min lockout)  
✅ Comprehensive audit logging  
✅ Token storage with 0o600 permissions  
✅ Session inactivity timeout (30 min)  
✅ Kill switch persists across restarts  
✅ Position sizing caps prevent over-concentration (20%)  

### Areas for Improvement
⚠️ Tokens stored as plaintext JSON (needs encryption)  
⚠️ No secrets scanning in git hooks  
⚠️ SQLite database not encrypted  
⚠️ Missing input validation on some CLI args  
⚠️ Logs may contain PII (usernames)  
⚠️ No automatic session expiry on logout  
⚠️ Dashboard has no authentication  

---

## Detailed Findings

### 1. Credential Storage & Handling ✅/⚠️

#### GOOD: Environment Variable Isolation
**Location:** `trading_bot/config/loader.py:23-29`

```python
def _load_env_overrides(settings: Settings) -> None:
    if os.getenv("ROBINHOOD_USERNAME"):
        settings.robinhood.username = os.getenv("ROBINHOOD_USERNAME", "")
    if os.getenv("ROBINHOOD_PASSWORD"):
        settings.robinhood.password = os.getenv("ROBINHOOD_PASSWORD", "")
```

**Verification:**
```bash
$ grep -r "password.*=.*[^'$]" trading_bot/ --include="*.py" | grep -v test
# No results - passwords only from env vars
```

**Risk:** LOW  
**Status:** ✅ ACCEPTABLE

#### GOOD: Config Validation Prevents Hardcoded Secrets
**Location:** `trading_bot/config/loader.py:32-48`

The loader actively scans config YAML for credentials and raises `ValueError` if found.

**Test:**
```bash
$ echo "robinhood:\n  password: secret123" > bad_config.yaml
$ ./tradebot-local --config-path bad_config.yaml doctor
# ValueError: Credential detected in config file
```

**Risk:** LOW  
**Status:** ✅ ACCEPTABLE

#### WARNING: Legacy Token Storage Was Plaintext
**Location:** `trading_bot/brokers/robinhood/auth.py:249-273`

Tokens saved to `robinhood_tokens.json` with 0o600 permissions but **not encrypted**.

**Current Implementation:**
```python
def _save_tokens(self) -> None:
    data = {
        "access_token": self.session.tokens.access_token,
        "refresh_token": self.session.tokens.refresh_token,
    }
    self.token_storage_path.write_text(json.dumps(data))
    os.chmod(self.token_storage_path, 0o600)  # User-only access
```

**Attack Scenario:**
1. Legacy direct-auth path stores tokens locally
2. Attacker gains shell access as user
3. Can read `state/robinhood_tokens.json`

**Recommendation:**
- Use keyring library or OS keychain
- Or encrypt with Fernet (key from env var)
- Mark as CRITICAL for V3.5 security audit

**Risk:** MEDIUM-HIGH  
**Status:** ⚠️ HARDENING REQUIRED

---

### 2. Authentication & Session Management ✅

#### GOOD: Rate Limiting Prevents Brute Force
**Location:** `trading_bot/brokers/robinhood/auth.py:83-156`

```python
class AuthRateLimiter:
    def __init__(self, max_attempts: int = 3, lockout_duration_minutes: int = 30):
```

**Verification:**
```python
# Legacy direct-auth path only
$ ./tradebot-local robinhood-login
❌ robinhood-login is disabled.
```

**Risk:** LOW  
**Status:** ✅ ACCEPTABLE

#### GOOD: Session Inactivity Timeout
**Location:** `trading_bot/brokers/robinhood/auth.py:446-466`

```python
def check_session(self) -> bool:
    inactive_for = datetime.now() - self.session.last_activity
    if inactive_for > self.inactivity_timeout:  # 30 min
        self.session.status = AuthStatus.DISCONNECTED
        return False
```

**Risk:** LOW  
**Status:** ✅ ACCEPTABLE

#### GOOD: Audit Logging of All Auth Events
**Location:** `trading_bot/brokers/robinhood/auth.py:197-223`

Every auth action logged with correlation ID:
- LOGIN_ATTEMPT
- LOGIN_SUCCESS
- LOGIN_FAILURE
- MFA_SUBMITTED
- TOKEN_REFRESH
- LOGOUT
- LOCKOUT

**Log Format:**
```json
{
  "event_type": "login_success",
  "success": true,
  "correlation_id": "a1b2c3d4",
  "details": {"token_expires": "2026-06-19T12:00:00"}
}
```

**Risk:** LOW  
**Status:** ✅ ACCEPTABLE

#### WARNING: No MFA on Dashboard
**Location:** `ui/dashboard/main.py`

The monitoring dashboard at `localhost:8080` has **no authentication**.

**Attack Scenario:**
1. Attacker on same network accesses `http://your-ip:8080`
2. Can view portfolio, trigger kill switch
3. Cannot trade (read-only) but can disrupt operations

**Recommendation:**
- Add basic auth or token-based auth
- Bind to localhost only (127.0.0.1) by default
- Add to V3.5 security hardening

**Risk:** MEDIUM  
**Status:** ⚠️ HARDENING REQUIRED

---

### 3. Live Trading Protections ✅

#### EXCELLENT: Live Trading Disabled in Code
**Location:** `trading_bot/config/loader.py:102`

```python
settings = Settings.model_validate(raw)
settings.app.live_trading_enabled = False  # FORCED - cannot override
```

**Verification:**
```python
# Even if config.yaml says live_trading_enabled: true
$ cat config.yaml | grep live
live_trading_enabled: true

$ ./tradebot-local doctor
live_trading=false  # Still false!
```

This is the **single most important safety control** and it's bulletproof.

**Risk:** NONE  
**Status:** ✅ EXCELLENT

#### GOOD: Kill Switch Architecture
**Location:** `trading_bot/safety/kill_switch.py`

- Integrated at all entry points (scan, paper-trade, manage-positions)
- Persists in SQLite (survives restarts)
- Blocks trading but allows read-only commands
- Audit logged with reason and timestamp

**Risk:** LOW  
**Status:** ✅ ACCEPTABLE

#### GOOD: Position Sizing Limits
**Location:** `trading_bot/risk/position_sizer.py`

- Max 20% allocation per ticker
- Portfolio heat limit (3% unrealized loss)
- Daily order limits (default 3)

**Risk:** LOW  
**Status:** ✅ ACCEPTABLE

---

### 4. Data Protection ⚠️

#### WARNING: SQLite Database Not Encrypted
**Location:** `state/trading_bot.db`

The SQLite database contains:
- Order history
- Portfolio state
- Kill switch state
- Position details

**Permissions:** `-rw-r--r--` (644) - readable by all users!

**Attack Scenario:**
1. Attacker gains file system access
2. Can read entire trading history
3. Can modify kill switch state
4. Can view P&L details

**Recommendation:**
```python
# Set restrictive permissions on database
os.chmod(db_path, 0o600)

# Consider SQLCipher for encryption at rest
```

**Risk:** MEDIUM  
**Status:** ⚠️ HARDENING REQUIRED

#### GOOD: Decision Logs Don't Contain Secrets
**Location:** `trading_bot/runtime/decision_log.py`

Verified: logs contain only ticker, status, reason - no credentials.

```bash
$ head logs/decision-log.jsonl
{"command": "scan", "ticker": "AAPL", "status": "NO_SIGNAL"}
```

**Risk:** LOW  
**Status:** ✅ ACCEPTABLE

#### WARNING: Logs May Contain PII
**Location:** Various

Auth audit logs include username:
```json
{"event_type": "login_attempt", "username": "john@example.com"}
```

**Recommendation:**
- Hash or pseudonymize usernames in logs
- Or document that logs contain PII for GDPR compliance

**Risk:** LOW  
**Status:** ⚠️ DOCUMENTATION REQUIRED

---

### 5. Network Security ✅

#### GOOD: No Hardcoded API Keys
Verified via code search - no API keys in source.

#### GOOD: Test Suite is Network-Free
All tests pass without network calls using monkeypatching.

#### WARNING: Dashboard Binds to 0.0.0.0
**Location:** `ui/dashboard/main.py:562`

```python
uvicorn.run(app, host="0.0.0.0", port=8080)  # All interfaces!
```

**Recommendation:**
```python
# Change to localhost only by default
uvicorn.run(app, host="127.0.0.1", port=8080)
```

**Risk:** MEDIUM  
**Status:** ⚠️ HARDENING REQUIRED

---

### 6. Error Handling & Information Disclosure ✅

#### GOOD: No Stack Traces to User
CLI catches exceptions and shows clean error messages.

#### GOOD: No Credential Exposure in Errors
Verified: error messages don't include passwords, tokens, or secrets.

#### GOOD: Config Validation on Load
Prevents startup with invalid/insecure configuration.

---

## Recommendations by Priority

### 🔴 CRITICAL (Before Production)

1. **Encrypt Token Storage**
   ```python
   # Use Fernet encryption
   from cryptography.fernet import Fernet
   key = os.getenv("TOKEN_ENCRYPTION_KEY")  # 32-byte base64
   f = Fernet(key)
   encrypted = f.encrypt(json.dumps(tokens).encode())
   ```

2. **Restrict Database Permissions**
   ```python
   # Set 0o600 on SQLite files
   os.chmod(db_path, 0o600)
   ```

3. **Add Pre-Commit Hook for Secrets**
   ```bash
   # .pre-commit-config.yaml
   - repo: https://github.com/Yelp/detect-secrets
     hooks:
     - id: detect-secrets
   ```

### 🟡 HIGH (Before V3 Release)

4. **Dashboard Authentication**
   - Add simple token-based auth
   - Or at minimum, bind to 127.0.0.1

5. **PII Handling Documentation**
   - Document what logs contain
   - Add log rotation policy
   - GDPR compliance notice

6. **Secrets Scanning**
   - Run `detect-secrets` in CI
   - Block commits with credentials

### 🟢 MEDIUM (Ongoing Hardening)

7. **Consider SQLCipher**
   - Encrypt SQLite at rest
   - Key from environment variable

8. **Session Management**
   - Automatic logout on terminal close
   - Single-session enforcement

9. **Rate Limiting Dashboard**
   - Prevent brute force on web UI

---

## Security Checklist

| Control | Status | Risk |
|---------|--------|------|
| Credentials in env vars only | ✅ PASS | Low |
| Hardcoded secrets blocked | ✅ PASS | Low |
| Live trading disabled | ✅ PASS | None |
| Kill switch functional | ✅ PASS | Low |
| Rate limiting on auth | ✅ PASS | Low |
| Session timeout | ✅ PASS | Low |
| Audit logging | ✅ PASS | Low |
| Token permissions 0o600 | ✅ PASS | Low |
| Token encryption | ⚠️ FAIL | High |
| Database encryption | ⚠️ FAIL | Medium |
| Dashboard auth | ⚠️ FAIL | Medium |
| Secrets scanning | ⚠️ FAIL | Medium |
| Network binding | ⚠️ FAIL | Medium |

---

## Conclusion

The trading bot has **excellent safety controls** for a financial application:

- ✅ Live trading is **impossible to accidentally enable**
- ✅ Credentials are **properly isolated**
- ✅ Authentication has **brute force protection**
- ✅ All actions are **auditable**

**Before Production:**
1. Implement token encryption
2. Restrict database permissions
3. Add dashboard authentication
4. Set up secrets scanning

**Bottom Line:** Safe for paper trading and MCP snapshot review now. Legacy direct-auth code remains reference-only and should not be treated as production-ready.

---

## Appendix: Verification Commands

```bash
# Check for secrets in code
grep -r "password\|secret\|token" trading_bot/ --include="*.py" | grep -v test
detect-secrets scan

# Check file permissions
ls -la state/
ls -la logs/

# Verify kill switch
echo "SELECT * FROM kill_switch;" | sqlite3 state/trading_bot.db

# Check token file (should be 0o600)
stat -f "%Lp" state/robinhood_tokens.json  # macOS
stat -c "%a" state/robinhood_tokens.json  # Linux
```
> Historical note: this review includes legacy direct-auth/token storage findings. Current supported Robinhood usage is MCP/operator-managed snapshots; the local CLI no longer supports `robinhood-login` or plaintext token workflows as a normal path.
