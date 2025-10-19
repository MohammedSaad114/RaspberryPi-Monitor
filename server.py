# server.py
import os, json, platform, time, shutil, subprocess, re
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import psutil

app = FastAPI()

FS_EXCLUDE = {
    "tmpfs","devtmpfs","proc","sysfs","cgroup","cgroup2","pstore",
    "securityfs","debugfs","tracefs","ramfs","autofs","overlay",
    "overlayfs","fusectl","squashfs","nsfs"
}

SMART_CACHE_TTL = 60.0
_smart_cache: Dict[str, Dict[str, Any]] = {}
_smart_cached_at: Dict[str, float] = {}

ENV_SMART_DEV = os.getenv("SMART_DEV")          # /dev/sda
ENV_SMART_DRIVER = os.getenv("SMART_DRIVER")   

def fmt_gb(v: int) -> float:
    return round(v / (1024 ** 3), 2)

def read_cpu_temps() -> Optional[float]:
    for p in ("/sys/class/thermal/thermal_zone0/temp",
              "/sys/devices/virtual/thermal/thermal_zone0/temp"):
        try:
            with open(p, "r") as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            pass
    vc = "/usr/bin/vcgencmd"
    if os.path.exists(vc):
        try:
            out = subprocess.check_output([vc, "measure_temp"], timeout=3).decode()
            return float(out.split("=")[1].split("'")[0])
        except Exception:
            return None
    return None

def disk_kind(device: str) -> str:
    if device.startswith("/dev/mmcblk"): return "sdcard"
    if device.startswith("/dev/sd"): return "usb"
    return "other"

def base_device(dev: str) -> str:
    if dev.startswith("/dev/sd"):
        b = dev
        while b and b[-1].isdigit(): b = b[:-1]
        return b
    if dev.startswith("/dev/mmcblk") and "p" in dev:
        return dev.split("p")[0]
    return dev

def list_disks():
    out = []
    for p in psutil.disk_partitions(all=False):
        if p.fstype in FS_EXCLUDE: continue
        if any(bad in p.mountpoint for bad in ("/proc","/sys","/run","/snap","/var/lib/docker")): continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except PermissionError:
            continue
        out.append({
            "device": p.device,
            "baseDevice": base_device(p.device),
            "mount": p.mountpoint,
            "fstype": p.fstype,
            "kind": disk_kind(p.device),
            "total": fmt_gb(u.total),
            "used": fmt_gb(u.used),
            "free": fmt_gb(u.free),
            "percent": round(u.percent, 1),
        })
    out.sort(key=lambda d: (0 if d["mount"]=="/" else 1,
                            0 if d["kind"]=="sdcard" else 1 if d["kind"]=="usb" else 2,
                            d["mount"]))
    return out

# ---------- Pi power / throttle (vcgencmd) ----------
def read_throttle() -> Dict[str, Any]:
    data = {
        "available": False, "raw": None,
        "under_voltage_now": False, "freq_capped_now": False, "throttled_now": False,
        "under_voltage_hist": False, "freq_capped_hist": False, "throttled_hist": False,
        "status": "Unknown", "note": None,
    }
    vc = "/usr/bin/vcgencmd"
    if not os.path.exists(vc):
        data["note"] = "vcgencmd not found"; return data
    try:
        out = subprocess.check_output([vc, "get_throttled"], timeout=3).decode().strip()
        raw = out.split("=")[1] if "=" in out else out
        val = int(raw, 16) if raw.startswith("0x") else int(raw, 0)
        data["available"] = True; data["raw"] = f"0x{val:x}"
        data["under_voltage_now"] = bool(val & (1<<0))
        data["freq_capped_now"]   = bool(val & (1<<1))
        data["throttled_now"]     = bool(val & (1<<2))
        data["under_voltage_hist"]= bool(val & (1<<16))
        data["freq_capped_hist"]  = bool(val & (1<<17))
        data["throttled_hist"]    = bool(val & (1<<18))
        if data["under_voltage_now"]: data["status"] = "Under-voltage NOW"
        elif data["throttled_now"]:  data["status"] = "Throttled NOW"
        elif data["freq_capped_now"]:data["status"] = "Frequency capped NOW"
        elif (data["under_voltage_hist"] or data["throttled_hist"] or data["freq_capped_hist"]):
            data["status"] = "Power issue occurred"
        else: data["status"] = "OK"
        return data
    except Exception as e:
        data["note"] = f"vcgencmd failed: {e}"; return data

