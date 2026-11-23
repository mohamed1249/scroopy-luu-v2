import subprocess
import unittest
from unittest.mock import MagicMock, patch
import launch


class LauncherTests(unittest.TestCase):
    @patch('launch.subprocess.run')
    @patch('launch.mongo_ready', return_value=True)
    def test_running_database_is_left_alone(self, ready, run):
        launch.ensure_mongodb('mongodb://127.0.0.1:27017/')
        run.assert_not_called()

    @patch('launch.os.name', 'nt')
    @patch('launch.subprocess.run', return_value=MagicMock(returncode=0))
    @patch('launch.mongo_ready', side_effect=[False, True])
    def test_starts_stopped_local_service(self, ready, run):
        launch.ensure_mongodb('mongodb://127.0.0.1:27017/')
        self.assertEqual(run.call_args.args[0], ['sc.exe', 'start', 'MongoDB'])

    @patch('launch.os.name', 'nt')
    @patch('launch.subprocess.run', return_value=MagicMock(returncode=5))
    @patch('launch.mongo_ready', return_value=False)
    def test_permission_denied_explains_manual_step(self, ready, run):
        with self.assertRaisesRegex(RuntimeError, 'administrator permission'):
            launch.ensure_mongodb('mongodb://localhost:27017/')

    @patch('launch.subprocess.run')
    @patch('launch.mongo_ready', return_value=False)
    def test_remote_database_never_starts_local_service(self, ready, run):
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            launch.ensure_mongodb('mongodb://db.example:27017/')
        run.assert_not_called()

    def test_cleanup_only_stops_owned_children(self):
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('child', 5), 0]
        launch.stop_children([child])
        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    @patch('launch.stop_children')
    @patch('launch.subprocess.Popen')
    @patch('launch.ensure_mongodb', side_effect=RuntimeError('not ready'))
    @patch('launch.check_ports')
    def test_failed_database_does_not_launch_apps(self, ports, mongo, popen, stop):
        self.assertEqual(launch.main(), 1)
        popen.assert_not_called()
        stop.assert_called_once_with([])

    @patch('launch.stop_children')
    @patch('launch.webbrowser.open')
    @patch('launch.time.sleep', side_effect=KeyboardInterrupt)
    @patch('launch.wait_for_service')
    @patch('launch.subprocess.Popen')
    @patch('launch.ensure_mongodb')
    @patch('launch.check_ports')
    def test_launcher_opens_ui_and_cleans_up_on_ctrl_c(self, ports, mongo, popen, wait, sleep, browser, stop):
        first, second = MagicMock(), MagicMock()
        first.poll.return_value = second.poll.return_value = None
        popen.side_effect = [first, second]
        self.assertEqual(launch.main(), 0)
        self.assertEqual(popen.call_count, 2)
        browser.assert_called_once_with('http://127.0.0.1:8501')
        stop.assert_called_once_with([first, second])

    @patch('launch.stop_children')
    @patch('launch.wait_for_service', side_effect=RuntimeError('startup failed'))
    @patch('launch.subprocess.Popen')
    @patch('launch.ensure_mongodb')
    @patch('launch.check_ports')
    def test_startup_failure_cleans_up_started_process(self, ports, mongo, popen, wait, stop):
        self.assertEqual(launch.main(), 1)
        popen.assert_called_once()
        stop.assert_called_once_with([popen.return_value])
