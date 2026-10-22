"""Alert mail and watchdog heartbeat tests. No network or credentials."""
import json
import tempfile
import unittest
from pathlib import Path

from info_hub.health import write_heartbeat
from tools.alert_mail import build_message, load_request

TMP = Path(__file__).resolve().parents[1] / 'data' / 'test-tmp'
TMP.mkdir(parents=True, exist_ok=True)


class AlertMailTests(unittest.TestCase):
    def test_request_and_readonly_message(self):
        with tempfile.TemporaryDirectory(dir=TMP) as folder:
            path = Path(folder) / 'request.json'
            path.write_text(
                json.dumps({'subject': '[info-hub] 异常：QQ 离线', 'body': '第一行\n第二行'},
                           ensure_ascii=False),
                encoding='utf-8',
            )
            subject, body = load_request(path)
            msg = build_message(
                sender='sender@example.com',
                sender_name='info-hub',
                recipient='recipient@example.com',
                subject=subject,
                body=body,
            )
            self.assertEqual(msg['X-InfoHub-Alert'], '1')
            self.assertEqual(msg['X-InfoHub-ReadOnly'], '1')
            self.assertEqual(msg['To'], 'recipient@example.com')
            self.assertIn('第二行', msg.get_content())

    def test_request_rejects_empty_and_multiline_subject(self):
        with tempfile.TemporaryDirectory(dir=TMP) as folder:
            path = Path(folder) / 'request.json'
            for payload in ({'subject': '', 'body': 'x'},
                            {'subject': 'a\nb', 'body': 'x'},
                            {'subject': 'x', 'body': ''}):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        load_request(path)

    def test_heartbeat_is_atomic_and_complete(self):
        with tempfile.TemporaryDirectory(dir=TMP) as folder:
            path = Path(folder) / 'heartbeat.json'
            write_heartbeat(path, True)
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(data['ok'])
            self.assertFalse(path.with_name('heartbeat.json.tmp').exists())
            self.assertIsInstance(data['pid'], int)
            self.assertIsInstance(data['updated'], int)

    def test_guard_checks_process_online_and_heartbeat(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / 'infohub_login_guard.ps1').read_text(encoding='utf-8')
        for token in ('Get-BotProcess', 'Get-Heartbeat', 'Invoke-AlertCheck',
                      'tools\\alert_mail.py', 'data\\PAUSED', 'heartbeat.json'):
            with self.subTest(token=token):
                self.assertIn(token, text)


if __name__ == '__main__':
    unittest.main()
