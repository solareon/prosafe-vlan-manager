from pathlib import Path

import pytest
import requests

from prosafe.switches.gs308e import Switch

FIXTURES = Path(__file__).parent / "fixtures" / "gs308e"


class ReadOnlySession:
    def __init__(self):
        self.responses = {}

    def request(self, method, url, *, data, timeout, allow_redirects):
        path = url.rsplit("/", 1)[-1]
        if method == "GET":
            fixture = {
                "8021qCf.htm": "configuration.html",
                "8021qMembe.htm": "vlan1.html",
                "portPVID.htm": "pvids.html",
            }[path]
        else:
            # Reject every write-capable payload, not merely unexpected endpoints.
            assert path == "8021qMembe.cgi"
            assert set(data) == {"VLAN_ID", "hash"}
            fixture = {1: "vlan1.html", 100: "vlan100.html"}[data["VLAN_ID"]]
        response = requests.Response()
        response.status_code = 200
        response._content = self.responses.get(fixture, (FIXTURES / fixture).read_text()).encode()
        return response


@pytest.fixture
def switch():
    switch = Switch("192.0.2.3", "unused-fixture-password")
    switch._s = ReadOnlySession()
    return switch


def test_reads_complete_membership_and_pvids_without_write_payload(switch):
    assert switch.fetch_vlan_membership() == {
        1: {1: 1, 2: 1, **dict.fromkeys(range(3, 9), 3)},
        100: {1: 2, 2: 2, **dict.fromkeys(range(3, 9), 1)},
    }
    assert switch.fetch_pvids() == {1: 1, 2: 1, **dict.fromkeys(range(3, 9), 100)}


def test_unchanged_live_fixture_configuration_causes_no_writes(switch):
    membership = switch.fetch_vlan_membership()
    pvids = switch.fetch_pvids()
    switch.apply_vlan_config(membership, pvids)


@pytest.mark.parametrize("replacement", ["11333", "11333334"])
def test_rejects_incomplete_or_unknown_membership(switch, replacement):
    switch._s.responses["vlan1.html"] = (
        (FIXTURES / "vlan1.html").read_text().replace("11333333", replacement)
    )
    with pytest.raises(ValueError, match="Invalid membership"):
        switch.fetch_vlan_membership()


def test_rejects_wrong_selected_vlan(switch):
    switch._s.responses["vlan100.html"] = (FIXTURES / "vlan1.html").read_text()
    with pytest.raises(ValueError, match="wrong VLAN"):
        switch.fetch_vlan_membership()


def test_rejects_disabled_advanced_vlan_without_enabling_it(switch):
    switch._s.responses["configuration.html"] = (
        (FIXTURES / "configuration.html").read_text().replace("disabled checked", "disabled")
    )
    with pytest.raises(ValueError, match="already be enabled"):
        switch.fetch_vlan_membership()


def test_rejects_incomplete_pvid_inventory(switch):
    switch._s.responses["pvids.html"] = (
        (FIXTURES / "pvids.html").read_text().replace('value="8"', 'value="9"')
    )
    with pytest.raises(ValueError, match="Incomplete PVID"):
        switch.fetch_pvids()


def test_expired_session_is_not_accepted_as_configuration(switch):
    switch._s.responses["vlan1.html"] = (
        "<html><script>function RedirectToLoginPage() {}</script></html>"
    )
    with pytest.raises(ValueError, match="session expired"):
        switch.fetch_vlan_membership()


def test_switch_error_is_reported_without_including_credentials(switch):
    switch._s.responses["vlan100.html"] = '<input name="err_msg" value="Invalid VLAN">'
    with pytest.raises(ValueError, match="Switch rejected request: Invalid VLAN"):
        switch.fetch_vlan_membership()


def test_logout_redirect_clears_session_without_failing_export(monkeypatch):
    switch = Switch("192.0.2.3", "unused")
    switch._session_hash = "12345"
    response = requests.Response()
    response.status_code = 200
    response._content = b"<script>function RedirectToLoginPage() {}</script>"
    monkeypatch.setattr(switch._s, "request", lambda *args, **kwargs: response)
    switch.logout()
    assert switch._session_hash is None
