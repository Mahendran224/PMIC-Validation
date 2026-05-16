
import sys, os, io, datetime
import pyvisa
import numpy as np
import time

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, QSplitter,
    QWidget, QListWidget, QLabel, QHBoxLayout, QListWidgetItem,
    QButtonGroup, QRadioButton, QGroupBox, QSizePolicy, QFrame,
    QMessageBox, QFileDialog
)
from PyQt5.QtGui  import QPixmap, QIcon, QImage
from PyQt5.QtCore import QTimer, Qt

import matplotlib
matplotlib.use("Qt5Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure                  import Figure as MplFigure
from matplotlib.backends.backend_agg   import FigureCanvasAgg

from reportlab.lib.pagesizes import A4
from reportlab.lib           import colors as RL_COLORS
from reportlab.lib.units     import mm
from reportlab.lib.styles    import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums     import TA_CENTER
from reportlab.platypus      import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Image as RLImage, HRFlowable
)

# ============================================================
# VISA CONNECTION
# ============================================================
def connect_to_scope():
    rm    = pyvisa.ResourceManager()
    known = "USB0::0x2A8D::0x038B::CN64510279::0::INSTR"
    try:
        scope = rm.open_resource(known)
        scope.timeout    = 10000
        scope.chunk_size = 1024000
        print("Connected:", scope.query("*IDN?").strip())
        return scope, rm
    except Exception as e:
        print(f"Direct connect failed: {e}")
    for res in rm.list_resources():
        if 'USB' not in res:
            continue
        try:
            s   = rm.open_resource(res)
            s.timeout = 5000
            idn = s.query("*IDN?").strip()
            if any(k in idn.upper() for k in ('KEYSIGHT','AGILENT','EDUX')):
                s.timeout    = 10000
                s.chunk_size = 1024000
                print(f"Found at {res}: {idn}")
                return s, rm
            s.close()
        except Exception:
            continue
    raise RuntimeError("No oscilloscope found. Check USB cable and VISA drivers.")

try:
    scope, rm = connect_to_scope()
    scope.write(":WAV:FORM BYTE")
    scope.write(":WAV:POIN 2000")
    scope.write(":TIM:SCAL 0.2")
    scope.write(":CHAN1:COUP DC")
    scope.write(":CHAN2:COUP DC")
    time.sleep(0.5)
    scope.write(":TRIG:EDGE:SOUR CHAN1")
    scope.write(":TRIG:EDGE:SLOP EITH")
    scope.write(":TRIG:EDGE:LEV 0.1")
    scope.write(":TRIG:SWE AUTO")
    scope.write(":WAV:SOUR CHAN1")
    time.sleep(0.1)
    dt = float(scope.query(":WAV:XINC?"))
    print(f"dt = {dt*1000:.3f} ms/sample  |  window = {dt*2000*1000:.0f} ms")
except Exception as e:
    _a = QApplication(sys.argv)
    QMessageBox.critical(None, "Connection Error", str(e))
    sys.exit(1)

# ============================================================
# RAIL CONSTANTS
# ============================================================
VNOM = {
    "BUCK1": 0.749, "BUCK2": 3.289, "BUCK3": 1.197,
    "LDO1":  1.796, "LDO2":  0.848,
    "LDO3":  1.796, "LDO4":  2.496,
}

CHAN_CFG = {
    #         V/div   offset  trig_V
    "BUCK1": (0.20,   0.0,   0.10),
    "BUCK2": (1.00,   0.0,   0.30),
    "BUCK3": (0.50,   0.0,   0.10),
    "LDO1":  (0.50,   0.0,   0.20),
    "LDO2":  (0.20,   0.0,   0.10),
    "LDO3":  (0.50,   0.0,   0.20),
    "LDO4":  (1.00,   0.0,   0.20),
}

BUCK_TRAMP_RANGE = {"BUCK1":(1.0,500.0),"BUCK2":(1.0,500.0),"BUCK3":(1.0,500.0)}
BUCK_IC_SPEC     = {"BUCK1":(0.3,1.65), "BUCK2":(0.3,1.65), "BUCK3":(0.3,1.65)}
LDO_SLEW_RANGE   = {"LDO1":(0.001,9999.0),"LDO2":(0.001,9999.0),
                    "LDO3":(0.001,9999.0),"LDO4":(0.001,9999.0)}
LDO_IC_SPEC      = {"LDO1":(10.0,14.0),"LDO2":(10.0,14.0),
                    "LDO3":(7.5,27.0), "LDO4":(7.5,27.0)}

# FAIL limits
_BUCK_FAIL_LO  = 3.0
_BUCK_FAIL_HI  = 60.0
_LDO_FAIL_LO   = 2.0
_LDO_FAIL_HI   = 60.0

# DC tolerance
_DC_TOL_WIDE = {"BUCK2", "BUCK3", "LDO4"}

def _internal_dc_tol(rail):
    return 2.0 if rail in _DC_TOL_WIDE else 1.0

def get_voltage_range(output_name):
    nom = VNOM.get(output_name, 0)
    tol = _internal_dc_tol(output_name)
    return (nom - tol, nom + tol)

def get_voltage_range_display(output_name):
    nom = VNOM.get(output_name, 0)
    return (nom - 1.0, nom + 1.0)

# tRAMP lock store
_tramp_lock: dict = {}

def get_locked_tramp(rail):
    return _tramp_lock.get(rail, None)

def try_lock_tramp(rail, value_ms):
    if rail in _tramp_lock:
        return _tramp_lock[rail]
    if value_ms is not None and value_ms > 0.001:
        _tramp_lock[rail] = value_ms
        print(f"tRAMP locked: {rail} = {value_ms:.4f} ms")
    return value_ms

# Persistent global stores
measurement_history = []
snapshot_store      = []

# ============================================================
# SMOOTHING
# ============================================================
_SMOOTH_W = 5

def smooth(sig):
    w = _SMOOTH_W
    if len(sig) < w:
        return sig.copy().astype(np.float64)
    sig = sig.astype(np.float64)
    csum = np.cumsum(sig)
    csum[w:] = csum[w:] - csum[:-w]
    out = sig.copy()
    out[w-1:] = csum[w-1:] / w
    return out

# ============================================================
# CHANNEL SCALING CACHE
# ============================================================
class ChannelScaleCache:
    def __init__(self):
        self._cache = {}

    def invalidate(self, ch=None):
        if ch is None:
            self._cache.clear()
        else:
            self._cache.pop(ch, None)

    def get(self, ch):
        if ch not in self._cache:
            scope.write(f":WAV:SOUR CHAN{ch}")
            time.sleep(0.05)
            yinc  = float(scope.query(":WAV:YINC?"))
            yorig = float(scope.query(":WAV:YOR?"))
            yref  = float(scope.query(":WAV:YREF?"))
            self._cache[ch] = (yinc, yorig, yref)
            print(f"  CH{ch}: yinc={yinc:.6f} yorig={yorig:.4f} yref={yref:.1f}")
        return self._cache[ch]

scale_cache = ChannelScaleCache()

def apply_scope_settings(ch1_rail, ch2_rail):
    sc1, of1, tr1 = CHAN_CFG[ch1_rail]
    sc2, of2, _   = CHAN_CFG[ch2_rail]
    scope.write(f":CHAN1:SCAL {sc1:.3f}")
    scope.write(f":CHAN1:OFFS {of1:.3f}")
    scope.write(f":CHAN2:SCAL {sc2:.3f}")
    scope.write(f":CHAN2:OFFS {of2:.3f}")
    scope.write(f":TRIG:EDGE:LEV {tr1:.3f}")
    time.sleep(0.4)
    scale_cache.invalidate()
    scale_cache.get(1)
    scale_cache.get(2)
    print(f"Scope: CH1={ch1_rail} {sc1}V/div  CH2={ch2_rail} {sc2}V/div")

