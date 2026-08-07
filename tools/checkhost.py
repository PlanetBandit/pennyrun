#!/usr/bin/env python3
"""Can this machine run the sweep? Ask, and report honestly.

Three outcomes, deliberately distinct:
  0 GOOD     Home Depot quotes prices here.
  1 BLOCKED  They answered and refused. Datacentre ranges get this.
  2 UNKNOWN  We never reached them. Our problem, not theirs.

Exit 2 used to be reported as BLOCKED, which sent one investigation down
the wrong road entirely.
"""
import os
import sys

# `python3 tools/checkhost.py` puts tools/ on sys.path, not the repo root,
# so `from tools import hdclient` can't resolve on its own -- see sweep.py
# for the same fix and the reasoning.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools import hdclient


def verdict(results):
    pricing = results.get("pricing API", "")

    if pricing == "ok":
        return 0, "GOOD -- Home Depot quotes prices from this machine."

    if pricing.startswith("refused"):
        return 1, ("BLOCKED -- they answered and refused this address.\n"
                   "     datacentre ranges (GitHub Actions, DigitalOcean) get 206 no\n"
                   "     matter what the client looks like -- measured six impersonation\n"
                   "     profiles, all refused. Run the sweep from a residential\n"
                   "     connection instead.\n"
                   f"     detail: {pricing}")

    return 2, ("UNKNOWN -- could not reach Home Depot or received an unexpected state.\n"
               "     This is likely a local network or TLS problem, not a refusal.\n"
               "     On macOS this is usually Python missing root certificates:\n"
               "     run /Applications/Python*/Install\\ Certificates.command\n"
               "     If you see this on a different platform, check network connectivity.\n"
               f"     detail: {pricing}")


def main():
    results = hdclient.probe()
    if results:
        width = max(len(k) for k in results)
        for host, state in results.items():
            print(f"  {host:<{width}}  {state}")
    print()
    code, msg = verdict(results)
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
