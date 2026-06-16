#!/usr/bin/env python3
"""One-off: discover descriptive_name for each of the 18 Google Ads
accounts and save data/google_ads_account_map.json (private, gitignored).
Stays within the manager-account privacy policy: we save the map LOCALLY
but the snapshot.json public mirror never shows customer_ids.
"""
import json, pathlib, time
ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_google_ads_account_map.json"  # underscore prefix = private to validator

ACCOUNTS = ["4276567700","2481492821","3737640297","6442679282","7621293648","8287478168","2816298093","3575932013","4787133203","8668802505","7341541088","1181427688","9588043178","7980265317","8712971350","9663587549","4867393335","4195539325"]
# we already know
KNOWN = {"4276567700": "Clove Dental Camarillo"}

# Will be filled by calling list-customer-clients on each account
result = {"accounts": []}
for acct in ACCOUNTS:
    result["accounts"].append({"customer_id": acct, "descriptive_name": KNOWN.get(acct, "")})
OUT.write_text(json.dumps(result, indent=2))
print("seeded", OUT, "— now fill descriptive_name from list-customer-clients calls")
