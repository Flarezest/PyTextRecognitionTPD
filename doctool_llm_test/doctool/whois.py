"""WHOIS: статус домена в реестре — для доменов, которых нет в manager.

Запрос идёт напрямую к WHOIS-серверу реестра (TCP-порт 43). Для .RU/.SU/.РФ — whois.tcinet.ru.
Для остальных зон сервер берётся из whois.iana.org; если порт 43 закрыт, для них пробуется RDAP
(https://rdap.org). Наружу уходит только имя домена.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

from . import parsers

SERVERS = {
    "ru": "whois.tcinet.ru", "su": "whois.tcinet.ru", "xn--p1ai": "whois.tcinet.ru",
    "com": "whois.verisign-grs.com", "net": "whois.verisign-grs.com", "org": "whois.publicinterestregistry.org",
    "moscow": "whois.nic.moscow", "xn--80adxhks": "whois.nic.moscow",
}
NOT_FOUND_RE = re.compile(r"no entries found|no match for|not found|no data found|^status:\s*free|"
                          r"domain not found|object does not exist", re.I | re.M)
KEYS = {
    "registrar": ("registrar", "sponsoring registrar", "registrar name"),
    "state": ("state", "domain status", "status"),
    "created": ("created", "creation date", "registered"),
    "paid_till": ("paid-till", "registry expiry date", "registrar registration expiration date", "expiration date",
                  "expires"),
    "free_date": ("free-date",),
    "org": ("org", "registrant organization"),
    "person": ("person",),
}
MANUAL_URL = "https://www.reg.ru/whois/?dname={domain}"


@dataclass
class WhoisInfo:
    domain: str
    status: str = ""          # registered | free | error
    registrar: str = ""
    state: str = ""
    created: str = ""
    paid_till: str = ""
    free_date: str = ""
    org: str = ""
    person: str = ""
    server: str = ""
    error: str = ""
    manual_url: str = ""
    raw: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self, raw: bool = False) -> dict:
        d = asdict(self)
        if not raw:
            d.pop("raw", None)
        return d

    @property
    def summary(self) -> str:
        if self.status == "free":
            return "WHOIS: домен свободен (не зарегистрирован)"
        if self.status == "registered":
            parts = [f"регистратор {self.registrar}" if self.registrar else "",
                     f"статус {self.state}" if self.state else "",
                     f"оплачен до {self.paid_till[:10]}" if self.paid_till else ""]
            return "WHOIS: зарегистрирован" + (" — " + ", ".join(p for p in parts if p) if any(parts) else "")
        return f"WHOIS недоступен: {self.error}" if self.error else "WHOIS не выполнялся"


def _query(server: str, query: str, timeout: float) -> str:
    with socket.create_connection((server, 43), timeout=timeout) as s:
        s.sendall((query + "\r\n").encode("ascii"))
        chunks = []
        while True:
            c = s.recv(8192)
            if not c:
                break
            chunks.append(c)
    data = b"".join(chunks)
    for enc in ("utf-8", "cp1251", "koi8-r"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


class NoSuchZone(Exception):
    """Доменной зоны нет в корневой зоне (ответ IANA) — например, «.pd» вместо «.рф» из-за ошибки распознавания."""


def server_for(tld: str, timeout: float = 8) -> str:
    if tld in SERVERS:
        return SERVERS[tld]
    txt = _query("whois.iana.org", tld, timeout)
    if re.search(r"returned 0 objects", txt, re.I) or not re.search(r"^domain:\s*\S", txt, re.M | re.I):
        raise NoSuchZone(tld)
    m = re.search(r"^whois:\s*(\S+)", txt, re.M | re.I)
    return m.group(1) if m else ""


def parse(domain: str, text: str, server: str = "") -> WhoisInfo:
    info = WhoisInfo(domain=domain, server=server, raw=text[:4000], manual_url=MANUAL_URL.format(domain=domain))
    if NOT_FOUND_RE.search(text) and not re.search(r"^\s*domain(?: name)?:\s*\S", text, re.I | re.M):
        info.status = "free"
        return info
    vals: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith(("%", "#", ">>>")) or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if v:
            vals.setdefault(k, []).append(v)
    for attr, keys in KEYS.items():
        for k in keys:
            if k in vals:
                v = ", ".join(dict.fromkeys(x.split(" ")[0] if attr == "state" and k != "state" else x for x in vals[k]))
                setattr(info, attr, v)
                break
    info.status = "registered" if (vals.get("domain") or vals.get("domain name") or info.registrar) else "error"
    if info.status == "error":
        info.error = "непонятный ответ WHOIS-сервера"
    return info


def _rdap(domain: str, timeout: float) -> WhoisInfo:
    url = f"https://rdap.org/domain/{domain}"
    info = WhoisInfo(domain=domain, server="rdap.org", manual_url=MANUAL_URL.format(domain=domain))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            info.status = "free"
            return info
        raise
    info.status = "registered"
    info.state = ", ".join(data.get("status") or [])
    for ev in data.get("events") or []:
        if ev.get("eventAction") == "registration":
            info.created = ev.get("eventDate", "")
        elif ev.get("eventAction") == "expiration":
            info.paid_till = ev.get("eventDate", "")
    for ent in data.get("entities") or []:
        if "registrar" in (ent.get("roles") or []):
            for item in (ent.get("vcardArray") or [None, []])[1]:
                if item and item[0] == "fn":
                    info.registrar = item[3]
    return info


def lookup(domain: str, timeout: float = 10) -> WhoisInfo:
    """WHOIS по домену. Ошибки сети не выбрасываются — возвращается status="error".
    Несуществующая зона («.pd» из-за ошибки распознавания) — тоже status="error", а не «свободен»
    (RDAP на неизвестную зону отвечает 404, и раньше такой домен считался свободным)."""
    d = parsers.to_punycode(domain.strip().lower().rstrip("."))
    tld = d.rsplit(".", 1)[-1]
    server = ""
    try:
        server = server_for(tld, timeout)
        if not server:
            raise OSError(f"не найден WHOIS-сервер для зоны .{tld}")
        return parse(domain, _query(server, d, timeout), server)
    except NoSuchZone:
        return WhoisInfo(domain=domain, status="error", server="whois.iana.org",
                         error=f"зоны .{tld} не существует — проверьте написание домена",
                         manual_url=MANUAL_URL.format(domain=domain))
    except OSError as e:
        err = f"{server or 'whois'}:43 — {e}"
    if tld not in ("ru", "su", "xn--p1ai"):
        try:
            return _rdap(d, timeout)
        except Exception as e:  # noqa: BLE001
            err += f"; RDAP — {e}"
    return WhoisInfo(domain=domain, status="error", server=server, error=err + " (возможно, порт 43 закрыт в сети)",
                     manual_url=MANUAL_URL.format(domain=domain))
