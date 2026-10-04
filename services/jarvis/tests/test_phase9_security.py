"""The Security Center.

The Phase 9 gate is a claim about honesty, not coverage: findings must carry
evidence and the correct classification, and **unfamiliar must be reported as
unfamiliar, never as malware**. Most of these tests are attempts to get Jarvis
to overstate — to call something suspicious because it is new, to report "all
clear" when it could not look, or to let a severity sneak past a classification
that does not justify it.
"""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.db.engine import Database
from jarvis.security.baseline import Baseline
from jarvis.security.center import FindingsStore, SecurityCenter
from jarvis.security.checks import (
    ALL_CHECKS,
    AntivirusCheck,
    DeviceCheck,
    EncryptionCheck,
    FirewallCheck,
    NetworkCheck,
    StartupCheck,
    _volatile_path,
)
from jarvis.security.classify import Signals, classify, novelty_note, severity_for
from jarvis.security.collectors import (
    PosixCollector,
    PostureCollector,
    Reading,
    WindowsCollector,
    is_external,
)
from jarvis.security.types import Category, Classification, Finding, Severity, fingerprint


class FakeCollector(PostureCollector):
    """A collector whose every reading is dictated by the test."""

    name = "fake"

    def __init__(self, **readings: Reading) -> None:
        self.readings = readings

    def _get(self, name: str) -> Reading:
        return self.readings.get(name, Reading.unknown(f"{name} not set in this test"))

    def antivirus(self) -> Reading:
        return self._get("antivirus")

    def firewall(self) -> Reading:
        return self._get("firewall")

    def disk_encryption(self) -> Reading:
        return self._get("disk_encryption")

    def updates(self) -> Reading:
        return self._get("updates")

    def startup_items(self) -> Reading:
        return self._get("startup_items")

    def removable_devices(self) -> Reading:
        return self._get("removable_devices")

    def listeners(self) -> Reading:
        return self._get("listeners")


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def baseline(db: Database) -> Baseline:
    return Baseline(db)


# ── the rule the whole phase exists for ──────────────────────────────────
class TestClassification:
    def test_novelty_alone_is_normal(self) -> None:
        """The single most important test in this file.

        A tool that calls everything it has not seen before "suspicious" trains
        its user to dismiss it, and then the one real alert is dismissed too.
        """
        assert classify(Signals(novel=True)) is Classification.NORMAL

    def test_novelty_does_not_upgrade_a_weakness(self) -> None:
        assert classify(Signals(weakness=True, novel=True)) is Classification.POTENTIAL

    def test_only_a_named_pattern_produces_suspicious(self) -> None:
        assert classify(Signals(matched_pattern="startup command under tmp")) is (
            Classification.SUSPICIOUS
        )
        assert classify(Signals(weakness=True, observed_fact=True, novel=True)) is not (
            Classification.SUSPICIOUS
        )

    def test_a_measured_fact_is_confirmed(self) -> None:
        assert classify(Signals(observed_fact=True)) is Classification.CONFIRMED

    def test_a_weakness_outranks_a_fact(self) -> None:
        """ "Firewall is off" is about what could happen, not what did."""
        assert classify(Signals(weakness=True, observed_fact=True)) is Classification.POTENTIAL

    def test_trusted_silences_everything_including_a_pattern(self) -> None:
        assert classify(Signals(matched_pattern="anything", trusted=True)) is (
            Classification.NORMAL
        )

    def test_nothing_measured_is_normal(self) -> None:
        assert classify(Signals()) is Classification.NORMAL

    def test_severity_cannot_outrun_the_classification(self) -> None:
        """Closing the back door: "it is new, so call it critical"."""
        assert severity_for(Classification.NORMAL, base=Severity.CRITICAL) is Severity.INFO

    def test_a_potential_risk_is_never_critical(self) -> None:
        """Critical belongs to things that have happened."""
        assert severity_for(Classification.POTENTIAL, base=Severity.CRITICAL) is Severity.HIGH

    def test_a_real_severity_survives(self) -> None:
        assert severity_for(Classification.SUSPICIOUS, base=Severity.HIGH) is Severity.HIGH

    def test_the_novelty_sentence_says_new_not_bad(self) -> None:
        note = novelty_note(True, "this program")
        assert "first time" in note
        assert "not evidence of a problem" in note
        for word in ("malware", "virus", "malicious", "threat", "infected"):
            assert word not in note.lower()

    def test_classification_meanings_never_imply_malware(self) -> None:
        meaning = Classification.SUSPICIOUS.meaning
        assert "not a malware verdict" in meaning
        assert "pattern" in meaning


