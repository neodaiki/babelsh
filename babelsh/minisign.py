"""Verify minisign signatures (https://jedisct1.github.io/minisign/). Stdlib only.

Formats handled:
  public key  : [untrusted comment line]  base64( "Ed" | keyid[8] | ed25519_pk[32] )
  signature   : untrusted comment / base64( alg[2] | keyid[8] | sig[64] ) /
                "trusted comment: ..." / base64( global_sig[64] )
  alg "ED" = signature over BLAKE2b-512(message) (minisign default), "Ed" = over the message.
  global_sig = Ed25519 over ( sig[64] | trusted_comment_bytes ): binds the trusted comment.
"""
import base64
import binascii
import hashlib

from . import ed25519


class SignatureError(Exception):
    pass


def _b64(line, what):
    try:
        return base64.b64decode(line.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("%s is not valid base64" % what)


def parse_public_key(text):
    """Accept a whole minisign .pub file or just its base64 line. Returns (keyid, pubkey)."""
    lines = [l for l in text.strip().splitlines() if l.strip() and not l.startswith("untrusted comment:")]
    if len(lines) != 1:
        raise SignatureError("public key: expected exactly one base64 line")
    raw = _b64(lines[0], "public key")
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise SignatureError("public key: not a minisign Ed25519 key")
    return raw[2:10], raw[10:]


def parse_signature(text):
    lines = text.strip("\n").split("\n")
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") \
            or not lines[2].startswith("trusted comment: "):
        raise SignatureError("malformed signature file")
    raw = _b64(lines[1], "signature")
    if len(raw) != 74 or raw[:2] not in (b"Ed", b"ED"):
        raise SignatureError("unsupported signature algorithm")
    global_sig = _b64(lines[3], "global signature")
    if len(global_sig) != 64:
        raise SignatureError("bad global signature length")
    comment = lines[2][len("trusted comment: "):]
    return {"alg": raw[:2], "keyid": raw[2:10], "sig": raw[10:], "comment": comment,
            "global": global_sig}


def verify(message, signature_text, public_keys):
    """Verify `message` (bytes) against a .minisig text using any of `public_keys`
    (list of base64 lines / .pub texts). Returns the trusted comment (str)."""
    sig = parse_signature(signature_text)
    keys = [parse_public_key(k) for k in public_keys]
    candidates = [pk for keyid, pk in keys if keyid == sig["keyid"]]
    if not candidates:
        raise SignatureError("signature was made by an unknown key (id %s)" % sig["keyid"].hex())
    signed = hashlib.blake2b(message, digest_size=64).digest() if sig["alg"] == b"ED" else message
    for pk in candidates:
        if ed25519.verify(pk, signed, sig["sig"]) and \
                ed25519.verify(pk, sig["sig"] + sig["comment"].encode("utf-8"), sig["global"]):
            return sig["comment"]
    raise SignatureError("signature does not match the content")
