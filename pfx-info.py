#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["cryptography>=42"]
# ///
"""pfx-info.py — Print what is inside a PFX (PKCS#12) file.

Reports, for the file as a whole:

    * size and SHA-256 of the file
    * MAC algorithm and iteration count (the password integrity check)
    * how each part is encrypted: the certificates and the private key
      (e.g. PBES2 with AES-256-CBC, or legacy RC2-40), flagging legacy schemes
      that OpenSSL 3 rejects without its -legacy option

For the private key: its type and size, and whether it matches the leaf
certificate.

For each certificate, leaf first, then the chain in issuing order:

    * subject, issuer, serial number, friendly name
    * validity period, with days until expiry (or days since)
    * signature algorithm and public key type/size
    * SHA-1 thumbprint (what Windows shows) and SHA-256 fingerprint
    * extensions: CA flag, key usage, extended key usage, subject alternative
      names, key identifiers, CRL and OCSP locations, policies
    * which certificate in the file issued it, with the signature checked

No system `openssl` is needed. The `cryptography` package bundles its own
OpenSSL, and uv installs it from the inline dependency above.

Password: -p/--password, then $PFX_PASSWORD, then no password, then an empty
password, then a prompt if stdin is a terminal.

Examples:
    pfx-info cert.pfx
    pfx-info -p secret cert.p12
    pfx-info --format json cert.pfx

Exit status:
    0   success
    1   file could not be read, wrong password, or not a PFX file
    2   usage error (handled by argparse)
"""

import argparse
import getpass
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID

__version__ = "1.0.0"

# --- OIDs used in the PKCS#12 container itself -------------------------------

OID_NAMES = {
    # PKCS#7 content types
    "1.2.840.113549.1.7.1": "data",
    "1.2.840.113549.1.7.2": "signedData",
    "1.2.840.113549.1.7.6": "encryptedData",
    # PKCS#12 bag types
    "1.2.840.113549.1.12.10.1.1": "keyBag",
    "1.2.840.113549.1.12.10.1.2": "pkcs8ShroudedKeyBag",
    "1.2.840.113549.1.12.10.1.3": "certBag",
    "1.2.840.113549.1.12.10.1.4": "crlBag",
    "1.2.840.113549.1.12.10.1.5": "secretBag",
    "1.2.840.113549.1.12.10.1.6": "safeContentsBag",
    # PKCS#12 v1 password-based encryption
    "1.2.840.113549.1.12.1.1": "pbeWithSHA1And128BitRC4",
    "1.2.840.113549.1.12.1.2": "pbeWithSHA1And40BitRC4",
    "1.2.840.113549.1.12.1.3": "pbeWithSHA1And3-KeyTripleDES-CBC",
    "1.2.840.113549.1.12.1.4": "pbeWithSHA1And2-KeyTripleDES-CBC",
    "1.2.840.113549.1.12.1.5": "pbeWithSHA1And128BitRC2-CBC",
    "1.2.840.113549.1.12.1.6": "pbeWithSHA1And40BitRC2-CBC",
    # PKCS#5 v2
    "1.2.840.113549.1.5.12": "PBKDF2",
    "1.2.840.113549.1.5.13": "PBES2",
    "1.2.840.113549.1.5.14": "PBMAC1",
    # PRFs
    "1.2.840.113549.2.7": "HMAC-SHA1",
    "1.2.840.113549.2.8": "HMAC-SHA224",
    "1.2.840.113549.2.9": "HMAC-SHA256",
    "1.2.840.113549.2.10": "HMAC-SHA384",
    "1.2.840.113549.2.11": "HMAC-SHA512",
    # ciphers
    "2.16.840.1.101.3.4.1.2": "AES-128-CBC",
    "2.16.840.1.101.3.4.1.22": "AES-192-CBC",
    "2.16.840.1.101.3.4.1.42": "AES-256-CBC",
    "1.2.840.113549.3.7": "DES-EDE3-CBC",
    "1.3.14.3.2.7": "DES-CBC",
    "1.2.840.113549.3.2": "RC2-CBC",
    # digests (MAC)
    "1.2.840.113549.2.5": "MD5",
    "1.3.14.3.2.26": "SHA-1",
    "2.16.840.1.101.3.4.2.4": "SHA-224",
    "2.16.840.1.101.3.4.2.1": "SHA-256",
    "2.16.840.1.101.3.4.2.2": "SHA-384",
    "2.16.840.1.101.3.4.2.3": "SHA-512",
}