# ── baseline ─────────────────────────────────────────────────────────────
class TestBaseline:
    def test_first_sighting_is_new_and_the_second_is_not(self, baseline: Baseline) -> None:
        print_ = fingerprint("startup", "Thing")
        assert baseline.observe(print_, "startup", "Thing") is True
        assert baseline.observe(print_, "startup", "Thing") is False

    def test_familiar_is_not_the_same_as_trusted(self, baseline: Baseline) -> None:
        """Malware installed before Jarvis does not become safe by being first."""
        print_ = fingerprint("startup", "Thing")
        baseline.observe(print_, "startup", "Thing")
        assert baseline.is_trusted(print_) is False

    def test_trust_is_only_set_explicitly(self, baseline: Baseline) -> None:
        print_ = fingerprint("startup", "Thing")
        baseline.observe(print_, "startup", "Thing")
        assert baseline.trust(print_) is True
        assert baseline.is_trusted(print_) is True
        assert baseline.trust(print_, False) is True
        assert baseline.is_trusted(print_) is False

    def test_trusting_something_unknown_fails_rather_than_inventing_a_row(
        self, baseline: Baseline
    ) -> None:
        assert baseline.trust("not-a-real-fingerprint") is False

    def test_a_first_scan_is_distinguishable(self, baseline: Baseline) -> None:
        assert baseline.established() is False
        baseline.observe(fingerprint("startup", "x"), "startup", "x")
        assert baseline.established() is True


# ── checks must never claim more than they measured ──────────────────────
class TestChecksAreHonest:
    def test_a_check_that_cannot_run_says_so(self, baseline: Baseline) -> None:
        """ "No findings" would read as "all clear" when nothing was looked at."""
        check = AntivirusCheck(FakeCollector(), baseline)
        result = check.run()
        assert result.ran is False
        assert result.findings == []
        assert result.unavailable_reason

    def test_antivirus_off_is_a_potential_risk_with_evidence(self, baseline: Baseline) -> None:
        collector = FakeCollector(
            antivirus=Reading.measured(
                {"RealTimeProtectionEnabled": False}, source="Get-MpComputerStatus"
            )
        )
        finding = AntivirusCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.POTENTIAL
        assert finding.evidence["realTimeProtection"] is False
        assert finding.evidence["source"] == "Get-MpComputerStatus"
        assert finding.remediation

    def test_healthy_antivirus_is_normal_and_informational(self, baseline: Baseline) -> None:
        collector = FakeCollector(
            antivirus=Reading.measured(
                {"RealTimeProtectionEnabled": True, "AntivirusSignatureAge": 1}, source="x"
            )
        )
        finding = AntivirusCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.NORMAL
        assert finding.severity is Severity.INFO
        assert finding.actionable is False

    def test_a_disabled_firewall_names_the_profiles(self, baseline: Baseline) -> None:
        collector = FakeCollector(
            firewall=Reading.measured(
                [{"Name": "Public", "Enabled": False}, {"Name": "Private", "Enabled": True}],
                source="Get-NetFirewallProfile",
            )
        )
        finding = FirewallCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.POTENTIAL
        assert finding.evidence["disabledProfiles"] == ["Public"]

    def test_unencrypted_volumes_are_a_potential_risk_not_an_event(
        self, baseline: Baseline
    ) -> None:
        collector = FakeCollector(
            disk_encryption=Reading.measured(
                {"encrypted": [], "unencrypted": ["sda2"]}, source="lsblk"
            )
        )
        finding = EncryptionCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.POTENTIAL
        assert finding.severity.rank <= Severity.HIGH.rank
        assert "would happen" in finding.explanation


