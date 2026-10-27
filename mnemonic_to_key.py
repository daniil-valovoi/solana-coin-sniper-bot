"""Derive a Solana private key (base58, Phantom-style 64-byte format) from a 24-word BIP39 passphrase.

Uses bip_utils for BIP39 seed + SLIP-0010 ed25519 derivation and solders for the keypair.
Default derivation path: m/44'/501'/0'/0' (Phantom/Solflare standard).
"""
import sys
import base58
from bip_utils import (
    Bip39SeedGenerator,
    Bip39MnemonicValidator,
    Bip39Languages,
    Bip32Slip10Ed25519,
)
from solders.keypair import Keypair

SOLANA_COIN_TYPE = 501


def derive_keypair(mnemonic: str, passphrase: str = "", account: int = 0) -> Keypair:
    Bip39MnemonicValidator(Bip39Languages.ENGLISH).Validate(mnemonic)  # raises on invalid mnemonic
    seed = Bip39SeedGenerator(mnemonic).Generate(passphrase)
    # SLIP-0010 ed25519: master key, then harden-only path
    bip32 = Bip32Slip10Ed25519.FromSeed(seed)
    path = f"m/44'/{SOLANA_COIN_TYPE}'/{account}'/0'"
    bip32 = bip32.DerivePath(path)
    return Keypair.from_bytes(bip32.PrivateKey().Raw().ToBytes() + bip32.PublicKey().RawCompressed().ToBytes()[1:])


def main():
    if len(sys.argv) > 1:
        mnemonic = " ".join(sys.argv[1:])
    else:
        mnemonic = input("Enter your 24-word mnemonic: ").strip()

    passphrase = input("BIP39 passphrase (empty if none): ") if len(sys.argv) <= 1 else ""

    try:
        kp = derive_keypair(mnemonic, passphrase)
    except Exception as e:
        print(f"[ERROR] Invalid mnemonic: {e}")
        sys.exit(1)

    print(f"Public key : {kp.pubkey()}")
    print(f"Private key: {base58.b58encode(kp.secret() + bytes(kp.pubkey())).decode()}")


if __name__ == "__main__":
    main()
