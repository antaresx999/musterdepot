import os
import re
import tempfile

import pytest

# Vor dem Import der App: eigene Testdatenbank, kein Internet, kein Hintergrunddienst
_tmp = tempfile.mkdtemp(prefix="musterdepot-test-")
os.environ.update({
    "MUSTERDEPOT_DATA": _tmp,
    "MUSTERDEPOT_OFFLINE": "1",
    "MUSTERDEPOT_WORKER": "0",
    "MUSTERDEPOT_ADMIN_USER": "admin",
    "MUSTERDEPOT_ADMIN_PASSWORD": "admin-test-pw",
})

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def csrf_of(client, url):
    html = client.get(url).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture(scope="session")
def admin():
    with TestClient(app) as c:
        tok = csrf_of(c, "/login")
        r = c.post("/login", data={"username": "admin", "password": "admin-test-pw", "csrf": tok, "next": "/"})
        assert r.status_code in (200, 303)
        c.csrf = csrf_of(c, "/")
        yield c