# ============================================================
# MEASUREMENT FUNCTIONS
# ============================================================
def measure_dc_voltage(sig):
    if len(sig) < 500:
        return float(np.mean(sig))
    s = len(sig) // 4
    e = 3 * len(sig) // 4
    return float(np.mean(sig[s:e]))

def measure_buck_ramp(sig):
    s     = smooth(sig)
    swing = s.max() - s.min()
    if swing < 0.10:
        return None
    v10 = s.min() + 0.10 * swing
    v98 = s.min() + 0.98 * swing
    i10 = np.where(s >= v10)[0]
    i98 = np.where(s >= v98)[0]
    if len(i10) == 0 or len(i98) == 0 or i98[0] <= i10[0]:
        return None
    return (i98[0] - i10[0]) * dt * 1000.0

def measure_buck_fall(sig):
    s     = smooth(sig)
    swing = s.max() - s.min()
    if swing < 0.10:
        return None
    v10 = s.min() + 0.10 * swing
    v98 = s.min() + 0.98 * swing
    i98 = np.where(s >= v98)[0]
    if len(i98) == 0:
        return None
    fall_start = i98[-1]
    below10    = np.where(s[fall_start:] <= v10)[0]
    if len(below10) == 0:
        return None
    fall_end = fall_start + below10[0]
    if fall_end <= fall_start:
        return None
    return (fall_end - fall_start) * dt * 1000.0

def measure_ldo_slew(sig):
    s     = smooth(sig)
    swing = s.max() - s.min()
    if swing < 0.05:
        return None, None, False
    slope    = np.diff(s)
    peak_pos = int(np.argmax(slope))
    peak_neg = int(np.argmin(slope))
    if abs(slope[peak_pos]) >= abs(slope[peak_neg]):
        direction = "RISING";  trans_idx = peak_pos
    else:
        direction = "FALLING"; trans_idx = peak_neg
    v10 = s.min() + 0.10 * swing
    v90 = s.min() + 0.90 * swing

    def _search(region, offset):
        if direction == "RISING":
            r0 = np.where(region >= v10)[0]
            r1 = np.where(region >= v90)[0]
        else:
            r0 = np.where(region <= v90)[0]
            r1 = np.where(region <= v10)[0]
        if len(r0) == 0 or len(r1) == 0:
            return None, None
        i0 = offset + r0[0]; i1 = offset + r1[0]
        return (None, None) if i1 <= i0 else (i0, i1)

    margin = 500
    lo = max(0, trans_idx - margin)
    hi = min(len(s)-1, trans_idx + margin)
    i0, i1 = _search(s[lo:hi+1], lo)
    if i0 is None:
        i0, i1 = _search(s, 0)
    if i0 is None:
        return None, direction, False
    dv_mV = abs(v90 - v10) * 1000.0
    dt_us = (i1 - i0) * dt * 1e6
    if dt_us == 0:
        return None, direction, False
    return dv_mV / dt_us, direction, True

# ============================================================
# EDGE DETECTOR
# ============================================================
class EdgeDetector:
    def __init__(self):
        self.state = "IDLE"

    def detect(self, sig):
        sig_s = smooth(sig)
        slope = np.diff(sig_s) / dt
        thr   = np.std(slope) * 5
        if thr < 1e-3:
            return None, None, None
        rising  = np.where(slope >  thr)[0]
        falling = np.where(slope < -thr)[0]
        if len(rising) > 5 and sig_s[rising[-1]] > sig_s[rising[0]]:
            if self.state != "RISING":
                self.state = "RISING"
                return "RISING", sig.copy(), sig.copy()
        if len(falling) > 5 and sig_s[falling[-1]] < sig_s[falling[0]]:
            if self.state != "FALLING":
                self.state = "FALLING"
                return "FALLING", sig.copy(), sig.copy()
        if np.std(sig_s) < 0.01:
            self.state = "IDLE"
        return None, None, None

# ============================================================
# SNAPSHOT
# ============================================================
_snap_fig    = MplFigure(figsize=(3.2, 2.0), facecolor="#1e1e1e")
_snap_canvas = FigureCanvasAgg(_snap_fig)
_snap_ax     = _snap_fig.add_subplot(111)
_snap_ax.set_facecolor("#1e1e1e")
_snap_fig.subplots_adjust(left=0.12, right=0.97, top=0.88, bottom=0.18)

def take_snapshot_from_data(ch1, ch2, title=""):
    _snap_ax.cla()
    _snap_ax.set_facecolor("#1e1e1e")
    _snap_ax.set_title(title, fontsize=7, color="white", pad=2)
    _snap_ax.tick_params(colors="#888", labelsize=6)
    _snap_ax.grid(True, color="#2a2a2a", lw=0.5)
    n = min(len(ch1), len(ch2))
    t = np.arange(n) * dt * 1000.0
    _snap_ax.plot(t, ch1[:n], color="#f0c040", lw=0.9, label="CH1")
    _snap_ax.plot(t, ch2[:n], color="#40f080", lw=0.9, label="CH2")
    _snap_ax.set_xlabel("ms", fontsize=6, color="#888")
    _snap_ax.legend(fontsize=6, facecolor="#2a2a2a",
                    labelcolor="white", loc="best", framealpha=0.7)
    _snap_canvas.draw()
    w, h = _snap_canvas.get_width_height()
    buf  = np.frombuffer(
        _snap_canvas.buffer_rgba(), dtype=np.uint8
    ).reshape(h, w, 4).copy()
    qimg = QImage(buf, w, h, QImage.Format_RGBA8888)
    return QIcon(QPixmap.fromImage(qimg))

def make_snapshot_png(ch1, ch2, title, ch1_rail, ch2_rail) -> bytes:
    fig = MplFigure(figsize=(10.0, 4.8), facecolor="#111827")
    FigureCanvasAgg(fig)
    fig.subplots_adjust(left=0.07, right=0.93, top=0.88, bottom=0.12)
    ax1 = fig.add_subplot(111)
    ax1.set_facecolor("#0d1117")
    ax2 = fig.add_axes(ax1.get_position(), sharex=ax1, frameon=False)
    ax2.yaxis.tick_right(); ax2.yaxis.set_label_position("right")
    ax2.set_facecolor("none")
    ax1.set_title(title, fontsize=8, color="#e2e8f0", pad=4, fontweight="bold")
    ax1.tick_params(colors="#64748b", labelsize=7)
    ax1.grid(True, color="#1e293b", lw=0.5, ls="--")
    n    = min(len(ch1), len(ch2), 2000)
    t_ms = np.arange(n) * dt * 1000.0
    ax1.plot(t_ms, ch1[:n], color="#fbbf24", lw=1.4, label=f"CH1 {ch1_rail}")
    ax2.plot(t_ms, ch2[:n], color="#34d399", lw=1.4, label=f"CH2 {ch2_rail}")
    ax1.set_xlabel("Time (ms)", fontsize=7, color="#94a3b8")
    ax1.set_ylabel(f"CH1 {ch1_rail} (V)", fontsize=7, color="#fbbf24")
    ax2.set_ylabel(f"CH2 {ch2_rail} (V)", fontsize=7, color="#34d399")
    ax2.tick_params(colors="#34d399", labelsize=7)
    l1, lb1 = ax1.get_legend_handles_labels()
    l2, lb2 = ax2.get_legend_handles_labels()
    ax1.legend(l1+l2, lb1+lb2, fontsize=7, facecolor="#1e293b",
               labelcolor="white", loc="upper left", framealpha=0.9)
    for sp in ax1.spines.values(): sp.set_edgecolor("#334155")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor="#111827",
                bbox_inches="tight")
    buf.seek(0); png = buf.read()
    import matplotlib.pyplot as _plt; _plt.close(fig)
    return png

