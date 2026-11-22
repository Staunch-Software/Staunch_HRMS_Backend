"""Prints a new Fernet key for FIELD_ENCRYPTION_KEY in backend/.env.

Back the key up before using it: losing it makes every encrypted field and
generated bank file unreadable.
"""
from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode("ascii"))
