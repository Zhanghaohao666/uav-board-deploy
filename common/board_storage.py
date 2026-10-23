#!/usr/bin/env python3
"""Prepare a separate data filesystem and bounded system logging on fresh boards.

No partitioning/formatting, no camera restarts and no deletion of existing logs.
The caller owns the surrounding deployment transaction and must call rollback()
with the returned receipt if a later installation step fails.
"""
import datetime
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

SYSTEM_ROOT = Path('/')
MIN_FREE = 10 * 1024**3
STAMP_FORMAT = '%Y%m%d_%H%M%S_%f'


def system(path):
    return SYSTEM_ROOT / path.lstrip('/')


def run(argv, check=True, timeout=45):
    return subprocess.run(argv, check=check, timeout=timeout, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _flatten(rows):
    for row in rows:
        yield row
        yield from _flatten(row.get('children', []))


def available_partitions():
    """Read-only discovery for the caller's explicit data-UUID selection."""
    result = run(['lsblk', '-J', '-p', '-o', 'NAME,TYPE,FSTYPE,UUID,MOUNTPOINTS,RO,SIZE'])
    rows = list(_flatten(json.loads(result.stdout)['blockdevices']))
    uuid_counts = {}
    for row in rows:
        if row.get('uuid'):
            value = row['uuid'].lower()
            uuid_counts[value] = uuid_counts.get(value, 0) + 1
    return [dict(device=r['name'], uuid=r['uuid'], size=r.get('size', ''), fstype='ext4')
            for r in rows if r.get('type') in ('part', 'disk') and r.get('fstype') == 'ext4'
            and r.get('uuid') and uuid_counts[r['uuid'].lower()] == 1
            and not r.get('ro') and not any(r.get('mountpoints') or [])]


def _mount_record(path):
    result = run(['findmnt', '-J', '-T', str(path), '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'], check=False)
    if result.returncode:
        return None
    rows = list(_flatten(json.loads(result.stdout).get('filesystems', [])))
    return next((r for r in rows if r.get('target') == str(path)), None)


def _data_ready(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir() or not os.path.ismount(path):
        raise ValueError('需要已挂载的独立数据盘：' + str(path))
    if path.stat().st_dev == system('/').stat().st_dev:
        raise ValueError('数据目录仍在系统盘，拒绝写入：' + str(path))
    mount = _mount_record(path)
    if not mount or mount.get('fstype') not in ('ext4', 'xfs', 'btrfs'):
        raise ValueError('数据盘必须为可识别的 Linux 本地文件系统')
    if 'rw' not in mount.get('options', '').split(','):
        raise ValueError('数据盘为只读挂载')
    free = shutil.disk_usage(path).free
    if free < MIN_FREE:
        raise ValueError('数据盘可用空间不足 10 GiB')
    return dict(mount=mount, free_bytes=free)


def _choose_root(role, data_root):
    if role not in ('main', 'algorithm'):
        raise ValueError('role 必须为 main 或 algorithm')
    allowed = ('/data',) if role == 'main' else ('/mnt/data', '/data')
    if data_root is not None and data_root not in allowed:
        raise ValueError('主控使用 /data；算法板使用 /mnt/data 或 /data')
    if data_root:
        return data_root
    for name in allowed:
        if os.path.ismount(system(name)):
            return name
    return allowed[0]


def _mount_unit(root, uuid, fstype='ext4'):
    unit = root.strip('/').replace('/', '-') + '.mount'
    text = '''[Unit]
Description=UAV explicitly selected data filesystem
Before=var-log.mount

[Mount]
What=/dev/disk/by-uuid/%s
Where=%s
Type=%s
Options=rw,noatime
TimeoutSec=30

[Install]
WantedBy=local-fs.target
''' % (uuid, root, fstype)
    return unit, text


def _existing_mount_persistence(root, ready):
    """Return an additive persistence change only when no persistent owner exists."""
    mount = ready['mount']
    source = mount['source'].split('[', 1)[0]
    uuid = run(['findmnt', '-n', '-T', str(system(root)), '-o', 'UUID']).stdout.strip()

    def same_source(candidate):
        if candidate.startswith('UUID='):
            return bool(uuid) and candidate[5:].lower() == uuid.lower()
        return candidate.startswith('/dev/') and os.path.realpath(candidate) == os.path.realpath(source)

    fstab = system('/etc/fstab')
    if fstab.exists():
        rows = [line.split('#', 1)[0].split() for line in fstab.read_text().splitlines()]
        selected = [row for row in rows if len(row) >= 4 and row[1] == root]
        if selected:
            if len(selected) != 1 or not same_source(selected[0][0]) or selected[0][2] != mount['fstype'] or 'noauto' in selected[0][3].split(','):
                raise ValueError('现有 fstab 数据盘配置与实际挂载不匹配或未设自动挂载：' + root)
            return None
    unit = root.strip('/').replace('/', '-') + '.mount'
    result = run(['systemctl', 'show', unit, '-p', 'FragmentPath', '-p', 'What', '-p', 'Where', '-p', 'Type'], check=False)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    fragment = fields.get('FragmentPath', '')
    if fragment:
        if fragment.startswith('/run/') or fields.get('Where') != root or fields.get('Type') != mount['fstype'] or not same_source(fields.get('What', '')):
            raise ValueError('现有数据盘 mount unit 不是匹配的永久配置：' + unit)
        if run(['systemctl', 'is-enabled', unit], check=False).returncode == 0:
            return None
        return dict(unit=unit, create=False, enable=True)
    unit_path = system('/etc/systemd/system/' + unit)
    if unit_path.exists() or unit_path.is_symlink():
        raise ValueError('数据盘 mount unit 尚未加载或已屏蔽，拒绝覆盖：' + unit)
    if not re.fullmatch(r'[a-fA-F0-9-]{8,64}', uuid):
        raise ValueError('已挂载数据盘没有可用 UUID，无法安全建立开机挂载')
    rows = list(_flatten(json.loads(run(['lsblk', '-J', '-p', '-o', 'NAME,UUID']).stdout)['blockdevices']))
    if sum(str(r.get('uuid', '')).lower() == uuid.lower() for r in rows) != 1:
        raise ValueError('数据盘 UUID 不唯一，拒绝创建可能挂错盘的开机配置')
    _, content = _mount_unit(root, uuid, mount['fstype'])
    return dict(unit=unit, content=content, create=True, enable=True)


def _selected_partition(uuid):
    if not isinstance(uuid, str) or not re.fullmatch(r'[a-fA-F0-9-]{8,64}', uuid):
        raise ValueError('数据盘 UUID 格式无效')
    matches = [r for r in available_partitions() if r['uuid'].lower() == uuid.lower()]
    if len(matches) != 1:
        raise ValueError('UUID 必须唯一对应未挂载、可写的现有 ext4 数据分区；不会格式化')
    device = Path(matches[0]['device'])
    if not device.exists() or not stat.S_ISBLK(device.stat().st_mode):
        raise ValueError('数据盘不是可用块设备')
    return matches[0]


def _log_files(role, root):
    destination = root + '/uav-system-logs/' + role
    dependencies = '''[Unit]
Requires=var-log.mount
After=var-log.mount
ConditionPathIsMountPoint=/var/log
'''
    files = {
        '/etc/systemd/system/var-log.mount': '''[Unit]
Description=Store UAV system logs on the data filesystem
RequiresMountsFor=%s
ConditionPathIsMountPoint=%s
Before=systemd-journal-flush.service systemd-tmpfiles-setup.service rsyslog.service

[Mount]
What=%s
Where=/var/log
Type=none
Options=bind
TimeoutSec=30

[Install]
WantedBy=local-fs.target
''' % (destination, root, destination),
        '/etc/systemd/journald.conf.d/90-uav-data-log.conf': '''[Journal]
Storage=auto
Compress=yes
SystemMaxUse=512M
SystemKeepFree=2G
SystemMaxFileSize=32M
SystemMaxFiles=32
RuntimeMaxUse=64M
RuntimeKeepFree=128M
MaxRetentionSec=7day
MaxFileSec=1day
RateLimitIntervalSec=30s
RateLimitBurst=1000
ForwardToSyslog=yes
''',
        '/etc/systemd/system/rsyslog.service.d/90-uav-data-log.conf': dependencies,
        '/etc/systemd/system/systemd-journal-flush.service.d/90-uav-data-log.conf': dependencies,
        '/etc/systemd/system/logrotate.timer.d/90-uav-data-log.conf': '''[Timer]
OnCalendar=
OnCalendar=*-*-* *:0/10:00
AccuracySec=1m
RandomizedDelaySec=0
Persistent=true
''',
        '/etc/systemd/system/logrotate.service.d/90-uav-data-log.conf': '''[Unit]
ConditionACPower=
ConditionPathIsMountPoint=/var/log
''',
        '/etc/logrotate.d/rsyslog': '''/var/log/syslog
/var/log/mail.info
/var/log/mail.warn
/var/log/mail.err
/var/log/mail.log
/var/log/daemon.log
/var/log/kern.log
/var/log/auth.log
/var/log/user.log
/var/log/lpr.log
/var/log/cron.log
/var/log/debug
/var/log/messages
{
    daily
    maxsize 20M
    rotate 7
    maxage 7
    missingok
    notifempty
    compress
    delaycompress
    create 0640 syslog adm
    sharedscripts
    postrotate
        /usr/lib/rsyslog/rsyslog-rotate
    endscript
}
''',
    }
    return destination, files


def _existing_logs(root):
    """Recognize the earlier field deployment without changing its live setup."""
    varlog = system('/var/log')
    mount = system('/etc/systemd/system/var-log.mount')
    if not os.path.ismount(varlog):
        if mount.exists() or mount.is_symlink():
            raise ValueError('已有未生效的 var-log.mount，请先核对，未覆盖')
        return None
    if varlog.stat().st_dev != system(root).stat().st_dev:
        raise ValueError('/var/log 已挂载到其他盘，未覆盖')
    journal = system('/etc/systemd/journald.conf.d/90-uav-data-log.conf')
    timer = system('/etc/systemd/system/logrotate.timer.d/90-uav-data-log.conf')
    rotate = system('/etc/logrotate.d/rsyslog')
    for p in (mount, journal, timer, rotate):
        if p.is_symlink() or not p.is_file():
            raise ValueError('已有日志方案无法确认为受限数据盘配置：' + str(p))
    content = journal.read_text()
    for setting in ('Storage=auto', 'SystemMaxUse=512M', 'RuntimeMaxUse=64M'):
        if setting not in content.splitlines():
            raise ValueError('现有 journald 容量配置不同，请先核对：' + setting)
    if 'OnCalendar=*-*-* *:0/10:00' not in timer.read_text().splitlines():
        raise ValueError('现有日志轮转周期不同，未覆盖')
    if not re.search(r'^\s*maxsize\s+20M\s*$', rotate.read_text(), re.M) or not re.search(r'^\s*rotate\s+7\s*$', rotate.read_text(), re.M):
        raise ValueError('现有 syslog 容量限制不同，未覆盖')
    match = re.search(r'^What=(.+)$', mount.read_text(), re.M)
    if not match or not match.group(1).startswith(root + '/') or not system(match.group(1)).is_dir() or not os.path.samefile(varlog, system(match.group(1))):
        raise ValueError('现有日志挂载来源与配置不一致')
    if run(['systemctl', 'is-enabled', 'var-log.mount'], check=False).returncode:
        raise ValueError('现有日志挂载未配置开机启用')
    return dict(reused=True, root=match.group(1))


def fan_status():
    """Only inspect known services and PWM; never write an unknown controller."""
    units = {}
    for name in ('fan-control.service', 'fan-on.service'):
        r = run(['systemctl', 'show', name, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'UnitFileState'], check=False)
        units[name] = dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)
    pwms = {}
    for channel in system('/sys/class/pwm').glob('pwmchip*/pwm[0-9]*'):
        if not channel.is_dir():
            continue
        values = {}
        for name in ('enable', 'period', 'duty_cycle', 'polarity'):
            try:
                values[name] = (channel / name).read_text().strip()
            except OSError:
                pass
        pwms[str(channel)] = values
    return dict(services=units, pwm=pwms, note='仅检查软件状态；无转速反馈时需观察风扇实际转动')


def preflight(role, data_root=None, data_uuid=None):
    """Read-only plan; unformatted disks and implicit rootfs fallback are refused."""
    root = _choose_root(role, data_root)
    path = system(root)
    if path.is_symlink():
        raise ValueError('数据挂载点不能是符号链接')
    plan = dict(version=1, role=role, data_root=root, data_uuid=data_uuid,
                mount=None, persistence=None, existing_logs=None)
    if os.path.ismount(path):
        ready = _data_ready(path)
        if data_uuid:
            r = run(['findmnt', '-n', '-T', str(path), '-o', 'UUID'])
            if r.stdout.strip().lower() != data_uuid.lower():
                raise ValueError('已挂载数据盘的 UUID 与指定值不同')
        plan['data'] = ready
        plan['persistence'] = _existing_mount_persistence(root, ready)
        plan['existing_logs'] = _existing_logs(root)
    else:
        if not data_uuid:
            raise ValueError('尚无独立数据盘挂载：%s。请明确选择已有 ext4 分区 UUID；本工具不会格式化，也不会写入系统盘。' % root)
        partition = _selected_partition(data_uuid)
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValueError('数据挂载点不为空，拒绝遮盖原文件：' + root)
        unit, content = _mount_unit(root, partition['uuid'])
        if system('/etc/systemd/system/' + unit).exists():
            raise ValueError('已有数据盘 mount unit，未覆盖：' + unit)
        fstab = system('/etc/fstab')
        if fstab.exists():
            for line in fstab.read_text().splitlines():
                fields = line.split('#', 1)[0].split()
                if len(fields) > 1 and fields[1] == root:
                    raise ValueError('fstab 已有此挂载点配置，请先核对')
        if system('/etc/systemd/system/var-log.mount').exists() or os.path.ismount(system('/var/log')):
            raise ValueError('未挂载数据盘但已有日志挂载配置，请先核对')
        plan['mount'] = dict(unit=unit, content=content, partition=partition)
    destination, files = _log_files(role, root)
    plan['log_root'] = destination
    plan['files'] = files
    if not plan['existing_logs']:
        if system(destination).exists():
            raise ValueError('目标日志目录已存在，拒绝覆盖：' + destination)
        varlog = system('/var/log')
        if varlog.is_symlink() or not varlog.is_dir():
            raise ValueError('/var/log 必须为普通目录')
        for binary in ('rsync', 'logrotate', 'logger', 'journalctl', 'systemd-analyze'):
            if not shutil.which(binary):
                raise ValueError('缺少日志部署工具：' + binary)
        for unit in ('systemd-journald.service', 'rsyslog.service'):
            if run(['systemctl', 'is-active', unit], check=False).returncode:
                raise ValueError('日志服务未运行：' + unit)
        if not system('/usr/lib/rsyslog/rsyslog-rotate').is_file():
            raise ValueError('缺少 rsyslog 标准轮转脚本')
        for name in files:
            p = system(name)
            if p.is_symlink():
                raise ValueError('拒绝覆盖符号链接配置：' + name)
            if p.exists() and name != '/etc/logrotate.d/rsyslog':
                raise ValueError('已有同名日志配置，未覆盖：' + name)
        for name in ('journal', 'syslog', 'kern.log', 'auth.log'):
            if (varlog / name).is_symlink():
                raise ValueError('现有日志文件是符号链接，需先核对：' + name)
    plan['fan'] = fan_status()
    return plan


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.uav-new')
    created = False
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            created = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def _save_file(receipt, name, content):
    path = system(name)
    if path.is_symlink():
        raise ValueError('配置路径变为符号链接：' + name)
    backup = None
    if path.exists():
        saved = Path(receipt['backup']) / 'files' / name.lstrip('/')
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, saved)
        backup = str(saved)
    _write(path, content)
    receipt['changed_files'].append(dict(path=name, backup=backup,
                                        sha256=hashlib.sha256(content.encode()).hexdigest()))


