"""What this computer has: memory, graphics memory and room on each disk.
No new dependency and no subprocess for any of it, except the AI's own measure (measure_ai), which runs lastbook-llm."""
import os
import shutil
import subprocess
from pathlib import Path

GiB = 2 ** 30
RESERVE = int(2.5 * GiB)     # the same reserve llm_local.judge keeps for the system and the rest of Lastbook
VRAM_KEPT = GiB              # graphics memory the estimate leaves for the card's own work


# ------------------------------------------------------------------------------------------------------------- Linux --
def _linux_ram():
    """MemTotal from /proc/meminfo, in bytes."""
    with open('/proc/meminfo', encoding='ascii', errors='replace') as f:
        for line in f:
            if line.startswith('MemTotal:'):
                return int(line.split()[1]) * 1024
    return None


def _linux_gpus():
    """NVIDIA cards with their memory, from the driver's own files under /proc and /sys: no nvidia-smi, no subprocess.
    Other cards (AMD through amdgpu's mem_info_vram_total, which Vulkan would use) are listed with what the kernel says.
    lastbook-llm's own --measure (measure_ai) is what decides once the model is here; this only shapes the estimate before that."""
    out = []
    drm = Path('/sys/class/drm')
    try:
        cards = sorted(p for p in drm.iterdir() if p.name.startswith('card') and '-' not in p.name)
    except OSError:
        cards = []
    for card in cards:
        dev = card / 'device'
        try:
            vendor = (dev / 'vendor').read_text().strip()
        except OSError:
            continue
        vram = 0
        try:
            vram = int((dev / 'mem_info_vram_total').read_text().strip())    # amdgpu
        except (OSError, ValueError):
            pass
        name = {'0x10de': 'NVIDIA', '0x1002': 'AMD', '0x8086': 'Intel'}.get(vendor, vendor)
        if vendor == '0x10de':
            name, vram = _nvidia_card(dev, name, vram)
        if vendor == '0x8086' and not vram:
            continue        # integrated graphics share the computer's memory: no card memory to count
        out.append({'name': name, 'vram_bytes': vram})
    return out


def _apple_silicon_linux(tree='/proc/device-tree'):
    """The model name of an Apple silicon computer running Linux, else None. The kernel's device tree names it ("apple,t8103"); its
    graphics are not on a PCI bus, so _linux_gpus finds no card, and they share the computer's one memory."""
    try:
        if not any(c.startswith(b'apple,') for c in (Path(tree) / 'compatible').read_bytes().split(b'\0')):
            return None
        return (Path(tree) / 'model').read_bytes().split(b'\0')[0].decode('utf-8', 'replace') or 'Apple silicon'
    except OSError:
        return None


def _nvidia_card(dev, name, vram):
    """The model name from the NVIDIA driver's /proc/driver/nvidia/gpus/<bus id>/information. Its memory is not in a file the driver
    keeps for everyone, so it comes from the PCI BAR the card maps for its memory when that is the full size (resizable BAR), else 0."""
    try:
        bus = dev.resolve().name
        info = Path('/proc/driver/nvidia/gpus') / bus / 'information'
        for line in info.read_text(errors='replace').splitlines():
            if line.startswith('Model:'):
                name = line.split(':', 1)[1].strip()
    except OSError:
        pass
    if not vram:
        try:
            sizes = []
            for line in (dev / 'resource').read_text().splitlines():
                start, end, _ = (int(x, 16) for x in line.split())
                if end > start:
                    sizes.append(end - start + 1)
            big = max(sizes) if sizes else 0
            vram = big if big >= 2 * GiB else 0
        except (OSError, ValueError):
            pass
    return name, vram


# ------------------------------------------------------------------------------------------------------------- disks --
def disk_of(path):
    """(free, total) on the disk that holds path, or the nearest folder above it that exists."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    u = shutil.disk_usage(p)
    return u.free, u.total


def candidate_disks(extra=()):
    """Disk capacity for app-owned or explicitly selected paths only."""
    places = list(map(Path, extra))
    seen, out = set(), []
    for p in places:
        try:
            q = p
            while not q.exists() and q.parent != q:
                q = q.parent
            dev = os.stat(q).st_dev
            if dev in seen:
                continue
            free, total = disk_of(q)
        except OSError:
            continue
        seen.add(dev)
        out.append({'path': str(p), 'free_bytes': free, 'total_bytes': total})
    return out


# ------------------------------------------------------------------------------------------------------------ the AI --
def _gb(n):
    return f'{n / 1e9:.0f} GB' if n >= 10e9 else f'{n / 1e9:.1f} GB'


def estimate_ai(ram, gpus, unified, memory_bytes):
    """Before the model file is here to measure: the model and its context need memory_bytes, plus RESERVE for everything else.
    With unified memory (Apple silicon) that is all one memory. With a graphics card, the part that fits on it does not count against the computer's own."""
    needs = int(memory_bytes + RESERVE)
    if not ram:
        return {'fits': False, 'needs_bytes': needs, 'estimate': True, 'why': 'Lastbook could not tell how much memory this computer has.'}
    if unified or not gpus:
        in_ram = needs
    else:
        vram = max(g['vram_bytes'] for g in gpus)
        in_ram = int(max(memory_bytes - max(vram - VRAM_KEPT, 0), 0) + RESERVE)
    fits = ram >= in_ram
    out = {'fits': fits, 'needs_bytes': needs, 'estimate': True}
    if fits:
        out['why'] = f'The local AI needs about {_gb(needs)} of memory, and this computer has {_gb(ram)}' + (
            '.' if unified or not gpus else ' plus its graphics memory.')
    else:
        out['why'] = (f'The local AI needs about {_gb(in_ram)} of this computer’s memory, and it has {_gb(ram)}. '
                      'Lastbook works without it: search and reading are the same.')
    return out


def measure_ai(llm_local, model_path, timeout=180):
    """llama.cpp's own measure of the model file on this computer, judged as the AI judges it. None if it cannot be measured."""
    runner = llm_local.runner_path()
    if not runner or not Path(model_path).is_file():
        return None
    try:
        fit, _ = llm_local.measure(runner, model_path, timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not fit:
        return None
    j = llm_local.judge(fit)
    return {'fits': bool(j['fits']), 'needs_bytes': int(j['needs']), 'estimate': False,
            'why': j.get('why') or f'Measured on this computer: the local AI needs about {_gb(j["needs"])} of memory, and it fits.'}


def detect(extra_disks=()):
    """Everything but the AI's fit, which the caller adds (estimate_ai or measure_ai)."""
    ram, gpus, unified = None, [], False
    try:
        ram = _linux_ram()
        gpus = _linux_gpus()
        if model := _apple_silicon_linux():
            gpus, unified = [{'name': model, 'vram_bytes': 0}], True
    except Exception as e:      # a strange driver must not stop setup: it then estimates from what it has
        print('setup: hardware detection:', type(e).__name__, e, flush=True)
    return {'ram_bytes': ram, 'unified_memory': unified, 'gpus': gpus, 'disks': candidate_disks(extra_disks)}