class TestStartupCheck:
    def _collector(self, items: list[dict[str, Any]]) -> FakeCollector:
        return FakeCollector(startup_items=Reading.measured(items, source="test"))

    def test_an_unrecognised_startup_program_is_not_suspicious(self, baseline: Baseline) -> None:
        """The failure this phase exists to prevent."""
        baseline.observe(fingerprint("x", "y"), "x", "y")  # establish a baseline
        collector = self._collector(
            [{"name": "QtWebEngineProcess", "command": "C:/Program Files/App/q.exe"}]
        )
        findings = StartupCheck(collector, baseline).run().findings
        assert all(f.classification is not Classification.SUSPICIOUS for f in findings)
        assert all(f.severity is Severity.INFO for f in findings)

    def test_a_new_startup_program_is_reported_as_new(self, baseline: Baseline) -> None:
        baseline.observe(fingerprint("x", "y"), "x", "y")
        collector = self._collector([{"name": "NewThing", "command": "C:/Program Files/n.exe"}])
        finding = StartupCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.NORMAL
        assert "new startup program" in finding.title
        assert "not evidence of a problem" in finding.explanation

    def test_a_startup_program_in_a_temp_folder_is_suspicious_with_a_named_reason(
        self, baseline: Baseline
    ) -> None:
        collector = self._collector(
            [{"name": "svchost", "command": r"C:\Users\me\AppData\Local\Temp\svchost.exe"}]
        )
        finding = StartupCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.SUSPICIOUS
        assert finding.evidence["matchedPattern"], "a suspicion must name its pattern"
        assert "not a malware verdict" in finding.explanation

    def test_trusting_a_suspicious_entry_silences_it(self, baseline: Baseline) -> None:
        command = r"C:\Users\me\AppData\Local\Temp\svchost.exe"
        print_ = fingerprint(Category.STARTUP.value, "svchost", command)
        baseline.observe(print_, Category.STARTUP.value, "svchost")
        baseline.trust(print_)
        collector = self._collector([{"name": "svchost", "command": command}])
        finding = StartupCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.NORMAL

    def test_the_first_scan_does_not_report_everything_as_new(self, baseline: Baseline) -> None:
        """Fifty "new" items on a first run is noise nobody reads."""
        collector = self._collector(
            [{"name": f"App{i}", "command": f"/usr/bin/app{i}"} for i in range(50)]
        )
        findings = StartupCheck(collector, baseline).run().findings
        assert len(findings) == 1
        assert "learning what is normal" in findings[0].explanation

    def test_nothing_new_says_nothing_changed(self, baseline: Baseline) -> None:
        collector = self._collector([{"name": "App", "command": "/usr/bin/app"}])
        StartupCheck(collector, baseline).run()  # first scan learns it
        findings = StartupCheck(collector, baseline).run().findings
        assert "all familiar" in findings[0].title
        assert findings[0].classification is Classification.NORMAL


class TestNetworkCheck:
    def test_an_external_listener_is_reported_without_accusation(self, baseline: Baseline) -> None:
        collector = FakeCollector(
            listeners=Reading.measured(
                [
                    {
                        "port": 8080,
                        "address": "0.0.0.0",
                        "protocol": "tcp",
                        "pid": 10,
                        "process": "node",
                        "exe": "/usr/bin/node",
                        "external": True,
                    }
                ],
                source="psutil",
            )
        )
        finding = NetworkCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.NORMAL
        assert "not because anything is wrong" in finding.explanation

    def test_a_listener_running_from_temp_is_suspicious(self, baseline: Baseline) -> None:
        collector = FakeCollector(
            listeners=Reading.measured(
                [
                    {
                        "port": 4444,
                        "address": "0.0.0.0",
                        "protocol": "tcp",
                        "pid": 10,
                        "process": "x",
                        "exe": "/tmp/x",
                        "external": True,
                    }
                ],
                source="psutil",
            )
        )
        finding = NetworkCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.SUSPICIOUS
        assert finding.evidence["matchedPattern"]
        assert finding.remediation

    def test_loopback_only_listeners_are_not_exposed(self, baseline: Baseline) -> None:
        collector = FakeCollector(
            listeners=Reading.measured(
                [
                    {
                        "port": 5432,
                        "address": "127.0.0.1",
                        "protocol": "tcp",
                        "pid": 1,
                        "process": "postgres",
                        "exe": "/usr/bin/postgres",
                        "external": False,
                    }
                ],
                source="psutil",
            )
        )
        finding = NetworkCheck(collector, baseline).run().findings[0]
        assert "Nothing is reachable" in finding.title

    @pytest.mark.parametrize(
        ("address", "external"),
        [
            ("0.0.0.0", True),
            ("::", True),
            ("127.0.0.1", False),
            ("127.0.1.1", False),
            ("::1", False),
            ("[::1]", False),
            ("192.168.1.5", True),
        ],
    )
    def test_external_detection(self, address: str, external: bool) -> None:
        """Regression: `not startswith("127.")` made ::1 look externally reachable."""
        assert is_external(address) is external