def _verify_logging(plan):
    varlog = system('/var/log')
    if not os.path.ismount(varlog) or not os.path.samefile(varlog, system(plan['log_root'])):
        raise ValueError('日志未挂载到指定数据盘目录')
    marker = 'uav-storage-verified-' + datetime.datetime.now().strftime(STAMP_FORMAT)
    run(['logger', '-t', 'uav-storage', marker])
    run(['journalctl', '--sync'])
    # logger, journald and rsyslog communicate asynchronously: wait a bounded
    # interval for both persistent destinations rather than race their queues.
    for _ in range(20):
        journal = run(['journalctl', '-t', 'uav-storage', '-n', '5', '-o', 'cat', '--no-pager'])
        try:
            with (system(plan['log_root']) / 'syslog').open('rb') as f:
                f.seek(max(0, os.fstat(f.fileno()).st_size - 1024 * 1024))
                syslog_ok = marker.encode() in f.read()
        except FileNotFoundError:
            syslog_ok = False
        if marker in journal.stdout and syslog_ok:
            return
        time.sleep(0.2)
    raise ValueError('journald/syslog 数据盘写入验证失败')


def _root_log_residue(receipt):
    """Bound text logs kept open by desktop/other processes across migration."""
    shadow = system(receipt['shadow'])
    found = {}
    for descriptor in system('/proc').glob('[0-9]*/fd/*'):
        try:
            target = os.readlink(descriptor)
            path = Path(target)
            if not target.startswith(str(shadow) + '/') or target.endswith(' (deleted)') or path.is_symlink():
                continue
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                continue
            found[target] = info.st_size
        except OSError:
            continue
    if not found:
        receipt['root_log_residue'] = []
        return
    config = '/etc/logrotate.d/uav-root-log-residue'
    if system(config).exists() or system(config).is_symlink():
        raise ValueError('已有系统盘残留日志轮转配置，拒绝覆盖')
    stanzas = []
    report = []
    for target, size in sorted(found.items()):
        path = Path(target)
        record = dict(path=target, size=size, managed=False)
        known = (path.name.endswith(('.log', '.txt', '_log')) or path.name in
                 ('syslog', 'debug', 'messages', 'dmesg'))
        if not known or any(ch in target for ch in '\n\r'):
            record['note'] = '未自动轮转未知/非文本日志；保留原文件，请检查或重启其所属程序'
        else:
            with path.open('rb') as f:
                text_log = b'\0' not in f.read(8192)
            if not text_log:
                record['note'] = '检测到二进制内容，保留原文件'
            else:
                # Each path gets its own archive directory to prevent basename
                # collisions, e.g. two different processes both writing app.log.
                archive = system(receipt['log_root']) / 'migration-residue' / hashlib.sha256(target.encode()).hexdigest()[:16]
                archive.mkdir(parents=True, mode=0o755)
                quoted = '"' + target.replace('\\', '\\\\').replace('"', '\\"') + '"'
                stanzas.append('''%s
{
    size 1M
    rotate 2
    maxage 7
    missingok
    notifempty
    copytruncate
    compress
    su root root
    olddir %s
}
''' % (quoted, archive))
                record.update(managed=True, archive=str(archive), maxsize='1M', retained=2)
        report.append(record)
    if stanzas:
        _save_file(receipt, config, '\n'.join(stanzas))
        run(['logrotate', '-d', str(system('/etc/logrotate.conf'))])
    receipt['root_log_residue'] = report
    unbounded = [r['path'] for r in report if not r['managed']]
    if unbounded:
        print('仍有程序持有原系统盘日志（未删除），需复核：' + ', '.join(unbounded), flush=True)


