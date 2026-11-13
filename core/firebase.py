"""Firebase Admin setup: token verification + Firestore client."""
import firebase_admin
from django.conf import settings
from firebase_admin import auth, credentials, firestore

_db = None


def get_db():
    global _db
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(settings.FIREBASE_CREDENTIALS))
    if _db is None:
        _db = firestore.client()
    return _db


def verify_token(id_token):
    get_db()  # make sure the app is initialised
    return auth.verify_id_token(id_token, clock_skew_seconds=10)
