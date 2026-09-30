"""Raw CAN monitor through the vLinker FS, one continuous timestamped capture.

The adapter is an STN1170 (OBDLink-class): `STBR` lifts the UART to 2 Mbaud
for the run (volatile, ATZ restores 115200) and `STMA` monitors everything.
At 115200 the car's bus overflows the adapter in ~1.4 s. Run on the Pi
with the app stopped (it owns the port):

    systemctl --user stop laladash.service
    Installation/env/bin/python Tools/can_capture.py <seconds> <out.txt>
    systemctl --user start laladash.service

Findings so far are in Data/can_captures/README.md.

Output lines: "<t_since_start> <ID> <DATAHEX>"; a header records the adapter
identity, protocol and the UART baud that was achieved.
"""
import sys, time, serial

PORT = "/dev/ttyUSB0"
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/can_capture.txt"


def cmd(ser, c, wait=0.35):
    ser.reset_input_buffer()
    ser.write((c + "\r").encode())
    time.sleep(wait)
    out = b""
    t0 = time.time()
    while time.time() - t0 < 1.5:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            out += chunk
            if out.rstrip().endswith(b">"):
                break
        else:
            break
    return out.decode(errors="ignore").replace("\r", " | ").strip()


def _read_until(ser, needles, timeout):
    out = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            out += chunk
            if any(n in out for n in needles):
                return out
    return out


def try_baud(ser, target):
    """STN1170 STBR handshake: 'OK' at the old rate, the ID string at the new
    rate, a CR from us within STBRT, 'OK' at the new rate. Volatile — an ATZ
    or power cycle puts the adapter back on its stored 115200."""
    cmd(ser, "STBRT 2000", 0.2)
    ser.reset_input_buffer()
    ser.write(("STBR %d\r" % target).encode())
    r = _read_until(ser, (b"OK", b"?"), 1.5)
    if b"OK" not in r:
        return False
    ser.baudrate = target
    r = _read_until(ser, (b"STN", b"ELM", b"OBD"), 1.5)
    if not r:
        ser.baudrate = 115200
        time.sleep(0.5)
        ser.reset_input_buffer()
        return False
    ser.write(b"\r")
    r = _read_until(ser, (b"OK", b">"), 1.5)
    if b"OK" in r or b">" in r:
        return True
    ser.baudrate = 115200
    time.sleep(0.5)
    ser.reset_input_buffer()
    return False


ser = serial.Serial(PORT, 115200, timeout=0.05)
hdr = []
hdr.append("ATZ: " + cmd(ser, "ATZ", 1.2))
hdr.append("ATE0: " + cmd(ser, "ATE0"))
hdr.append("ATI: " + cmd(ser, "ATI"))
hdr.append("AT@1: " + cmd(ser, "AT@1"))
hdr.append("STI: " + cmd(ser, "STI"))
hdr.append("ATPPS: " + cmd(ser, "ATPPS", 0.6)[:400])
hdr.append("STP?: " + cmd(ser, "STP"))          # OBDLink-style bus select support?
hdr.append("ATMSCAN?: " + cmd(ser, "ATMSCAN"))  # some FORScan adapters
hdr.append("ATHSCAN?: " + cmd(ser, "ATHSCAN"))
for c in ("ATL0", "ATS0", "ATH1", "ATCAF0", "ATSP6", "ATAL"):
    hdr.append(c + ": " + cmd(ser, c))
hdr.append("ATDP: " + cmd(ser, "ATDP"))
hdr.append("ATRV: " + cmd(ser, "ATRV"))

baud_ok = "115200"
for target in (2000000, 1000000, 921600, 500000):
    if try_baud(ser, target):
        baud_ok = str(target)
        break
hdr.append("UART: " + baud_ok)
hdr.append("STI@new: " + cmd(ser, "STI"))

# ---- monitor ----
ser.timeout = 0.02
ser.reset_input_buffer()
ser.write(b"STMA\r")
t0 = time.time()
buf = b""
n = 0
with open(OUT, "w") as f:
    for h in hdr:
        f.write("# " + h + "\n")
    f.write("# start %s\n" % time.strftime("%H:%M:%S"))
    while time.time() - t0 < SECONDS:
        chunk = ser.read(8192)
        if chunk:
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                s = line.strip().decode(errors="ignore")
                if not s or s in ("ATMA", "STM", "STMA", "SEARCHING...", ">"):
                    continue
                if s.startswith("BUFFER") or s.startswith("<") or s == "STOPPED":
                    f.write("# %.3f %s\n" % (time.time() - t0, s))
                    continue
                s = s.replace(" ", "")
                if len(s) >= 3 and all(c in "0123456789ABCDEF" for c in s):
                    f.write("%.3f %s %s\n" % (time.time() - t0, s[:3], s[3:]))
                    n += 1
    ser.write(b"\r")  # any char stops ATMA
    time.sleep(0.4)
    ser.reset_input_buffer()
    f.write("# end frames=%d\n" % n)

# leave the adapter as we found it: ATZ reverts STBR to the stored 115200
ser.write(b"ATZ\r")
time.sleep(1.5)
ser.baudrate = 115200
time.sleep(0.3)
cmd(ser, "ATZ", 1.2)
ser.close()
print("frames=%d uart=%s out=%s" % (n, baud_ok, OUT))
for h in hdr:
    print("  " + h[:160])