# Schemes that OpenSSL 3 only loads with the legacy provider (-legacy).
LEGACY_OIDS = {
    "1.2.840.113549.1.12.1.1",
    "1.2.840.113549.1.12.1.2",
    "1.2.840.113549.1.12.1.4",
    "1.2.840.113549.1.12.1.5",
    "1.2.840.113549.1.12.1.6",
    "1.3.14.3.2.7",
    "1.2.840.113549.3.2",
}

BAG_KEY = {"1.2.840.113549.1.12.10.1.1", "1.2.840.113549.1.12.10.1.2"}
BAG_CERT = "1.2.840.113549.1.12.10.1.3"

# Common EKU and policy OIDs that cryptography has no name for.
EXTRA_OID_NAMES = {
    "1.3.6.1.4.1.311.10.3.12": "documentSigning (Microsoft)",
    "1.3.6.1.4.1.311.20.2.2": "smartcardLogon",
    "1.3.6.1.4.1.311.10.3.4": "encryptedFileSystem",
    "1.3.6.1.4.1.311.2.1.21": "individualCodeSigning",
    "1.3.6.1.4.1.311.2.1.22": "commercialCodeSigning",
    "2.23.140.1.2.1": "domain-validated",
    "2.23.140.1.2.2": "organization-validated",
    "2.23.140.1.2.3": "individual-validated",
    "2.23.140.1.1": "extended-validation",
    "2.23.140.1.4.1": "code-signing",
    "2.5.29.32.0": "anyPolicy",
}


def oid_name(oid):
    """Readable name for a cryptography ObjectIdentifier."""
    dotted = oid.dotted_string
    if dotted in EXTRA_OID_NAMES:
        return EXTRA_OID_NAMES[dotted]
    name = getattr(oid, "_name", None)
    if name and name != "Unknown OID":
        return name
    return dotted


# --- Minimal DER/BER reader --------------------------------------------------
#
# cryptography decrypts the file but does not report how it was protected, so
# this walks the outer PKCS#12 structure to read the MAC and encryption
# algorithms. It accepts BER indefinite lengths and constructed OCTET STRINGs,
# which some Java and Windows exporters produce.


class Node:
    def __init__(self, tag, content, children):
        self.tag = tag
        self.content = content
        self.children = children

    def octets(self):
        if self.children is None:
            return self.content
        return b"".join(c.octets() for c in self.children)

    def integer(self):
        return int.from_bytes(self.content, "big", signed=True)

    def oid(self):
        data = self.content
        first = data[0]
        parts = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
        value = 0
        for b in data[1:]:
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                parts.append(value)
                value = 0
        return ".".join(map(str, parts))


def parse_one(buf, pos):
    if pos + 2 > len(buf):
        raise ValueError("truncated ASN.1")
    tag = buf[pos]
    if tag & 0x1F == 0x1F:
        raise ValueError("high-tag-number ASN.1 not supported")
    length = buf[pos + 1]
    pos += 2
    constructed = bool(tag & 0x20)
    if length == 0x80:
        if not constructed:
            raise ValueError("indefinite length on primitive ASN.1")
        children = []
        while buf[pos : pos + 2] != b"\x00\x00":
            child, pos = parse_one(buf, pos)
            children.append(child)
        return Node(tag, None, children), pos + 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[pos : pos + n], "big")
        pos += n
    end = pos + length
    if end > len(buf):
        raise ValueError("truncated ASN.1")
    content = buf[pos:end]
    children = parse_all(content) if constructed else None
    return Node(tag, content, children), end


def parse_all(buf):
    nodes, pos = [], 0
    while pos < len(buf):
        node, pos = parse_one(buf, pos)
        nodes.append(node)
    return nodes


