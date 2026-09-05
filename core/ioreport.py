"""
stethoscope core.ioreport: the IOReport probe (GPU performance states and
energy), read without root.

`powermetrics --samplers gpu_power` is the ground truth for GPU residency,
frequency and watts, and it needs root plus a TTY for sudo (casebook 0007.12).
Every number it prints for the GPU comes from the same kernel reporting
channels this module subscribes to directly through the private IOReport
framework, which needs neither (casebook 0008). Two channel groups:

  * "GPU Stats" / "GPU Performance States": a state channel: OFF, P1..Pn,
    each with residency in ticks of a 24 MHz clock (unit label "24Mticks").
    The residencies over an interval sum to 24e6 x interval seconds, which
    core.validate checks (measured -0.13% against wall clock, casebook
    0008.11). active = 1 - OFF share; the mean of the state frequencies
    weighted by residency is the "HW active frequency" powermetrics prints.

  * "Energy Model" / "GPU Energy": a simple cumulative counter in nJ.
    delta / interval is watts. Measured 32.3-44.9 W on a GPU powermetrics
    had at 38.6-46.0 W minutes earlier (casebook 0008.11).

Everything here degrades to None. IOReport is a private framework: if the
dylib, a symbol, or a channel is missing, `AVAILABLE` is False and the gpu
scope renders "-" for residency, frequency and watts. Never a fabricated
zero (the disk/cpu scopes' rule).

ctypes discipline: every function has argtypes. Without them Python ints are
passed as 32-bit C ints, which truncates a 64-bit CFTypeRef and segfaults
(observed while building this, casebook 0008.11).

No third-party dependencies: system Python 3 + ctypes only.
"""

import ctypes
import re

P = ctypes.c_void_p
_UTF8 = 0x08000100

_CF = None
_IOR = None
AVAILABLE = False
_why_unavailable = None

_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_IOR_PATH = "/usr/lib/libIOReport.dylib"

GROUP_STATS = "GPU Stats"
SUBGROUP_PSTATES = "GPU Performance States"
GROUP_ENERGY = "Energy Model"
CHANNEL_ENERGY = "GPU Energy"
TICKS_PER_S = 24_000_000          # the "24Mticks" residency clock


