"""Nostr key normalization and BIP-340 registration proof verification."""

import hashlib
import re

import secp256k1
from nostr import bech32
from nostr.key import PublicKey


def normalize_pubkey(value: str) -> str:
    """Accept hex or checksummed NIP-19 npub keys; return lowercase x-only hex."""
    if not isinstance(value, str):
        raise ValueError("Invalid Nostr public key: expected hex or npub.")
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raw = bytes.fromhex(value)
    else:
        hrp, data, encoding = bech32.bech32_decode(value)
        if hrp != "npub" or encoding != bech32.Encoding.BECH32:
            raise ValueError("Invalid Nostr public key: expected 64 hex characters or a valid npub.")
        decoded = bech32.convertbits(data, 5, 8, pad=False)
        if decoded is None or len(decoded) != 32:
            raise ValueError("Invalid npub length or padding.")
        raw = bytes(decoded)
    try:
        secp256k1.PublicKey(b"\x02" + raw, raw=True)
    except Exception as e:
        raise ValueError("Invalid Nostr public key: not a secp256k1 curve point.") from e
    return raw.hex()


def verify_signature(pubkey: str, challenge: str, signature: str) -> bool:
    """Verify a raw Schnorr signature over SHA-256(exact UTF-8 challenge text)."""
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{128}", signature):
        return False
    try:
        digest = hashlib.sha256(challenge.encode("utf-8")).hexdigest()
        return PublicKey(bytes.fromhex(pubkey)).verify_signed_message_hash(digest, signature)
    except Exception:
        return False