def parse(buf):
    return parse_one(buf, 0)[0]


def describe_algorithm(alg):
    """Turn an AlgorithmIdentifier node into a dict."""
    oid = alg.children[0].oid()
    params = alg.children[1] if len(alg.children) > 1 else None
    info = {"oid": oid, "name": OID_NAMES.get(oid, oid)}
    legacy = oid in LEGACY_OIDS

    if oid.startswith("1.2.840.113549.1.12.1.") and params is not None:
        info["iterations"] = params.children[1].integer()
    elif oid == "1.2.840.113549.1.5.13" and params is not None:  # PBES2
        kdf, scheme = params.children[0], params.children[1]
        kdf_oid = kdf.children[0].oid()
        info["kdf"] = OID_NAMES.get(kdf_oid, kdf_oid)
        if kdf_oid == "1.2.840.113549.1.5.12":  # PBKDF2
            kp = kdf.children[1].children
            info["iterations"] = kp[1].integer()
            prf = "1.2.840.113549.2.7"  # default HMAC-SHA1
            for extra in kp[2:]:
                if extra.tag == 0x30:
                    prf = extra.children[0].oid()
            info["prf"] = OID_NAMES.get(prf, prf)
        cipher_oid = scheme.children[0].oid()
        info["cipher"] = OID_NAMES.get(cipher_oid, cipher_oid)
        legacy = legacy or cipher_oid in LEGACY_OIDS
    info["legacy"] = legacy
    return info


def summarize_algorithm(info):
    if info is None:
        return "none (not encrypted)"
    if info["name"] == "PBES2":
        text = f"PBES2 ({info.get('kdf', '?')}"
        if "prf" in info:
            text += f"-{info['prf']}"
        text += f", {info.get('cipher', '?')})"
    else:
        text = info["name"]
    if "iterations" in info:
        text += f", {info['iterations']:,} iterations"
    if info["legacy"]:
        text += "  [legacy: OpenSSL 3 needs -legacy]"
    return text


def bag_kinds(safe_contents):
    """Return [(bag_type_oid, key_encryption_info_or_None)] for SafeContents."""
    out = []
    for bag in safe_contents.children:
        bag_oid = bag.children[0].oid()
        value = bag.children[1].children[0]  # [0] EXPLICIT
        if bag_oid == "1.2.840.113549.1.12.10.1.2":
            out.append((bag_oid, describe_algorithm(value.children[0])))
        elif bag_oid == "1.2.840.113549.1.12.10.1.6":
            out.extend(bag_kinds(value))
        else:
            out.append((bag_oid, None))
    return out


def container_info(data):
    """Read the MAC and encryption scheme of each part of the PFX."""
    pfx = parse(data)
    version = pfx.children[0].integer()
    auth_safe = pfx.children[1]
    content_type = auth_safe.children[0].oid()
    info = {"version": version, "mac": None, "parts": []}

    if len(pfx.children) > 2:
        mac = pfx.children[2]
        digest_alg = mac.children[0].children[0]
        mac_oid = digest_alg.children[0].oid()
        iterations = mac.children[2].integer() if len(mac.children) > 2 else 1
        info["mac"] = {
            "algorithm": OID_NAMES.get(mac_oid, mac_oid),
            "iterations": iterations,
        }

    if content_type != "1.2.840.113549.1.7.1":
        info["authSafe"] = OID_NAMES.get(content_type, content_type)
        return info

    safes = parse(auth_safe.children[1].children[0].octets())
    for ci in safes.children:
        ci_type = ci.children[0].oid()
        if ci_type == "1.2.840.113549.1.7.1":
            inner = parse(ci.children[1].children[0].octets())
            for bag_oid, key_alg in bag_kinds(inner):
                info["parts"].append(
                    {"holds": OID_NAMES.get(bag_oid, bag_oid), "encryption": key_alg}
                )
        elif ci_type == "1.2.840.113549.1.7.6":
            enc_data = ci.children[1].children[0]
            eci = enc_data.children[1]
            info["parts"].append(
                {
                    "holds": "encrypted bags (usually certificates)",
                    "encryption": describe_algorithm(eci.children[1]),
                }
            )
        else:
            info["parts"].append(
                {"holds": OID_NAMES.get(ci_type, ci_type), "encryption": None}
            )
    return info