def _bind():
    """Load CoreFoundation + IOReport and declare every signature we call."""
    global _CF, _IOR, AVAILABLE, _why_unavailable
    try:
        cf = ctypes.CDLL(_CF_PATH)
        ior = ctypes.CDLL(_IOR_PATH)
    except OSError as e:
        _why_unavailable = "dlopen failed: %s" % e
        return

    try:
        cf.CFStringCreateWithCString.restype = P
        cf.CFStringCreateWithCString.argtypes = [P, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [P, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFDictionaryGetValue.restype = P
        cf.CFDictionaryGetValue.argtypes = [P, P]
        cf.CFDictionaryCreateMutableCopy.restype = P
        cf.CFDictionaryCreateMutableCopy.argtypes = [P, ctypes.c_long, P]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [P]
        cf.CFArrayGetValueAtIndex.restype = P
        cf.CFArrayGetValueAtIndex.argtypes = [P, ctypes.c_long]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [P]

        ior.IOReportCopyChannelsInGroup.restype = P
        ior.IOReportCopyChannelsInGroup.argtypes = [P, P, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64]
        ior.IOReportMergeChannels.restype = None
        ior.IOReportMergeChannels.argtypes = [P, P, P]
        ior.IOReportCreateSubscription.restype = P
        ior.IOReportCreateSubscription.argtypes = [P, P, ctypes.POINTER(P), ctypes.c_uint64, P]
        ior.IOReportCreateSamples.restype = P
        ior.IOReportCreateSamples.argtypes = [P, P, P]
        ior.IOReportCreateSamplesDelta.restype = P
        ior.IOReportCreateSamplesDelta.argtypes = [P, P, P]
        for fn in ("IOReportChannelGetGroup", "IOReportChannelGetSubGroup",
                   "IOReportChannelGetChannelName", "IOReportChannelGetUnitLabel"):
            getattr(ior, fn).restype = P
            getattr(ior, fn).argtypes = [P]
        ior.IOReportStateGetCount.restype = ctypes.c_int32
        ior.IOReportStateGetCount.argtypes = [P]
        ior.IOReportStateGetNameForIndex.restype = P
        ior.IOReportStateGetNameForIndex.argtypes = [P, ctypes.c_int32]
        ior.IOReportStateGetResidency.restype = ctypes.c_int64
        ior.IOReportStateGetResidency.argtypes = [P, ctypes.c_int32]
        ior.IOReportSimpleGetIntegerValue.restype = ctypes.c_int64
        ior.IOReportSimpleGetIntegerValue.argtypes = [P, ctypes.c_int32]
    except AttributeError as e:
        _why_unavailable = "IOReport symbol missing: %s" % e
        return

    _CF, _IOR = cf, ior
    AVAILABLE = True


_bind()


def why_unavailable():
    return _why_unavailable


def _cfstr(s):
    return _CF.CFStringCreateWithCString(None, s.encode("utf-8"), _UTF8)


def _pystr(ref, size=512):
    if not ref:
        return None
    buf = ctypes.create_string_buffer(size)
    if _CF.CFStringGetCString(ref, buf, size, _UTF8):
        return buf.value.decode("utf-8", "replace")
    return None


class GPUReporter:
    """One IOReport subscription to the GPU perf-state and energy channels.

    Create once, call `sample()` per interval, and `delta(prev, cur)` to
    turn two raw samples into a dict:

        {"states":    [(name, residency_ticks), ...] in kernel order,
         "ticks":     sum of residencies (the interval, in 24 MHz ticks),
         "energy_nj": cumulative energy delta, or None if the channel is absent}

    `available` is False (and `sample()` returns None) if IOReport is
    missing or the GPU channels are not published on this machine.
    """

    def __init__(self):
        self.available = False
        self.reason = _why_unavailable
        self._sub = None
        self._subbed = None
        self._key_channels = None
        if not AVAILABLE:
            return
        try:
            self._subscribe()
        except Exception as e:           # a private framework: fail closed
            self.reason = "IOReport subscription failed: %s" % e
            self.available = False

    def _subscribe(self):
        chans = _IOR.IOReportCopyChannelsInGroup(
            _cfstr(GROUP_STATS), _cfstr(SUBGROUP_PSTATES), 0, 0, 0)
        if not chans:
            self.reason = "no %r/%r channel" % (GROUP_STATS, SUBGROUP_PSTATES)
            return
        energy = _IOR.IOReportCopyChannelsInGroup(_cfstr(GROUP_ENERGY), None, 0, 0, 0)
        if energy:
            _IOR.IOReportMergeChannels(chans, energy, None)
        mutable = _CF.CFDictionaryCreateMutableCopy(None, 0, chans)
        subbed = P()
        sub = _IOR.IOReportCreateSubscription(None, mutable, ctypes.byref(subbed), 0, None)
        if not sub:
            self.reason = "IOReportCreateSubscription returned NULL"
            return
        self._sub, self._subbed = sub, subbed
        self._key_channels = _cfstr("IOReportChannels")
        self.available = True

    def sample(self):
        """Raw CF sample (opaque). Release with `release()` when done."""
        if not self.available:
            return None
        return _IOR.IOReportCreateSamples(self._sub, self._subbed, None)

    def release(self, ref):
        if ref:
            _CF.CFRelease(ref)

    def delta(self, prev, cur):
        """Reduce two raw samples to the GPU vitals dict (see class doc)."""
        if not (self.available and prev and cur):
            return None
        d = _IOR.IOReportCreateSamplesDelta(prev, cur, None)
        if not d:
            return None
        try:
            arr = _CF.CFDictionaryGetValue(d, self._key_channels)
            n = _CF.CFArrayGetCount(arr) if arr else 0
            states, energy_nj = None, None
            for i in range(n):
                ch = _CF.CFArrayGetValueAtIndex(arr, i)
                group = _pystr(_IOR.IOReportChannelGetGroup(ch))
                if group == GROUP_STATS:
                    if _pystr(_IOR.IOReportChannelGetSubGroup(ch)) != SUBGROUP_PSTATES:
                        continue
                    count = _IOR.IOReportStateGetCount(ch)
                    states = [(_pystr(_IOR.IOReportStateGetNameForIndex(ch, k)),
                               _IOR.IOReportStateGetResidency(ch, k))
                              for k in range(count)]
                elif group == GROUP_ENERGY:
                    if _pystr(_IOR.IOReportChannelGetChannelName(ch)) != CHANNEL_ENERGY:
                        continue
                    unit = _pystr(_IOR.IOReportChannelGetUnitLabel(ch))
                    v = _IOR.IOReportSimpleGetIntegerValue(ch, 0)
                    # Normalize to nJ whatever the kernel labels it.
                    energy_nj = {"nJ": v, "uJ": v * 1000, "mJ": v * 1_000_000}.get(unit)
            if states is None:
                return None
            return {"states": states,
                    "ticks": sum(r for _, r in states),
                    "energy_nj": energy_nj}
        finally:
            _CF.CFRelease(d)


# ---------------------------------------------------------------------------
# pure derivations over a delta dict: shared by the gpu scope and validate
# ---------------------------------------------------------------------------

def active_fraction(delta):
    """1 - OFF residency share. None if there is no OFF state to read."""
    if not delta or not delta["ticks"]:
        return None
    off = next((r for name, r in delta["states"] if name == "OFF"), None)
    if off is None:
        return None
    return 1.0 - off / delta["ticks"]


_PSTATE_RE = re.compile(r"^P(\d+)$")


def state_mhz(name, pstate_mhz):
    """MHz for a state named 'P<n>' from the pmgr table (OFF at index 0,
    P<n> at index n). None for OFF, for names not of that shape, and for
    states past the table (the unused upper P-states)."""
    m = _PSTATE_RE.match(name or "")
    if not m or not pstate_mhz:
        return None
    n = int(m.group(1))
    return pstate_mhz[n] if 0 < n < len(pstate_mhz) and pstate_mhz[n] else None


def active_frequency_mhz(delta, pstate_mhz):
    """Residency-weighted mean frequency of the non-OFF states that map to
    a frequency. Keyed by the state's own number, not its list position,
    so an omitted state cannot shift every frequency by one bin. None if
    nothing mapped."""
    if not delta or not pstate_mhz:
        return None
    weighted = active = 0
    for name, r in delta["states"]:
        mhz = state_mhz(name, pstate_mhz)
        if mhz is None:
            continue
        weighted += r * mhz
        active += r
    return weighted / active if active else None


def watts(delta, seconds):
    """Energy delta over `seconds`, in W. None when the channel is absent."""
    if not delta or delta["energy_nj"] is None or not seconds:
        return None
    return max(0, delta["energy_nj"]) / seconds / 1e9
