#!/usr/bin/env python3
"""Smoke and unit tests for antigravity_usage_scanner."""

import fcntl
import os
import sys
import tempfile
from pathlib import Path

# Add scripts directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from antigravity_usage_scanner import (
    parse_presence,
    prune_stale_locks,
    check_session_working,
    scan,
    default_base_dir,
    sanitize_plain_text,
    read_configured_model,
    compute_bucket_forecast,
    update_quota_snapshots,
    format_hours_duration,
    normalize_timestamp_seconds,
    parse_transcripts,
    check_and_send_quota_notifications,
)


import datetime as dt
import json
import unittest


class TestAntigravityScanner(unittest.TestCase):
    def test_sanitize_plain_text(self):
        self.assertEqual(sanitize_plain_text("hello\x00 world\t"), "hello world")
        self.assertEqual(sanitize_plain_text(None), "")
        self.assertEqual(sanitize_plain_text("   spaced   out   "), "spaced out")

    def test_read_configured_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            # Default fallback when no settings file exists
            self.assertEqual(read_configured_model(pdir), "Gemini 3.8 Flash (High)")

            # Reads model from settings.json
            settings_file = pdir / "settings.json"
            settings_file.write_text(json.dumps({"model": "Claude Opus 4.6 (Thinking)"}))
            self.assertEqual(read_configured_model(pdir), "Claude Opus 4.6 (Thinking)")

    def test_format_hours_duration(self):
        self.assertEqual(format_hours_duration(0.5), "30m")
        self.assertEqual(format_hours_duration(2.5), "2.5h")
        self.assertEqual(format_hours_duration(26.0), "1d 2h")
        # Rollover fix: 47.7 % 24 = 23.7, round = 24 → should become 2d, not "1d 24h"
        self.assertEqual(format_hours_duration(47.7), "2d")
        self.assertEqual(format_hours_duration(24.0), "1d")
        self.assertEqual(format_hours_duration(0), "0m")

    def test_normalize_timestamp_seconds(self):
        # Epoch seconds should pass through
        self.assertAlmostEqual(normalize_timestamp_seconds(1725753600), 1725753600.0)
        # Epoch milliseconds should be divided by 1000
        self.assertAlmostEqual(normalize_timestamp_seconds(1725753600000), 1725753600.0)
        # None returns 0
        self.assertEqual(normalize_timestamp_seconds(None), 0.0)
        # Invalid string returns 0
        self.assertEqual(normalize_timestamp_seconds("not-a-number"), 0.0)

    def test_compute_bucket_forecast(self):
        now_dt = dt.datetime.now(dt.timezone.utc)
        reset_in_5h = (now_dt + dt.timedelta(hours=5)).isoformat().replace("+00:00", "Z")

        # Stable / idle: burn rate ~ 0
        burn_txt, fc_txt, status = compute_bucket_forecast(90.0, 0.0, reset_in_5h)
        self.assertEqual(status, "stable")
        self.assertEqual(fc_txt, "Paced to reset")

        # Safe pace: 80% remaining, burning 2%/h for 5h -> 70% left at reset
        burn_txt, fc_txt, status = compute_bucket_forecast(80.0, 2.0, reset_in_5h)
        self.assertEqual(status, "safe")
        self.assertIn("On pace", fc_txt)
        self.assertEqual(burn_txt, "2.0%/h")

        # Warning / tight pace: 20% remaining, burning 2%/h for 5h -> 10% left at reset (<15%)
        burn_txt, fc_txt, status = compute_bucket_forecast(20.0, 2.0, reset_in_5h)
        self.assertEqual(status, "warning")
        self.assertIn("Tight pace", fc_txt)

        # Critical: 20% remaining, burning 10%/h for 5h -> depletes in 2.0h before 5h reset
        burn_txt, fc_txt, status = compute_bucket_forecast(20.0, 10.0, reset_in_5h)
        self.assertEqual(status, "critical")
        self.assertIn("Depletes in ~2.0h (before reset)", fc_txt)

    def test_update_quota_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            groups = [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {"id": "gemini-5h", "remaining_fraction": 0.90}
                    ]
                }
            ]
            rates = update_quota_snapshots(pdir, groups)
            # First snapshot establishes baseline
            self.assertIn("gemini-5h", rates)
            self.assertEqual(rates["gemini-5h"], 0.0)

            snap_file = pdir / "cache" / "quota_snapshots.json"
            self.assertTrue(snap_file.exists())

    def test_transcript_caching(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            brain_dir = pdir / "brain"
            tpath = brain_dir / "conv1" / ".system_generated" / "logs" / "transcript.jsonl"
            tpath.parent.mkdir(parents=True, exist_ok=True)
            tpath.write_text(json.dumps({
                "type": "USER_INPUT",
                "created_at": "2026-09-08T01:00:00Z",
                "tool_calls": [{"name": "run_command"}]
            }) + "\n")

            # First parse: builds cache
            tools1, models1, list1, latest1 = parse_transcripts(brain_dir, "2026-09-08", ["2026-09-08"], base_dir=pdir)
            self.assertEqual(tools1["run_command"], 1)

            cache_file = pdir / "cache" / "transcript_stats_cache.json"
            self.assertTrue(cache_file.exists())

            # Second parse: reads from cache
            tools2, models2, list2, latest2 = parse_transcripts(brain_dir, "2026-09-08", ["2026-09-08"], base_dir=pdir)
            self.assertEqual(tools2["run_command"], 1)

    def test_presence_flock_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            lock_held = pdir / "held_session.lock"
            lock_stale = pdir / "stale_session.lock"

            lock_held.touch()
            lock_stale.touch()

            # Hold lock on lock_held
            f = open(lock_held, "rb")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            active = parse_presence(pdir)
            self.assertIn("held_session", active)
            self.assertNotIn("stale_session", active)

            # Release and verify it becomes inactive
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()

            active_after = parse_presence(pdir)
            self.assertNotIn("held_session", active_after)

    def test_concurrent_presence_detection(self):
        """Verify concurrent multi-monitor scans do not cause false active sessions on stale lock files."""
        import concurrent.futures
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            lock_held = pdir / "active_real.lock"
            lock_held.touch()
            # Create several stale lock files
            for i in range(10):
                (pdir / f"stale_{i}.lock").touch()

            # Hold exclusive lock on the one real active session
            f = open(lock_held, "rb")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            try:
                def run_check():
                    return parse_presence(pdir)

                # Simulate 8 concurrent monitor/widget threads checking presence simultaneously
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    futures = [ex.submit(run_check) for _ in range(20)]
                    for fut in futures:
                        res = fut.result()
                        self.assertEqual(res, {"active_real"}, "Concurrent presence checks must not falsely report stale locks as active")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                f.close()

    def test_scan_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            cache_dir = base_dir / "cache"
            cache_dir.mkdir(parents=True)
            mock_quota = {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "name": "Weekly Limit Remaining",
                                "remaining_fraction": 0.85,
                                "reset_time": "2026-09-11T02:24:22Z"
                            }
                        ]
                    }
                ]
            }
            (cache_dir / "quota_usage_cache.json").write_text(json.dumps(mock_quota))

            data = scan(base_dir, force=False)

            self.assertEqual(data["schemaVersion"], 1)
            self.assertEqual(data["id"], "antigravity")
            self.assertIn("activeStatus", data)
            self.assertIn("todayPrompts", data)
            self.assertIn("todayTotalTokens", data)
            self.assertIn("weekTotalTokens", data)
            self.assertIn("totalTokens", data)
            self.assertIn("tokenGroups", data)
            self.assertIsInstance(data["tokenGroups"], list)
            self.assertIn("recentSessions", data)
            self.assertIsInstance(data["recentSessions"], list)
            self.assertIn("limits", data)
            self.assertIsInstance(data["limits"], list)
            self.assertIn("currentModel", data)
            self.assertEqual(data["currentModel"], "Gemini 3.8 Flash (High)")
            self.assertTrue(len(data["limits"]) > 0)
            self.assertIn("burnRatePerHour", data["limits"][0])
            self.assertIn("forecastText", data["limits"][0])
            self.assertIn("forecastStatus", data["limits"][0])
            # Verify recentDays entries include 'steps' and 'tokens' fields
            self.assertIn("recentDays", data)
            self.assertTrue(len(data["recentDays"]) > 0)
            for day_entry in data["recentDays"]:
                self.assertIn("steps", day_entry)
                self.assertIn("tokens", day_entry)
            self.assertIn("quotaUpdatedAt", data)
            self.assertIn("lastFullRefreshMs", data)
            self.assertTrue(data["lastFullRefreshMs"] > 0)

    def test_quota_notifications_cooldown_and_consolidation(self):
        from unittest.mock import patch
        import concurrent.futures
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            low_quota_groups = [
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {"id": "3p-weekly", "name": "Weekly Limit", "remainingPercent": 0},
                        {"id": "3p-5h", "name": "5-Hour Limit", "remainingPercent": 2},
                    ]
                }
            ]
            healthy_quota_groups = [
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {"id": "3p-weekly", "name": "Weekly Limit", "remainingPercent": 100},
                        {"id": "3p-5h", "name": "5-Hour Limit", "remainingPercent": 100},
                    ]
                }
            ]

            with patch("subprocess.run") as mock_run:
                # 1. First run: sends exactly 1 consolidated notification for the group
                check_and_send_quota_notifications(pdir, low_quota_groups, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 1)
                args, _ = mock_run.call_args
                cmd = args[0]
                self.assertEqual(cmd[0], "omarchy-notification-send")
                self.assertIn("0%", cmd[7])
                self.assertIn("Weekly Limit (0%)", cmd[8])
                self.assertIn("5-Hour Limit (2%)", cmd[8])

                # 2. Second run immediately after: does NOT send duplicate
                mock_run.reset_mock()
                check_and_send_quota_notifications(pdir, low_quota_groups, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 0)

                # 3. Third run later while quota remains at 0%: does NOT nag/resend
                check_and_send_quota_notifications(pdir, low_quota_groups, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 0)

                # 4. Quota replenishes back to 100%
                check_and_send_quota_notifications(pdir, healthy_quota_groups, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 0)

                # 5. Quota drops again in the future: sends 1 notification
                check_and_send_quota_notifications(pdir, low_quota_groups, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 1)

    def test_concurrent_quota_notifications(self):
        """Verify concurrent multi-monitor scans do not send duplicate notifications."""
        from unittest.mock import patch
        import concurrent.futures
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            low_quota_groups = [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {"id": "gemini-5h", "name": "5-Hour Limit", "remainingPercent": 3},
                    ]
                }
            ]

            with patch("subprocess.run") as mock_run:
                def run_notify():
                    check_and_send_quota_notifications(pdir, low_quota_groups, threshold_pct=15)

                # Simulate 8 concurrent monitor bar threads checking quota at the exact same moment
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    futures = [ex.submit(run_notify) for _ in range(16)]
                    for fut in futures:
                        fut.result()

                # Exactly ONE notification must be sent across all concurrent threads
                self.assertEqual(mock_run.call_count, 1)

    def test_token_usage_parsing(self):
        import json
        from datetime import datetime, timedelta
        from antigravity_usage_scanner import parse_transcripts

        with tempfile.TemporaryDirectory() as tmpdir:
            brain_dir = Path(tmpdir) / "brain"
            sess_dir = brain_dir / "session-1" / ".system_generated" / "logs"
            sess_dir.mkdir(parents=True)

            now = datetime.now().astimezone()
            today_str = now.strftime("%Y-%m-%d")
            three_days_ago = now - timedelta(days=3)
            ten_days_ago = now - timedelta(days=10)
            last_7_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]

            steps = [
                {
                    "step_index": 0,
                    "type": "GENERIC",
                    "created_at": now.isoformat(),
                    "content": "Changed `Model Selection` from `Default` to `Gemini 4 Argon (High)`. No need to acknowledge.",
                },
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "created_at": now.isoformat(),
                    "content": "Hello today",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "created_at": now.isoformat(),
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "cache_read_tokens": 5000,
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "created_at": three_days_ago.isoformat(),
                    "input_tokens": 800,
                    "output_tokens": 200,
                    "cache_read_tokens": 1000,
                },
                {
                    "step_index": 4,
                    "type": "GENERIC",
                    "created_at": ten_days_ago.isoformat(),
                    "content": "Changed `Model Selection` from `Gemini 4 Argon (High)` to `Claude Sonnet 4.6 (Thinking)`. No need to acknowledge.",
                },
                {
                    "step_index": 5,
                    "type": "PLANNER_RESPONSE",
                    "created_at": ten_days_ago.isoformat(),
                    "input_tokens": 400,
                    "output_tokens": 100,
                    "cache_read_tokens": 500,
                },
            ]
            tfile = sess_dir / "transcript.jsonl"
            with open(tfile, "w", encoding="utf-8") as f:
                for s in steps:
                    f.write(json.dumps(s) + "\n")

            session_stats = {}
            daily_stats = {
                d: {"tokens": 0, "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "steps": 0, "prompts": 0}
                for d in last_7_days
            }
            _, models_stats, model_list, _ = parse_transcripts(
                brain_dir, today_str, last_7_days, session_stats=session_stats, daily_stats=daily_stats
            )
            by_name = {m["name"]: m for m in model_list}

            m_today = by_name["Gemini 4 Argon (High)"]
            self.assertEqual(m_today["todayTokens"], 6500)
            self.assertEqual(m_today["todayInputTokens"], 1200)
            self.assertEqual(m_today["todayOutputTokens"], 300)
            self.assertEqual(m_today["todayCacheReadTokens"], 5000)
            self.assertEqual(m_today["weekTokens"], 8500)
            self.assertEqual(m_today["totalTokens"], 8500)

            m_old = by_name["Claude Sonnet 4.6 (Thinking)"]
            self.assertEqual(m_old["todayTokens"], 0)
            self.assertEqual(m_old["weekTokens"], 0)
            self.assertEqual(m_old["totalTokens"], 1000)

            self.assertEqual(daily_stats[today_str]["tokens"], 6500)
            self.assertIn("session-1", session_stats)
            self.assertEqual(session_stats["session-1"]["tokens"], 9500)
            self.assertEqual(session_stats["session-1"]["prompts"], 1)
            self.assertEqual(session_stats["session-1"]["steps"], 6)


if __name__ == "__main__":
    unittest.main()
