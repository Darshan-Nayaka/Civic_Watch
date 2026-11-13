
"""Firebase Admin setup: token verification + Firestore client."""

import os
import json
import firebase_admin
from django.conf import settings
from firebase_admin import auth, credentials, firestore

_db = None


def get_db():
    global _db

    if not firebase_admin._apps:

        # Azure: Read Firebase credentials from environment variable
        firebase_json = os.getenv("FIREBASE_CREDENTIALS_JSON")

        if firebase_json:
            credentials_dict = json.loads(firebase_json)
            cred = credentials.Certificate(credentials_dict)

        # Local: Read serviceAccountKey.json
        else:
            cred = credentials.Certificate(
                settings.FIREBASE_CREDENTIALS
            )

        firebase_admin.initialize_app(cred)

    if _db is None:
        _db = firestore.client()

    return _db


def verify_token(id_token):
    get_db()
    return auth.verify_id_token(
        id_token,
        clock_skew_seconds=10
    )
