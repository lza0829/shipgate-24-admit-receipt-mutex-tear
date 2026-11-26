import os
import tempfile
import uuid

import pytest

# 独立临时库，避免污染 data/shipgate.db
_tmp = tempfile.TemporaryDirectory()
os.environ["SHIPGATE_DB"] = os.path.join(_tmp.name, "fixture.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def app_id(client):
    r = client.post(
        "/api/应用",
        json={"名称": f"互斥夹具应用-{uuid.uuid4().hex[:8]}", "负责人": "夹具", "状态": "启用"},
    )
    assert r.status_code == 200, r.text
    return r.json()["编号"]


def make_release(client, app_id, version):
    r = client.post(
        "/api/发版单",
        json={"应用编号": app_id, "目标版本": version, "制品摘要": f"sha256:{version}"},
    )
    assert r.status_code == 200, r.text
    rid = r.json()["编号"]
    g = client.post(f"/api/发版单/{rid}/流水线", json={"流水线": "绿"})
    assert g.status_code == 200, g.text
    return rid


def release_status(client, rid):
    return client.get(f"/api/发版单/{rid}").json()["状态"]


def admit(client, rid):
    return client.post(f"/api/发版单/{rid}/入闸", json={})
