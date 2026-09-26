"""「谁在写盘」诊断的单元测试。

所有系统路径（/proc、/nas、容器目录）都指到临时目录，不碰真实机器。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import engine

MOUNTS = """\
/dev/root / erofs ro,relatime 0 0
tmpfs /tmp tmpfs rw,nosuid 0 0
devtmpfs /dev devtmpfs rw 0 0
/dev/mmcblk0p11 /log ext4 rw,relatime 0 0
/dev/md0 {root}{sep}sys_mnt btrfs rw,relatime,space_cache=v2 0 0
/dev/sda2 {root}{sep}pa0_mnt btrfs rw,relatime 0 0
/dev/sdb2 {root}{sep}pa1_mnt btrfs rw,relatime 0 0
"""

MDSTAT = """\
Personalities : [linear] [raid0] [raid1] 
md0 : active raid1 sdb1[1] sda1[0]
      20970368 blocks super 1.0 [2/2] [UU]
      bitmap: 0/1 pages [0KB], 65536KB chunk

unused devices: <none>
"""


def diskstats_line(major, minor, name, sectors_read, sectors_written):
    return '%4d %7d %-14s %8d %4d %10d %6d %8d %4d %10d %6d 0 0 0 0' % (
        major, minor, name, 100, 0, sectors_read, 0, 100, 0, sectors_written, 0)


class ActivityTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('sys_mnt', 'pa0_mnt', 'pa1_mnt'):
            (self.root / name).mkdir()
        self.sys_mnt = str(self.root / 'sys_mnt')
        self.pa0_mnt = str(self.root / 'pa0_mnt')
        self.pa1_mnt = str(self.root / 'pa1_mnt')
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.write_mounts()
        self.write_mdstat()
        self.write_diskstats({})
        self.patchers = [
            patch.object(engine, 'PROC_ROOT', self.proc),
            patch.object(engine, 'PROC_MOUNTS', self.proc / 'mounts'),
            patch.object(engine, 'PROC_MDSTAT', self.proc / 'mdstat'),
            patch.object(engine, 'PROC_DISKSTATS', self.proc / 'diskstats'),
            patch.object(engine, 'FINDEX_DIR', self.root / 'findex'),
            patch.object(engine, 'CONTAINER_DIR', self.root / 'containers'),
            patch.object(engine, 'PLUGIN_ROOT', self.root / 'plugins'),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reset_activity()
        self.addCleanup(self.reset_activity)

    def reset_activity(self):
        with engine._activity_lock:
            engine._activity_samples.clear()
            engine._activity_files.clear()
            engine._activity_writers.clear()
            engine._activity_events.clear()
            engine._activity_owners.clear()
            engine._activity_mountpoints.clear()
            engine._activity_writers_at = 0.0
            engine._activity_events_at = 0.0
            engine._activity_owners_at = 0.0
            engine._activity_viewers_at = 0.0

    def write_mounts(self):
        (self.proc / 'mounts').write_text(
            MOUNTS.format(root=self.root, sep=os.sep), encoding='utf-8')

    def write_mdstat(self, text=MDSTAT):
        (self.proc / 'mdstat').write_text(text, encoding='utf-8')

    def write_diskstats(self, values):
        lines = []
        for name, (major, minor, read, written) in values.items():
            lines.append(diskstats_line(major, minor, name, read, written))
        (self.proc / 'diskstats').write_text('\n'.join(lines) + '\n', encoding='utf-8')


class ProcParsingTests(ActivityTestCase):
    def test_read_mounts_skips_pseudo_filesystems(self):
        mounts = engine.read_mounts()
        by_point = {m['mountpoint']: m for m in mounts}
        self.assertIn(self.sys_mnt, by_point)
        self.assertIn(self.pa0_mnt, by_point)
        self.assertIn(self.pa1_mnt, by_point)
        self.assertIn('/log', by_point)                 # eMMC 也是块设备挂载
        self.assertNotIn('/tmp', by_point)
        self.assertNotIn('/', by_point)                 # erofs 只读根
        self.assertEqual(by_point[self.sys_mnt]['name'], 'md0')

    def test_read_mdstat_parses_members(self):
        arrays = engine.read_mdstat()
        self.assertEqual(arrays['md0']['level'], 'raid1')
        self.assertCountEqual(arrays['md0']['members'], ['sda1', 'sdb1'])

    def test_read_diskstats_parses_sector_columns(self):
        self.write_diskstats({'sda1': (8, 1, 1000, 2000), 'sdb2': (8, 18, 30, 40)})
        stats = engine.read_diskstats()
        self.assertEqual(stats['sda1'], {'read_sectors': 1000, 'write_sectors': 2000})
        self.assertEqual(stats['sdb2'], {'read_sectors': 30, 'write_sectors': 40})

    def test_hdd_classification(self):
        arrays = engine.read_mdstat()
        self.assertEqual(engine.physical_disk('sdb1'), 'sdb')
        self.assertEqual(engine.physical_disk('nvme0n1p2'), 'nvme0n1')
        self.assertEqual(engine.physical_disk('mmcblk0p11'), 'mmcblk0')
        self.assertCountEqual(engine.backing_physical('md0', arrays), ['sda', 'sdb'])
        self.assertEqual(engine.backing_physical('sda2', arrays), ['sda'])
        self.assertTrue(engine.is_hdd_backed('md0', arrays))
        self.assertTrue(engine.is_hdd_backed('sda2', arrays))
        self.assertFalse(engine.is_hdd_backed('mmcblk0p11', arrays))
        self.assertFalse(engine.is_hdd_backed('dm-0', arrays))

    def test_scan_files_respects_depth(self):
        deep = self.root / 'pa1_mnt' / 'u1' / 'data' / 'Cfg' / 'db'
        deep.mkdir(parents=True)
        (deep / 'jellyfin.db-shm').write_text('x', encoding='utf-8')
        (self.root / 'pa1_mnt' / 'top.txt').write_text('y', encoding='utf-8')
        shallow = engine.scan_files(str(self.root / 'pa1_mnt'), 2)
        self.assertIn(str(self.root / 'pa1_mnt' / 'top.txt'), shallow)
        self.assertNotIn(str(deep / 'jellyfin.db-shm'), shallow)
        everything = engine.scan_files(str(self.root / 'pa1_mnt'), 8)
        self.assertIn(str(deep / 'jellyfin.db-shm'), everything)


class RateSummaryTests(ActivityTestCase):
    def feed(self, first_written, second_written, seconds=10):
        self.write_diskstats({
            'sda1': (8, 1, 0, first_written),
            'sdb1': (8, 17, 0, first_written),
            'sda2': (8, 2, 0, 0),
            'sdb2': (8, 18, 0, 0),
            'md0': (9, 0, 0, first_written),
        })
        started = time.time() - seconds
        with engine._activity_lock:
            engine._activity_samples.clear()
            engine._activity_samples.append((started, engine.read_diskstats()))
        self.write_diskstats({
            'sda1': (8, 1, 0, second_written),
            'sdb1': (8, 17, 0, second_written),
            'sda2': (8, 2, 0, 0),
            'sdb2': (8, 18, 0, 0),
            'md0': (9, 0, 0, second_written),
        })
        with engine._activity_lock:
            engine._activity_samples.append((started + seconds, engine.read_diskstats()))

    def test_summary_reports_busy_mount(self):
        # 10 秒写 200 扇区 = 20 扇区/秒 = 10 KB/s
        self.feed(0, 200)
        summary = engine.write_rate_summary()
        self.assertTrue(summary['hasWrites'])
        self.assertIn(self.sys_mnt, summary['summary'])
        self.assertIn('10.0 KB/s', summary['summary'])
        self.assertFalse(summary['sampling'])

    def test_summary_is_quiet_without_writes(self):
        self.feed(500, 500)
        summary = engine.write_rate_summary()
        self.assertFalse(summary['hasWrites'])
        self.assertEqual(summary['summary'], '')

    def test_summary_needs_two_samples(self):
        with engine._activity_lock:
            engine._activity_samples.clear()
        self.assertTrue(engine.write_rate_summary()['sampling'])

    def test_snapshot_includes_activity(self):
        with patch.object(engine, 'run', return_value=type('R', (), {'returncode': 0, 'stdout': ''})()), \
                patch.object(engine, 'disk_summary', return_value=[]):
            data = engine.snapshot()
        self.assertIn('activity', data)
        self.assertIn('hasWrites', data['activity'])


class ActivitySnapshotTests(ActivityTestCase):
    def test_mirrored_raid_is_called_out(self):
        self.feed(0, 4000)                      # 每块成员盘同样多 → RAID1 镜像
        with patch.object(engine, 'refresh_writers'), patch.object(engine, 'refresh_events'):
            data = engine.activity_snapshot()
        self.assertEqual(len(data['mirrored']), 1)
        self.assertIn('md0', data['mirrored'][0])
        raid_row = next(row for row in data['mounts'] if row['mountpoint'] == self.sys_mnt)
        self.assertTrue(raid_row['mirrored'])
        self.assertTrue(raid_row['busy'])
        self.assertTrue(any('RAID1' in note for note in data['notes']))
        data_row = next(row for row in data['mounts'] if row['mountpoint'] == self.pa0_mnt)
        self.assertFalse(data_row['busy'])

    def test_uneven_members_is_not_mirroring(self):
        self.write_diskstats({'sda1': (8, 1, 0, 4000), 'sdb1': (8, 17, 0, 100), 'md0': (9, 0, 0, 100)})
        started = time.time() - 10
        with engine._activity_lock:
            engine._activity_samples.clear()
            engine._activity_samples.append((started, engine.read_diskstats()))
        self.write_diskstats({'sda1': (8, 1, 0, 8000), 'sdb1': (8, 17, 0, 200), 'md0': (9, 0, 0, 200)})
        with engine._activity_lock:
            engine._activity_samples.append((started + 10, engine.read_diskstats()))
        with patch.object(engine, 'refresh_writers'), patch.object(engine, 'refresh_events'):
            data = engine.activity_snapshot()
        self.assertEqual(data['mirrored'], [])

    def test_viewer_marker_controls_scanning(self):
        self.assertFalse(engine.viewer_active())
        engine.mark_viewer()
        self.assertTrue(engine.viewer_active())

    def feed(self, first, second, seconds=10.0):
        self.write_diskstats({
            'sda1': (8, 1, 0, first), 'sdb1': (8, 17, 0, first),
            'sda2': (8, 2, 0, 0), 'sdb2': (8, 18, 0, 0), 'md0': (9, 0, 0, first),
        })
        started = time.time() - seconds
        with engine._activity_lock:
            engine._activity_samples.clear()
            engine._activity_samples.append((started, engine.read_diskstats()))
        self.write_diskstats({
            'sda1': (8, 1, 0, second), 'sdb1': (8, 17, 0, second),
            'sda2': (8, 2, 0, 0), 'sdb2': (8, 18, 0, 0), 'md0': (9, 0, 0, second),
        })
        with engine._activity_lock:
            engine._activity_samples.append((started + seconds, engine.read_diskstats()))


class WriterScanTests(ActivityTestCase):
    def test_scan_reports_changed_and_held_files(self):
        target = self.root / 'sys_mnt' / 'raw_event_log.db-wal'
        target.write_text('a', encoding='utf-8')
        engine.refresh_writers(force=True)              # 第一次只建立基线
        with engine._activity_lock:
            self.assertEqual(engine._activity_writers, [])
        target.write_text('abcdef', encoding='utf-8')
        engine.refresh_writers(force=True)
        with engine._activity_lock:
            writers = [dict(item) for item in engine._activity_writers]
        self.assertEqual(len(writers), 1)
        self.assertEqual(writers[0]['path'], str(target))
        self.assertEqual(writers[0]['delta'], 5)
        self.assertEqual(writers[0]['kind'], 'mod')

    def test_scan_is_throttled_without_force(self):
        engine.refresh_writers(force=True)
        with engine._activity_lock:
            first = engine._activity_writers_at
        engine.refresh_writers()
        with engine._activity_lock:
            self.assertEqual(engine._activity_writers_at, first)


class OwnerLookupTests(ActivityTestCase):
    def make_container(self, name, source, target='/config', running=True):
        folder = self.root / 'containers' / name
        folder.mkdir(parents=True)
        config = {
            'Name': '/' + name,
            'Config': {'Image': 'jellyfin/jellyfin:latest@sha256:abc'},
            'State': {'Running': running},
            'MountPoints': {target: {'Source': source}},
        }
        (folder / 'config.v2.json').write_text(json.dumps(config), encoding='utf-8')

    def test_owner_matches_underlying_mount_via_pool_alias(self):
        """容器挂载写的是 cfs 聚合层路径，文件实际落在底层挂载点。"""
        self.make_container('xiaomi-plugin-jellyfin', '/nas/pool0/u1/下载/JellyfinConfig')
        engine.refresh_owners(force=True)
        underlying = self.pa1_mnt + '/u1/下载/JellyfinConfig/data/jellyfin.db-shm'
        owner = engine.owner_for(underlying)
        self.assertIn('xiaomi-plugin-jellyfin', owner)
        self.assertIn('jellyfin/jellyfin:latest', owner)

    def test_owner_matches_direct_path(self):
        self.make_container('xiaomi-plugin-emby', self.pa0_mnt + os.sep + 'EmbyConfig')
        engine.refresh_owners(force=True)
        owner = engine.owner_for(self.pa0_mnt + os.sep + 'EmbyConfig' + os.sep + 'data' + os.sep + 'emby.db')
        self.assertIn('xiaomi-plugin-emby', owner)
        self.assertIn('运行中', owner)

    def test_owner_is_empty_for_unrelated_path(self):
        engine.refresh_owners(force=True)
        self.assertEqual(engine.owner_for('/nas/sys/findex/raw_event_log.db'), '')

    def test_stopped_container_is_labelled(self):
        self.make_container('xiaomi-plugin-emby', self.pa0_mnt + os.sep + 'EmbyConfig', running=False)
        engine.refresh_owners(force=True)
        self.assertIn('已停止', engine.owner_for(self.pa0_mnt + os.sep + 'EmbyConfig' + os.sep + 'x'))

    def test_plugin_labels_from_meta(self):
        meta_dir = self.root / 'plugins' / 'jellyfin' / 'current'
        meta_dir.mkdir(parents=True)
        (meta_dir / 'plugin-meta.json').write_text(json.dumps({'name': 'Jellyfin'}), encoding='utf-8')
        self.assertEqual(engine.plugin_labels(), {'jellyfin': 'Jellyfin'})


class EventLogTests(ActivityTestCase):
    def write_event_db(self, db_name, table, rows):
        folder = self.root / 'findex'
        folder.mkdir(exist_ok=True)
        connection = sqlite3.connect(str(folder / db_name))
        connection.execute('create table "%s" (event_time_ms integer, parent_path text, name text)' % table)
        connection.executemany('insert into "%s" values (?, ?, ?)' % table, rows)
        connection.commit()
        connection.close()

    def test_reads_paths_and_counts(self):
        self.write_event_db('raw_event_log.db', 'raw_event_log', [
            (100, '/nas/mnt/pa1/u1/Cfg/data', 'jellyfin.db-wal'),
            (200, '/nas/mnt/pa1/u1/Cfg/data', 'jellyfin.db-wal'),
            (300, '/nas/mnt/pa1/u1/Cfg/data', 'jellyfin.db-shm'),
        ])
        counts, note = engine.read_event_db('raw_event_log.db', 'raw_event_log')
        self.assertEqual(note, '')
        self.assertEqual(counts['/nas/mnt/pa1/u1/Cfg/data/jellyfin.db-wal'], 2)
        self.assertEqual(counts['/nas/mnt/pa1/u1/Cfg/data/jellyfin.db-shm'], 1)

    def test_missing_db_is_reported_not_raised(self):
        counts, note = engine.read_event_db('raw_event_log.db', 'raw_event_log')
        self.assertEqual(counts, {})
        self.assertIn('未找到', note)

    def test_refresh_events_merges_and_sorts(self):
        self.write_event_db('raw_event_log.db', 'raw_event_log', [
            (100, '/nas/sys/findex', 'a.db-wal'),
            (200, '/nas/sys/findex', 'a.db-wal'),
            (300, '/nas/sys/findex', 'b.db-wal'),
        ])
        self.write_event_db('normalized_event_log.db', 'normalized_event_log', [
            (100, '/nas/sys/findex', 'a.db-wal'),
        ])
        engine.refresh_events(force=True)
        with engine._activity_lock:
            events = [dict(item) for item in engine._activity_events]
        self.assertEqual(events[0]['path'], '/nas/sys/findex/a.db-wal')
        self.assertEqual(events[0]['count'], 2)

    def test_snapshot_decorates_events_with_owner(self):
        self.make_container_fixture()
        self.write_event_db('raw_event_log.db', 'raw_event_log', [
            (100, self.pa1_mnt + os.sep + 'u1' + os.sep + 'Cfg', 'jellyfin.db-shm'),
        ])
        with patch.object(engine, 'refresh_writers'):
            data = engine.activity_snapshot()
        self.assertTrue(data['events'])
        self.assertIn('xiaomi-plugin-jellyfin', data['events'][0]['owner'])

    def make_container_fixture(self):
        folder = self.root / 'containers' / 'xiaomi-plugin-jellyfin'
        folder.mkdir(parents=True, exist_ok=True)
        config = {
            'Name': '/xiaomi-plugin-jellyfin',
            'Config': {'Image': 'jellyfin/jellyfin:latest'},
            'State': {'Running': True},
            'MountPoints': {'/config': {'Source': self.pa1_mnt + os.sep + 'u1' + os.sep + 'Cfg'}},
        }
        (folder / 'config.v2.json').write_text(json.dumps(config), encoding='utf-8')


class VersionTests(unittest.TestCase):
    def test_installed_version_falls_back_in_source_tree(self):
        self.assertEqual(engine.installed_version(), engine.VERSION)

    def test_installed_version_reads_release_dir(self):
        with patch.object(engine, '__file__',
                          '/data/plugin/disk-sleep/releases/0.1.2-1790354112-1647038/engine.py'):
            self.assertEqual(engine.installed_version(), '0.1.2')


if __name__ == '__main__':
    unittest.main()
