"""Hybrid RSA+AES request/response encryption envelope, implemented against
ICICI's documented scheme (Composite_Pay_API_1.23.pdf, "Encryption &
decryption" section and "ENCRYPTION-DECRYPTION PROCESS - For Composite
Payment, Status Check APIs, and CIB APIs", pages 75-78).

Scheme (client -> ICICI):
    SessionKey    = random bytes, 16 (AES-128) or 32 (AES-256)
    encryptedKey  = Base64(RSA_encrypt(SessionKey, ICICI's public key))
                    using PKCS1v15 ("oaepHashingAlgorithm": "NONE") or
                    OAEP-SHA1-MGF1 ("oaepHashingAlgorithm": "SHA1")
    IV            = 16 random bytes
    encryptedData = Base64(AES/CBC/PKCS5Padding(json_payload, SessionKey, IV))
    -> sent as {"iv": Base64(IV), "encryptedData": ...} (the recommended
       "send IV in the iv tag" option, rather than prefixing it onto
       encryptedData).

Scheme (ICICI -> client, response decryption):
    IV          = first 16 bytes of Base64-decoded encryptedData (ICICI
                  always embeds the IV in encryptedData itself for
                  responses, per the doc — never in a separate "iv" field).
    SessionKey  = RSA_decrypt(Base64-decoded encryptedKey, client's
                  private key)
    payload     = AES/CBC/PKCS5Padding-decrypt(rest of encryptedData,
                  SessionKey, IV)

THIS MODULE IS NOT YET USABLE END-TO-END: ICICI's actual RSA public key
(ICICIPubKey.cer) and the client's own key pair are exchanged during
onboarding, which has not happened. It is wired up and unit-testable with
a locally-generated RSA key pair (see tests/test_icici_api_client.py), but
gated behind `settings.ICICI_ENCRYPTION_ENABLED` (default False) in
client.py so it is never invoked with real credentials until the user
confirms with their RM (a) that UAT actually requires this envelope at
all (banks sometimes leave UAT unencrypted) and (b) the real key material
is in hand.
"""
import base64
import json
import os
from typing import Optional

from cryptography.hazmat.primitives import hashes, padding as sym_padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AES_BLOCK_SIZE_BITS = 128


def generate_session_key(length: int = 16) -> bytes:
    if length not in (16, 32):
        raise ValueError("ICICI's documented session key length is 16 (AES-128) or 32 (AES-256) bytes.")
    return os.urandom(length)


def _asym_padding_for(oaep_hashing_algorithm: str):
    algo = (oaep_hashing_algorithm or "NONE").upper()
    if algo == "NONE":
        return asym_padding.PKCS1v15()
    if algo == "SHA1":
        return asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(),
            label=None,
        )
    raise ValueError(f"Unsupported oaepHashingAlgorithm '{oaep_hashing_algorithm}' — ICICI documents only NONE or SHA1.")


def rsa_encrypt_session_key(session_key: bytes, public_key_pem: bytes, oaep_hashing_algorithm: str = "NONE") -> bytes:
    public_key = serialization.load_pem_public_key(public_key_pem)
    return public_key.encrypt(session_key, _asym_padding_for(oaep_hashing_algorithm))


def rsa_decrypt_session_key(
    encrypted_key: bytes,
    private_key_pem: bytes,
    oaep_hashing_algorithm: str = "NONE",
    private_key_password: Optional[bytes] = None,
) -> bytes:
    private_key = serialization.load_pem_private_key(private_key_pem, password=private_key_password)
    return private_key.decrypt(encrypted_key, _asym_padding_for(oaep_hashing_algorithm))


def aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    padder = sym_padding.PKCS7(AES_BLOCK_SIZE_BITS).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(AES_BLOCK_SIZE_BITS).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def build_encrypted_request(
    payload: dict,
    public_key_pem: bytes,
    service: str,
    oaep_hashing_algorithm: str = "NONE",
    request_id: str = "",
    client_info: str = "",
    session_key_length: int = 16,
) -> dict:
    """Wraps `payload` in ICICI's documented envelope:
    {requestId, service, encryptedKey, oaepHashingAlgorithm, iv,
    encryptedData, clientInfo, optionalParam}."""
    session_key = generate_session_key(session_key_length)
    iv = os.urandom(16)
    plaintext = json.dumps(payload).encode("utf-8")
    encrypted_data = aes_cbc_encrypt(plaintext, session_key, iv)
    encrypted_key = rsa_encrypt_session_key(session_key, public_key_pem, oaep_hashing_algorithm)
    return {
        "requestId": request_id,
        "service": service,
        "encryptedKey": base64.b64encode(encrypted_key).decode("ascii"),
        "oaepHashingAlgorithm": oaep_hashing_algorithm,
        "iv": base64.b64encode(iv).decode("ascii"),
        "encryptedData": base64.b64encode(encrypted_data).decode("ascii"),
        "clientInfo": client_info,
        "optionalParam": "",
    }


def decrypt_response(
    response_envelope: dict,
    private_key_pem: bytes,
    oaep_hashing_algorithm: str = "NONE",
) -> dict:
    """Decrypts an ICICI response envelope (encryptedKey/encryptedData) back
    into the plain JSON dict. Per the doc, ICICI always embeds the IV as
    the first 16 bytes of the (base64-decoded) encryptedData for
    responses — never in a separate `iv` field."""
    encrypted_key = base64.b64decode(response_envelope["encryptedKey"])
    session_key = rsa_decrypt_session_key(encrypted_key, private_key_pem, oaep_hashing_algorithm)

    raw = base64.b64decode(response_envelope["encryptedData"])
    iv, ciphertext = raw[:16], raw[16:]
    plaintext = aes_cbc_decrypt(ciphertext, session_key, iv)
    return json.loads(plaintext.decode("utf-8"))