class TestDeviceCheck:
    def test_a_new_device_is_reported_as_new_only(self, baseline: Baseline) -> None:
        baseline.observe(fingerprint("x", "y"), "x", "y")
        collector = FakeCollector(
            removable_devices=Reading.measured(
                [{"model": "SanDisk Ultra", "serialNumber": "ABC123"}], source="lsblk"
            )
        )
        finding = DeviceCheck(collector, baseline).run().findings[0]
        assert finding.classification is Classification.NORMAL
        assert finding.severity is Severity.INFO


def test_volatile_path_detection() -> None:
    assert _volatile_path(r"C:\Users\me\AppData\Local\Temp\x.exe") == "appdata/local/temp"
    assert _volatile_path("/tmp/payload") == "tmp"
    assert _volatile_path("/usr/bin/node") == ""
    assert _volatile_path("") == ""


# ── findings store ───────────────────────────────────────────────────────
class TestFindingsStore:
    def _finding(self, title: str = "Firewall is off") -> Finding:
        return Finding(
            category=Category.FIREWALL,
            title=title,
            classification=Classification.POTENTIAL,
            severity=Severity.HIGH,
            explanation="x",
            evidence={"a": 1},
        )

    def test_the_same_condition_is_one_row_across_scans(self, db: Database) -> None:
        """A growing wall of identical alerts is how a user stops reading them."""
        store = FindingsStore(db)
        store.record(self._finding())
        second = store.record(self._finding())
        assert store.count() == 1
        assert second.seen_count == 2

    def test_a_fixed_condition_stops_being_reported(self, db: Database) -> None:
        store = FindingsStore(db)
        recorded = store.record(self._finding())
        assert store.resolve_missing(set()) == 1
        assert store.count() == 0
        assert recorded.id

    def test_a_condition_that_returns_is_open_again(self, db: Database) -> None:
        store = FindingsStore(db)
        store.record(self._finding())
        store.resolve_missing(set())
        again = store.record(self._finding())
        assert again.status == "open"
        assert store.count() == 1

    def test_acknowledging_keeps_it_but_stops_highlighting_it(self, db: Database) -> None:
        store = FindingsStore(db)
        recorded = store.record(self._finding())
        assert store.acknowledge(recorded.id) is True
        rows = store.open_findings()
        assert rows[0]["status"] == "acknowledged"

    def test_acknowledging_something_absent_fails(self, db: Database) -> None:
        assert FindingsStore(db).acknowledge("find_nope") is False

    def test_the_schema_refuses_an_invented_classification(self, db: Database) -> None:
        """The honesty rule is enforced by the database, not by prose."""
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO security_findings (id, detected_at, category, severity,"
                " classification, title, fingerprint, first_seen, last_seen)"
                " VALUES ('f','t','firewall','high','probably_malware','t','fp','t','t')"
            )


