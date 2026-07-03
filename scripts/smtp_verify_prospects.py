#!/usr/bin/env python3
"""SMTP RCPT-TO mailbox verifier (free, no API).

For each prospect domain in _b2b_prospect_pool.json, guess a small set of
likely local-parts, look up MX, and probe RCPT TO without sending any body.

Result written to data/_email_verification.json with per-domain best guess:
  { "domain": { "valid_email": "...", "status": "valid|invalid|unknown|risky|catch_all", "checked_at": "..." } }

Bounced domains (from _bounces.json) are marked invalid without probing.

Safe by design:
- No email is ever sent (only RCPT TO + RSET + QUIT).
- Uses HELO from a neutral hostname; MAIL FROM uses postmaster@<our_domain>.
- 8s connect timeout, 10s command timeout, 1 domain at a time to avoid tarpits.
- Skips domains that block or defer (marked "unknown"); does NOT add unknowns to queue.
"""
from __future__ import annotations
import json
import socket
import smtplib
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import dns.resolver  # dnspython
    HAVE_DNS = True
except Exception:
    HAVE_DNS = False

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

HELO_HOST = "mail.clovedds.com"
MAIL_FROM = "postmaster@clovedds.com"
CONNECT_TIMEOUT = 8
CMD_TIMEOUT = 10

# Likely local parts, in order of preference for a business intro
LOCAL_PARTS = [
    "info", "hello", "contact", "admin", "office",
    "hr", "careers", "sales", "marketing",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_mx(domain: str) -> Optional[str]:
    if not HAVE_DNS:
        return None
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        hosts = sorted([(r.preference, str(r.exchange).rstrip(".")) for r in answers])
        return hosts[0][1] if hosts else None
    except Exception:
        return None


def probe_rcpt(mx: str, email: str) -> str:
    """Return 'valid' | 'invalid' | 'unknown' | 'catch_all'.

    We do a single RCPT TO then RSET+QUIT. If the server accepts a
    known-bogus address too, we flag catch_all so the result is not trusted.
    """
    try:
        with smtplib.SMTP(mx, 25, timeout=CONNECT_TIMEOUT) as s:
            s.timeout = CMD_TIMEOUT
            s.ehlo(HELO_HOST)
            # Attempt STARTTLS if offered (some servers require it before RCPT)
            try:
                if s.has_extn("starttls"):
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    s.starttls(context=ctx)
                    s.ehlo(HELO_HOST)
            except Exception:
                pass
            s.mail(MAIL_FROM)
            code, _ = s.rcpt(email)
            # Catch-all check with a random local part
            bogus = "zzz-nomatch-9x7q@" + email.split("@", 1)[1]
            try:
                c2, _ = s.rcpt(bogus)
            except smtplib.SMTPException:
                c2 = 550
            s.rset()
            s.quit()
            if 200 <= code < 300:
                if 200 <= c2 < 300:
                    return "catch_all"
                return "valid"
            if code in (550, 551, 553, 554):
                return "invalid"
            return "unknown"
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
            socket.timeout, socket.gaierror, ConnectionRefusedError, OSError):
        return "unknown"
    except smtplib.SMTPResponseException as e:
        if 500 <= e.smtp_code < 600:
            return "invalid"
        return "unknown"
    except Exception:
        return "unknown"


def verify_domain(domain: str) -> dict:
    mx = get_mx(domain)
    if not mx:
        return {"domain": domain, "status": "no_mx", "valid_email": None,
                "checked_at": utcnow()}
    for lp in LOCAL_PARTS:
        em = lp + "@" + domain
        status = probe_rcpt(mx, em)
        if status == "valid":
            return {"domain": domain, "status": "valid", "valid_email": em,
                    "mx": mx, "checked_at": utcnow()}
        if status == "catch_all":
            # Server accepts everything — can't trust; still keep as risky
            return {"domain": domain, "status": "catch_all",
                    "valid_email": lp + "@" + domain, "mx": mx,
                    "checked_at": utcnow()}
        # invalid or unknown -> try next local part
    return {"domain": domain, "status": "no_match", "valid_email": None,
            "mx": mx, "checked_at": utcnow()}


def main() -> None:
    pool_path = DATA / "_b2b_prospect_pool.json"
    bounces_path = DATA / "_bounces.json"
    out_path = DATA / "_email_verification.json"

    if not pool_path.exists():
        print("No prospect pool file; skipping verification.")
        return

    pool = json.loads(pool_path.read_text())
    prospects = pool.get("prospects") or []

    # Load bounced domains
    bounced_domains: set = set()
    if bounces_path.exists():
        bj = json.loads(bounces_path.read_text())
        for em in (bj.get("external_prospect_bounces") or {}):
            if "@" in em:
                bounced_domains.add(em.split("@", 1)[1].lower())

    # Preserve previous results to avoid re-probing every day
    previous: dict = {}
    if out_path.exists():
        try:
            previous = json.loads(out_path.read_text()).get("results", {}) or {}
        except Exception:
            previous = {}

    # Rank prospects by score, take top 40 for verification budget
    ranked = sorted(
        [p for p in prospects if p.get("has_website")],
        key=lambda x: (x.get("score") or 0),
        reverse=True,
    )
    # Cap per run: full verifier can be slow; run again tomorrow to widen coverage.
    RUN_BUDGET = int((__import__("os").environ.get("VERIFY_BUDGET") or "20"))
    to_check = ranked[:RUN_BUDGET]

    results: dict = dict(previous)
    checked = 0
    for p in to_check:
        d = (p.get("domain") or "").lower().strip()
        if not d:
            continue
        if d in bounced_domains:
            results[d] = {"domain": d, "status": "bounced_history",
                          "valid_email": None, "checked_at": utcnow()}
            continue
        if d in results and results[d].get("status") in ("valid", "invalid", "no_mx"):
            continue  # already known
        r = verify_domain(d)
        results[d] = r
        checked += 1
        print(f"  {d} -> {r.get('status')} ({r.get('valid_email') or '-'})")

    counts = {"valid": 0, "catch_all": 0, "invalid": 0, "no_mx": 0,
              "no_match": 0, "unknown": 0, "bounced_history": 0}
    for r in results.values():
        s = r.get("status") or "unknown"
        counts[s] = counts.get(s, 0) + 1

    out = {
        "generated_at": utcnow(),
        "method": "SMTP RCPT-TO probe (free, no send)",
        "helo": HELO_HOST,
        "mail_from": MAIL_FROM,
        "verified_domain_count": len(results),
        "verified_this_run": checked,
        "counts": counts,
        "results": results,
    }
    out_path.write_text(json.dumps(out, indent=2))
    print(f"Wrote {out_path} — checked {checked}, total known {len(results)}")


if __name__ == "__main__":
    main()