# --- Loading -----------------------------------------------------------------


def load(data, password):
    pw = password.encode() if password is not None else None
    return pkcs12.load_pkcs12(data, pw)


def load_with_fallbacks(data, password):
    """Return (bundle, password_used) or raise ValueError."""
    if password is not None:
        return load(data, password), password
    for candidate in (None, ""):
        try:
            return load(data, candidate), candidate
        except ValueError:
            pass
    if not sys.stdin.isatty():
        raise ValueError("wrong password or invalid PFX file")
    entered = getpass.getpass("PFX password: ")
    return load(data, entered), entered


# --- Describing keys and certificates ----------------------------------------


def describe_public_key(key):
    if isinstance(key, (rsa.RSAPublicKey, rsa.RSAPrivateKey)):
        return f"RSA {key.key_size}-bit"
    if isinstance(key, (ec.EllipticCurvePublicKey, ec.EllipticCurvePrivateKey)):
        return f"EC {key.curve.name} ({key.curve.key_size}-bit)"
    if isinstance(key, (ed25519.Ed25519PublicKey, ed25519.Ed25519PrivateKey)):
        return "Ed25519"
    if isinstance(key, (ed448.Ed448PublicKey, ed448.Ed448PrivateKey)):
        return "Ed448"
    if isinstance(key, (dsa.DSAPublicKey, dsa.DSAPrivateKey)):
        return f"DSA {key.key_size}-bit"
    return type(key).__name__


def spki(public_key):
    return public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def general_names(names):
    out = []
    for n in names:
        if isinstance(n, x509.DNSName):
            out.append(f"DNS:{n.value}")
        elif isinstance(n, x509.IPAddress):
            out.append(f"IP:{n.value}")
        elif isinstance(n, x509.RFC822Name):
            out.append(f"email:{n.value}")
        elif isinstance(n, x509.UniformResourceIdentifier):
            out.append(f"URI:{n.value}")
        elif isinstance(n, x509.DirectoryName):
            out.append(f"DirName:{n.value.rfc4514_string()}")
        elif isinstance(n, x509.OtherName):
            out.append(f"otherName:{oid_name(n.type_id)}")
        else:
            out.append(str(n.value))
    return out


KEY_USAGE_FIELDS = (
    "digital_signature",
    "content_commitment",
    "key_encipherment",
    "data_encipherment",
    "key_agreement",
    "key_cert_sign",
    "crl_sign",
)


def describe_extensions(cert):
    ext = {}
    other = []
    for e in cert.extensions:
        v = e.value
        oid = e.oid
        if oid == ExtensionOID.BASIC_CONSTRAINTS:
            ext["ca"] = v.ca
            if v.path_length is not None:
                ext["pathLength"] = v.path_length
        elif oid == ExtensionOID.KEY_USAGE:
            usages = [f for f in KEY_USAGE_FIELDS if getattr(v, f)]
            if v.key_agreement:
                if v.encipher_only:
                    usages.append("encipher_only")
                if v.decipher_only:
                    usages.append("decipher_only")
            ext["keyUsage"] = usages
        elif oid == ExtensionOID.EXTENDED_KEY_USAGE:
            ext["extendedKeyUsage"] = [oid_name(u) for u in v]
        elif oid == ExtensionOID.SUBJECT_ALTERNATIVE_NAME:
            ext["subjectAltNames"] = general_names(v)
        elif oid == ExtensionOID.SUBJECT_KEY_IDENTIFIER:
            ext["subjectKeyId"] = v.digest.hex().upper()
        elif oid == ExtensionOID.AUTHORITY_KEY_IDENTIFIER:
            if v.key_identifier:
                ext["authorityKeyId"] = v.key_identifier.hex().upper()
        elif oid == ExtensionOID.CRL_DISTRIBUTION_POINTS:
            urls = []
            for dp in v:
                urls.extend(general_names(dp.full_name or []))
            ext["crlDistributionPoints"] = urls
        elif oid == ExtensionOID.AUTHORITY_INFORMATION_ACCESS:
            ext["authorityInfoAccess"] = [
                f"{oid_name(ad.access_method)}: {general_names([ad.access_location])[0]}"
                for ad in v
            ]
        elif oid == ExtensionOID.CERTIFICATE_POLICIES:
            ext["policies"] = [oid_name(p.policy_identifier) for p in v]
        else:
            other.append(oid_name(oid) + (" (critical)" if e.critical else ""))
    if other:
        ext["other"] = other
    return ext