# ── a whole scan ─────────────────────────────────────────────────────────
class TestScan:
    def test_a_real_scan_runs_every_check(self, db: Database) -> None:
        report = SecurityCenter(db).scan()
        assert len(report.results) == len(ALL_CHECKS)

    def test_checks_that_could_not_run_are_counted_in_the_headline(self, db: Database) -> None:
        """ "Everything looks fine" after half the checks failed is the worst lie."""
        center = SecurityCenter(db, posture=FakeCollector())
        report = center.scan()
        assert len(report.unavailable) == len(ALL_CHECKS)
        assert "no idea what its security posture is" in report.headline()

    def test_a_broken_check_never_stops_the_others(self, db: Database) -> None:
        class Exploding(FakeCollector):
            def firewall(self) -> Reading:
                raise RuntimeError("boom")

        report = SecurityCenter(db, posture=Exploding()).scan()
        assert len(report.results) == len(ALL_CHECKS)
        failed = [r for r in report.results if "boom" in r.unavailable_reason]
        assert failed and failed[0].ran is False

    def test_actionable_findings_come_first_and_worst_first(self, db: Database) -> None:
        collector = FakeCollector(
            antivirus=Reading.measured({"RealTimeProtectionEnabled": False}, source="t"),
            startup_items=Reading.measured([{"name": "x", "command": "/tmp/x"}], source="t"),
        )
        report = SecurityCenter(db, posture=collector).scan()
        assert report.actionable[0].classification is Classification.SUSPICIOUS

    def test_normal_findings_are_kept_but_not_actionable(self, db: Database) -> None:
        collector = FakeCollector(
            firewall=Reading.measured([{"Name": "Public", "Enabled": True}], source="t")
        )
        report = SecurityCenter(db, posture=collector).scan()
        assert any(f.classification is Classification.NORMAL for f in report.findings)
        assert report.actionable == []

    def test_only_actionable_findings_are_stored(self, db: Database) -> None:
        """The findings table is a to-do list, not a log of everything checked."""
        collector = FakeCollector(
            firewall=Reading.measured([{"Name": "Public", "Enabled": True}], source="t")
        )
        center = SecurityCenter(db, posture=collector)
        center.scan()
        assert center.findings.count() == 0

    def test_the_center_never_offers_a_way_to_change_a_setting(self, db: Database) -> None:
        """Read-only by construction, asserted so a future change has to be deliberate."""
        center = SecurityCenter(db)
        forbidden = ("enable", "disable", "set_", "turn_on", "turn_off", "remediate", "fix")
        for name in dir(center.collector):
            if name.startswith("_"):
                continue
            assert not name.startswith(forbidden), f"collector exposes {name}"
        assert center.status()["neverChangesSettings"] is True


# ── the collectors themselves ────────────────────────────────────────────
class TestCollectors:
    def test_the_posix_collector_reads_real_startup_items(self) -> None:
        reading = PosixCollector().startup_items()
        assert reading.known
        assert isinstance(reading.value, list)
        assert reading.source

    def test_the_posix_collector_reads_real_listeners(self) -> None:
        reading = PosixCollector().listeners()
        assert reading.known or "administrator" in reading.reason

    def test_windows_only_checks_say_so_rather_than_inventing_a_status(self) -> None:
        reading = PosixCollector().antivirus()
        assert reading.known is False
        assert "Windows" in reading.reason

    def test_the_windows_collector_only_uses_read_only_cmdlets(self) -> None:
        """Nothing here changes a setting, and that is asserted rather than assumed.

        These cannot be executed in Linux CI, so the guarantee is checked
        structurally: every cmdlet named is a Get- or a Where-/Select- filter.
        """
        import inspect

        source = inspect.getsource(WindowsCollector)
        for verb in ("Set-", "Remove-", "Disable-", "Enable-", "New-", "Start-", "Stop-"):
            assert verb not in source, f"the Windows collector uses {verb}"
        assert "Get-MpComputerStatus" in source
        assert "Get-NetFirewallProfile" in source
        assert "Get-BitLockerVolume" in source