def apply(plan, start=True):
    """Apply a fresh preflight plan and return a rollback receipt.

    Storage/logging are always made effective immediately. start=False refers
    to application services in the caller and does not defer storage mounts.
    """
    if os.geteuid() != 0:
        raise ValueError('存储部署需要 sudo')
    current = preflight(plan['role'], plan['data_root'], plan.get('data_uuid'))
    if current['existing_logs'] and not current['persistence']:
        return dict(version=1, reused=True, data_root=current['data_root'],
                    log_root=current['existing_logs']['root'], changed_files=[], enabled=[])
    plan = current
    stamp = datetime.datetime.now().strftime(STAMP_FORMAT)
    backup = system('/var/backups/uav-storage') / stamp
    backup.mkdir(parents=True, mode=0o700)
    receipt = dict(version=1, reused=False, backup=str(backup), data_root=plan['data_root'],
                   log_root=plan['log_root'], changed_files=[], enabled=[],
                   mounted_data=False, renamed_logs=False, shadow='/var/log.uav-before-data_' + stamp,
                   varlog_mounted=False, journal_relinquished=False, data_unit=None)
    try:
        if plan['mount']:
            mount = plan['mount']
            # Re-discovery in preflight ensures the chosen UUID remains unused.
            system(plan['data_root']).mkdir(parents=True, exist_ok=True)
            receipt['data_unit'] = mount['unit']
            _save_file(receipt, '/etc/systemd/system/' + mount['unit'], mount['content'])
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'start', mount['unit']])
            receipt['mounted_data'] = True
            _data_ready(system(plan['data_root']))
            receipt['enabled'].append(mount['unit'])
            run(['systemctl', 'enable', mount['unit']])
        _data_ready(system(plan['data_root']))
        if plan['persistence']:
            persistence = plan['persistence']
            if persistence['create']:
                _save_file(receipt, '/etc/systemd/system/' + persistence['unit'], persistence['content'])
                run(['systemctl', 'daemon-reload'])
            if persistence['enable']:
                receipt['enabled'].append(persistence['unit'])
                run(['systemctl', 'enable', persistence['unit']])
            # The data filesystem was already mounted: rollback must never stop it.
        if plan['existing_logs']:
            receipt['log_root'] = plan['existing_logs']['root']
            (backup / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
            return receipt
        destination = system(plan['log_root'])
        destination.mkdir(parents=True)
        for name, content in plan['files'].items():
            _save_file(receipt, name, content)
        run(['systemd-analyze', 'verify', str(system('/etc/systemd/system/var-log.mount'))])
        run(['logrotate', '-d', str(system('/etc/logrotate.conf'))])
        run(['journalctl', '--sync'])
        run(['journalctl', '--relinquish-var'])
        receipt['journal_relinquished'] = True
        run(['rsync', '-aHAXS', '--numeric-ids', str(system('/var/log')) + '/', str(destination) + '/'], timeout=180)
        journal_dir = destination / 'journal'
        if journal_dir.is_symlink():
            raise ValueError('日志复制结果的 journal 为符号链接')
        journal_dir.mkdir(exist_ok=True, mode=0o2755)
        try:
            os.chown(journal_dir, 0, grp.getgrnam('systemd-journal').gr_gid)
        except KeyError:
            pass
        journal_dir.chmod(0o2755)
        # Preserve the whole root-disk log directory, including open descriptors.
        varlog = system('/var/log')
        info = varlog.stat()
        os.rename(varlog, system(receipt['shadow']))
        receipt['renamed_logs'] = True
        varlog.mkdir(mode=stat.S_IMODE(info.st_mode))
        os.chown(varlog, info.st_uid, info.st_gid)
        # If data is absent at a later boot Storage=auto remains bounded in RAM.
        (varlog / 'journal').symlink_to(destination / 'journal')
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'start', 'var-log.mount'])
        receipt['varlog_mounted'] = True
        for unit in ('var-log.mount', 'logrotate.timer'):
            if run(['systemctl', 'is-enabled', unit], check=False).returncode:
                receipt['enabled'].append(unit)
                run(['systemctl', 'enable', unit])
        run(['systemctl', 'restart', 'systemd-journald.service'])
        run(['journalctl', '--flush'])
        run(['systemctl', 'restart', 'rsyslog.service', 'logrotate.timer'])
        _verify_logging(plan)
        _root_log_residue(receipt)
        (backup / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
        return receipt
    except BaseException as error:
        rollback_result = rollback(receipt)
        (backup / 'failure.json').write_text(json.dumps(dict(error=str(error), receipt=receipt,
                                                           rollback=rollback_result), ensure_ascii=False, indent=2))
        raise RuntimeError('存储/日志部署失败：%s；回退结果：%s' % (error, rollback_result)) from error


def rollback(receipt):
    """Undo only our mount/config changes; retain data and original log files."""
    if receipt.get('reused'):
        return dict(restored=True, errors=[])
    errors = []

    def attempt(argv):
        try:
            result = run(argv, check=False)
            if result.returncode:
                errors.append(' '.join(argv) + ': ' + result.stderr[-500:])
            return result.returncode == 0
        except Exception as error:
            errors.append(str(error))
            return False

    shadow = system(receipt['shadow'])
    renamed = receipt.get('renamed_logs') and shadow.is_dir() and not shadow.is_symlink()
    if renamed:
        attempt(['journalctl', '--relinquish-var'])
        attempt(['systemctl', 'stop', 'rsyslog.service', 'syslog.socket'])
        if os.path.ismount(system('/var/log')):
            attempt(['systemctl', 'stop', 'var-log.mount'])
        if os.path.ismount(system('/var/log')):
            errors.append('/var/log 仍被使用，未强制卸载或覆盖；保留配置及数据盘供人工恢复')
            attempt(['journalctl', '--flush'])
            attempt(['systemctl', 'restart', 'rsyslog.service'])
            return dict(restored=False, errors=errors)
        try:
            varlog = system('/var/log')
            # The temporary mountpoint contains only our fallback journal symlink.
            if varlog.exists():
                failed = system(receipt['shadow'] + '.failed-mountpoint')
                os.rename(varlog, failed)
            os.rename(shadow, varlog)
        except OSError as error:
            errors.append(str(error))
            return dict(restored=False, errors=errors)
    for unit in reversed(receipt.get('enabled', [])):
        attempt(['systemctl', 'disable', unit])
    if receipt.get('mounted_data') and receipt.get('data_unit'):
        # Stop while the unit definition still exists; never force-unmount a busy disk.
        if not attempt(['systemctl', 'stop', receipt['data_unit']]):
            errors.append('数据盘卸载失败，保留本次 mount unit 供人工恢复')
            return dict(restored=False, errors=errors)
    for item in reversed(receipt.get('changed_files', [])):
        path = system(item['path'])
        try:
            if path.is_symlink() or (path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']):
                errors.append('配置已被其他人修改，未覆盖：' + item['path'])
                continue
            if item['backup']:
                shutil.copy2(item['backup'], path)
            else:
                path.unlink(missing_ok=True)
        except OSError as error:
            errors.append(str(error))
    attempt(['systemctl', 'daemon-reload'])
    if renamed:
        attempt(['systemctl', 'restart', 'systemd-journald.service'])
        attempt(['journalctl', '--flush'])
        attempt(['systemctl', 'restart', 'rsyslog.service', 'logrotate.timer'])
    elif receipt.get('journal_relinquished'):
        # A failed rsync before the directory rename still relinquished the
        # original persistent journal; restore its writer on this failure path.
        attempt(['journalctl', '--flush'])
    return dict(restored=not errors, errors=errors)
