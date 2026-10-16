"""Passwort-Hashing, Benutzerverwaltung und Session-Dependencies."""

from __future__ import annotations

import sqlite3

import bcrypt
from fastapi import HTTPException, Request

from heizoel import db


class RedirectTo(Exception):
    """Wird von Dependencies geworfen; app.py wandelt sie in eine Weiterleitung um."""

    def __init__(self, url: str):
        self.url = url


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("ascii"))
    except ValueError:
        return False


def create_user(email: str, name: str, password: str, role: str = "user") -> int:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("Bitte eine gültige E-Mail-Adresse angeben.")
    if not name.strip():
        raise ValueError("Bitte einen Namen angeben.")
    if len(password) < 8:
        raise ValueError("Das Passwort muss mindestens 8 Zeichen lang sein.")
    if role not in ("user", "admin"):
        raise ValueError("Ungültige Rolle.")
    try:
        return db.execute(
            "INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, ?)",
            (email, name.strip(), hash_password(password), role),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("Diese E-Mail-Adresse ist bereits registriert.") from exc


def authenticate(email: str, password: str) -> sqlite3.Row | None:
    user = db.query_one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
    if user and verify_password(password, user["password_hash"]):
        return user
    return None


def get_current_user(request: Request) -> sqlite3.Row | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def require_user(request: Request) -> sqlite3.Row:
    user = get_current_user(request)
    if user is None:
        request.session.clear()
        raise RedirectTo("/login")
    return user


def require_profile(request: Request) -> sqlite3.Row:
    """Eingeloggt und Profil vorhanden, sonst Onboarding."""
    user = require_user(request)
    if db.query_one("SELECT 1 FROM profiles WHERE user_id = ?", (user["id"],)) is None:
        raise RedirectTo("/onboarding")
    return user


def require_admin(request: Request) -> sqlite3.Row:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Nur für Administratoren.")
    return user