# ── the agent path and the API ───────────────────────────────────────────
class TestSecurityThroughTheAgent:
    async def test_asking_about_security_runs_a_real_scan(self, ctx) -> None:
        from jarvis.agents.types import EventType

        session = ctx.sessions.create("s")
        results = [
            e
            async for e in ctx.orchestrator.handle(session.id, "check my security")
            if e.type is EventType.TOOL_RESULT
        ]
        assert results, "a security question must reach the security tool"
        data = results[0].data["data"]
        assert data["checksTotal"] == len(ALL_CHECKS)
        assert "headline" in data

    async def test_the_reply_never_claims_more_than_was_checked(self, ctx) -> None:
        from jarvis.agents.types import EventType

        session = ctx.sessions.create("s")
        summaries = [
            str(e.data.get("summary", ""))
            async for e in ctx.orchestrator.handle(session.id, "am I safe")
            if e.type is EventType.TOOL_RESULT
        ]
        text = " ".join(summaries).lower()
        # Whatever it found, it must not pronounce the machine secure outright.
        assert "you are secure" not in text
        assert "no viruses" not in text

    async def test_the_security_tool_is_registered_and_read_only(self, ctx) -> None:
        spec = next(s for s in ctx.registry.specs() if s["name"] == "security")
        assert spec["scopes"] == ["security.read"]
        assert spec["risk"] == "safe"


class TestSecurityApi:
    async def test_status_before_any_scan(self, client) -> None:
        body = (await client.get("/security/status")).json()
        assert body["lastScan"] is None
        assert body["baselineEstablished"] is False
        assert body["neverChangesSettings"] is True

    async def test_a_scan_returns_checks_and_what_could_not_run(self, client) -> None:
        body = (await client.post("/security/scan")).json()
        assert body["checksTotal"] == len(ALL_CHECKS)
        assert isinstance(body["unavailable"], list)
        for entry in body["unavailable"]:
            assert entry["reason"], "an unavailable check must say why"

    async def test_every_finding_carries_its_evidence(self, client) -> None:
        body = (await client.post("/security/scan")).json()
        for finding in body["findings"]:
            assert finding["evidence"], f"{finding['title']} has no evidence"
            assert finding["explanation"]
            assert finding["classificationMeaning"]

    async def test_no_finding_claims_malware(self, client) -> None:
        """The gate, asserted against the real output of a real scan."""
        body = (await client.post("/security/scan")).json()
        text = " ".join(f"{f['title']} {f['explanation']}" for f in body["findings"]).lower()
        for word in ("malware", "virus detected", "infected", "trojan", "malicious"):
            assert word not in text, f"a scan output used the word {word!r}"

    async def test_acknowledging_a_finding(self, client, ctx) -> None:
        await client.post("/security/scan")
        findings = ctx.security.findings.open_findings()
        if not findings:
            pytest.skip("this machine produced no actionable findings")
        response = await client.post(f"/security/findings/{findings[0]['id']}/acknowledge")
        assert response.status_code == 200
        assert ctx.security.findings.open_findings()[0]["status"] == "acknowledged"

    async def test_acknowledging_something_absent_is_404(self, client) -> None:
        assert (await client.post("/security/findings/nope/acknowledge")).status_code == 404

    async def test_the_baseline_is_listed_after_a_scan(self, client) -> None:
        await client.post("/security/scan")
        body = (await client.get("/security/baseline")).json()
        assert isinstance(body["baseline"], list)

    async def test_trusting_something_absent_is_404(self, client) -> None:
        response = await client.post("/security/baseline/not-a-fingerprint/trust", json={})
        assert response.status_code == 404

    async def test_security_endpoints_need_a_token(self, app) -> None:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
            assert (await anon.get("/security/status")).status_code == 401
            assert (await anon.post("/security/scan")).status_code == 401

    async def test_a_scan_is_audited(self, client, ctx) -> None:
        await client.post("/security/scan")
        assert any(e["action"] == "security.scan" for e in ctx.audit.recent(limit=20))
        assert ctx.audit.verify()[0]

    async def test_there_is_no_endpoint_that_changes_a_setting(self, app) -> None:
        """Asserted on the routing table so adding one has to be deliberate."""
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        security = {p for p in paths if p.startswith("/security")}
        for path in security:
            assert not any(
                word in path for word in ("enable", "disable", "remediate", "fix", "repair")
            ), f"{path} looks like it changes a setting"
