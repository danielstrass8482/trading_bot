"""
test_saxo_connection.py – Einmaliger Verbindungstest der Saxo-Integration.
Prüft die komplette Kette: DB-Token lesen (ggf. refreshen) → Saxo API-Call.
"""

import json

from saxo_client import saxo_api_get

if __name__ == "__main__":
    account_info = saxo_api_get("port/v1/accounts/me")
    print("✅ Saxo-Verbindung erfolgreich. Kontoinformationen:")
    print(json.dumps(account_info, indent=2, ensure_ascii=False))