def describe_cert(cert, friendly_name, now):
    not_after = cert.not_valid_after_utc
    not_before = cert.not_valid_before_utc
    days = (not_after - now).days
    if now < not_before:
        status = f"not yet valid (starts in {(not_before - now).days} days)"
    elif days >= 0:
        status = f"expires in {days} days"
    else:
        status = f"EXPIRED {-days} days ago"
    try:
        sig = oid_name(cert.signature_algorithm_oid)
    except Exception:
        sig = "unknown"
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "friendlyName": friendly_name.decode("utf-8", "replace")
        if friendly_name
        else None,
        "serial": format(cert.serial_number, "X"),
        "version": cert.version.name,
        "notBefore": not_before.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "notAfter": not_after.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "daysRemaining": days,
        "status": status,
        "selfSigned": cert.subject == cert.issuer,
        "signatureAlgorithm": sig,
        "publicKey": describe_public_key(cert.public_key()),
        "sha1": cert.fingerprint(hashes.SHA1()).hex().upper(),
        "sha256": cert.fingerprint(hashes.SHA256()).hex().upper(),
        "extensions": describe_extensions(cert),
    }


def issued_by(child, parent):
    if child.issuer != parent.subject:
        return False
    try:
        child.verify_directly_issued_by(parent)
        return True
    except Exception:
        return False


def order_chain(leaf, others):
    """Return certs leaf first, then each issuer in turn, then leftovers."""
    ordered = [leaf] if leaf is not None else []
    remaining = list(others)
    current = leaf
    while current is not None and remaining:
        nxt = next(
            (c for c in remaining if c[0] is not current[0] and issued_by(current[0], c[0])),
            None,
        )
        if nxt is None:
            break
        ordered.append(nxt)
        remaining.remove(nxt)
        current = nxt
    return ordered + remaining


def build_report(path, data, bundle):
    now = datetime.now(timezone.utc)
    report = {
        "file": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest().upper(),
    }

    try:
        report["container"] = container_info(data)
    except Exception as exc:  # the certs still loaded, so keep going
        report["container"] = {"error": f"could not read structure: {exc}"}

    leaf = (bundle.cert.certificate, bundle.cert.friendly_name) if bundle.cert else None
    others = [(c.certificate, c.friendly_name) for c in bundle.additional_certs]

    key = bundle.key
    if key is not None:
        key_info = {"type": describe_public_key(key), "matchesCertificate": None}
        if leaf is not None:
            key_info["matchesCertificate"] = spki(key.public_key()) == spki(
                leaf[0].public_key()
            )
        report["privateKey"] = key_info
    else:
        report["privateKey"] = None

    ordered = order_chain(leaf, others)
    certs = []
    for i, (cert, fname) in enumerate(ordered):
        entry = describe_cert(cert, fname, now)
        entry["role"] = (
            "certificate (matches key)" if leaf is not None and i == 0 else "chain"
        )
        entry["issuedBy"] = None
        for j, (other, _) in enumerate(ordered):
            if j != i and issued_by(cert, other):
                entry["issuedBy"] = j + 1
                break
        if entry["issuedBy"] is None and entry["selfSigned"]:
            try:
                cert.verify_directly_issued_by(cert)
                entry["issuedBy"] = "self"
            except Exception:
                pass
        certs.append(entry)
    report["certificates"] = certs
    return report


# --- Output ------------------------------------------------------------------


def line(label, value, indent=2):
    return f"{' ' * indent}{label + ':':<15} {value}"


