"""Optional password gate for the dashboard.

Set in .env (recommended: the hash, not the password):
    GIM_DASHBOARD_PASSWORD_SHA256=<output of: python -m gim.ui.auth>
If neither variable is set, no login is shown (fine while the app only listens on
127.0.0.1). On a server ALWAYS combine this with Tailscale or an authenticating proxy --
see docs/DEPLOYMENT.md. This gate is a second layer, not a replacement for HTTPS.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import time

import streamlit as st


def _expected() -> str | None:
    h = os.getenv("GIM_DASHBOARD_PASSWORD_SHA256", "").strip().lower()
    if h:
        return h
    pw = os.getenv("GIM_DASHBOARD_PASSWORD", "")
    return hashlib.sha256(pw.encode()).hexdigest() if pw else None


def check(password: str) -> bool:
    expected = _expected()
    return bool(expected) and hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), expected)


def gate() -> bool:
    """True if the user may proceed."""
    if not _expected() or st.session_state.get("gim_authed"):
        return True
    st.markdown("### 🌐 Global Intelligence Monitor")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            if check(pw):
                st.session_state["gim_authed"] = True
                st.rerun()
            time.sleep(1.5)  # slow down guessing
            st.error("Wrong password.")
    return False


if __name__ == "__main__":
    print(hashlib.sha256(getpass.getpass("Dashboard password: ").encode()).hexdigest())