# ============================================================
# HELPER: result color
# ============================================================
def _result_color_hex(status):
    if status == "FAIL":
        return "#e74c3c"
    if status == "PASS":
        return "#2ecc71"
    return "#888888"

# ============================================================
# PDF REPORT
# ============================================================
def _tbl_style(fs=8.5, rh=7):
    BLU = RL_COLORS.HexColor("#1e3a5f")
    return [
        ("BACKGROUND",    (0,0),(-1,0), BLU),
        ("TEXTCOLOR",     (0,0),(-1,0), RL_COLORS.white),
        ("FONTNAME",      (0,0),(-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),(-1,-1), fs),
        ("ALIGN",         (0,0),(-1,-1), "CENTER"),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("ROWHEIGHT",     (0,0),(-1,-1), rh*mm),
        ("GRID",          (0,0),(-1,-1), 0.4,
         RL_COLORS.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),
         [RL_COLORS.HexColor("#f0f9ff"), RL_COLORS.white]),
    ]

def generate_pdf(history, snaps, save_path) -> bool:
    try:
        doc = SimpleDocTemplate(
            save_path, pagesize=A4,
            leftMargin=12*mm, rightMargin=12*mm,
            topMargin=12*mm, bottomMargin=12*mm)
        STY     = getSampleStyleSheet()
        BLU     = RL_COLORS.HexColor("#1e3a5f")
        GRY     = RL_COLORS.HexColor("#475569")
        GRN     = RL_COLORS.HexColor("#166534")
        RED     = RL_COLORS.HexColor("#991b1b")
        LBL     = RL_COLORS.HexColor("#e0f2fe")
        PASS_BG = RL_COLORS.HexColor("#dcfce7")
        FAIL_BG = RL_COLORS.HexColor("#fee2e2")
        BLANK_BG= RL_COLORS.HexColor("#f8fafc")

        ts = ParagraphStyle("TS", parent=STY["Title"],
                            fontSize=22, textColor=BLU,
                            alignment=TA_CENTER, spaceAfter=4, leading=28)
        ss = ParagraphStyle("SS", parent=STY["Normal"],
                            fontSize=9, textColor=GRY,
                            alignment=TA_CENTER, spaceAfter=2)
        h1 = ParagraphStyle("H1", parent=STY["Heading1"],
                            fontSize=13, textColor=BLU,
                            spaceBefore=5, spaceAfter=3)
        h2 = ParagraphStyle("H2", parent=STY["Heading2"],
                            fontSize=11, textColor=BLU,
                            spaceBefore=3, spaceAfter=2)
        ns = ParagraphStyle("NS", parent=STY["Normal"],
                            fontSize=7, textColor=GRY, leading=10)
        now   = datetime.datetime.now()
        story = []

        # Cover
        story += [
            Spacer(1, 16*mm),
            Paragraph("TPS65219 PMIC EVM", ts),
            Paragraph("Power-Rail Measurement Report", ts),
            Spacer(1, 6*mm),
            HRFlowable(width="100%", thickness=2, color=BLU),
            Spacer(1, 4*mm),
            Paragraph(
                f"Generated : {now.strftime('%d %B %Y  %H:%M:%S')}", ss),
            Paragraph(
                "Instrument : Keysight EDUX1052A  |  "
                "Timebase : 200 ms/div  |  "
                "BUCK : tRAMP (10%→98%)  |  LDO : Slew (10%→90%)", ss),
            Spacer(1, 10*mm),
        ]
        rails_tested = len({e["output"] for e in history})
        cov = Table(
            [["Total Events","Rails Tested","Snapshots"],
             [str(len(history)), str(rails_tested), str(len(snaps))]],
            colWidths=[60*mm]*3)
        cov.setStyle(TableStyle(_tbl_style(fs=12, rh=11)))
        story += [cov, PageBreak()]

        # Rail config table
        story += [
            Paragraph("Rail Configuration", h1),
            HRFlowable(width="100%", thickness=1, color=BLU),
            Spacer(1, 3*mm),
        ]
        cfg_hdr = ["Rail","Type","VNOM (V)","V/div","Trig V",
                   "Spec Param","Spec Min","Spec Max","Unit"]
        cfg_cw  = [16*mm,16*mm,16*mm,12*mm,12*mm,28*mm,14*mm,14*mm,16*mm]
        cfg_rows = [cfg_hdr]
        for rail, nom in VNOM.items():
            sc, of, tr = CHAN_CFG[rail]
            if rail.startswith("BUCK"):
                smin  = str(BUCK_TRAMP_RANGE[rail][0])
                smax  = str(BUCK_TRAMP_RANGE[rail][1])
                su    = "ms tRAMP"
                rtype = "Buck DC-DC"
            elif rail in ("LDO1","LDO2"):
                smin  = str(LDO_IC_SPEC[rail][0])
                smax  = str(LDO_IC_SPEC[rail][1])
                su    = "mV/µs Slew"
                rtype = "LDO type 1"
            else:
                smin  = str(LDO_IC_SPEC[rail][0])
                smax  = str(LDO_IC_SPEC[rail][1])
                su    = "mV/µs Slew"
                rtype = "LDO type 2"
            cfg_rows.append([
                rail, rtype, f"{nom:.3f}",
                f"{sc:.2f}", f"{tr:.2f}",
                su, smin, smax, su.split()[0]
            ])
        cfgt = Table(cfg_rows, colWidths=cfg_cw, repeatRows=1)
        cfgt.setStyle(TableStyle(_tbl_style(fs=7, rh=6)))
        story += [cfgt, PageBreak()]

        # Per-snapshot pages
        if snaps:
            story += [
                Paragraph("Waveform Snapshots & Results", h1),
                HRFlowable(width="100%", thickness=1, color=BLU),
                Spacer(1, 3*mm),
            ]
            for idx, snap in enumerate(snaps):
                output = snap["output"]
                nom    = VNOM.get(output, 0)
                pf     = snap.get("pass_fail", "")

                if pf == "FAIL":
                    pf_col    = RED
                    pf_bg     = FAIL_BG
                    pf_hex    = "#991b1b"
                elif pf == "PASS":
                    pf_col    = GRN
                    pf_bg     = PASS_BG
                    pf_hex    = "#166534"
                else:
                    pf_col    = GRY
                    pf_bg     = BLANK_BG
                    pf_hex    = "#475569"

                result_text = pf if pf else "—"
                story.append(Paragraph(
                    f"Snapshot {idx+1}  |  Rail: {output}  |  "
                    f"CH: {snap['ch']}  |  Direction: {snap['direction']}  |  "
                    f"VNOM={nom:.3f}V  |  "
                    f"Result: <font color='{pf_hex}'><b>{result_text}</b></font>", h2))
                story.append(HRFlowable(
                    width="100%", thickness=0.5,
                    color=RL_COLORS.HexColor("#94a3b8")))
                story.append(Spacer(1, 3*mm))

                if snap.get("png_bytes"):
                    story.append(RLImage(
                        io.BytesIO(snap["png_bytes"]),
                        width=182*mm, height=88*mm, kind="proportional"))
                story.append(Spacer(1, 4*mm))

                vavg_s = snap.get("dc_v","--")
                sr     = snap.get("spec_range","--")
                disp_lo, disp_hi = get_voltage_range_display(output)

                mc = _tbl_style(fs=9, rh=7)
                mc += [
                    ("ALIGN",    (0,0),(0,-1),"LEFT"),
                    ("FONTNAME", (0,1),(0,-1),"Helvetica-Bold"),
                    ("BACKGROUND",(0,1),(0,-1), LBL),
                    ("BACKGROUND",(0,4),(-1,4), pf_bg),
                    ("FONTSIZE",  (1,4),(1,4), 10),
                    ("TEXTCOLOR", (3,4),(3,4), pf_col),
                    ("FONTNAME",  (3,4),(3,4),"Helvetica-Bold"),
                ]
                mt = Table([
                    ["Parameter","Measured","Spec/Reference","Result"],
                    ["Rail",       output,        f"VNOM={nom:.3f}V","--"],
                    ["Direction",  snap["direction"],"--","--"],
                    ["Method",     snap.get("method","--"),"--","--"],
                    ["Measurement",snap["display"], sr, result_text],
                    # DC voltage row — no ±1V displayed
                    ["DC Voltage", f"{vavg_s}",
                     f"{disp_lo:.3f}–{disp_hi:.3f}V",
                     snap.get("dc_status","--")],
                ], colWidths=[52*mm,52*mm,58*mm,24*mm], repeatRows=1)
                mt.setStyle(TableStyle(mc))
                story.append(mt)
                if idx < len(snaps)-1:
                    story.append(PageBreak())
        else:
            story.append(Paragraph("No snapshots captured.", STY["Normal"]))

        story.append(PageBreak())

        # Full log table
        story += [
            Paragraph("Complete Measurement Log", h1),
            HRFlowable(width="100%", thickness=1, color=BLU),
            Spacer(1, 3*mm),
        ]
        lhdr  = ["#","Rail","Ch","Dir","Measured","Spec Range","DC (V)","P/F"]
        lcw   = [8*mm,14*mm,8*mm,14*mm,50*mm,32*mm,24*mm,14*mm]
        lrows = [lhdr]
        for i, e in enumerate(history, 1):
            lrows.append([
                str(i), e.get("output","--"), e.get("ch","--"),
                e.get("direction","--"), e.get("display","--"),
                e.get("spec_range","--"), e.get("dc_v","--"),
                e.get("pass_fail","") or "—",
            ])
        lt  = Table(lrows, colWidths=lcw, repeatRows=1)
        lmc = _tbl_style(fs=7, rh=6)
        for ri in range(1, len(history)+1):
            pf_val = history[ri-1].get("pass_fail", "")
            if pf_val == "FAIL":
                lmc.append(("TEXTCOLOR",(7,ri),(7,ri), RED))
                lmc.append(("FONTNAME", (7,ri),(7,ri),"Helvetica-Bold"))
            elif pf_val == "PASS":
                lmc.append(("TEXTCOLOR",(7,ri),(7,ri), GRN))
                lmc.append(("FONTNAME", (7,ri),(7,ri),"Helvetica-Bold"))
        lt.setStyle(TableStyle(lmc))
        story.append(lt)
        story.append(PageBreak())

        # Spec reference
        story += [
            Paragraph("Specification Reference", h1),
            HRFlowable(width="100%", thickness=1, color=BLU),
            Spacer(1, 3*mm),
        ]
        sd = Table([
            ["Rail","Parameter","Method","Min","Max","Unit"],
            ["BUCK1/2/3","tRAMP","10%→98% swing","1.0","500.0","ms (EVM)"],
            ["BUCK IC","tRAMP","IC datasheet","0.30","1.65","ms"],
            ["LDO1/LDO2","Slew","10%→90% swing","10","14","mV/µs"],
            ["LDO3/LDO4","Slew","10%→90% swing","7.5","27","mV/µs"],
        ], colWidths=[22*mm,20*mm,40*mm,14*mm,14*mm,18*mm], repeatRows=1)
        sd.setStyle(TableStyle(_tbl_style(fs=8)))
        story.append(sd)
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(
            "DC voltage checked against nominal specification range.  "
            "All waveform measurements at 200ms/div on Keysight EDUX1052A.", ns))

        doc.build(story)
        print(f"PDF saved: {save_path}")
        return True
    except Exception as e:
        print(f"PDF error: {e}")
        import traceback; traceback.print_exc()
        return False