def render_human(r):
    out = []
    out.append(r["file"])
    out.append(line("Size", f"{r['size']:,} bytes"))
    out.append(line("SHA-256", r["sha256"]))

    c = r["container"]
    out.append("")
    out.append("Protection")
    if "error" in c:
        out.append(f"  {c['error']}")
    else:
        mac = c["mac"]
        out.append(
            line(
                "MAC",
                f"{mac['algorithm']}, {mac['iterations']:,} iterations" if mac else "none",
            )
        )
        if "authSafe" in c:
            out.append(line("Content", f"{c['authSafe']} (not password-based)"))
        for part in c["parts"]:
            holds = part["holds"]
            label = {
                "pkcs8ShroudedKeyBag": "Private key",
                "keyBag": "Private key",
                "certBag": "Certificate",
                "encrypted bags (usually certificates)": "Certificates",
            }.get(holds, holds)
            out.append(line(label, summarize_algorithm(part["encryption"])))

    out.append("")
    out.append("Private key")
    k = r["privateKey"]
    if k is None:
        out.append("  none")
    else:
        out.append(line("Type", k["type"]))
        if k["matchesCertificate"] is not None:
            out.append(
                line("Matches cert", "yes" if k["matchesCertificate"] else "NO")
            )

    for i, cert in enumerate(r["certificates"], 1):
        out.append("")
        out.append(f"[{i}] {cert['role'].capitalize()}")
        out.append(line("Subject", cert["subject"]))
        out.append(line("Issuer", cert["issuer"]))
        issued = cert["issuedBy"]
        if issued == "self":
            out.append(line("Issued by", "itself (self-signed root)"))
        elif issued is not None:
            out.append(line("Issued by", f"[{issued}] (signature valid)"))
        else:
            out.append(line("Issued by", "not in this file"))
        if cert["friendlyName"]:
            out.append(line("Friendly name", cert["friendlyName"]))
        out.append(line("Serial", cert["serial"]))
        out.append(line("Not before", cert["notBefore"]))
        out.append(line("Not after", f"{cert['notAfter']} ({cert['status']})"))
        out.append(line("Signature", cert["signatureAlgorithm"]))
        out.append(line("Public key", cert["publicKey"]))
        out.append(line("SHA-1", cert["sha1"]))
        out.append(line("SHA-256", cert["sha256"]))

        e = cert["extensions"]
        if "ca" in e:
            ca = "yes" if e["ca"] else "no"
            if "pathLength" in e:
                ca += f" (path length {e['pathLength']})"
            out.append(line("CA", ca))
        for key, label in (
            ("keyUsage", "Key usage"),
            ("extendedKeyUsage", "Ext key usage"),
            ("subjectAltNames", "Alt names"),
            ("crlDistributionPoints", "CRL"),
            ("authorityInfoAccess", "AIA"),
            ("policies", "Policies"),
            ("other", "Other ext"),
        ):
            values = e.get(key)
            if not values:
                continue
            out.append(line(label, values[0]))
            for v in values[1:]:
                out.append(f"{' ' * 18}{v}")
        if "subjectKeyId" in e:
            out.append(line("Subject key ID", e["subjectKeyId"]))
        if "authorityKeyId" in e:
            out.append(line("Auth key ID", e["authorityKeyId"]))
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(
        description="Print what is inside a PFX (PKCS#12) file.",
        epilog="Run with no options for readable output; --format json for scripting.",
    )
    parser.add_argument("pfx", help="path to the .pfx or .p12 file")
    parser.add_argument(
        "-p",
        "--password",
        help="PFX password (default: $PFX_PASSWORD, then none, then empty, then prompt)",
    )
    parser.add_argument(
        "--format", choices=("human", "json"), default="human", help="output format"
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    try:
        with open(args.pfx, "rb") as f:
            data = f.read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    password = args.password
    if password is None:
        password = os.environ.get("PFX_PASSWORD")
    try:
        bundle, _ = load_with_fallbacks(data, password)
    except ValueError:
        print("error: wrong password or invalid PFX file", file=sys.stderr)
        return 1

    report = build_report(args.pfx, data, bundle)
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
