"""Shared test plumbing.

Tests speak to the app over HTTP exactly like the browser does, including the
login: there is deliberately no "skip auth in tests" switch, because a bypass
would make the isolation tests prove nothing about the deployed behaviour.
"""

import httpx

# Matches the default `admin_user`, with a password injected by each test's
# Settings call. Never used outside tests.
TEST_USER = "admin"
TEST_PASSWORD = "test-password-123"


class AuthedClient(httpx.AsyncClient):
    """An ASGI client that logs the test account in on first request."""

    _logged_in = False

    async def send(self, request: httpx.Request, **kwargs):
        if not self._logged_in:
            # Set before the await so a failing login surfaces instead of looping.
            self._logged_in = True
            # `request()` rather than `send()`: only the former extracts the
            # Set-Cookie header into this client's cookie jar.
            login = await self.request("POST", "/api/auth/login",
                                       json={"username": TEST_USER, "password": TEST_PASSWORD})
            if login.status_code != 200:
                raise AssertionError(
                    f"Test login failed: {login.status_code} {login.text[:200]}")
            # The request was built before the cookie existed, and httpx writes
            # cookies into the headers at build time, so it has to be added now.
            cookie = "; ".join(f"{item.name}={item.value}" for item in self.cookies.jar)
            if cookie:
                request.headers["Cookie"] = cookie
        return await super().send(request, **kwargs)
