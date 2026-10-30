"""HTTP management for the original GS308E firmware family (not GS308Ev4)."""

from hashlib import md5
from itertools import zip_longest
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from .general import BaseSwitch, PvidConfig, SingleVlanConfig, VlanConfig, VlanPortMembership


class Switch(BaseSwitch):
    """Use selection-only requests for reads; never submit membership while reading."""

    _port_count = 8
    _preserve_omitted_ports = True
    _retain_vlan_one = True
    _require_vlan_one_member = True

    def __init__(self, address: str, password: str):
        self._address = address if "://" in address else f"http://{address}"
        parsed = urlsplit(self._address)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Switch address must be an HTTP(S) host")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "Switch address must be a host without credentials, path, query, or fragment"
            )
        self._address = self._address.rstrip("/")
        self._password = password
        self._s = requests.Session()
        self._session_hash = None

    def _request(self, path: str, data: dict | None = None) -> BeautifulSoup:
        method = "GET" if data is None else "POST"
        response = self._s.request(
            method, self._address + path, data=data, timeout=15, allow_redirects=False
        )
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError("Unexpected switch redirect; authenticate again")
        if "RedirectToLoginPage" in response.text and path != "/logout.cgi":
            raise ValueError("Switch session expired; authenticate again")
        soup = BeautifulSoup(response.text, "html.parser")
        error = soup.find("input", attrs={"name": "err_msg"})
        if error and error.get("value"):
            raise ValueError(f"Switch rejected request: {error['value']}")
        return soup

    @staticmethod
    def _field(soup: BeautifulSoup, name: str) -> str:
        element = soup.find("input", attrs={"name": name})
        if element is None or not isinstance(element.get("value"), str):
            raise ValueError(f"Missing switch field {name!r}; unsupported page or expired session")
        return element["value"]

    def _page(self, path: str) -> BeautifulSoup:
        soup = self._request(path)
        self._session_hash = self._field(soup, "hash")
        return soup

    def login(self):
        soup = self._request("/login.htm")
        salt_input = soup.find("input", id="rand")
        if salt_input is None or not salt_input.get("value"):
            raise ValueError("Switch did not supply a login challenge")
        merged = "".join(
            a + b for a, b in zip_longest(self._password, salt_input["value"], fillvalue="")
        )
        self._request(
            "/login.cgi", {"password": md5(merged.encode(), usedforsecurity=False).hexdigest()}
        )
        info = self._page("/switch_info.htm")
        product = info.find("td", string="Product Name")
        if product is None or product.find_next("td").get_text(strip=True) != "GS308E":
            raise ValueError("Expected original GS308E; refusing an unsupported switch model")

    def logout(self):
        try:
            self._request("/logout.cgi")
        finally:
            self._session_hash = None
            self._s.close()

    def _configuration_page(self) -> BeautifulSoup:
        soup = self._page("/8021qCf.htm")
        enabled = soup.find("input", attrs={"name": "status", "value": "Enable", "checked": True})
        if enabled is None:
            raise ValueError(
                "Advanced 802.1Q must already be enabled; no mode changes are automatic"
            )
        return soup

    def fetch_information(self) -> dict[str, str]:
        soup = self._page("/switch_info.htm")
        result = {}
        for name in ("Product Name", "Firmware Version", "Bootloader Version", "MAC Address"):
            label = soup.find("td", string=name)
            if label is None:
                raise ValueError(f"Missing switch information: {name}")
            result[name] = label.find_next("td").get_text(strip=True)
        return result

    def fetch_vlan_membership(self) -> VlanConfig:
        self._configuration_page()
        soup = self._page("/8021qMembe.htm")
        selection = soup.find("select", attrs={"name": "VLAN_ID"})
        if selection is None:
            raise ValueError("Missing VLAN selector")
        vids = [int(option["value"]) for option in selection.find_all("option")]
        if not vids or len(vids) != len(set(vids)) or any(not 1 <= vid <= 4094 for vid in vids):
            raise ValueError("Invalid VLAN inventory")
        result = {}
        for vid in sorted(vids):
            # Firmware distinguishes selection from a write by absence of hiddenMem.
            selected = self._request(
                "/8021qMembe.cgi", {"VLAN_ID": vid, "hash": self._session_hash}
            )
            self._session_hash = self._field(selected, "hash")
            if self._field(selected, "VLAN_ID_HD") != str(vid):
                raise ValueError(f"Switch returned the wrong VLAN for {vid}")
            states = self._field(selected, "hiddenMem")
            if len(states) != self._port_count or any(state not in "123" for state in states):
                raise ValueError(f"Invalid membership for VLAN {vid}")
            result[vid] = {
                port: VlanPortMembership(int(state)) for port, state in enumerate(states, 1)
            }
        return result

    def fetch_pvids(self) -> PvidConfig:
        soup = self._page("/portPVID.htm")
        result = {}
        for row in soup.select("tr.portID"):
            hidden = row.find("input", attrs={"type": "hidden"})
            cell = row.find("td", attrs={"sel": "input"})
            if hidden is None or cell is None:
                raise ValueError("Invalid PVID row")
            port, vid = int(hidden["value"]), int(cell.get_text(strip=True))
            if port in result or not 1 <= vid <= 4094:
                raise ValueError("Invalid or duplicate PVID row")
            result[port] = vid
        if set(result) != set(range(1, self._port_count + 1)):
            raise ValueError("Incomplete PVID inventory")
        return result

    def _add_vlan(self, vid: int):
        page = self._configuration_page()
        self._request(
            "/8021qCf.cgi",
            {
                "status": "Enable",
                "hiddVlan": "",
                "ADD_VLANID": vid,
                "vlanNum": self._field(page, "vlanNum"),
                "hash": self._session_hash,
                "ACTION": "Add",
            },
        )

    def _delete_vlans(self, vids: list[int]):
        if not vids:
            return
        page = self._configuration_page()
        fields = {field["value"]: field["name"] for field in page.select('input[name^="vlanck"]')}
        form = {
            "status": "Enable",
            "hiddVlan": "",
            "ADD_VLANID": "",
            "vlanNum": self._field(page, "vlanNum"),
            "hash": self._session_hash,
            "ACTION": "Delete",
        }
        for vid in vids:
            if str(vid) not in fields:
                raise ValueError(f"VLAN {vid} no longer exists")
            form[fields[str(vid)]] = vid
        self._request("/8021qCf.cgi", form)

    def _write_vlan_membership(self, vid: int, membership: SingleVlanConfig):
        self._page("/8021qMembe.htm")
        self._request(
            "/8021qMembe.cgi",
            {
                "VLAN_ID": vid,
                "VLAN_ID_HD": vid,
                "hash": self._session_hash,
                "hiddenMem": "".join(
                    str(int(membership[port])) for port in range(1, self._port_count + 1)
                ),
            },
        )

    def _write_pvids(self, pvids: PvidConfig):
        for vid in sorted(set(pvids.values())):
            self._page("/portPVID.htm")
            form = {"pvid": vid, "hash": self._session_hash}
            form.update(
                {f"port{port}": "checked" for port, value in sorted(pvids.items()) if value == vid}
            )
            self._request("/portPVID.cgi", form)
