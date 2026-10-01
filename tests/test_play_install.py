"""Synthetic UI regressions; real APKs and device evidence never enter Git."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/android-workbench/components/play-install/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))
import play_ui as ui
import play_install as installer
from workbench.project import generate
from workbench.registry import normalize
from workbench.service import DEFAULTS
from workbench.common import WorkbenchError

TITLE, DEVELOPER, PACKAGE = "Example Utility", "Example Developer", "com.example.utility"


def node(parent, box, *, text="", desc="", clickable=False, scrollable=False, package=ui.PLAY, **extra):
    return ET.SubElement(parent, "node", {"bounds": box, "text": text, "content-desc": desc,
                       "clickable": str(clickable).lower(), "scrollable": str(scrollable).lower(),
                       "enabled": "true", "package": package, **extra})


def fixture(compact=True):
    root = ET.Element("hierarchy")
    body = node(root, "[0,0][1080,2200]", scrollable=True)
    header = node(body, "[60,700][1020,880]")
    if compact:
        identity = node(header, "[260,706][690,878]", desc=TITLE + "\n" + DEVELOPER + "\nContains ads\n")
        button = node(header, "[730,717][1015,849]", clickable=True)
        label = node(button, "[773,756][849,811]", desc="安装")
        node(label, "[773,756][849,811]", text="安装")
        dropdown = node(button, "[880,717][1015,849]", clickable=True)
        node(dropdown, "[882,728][1014,838]", desc="在更多设备上安装")
    else:
        identity = node(header, "[260,706][900,766]", text=TITLE)
        node(header, "[260,770][900,825]", text=DEVELOPER)
        for box, desc in (("[60,900][320,1050]", "3.2 stars"),
                          ("[420,900][680,1050]", "Content rating Everyone"),
                          ("[780,900][1040,1050]", "500K downloads")):
            node(body, box, desc=desc)
        button = node(body, "[60,1110][1020,1240]", clickable=True)
        label = node(button, "[465,1150][615,1205]", text="Install")
    return root, body, header, identity, button, label


def xml(root):
    return ET.tostring(root, encoding="unicode")


def context(package=PACKAGE, sheet=False, parent_task="5"):
    details = "ActivityRecord{abc u0 " + ui.DETAILS + " t" + parent_task + "}"
    resumed = "ActivityRecord{def u0 " + ui.SHEET + " t5}" if sheet else details
    parts = ["topResumedActivity=" + resumed,
             " * Hist #1: " + resumed,
             " packageName=com.android.vending processName=com.android.vending",
             " state=RESUMED finishing=false"]
    if sheet:
        parts += [" Intent { act=com.google.android.finsky.launchInStoreBottomSheetDetailsPage cmp=" + ui.SHEET + " }",
                  " resultTo=" + details, " * Hist #0: " + details,
                  " packageName=com.android.vending processName=com.android.vending", " state=PAUSED finishing=false"]
    parts += [" Intent { act=android.intent.action.VIEW dat=market://details?id=" + package + " pkg=com.android.vending }"]
    return "\n".join(parts)


class PlayUiTest(unittest.TestCase):
    def test_compact_header_and_merged_identity_bind_to_install_label(self):
        root, *_ = fixture()
        action = ui.find_install_action(xml(root), TITLE, DEVELOPER)
        self.assertIsNotNone(action)
        self.assertEqual(action.layout, "compact_header")
        self.assertEqual(action.center, (811, 783))
        self.assertLess(action.center[0], 880, "Tap must not hit the adjacent device picker")

    def test_conventional_full_details_still_work(self):
        root, *_ = fixture(False)
        self.assertEqual(ui.find_install_action(xml(root), TITLE, DEVELOPER).layout, "full_details")

    def test_wrong_title_developer_substrings_and_package_are_rejected(self):
        for field, value in (("content-desc", TITLE + " extra\n" + DEVELOPER),
                             ("content-desc", TITLE + "\nOther Developer"),
                             ("package", "com.example.overlay")):
            root, _, _, identity, *_ = fixture()
            identity.set(field, value)
            self.assertIsNone(ui.find_install_action(xml(root), TITLE, DEVELOPER))

    def test_disabled_install_and_overlapping_device_picker_are_rejected(self):
        root, _, _, _, button, label = fixture()
        button.set("enabled", "false")
        self.assertIsNone(ui.find_install_action(xml(root), TITLE, DEVELOPER))
        button.set("enabled", "true")
        picker = next(n for n in button if n.get("clickable") == "true")
        picker.set("bounds", "[800,717][1015,849]")
        self.assertIsNone(ui.find_install_action(xml(root), TITLE, DEVELOPER))

    def test_recommendation_card_cannot_authorize_install(self):
        root, body, header, _, button, _ = fixture()
        body.remove(header)
        node(body, "[60,80][1020,400]", desc="A different primary app")
        body.append(header)
        self.assertIsNone(ui.find_install_action(xml(root), TITLE, DEVELOPER))

    def test_foreign_overlay_and_hidden_parent_cannot_receive_tap(self):
        root, _, header, _, button, _ = fixture()
        node(root, "[700,700][900,900]", clickable=True, package="com.example.overlay")
        self.assertIsNone(ui.find_install_action(xml(root), TITLE, DEVELOPER))
        root.remove(root[-1])
        header.set("visible-to-user", "false")
        self.assertIsNone(ui.find_install_action(xml(root), TITLE, DEVELOPER))

    def test_duplicate_install_controls_are_not_guessed(self):
        root, _, header, *_ = fixture()
        target = node(header, "[700,820][870,877]", clickable=True)
        node(target, "[720,830][810,870]", text="Install")
        with self.assertRaisesRegex(ui.UiError, "Multiple Install"):
            ui.find_install_action(xml(root), TITLE, DEVELOPER)

    def test_context_binds_resumed_sheet_to_same_task_and_requested_package(self):
        ui.verify_activity(context(), PACKAGE)
        ui.verify_activity(context(sheet=True), PACKAGE)
        for output in (context("com.example.other"), context(sheet=True, parent_task="6"),
                       context(sheet=True).replace("u0 " + ui.DETAILS, "u10 " + ui.DETAILS),
                       context(sheet=True).replace("resultTo=", "unrelated=")):
            with self.assertRaises(ui.UiError):
                ui.verify_activity(output, PACKAGE)

    def test_android12_resumed_state_fields_and_internal_legacy_market_intent(self):
        output = context(sheet=True).replace('topResumedActivity=', 'ResumedActivity: ')
        output = output.replace('state=RESUMED finishing=false', 'state=RESUMED stopped=false delayedResume=false finishing=false')
        output = output.replace('state=PAUSED finishing=false', 'state=PAUSED stopped=false delayedResume=false finishing=false')
        output = output.replace('market://details?id=', 'http://market.android.com/details?id=')
        ui.verify_activity(output, PACKAGE)
        for invalid in (output.replace(PACKAGE, 'com.example.other'),
                        output.replace('finishing=false', 'finishing=true'),
                        output.replace('market.android.com', 'market.android.com.evil')):
            if invalid != output:
                with self.assertRaises(ui.UiError):
                    ui.verify_activity(invalid, PACKAGE)

    def test_google_only_sources(self):
        for source in (PACKAGE, "https://play.google.com/store/apps/details?id=" + PACKAGE,
                       "https://play.google.com/store/apps/details/Example_Utility?id=" + PACKAGE,
                       "market://details?id=" + PACKAGE):
            self.assertEqual(ui.listing_package(source), PACKAGE)
        for source in ("https://mirror.example/app.apk", "https://play.google.com.evil/store/apps/details?id=" + PACKAGE,
                       "https://play.google.com/store/apps/details?id=" + PACKAGE + "&id=com.example.other"):
            with self.assertRaises(ui.UiError):
                ui.listing_package(source)


class FakeDevice:
    def __init__(self, pages, installed=False):
        self.pages = iter(pages)
        self.serial = "synthetic-device"
        self.calls = []
        self.is_installed = installed

    def installed(self, package):
        return {"package": package, "installer": ui.PLAY} if self.is_installed else None

    def shell(self, *args):
        self.calls.append(args)
        return type("Result", (), {"stdout": b""})()

    def capture(self, label):
        return next(self.pages)

    def screenshot(self, label):
        self.calls.append(("screenshot", label))


class PlayInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.identity = {"package": PACKAGE, "title": TITLE, "developer": DEVELOPER}
        self.args = type("Args", (), {"source": PACKAGE, "mode": "install", "output": self.output,
                                     "page_timeout": .01, "install_timeout": .01, "poll_interval": .001})()

    def test_unknown_layout_emits_llm_handoff_without_tapping(self):
        root, _, _, identity, *_ = fixture()
        identity.set("content-desc", "Unknown app")
        device = FakeDevice([(xml(root), context())] * 10)
        with patch.object(installer, "official_identity", return_value=self.identity):
            result = installer.install(self.args, device, {"tap_sent": False})
        self.assertEqual(result["outcome"], "needs_llm")
        self.assertNotIn(("input", "tap"), [c[:2] for c in device.calls])
        handoff = json.loads((self.output / "llm-repair.json").read_text())
        self.assertEqual(handoff["next_operation"], "apk.play_analyze_ui")
        self.assertEqual(handoff["max_repair_attempts"], 2)

    def test_unrecognized_layout_with_correct_identity_is_routed_to_llm(self):
        root, _, header, identity, *_ = fixture()
        identity.set("content-desc", TITLE)
        node(header, "[260,810][690,878]", text=DEVELOPER)
        device = FakeDevice([(xml(root), context())] * 10)
        with patch.object(installer, "official_identity", return_value=self.identity):
            result = installer.install(self.args, device, {"tap_sent": False})
        self.assertEqual(result["outcome"], "needs_llm")
        self.assertTrue(result["llm_repair"]["candidates"])
        self.assertFalse(any(c[:2] == ("input", "tap") for c in device.calls))

    def test_official_jsonld_title_slug_is_accepted_but_other_package_is_not(self):
        payload = {"url": "https://play.google.com/store/apps/details/Example_Utility?id=" + PACKAGE,
                   "name": TITLE, "author": {"name": DEVELOPER}}
        response = type("Response", (), {"geturl": lambda s: "https://play.google.com/store/apps/details?id=" + PACKAGE,
                                         "read": lambda s, n: ('<script type="application/ld+json">' + json.dumps(payload) + '</script>').encode(),
                                         "__enter__": lambda s: s, "__exit__": lambda *a: None})()
        with patch.object(installer, "urlopen", return_value=response):
            self.assertEqual(installer.official_identity(PACKAGE)["developer"], DEVELOPER)
            payload["url"] = payload["url"].replace(PACKAGE, "com.example.other")
            with self.assertRaises(ui.UiError):
                installer.official_identity(PACKAGE)

    def test_changed_identity_before_tap_never_sends_input(self):
        root, *_ = fixture()
        other, _, _, identity, *_ = fixture()
        identity.set("content-desc", TITLE + "\nOther Developer")
        device = FakeDevice([(xml(root), context()), (xml(other), context())])
        with patch.object(installer, "official_identity", return_value=self.identity):
            result = installer.install(self.args, device, {"tap_sent": False})
        self.assertEqual(result["outcome"], "needs_llm")
        self.assertFalse(any(c[:2] == ("input", "tap") for c in device.calls))
        handoff = json.loads((self.output / "llm-repair.json").read_text())
        self.assertIn("pre-tap.xml", handoff["evidence"])
        self.assertEqual(handoff["ui_sha256"], installer.digest(xml(other).encode()))

    def test_foreground_package_change_before_tap_never_sends_input(self):
        root, *_ = fixture()
        device = FakeDevice([(xml(root), context()), (xml(root), context("com.example.other"))])
        with patch.object(installer, "official_identity", return_value=self.identity):
            result = installer.install(self.args, device, {"tap_sent": False})
        self.assertEqual(result["outcome"], "needs_llm")
        self.assertFalse(any(c[:2] == ("input", "tap") for c in device.calls))

    def test_install_success_requires_package_manager_and_play_installer(self):
        root, *_ = fixture()
        device = FakeDevice([(xml(root), context())] * 2)
        original_shell = device.shell

        def shell(*args):
            if args[:2] == ("input", "tap"):
                device.is_installed = True
            return original_shell(*args)

        device.shell = shell
        state = {"tap_sent": False}
        with patch.object(installer, "official_identity", return_value=self.identity):
            result = installer.install(self.args, device, state)
        self.assertEqual(result["outcome"], "installed")
        self.assertEqual(result["cleanup_status"], "passed")
        self.assertIn(("input", "tap", "811", "783"), device.calls)

    def test_timeout_after_tap_retains_unknown_install_state(self):
        root, *_ = fixture()
        device = FakeDevice([(xml(root), context())] * 20)
        state = {"tap_sent": False}
        with patch.object(installer, "official_identity", return_value=self.identity):
            result = installer.install(self.args, device, state)
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertTrue(state["tap_sent"])

    def test_real_account_gate_requires_user_instead_of_parser_repair(self):
        root, body, *_ = fixture()
        node(body, "[100,100][900,200]", text="Authentication is required")
        device = FakeDevice([(xml(root), context())])
        with patch.object(installer, "official_identity", return_value=self.identity):
            result = installer.install(self.args, device, {})
        self.assertEqual(result["outcome"], "requires_user")
        self.assertEqual(result["gate"], "sign_in")
        self.assertFalse((self.output / "llm-repair.json").exists())

    def test_recovery_cannot_release_unverified_installation(self):
        self.args.mode = "recover"
        result = installer.install(self.args, FakeDevice([]), {})
        self.assertEqual(result["cleanup_status"], "failed")

    def test_registry_requires_device_and_tracks_replay_inputs(self):
        project = self.output / "project"
        project.mkdir()
        (project / "workbench.project.json").write_text(json.dumps(generate(project, ROOT, sys.executable)))
        with self.assertRaisesRegex(WorkbenchError, "Device required"):
            normalize({"project": str(project), "operation": "apk.play_install",
                       "args": ["install", PACKAGE, "--serial", "fixture"]}, DEFAULTS, self.output / "job")
        hierarchy = self.output / "synthetic.xml"
        hierarchy.write_text(xml(fixture()[0]))
        identity = self.output / "synthetic.json"
        identity.write_text(json.dumps(self.identity))
        job = normalize({"project": str(project), "operation": "apk.play_analyze_ui",
                         "args": ["analyze-ui", "--xml", str(hierarchy), "--identity", str(identity)]},
                        DEFAULTS, self.output / "job")
        self.assertTrue({str(hierarchy), str(identity)} <= set(job["input_fingerprints"]))
        self.assertTrue(job["confirms_cleanup"])

    def test_offline_replay_checks_activity_binding_as_well_as_ui(self):
        hierarchy = self.output / "page.xml"
        hierarchy.write_text(xml(fixture()[0]))
        identity = self.output / "identity.json"
        identity.write_text(json.dumps(self.identity))
        activity = self.output / "activity.txt"
        activity.write_text(context("com.example.other"))
        args = type("Args", (), {"xml": hierarchy, "identity": identity, "activity": activity})()
        result = installer.analyze_ui(args)
        self.assertEqual(result["outcome"], "needs_llm")
        self.assertFalse(result["activity_verified"])
        activity.write_text(context(sheet=True))
        result = installer.analyze_ui(args)
        self.assertEqual(result["outcome"], "recognized")
        self.assertTrue(result["activity_verified"])


if __name__ == "__main__":
    unittest.main()