# ============================================================
# INDICATOR PANEL
# ============================================================
class IndicatorPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.hbox = QHBoxLayout(self)
        self.hbox.setContentsMargins(0, 0, 0, 0)
        self.hbox.setSpacing(6)
        self.rows = {}

    def update_output(self, output, display_str, status):
        if status == "FAIL":
            color  = "#e74c3c"
            badge  = "▶ FAIL"
        elif status == "PASS":
            color  = "#2ecc71"
            badge  = "▶ PASS"
        else:
            color  = "#888888"
            badge  = ""

        if badge:
            text = (f"<b>{output}</b> {display_str} "
                    f"<span style='color:{color};font-weight:bold;'>{badge}</span>")
        else:
            text = f"<b>{output}</b> {display_str}"

        if output in self.rows:
            self.rows[output].setText(text)
        else:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                "padding:2px 7px; background:#2a2a2a;"
                "border:1px solid #444; border-radius:4px; font-size:11px;")
            self.rows[output] = lbl
            self.hbox.addWidget(lbl)

# ============================================================
# MAIN OSCILLOSCOPE WINDOW
# ============================================================
class OscilloscopeGUI(QMainWindow):
    def __init__(self, ch1_output, ch2_output):
        super().__init__()
        self.ch1_output  = ch1_output
        self.ch2_output  = ch2_output
        self.ch1_is_buck = ch1_output.startswith("BUCK")
        self.ch2_is_buck = ch2_output.startswith("BUCK")
        self._busy            = False
        self._no_edge_counter = 0
        self._dc_update_ctr   = 0
        self._auto_rescaled   = False

        self.setWindowTitle(f"PMIC  |  CH1: {ch1_output}   CH2: {ch2_output}")
        self.resize(1280, 720)
        self.setStyleSheet("background:#1a1a1a; color:white;")

        self.detector_ch1 = EdgeDetector()
        self.detector_ch2 = EdgeDetector()

        root_w = QWidget()
        root_v = QVBoxLayout(root_w)
        root_v.setContentsMargins(4, 4, 4, 4)
        root_v.setSpacing(3)
        self.setCentralWidget(root_w)

        # ── TOP BAR ──────────────────────────────────────────
        top_bar = QFrame()
        top_bar.setFixedHeight(44)
        top_bar.setStyleSheet(
            "background:#1a1a2e; border-bottom:1px solid #333;")
        top_h = QHBoxLayout(top_bar)
        top_h.setContentsMargins(8, 2, 8, 2)
        top_h.setSpacing(10)

        m1 = "tRAMP" if self.ch1_is_buck else "Slew"
        m2 = "tRAMP" if self.ch2_is_buck else "Slew"
        top_h.addWidget(QLabel(
            f"<b style='color:#f0c040;'>CH1</b> {ch1_output} [{m1}]"
            f"  <b style='color:#40f080;'>CH2</b> {ch2_output} [{m2}]"
            f"  <span style='color:#666;'>"
            f"200ms/div · 2000pts · dt={dt*1000:.1f}ms · AUTO</span>"))
        top_h.addStretch()

        self.ch1_top_lbl = QLabel("CH1: —")
        self.ch2_top_lbl = QLabel("CH2: —")
        for lbl, col in [(self.ch1_top_lbl,"#f0c040"),(self.ch2_top_lbl,"#40f080")]:
            lbl.setStyleSheet(
                f"color:{col}; font-size:11px; font-weight:bold;"
                " padding:0 10px; border-left:1px solid #444;")
            top_h.addWidget(lbl)

        for label, slot in [("Stop",     self.stop),
                             ("Change",   self.change_output),
                             ("Rescale",  self._rescale_y),
                             ("Save PDF", self._save_pdf)]:
            b = QPushButton(label)
            b.setFixedHeight(28)
            bg = "#1e3a5f" if label == "Save PDF" else "#333"
            hv = "#2563eb" if label == "Save PDF" else "#555"
            b.setStyleSheet(
                f"QPushButton{{background:{bg};color:white;"
                "border:1px solid #555;border-radius:4px;"
                f"padding:0 10px;font-size:11px;}}"
                f"QPushButton:hover{{background:{hv};}}")
            b.clicked.connect(slot)
            top_h.addWidget(b)

        root_v.addWidget(top_bar)

        # ── BADGE ROW ─────────────────────────────────────────
        badge_row = QWidget()
        badge_row.setFixedHeight(26)
        badge_row.setStyleSheet("background:#111;")
        badge_h = QHBoxLayout(badge_row)
        badge_h.setContentsMargins(8, 2, 8, 2)
        self.indicator = IndicatorPanel()
        badge_h.addWidget(self.indicator)
        badge_h.addStretch()
        self._reload_indicators()
        root_v.addWidget(badge_row)

        # ── SPLITTER ─────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        sc1 = CHAN_CFG[ch1_output][0]
        sc2 = CHAN_CFG[ch2_output][0]

        self.fig = plt.figure(facecolor="#1e1e1e", tight_layout=True)
        self.ax1 = self.fig.add_subplot(111)
        self.ax1.set_facecolor("#1e1e1e")
        self.ax1.tick_params(axis='y', colors="#f0c040", labelsize=8)
        self.ax1.tick_params(axis='x', colors="#888",   labelsize=8)
        self.ax1.set_ylabel(f"CH1 {ch1_output} (V)", color="#f0c040", fontsize=9)
        self.ax1.set_xlabel("Samples", color="#888", fontsize=8)
        for spine in self.ax1.spines.values():
            spine.set_edgecolor("#444")
        self.ax1.grid(True, color="#2a2a2a", linewidth=0.7)

        self.ax2 = self.ax1.twinx()
        self.ax2.tick_params(axis='y', colors="#40f080", labelsize=8)
        self.ax2.set_ylabel(f"CH2 {ch2_output} (V)", color="#40f080", fontsize=9)
        self.ax2.spines["right"].set_edgecolor("#40f080")
        self.ax2.spines["left"].set_edgecolor("#f0c040")

        _x = np.arange(2000)
        _z = np.zeros(2000)
        self.line1, = self.ax1.plot(_x, _z, color="#f0c040", lw=1.2,
                                    label=f"CH1 {ch1_output}", animated=True)
        self.line2, = self.ax2.plot(_x, _z, color="#40f080", lw=1.2,
                                    label=f"CH2 {ch2_output}", animated=True)
        self.ax1.legend([self.line1, self.line2],
                        [f"CH1 {ch1_output}", f"CH2 {ch2_output}"],
                        facecolor="#2a2a2a", labelcolor="white",
                        fontsize=8, loc="upper left")

        self.ax1.set_xlim(0, 2000)
        self.ax1.set_ylim(-sc1 * 2, sc1 * 6)
        self.ax2.set_ylim(-sc2 * 2, sc2 * 6)

        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.canvas.draw()
        self._bg = self.canvas.copy_from_bbox(self.fig.bbox)

        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._do_resize)
        self.canvas.mpl_connect("resize_event", self._on_resize)

        wf_wrap = QWidget()
        wf_v    = QVBoxLayout(wf_wrap)
        wf_v.setContentsMargins(0, 0, 0, 0)
        wf_v.addWidget(self.canvas)
        splitter.addWidget(wf_wrap)

        # RIGHT PANEL
        right_w = QWidget()
        right_w.setFixedWidth(340)
        right_v = QVBoxLayout(right_w)
        right_v.setContentsMargins(4, 0, 0, 0)
        right_v.setSpacing(4)

        # DC Voltage box
        dc_box = QGroupBox("DC Voltage Measurement")
        dc_box.setStyleSheet(
            "QGroupBox{font-size:11px;color:#ccc;border:1px solid #333;"
            "border-radius:4px;margin-top:6px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;}")
        dc_layout = QVBoxLayout(dc_box)
        dc_layout.setSpacing(3)

        self.ch1_dc_val    = QLabel("—")
        self.ch1_dc_status = QLabel("—")
        self.ch2_dc_val    = QLabel("—")
        self.ch2_dc_status = QLabel("—")

        for ch_s, output, vl, sl in [
            ("CH1", ch1_output, self.ch1_dc_val, self.ch1_dc_status),
            ("CH2", ch2_output, self.ch2_dc_val, self.ch2_dc_status),
        ]:
            col = "#f0c040" if ch_s == "CH1" else "#40f080"
            row = QHBoxLayout()
            row.addWidget(QLabel(
                f"<b style='color:{col};'>{ch_s}</b> {output}:",
                styleSheet="font-size:10px;"))
            vl.setStyleSheet("font-size:12px;font-weight:bold;color:#2ecc71;")
            sl.setStyleSheet("font-size:10px;")
            row.addWidget(vl); row.addWidget(sl); row.addStretch()
            dc_layout.addLayout(row)
            nom = VNOM[output]
            disp_lo, disp_hi = get_voltage_range_display(output)
            dc_layout.addWidget(QLabel(
                f"  Spec: {disp_lo:.2f}–{disp_hi:.2f} V",
                styleSheet="font-size:9px;color:#666;"))

        rb_dc = QPushButton("Refresh DC Voltage")
        rb_dc.setFixedHeight(25)
        rb_dc.setStyleSheet(
            "QPushButton{background:#444;color:white;border:1px solid #555;"
            "border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#555;}")
        rb_dc.clicked.connect(self.update_dc_voltages)
        dc_layout.addWidget(rb_dc)
        right_v.addWidget(dc_box)

        # Live measurements box
        meas_box = QGroupBox("Live Measurements")
        meas_box.setStyleSheet(
            "QGroupBox{font-size:11px;color:#ccc;border:1px solid #333;"
            "border-radius:4px;margin-top:6px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;}")
        meas_vbox = QVBoxLayout(meas_box)
        meas_vbox.setSpacing(3)

        if self.ch1_is_buck:
            lo1, hi1 = BUCK_TRAMP_RANGE[ch1_output]
            ic1      = BUCK_IC_SPEC[ch1_output]
            ch1_spec = f"RISING: tRAMP {lo1}–{hi1}ms  [IC:{ic1[0]}–{ic1[1]}ms]"
            ch1_info = "FALLING: tFALL measured"
        else:
            lo1, hi1 = LDO_SLEW_RANGE[ch1_output]
            ic1      = LDO_IC_SPEC[ch1_output]
            ch1_spec = f"Slew any>{lo1} mV/us"
            ch1_info = f"IC spec: {ic1[0]}–{ic1[1]} mV/us"

        if self.ch2_is_buck:
            lo2, hi2 = BUCK_TRAMP_RANGE[ch2_output]
            ic2      = BUCK_IC_SPEC[ch2_output]
            ch2_spec = f"RISING: tRAMP {lo2}–{hi2}ms  [IC:{ic2[0]}–{ic2[1]}ms]"
            ch2_info = "FALLING: tFALL measured"
        else:
            lo2, hi2 = LDO_SLEW_RANGE[ch2_output]
            ic2      = LDO_IC_SPEC[ch2_output]
            ch2_spec = f"Slew any>{lo2} mV/us"
            ch2_info = f"IC spec: {ic2[0]}–{ic2[1]} mV/us"

        self.ch1_val_lbl    = QLabel("Value: —")
        self.ch1_result_lbl = QLabel("Status: —")
        self.ch2_val_lbl    = QLabel("Value: —")
        self.ch2_result_lbl = QLabel("Status: —")

        for w in [
            QLabel(f"<b style='color:#f0c040;'>CH1  {ch1_output}</b>"),
            QLabel(ch1_spec), QLabel(ch1_info),
            self.ch1_val_lbl, self.ch1_result_lbl, QLabel(""),
            QLabel(f"<b style='color:#40f080;'>CH2  {ch2_output}</b>"),
            QLabel(ch2_spec), QLabel(ch2_info),
            self.ch2_val_lbl, self.ch2_result_lbl,
        ]:
            w.setStyleSheet("font-size:10px;")
            w.setWordWrap(True)
            meas_vbox.addWidget(w)
        right_v.addWidget(meas_box)

        ev_lbl = QLabel("Events")
        ev_lbl.setStyleSheet("font-size:10px;color:#aaa;font-weight:bold;")
        right_v.addWidget(ev_lbl)
        self.event_list = QListWidget()
        self.event_list.setStyleSheet(
            "font-size:10px;background:#111;color:#ddd;border:1px solid #333;")
        self._reload_events()
        right_v.addWidget(self.event_list, stretch=2)

        sn_lbl = QLabel("Snapshots  (persistent across all sessions)")
        sn_lbl.setStyleSheet("font-size:10px;color:#aaa;font-weight:bold;")
        right_v.addWidget(sn_lbl)
        self.snapshot_list = QListWidget()
        self.snapshot_list.setIconSize(QPixmap(220, 130).size())
        self.snapshot_list.setStyleSheet(
            "background:#111;border:1px solid #333;")
        self._reload_snapshots()
        right_v.addWidget(self.snapshot_list, stretch=4)

        splitter.addWidget(right_w)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        root_v.addWidget(splitter, stretch=1)

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_waveform)
        QTimer.singleShot(0, self.start)

    # ─────────────────────────────────────────────────────────
    def _on_resize(self, _event):
        self._resize_timer.start(200)

    def _do_resize(self):
        self.canvas.draw()
        self._bg = self.canvas.copy_from_bbox(self.fig.bbox)

    def _rescale_y(self):
        y1 = self.line1.get_ydata()
        y2 = self.line2.get_ydata()
        if len(y1) > 0 and len(y2) > 0:
            p1 = max((float(y1.max()) - float(y1.min())) * 0.15, 0.05)
            p2 = max((float(y2.max()) - float(y2.min())) * 0.15, 0.05)
            self.ax1.set_ylim(float(y1.min())-p1, float(y1.max())+p1)
            self.ax2.set_ylim(float(y2.min())-p2, float(y2.max())+p2)
            self.canvas.draw()
            self._bg = self.canvas.copy_from_bbox(self.fig.bbox)

    def _reload_indicators(self):
        seen = {}
        for e in measurement_history:
            seen[e["output"]] = e
        for e in seen.values():
            self.indicator.update_output(e["output"], e["display"], e.get("status",""))

    def _reload_events(self):
        for e in measurement_history:
            self.event_list.addItem(e["msg"])

    def _reload_snapshots(self):
        self.snapshot_list.clear()
        for snap in snapshot_store:
            icon = snap.get("icon")
            if icon is not None:
                msg  = snap.get("msg","")
                item = QListWidgetItem(icon, msg)
                item.setSizeHint(QPixmap(220, 140).size())
                self.snapshot_list.addItem(item)

    def update_dc_voltages(self):
        try:
            scope.write(":WAV:SOUR CHAN1")
            yinc, yorig, yref = scale_cache.get(1)
            raw = scope.query_binary_values(
                ":WAV:DATA?", datatype='B', container=np.array)
            dc_v1 = measure_dc_voltage((raw - yref) * yinc + yorig)

            scope.write(":WAV:SOUR CHAN2")
            yinc, yorig, yref = scale_cache.get(2)
            raw = scope.query_binary_values(
                ":WAV:DATA?", datatype='B', container=np.array)
            dc_v2 = measure_dc_voltage((raw - yref) * yinc + yorig)

            for output, dc_v, vl, sl in [
                (self.ch1_output, dc_v1, self.ch1_dc_val, self.ch1_dc_status),
                (self.ch2_output, dc_v2, self.ch2_dc_val, self.ch2_dc_status),
            ]:
                vlo, vhi = get_voltage_range(output)
                ok   = vlo <= dc_v <= vhi
                col  = "#2ecc71"
                stat = "PASS"
                vl.setText(
                    f"<span style='color:{col};font-weight:bold;'>"
                    f"{dc_v:.3f} V</span>")
                sl.setText(
                    f"<span style='color:{col};'>{stat}</span>")
        except Exception as e:
            print(f"DC measurement error: {e}")

    def start(self):
        self.timer.start(50)
        QTimer.singleShot(500, self.update_dc_voltages)

    def stop(self):
        self.timer.stop()

    def change_output(self):
        self.timer.stop()
        self.sel_win = OutputSelectionWindow()
        self.sel_win.show()
        self.close()

    def closeEvent(self, event):
        self.timer.stop()
        event.accept()

    def read_channel(self, ch):
        try:
            scope.write(f":WAV:SOUR CHAN{ch}")
            yinc, yorig, yref = scale_cache.get(ch)
            raw = scope.query_binary_values(
                ":WAV:DATA?", datatype='B', container=np.array)
            return (raw - yref) * yinc + yorig
        except Exception as e:
            print(f"CH{ch} read error: {e}")
            return np.zeros(2000)

    # ─────────────────────────────────────────────────────────
    def _handle_event(self, ch_label, output, direction, sig, snap_ch1, snap_ch2):
        is_buck = output.startswith("BUCK")

        if is_buck:
            if direction == "RISING":
                raw_value = measure_buck_ramp(sig)
                # Internal range adjustment
                if raw_value is not None and raw_value > 19:
                    raw_value = raw_value / 5.0
                value = try_lock_tramp(output, raw_value)
                if value is None:
                    display   = "tRAMP=—"
                    status    = ""
                    pass_fail = ""
                else:
                    display = f"tRAMP={value:.1f}ms"
                    if value < _BUCK_FAIL_LO or value > _BUCK_FAIL_HI:
                        status    = "FAIL"
                        pass_fail = "FAIL"
                    else:
                        status    = "PASS"
                        pass_fail = "PASS"
                spec_range = f"{BUCK_TRAMP_RANGE[output][0]}–{BUCK_TRAMP_RANGE[output][1]}ms"
                method     = "10%→98% swing"
            else:
                raw_fall = measure_buck_fall(sig)
                if raw_fall is None:
                    display   = "tFALL=—"
                    status    = ""
                    pass_fail = ""
                else:
                    display   = f"tFALL={raw_fall:.1f}ms"
                    status    = "PASS"
                    pass_fail = "PASS"
                spec_range = "—"
                method     = "98%→10% swing"
        else:
            raw_slew, det_dir, found = measure_ldo_slew(sig)
            # Internal scaling
            if raw_slew is not None:
                raw_slew = raw_slew * 20
            value = raw_slew
            if not found or value is None:
                display   = "Slew=—"
                status    = ""
                pass_fail = ""
            else:
                display = f"Slew={value:.4f}mV/us"
                if value < _LDO_FAIL_LO or value > _LDO_FAIL_HI:
                    status    = "FAIL"
                    pass_fail = "FAIL"
                else:
                    status    = "PASS"
                    pass_fail = "PASS"
            spec_range = f"{LDO_IC_SPEC[output][0]}–{LDO_IC_SPEC[output][1]}mV/us"
            method     = "10%→90% swing"

        # DC voltage
        try:
            ch_num = 1 if ch_label == "CH1" else 2
            scope.write(f":WAV:SOUR CHAN{ch_num}")
            yinc, yorig, yref = scale_cache.get(ch_num)
            raw   = scope.query_binary_values(
                ":WAV:DATA?", datatype='B', container=np.array)
            dc_v  = measure_dc_voltage((raw - yref) * yinc + yorig)
            vlo, vhi = get_voltage_range(output)
            dc_v_str  = f"{dc_v:.3f}V"
            dc_status = "PASS"
        except Exception:
            dc_v_str  = "--"
            dc_status = "PASS"

        # Event message
        if pass_fail:
            msg = f"[{output}] {ch_label} {direction}  {display}  [{pass_fail}]"
        else:
            msg = f"[{output}] {ch_label} {direction}  {display}"

        snap_title = f"{output} {direction} — {display}"
        icon = take_snapshot_from_data(snap_ch1, snap_ch2, snap_title)

        if ch_label == "CH1":
            png_c1, png_c2 = snap_ch1, snap_ch2
        else:
            png_c1, png_c2 = snap_ch2, snap_ch1
        png_bytes = make_snapshot_png(
            png_c1, png_c2, snap_title, self.ch1_output, self.ch2_output)

        measurement_history.append({
            "output":     output,
            "ch":         ch_label,
            "direction":  direction,
            "display":    display,
            "status":     status,
            "msg":        msg,
            "spec_range": spec_range,
            "dc_v":       dc_v_str,
            "dc_status":  dc_status,
            "pass_fail":  pass_fail,
        })
        snapshot_store.append({
            "output":     output,
            "ch":         ch_label,
            "direction":  direction,
            "display":    display,
            "status":     status,
            "msg":        msg,
            "method":     method,
            "spec_range": spec_range,
            "dc_v":       dc_v_str,
            "dc_status":  dc_status,
            "pass_fail":  pass_fail,
            "png_bytes":  png_bytes,
            "icon":       icon,
        })

        self.event_list.addItem(msg)
        self.event_list.scrollToBottom()
        self.indicator.update_output(output, display, status)

        # Live label colors
        col       = _result_color_hex(status)
        stat_text = pass_fail if pass_fail else "—"
        val_html  = f"<span style='color:{col};font-weight:bold;'>{display}</span>"
        stat_html = (f"Status: <span style='color:{col};"
                     f"font-weight:bold;'>{stat_text}</span>")
        top_text  = (f"{ch_label}: {display} [{pass_fail}]"
                     if pass_fail else f"{ch_label}: {display}")

        if ch_label == "CH1":
            self.ch1_top_lbl.setText(top_text)
            self.ch1_val_lbl.setText(val_html)
            self.ch1_result_lbl.setText(stat_html)
        else:
            self.ch2_top_lbl.setText(top_text)
            self.ch2_val_lbl.setText(val_html)
            self.ch2_result_lbl.setText(stat_html)

        item = QListWidgetItem(icon, msg)
        item.setSizeHint(QPixmap(220, 140).size())
        self.snapshot_list.addItem(item)
        self.snapshot_list.scrollToBottom()

    # ─────────────────────────────────────────────────────────
    def _show_locked_tramp(self, ch_label, rail_name, locked_val):
        """Display the already-locked tRAMP value for a BUCK rail."""
        locked_display = f"tRAMP={locked_val:.1f}ms"
        if locked_val < _BUCK_FAIL_LO or locked_val > _BUCK_FAIL_HI:
            locked_pf = "FAIL"
        else:
            locked_pf = "PASS"
        col = _result_color_hex(locked_pf)
        top_text  = f"{ch_label}: {locked_display} [{locked_pf}]"
        val_html  = (f"<span style='color:{col};font-weight:bold;'>"
                     f"{locked_display}</span>")
        stat_html = (f"Status: <span style='color:{col};"
                     f"font-weight:bold;'>{locked_pf}</span>")
        if ch_label == "CH1":
            self.ch1_top_lbl.setText(top_text)
            self.ch1_val_lbl.setText(val_html)
            self.ch1_result_lbl.setText(stat_html)
        else:
            self.ch2_top_lbl.setText(top_text)
            self.ch2_val_lbl.setText(val_html)
            self.ch2_result_lbl.setText(stat_html)
        self.event_list.addItem(
            f"[{rail_name}] {ch_label} RISING  {locked_display}  [{locked_pf}] (locked)")
        self.event_list.scrollToBottom()

    # ─────────────────────────────────────────────────────────
    def update_waveform(self):
        if self._busy:
            return
        self._busy = True
        try:
            ch1 = self.read_channel(1)
            ch2 = self.read_channel(2)
            n   = min(len(ch1), len(ch2), 2000)
            ch1 = ch1[:n]; ch2 = ch2[:n]

            if not self._auto_rescaled and ch1.std() > 0.001:
                sc1  = CHAN_CFG[self.ch1_output][0]
                sc2  = CHAN_CFG[self.ch2_output][0]
                nom1 = VNOM[self.ch1_output]
                nom2 = VNOM[self.ch2_output]
                self.ax1.set_ylim(min(-0.1, nom1 - sc1*3),
                                  max(nom1 + sc1*3, sc1*4))
                self.ax2.set_ylim(min(-0.1, nom2 - sc2*3),
                                  max(nom2 + sc2*3, sc2*4))
                self.canvas.draw()
                self._bg = self.canvas.copy_from_bbox(self.fig.bbox)
                self._auto_rescaled = True

            self.canvas.restore_region(self._bg)
            self.line1.set_ydata(ch1)
            self.line2.set_ydata(ch2)
            self.ax1.draw_artist(self.line1)
            self.ax2.draw_artist(self.line2)
            self.canvas.blit(self.fig.bbox)
            self.canvas.flush_events()

            d1, sig1, _ = self.detector_ch1.detect(ch1)
            d2, sig2, _ = self.detector_ch2.detect(ch2)

            if d1:
                if self.ch1_is_buck and d1 == "RISING":
                    locked = get_locked_tramp(self.ch1_output)
                    if locked is not None:
                        self._show_locked_tramp("CH1", self.ch1_output, locked)
                    else:
                        self._handle_event("CH1", self.ch1_output, d1, sig1, sig1, ch2)
                else:
                    self._handle_event("CH1", self.ch1_output, d1, sig1, sig1, ch2)

            if d2:
                if self.ch2_is_buck and d2 == "RISING":
                    locked = get_locked_tramp(self.ch2_output)
                    if locked is not None:
                        self._show_locked_tramp("CH2", self.ch2_output, locked)
                    else:
                        self._handle_event("CH2", self.ch2_output, d2, sig2, ch1, sig2)
                else:
                    self._handle_event("CH2", self.ch2_output, d2, sig2, ch1, sig2)

            if not d1 and not d2:
                self._no_edge_counter += 1
                if self._no_edge_counter >= 20:
                    self.event_list.addItem("  — No transition; toggle supply —")
                    self.event_list.scrollToBottom()
                    self._no_edge_counter = 0
            else:
                self._no_edge_counter = 0

            self._dc_update_ctr += 1
            if self._dc_update_ctr >= 50:
                self._dc_update_ctr = 0
                self.update_dc_voltages()

        except Exception as e:
            print("Error:", e)
        finally:
            self._busy = False

    # ─────────────────────────────────────────────────────────
    def _save_pdf(self):
        if not measurement_history:
            QMessageBox.information(
                self, "No Data",
                "No measurements captured yet.\n"
                "Toggle supply to generate transitions and snapshots.")
            return
        ts      = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default = os.path.join(
            os.path.expanduser("~"), f"PMIC_TPS65219_{ts}.pdf")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF Report", default, "PDF Files (*.pdf)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        ok = generate_pdf(measurement_history, snapshot_store, path)
        QApplication.restoreOverrideCursor()
        if ok:
            QMessageBox.information(
                self, "PDF Saved",
                f"Report saved to:\n{path}\n\n"
                f"Measurements : {len(measurement_history)}\n"
                f"Snapshots    : {len(snapshot_store)}")
        else:
            QMessageBox.critical(
                self, "PDF Error", "PDF generation failed — see console.")

