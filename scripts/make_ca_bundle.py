"""Build a CA bundle = certifi roots + the Windows ROOT/CA certificate stores.

Why: some hosts run TLS-inspecting security software that
re-signs HTTPS certificates with its own root, which Windows trusts but Python's certifi does not.
Tools that use certifi (PlatformIO) then fail. This bundle makes them trust exactly what the OS
trusts. Certificate verification is never disabled. Output is machine-specific and not tracked.
"""

import ssl
import sys
from pathlib import Path

import certifi


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".venv") / "oyster-ca-bundle.pem"
    pems = [Path(certifi.where()).read_text(encoding="ascii")]
    added = 0
    if sys.platform == "win32":
        seen = set()
        for store in ("ROOT", "CA"):
            for der, encoding, trust in ssl.enum_certificates(store):
                if encoding != "x509_asn" or der in seen:
                    continue
                if trust is not True and "1.3.6.1.5.5.7.3.1" not in trust:  # server-auth usage only
                    continue
                seen.add(der)
                pems.append(ssl.DER_cert_to_PEM_cert(der))
                added += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(pems), encoding="ascii")
    print(f"wrote {out} (certifi + {added} OS certificates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