# ---------- SMART helpers ----------
def which_smartctl() -> Optional[str]:
    for path in ("/usr/sbin/smartctl", "/sbin/smartctl", shutil.which("smartctl")):
        if path and os.path.exists(path):
            return path
    return None

def _smart_cmd_base():
    sc = which_smartctl()
    if not sc: return None
    if os.geteuid() != 0:
        return ["sudo", "-n", sc]
    return [sc]

def _run(cmd) -> Dict[str, Any]:
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(timeout=8)
        return {"stdout": out.decode(errors="ignore"),
                "stderr": err.decode(errors="ignore"),
                "code": p.returncode,
                "cmd": " ".join(cmd)}
    except Exception as e:
        return {"stdout":"", "stderr": str(e), "code": None, "cmd": " ".join(cmd)}

DEFAULT_DRIVERS = ["plain","sat","sat,12","scsi","usbjmicron","usbsunplus","usbcypress"]
def driver_flags(name: str): return [] if name == "plain" else ["-d", name]

def _parse_smart_json(obj: dict) -> dict:
    out = {
        "available": True,
        "model": obj.get("model_name") or obj.get("device", {}).get("model_name") or obj.get("model_family"),
        "serial": obj.get("serial_number"), "firmware": obj.get("firmware_version"),
        "health_passed": obj.get("smart_status", {}).get("passed"),
        "temperature": (obj.get("temperature") or {}).get("current"),
        "power_on_hours": None, "reallocated": None, "pending": None, "uncorrectable": None,
        "crc_errors": None, "start_stop": None, "power_cycles": None,
        "power_hint": None, "status": None, "note": None,
    }
    attrs = {}
    for a in obj.get("ata_smart_attributes", {}).get("table", []):
        name = a.get("name"); raw = a.get("raw", {}).get("value")
        if name: attrs[name] = raw
    out["power_on_hours"] = attrs.get("Power_On_Hours")
    out["reallocated"] = attrs.get("Reallocated_Sector_Ct")
    out["pending"] = attrs.get("Current_Pending_Sector")
    out["uncorrectable"] = attrs.get("Reported_Uncorrect") or attrs.get("Offline_Uncorrectable")
    out["crc_errors"] = attrs.get("UDMA_CRC_Error_Count")
    out["start_stop"] = attrs.get("Start_Stop_Count")
    out["power_cycles"] = attrs.get("Power_Cycle_Count")
    power_issue = isinstance(out["crc_errors"], int) and out["crc_errors"] > 0
    if out["health_passed"] is False: out["status"] = "Fail"
    elif power_issue: out["status"] = "Check power/cable"
    elif any((x is not None and x > 0) for x in [out["reallocated"], out["pending"], out["uncorrectable"]]): out["status"] = "Watch"
    else: out["status"] = "OK"
    if power_issue: out["power_hint"] = "CRC errors detected — often power/cable/USB link."
    return out

def read_smart(dev: str) -> dict:
    now = time.time()
    if dev in _smart_cache and (now - _smart_cached_at.get(dev, 0.0)) < SMART_CACHE_TTL:
        return _smart_cache[dev]
    base = _smart_cmd_base()
    if not base:
        data = {"available": False, "note": "smartctl not found"}
        _smart_cache[dev] = data; _smart_cached_at[dev] = now; return data
    drivers = [ENV_SMART_DRIVER.strip()] + [d for d in DEFAULT_DRIVERS if d != ENV_SMART_DRIVER.strip()] if ENV_SMART_DRIVER else DEFAULT_DRIVERS
    last_note = ""
    for drv in drivers:
        cmd = base + ["-a","--json"] + driver_flags(drv) + [dev]
        res = _run(cmd)
        if res["stdout"].strip().startswith("{"):
            try:
                obj = json.loads(res["stdout"])
                parsed = _parse_smart_json(obj)
                _smart_cache[dev] = parsed; _smart_cached_at[dev] = now; return parsed
            except Exception as e:
                last_note = f"{res['cmd']} -> JSON parse error: {e}"; break
        else:
            err_snip = (res["stderr"] or "")[:160].strip().replace("\n"," ")
            last_note = f"{res['cmd']} -> code={res['code']} err='{err_snip}'"
    data = {"available": False, "note": last_note or "smartctl read failed"}
    _smart_cache[dev] = data; _smart_cached_at[dev] = now; return data

