"""Give only selected dedicated main-board interfaces to the mode manager."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess

CONF = Path('/etc/NetworkManager/conf.d/90-uav-dedicated.conf')
IFUPDOWN = Path('/etc/network/interfaces')
IFUPDOWN_DIR = Path('/etc/network/interfaces.d')

def run(argv, check=True):
    return subprocess.run(argv, check=check, capture_output=True, text=True, timeout=30)

def _address(value):
    value = value.strip('[]').split('%', 1)[0]
    try:
        parsed = ipaddress.ip_address(value)
        return str(parsed.ipv4_mapped or parsed) if isinstance(parsed, ipaddress.IPv6Address) else str(parsed)
    except ValueError:
        return None

def _management_ifaces(names, present):
    # Releasing a physical member can also disconnect the IP on its bridge/bond.
    guarded = set(names)
    pending = list(names)
    by_index = {r.get('ifindex'): n for n, r in present.items()}
    while pending:
        name = pending.pop()
        master = present[name].get('master')
        if isinstance(master, int): master = by_index.get(master)
        if master and master in present and master not in guarded:
            guarded.add(master); pending.append(master)
    return guarded

def _ssh_local_addresses(ssh_connection):
    """Protect live SSH sessions even when sudo has removed SSH_CONNECTION."""
    local = set()
    ssh = ssh_connection.split()
    if len(ssh) == 4 and _address(ssh[2]): local.add(_address(ssh[2]))
    try:
        result = run(['ss', '-Htnp', 'state', 'established'], check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('无法检查 SSH 实际连接，未变更专用网口') from error
    if result.returncode:
        raise ValueError('无法检查 SSH 实际连接，未变更专用网口')
    for line in result.stdout.splitlines():
        # With a state filter ss omits State: Recv-Q Send-Q Local Peer [Process].
        # Some versions retain it; locating numeric queue columns handles both.
        columns = line.split()
        offset = 1 if columns and columns[0].upper() in ('ESTAB', 'ESTABLISHED') else 0
        if len(columns) < offset + 4:
            if 'sshd' in line:
                raise ValueError('无法解析 SSH 实际连接，未变更专用网口')
            continue
        endpoint = columns[offset + 2]
        if ':' not in endpoint:
            if 'sshd' in line:
                raise ValueError('无法解析 SSH 本地地址，未变更专用网口')
            continue
        address, port = endpoint.rsplit(':', 1)
        is_ssh = port == '22' or bool(re.search(r'\("sshd(?:-session|-auth)?"', line))
        if not is_ssh:
            continue
        normalized = _address(address)
        if normalized is None:
            raise ValueError('无法确定 SSH 本地地址，未变更专用网口')
        local.add(normalized)
    return local

def preflight(config, ssh_connection=None):
    names = [config['payload_iface'], config['mk22_iface']]
    if config['default_mode'] == 'dual': names.append(config['extension_iface'])
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', n) for n in names):
        raise ValueError('无效专用网口')
    infos = json.loads(run(['ip', '-j', 'address', 'show']).stdout)
    present = {i['ifname']: i for i in infos}
    if not set(names) <= present.keys(): raise ValueError('选定网口不存在')
    guarded = _management_ifaces(names, present)
    routes = json.loads(run(['ip', '-j', 'route', 'show', 'default']).stdout)
    if any(r.get('dev') in guarded for r in routes):
        raise ValueError('选定的载荷/MK22网口承担默认上网路由，请先通过Wi-Fi或另一网口管理新板')
    ssh_local = _ssh_local_addresses(os.environ.get('SSH_CONNECTION', '') if ssh_connection is None else ssh_connection)
    if any(_address(a.get('local', '')) in ssh_local for n in guarded for a in present[n].get('addr_info', [])):
        raise ValueError('SSH正在使用选定专用网口，请通过Wi-Fi或本机终端部署，避免断开管理连接')
    # Do not fight a second persistent network owner on an unknown BSP.
    for p in [IFUPDOWN] + list(IFUPDOWN_DIR.glob('*')):
        if p.is_file() and any(re.search(r'^\s*(?:auto|allow-hotplug|iface)\s+'+re.escape(n)+r'(?:\s|$)', p.read_text(), re.M) for n in names):
            raise ValueError('厂家ifupdown已管理选定网口，请先核对配置：'+str(p))
    if run(['systemctl', 'is-active', 'systemd-networkd.service'], check=False).returncode == 0:
        raise ValueError('此BSP使用systemd-networkd；请先明确专用口配置，工具不覆盖未知网络管理方式')
    nm = run(['systemctl', 'is-active', 'NetworkManager.service'], check=False).returncode == 0
    content = ''.join('[device-uav-%s]\nmatch-device=interface-name:%s\nmanaged=0\n\n' % (n,n) for n in names)
    if CONF.is_symlink() or (CONF.exists() and CONF.read_text() != content):
        raise ValueError('专用网口配置已存在且不同，保持原状：'+str(CONF))
    managed = {}
    if nm:
        for name in names:
            managed[name] = run(['nmcli','-g','GENERAL.NM-MANAGED','device','show',name]).stdout.strip().lower() == 'yes'
    return {'names':names, 'nm':nm, 'managed':managed, 'content':content,
            'existing':CONF.exists(), 'before':infos, 'guarded_ifaces': sorted(guarded)}

def apply(plan, start=True):
    receipt = dict(plan, changed=[], created=False, reloaded=False)
    try:
        # Close the preflight/apply gap: a user may have opened SSH in between.
        infos = json.loads(run(['ip', '-j', 'address', 'show']).stdout)
        present = {i['ifname']: i for i in infos}
        if not set(plan['names']) <= present.keys(): raise ValueError('选定网口已消失')
        guarded = _management_ifaces(plan['names'], present)
        routes = json.loads(run(['ip', '-j', 'route', 'show', 'default']).stdout)
        if any(r.get('dev') in guarded for r in routes):
            raise ValueError('专用网口已开始承担默认路由，未变更管理连接')
        local = _ssh_local_addresses(os.environ.get('SSH_CONNECTION', ''))
        if any(_address(a.get('local', '')) in local for n in guarded for a in present[n].get('addr_info', [])):
            raise ValueError('SSH已开始使用专用网口，未变更管理连接')
        if plan['nm']:
            if CONF.is_symlink() or (CONF.exists() and CONF.read_text() != plan['content']):
                raise ValueError('专用网口配置已变化，未覆盖')
            if not plan['existing']:
                CONF.parent.mkdir(parents=True,exist_ok=True)
                with CONF.open('x') as f: f.write(plan['content'])
                receipt['created'] = True
            if not start:
                # New persistent policy takes effect on the next boot only.
                return receipt
            # Reload can apply managed=0 before the explicit device operations.
            receipt['changed'] = [n for n in plan['names'] if plan['managed'].get(n)]
            receipt['reloaded'] = True
            run(['nmcli','general','reload','conf'])
            for name in plan['names']:
                if plan['managed'].get(name):
                    run(['nmcli','device','set',name,'managed','no'])
        return receipt
    except Exception:
        rollback(receipt)
        raise

def rollback(receipt):
    if not receipt.get('created') and not receipt.get('changed'): return
    if receipt.get('created'): CONF.unlink(missing_ok=True)
    if receipt.get('nm') and receipt.get('reloaded', bool(receipt.get('changed'))):
        run(['nmcli','general','reload','conf'],check=False)
        for name in receipt.get('changed',[]):
            run(['nmcli','device','set',name,'managed','yes'],check=False)