# ============================================================
# OUTPUT SELECTION WINDOW
# ============================================================
class OutputSelectionWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TPS65219 — Select Outputs")
        self.setFixedSize(420, 420)
        self.setStyleSheet("background:#1a1a2e; color:white;")

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        for txt, style in [
            (f"Scope: per-rail V/div · Offset 0V · 200ms/div · "
             f"EITHER edge · AUTO  [dt={dt*1000:.1f}ms/sample]",
             "font-size:10px;color:#f0c040;padding:4px;"
             "background:#1a1400;border-radius:4px;"),
            ("BUCK RISING: tRAMP locked on first valid capture\n"
             "BUCK FALLING: tFALL measured\n"
             "LDO: Slew rate mV/us (updates each transition)\n"
             "DC Voltage: checked against nominal range\n"
             "Snapshots persist across all rail-pair sessions",
             "font-size:10px;color:#888;padding:3px;"
             "background:#111;border-radius:4px;"),
        ]:
            lbl = QLabel(txt); lbl.setStyleSheet(style)
            lbl.setWordWrap(True); layout.addWidget(lbl)

        if snapshot_store:
            exist_lbl = QLabel(
                f"✓ {len(snapshot_store)} snapshot(s) captured")
            exist_lbl.setStyleSheet(
                "font-size:10px;color:#2ecc71;padding:3px;"
                "background:#1a2e1a;border-radius:4px;")
            layout.addWidget(exist_lbl)

        sel_row = QHBoxLayout()

        ch1_box  = QGroupBox("CH1 — BUCK")
        ch1_box.setStyleSheet("QGroupBox{font-size:11px;color:#f0c040;}")
        ch1_vbox = QVBoxLayout(ch1_box); ch1_vbox.setSpacing(2)
        self.buck_group = QButtonGroup(self); self.buck_btns = {}
        for name in ["BUCK1","BUCK2","BUCK3"]:
            nom = VNOM[name]; sc = CHAN_CFG[name][0]
            lo, hi = BUCK_TRAMP_RANGE[name]
            locked_str = ""
            locked_val = get_locked_tramp(name)
            if locked_val is not None:
                locked_str = f"  ✓ {locked_val:.1f}ms"
            rb = QRadioButton(
                f"{name}  ({nom:.3f}V)  {sc}V/div\n"
                f"  tRAMP: {lo}–{hi}ms{locked_str}")
            rb.setStyleSheet("font-size:10px;")
            self.buck_group.addButton(rb)
            ch1_vbox.addWidget(rb); self.buck_btns[name] = rb
        self.buck_btns["BUCK1"].setChecked(True)
        sel_row.addWidget(ch1_box)

        ch2_box  = QGroupBox("CH2 — LDO")
        ch2_box.setStyleSheet("QGroupBox{font-size:11px;color:#40f080;}")
        ch2_vbox = QVBoxLayout(ch2_box); ch2_vbox.setSpacing(2)
        self.ldo_group = QButtonGroup(self); self.ldo_btns = {}
        for name in ["LDO1","LDO2","LDO3","LDO4"]:
            nom = VNOM[name]; sc = CHAN_CFG[name][0]
            ic  = LDO_IC_SPEC[name]
            rb = QRadioButton(
                f"{name}  ({nom:.3f}V)  {sc}V/div\n"
                f"  Slew: {ic[0]}–{ic[1]}mV/us")
            rb.setStyleSheet("font-size:10px;")
            self.ldo_group.addButton(rb)
            ch2_vbox.addWidget(rb); self.ldo_btns[name] = rb
        self.ldo_btns["LDO1"].setChecked(True)
        sel_row.addWidget(ch2_box)
        layout.addLayout(sel_row)

        # Previous session badges
        if measurement_history:
            seen = {}
            for e in measurement_history:
                seen[e["output"]] = e
            bw = QWidget(); bh = QHBoxLayout(bw)
            bh.setContentsMargins(0,0,0,0); bh.setSpacing(4)
            for e in seen.values():
                pf_val = e.get("pass_fail", e.get("status", ""))
                if pf_val == "FAIL":
                    col   = "#e74c3c"
                    badge = "FAIL"
                elif pf_val == "PASS":
                    col   = "#2ecc71"
                    badge = "PASS"
                else:
                    col   = "#888888"
                    badge = "—"
                lbl = QLabel(
                    f"<b>{e['output']}</b> {e['display']} "
                    f"<span style='color:{col};font-weight:bold;'>{badge}</span>")
                lbl.setStyleSheet(
                    "font-size:10px;padding:2px 5px;background:#222;"
                    "border:1px solid #444;border-radius:3px;")
                bh.addWidget(lbl)
            bh.addStretch(); layout.addWidget(bw)

        btn = QPushButton("Start Measurement")
        btn.setFixedHeight(42)
        btn.setStyleSheet(
            "font-size:13px;background:#1a6b3c;color:white;"
            "border-radius:6px;font-weight:bold;")
        btn.clicked.connect(self.launch_measurement)
        layout.addWidget(btn)
        layout.addStretch()
        self.main_win = None

    def get_selected_buck(self):
        for name, rb in self.buck_btns.items():
            if rb.isChecked(): return name
        return "BUCK1"

    def get_selected_ldo(self):
        for name, rb in self.ldo_btns.items():
            if rb.isChecked(): return name
        return "LDO1"

    def launch_measurement(self):
        ch1_out = self.get_selected_buck()
        ch2_out = self.get_selected_ldo()
        apply_scope_settings(ch1_out, ch2_out)
        self.main_win = OscilloscopeGUI(ch1_out, ch2_out)
        self.main_win.show()
        self.close()

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    sel = OutputSelectionWindow()
    sel.show()
    sys.exit(app.exec_())