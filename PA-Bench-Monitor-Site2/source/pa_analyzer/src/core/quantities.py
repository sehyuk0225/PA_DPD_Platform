"""Registry of measurable quantities and measurement-condition fields.

Everything that the parser must recognise in an Excel/CSV header lives here,
so adding a new quantity (e.g. ACLR, EVM, AM-PM) is a one-line change.

Header matching works on a *normalised* key: upper-case, bracketed unit
removed, all non-alphanumerics removed.  e.g. "VGS,CA (V)" -> "VGSCA",
"Pout [dBm]" -> "POUT", "PAE(%)" -> "PAE".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------
_BRACKETS = re.compile(r"[\(\[\{].*?[\)\]\}]")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")

# Unit suffixes that may be glued to a header, e.g. "Pout_dBm", "Gain_dB".
# Only tried when the plain key does not match anything (see match_alias).
UNIT_SUFFIXES = ("DBM", "DB", "PCT", "PERCENT", "GHZ", "MHZ", "KHZ", "HZ",
                 "MA", "W", "V", "A", "DEGC", "C")


_KOREAN = {"주파수": "FREQ", "이득": "GAIN", "출력전력": "POUT", "출력": "POUT", "입력전력": "PIN", "입력": "PIN",
           "효율": "PAE", "드레인효율": "DE"}


def normalize_key(text: object) -> str:
    """Normalise a header / label for alias matching (Korean names like '주파수' too)."""
    if text is None:
        return ""
    s = str(text).strip()
    for ko, en in sorted(_KOREAN.items(), key=lambda kv: -len(kv[0])):
        if s.replace(" ", "").startswith(ko):
            s = en + s.replace(" ", "")[len(ko):]
            break
    s = s.upper()
    s = _BRACKETS.sub("", s)
    s = s.replace("%", "")
    return _NON_ALNUM.sub("", s)


def unit_hint(text: object) -> str:
    """Return the unit written in a header such as 'Pout (dBm)' or 'Freq_MHz'."""
    if text is None:
        return ""
    s = str(text).upper()
    m = re.search(r"[\(\[\{]\s*([^\)\]\}]*)\s*[\)\]\}]", s)
    if m:
        return m.group(1).strip()
    for u in ("GHZ", "MHZ", "KHZ", "DBM", "DB", "%"):
        if s.rstrip().endswith(u):
            return u
    return ""


# --------------------------------------------------------------------------
# Quantities (data columns)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Quantity:
    key: str                 # internal id, e.g. "pout"
    label: str               # axis label text, e.g. "Pout"
    unit: str                # e.g. "dBm"
    aliases: Tuple[str, ...]  # normalised header aliases
    fmt: str = "{:.2f}"      # tooltip format

    @property
    def axis_label(self) -> str:
        return f"{self.label} ({self.unit})" if self.unit else self.label


QUANTITIES: Dict[str, Quantity] = {}


def register_quantity(q: Quantity) -> Quantity:
    QUANTITIES[q.key] = q
    return q


POUT = register_quantity(Quantity(
    "pout", "Pout", "dBm",
    ("DUTPOUT", "POUTDUT", "POUT", "PO", "OUTPUTPOWER", "POUTPUT", "POUTAVG", "PAVGOUT", "PLOAD", "PL")))
PAE = register_quantity(Quantity(
    "pae", "PAE", "%",
    ("PAE", "POWERADDEDEFFICIENCY", "EFFPAE", "PAEPCT")))
GAIN = register_quantity(Quantity(
    "gain", "Gain", "dB",
    ("GAIN", "GT", "GP", "POWERGAIN", "TRANSDUCERGAIN", "GAINDB")))
PIN = register_quantity(Quantity(
    "pin", "Pin", "dBm",
    ("DUTPIN", "PINDUT", "PIN", "PINPUT", "INPUTPOWER", "PAVS", "PAVAIL", "PINAVG")))
DE = register_quantity(Quantity(
    "de", "Drain Eff.", "%",
    ("DE", "DRAINEFF", "DRAINEFFICIENCY", "EFF", "EFFICIENCY", "ETA", "ETAD")))
IDD = register_quantity(Quantity(
    "idd", "Idd", "mA",
    ("IDD", "ID", "IDC", "ICC", "IDRAIN", "CURRENT", "IDDTOTAL")))
ACLR = register_quantity(Quantity(
    "aclr", "ACLR", "dBc",
    ("ACLR", "ACPR", "ACLRL", "ACLRLOW")))
EVM = register_quantity(Quantity(
    "evm", "EVM", "%", ("EVM", "EVMRMS")))
# raw-data columns that must NOT be taken for Pout / Pin (instrument readings, mW values)
SG_POUT = register_quantity(Quantity("sg_pout", "SG Pout", "dBm", ("SGPOUT", "SGOUT", "SGPOWER", "SGLEVEL")))
SA_PIN = register_quantity(Quantity("sa_pin", "SA Pin", "dBm", ("SAPIN", "SAPOWER", "SAIN")))
POUT_MW = register_quantity(Quantity("pout_mw", "Pout", "mW", ("POUTMW",)))
PIN_MW = register_quantity(Quantity("pin_mw", "Pin", "mW", ("PINMW",)))
PDC = register_quantity(Quantity("pdc", "DC Power", "mW", ("DCPOWER", "PDC", "PDCTOTAL")))
PDC_MAIN = register_quantity(Quantity("pdc_main", "DC Power Main", "mW", ("DCPOWERMAIN", "PDCMAIN")))
IDC_DRV = register_quantity(Quantity("idc_drv", "IDC Driver", "mA", ("IDCDRIVER", "IDCDRV", "IDDRV", "IDRIVER")))
IDC_CA = register_quantity(Quantity("idc_ca", "IDC CA", "mA", ("IDCCA", "IDCMAIN", "IDCA", "IDMAIN")))
IDC_PK = register_quantity(Quantity("idc_pk", "IDC PK", "mA", ("IDCPK", "IDCPEAK", "IDPK", "IDPEAK")))
AMPM = register_quantity(Quantity(
    "ampm", "AM-PM", "deg", ("AMPM", "PHASE", "S21PHASE", "PHASEDEV")))


# --------------------------------------------------------------------------
# Condition fields (scalar per dataset)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ConditionField:
    key: str
    label: str               # display label, e.g. "VGS,CA"
    unit: str
    aliases: Tuple[str, ...]
    numeric: bool = True
    always_show: bool = False
    fmt: str = "{:.2f}"


CONDITION_FIELDS: Dict[str, ConditionField] = {}


def register_condition(c: ConditionField) -> ConditionField:
    CONDITION_FIELDS[c.key] = c
    return c


register_condition(ConditionField(
    "freq", "FREQ", "GHz", ("FREQ", "FREQUENCY", "FRQ", "FC", "CENTERFREQ", "RFFREQ"),
    always_show=True))
register_condition(ConditionField(
    "vgs_drv", "VG_DRV", "V",
    ("VGSDRV", "VGDRV", "VGSDRIVER", "VGDRIVER", "VGSDR", "VGSD1", "VGSDRIVE"),
    always_show=True))
register_condition(ConditionField(
    "vgs_ca", "VG_CA", "V",
    ("VGSCA", "VGCA", "VGSCARRIER", "VGSMAIN", "VGMAIN", "VGSC", "VGSMN"),
    always_show=True))
register_condition(ConditionField(
    "vgs_pk", "VG_PK", "V",
    ("VGSPK", "VGPK", "VGSPEAK", "VGSPEAKING", "VGPEAK", "VGSP", "VGSAUX"),
    always_show=True))
register_condition(ConditionField(
    "iq_drv", "IQ_DRV", "mA", ("IQDRV", "IQDRIVER", "IDQDRV", "IQD"), fmt="{:.1f}"))
register_condition(ConditionField(
    "iq_ca", "IQ_CA", "mA", ("IQCA", "IQMAIN", "IDQCA", "IDQMAIN"), fmt="{:.1f}"))
register_condition(ConditionField(
    "iq_pk", "IQ_PK", "mA", ("IQPK", "IQPEAK", "IDQPK"), fmt="{:.1f}"))
# ---- optional / future condition fields -----------------------------------
register_condition(ConditionField("vdd", "VDD", "V", ("VDD", "VD", "VDS", "VDRAIN", "VDDMAIN", "VDMAIN")))
register_condition(ConditionField("duty", "Pulse Duty", "%", ("PULSEDUTY", "DUTY", "DUTYCYCLE"), fmt="{:.1f}"))
register_condition(ConditionField("in_loss", "Input Loss", "dB", ("INPUTLOSS", "INLOSS", "LOSSIN")))
register_condition(ConditionField("out_loss", "Output Loss", "dB", ("OUTPUTLOSS", "OUTLOSS", "LOSSOUT")))
register_condition(ConditionField("vcc", "VCC", "V", ("VCC",)))
register_condition(ConditionField("vgg", "VGG", "V", ("VGG",)))
register_condition(ConditionField(
    "temp", "Temp", "°C", ("TEMP", "TEMPERATURE", "TA", "TAMB", "TCASE"), fmt="{:.1f}"))
register_condition(ConditionField("mode", "Mode", "", ("MODE",), numeric=False))
register_condition(ConditionField("bias", "Bias", "", ("BIAS", "BIASCONDITION"), numeric=False))
register_condition(ConditionField("load", "Load", "", ("LOAD", "LOADCONDITION", "ZL"), numeric=False))
register_condition(ConditionField("pin_cond", "Pin Cond.", "", ("INPUTPOWERCONDITION", "PINCONDITION"), numeric=False))
register_condition(ConditionField("dut", "DUT", "", ("DUT", "DEVICE", "SN", "SERIAL"), numeric=False))
register_condition(ConditionField("board", "Board", "", ("BOARD", "EVB", "PCB"), numeric=False))
register_condition(ConditionField("lot", "Lot", "", ("LOT", "LOTNO", "WAFER"), numeric=False))
register_condition(ConditionField("date", "Date", "", ("DATE", "MEASDATE"), numeric=False))

ALWAYS_SHOWN_CONDITIONS = tuple(k for k, c in CONDITION_FIELDS.items() if c.always_show)
# what the Measurement Condition panel / plot header shows by default (user-selectable)
DEFAULT_DISPLAY_CONDITIONS = ("vgs_drv", "vgs_ca", "vgs_pk", "iq_drv", "iq_ca")


# --------------------------------------------------------------------------
# Alias matching
# --------------------------------------------------------------------------
def _build_index(registry) -> Dict[str, str]:
    idx: Dict[str, str] = {}
    for key, item in registry.items():
        for a in item.aliases:
            idx.setdefault(a, key)
    return idx


_Q_INDEX = _build_index(QUANTITIES)
_C_INDEX = _build_index(CONDITION_FIELDS)


def _match(text: object, index: Dict[str, str]) -> Optional[str]:
    key = normalize_key(text)
    if not key:
        return None
    if key in index:
        return index[key]
    for suf in UNIT_SUFFIXES:
        if key.endswith(suf) and len(key) > len(suf):
            base = key[: -len(suf)]
            if base in index:
                return index[base]
    return None


def match_quantity(text: object) -> Optional[str]:
    """Return the quantity key for a header text, or None.

    Unit-aware: 'Pout(mW)' is NOT Pout in dBm -> 'pout_mw'.
    """
    q = _match(text, _Q_INDEX)
    if q in ("pout", "pin"):
        u = unit_hint(text).replace(" ", "")
        raw = str(text).upper().replace(" ", "")
        if u in ("MW", "W") or raw.endswith(("MW", "MW)")) and "DBM" not in raw:
            return q + "_mw"
    return q


def header_priority(text: object) -> int:
    """Higher = better column when several match the same quantity
    ('DUT_Pout (dBm)' beats a plain 'Pout')."""
    key = normalize_key(text)
    raw = str(text or "").upper()
    return (2 if key.startswith("DUT") or key.endswith("DUT") else 0) + (1 if "DBM" in raw else 0)


def match_condition(text: object) -> Optional[str]:
    """Return the condition-field key for a label text, or None."""
    return _match(text, _C_INDEX)
