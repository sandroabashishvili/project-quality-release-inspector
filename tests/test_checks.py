from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality_system.__main__ import release_exit_code
from quality_system.checks import check_git_hygiene, check_html, check_inventory, check_live_home, check_sitemap
from quality_system.checks_website import check_website_essentials
from quality_system.checks_operations import (
    check_automation_files,
    check_external_links,
    check_generator_contracts,
)
from quality_system.config import SYSTEM_ROOT, load_projects
from quality_system.history import compare_with_previous, save_history
from quality_system.models import Finding, Project, RunResult
from quality_system.policy import apply_policy, summarize_project
from quality_system.process import scan_lock
from quality_system.report import write_reports


VALID_HTML = """<!doctype html><html lang="de"><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Test page description">
<link rel="canonical" href="https://example.test/">
<meta property="og:title" content="Test"><meta property="og:description" content="Test">
<meta property="og:image" content="https://example.test/card.png"><meta property="og:url" content="https://example.test/">
<meta name="twitter:card" content="summary_large_image"><title>Test</title></head>
<body><h1>Test</h1><img src="image.png" alt="Test image"><a href="next/">Next</a></body></html>"""


class QualitySystemTests(unittest.TestCase):
    def test_real_configuration_resolves_project_paths_and_profiles(self):
        projects = load_projects()
        self.assertGreaterEqual(len(projects), 1)
        self.assertTrue(all(project.path.is_absolute() for project in projects))
        self.assertTrue(all(project.profile for project in projects))

    def test_new_project_uses_profile_without_core_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "site"
            project_root.mkdir()
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "id": "new-site", "name": "New Site", "path": "site",
                "profile": "static_website", "browser_paths": ["/"]
            }]}), encoding="utf-8")
            project = load_projects(config)[0]
            self.assertEqual(project.profile, "static_website")
            self.assertTrue(project.checks["browser"])
            self.assertEqual(project.path, project_root.resolve())

    def test_valid_static_project_passes_local_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(VALID_HTML, encoding="utf-8")
            (root / "next").mkdir()
            (root / "next" / "index.html").write_text(VALID_HTML.replace('href="next/"', 'href="../"').replace('src="image.png"', 'src="../image.png"'), encoding="utf-8")
            (root / "image.png").write_bytes(b"fixture")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/")
            findings = check_html(project)
            self.assertFalse(any(item.severity == "error" for item in findings))

    def test_broken_asset_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(VALID_HTML.replace("image.png", "missing.png"), encoding="utf-8")
            (root / "next").mkdir()
            (root / "next" / "index.html").write_text(VALID_HTML.replace('href="next/"', 'href="../"').replace('src="image.png"', 'src="../image.png"'), encoding="utf-8")
            (root / "image.png").write_bytes(b"fixture")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/")
            findings = check_html(project)
            self.assertTrue(any(item.check == "links" and item.severity == "error" for item in findings))

    def test_decorative_empty_alt_is_valid_accessibility_markup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html = VALID_HTML.replace('alt="Test image"', 'alt=""')
            (root / "index.html").write_text(html, encoding="utf-8")
            (root / "next").mkdir()
            (root / "next" / "index.html").write_text(html.replace('href="next/"', 'href="../"').replace('src="image.png"', 'src="../image.png"'), encoding="utf-8")
            (root / "image.png").write_bytes(b"fixture")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/")
            findings = check_html(project)
            self.assertFalse(any(item.check == "accessibility" and "alt text" in item.message for item in findings))

    def test_html_link_without_clean_route_is_informational(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(VALID_HTML.replace('href="next/"', 'href="next.html"'), encoding="utf-8")
            (root / "next.html").write_text(VALID_HTML.replace('href="next/"', 'href="./"'), encoding="utf-8")
            (root / "image.png").write_bytes(b"fixture")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/")
            findings = check_html(project)
            self.assertTrue(any(item.check == "clean-urls" and item.severity == "info" for item in findings))
            self.assertFalse(any(item.check == "clean-urls" and item.severity == "error" for item in findings))

    def test_html_link_is_an_error_when_clean_route_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(VALID_HTML.replace('href="next/"', 'href="next.html"'), encoding="utf-8")
            (root / "next.html").write_text(VALID_HTML.replace('href="next/"', 'href="./"'), encoding="utf-8")
            (root / "next").mkdir()
            (root / "next" / "index.html").write_text(VALID_HTML.replace('href="next/"', 'href="../"').replace('src="image.png"', 'src="../image.png"'), encoding="utf-8")
            (root / "image.png").write_bytes(b"fixture")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/")
            findings = check_html(project)
            self.assertTrue(any(item.check == "clean-urls" and item.severity == "error" for item in findings))

    def test_manual_sitemap_index_and_robots_reference_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sitemap.xml").write_text("<urlset><url><loc>https://example.test/</loc></url></urlset>", encoding="utf-8")
            (root / "sitemap_index.xml").write_text("<sitemapindex><sitemap><loc>https://example.test/sitemap.xml</loc></sitemap></sitemapindex>", encoding="utf-8")
            (root / "robots.txt").write_text("User-agent: *\nAllow: /\n\nSitemap: https://example.test/sitemap_index.xml\n", encoding="utf-8")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/")
            findings = check_sitemap(project)
            self.assertFalse(any(item.severity in {"error", "warning"} for item in findings))
            self.assertTrue(any("sitemap_index.xml references 1" in item.message for item in findings))

    def test_missing_required_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project("fixture", "Fixture", Path(tmp), "docs", required_files=["README.md"])
            findings = check_inventory(project)
            self.assertTrue(any(item.severity == "error" for item in findings))

    def test_secret_scanner_flags_tracked_secret_but_not_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            fake_secret = "abc" * 12
            (root / "settings.py").write_text(f'api_key = "{fake_secret}"\n', encoding="utf-8")
            (root / ".env.example").write_text('api_key = "replace-with-your-api-key"\n', encoding="utf-8")
            subprocess.run(["git", "add", "settings.py", ".env.example"], cwd=root, check=True)
            project = Project("fixture", "Fixture", root, "static")
            findings = check_git_hygiene(project)
            self.assertTrue(any(item.check == "security" and item.severity == "error" for item in findings))

    def test_offline_live_check_never_uses_network(self):
        project = Project("fixture", "Fixture", Path("."), "static", live_url="https://example.invalid/")
        with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            self.assertEqual(check_live_home(project, offline=True), [])

    def test_automation_audit_rejects_retired_domain_and_invalid_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "scripts" / "publish.sh").write_text("BASE=https://old.example.invalid\n", encoding="utf-8")
            (root / ".github" / "workflows" / "quality.yml").write_text("jobs: [invalid\n", encoding="utf-8")
            project = Project(
                "fixture", "Fixture", root, "static",
                automation_paths=["scripts/publish.sh"],
                forbidden_strings=["https://old.example.invalid"],
            )
            findings = check_automation_files(project)
            self.assertTrue(any(item.check == "automation" and item.severity == "error" for item in findings))
            self.assertTrue(any(item.check == "workflow" and item.severity == "error" for item in findings))

    def test_generator_contract_detects_old_domain_returning(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(
                "fixture", "Fixture", Path(tmp), "static",
                generator_contracts=[{
                    "name": "fixture renderer",
                    "command": ["python3", "-c", "print('https://old.example.invalid')"],
                    "expected_strings": ["https://new.example.test"],
                    "forbidden_strings": ["https://old.example.invalid"],
                }],
            )
            findings = check_generator_contracts(project)
            self.assertTrue(any(item.check == "generator-contract" and item.severity == "error" for item in findings))

    def test_generator_contract_passes_approved_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(
                "fixture", "Fixture", Path(tmp), "static",
                generator_contracts=[{
                    "name": "fixture renderer",
                    "command": ["python3", "-c", "print('https://new.example.test')"],
                    "expected_strings": ["https://new.example.test"],
                    "forbidden_strings": ["https://old.example.invalid"],
                }],
            )
            findings = check_generator_contracts(project)
            self.assertTrue(any(item.check == "generator-contract" and item.severity == "pass" for item in findings))

    def test_external_link_checker_is_offline_safe_and_flags_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text('<a href="https://outside.example/missing">Outside</a>', encoding="utf-8")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/", browser_paths=["/"])
            with patch("quality_system.checks_operations._probe_url", return_value=("https://outside.example/missing", 404, "Not Found")):
                findings = check_external_links(project, offline=False, full=True)
            self.assertTrue(any(item.check == "external-links" and item.severity == "warning" for item in findings))
            with patch("quality_system.checks_operations._probe_url", side_effect=AssertionError("network used")):
                self.assertEqual(check_external_links(project, offline=True, full=True), [])

    def test_complete_website_essentials_contract_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (180).to_bytes(4, "big") + (180).to_bytes(4, "big")
            (root / "favicon.svg").write_text('<svg viewBox="0 0 64 64"></svg>', encoding="utf-8")
            (root / "apple.png").write_bytes(png_header)
            (root / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (192).to_bytes(4, "big") + (192).to_bytes(4, "big"))
            (root / "icon-512.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (512).to_bytes(4, "big") + (512).to_bytes(4, "big"))
            (root / "card.png").write_bytes(png_header)
            (root / "manifest.webmanifest").write_text(json.dumps({
                "name": "Fixture", "short_name": "Fixture", "start_url": "/", "scope": "/", "display": "standalone",
                "icons": [
                    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            }), encoding="utf-8")
            (root / "robots.txt").write_text("User-agent: *\nAllow: /\nSitemap: https://example.test/sitemap.xml\n", encoding="utf-8")
            (root / "sitemap.xml").write_text("<urlset><url><loc>https://example.test/</loc></url></urlset>", encoding="utf-8")
            (root / "404.html").write_text("<!doctype html><title>Not found</title>", encoding="utf-8")
            (root / "site.css").write_text('@media (prefers-color-scheme: dark){} @media print{} @media (prefers-reduced-motion: reduce){}', encoding="utf-8")
            (root / "index.html").write_text("""<!doctype html><html><head>
<link rel="icon" href="favicon.svg"><link rel="apple-touch-icon" href="apple.png"><link rel="manifest" href="manifest.webmanifest">
<link rel="canonical" href="https://example.test/"><link rel="stylesheet" href="site.css">
<meta property="og:image" content="https://example.test/card.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:image" content="https://example.test/card.png">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebSite","url":"https://example.test/"}</script>
</head><body><nav>Navigation</nav><a href="mailto:test@example.test">Contact</a><footer>Footer</footer></body></html>""", encoding="utf-8")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/", theme_policy="automatic")
            findings = check_website_essentials(project)
            self.assertFalse([item for item in findings if item.severity in {"error", "warning"}], findings)

    def test_color_scheme_dark_does_not_fake_automatic_theme_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
            (root / "site.css").write_text(":root { color-scheme: dark; }", encoding="utf-8")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/", theme_policy="automatic")
            findings = check_website_essentials(project)
            self.assertTrue(any(item.check == "theme" and item.severity == "error" and "system-theme" in item.message for item in findings))

    def test_fixed_dark_policy_accepts_intentional_dark_only_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
            (root / "site.css").write_text(":root { color-scheme: dark; }", encoding="utf-8")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/", theme_policy="fixed-dark")
            findings = check_website_essentials(project)
            self.assertTrue(any(item.check == "theme" and item.severity == "pass" and "fixed dark" in item.message for item in findings))

    def test_javascript_system_theme_listener_counts_as_automatic_source_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
            (root / "theme.js").write_text('window.matchMedia("(prefers-color-scheme: dark)")', encoding="utf-8")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/", theme_policy="automatic")
            findings = check_website_essentials(project)
            self.assertTrue(any(item.check == "theme" and item.severity == "pass" for item in findings))

    def test_website_essentials_accepts_local_sitemap_index_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
            (root / "robots.txt").write_text("Sitemap: https://example.test/sitemap_index.xml\n", encoding="utf-8")
            (root / "sitemap.xml").write_text("<urlset></urlset>", encoding="utf-8")
            (root / "sitemap_index.xml").write_text("<sitemapindex></sitemapindex>", encoding="utf-8")
            (root / "404.html").write_text("<html></html>", encoding="utf-8")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/", theme_policy="fixed-light")
            findings = check_website_essentials(project)
            self.assertFalse(any(item.message.startswith("robots.txt sitemap reference") and item.severity == "error" for item in findings))

    def test_website_essentials_accepts_json_ld_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {"@context": "https://schema.org", "@graph": [{"@type": "WebSite", "name": "Fixture"}]}
            (root / "index.html").write_text(
                '<html><head><script type="application/ld+json">' + json.dumps(payload) + "</script></head><body></body></html>",
                encoding="utf-8",
            )
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/", theme_policy="fixed-light")
            findings = check_website_essentials(project)
            self.assertFalse(any(item.message == "Homepage JSON-LD is invalid" for item in findings))

    def test_manifest_rejects_wrong_project_path_and_missing_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "favicon.svg").write_text('<svg viewBox="0 0 64 64"></svg>', encoding="utf-8")
            (root / "manifest.webmanifest").write_text(json.dumps({
                "name": "Wrong Project", "short_name": "Wrong",
                "start_url": "/other-project/", "scope": "/other-project/",
                "display": "standalone",
                "icons": [
                    {"src": "missing-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "missing-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            }), encoding="utf-8")
            (root / "index.html").write_text(
                '<html><head><link rel="icon" href="favicon.svg">'
                '<link rel="manifest" href="manifest.webmanifest"></head></html>',
                encoding="utf-8",
            )
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/expected/")
            findings = check_website_essentials(project)
            self.assertTrue(any(item.message == "Manifest start_url or scope does not match the configured public site" for item in findings))
            self.assertTrue(any(item.message == "Manifest icon contract is invalid" for item in findings))

    def test_alternate_link_does_not_count_as_favicon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text('<html><head><link rel="alternate" href="feed.xml"></head></html>', encoding="utf-8")
            project = Project("fixture", "Fixture", root, "static", live_url="https://example.test/")
            findings = check_website_essentials(project)
            self.assertTrue(any(item.message == "Homepage has no favicon declaration" for item in findings))

    def test_release_policy_blocks_high_error_not_low_warning(self):
        project = Project("fixture", "Fixture", Path("."), "static")
        blocked = summarize_project(project, [Finding("fixture", "links", "error", "Broken", priority="high")])
        warned = summarize_project(project, [Finding("fixture", "ruff", "warning", "Lint", priority="low")])
        self.assertEqual(blocked["verdict"], "NOT READY")
        self.assertEqual(warned["verdict"], "READY WITH WARNINGS")
        self.assertEqual(release_exit_code({"fixture": blocked}), 1)
        self.assertEqual(release_exit_code({"fixture": warned}), 0)

    def test_history_detects_fixed_and_new_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp)
            project = Project("fixture", "Fixture", Path(tmp), "static")
            old = RunResult("2026-08-13T10:00:00+02:00", "quick", True, ["fixture"],
                            findings=[Finding("fixture", "links", "error", "Old broken link")])
            apply_policy([project], old)
            save_history(old, history)
            new = RunResult("2026-08-13T11:00:00+02:00", "quick", True, ["fixture"],
                            findings=[Finding("fixture", "seo", "warning", "New metadata warning")])
            apply_policy([project], new)
            compare_with_previous(new, history)
            self.assertEqual(new.comparison["fixed"], 1)
            self.assertEqual(new.comparison["new"], 1)
            self.assertEqual(new.findings[0].change, "new")

    def test_report_writes_html_json_and_machine_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            project = Project("fixture", "Fixture", reports, "static")
            result = RunResult("2026-08-13T12:00:00+02:00", "quick", True, ["fixture"],
                               findings=[Finding("fixture", "inventory", "pass", "OK")])
            apply_policy([project], result)
            result.comparison = {"available": False, "history": {}, "projects": {}}
            html_path, json_path = write_reports(result, reports)
            self.assertTrue(html_path.exists())
            self.assertTrue(json_path.exists())
            self.assertTrue((reports / "latest-summary.json").exists())
            self.assertIn("Quality & Release Dashboard", html_path.read_text(encoding="utf-8"))

    def test_scan_lock_rejects_parallel_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "scan.lock"
            with scan_lock(lock_path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with scan_lock(lock_path):
                        pass

    def test_browser_support_node_tests(self):
        node = SYSTEM_ROOT / "node_modules" / "node" / "bin" / "node"
        result = subprocess.run(
            [
                str(node),
                "--test",
                "browser/tests/visual-policy.test.mjs",
                "browser/tests/lighthouse-profile.test.mjs",
            ],
            cwd=SYSTEM_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