# ---------- USB link info (sysfs) ----------
def read_usb_info_for_block(dev_path: str) -> Dict[str, Any]:
    info = {"available": False}
    try:
        block = os.path.basename(dev_path)
        sys_dev = os.path.realpath(f"/sys/block/{block}/device")
        p = sys_dev
        usb_node = None
        for _ in range(12):
            if os.path.exists(os.path.join(p, "idVendor")) or os.path.exists(os.path.join(p, "bMaxPower")):
                usb_node = p
                break
            np = os.path.dirname(p)
            if not np or np == p: break
            p = np
        if not usb_node:
            return info
        def readf(name):
            f = os.path.join(usb_node, name)
            try:
                with open(f, "r") as fh:
                    return fh.read().strip()
            except Exception:
                return None
        product = readf("product")
        manufacturer = readf("manufacturer")
        idVendor = readf("idVendor")
        idProduct = readf("idProduct")
        bMaxPower = readf("bMaxPower")  # like "500mA" or "0mA"
        speed = readf("speed")          # like "480" or "5000" (Mb/s)

        # driver: look for interface :1.0 driver symlink
        driver = None
        # try common interface subdir
        for child in os.listdir(usb_node):
            if re.search(r":\d+\.\d+$", child):  # e.g., 1-1.3:1.0
                drv_link = os.path.join(usb_node, child, "driver")
                if os.path.islink(drv_link):
                    driver = os.path.basename(os.path.realpath(drv_link))
                    break
        # fallback: check any driver link under node
        if not driver:
            drv_link = os.path.join(usb_node, "driver")
            if os.path.islink(drv_link):
                driver = os.path.basename(os.path.realpath(drv_link))

        info.update({
            "available": True,
            "product": product,
            "manufacturer": manufacturer,
            "vid": idVendor,
            "pid": idProduct,
            "declaredMaxPower_mA": int(bMaxPower[:-2]) if (bMaxPower and bMaxPower.endswith("mA") and bMaxPower[:-2].isdigit()) else None,
            "speed_Mbps": int(float(speed)) if speed else None,
            "driver": driver,
            "sysfs_node": usb_node.split("/devices/")[-1] if "/devices/" in usb_node else usb_node,
            "note": "Declared bMaxPower is from USB descriptor, not live current.",
        })
        return info
    except Exception as e:
        info["note"] = f"usb sysfs read failed: {e}"
        return info

# ---------- API ----------
@app.get("/metrics")
def metrics():
    cpu_per_core: List[float] = psutil.cpu_percent(percpu=True, interval=0.2)
    vm = psutil.virtual_memory()

    disks = list_disks()
    seen = set()
    for d in disks:
        base_dev = d["baseDevice"]
        target = ENV_SMART_DEV or base_dev
        if d["kind"] == "usb" and base_dev not in seen and (ENV_SMART_DEV is None or base_dev == target):
            d["smart"] = read_smart(target)
            d["usb"] = read_usb_info_for_block(base_dev.replace("/dev/","/dev/"))  # base dev -> usb info
            seen.add(base_dev)
        else:
            d["smart"] = {"available": False}
            if d["kind"] == "usb":
                d["usb"] = read_usb_info_for_block(base_dev)
            else:
                d["usb"] = {"available": False}

    data = {
        "hostname": platform.node(),
        "platform": platform.system().lower(),
        "arch": platform.machine(),
        "cpuTemp": read_cpu_temps(),
        "cpuUsage": [round(x, 1) for x in cpu_per_core],
        "memoryUsage": {
            "total": fmt_gb(vm.total),
            "used": fmt_gb(vm.total - vm.available),
            "free": fmt_gb(vm.available),
        },
        "disks": disks,
        "power": {"throttle": read_throttle()},
    }
    return Response(content=json.dumps(data, separators=(",",":")),
                    media_type="application/json",
                    headers={"Cache-Control":"no-store"})

# ---------- Static ----------
app.mount("/", StaticFiles(directory="static", html=True), name="static")

@app.get("/favicon.ico")
def favicon():
    path = os.path.join("static", "favicon.ico")
    if os.path.exists(path):
        return FileResponse(path)
    return Response(status_code=204)
