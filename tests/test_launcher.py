"""启动器仅识别本工具进程，禁止停止同端口的其他服务。"""
import importlib.util
import unittest
import tempfile
from unittest.mock import patch, Mock
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def test_process_identity_requires_exact_script_path(self):
        self.assertIsNotNone(importlib.util.find_spec('launcher'), '需要独立启动器')
        from launcher import owns_command, ROOT
        self.assertTrue(owns_command([str(ROOT/'.venv'/'Scripts'/'python.exe'), str(ROOT/'app.py'), '--no-browser']))
        self.assertFalse(owns_command(['python.exe','D:/elsewhere/app.py']))
        self.assertFalse(owns_command(['python.exe','-c',str(ROOT/'app.py')]))

    def test_failed_start_does_not_overwrite_existing_pid(self):
        import launcher
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python = root/'.venv'/'Scripts'/'python.exe'
            python.parent.mkdir(parents=True)
            python.touch()
            (root/'data').mkdir()
            record = root/'data'/'server.json'
            record.write_text('{"pid":100,"created":1}',encoding='utf-8')
            failed = Mock(pid=200)
            failed.poll.return_value = 1
            with patch.object(launcher,'ROOT',root), patch.object(launcher,'PID_FILE',record), \
                 patch.object(launcher,'health',return_value=None), \
                 patch('launcher.subprocess.Popen',return_value=failed), \
                 patch('psutil.Process') as process:
                process.return_value.create_time.return_value=2
                with self.assertRaises(ValueError):
                    launcher.start()
            self.assertEqual(record.read_text(encoding='utf-8'),'{"pid":100,"created":1}')

    def test_start_timeout_terminates_unrecorded_process(self):
        import launcher
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python=root/'.venv'/'Scripts'/'python.exe'
            python.parent.mkdir(parents=True)
            python.touch()
            (root/'data').mkdir()
            process=Mock(pid=200)
            process.poll.return_value=None
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'PID_FILE',root/'data'/'server.json'), \
                 patch.object(launcher,'health',return_value=None),patch('launcher.subprocess.Popen',return_value=process), \
                 patch('launcher.time.sleep'):
                with self.assertRaises(ValueError):
                    launcher.start()
            process.terminate.assert_called_once()
            process.wait.assert_called()


if __name__ == '__main__':
    unittest.main()
